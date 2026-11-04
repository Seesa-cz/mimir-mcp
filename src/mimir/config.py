"""Mimir configuration — typed mapper from MIMIR_* env vars to Config dataclass.

All validation and priority resolution is handled by mimir.sh.
Never run server.py directly — always use mimir.sh.
"""

from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    index_dir: Path        # read-only: whole vault (INDEX_DIR)
    write_dir: Path        # read-write: mimir-kb/ only (WRITE_DIR)
    port: int
    chroma_dir: Path
    model: str
    idle_timeout: int      # seconds; 0 = never
    sync_interval: int     # seconds; 0 = disable periodic sync
    log_level: str
    log_file: Path
    max_log_mb: int
    max_log_files: int
    usage_log_file: Path   # structured JSONL log of MCP tool calls, for usage analysis
    auth_token: str | None  # Bearer token for auth; None = no auth (local only)
    bind_host: str          # "127.0.0.1" (default) or "0.0.0.0" (with proxy/Caddy)
    upgrade_check: bool     # compare the running version with GitHub
    upgrade_check_interval: int  # seconds; 0 = only at server start


def load() -> Config:
    """Map MIMIR_* env vars to Config. KeyError if required var missing."""
    return Config(
        index_dir=Path(os.environ["MIMIR_INDEX_DIR"]).expanduser().resolve(),
        write_dir=Path(os.environ["MIMIR_WRITE_DIR"]).expanduser().resolve(),
        port=int(os.environ["MIMIR_PORT"]),
        chroma_dir=Path(os.environ["MIMIR_CHROMA_DIR"]).expanduser().resolve(),
        model=os.environ["MIMIR_MODEL"],
        idle_timeout=int(os.environ.get("MIMIR_IDLE_TIMEOUT", "0")) * 60,
        sync_interval=int(os.environ.get("MIMIR_SYNC_INTERVAL", "5")) * 60,
        log_level=os.environ["MIMIR_LOG_LEVEL"].upper(),
        log_file=Path(os.environ["MIMIR_LOG_FILE"]).expanduser().resolve(),
        max_log_mb=int(os.environ["MIMIR_MAX_LOG_MB"]),
        max_log_files=int(os.environ["MIMIR_MAX_LOG_FILES"]),
        usage_log_file=Path(os.environ["MIMIR_USAGE_LOG_FILE"]).expanduser().resolve(),
        auth_token=os.environ.get("MIMIR_AUTH_TOKEN") or None,
        bind_host=os.environ.get("MIMIR_BIND_HOST", "127.0.0.1"),
        upgrade_check=os.environ.get("MIMIR_UPGRADE_CHECK", "true").strip().lower() != "false",
        upgrade_check_interval=int(os.environ.get("MIMIR_UPGRADE_CHECK_INTERVAL", "360")) * 60,
    )
