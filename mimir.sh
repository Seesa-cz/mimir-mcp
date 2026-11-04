#!/usr/bin/env bash
# mimir.sh — Mimir MCP server launcher
# Configuration resolution is in src/mimir_config.sh.
#
# Usage: mimir.sh [--help|--status|--start|--stop|--restart|--reindex|--init|--config]
# No args: prints --help output, then --status.
# Upgrade/deployment: use upgrade.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$APP_DIR/src/mimir_config.sh"
mimir_resolve_config

export FASTEMBED_CACHE_PATH="$MIMIR_FASTEMBED_CACHE"
PID_FILE="$MIMIR_PID_FILE"
SERVICE_NAME="mimir.service"

# --- PID helpers ---
_get_pid() {
    [[ -f "$PID_FILE" ]] || return 1
    local pid
    pid=$(cat "$PID_FILE")
    kill -0 "$pid" 2>/dev/null && echo "$pid" || return 1
}

_rotate_log() {
    local log="$MIMIR_LOG_FILE"
    local max_mb="$MIMIR_MAX_LOG_MB"
    local max_files="$MIMIR_MAX_LOG_FILES"
    [[ -f "$log" ]] || return 0
    [[ "$max_mb" -gt 0 ]] || return 0
    local size
    size=$(stat -c%s "$log" 2>/dev/null || echo 0)
    if [[ "$size" -gt $((max_mb * 1024 * 1024)) ]]; then
        [[ "$max_files" -gt 0 ]] && rm -f "${log}.${max_files}"
        local i=$((max_files - 1))
        while [[ "$i" -ge 1 ]]; do
            [[ -f "${log}.${i}" ]] && mv "${log}.${i}" "${log}.$((i + 1))"
            i=$((i - 1))
        done
        mv "$log" "${log}.1"
    fi
}

_check_meta() {
    local meta="$MIMIR_CHROMA_DIR/mimir.meta.yaml"
    if [[ -f "$meta" ]]; then
        local stored
        stored=$(grep -m1 '^model:' "$meta" | sed 's/^model:[[:space:]]*//')
        [[ "$stored" == "$MIMIR_MODEL" ]] && return
        echo "Reindex chromaDB: Model change detected ($stored → $MIMIR_MODEL)"
    else
        echo "Reindex chromaDB: Missing mimir.meta.yaml"
    fi
    rm -rf "$MIMIR_CHROMA_DIR"
    mkdir -p "$MIMIR_CHROMA_DIR"
    printf 'model: %s\n' "$MIMIR_MODEL" > "$meta"
}

_download_model() {
    # fastembed uses HF source slug for cache dir (e.g. qdrant/...), not the model name;
    # default cache is /tmp/fastembed_cache, not ~/.cache/fastembed
    local model_dir
    model_dir=$(uv --directory "$APP_DIR" run python -c "
import os
from fastembed import TextEmbedding
model = os.environ['MIMIR_MODEL']
cache = os.environ.get('FASTEMBED_CACHE_PATH', os.path.join(os.path.expanduser('~'), '.cache', 'fastembed'))
mods = [m for m in TextEmbedding.list_supported_models() if m['model'] == model]
if mods:
    slug = mods[0]['sources']['hf'].replace('/', '--')
    print(os.path.join(cache, 'models--' + slug))
" 2>/dev/null)
    if [[ -n "$model_dir" ]] && find "$model_dir" -name "*.onnx" -maxdepth 4 2>/dev/null | grep -q .; then
        return
    fi
    echo "Downloading model $MIMIR_MODEL..."
    if ! uv --directory "$APP_DIR" run python -c "
import os
import sys
from fastembed import TextEmbedding
model = os.environ['MIMIR_MODEL']
try:
    list(TextEmbedding(model).embed(['test']))
except Exception as e:
    print(f'ERROR: Cannot load model \"{model}\":\n{e}', file=sys.stderr)
    sys.exit(1)
"; then
        exit 1
    fi
}

# --- Commands ---
cmd_help() {
    echo "Usage: mimir.sh [--help|--status|--start|--stop|--restart|--reindex|--init|--config]"
}

cmd_status() {
    local health="http://127.0.0.1:${MIMIR_PORT}/health"
    local resp pid
    resp=$(curl -sf "$health" 2>/dev/null) || { echo "not running"; return 0; }
    pid=$(_get_pid 2>/dev/null || echo "?")
    echo "running: PID=$pid, health status: curl http://127.0.0.1:${MIMIR_PORT}/health"
    echo "$resp"
}

_SYNC_STALL_SECONDS=120   # no new log line for this long = sync considered stuck (progress logs every ~10s)

_wait_for_sync() {
    # Polls the health endpoint until status=ok (initial sync complete).
    # No limit on total duration — fails only when the server exits, logs an error,
    # or stops writing to the log for _SYNC_STALL_SECONDS.
    # $1 = log line offset: only scan lines written after this server launch.
    local log="$MIMIR_LOG_FILE"
    local offset="${1:-0}"
    local health="http://127.0.0.1:${MIMIR_PORT}/health"
    local last_progress="" progress_line new_log resp
    local log_len last_len=-1 last_activity
    last_activity=$(date +%s)
    while true; do
        # Server process gone (crash, OOM kill) — nothing left to wait for
        if ! _get_pid >/dev/null 2>&1; then
            echo "ERROR: server exited during sync — check $log" >&2
            return 1
        fi
        # Stall: log has not grown for _SYNC_STALL_SECONDS
        log_len=$(wc -l < "$log" 2>/dev/null || echo 0)
        if [[ "$log_len" -ne "$last_len" ]]; then
            last_len="$log_len"
            last_activity=$(date +%s)
        elif (( $(date +%s) - last_activity > _SYNC_STALL_SECONDS )); then
            echo "ERROR: sync stalled — no log activity for ${_SYNC_STALL_SECONDS}s — check $log" >&2
            return 1
        fi
        # Server error in log — checked first: a failed sync also ends with status "ok"
        new_log=$(tail -n "+$((offset + 1))" "$log" 2>/dev/null)
        if echo "$new_log" | grep -qE " (ERROR|CRITICAL) "; then
            echo "ERROR: server error during sync — check $log" >&2
            return 1
        fi
        # Done: health reports idle AND the server logged a successful startup sync
        resp=$(curl -sf "$health" 2>/dev/null) || true
        if [[ -n "$resp" ]] && echo "$resp" | grep -qE '"status"\s*:\s*"ok"' \
                && echo "$new_log" | grep -q "Startup sync: "; then
            echo "sync done"
            return 0
        fi
        # Progress (print only when line changes)
        progress_line=$(echo "$new_log" | grep -oE "Indexing: [0-9]+/[0-9]+ encoded \([0-9]+s\)" | tail -1) || true
        if [[ -n "$progress_line" && "$progress_line" != "$last_progress" ]]; then
            echo "  $progress_line"
            last_progress="$progress_line"
        fi
        sleep 3
    done
}

_port_in_use() {
    (exec 3<>"/dev/tcp/127.0.0.1/$MIMIR_PORT") 2>/dev/null
}

_health_pid() {
    curl -sf "http://127.0.0.1:${MIMIR_PORT}/health" 2>/dev/null \
        | sed -n 's/.*"pid":[[:space:]]*\([0-9][0-9]*\).*/\1/p'
}

_require_free_port() {
    _port_in_use || return 0
    local holder
    holder=$(_health_pid) || true
    echo "ERROR: port $MIMIR_PORT is already in use${holder:+ by mimir server PID=$holder} — not starting" >&2
    exit 1
}

cmd_start() {
    mimir_validate
    local pid
    if pid=$(_get_pid 2>/dev/null); then
        echo "already running: PID=$pid"
        return 0
    fi
    _require_free_port
    mkdir -p "$(dirname "$MIMIR_LOG_FILE")"
    _rotate_log
    _check_meta
    _download_model

    # Detect full reindex: metadata.db absent after meta check means DB was cleared
    local reindex=0
    [[ -f "$MIMIR_CHROMA_DIR/metadata.db" ]] || reindex=1

    # Snapshot log length so _wait_for_reindex ignores lines from previous runs
    local log_lines=0
    [[ -f "$MIMIR_LOG_FILE" ]] && log_lines=$(wc -l < "$MIMIR_LOG_FILE")

    nohup uv --directory "$APP_DIR" run python -m mimir.server \
        >>"$MIMIR_LOG_FILE" 2>&1 &
    local launcher=$!
    echo "server process launched — log: $MIMIR_LOG_FILE"

    # Wait for HTTP server (sync now runs in background → server starts in seconds)
    local health="http://127.0.0.1:${MIMIR_PORT}/health"
    for i in $(seq 1 30); do
        sleep 1
        if curl -sf "$health" >/dev/null 2>&1; then
            local real_pid health_pid
            real_pid=$(cat "$PID_FILE" 2>/dev/null || echo "?")
            # The port may have been taken after the check above: /health must be our own server
            health_pid=$(_health_pid) || true
            if [[ "$health_pid" != "$real_pid" ]]; then
                echo "ERROR: port $MIMIR_PORT answered by another process (PID=${health_pid:-unknown}), not this server — check $MIMIR_LOG_FILE" >&2
                kill "$launcher" 2>/dev/null || true
                exit 1
            fi
            echo "started: PID=$real_pid, health status: curl http://127.0.0.1:${MIMIR_PORT}/health"
            curl -sf "$health" 2>/dev/null; echo
            # Under systemd do not wait for the sync: it can outlast TimeoutStartSec
            if [[ "$reindex" -eq 0 || -n "${INVOCATION_ID:-}" ]]; then
                exit 0
            fi
            echo "Syncing in background — monitoring $MIMIR_LOG_FILE ..."
            _wait_for_sync "$log_lines"
            exit $?
        fi
    done

    echo "ERROR: server did not start within 30s — check $MIMIR_LOG_FILE" >&2
    exit 1
}

cmd_stop() {
    local pid
    if ! pid=$(_get_pid 2>/dev/null); then
        echo "not running"
        return 0
    fi
    kill "$pid" 2>/dev/null || true
    rm -f "$PID_FILE"
    # Wait for process to actually exit (SIGTERM is async; up to 10s)
    local i
    for i in $(seq 1 10); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 1
    done
    echo "stopped: PID=$pid"
}

cmd_restart() {
    cmd_stop || true
    sleep 1
    cmd_start
}

_service_enabled() {
    # True only when the enabled user unit runs this installation with this configuration,
    # so a dev clone or an env-overridden test instance never stops the production service.
    systemctl --user is-enabled --quiet "$SERVICE_NAME" 2>/dev/null || return 1
    local unit
    unit=$(systemctl --user cat "$SERVICE_NAME" 2>/dev/null) || return 1
    [[ "$unit" == *"$APP_DIR/mimir.sh --start"* ]] || return 1
    # The unit sets no MIMIR_* variables: compare with the config resolved without them
    local print_config='printf "%s\n" "$MIMIR_PORT" "$MIMIR_PID_FILE" "$MIMIR_CHROMA_DIR" "$MIMIR_LOG_FILE" "$MIMIR_INDEX_DIR" "$MIMIR_WRITE_DIR"'
    local service_config
    service_config=$(env -i HOME="$HOME" PATH="$PATH" XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-}" APP_DIR="$APP_DIR" \
        bash -c "source \"\$APP_DIR/src/mimir_config.sh\"; mimir_resolve_config; $print_config") || return 1
    [[ "$service_config" == "$(eval "$print_config")" ]]
}

cmd_reindex() {
    local use_service=0
    _service_enabled && use_service=1
    if [[ "$use_service" -eq 1 ]]; then
        systemctl --user stop "$SERVICE_NAME"
    fi
    cmd_stop || true
    # Our server is stopped now: a taken port would block the restart, so keep the index
    _require_free_port
    echo "Clearing index DB ($MIMIR_CHROMA_DIR)..."
    rm -f "$MIMIR_CHROMA_DIR/metadata.db" \
          "$MIMIR_CHROMA_DIR/metadata.db-wal" \
          "$MIMIR_CHROMA_DIR/metadata.db-shm"
    if [[ "$use_service" -eq 0 ]]; then
        cmd_start
        return
    fi
    # Rotate before the snapshot so the log offset stays valid after the service starts
    _rotate_log
    local log_lines=0
    [[ -f "$MIMIR_LOG_FILE" ]] && log_lines=$(wc -l < "$MIMIR_LOG_FILE")
    echo "Starting $SERVICE_NAME..."
    systemctl --user start "$SERVICE_NAME"
    echo "Syncing in background — monitoring $MIMIR_LOG_FILE ..."
    _wait_for_sync "$log_lines"
}

cmd_init() {
    mimir_validate
    local write_dir="$MIMIR_WRITE_DIR"
    if [[ ! -d "$write_dir" ]]; then
        echo "ERROR: MIMIR_WRITE_DIR does not exist: $write_dir" >&2
        echo "       Create the directory first, then run --init." >&2
        exit 1
    fi
    echo "This will create the default category structure in write_dir: $write_dir"
    read -r -p "Continue? [y/N] " _confirm
    case "$_confirm" in
        [yY]|[yY][eE][sS]) ;;
        *) echo "Aborted."; exit 0 ;;
    esac
    echo "Initializing write_dir: $write_dir"
    local created=0 skipped=0

    _init_category() {
        local dir="$1" usage="$2"
        local cat_dir="$write_dir/$dir"
        local yaml="$cat_dir/.mimir.yaml"
        mkdir -p "$cat_dir"
        if [[ -f "$yaml" ]]; then
            echo "  skip  $dir/.mimir.yaml (already exists)"
            skipped=$((skipped + 1))
        else
            printf 'usage: "%s"\n' "$usage" > "$yaml"
            echo "  wrote $dir/.mimir.yaml"
            created=$((created + 1))
        fi
    }

    _init_category "projects" \
        "Per-project documents: stack overview, module map, architecture, data sources, design decisions. Specific to one named project or tool. Use this when the note describes how a particular system is built, not a reusable pattern."
    _init_category "reference" \
        "Facts, specs, credentials, API endpoints, IP plans, hardware profiles, inventories. No problem/solution framing — just 'here is the data'."
    _init_category "solutions" \
        "A specific problem encountered and its concrete resolution (bug fixes, troubleshooting guides, non-obvious procedures, one-time decisions), as well as generic, project-agnostic patterns and reusable snippets. Framing: 'I had problem X, here is how I solved it' or 'here is a reusable approach for Y'."

    echo "done: $created created, $skipped skipped"
}

cmd_config() {
    mimir_validate
    echo "Resolved configuration:"
    printf "  %-22s = %-45s [%s]\n" \
        "MIMIR_INDEX_DIR"    "$MIMIR_INDEX_DIR"    "${_SRC[MIMIR_INDEX_DIR]:-?}" \
        "MIMIR_WRITE_DIR"    "$MIMIR_WRITE_DIR"    "${_SRC[MIMIR_WRITE_DIR]:-?}" \
        "MIMIR_PORT"         "$MIMIR_PORT"          "${_SRC[MIMIR_PORT]:-?}" \
        "MIMIR_CHROMA_DIR"   "$MIMIR_CHROMA_DIR"   "${_SRC[MIMIR_CHROMA_DIR]:-?}" \
        "MIMIR_MODEL"        "$MIMIR_MODEL"         "${_SRC[MIMIR_MODEL]:-?}" \
        "MIMIR_IDLE_TIMEOUT" "$MIMIR_IDLE_TIMEOUT" "${_SRC[MIMIR_IDLE_TIMEOUT]:-?}" \
        "MIMIR_SYNC_INTERVAL" "$MIMIR_SYNC_INTERVAL" "${_SRC[MIMIR_SYNC_INTERVAL]:-?}" \
        "MIMIR_LOG_LEVEL"    "$MIMIR_LOG_LEVEL"    "${_SRC[MIMIR_LOG_LEVEL]:-?}" \
        "MIMIR_LOG_FILE"     "$MIMIR_LOG_FILE"     "${_SRC[MIMIR_LOG_FILE]:-?}" \
        "MIMIR_MAX_LOG_MB"   "$MIMIR_MAX_LOG_MB"   "${_SRC[MIMIR_MAX_LOG_MB]:-?}" \
        "MIMIR_MAX_LOG_FILES" "$MIMIR_MAX_LOG_FILES" "${_SRC[MIMIR_MAX_LOG_FILES]:-?}" \
        "MIMIR_USAGE_LOG_FILE" "$MIMIR_USAGE_LOG_FILE" "${_SRC[MIMIR_USAGE_LOG_FILE]:-?}" \
        "MIMIR_AUTH_TOKEN"   "$MIMIR_AUTH_TOKEN"   "${_SRC[MIMIR_AUTH_TOKEN]:-?}" \
        "MIMIR_BIND_HOST"    "$MIMIR_BIND_HOST"    "${_SRC[MIMIR_BIND_HOST]:-?}"
}

# --- Dispatch ---
case "${1:-}" in
    "")        cmd_help; cmd_status ;;
    --help)    cmd_help ;;
    --status)  cmd_status ;;
    --start)   cmd_start ;;
    --stop)    cmd_stop ;;
    --restart) cmd_restart ;;
    --reindex) cmd_reindex ;;
    --init)    cmd_init ;;
    --config)  cmd_config ;;
    *) echo "Usage: mimir.sh [--help|--status|--start|--stop|--restart|--reindex|--init|--config]" >&2; exit 1 ;;
esac
