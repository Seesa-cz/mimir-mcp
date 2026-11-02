# Mimir — Codebase

## Repository Structure

```
mimir-mcp/                           ← git repo root (APP_DIR)
  mimir.sh                           ← server launcher (start/stop/restart/reindex/init/config)
  upgrade.sh                         ← deploy: stop → git pull → uv sync → --reindex
  config.yaml.template               ← full variable reference with comments (in git)
  pyproject.toml                     ← project metadata (name: mimir, hatch-vcs versioning)
  uv.lock
  README.md                          ← user documentation: install, config, tools
  CODEBASE.md                        ← this file
  tests/                             ← pytest: devlog format + tools on a temp vault
  src/
    mimir_config.sh                  ← shared bash library: 3-layer config resolution
    mimir/                           ← Python package
      __init__.py
      config.py                      ← Config dataclass, maps MIMIR_* env vars
      store.py                       ← sqlite-vec + FTS5 BM25, hybrid RRF, category weights
      indexer.py                     ← file sync, frontmatter parsing, chunking, NotesWatcher
      tools.py                       ← all @mcp.tool() definitions
      devlog.py                      ← dev history format: parse/render TASK, PROGRESS, DECISIONS
      server.py                      ← FastMCP, uvicorn, /health, Bearer auth middleware
      version_check.py               ← daemon: compares local git tag with remote on startup
  .chroma/                           ← vector index data dir (default, gitignored)
    metadata.db                      ← SQLite metadata + sqlite-vec `vec0` embeddings + FTS5 keyword index (WAL mode)
    embed_cache.db                   ← embedding cache keyed by (model|version, sha256(embed_text)); survives --reindex, delete to purge
    mimir.meta.yaml                  ← active model name; managed by mimir.sh
  server.log                         ← server log (default, gitignored)
  server.pid                         ← runtime PID (gitignored)
  usage.jsonl                        ← per-tool-call usage log, JSONL (default, gitignored)

~/.mimir/                            ← production deploy (git clone of same repo)
~/.config/mimir/config.yaml          ← user config (index_dir, write_dir, port, …)
```

## Entry Points

```bash
mimir.sh              # help + status (default)
mimir.sh --help       # usage
mimir.sh --status     # "running: PID=XXXX" or "not running"
mimir.sh --start      # start server (idempotent); waits for /health on full reindex
mimir.sh --stop       # stop server (waits up to 10s for clean exit)
mimir.sh --restart    # stop + start
mimir.sh --reindex    # clear vector DB + restart (forces full reindex)
mimir.sh --init       # scaffold write_dir with default categories (.mimir.yaml files)
mimir.sh --config     # show resolved config with source per variable

upgrade.sh            # mimir.sh --stop → git pull --ff-only → uv self update → uv sync → mimir.sh --reindex
```

## Module Responsibilities

| Module | Responsibility |
|---|---|
| `src/mimir_config.sh` | 3-layer config resolver (user < app_dir < env); sourced by mimir.sh and upgrade.sh. |
| `mimir.sh` | Process lifecycle, log rotation, model meta check, model pre-download, category scaffold. |
| `upgrade.sh` | Deployment script: stops server, pulls git, updates uv, syncs deps, triggers full reindex. |
| `src/mimir/config.py` | Maps `MIMIR_*` env vars to typed `Config` dataclass. |
| `src/mimir/store.py` | sqlite-vec+FTS5+sqlite3 vector store; dense cosine search (vec0 virtual table), BM25 keyword search (FTS5 `entries_fts`), hybrid RRF fusion with optional per-category `search_weight` applied to the fused score. `KNN_MAX` (4096) caps every knn `k`. Thread-safe via `RLock`. Embedding cache (`encode_cached`, `cache_get`/`cache_put`/`cache_prune`) in separate `embed_cache.db` with its own lock — unchanged chunk text is never re-encoded; bump `_CACHE_VERSION` when vectors for the same model change. |
| `src/mimir/indexer.py` | File scanning, frontmatter parsing, per-section chunking, full/incremental sync, `NotesWatcher` (watchdog). Title derived from H1 heading; category derived from filesystem path relative to `write_dir`. `scan_schema()` reads `usage` + `search_weight` from `.mimir.yaml`; weight is inherited by sub-categories. |
| `src/mimir/tools.py` | All `@mcp.tool()` definitions; search deduplicates results by file. `_tracked()` decorator + explicit `_log_usage()` calls in `search_notes` write one JSON line per call to the `mimir.usage` logger (duration, args, result summary / hit count / relevance). Note: `_tracked` truncates `result_summary` to 150 chars. Only the local instance is logged — multi-machine setups have no aggregated view. |
| `src/mimir/devlog.py` | Pure functions (no I/O) for the dev history format in `projects/<slug>/dev/`: slug check, note skeletons, task parse/add/state change, progress rows under date headings, decision add/status. Ignores lines inside code fences; raises `DevlogError` on invalid input or duplicate IDs. |
| `src/mimir/server.py` | FastMCP + uvicorn wiring, `/health` endpoint, optional Bearer auth middleware, background sync and version-check daemons. |
| `src/mimir/version_check.py` | Daemon thread: compares local `git describe` with remote tag; logs warning if outdated. |

## API / Interface

| Tool | Signature |
|---|---|
| `search_notes` | `(query="", k=5, tags=None, category=None, mode="hybrid")` — unknown `category` is an error, not an ignored filter |
| `read_note` | `(path: str, offset: int = 0, limit: int \| None = None)` — lines; `offset<0` counts from the end; paginated output starts with `[lines X–Y of N]` |
| `add_note` | `(path: str, tags: list[str], author: str, content: str)` |
| `edit_note` | `(path: str, content: str, tags: list[str] \| None)` |
| `patch_note` | `(path: str, old: str, new: str, replace_all: bool = False)` |
| `delete_note` | `(path: str)` |
| `move_note` | `(path: str, new_path: str)` |
| `add_category` | `(category: str, usage: str)` |
| `edit_category` | `(category: str, usage: str)` — update `usage`, preserving other `.mimir.yaml` keys |
| `move_category` | `(category: str, new_category: str)` |
| `delete_category` | `(category: str, force: bool = False)` |
| `list_tags` | `()` |
| `get_schema` | `()` — category tree from `.mimir.yaml` files, TTL-cached 30 s |
| `reindex` | `()` — force full re-sync |
| `dev_task` | `(project, id=None, description=None, ac=None, state=None, reason=None, decision=None)` — no `id` adds `T<n>`; with `id` sets state (`' ' ~ ! x -`) |
| `dev_log` | `(project, tasks: list[str], summary, implementation)` — row under today's `## YYYY-MM-DD`; tasks must exist |
| `dev_decision` | `(project, title=None, context=None, decision=None, rejected=None, consequences=None, tasks=None, status="accepted", id=None)` — no `id` adds `D<n>`; with `id` sets status |

```
GET /health  →  {"status":"ok"|"indexing","pid":N,"ip_add":str,"port":N,"notes":N,"chunks":N,"idle_seconds":N,"idle_timeout":N}
```

`status` reflects `indexer.is_indexing()` — "indexing" while startup sync, periodic sync, or file-watcher reindex is in flight (counter-based, tracked via `@_tracks_indexing` on `sync()` and `index_file()`). `notes` is the distinct source-file count (`store.count_files()`); `chunks` is the raw indexed-row count (`store.count()`) — long notes are split into several chunks, so `chunks` >= `notes`.

## Configuration Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `MIMIR_INDEX_DIR` | yes | — | Read-only vault path. |
| `MIMIR_WRITE_DIR` | yes | — | Read-write path for AI notes. |
| `MIMIR_PORT` | yes | — | HTTP port. |
| `MIMIR_CHROMA_DIR` | no | `<app_dir>/.chroma` | Vector index location. |
| `MIMIR_MODEL` | no | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` | fastembed ONNX model. |
| `MIMIR_IDLE_TIMEOUT` | no | `0` | Auto-shutdown after N minutes idle (0 = never). |
| `MIMIR_SYNC_INTERVAL` | no | `5` | Periodic re-sync interval in minutes (0 = disable). |
| `MIMIR_LOG_LEVEL` | no | `warning` | `warning` / `info` / `debug`. |
| `MIMIR_LOG_FILE` | no | `<app_dir>/server.log` | Log file path. |
| `MIMIR_MAX_LOG_MB` | no | `5` | Rotate log at N MB (0 = never). |
| `MIMIR_MAX_LOG_FILES` | no | `5` | Rotated backup count. |
| `MIMIR_USAGE_LOG_FILE` | no | `<app_dir>/usage.jsonl` | JSONL log of MCP tool calls (rotated via `MIMIR_MAX_LOG_MB`/`MIMIR_MAX_LOG_FILES`). |
| `MIMIR_AUTH_TOKEN` | no | `""` | Bearer token; empty = no auth (local only). |
| `MIMIR_BIND_HOST` | no | `127.0.0.1` | Listen address; `0.0.0.0` for reverse-proxy setups. |
| `MIMIR_FASTEMBED_CACHE` | no | `~/.cache/fastembed` | ONNX model cache dir; exported as `FASTEMBED_CACHE_PATH` (bash-only, not in config.py). |

## How to Add X

### New MCP tool

1. Add a `@mcp.tool()` function inside `register_tools()` in `src/mimir/tools.py` — tools are closures over the `cfg`, `store` and `touch_fn` parameters, there is no module-level accessor.
2. Call `touch_fn()` first (resets the idle-shutdown timer).
3. For write operations: resolve the path via `_resolve_write(path, cfg.index_dir, cfg.write_dir)`; it returns `None` for anything outside `write_dir`.
4. Add `@_tracked("<tool_name>", *arg_names)` for usage logging, or call `_log_usage()` explicitly when the return string doesn't capture what matters.

### Change the dev history format

1. Change parse/render functions and regexes in `src/mimir/devlog.py`.
2. Keep `tools.py` wrappers thin — they only scaffold, lock, load/save frontmatter and call `devlog`.
3. Update `tests/test_devlog.py`, run `uv run pytest`.
4. Existing notes in the vault are not migrated automatically.

### Run tests

`uv run pytest` — tool tests use a temp vault and stub `index_file`; never point tests at the production vault.

### New config variable

1. Add field to `Config` dataclass in `src/mimir/config.py` with a default.
2. Map `MIMIR_<VAR>` env var in `config.py:load()`.
3. Add bash parsing in `src/mimir_config.sh`: `_load_yaml` case + env snapshot + derived default.
4. Document in `config.yaml.template`.

### New category

Use the `add_category(category, usage)` MCP tool, or manually:

```bash
mkdir ~/obsidian/mimir-kb/<name>
cat > ~/obsidian/mimir-kb/<name>/.mimir.yaml <<EOF
usage: "What goes in this category."
EOF
```

`usage` and optional `search_weight` (float, default 1.0 — `<1` demotes, `>1` boosts, inherited by sub-categories) are the fields read; display name is derived from the directory name. Use `edit_category(category, usage)` to update it later. Or use `mimir.sh --init` to scaffold the three default categories (`projects`, `reference`, `solutions`) at once (idempotent).

### Change embedding model

1. Set `model: <new-model>` in `~/.config/mimir/config.yaml`.
2. Run `mimir.sh --reindex` — detects model change via `mimir.meta.yaml`, clears DB, rebuilds.
