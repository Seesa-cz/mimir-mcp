#!/usr/bin/env bash
# mimir_config.sh — shared config resolution library
#
# Resolves MIMIR_* variables from three layers (later overrides earlier):
#   1. ~/.config/mimir/config.yaml  (user config)
#   2. <app_dir>/config.yaml        (app/instance config)
#   3. MIMIR_* env vars             (highest priority)
#
# Usage: source this file with APP_DIR already set.
#   APP_DIR="$(cd "$(dirname "$0")" && pwd)"
#   source "$APP_DIR/src/mimir_config.sh"
#   mimir_resolve_config

# Track source of each resolved variable
declare -A _SRC

_set_var() {
    local key="$1" val="$2" src="$3"
    export "$key=$val"
    _SRC[$key]="$src"
}

_load_yaml() {
    local file="$1" src="$2"
    [ -f "$file" ] || return 0
    while IFS= read -r line; do
        [[ "$line" =~ ^[[:space:]]*# ]] && continue
        [[ -z "${line// }" ]] && continue
        [[ "$line" != *:* ]] && continue
        local key val
        key="${line%%:*}"
        key="${key// /}"
        val="${line#*:}"
        val="${val#"${val%%[![:space:]]*}"}"
        val="${val%"${val##*[![:space:]]}"}"
        case "$val" in
            '"'*'"') val="${val:1:${#val}-2}" ;;
            "'"*"'") val="${val:1:${#val}-2}" ;;
        esac
        val="${val/#\~/$HOME}"
        case "$key" in
            port)                  _set_var MIMIR_PORT             "$val" "$src" ;;
            index_dir)             _set_var MIMIR_INDEX_DIR        "$val" "$src" ;;
            write_dir)             _set_var MIMIR_WRITE_DIR        "$val" "$src" ;;
            chroma_dir)            _set_var MIMIR_CHROMA_DIR       "$val" "$src" ;;
            model)                 _set_var MIMIR_MODEL            "$val" "$src" ;;
            idle_timeout_minutes)  _set_var MIMIR_IDLE_TIMEOUT     "$val" "$src" ;;
            sync_interval_minutes) _set_var MIMIR_SYNC_INTERVAL    "$val" "$src" ;;
            log_level)             _set_var MIMIR_LOG_LEVEL        "$val" "$src" ;;
            log_file)              _set_var MIMIR_LOG_FILE         "$val" "$src" ;;
            max_log_mb)            _set_var MIMIR_MAX_LOG_MB       "$val" "$src" ;;
            max_log_files)         _set_var MIMIR_MAX_LOG_FILES    "$val" "$src" ;;
            usage_log_file)        _set_var MIMIR_USAGE_LOG_FILE   "$val" "$src" ;;
            auth_token)            _set_var MIMIR_AUTH_TOKEN       "$val" "$src" ;;
            bind_host)             _set_var MIMIR_BIND_HOST        "$val" "$src" ;;
            fastembed_cache_path)  _set_var MIMIR_FASTEMBED_CACHE  "$val" "$src" ;;
            upgrade_check)         _set_var MIMIR_UPGRADE_CHECK    "$val" "$src" ;;
            upgrade_check_interval_minutes) _set_var MIMIR_UPGRADE_CHECK_INTERVAL "$val" "$src" ;;
        esac
    done < "$file"
}

mimir_resolve_config() {
    local config_home="${XDG_CONFIG_HOME:-$HOME/.config}"
    local user_config="$config_home/mimir/config.yaml"
    local app_config="$APP_DIR/config.yaml"

    # Snapshot env vars set before resolution (highest priority)
    declare -A _ENV_BEFORE
    local _v
    for _v in MIMIR_PORT MIMIR_INDEX_DIR MIMIR_WRITE_DIR MIMIR_CHROMA_DIR \
              MIMIR_MODEL MIMIR_IDLE_TIMEOUT MIMIR_SYNC_INTERVAL MIMIR_LOG_LEVEL \
              MIMIR_LOG_FILE MIMIR_MAX_LOG_MB MIMIR_MAX_LOG_FILES MIMIR_USAGE_LOG_FILE \
              MIMIR_AUTH_TOKEN MIMIR_BIND_HOST MIMIR_PID_FILE MIMIR_FASTEMBED_CACHE \
              MIMIR_UPGRADE_CHECK MIMIR_UPGRADE_CHECK_INTERVAL; do
        [[ -n "${!_v+x}" ]] && _ENV_BEFORE[$_v]="${!_v}"
    done

    # Layer 1: user config
    _load_yaml "$user_config" "~/.config"
    # Layer 2: app config
    _load_yaml "$app_config"  "app_dir"
    # Layer 3: restore env vars (override everything)
    for _v in "${!_ENV_BEFORE[@]}"; do
        _set_var "$_v" "${_ENV_BEFORE[$_v]}" "env"
    done

    # Derived defaults for optional variables
    [[ -z "${MIMIR_CHROMA_DIR:-}"    ]] && _set_var MIMIR_CHROMA_DIR     "$APP_DIR/.chroma"    "derived"
    [[ -z "${MIMIR_MODEL:-}"         ]] && _set_var MIMIR_MODEL          "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2" "derived"
    [[ -z "${MIMIR_IDLE_TIMEOUT:-}"  ]] && _set_var MIMIR_IDLE_TIMEOUT   "0"                   "derived"
    [[ -z "${MIMIR_SYNC_INTERVAL:-}" ]] && _set_var MIMIR_SYNC_INTERVAL  "5"                   "derived"
    [[ -z "${MIMIR_LOG_LEVEL:-}"     ]] && _set_var MIMIR_LOG_LEVEL      "warning"             "derived"
    [[ -z "${MIMIR_LOG_FILE:-}"      ]] && _set_var MIMIR_LOG_FILE       "$APP_DIR/server.log" "derived"
    [[ -z "${MIMIR_MAX_LOG_MB:-}"    ]] && _set_var MIMIR_MAX_LOG_MB     "5"                   "derived"
    [[ -z "${MIMIR_MAX_LOG_FILES:-}" ]] && _set_var MIMIR_MAX_LOG_FILES  "5"                   "derived"
    [[ -z "${MIMIR_USAGE_LOG_FILE:-}"  ]] && _set_var MIMIR_USAGE_LOG_FILE  "$APP_DIR/usage.jsonl"    "derived"
    [[ -z "${MIMIR_AUTH_TOKEN:-}"      ]] && _set_var MIMIR_AUTH_TOKEN      ""                        "derived"
    [[ -z "${MIMIR_BIND_HOST:-}"       ]] && _set_var MIMIR_BIND_HOST       "127.0.0.1"               "derived"
    [[ -z "${MIMIR_FASTEMBED_CACHE:-}" ]] && _set_var MIMIR_FASTEMBED_CACHE "$HOME/.cache/fastembed"  "derived"
    [[ -z "${MIMIR_UPGRADE_CHECK:-}" ]] && _set_var MIMIR_UPGRADE_CHECK  "true"                "derived"
    [[ -z "${MIMIR_UPGRADE_CHECK_INTERVAL:-}" ]] && _set_var MIMIR_UPGRADE_CHECK_INTERVAL "360" "derived"
    # PID file always in APP_DIR — not configurable via config layers
    [[ -z "${MIMIR_PID_FILE:-}"      ]] && _set_var MIMIR_PID_FILE       "$APP_DIR/server.pid" "derived"
    return 0
}

mimir_validate() {
    local ok=1
    for var in MIMIR_INDEX_DIR MIMIR_WRITE_DIR MIMIR_PORT; do
        if [[ -z "${!var:-}" ]]; then
            echo "ERROR: Required variable $var is not set." >&2
            ok=0
        fi
    done
    if [[ ! "$MIMIR_MODEL" =~ ^[A-Za-z0-9._/-]+$ ]]; then
        echo "ERROR: MIMIR_MODEL contains invalid characters: \"$MIMIR_MODEL\"" >&2
        echo "       Model name must match [A-Za-z0-9._/-]+ (no spaces)." >&2
        ok=0
    fi
    [[ "$ok" -eq 1 ]] || exit 1
}
