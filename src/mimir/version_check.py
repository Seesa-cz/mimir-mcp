from __future__ import annotations

import logging
import re
import subprocess

logger = logging.getLogger(__name__)


def check_for_update() -> None:
    try:
        local = _local_tag()
        remote_tags = _remote_tags()
        latest = _latest(remote_tags)
        if latest and _newer(latest, local):
            logger.warning(
                "New Mimir version available: %s (installed %s). "
                "Run upgrade.sh to update.",
                latest, local,
            )
    except Exception:
        logger.debug("Version check skipped (offline / no git)")


def _local_tag() -> str:
    r = subprocess.run(
        ["git", "describe", "--tags", "--abbrev=0"],
        capture_output=True, text=True, timeout=5,
    )
    return r.stdout.strip() or "0.0.0"


def _remote_tags() -> list[str]:
    r = subprocess.run(
        ["git", "ls-remote", "--tags", "origin"],
        capture_output=True, text=True, timeout=10,
    )
    tags: set[str] = set()
    for line in r.stdout.splitlines():
        m = re.search(r"refs/tags/(.+?)(\^\{\})?$", line.strip())
        if m:
            tags.add(m.group(1))
    return list(tags)


def _latest(tags: list[str]) -> str | None:
    def key(t: str) -> tuple:
        return tuple(int(p) for p in re.sub(r"^v", "", t).split(".") if p.isdigit())
    return max(tags, key=key) if tags else None


def _newer(remote: str, local: str) -> bool:
    def parse(v: str):
        return tuple(int(p) for p in re.sub(r"^v", "", v).split(".") if p.isdigit())
    return parse(remote) > parse(local)
