"""Compare the running version with the newest release tag on GitHub.

The running version is read once at server start, so a later `git pull` without a
restart still counts as outdated. Results stay in memory for /health and search_notes.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# HTTPS, not `origin`: the public repo needs no SSH key, which a service may not have.
_REPO_URL = "https://github.com/Seesa-cz/mimir-mcp"
# Remote tags reach the LLM context through search_notes: accept release tags only.
_RELEASE_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")

_running: str | None = None
_latest: str | None = None
_check_state = "disabled"  # disabled | pending | ok | failed


def start_update_checks(enabled: bool, interval: int) -> None:
    """Read the running version; if enabled, check now and every interval seconds (0 = once)."""
    global _running, _check_state
    _running = _local_tag()
    if enabled:
        _check_state = "pending"
        threading.Thread(target=_check_worker, args=(interval,), daemon=True).start()


def status() -> dict:
    return {
        "version": _running or "unknown",
        "latest_version": _latest,
        "update_available": _update_available(),
        "update_check": _check_state,
    }


def update_notice() -> str:
    """One line for the LLM agent while a newer version exists, otherwise empty."""
    if not _update_available():
        return ""
    return (
        f"[Mimir {_latest} is available (running {_running}). "
        "Tell the user; do not upgrade on your own.]\n\n"
    )


def _update_available() -> bool:
    if _running is None or _latest is None:
        return False
    return _parse(_latest) > _parse(_running)


def _check_worker(interval: int) -> None:
    while True:
        _check_once()
        if interval <= 0:
            return
        time.sleep(interval)


def _check_once() -> None:
    global _latest, _check_state
    try:
        latest = max(_remote_tags(), key=_parse)
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        # Keep the last known result: a failed check says nothing about the versions
        _check_state = "failed"
        logger.info("Update check failed: %s", e)
        return
    changed = latest != _latest
    _latest = latest
    _check_state = "ok"
    if changed and _update_available():
        logger.warning(
            "New Mimir version available: %s (running %s). Run upgrade.sh to update.",
            _latest, _running,
        )


def _local_tag() -> str | None:
    try:
        r = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0"],
            cwd=Path(__file__).parent,
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    tag = r.stdout.strip()
    return tag if _RELEASE_TAG.match(tag) else None


def _remote_tags() -> list[str]:
    r = subprocess.run(
        ["git", "ls-remote", "--tags", "--refs", _REPO_URL],
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        capture_output=True, text=True, timeout=30, check=True,
    )
    names = (line.rpartition("refs/tags/")[2] for line in r.stdout.splitlines())
    return [name for name in names if _RELEASE_TAG.match(name)]


def _parse(tag: str) -> tuple[int, ...]:
    return tuple(int(part) for part in _RELEASE_TAG.match(tag).groups())
