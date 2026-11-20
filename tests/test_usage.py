"""Usage log: call_tool-level records, status classification, payload file, file placeholders."""

import asyncio
import json
import logging
import socket

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from mimir import usage


@pytest.fixture
def log(tmp_path):
    yield tmp_path
    for logger in (usage.usage_logger, usage.payload_logger):
        for h in [h for h in logger.handlers if isinstance(h, logging.FileHandler)]:
            h.close()
            logger.removeHandler(h)
    usage.TrackedFastMCP.usage_detail = "summary"


def _records(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def _server():
    mcp = usage.TrackedFastMCP("t")

    @mcp.tool()
    def echo(text: str, n: int = 1) -> str:
        usage.note(hits=[{"path": "a.md"}])
        return text * n

    @mcp.tool()
    def missing(path: str) -> str:
        return f"Note not found: {path}"

    return mcp


@pytest.mark.parametrize("text,status", [
    ("Saved: kb/a.md", "ok"),
    ("[lines 1–2 of 10]\nx", "ok"),
    ("Note not found: kb/a.md", "not_found"),
    ("String not found in kb/a.md", "not_found"),
    ("Category 'x' not found.", "not_found"),
    ("Ambiguous: 2 occurrences — use replace_all=True or more context", "ambiguous"),
    ("Note 'a.md' is outside write_dir — cannot edit.", "denied"),
    ("Path 'a' is outside write_dir — cannot write.", "denied"),
    ("Note already exists: kb/a.md", "conflict"),
    ("Category 'x' already exists.", "conflict"),
    ("Sync already in progress — try again when it finishes.", "busy"),
    ("No notes found matching: query='x'", "empty"),
    ("[indexing in progress — results may be incomplete]\n\nNo notes found matching: browse", "empty"),
    ("Unknown category 'x' — nothing was searched. Known categories: a", "invalid_args"),
    ("Path must end with .md", "invalid_args"),
    ("[offset 10 is beyond the end — note has 10 lines]", "invalid_args"),
    ("Category 'x' has 3 note(s). Use force=True to delete with all notes.", "invalid_args"),
    ("Error: unknown task(s) T9 — add them with dev_task first", "error"),
])
def test_classify(text, status):
    assert usage.classify(text) == status


def test_call_records(log):
    usage.setup(str(log / "u.jsonl"), "summary", 1, 1)
    mcp = _server()
    asyncio.run(mcp.call_tool("echo", {"text": "x" * 400}))
    asyncio.run(mcp.call_tool("missing", {"path": "a.md"}))
    with pytest.raises(ToolError):
        asyncio.run(mcp.call_tool("echo", {"text": "a", "n": "many"}))
    with pytest.raises(ToolError):
        asyncio.run(mcp.call_tool("nope", {}))
    ok, nf, bad, unknown = _records(log / "u.jsonl")
    assert ok["v"] == 2 and ok["host"] == socket.gethostname() and ok["req"]
    assert ok["status"] == "ok" and ok["resp_chars"] == 400
    assert len(ok["args"]["text"]) == usage.ARG_MAX_CHARS and ok["args_len"] == {"text": 400}
    assert ok["hits"] == [{"path": "a.md"}] and "result_summary" not in ok
    assert nf["status"] == "not_found" and nf["result_summary"] == "Note not found: a.md"
    assert bad["status"] == "invalid_args" and "error" in bad
    assert unknown["status"] == "unknown_tool"
    assert not (log / "u.payloads.jsonl").exists()


def test_full_detail_writes_payloads(log):
    usage.setup(str(log / "u.jsonl"), "full", 1, 1)
    asyncio.run(_server().call_tool("echo", {"text": "y" * 400}))
    (rec,) = _records(log / "u.jsonl")
    (payload,) = _records(log / "u.payloads.jsonl")
    assert payload["req"] == rec["req"]
    assert payload["args"]["text"] == "y" * 400 and payload["response"] == "y" * 400


def test_placeholders_and_month_switch(log, monkeypatch):
    usage.setup(str(log / "d" / "usage-{host}-{month}.jsonl"), "summary", 1, 1)
    usage.usage_logger.info("a")
    (handler,) = [h for h in usage.usage_logger.handlers if isinstance(h, usage.MonthlyFileHandler)]
    host = socket.gethostname()
    first = log / "d" / f"usage-{host}-{handler._month}.jsonl"
    assert first.read_text() == "a\n"
    handler._month = "1999-01"     # simulate a month change
    usage.usage_logger.info("b")
    assert first.read_text() == "a\nb\n"   # current month file again
    assert not (log / "d" / f"usage-{host}-1999-01.jsonl").exists()
