"""Mimir MCP server entry point."""

from __future__ import annotations
import hmac
import logging
import os
import signal
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from mcp.server.fastmcp import FastMCP

from .config import load as load_config
from .store import VectorStore
from .indexer import sync, start_watcher, is_indexing
from .tools import register_tools
from .version_check import check_for_update


def main() -> None:
    import uvicorn

    cfg = load_config()

    if cfg.bind_host != "127.0.0.1" and not cfg.auth_token:
        raise RuntimeError(
            f"Refusing to start: bind_host={cfg.bind_host!r} exposes the server beyond "
            "localhost, but MIMIR_AUTH_TOKEN is not set. Set MIMIR_AUTH_TOKEN or use "
            "bind_host=127.0.0.1."
        )

    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.WARNING),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    log = logging.getLogger(__name__)
    log.warning("Starting mimir server on port %d (pid %d)", cfg.port, os.getpid())

    # Usage log — one JSON line per MCP tool call, separate from server.log, for
    # offline analysis of real usage patterns (see tools.py touch points).
    cfg.usage_log_file.parent.mkdir(parents=True, exist_ok=True)
    usage_logger = logging.getLogger("mimir.usage")
    usage_logger.setLevel(logging.INFO)
    usage_logger.propagate = False
    usage_handler = RotatingFileHandler(
        cfg.usage_log_file,
        maxBytes=cfg.max_log_mb * 1024 * 1024,
        backupCount=cfg.max_log_files,
        encoding="utf-8",
    )
    usage_handler.setFormatter(logging.Formatter("%(message)s"))
    usage_logger.addHandler(usage_handler)

    # Validate required directories exist
    if not cfg.index_dir.exists():
        raise RuntimeError(f"MIMIR_INDEX_DIR does not exist: {cfg.index_dir}")
    if not cfg.write_dir.exists():
        raise RuntimeError(f"MIMIR_WRITE_DIR does not exist: {cfg.write_dir}")

    cfg.chroma_dir.mkdir(parents=True, exist_ok=True)

    # Write own PID so mimir.sh can reliably stop/status us
    # MIMIR_PID_FILE is always set by mimir.sh to $APP_DIR/server.pid; fallback covers direct invocation.
    _pid_path = os.environ.get("MIMIR_PID_FILE")
    pid_file = Path(_pid_path) if _pid_path else cfg.log_file.parent / "server.pid"
    pid_file.write_text(str(os.getpid()))

    store = VectorStore(cfg.chroma_dir, cfg.model)

    # Initial sync in background so HTTP server starts immediately
    def _initial_sync():
        try:
            n = sync(cfg.index_dir, store, cfg.write_dir)
            if n is not None:
                log.warning("Startup sync: %d notes updated", n)
        except Exception:
            log.exception("Startup sync failed")

    threading.Thread(target=_initial_sync, daemon=True).start()
    threading.Thread(target=check_for_update, daemon=True).start()

    mcp = FastMCP("mimir")

    _last_activity = [time.monotonic()]

    def touch():
        _last_activity[0] = time.monotonic()

    register_tools(mcp, cfg, store, touch)

    # Host header check — defense against DNS-rebinding attacks against the
    # loopback-only server (a page in the browser resolves an attacker-controlled
    # domain to 127.0.0.1 and calls this server as if it were same-origin).
    # Only enforced in loopback mode: in reverse-proxy mode (bind_host=0.0.0.0)
    # the Host header is proxy-controlled and unpredictable, and auth_token is
    # already mandatory there (see startup check above).
    if cfg.bind_host == "127.0.0.1":
        _allowed_hosts = {
            f"127.0.0.1:{cfg.port}", f"localhost:{cfg.port}", f"[::1]:{cfg.port}",
        }

        class HostCheckMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request: Request, call_next):
                if request.headers.get("host", "") not in _allowed_hosts:
                    return JSONResponse(
                        {"error": "forbidden", "detail": "Invalid Host header"},
                        status_code=403,
                    )
                return await call_next(request)

        mcp.streamable_http_app().add_middleware(HostCheckMiddleware)

    # Auth middleware — Bearer token check on every request except /health.
    # When auth_token is None/empty, auth is disabled (backward compatible).
    if cfg.auth_token:
        class AuthMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request: Request, call_next):
                if request.url.path == "/health":
                    return await call_next(request)
                auth = request.headers.get("Authorization", "")
                if not hmac.compare_digest(auth, f"Bearer {cfg.auth_token}"):
                    return JSONResponse(
                        {"error": "unauthorized", "detail": "Invalid or missing Bearer token"},
                        status_code=401,
                    )
                return await call_next(request)

        mcp.streamable_http_app().add_middleware(AuthMiddleware)
        log.warning("Bearer token auth enabled (skip /health)")

    # Health endpoint
    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        idle = round(time.monotonic() - _last_activity[0])
        return JSONResponse({
            "status": "indexing" if is_indexing() else "ok",
            "pid": os.getpid(),
            "ip_add": cfg.bind_host,
            "port": cfg.port,
            "notes": store.count_files(),
            "chunks": store.count(),
            "idle_seconds": idle,
            "idle_timeout": cfg.idle_timeout,
        })

    # Idle shutdown monitor
    def _shutdown_monitor():
        if cfg.idle_timeout == 0:
            return
        while True:
            time.sleep(60)
            if time.monotonic() - _last_activity[0] >= cfg.idle_timeout:
                os.kill(os.getpid(), signal.SIGTERM)
                return

    threading.Thread(target=_shutdown_monitor, daemon=True).start()

    # Periodic sync — safety net for filesystems where inotify is unreliable (e.g. NTFS via WSL)
    def _sync_worker():
        if cfg.sync_interval == 0:
            return
        log = logging.getLogger(__name__)
        while True:
            time.sleep(cfg.sync_interval)
            n = sync(cfg.index_dir, store, cfg.write_dir)
            if n:
                log.info("Periodic sync: %d note(s) updated", n)

    threading.Thread(target=_sync_worker, daemon=True).start()

    # File watcher for auto-reindexing
    observer = start_watcher(cfg.index_dir, store, cfg.write_dir)

    try:
        uvicorn.run(
            mcp.streamable_http_app(),
            host=cfg.bind_host,
            port=cfg.port,
            log_level=cfg.log_level.lower(),
        )
    finally:
        observer.stop()
        observer.join()


if __name__ == "__main__":
    main()
