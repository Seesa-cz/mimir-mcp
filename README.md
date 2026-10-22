# Mimir — Personal Knowledge Base MCP Server

Mimir is a local [MCP](https://modelcontextprotocol.io/) server that gives AI assistants semantic access to your personal notes. It indexes Markdown files, stores embeddings locally with sqlite-vec + SQLite FTS5 (no external vector DB), and exposes a clean set of tools for searching, reading, and writing notes.

Designed to work alongside [Obsidian](https://obsidian.md/) vaults, but works with any directory of Markdown files.

## How it works

```
AI assistant (Claude Code, etc.)
        │  MCP (Streamable HTTP)
        ▼
  Mimir server (FastMCP + uvicorn)
        │
        ├── sqlite-vec+FTS5  — local vector index (fastembed ONNX, no CUDA)
        ├── watchdog         — live reindex on file changes
        └── Markdown files   — YAML frontmatter, tags, categories
```

**Two-directory model:**

| Directory | Role |
|-----------|------|
| `INDEX_DIR` | Read-only. Full vault — all notes are searchable. |
| `WRITE_DIR` | Read-write. AI can only create/edit/delete notes here. |

This lets you point Mimir at your entire Obsidian vault while restricting AI writes to a dedicated subdirectory.

## Requirements

- [uv](https://docs.astral.sh/uv/) — Python package manager (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- `curl` — used by `mimir.sh` for health checks

Python and all Python dependencies are managed automatically by `uv` — nothing else to install.

## Installation

```bash
# Clone the repo
git clone https://github.com/seesa-cz/mimir-mcp ~/.mimir
cd ~/.mimir

# Create your user config (required variables)
mkdir -p ~/.config/mimir
cat > ~/.config/mimir/config.yaml <<EOF
index_dir: ~/obsidian          # read-only: your full vault
write_dir: ~/obsidian/mimir-kb # read-write: AI writes only here
port: 65501
EOF

# Start the server
bash mimir.sh --start
```

On first start, `mimir.sh --start` downloads and caches the `paraphrase-multilingual-MiniLM-L12-v2` ONNX embedding model (~120 MB) — this may take a few minutes. Run it manually at least once before relying on autostart. Subsequent starts take ~20 seconds (model already cached).

Check the server is running:

```bash
curl http://127.0.0.1:65501/health
# {"status":"ok","pid":12345,"ip_add":"127.0.0.1","port":65501,"notes":42,"chunks":58,"idle_seconds":5,"idle_timeout":0}
```

## Client Configuration (MCP Clients)

### Claude Code

```bash
claude mcp add --transport http --scope user mimir-kb http://127.0.0.1:65501/mcp
```

This writes the server entry to `~/.claude.json`. Use the same server name (`mimir-kb`) across all clients — the name becomes the tool prefix (`mcp__mimir-kb__search_notes`), and mixing names makes past sessions hard to search. After this, Mimir's tools are available in every Claude Code session.

## Autostart with systemd (Linux / WSL2)

Create `~/.config/systemd/user/mimir.service`:

```ini
[Unit]
Description=Mimir MCP Server
After=network.target

[Service]
Type=forking
TimeoutStartSec=infinity
Environment=PATH=/home/YOUR_USER/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/home/YOUR_USER/.mimir/mimir.sh --start
ExecStop=/home/YOUR_USER/.mimir/mimir.sh --stop
PIDFile=/home/YOUR_USER/.mimir/server.pid

[Install]
WantedBy=default.target
```

Enable and start:

```bash
systemctl --user daemon-reload
systemctl --user enable --now mimir.service
```

> `TimeoutStartSec=infinity`: `mimir.sh --start` waits up to 30 s for the HTTP server, then — only when the index DB is missing (first start, model change, `--reindex`) — waits for the full reindex to finish, which has no fixed duration. It fails on its own when the server exits, logs an `ERROR`, or writes nothing to the log for 120 s, so a systemd timeout would only kill a reindex that is still progressing.

## mimir.sh reference

```bash
mimir.sh              # show help + status (default)
mimir.sh --help       # show usage
mimir.sh --status     # "running: PID=XXXX" or "not running"
mimir.sh --start      # start server (idempotent)
mimir.sh --stop       # stop server
mimir.sh --restart    # stop + start
mimir.sh --reindex    # clear index DB and restart (full reindex, embedding cache kept)
mimir.sh --init       # scaffold write_dir with default categories
mimir.sh --config     # show resolved config with source per variable

upgrade.sh            # deploy: stop → git pull → uv self update → uv sync → --reindex
```

> **Embedding cache:** embeddings are cached in `chroma_dir/embed_cache.db`, keyed by model and a hash of the chunk text. A full reindex re-encodes only chunks whose text is not in the cache, so `--reindex` / `upgrade.sh` take seconds instead of minutes once the cache is warm (the first run after installing, or after a model change, still encodes everything). Edited notes re-encode only their changed sections. Entries unused for 30 days are pruned after each sync. Delete `embed_cache.db` to force re-encoding everything.

## Configuration

Configuration is resolved in layers (later overrides earlier):

1. `~/.config/mimir/config.yaml` — user config (base)
2. `<app_dir>/config.yaml` — app defaults
3. `MIMIR_*` environment variables — highest priority

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `index_dir` | **yes** | — | Read-only vault path |
| `write_dir` | **yes** | — | Read-write path for AI notes |
| `port` | **yes** | — | HTTP port |
| `chroma_dir` | no | `<app_dir>/.chroma` | Vector index location (sqlite-vec + FTS5 in `metadata.db`, embedding cache in `embed_cache.db`) |
| `model` | no | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` | fastembed ONNX embedding model |
| `idle_timeout_minutes` | no | `0` | Auto-shutdown after N minutes idle (0 = never) |
| `sync_interval_minutes` | no | `5` | Periodic re-sync interval (0 = disable) |
| `log_level` | no | `warning` | `warning` / `info` / `debug` |
| `log_file` | no | `<app_dir>/server.log` | Log file path |
| `max_log_mb` | no | `5` | Rotate log at N MB (0 = never) |
| `max_log_files` | no | `5` | Number of rotated backups |
| `fastembed_cache_path` | no | `~/.cache/fastembed` | ONNX model cache dir (persistent across reinstalls) |
| `usage_log_file` | no | `<app_dir>/usage.jsonl` | JSONL log of MCP tool calls, for usage analysis |
| `auth_token` | no | `""` | Bearer token; empty = no auth (local only) |
| `bind_host` | no | `127.0.0.1` | Listen address; `0.0.0.0` for reverse-proxy setups — requires `auth_token` to be set |

> **Model note:** `model` must be a [fastembed](https://qdrant.github.io/fastembed/)-compatible ONNX embedding model — not a generative LLM. See `config.yaml.template` for alternatives (e.g. `BAAI/bge-small-en-v1.5` for English-only, faster). After changing the model, delete `chroma_dir` and restart to force a full reindex — embeddings from different models are incompatible.

## MCP Tools

| Tool | Description |
|------|-------------|
| `search_notes(query, k?, tags?, category?, mode?)` | Semantic + keyword search across INDEX_DIR |
| `read_note(path)` | Full note content including frontmatter |
| `add_note(path, tags, author, content)` | Create a new note in WRITE_DIR |
| `edit_note(path, content, tags?)` | Replace note body |
| `patch_note(path, old, new, replace_all?)` | Partial edit via substring replace |
| `delete_note(path)` | Remove note from disk and index |
| `move_note(path, new_path)` | Move or rename a note within WRITE_DIR |
| `add_category(category, usage)` | Create a new category directory |
| `edit_category(category, usage)` | Update the usage guidance; other `.mimir.yaml` keys are preserved |
| `move_category(category, new_category)` | Rename or move a category |
| `delete_category(category, force?)` | Delete a category (and optionally its notes) |
| `list_tags()` | All tags sorted by frequency |
| `get_schema()` | Category tree from `.mimir.yaml` files |
| `reindex()` | Force full re-sync |

## Category system

Categories are filesystem-driven. A directory is a valid category if it contains a `.mimir.yaml` file with a `usage` field:

```yaml
usage: "A specific problem encountered and its concrete resolution. Bug fixes, troubleshooting guides, non-obvious procedures. Framing: 'I had problem X, here is how I solved it'."
```

`mimir.sh --init` scaffolds `write_dir` with three default categories (`projects`, `reference`, `solutions`). It skips directories that already have a `.mimir.yaml`. Add more with `add_category(category, usage)` as your needs grow.

Hierarchy is unlimited: `solutions`, `solutions/gis`, `solutions/gis/prague`. `search_notes(category="solutions")` matches `solutions` and all sub-categories. An unknown category name is an error, not a silently ignored filter.

### Search weight

A category may optionally set `search_weight` to demote or boost its notes in ranking:

```yaml
usage: "Raw session transcripts — high noise, low signal."
search_weight: 0.5    # 1.0 = neutral (default), <1 demote, >1 boost
```

Demoted notes stay fully indexed and searchable — they simply lose to better matches, and still surface when nothing better exists. Use this for bulky low-signal corpora that would otherwise crowd out curated notes. Sub-categories inherit the value unless they set their own.

The weight multiplies the fused RRF score, so it bites harder than the number suggests: RRF scores decay slowly with rank, and `0.5` was enough to cut session-log hits in a sample of 12 queries from 57 % of top-5 results to 12 %. Start near `0.5` and tune from there; only `mode="hybrid"` (the default) applies weights.

## Obsidian vault layout

```
~/obsidian/                    ← INDEX_DIR
  personal-notes/              ← indexed, AI can read
  mimir-kb/                    ← WRITE_DIR (AI writes here)
    knowledge/.mimir.yaml
    projects/.mimir.yaml
    session-logs/.mimir.yaml
```

Hidden directories (`.obsidian`, `.trash`, `.git`) are skipped. Notes without frontmatter are indexed gracefully — title defaults to filename, tags default to empty.

## What gets downloaded automatically

On first `mimir.sh --start`, `uv` installs all Python dependencies into an isolated virtualenv (FastMCP, fastembed, sqlite-vec, uvicorn, watchdog, …). Then fastembed downloads the ONNX embedding model (~120 MB) from HuggingFace and caches it in `fastembed_cache_path` (default `~/.cache/fastembed`, persists across reinstalls). This happens once — subsequent starts skip both steps.
