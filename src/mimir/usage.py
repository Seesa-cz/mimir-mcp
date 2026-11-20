"""Usage log — one JSONL record per MCP tool call, written at the call_tool level.

Records are written by `TrackedFastMCP.call_tool`, so every call is logged — including
argument validation errors and unknown tools, which never reach the tool function.
Tools add call-specific fields (search hits, cold start) with `note()`.

usage_log_file may contain placeholders:
  {host}  — hostname, so several machines can write into one synced directory
  {month} — YYYY-MM; the log then switches to a new file every month and is never
            rotated or deleted (without {month}: size rotation by max_log_mb/max_log_files)

usage_log_detail=full also writes full arguments and responses to
<usage_log_file stem>.payloads.jsonl, linked to the main record by `req`.
"""

from __future__ import annotations
import contextvars
import json
import logging
import re
import socket
import time
import uuid
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import ValidationError

from . import version_check

SCHEMA_VERSION = 2
ARG_MAX_CHARS = 300      # longer string args are truncated in the main log; full length goes to args_len
SUMMARY_CHARS = 150

usage_logger = logging.getLogger("mimir.usage")
payload_logger = logging.getLogger("mimir.usage.payloads")

_extra: contextvars.ContextVar[dict | None] = contextvars.ContextVar("mimir_usage_extra", default=None)
_host = socket.gethostname()

# Status of a tool call derived from its returned text (tools report failures as text,
# not exceptions). First match wins; checked against the text without notice prefixes.
_STATUS_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("error", re.compile(r"Error:")),
    ("denied", re.compile(r".*outside write_dir")),
    ("not_found", re.compile(r"Note not found|String not found|Category '.*' not found")),
    ("ambiguous", re.compile(r"Ambiguous:")),
    ("conflict", re.compile(r"Note already exists|.*already exists\.")),
    ("busy", re.compile(r"Sync already in progress")),
    ("empty", re.compile(r"Knowledge base is empty|No notes found|No tags yet|No categories found")),
    ("invalid_args", re.compile(
        r"Unknown category|Path must end with \.md|limit must be|\[offset .* beyond the end"
        r"|New task always starts|Source and target are the same|Category '.*' has \d+ note\(s\)\. Use force"
    )),
]
_NOTICE_RE = re.compile(r"^(\[[^\n]*\]\n\n)+")


def note(**fields: Any) -> None:
    """Add fields to the usage record of the current tool call (no-op outside a call)."""
    extra = _extra.get()
    if extra is not None:
        extra.update(fields)


def classify(text: str) -> str:
    """Map a tool's returned text to a status."""
    body = _NOTICE_RE.sub("", text, count=1)
    for status, pattern in _STATUS_PATTERNS:
        if pattern.match(body):
            return status
    return "ok"


def _result_text(result: Any) -> str:
    """Extract the text the client receives from a converted FastMCP tool result."""
    if isinstance(result, tuple):          # (unstructured content, structured content)
        result = result[0]
    content = getattr(result, "content", result)   # CallToolResult
    if isinstance(content, (list, tuple)):
        return "".join(getattr(block, "text", "") for block in content)
    return str(content)


def _short_args(arguments: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
    """Truncate long string arguments; return (args, {name: full length})."""
    args, lengths = {}, {}
    for name, value in arguments.items():
        if isinstance(value, str) and len(value) > ARG_MAX_CHARS:
            args[name] = value[:ARG_MAX_CHARS]
            lengths[name] = len(value)
        else:
            args[name] = value
    return args, lengths


def _client_fields(mcp: FastMCP) -> dict[str, Any]:
    """Session id and client name/version of the current request, where known."""
    out: dict[str, Any] = {}
    try:
        rc = mcp.get_context().request_context
    except (LookupError, ValueError):
        return out
    request = getattr(rc, "request", None)
    if request is not None and hasattr(request, "headers"):
        sid = request.headers.get("mcp-session-id")
        if sid:
            out["session"] = sid
    params = getattr(rc.session, "client_params", None)
    info = getattr(params, "clientInfo", None)
    if info is not None:
        out["client"] = f"{info.name}/{info.version}"
    return out


def _write(record: dict) -> None:
    usage_logger.info(json.dumps(record, ensure_ascii=False, default=str))


class TrackedFastMCP(FastMCP):
    """FastMCP that writes one usage record per tool call."""

    usage_detail: str = "summary"

    async def call_tool(self, name: str, arguments: dict[str, Any]):
        t0 = time.monotonic()
        extra: dict[str, Any] = {}
        token = _extra.set(extra)
        record: dict[str, Any] = {
            "v": SCHEMA_VERSION,
            "ts": datetime.now().isoformat(timespec="seconds"),
            "req": uuid.uuid4().hex[:12],   # unique; JSON-RPC ids restart in every session
            "host": _host,
            "server": version_check.status().get("version"),
            **_client_fields(self),
            "tool": name,
        }
        args, lengths = _short_args(arguments or {})
        record["args"] = args
        if lengths:
            record["args_len"] = lengths
        text = None
        try:
            result = await super().call_tool(name, arguments)
            text = _result_text(result)
            record["status"] = classify(text)
            record["resp_chars"] = len(text)
            return result
        except ToolError as e:
            cause = e.__cause__
            if self._tool_manager.get_tool(name) is None:
                record["status"] = "unknown_tool"
            elif isinstance(cause, ValidationError):
                record["status"] = "invalid_args"
            else:
                record["status"] = "error"
            record["error"] = str(cause or e)[:500]
            raise
        finally:
            _extra.reset(token)
            record.setdefault("status", "error")
            record["duration_ms"] = round((time.monotonic() - t0) * 1000, 1)
            if "hits" not in extra and text is not None:
                record["result_summary"] = text[:SUMMARY_CHARS]
            record.update(extra)
            _write(record)
            if self.usage_detail == "full":
                payload_logger.info(json.dumps(
                    {"ts": record["ts"], "req": record["req"], "tool": name,
                     "args": arguments, "response": text, "error": record.get("error")},
                    ensure_ascii=False, default=str,
                ))


class MonthlyFileHandler(logging.FileHandler):
    """Append to a file named from a template with {month}; switch files when the month changes."""

    def __init__(self, template: str, encoding: str = "utf-8") -> None:
        self._template = template
        self._month = datetime.now().strftime("%Y-%m")
        super().__init__(self._path(), encoding=encoding)

    def _path(self) -> str:
        return self._template.replace("{month}", self._month)

    def emit(self, record: logging.LogRecord) -> None:
        month = datetime.now().strftime("%Y-%m")
        if month != self._month:
            self.acquire()
            try:
                self._month = month
                self.close()
                self.baseFilename = str(Path(self._path()).resolve())
                Path(self.baseFilename).parent.mkdir(parents=True, exist_ok=True)
                self.stream = self._open()
            finally:
                self.release()
        super().emit(record)


def payload_template(template: str) -> str:
    """usage.jsonl → usage.payloads.jsonl (placeholders kept)."""
    p = Path(template)
    return str(p.with_name(p.stem + ".payloads" + (p.suffix or ".jsonl")))


def _handler(template: str, max_mb: int, max_files: int) -> logging.Handler:
    template = template.replace("{host}", _host)
    Path(template.replace("{month}", "x")).parent.mkdir(parents=True, exist_ok=True)
    if "{month}" in template:
        handler: logging.Handler = MonthlyFileHandler(template)
    else:
        handler = RotatingFileHandler(
            template, maxBytes=max_mb * 1024 * 1024, backupCount=max_files, encoding="utf-8",
        )
    handler.setFormatter(logging.Formatter("%(message)s"))
    return handler


def setup(template: str, detail: str, max_mb: int, max_files: int) -> None:
    """Attach file handlers to the usage loggers. detail: 'summary' | 'full'."""
    for logger, path in ((usage_logger, template), (payload_logger, payload_template(template))):
        logger.setLevel(logging.INFO)
        logger.propagate = False
        if logger is payload_logger and detail != "full":
            continue
        logger.addHandler(_handler(path, max_mb, max_files))
    TrackedFastMCP.usage_detail = detail
