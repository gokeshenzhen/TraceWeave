#!/usr/bin/env bash
# Canonical repository-local TraceWeave EDA installer.
#
# This is intentionally a thin orchestrator around the existing Source Graph
# and FSDB setup scripts. It never edits shell startup files or MCP client
# configuration, and it is not an extension mechanism for a PyPI installation.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SOURCE_GRAPH_SETUP="$REPO_ROOT/scripts/setup_source_graph.sh"
FSDB_SETUP="$REPO_ROOT/scripts/setup_fsdb.sh"
FSDB_VERIFY="$REPO_ROOT/scripts/verify_fsdb.sh"
VENV_PYTHON="$REPO_ROOT/.venv/bin/python"
SERVER="$REPO_ROOT/server.py"

MODE="install"
JSON_OUTPUT=0
VERDI_HOME_ARG=""
PRINT_CONFIG=""

usage() {
    cat <<'EOF'
Usage: bash scripts/install.sh [options]

Without arguments, install the complete repository-local EDA profile:
Source Graph, the FSDB wrapper, FSDB verification, and a repo runtime smoke
check. This command is idempotent and does not configure MCP clients.

Options:
  --check                       Strictly read-only readiness check
  --json                        Emit one machine-readable JSON receipt
  --verdi-home PATH             Use PATH as VERDI_HOME for this process only
  --print-config CLIENT         Print, but do not write, client configuration
                                CLIENT: codex, claude, copilot, or all
  -h, --help                    Show this help

--print-config implies --check. Run the normal installer first, then request a
configuration template. PyPI portable installations are intentionally outside
the scope of this repository-local installer.
EOF
}

usage_error() {
    printf '[install] ERROR: %s\n' "$1" >&2
    usage >&2
    exit 64
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --check)
            MODE="check"
            shift
            ;;
        --json)
            JSON_OUTPUT=1
            shift
            ;;
        --verdi-home)
            [ "$#" -ge 2 ] || usage_error "--verdi-home requires a path"
            [ -n "$2" ] || usage_error "--verdi-home path must not be empty"
            VERDI_HOME_ARG="$2"
            shift 2
            ;;
        --print-config)
            [ "$#" -ge 2 ] || usage_error "--print-config requires a client"
            case "$2" in
                codex|claude|copilot|all) PRINT_CONFIG="$2" ;;
                *) usage_error "unsupported client for --print-config: $2" ;;
            esac
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            usage_error "unknown argument: $1"
            ;;
    esac
done

if [ -n "$PRINT_CONFIG" ]; then
    MODE="check"
fi

if [ -n "$VERDI_HOME_ARG" ]; then
    export VERDI_HOME="$VERDI_HOME_ARG"
fi

log() {
    if [ "$JSON_OUTPUT" -eq 1 ]; then
        printf '[install] %s\n' "$*" >&2
    else
        printf '[install] %s\n' "$*"
    fi
}

run_step() {
    step_name="$1"
    shift
    log "$step_name"
    if [ "$JSON_OUTPUT" -eq 1 ]; then
        "$@" >&2
    else
        "$@"
    fi
}

emit_json() {
    ready="$1"
    failed_step="$2"
    exit_code="$3"
    json_python=""
    if [ -x "$VENV_PYTHON" ]; then
        json_python="$VENV_PYTHON"
    elif command -v python3 >/dev/null 2>&1; then
        json_python="$(command -v python3)"
    fi

    if [ -z "$json_python" ]; then
        # The path fields cannot be encoded safely without Python. Emit a valid
        # minimal receipt so an agent can still act on the prerequisite error.
        printf '{"schema_version":1,"profile":"repository_full_eda","mode":"%s","ready":%s,"failed_step":"json_encoder_unavailable","exit_code":%s}\n' \
            "$MODE" "$ready" "$exit_code"
        return
    fi

    "$json_python" - \
        "$MODE" "$ready" "$failed_step" "$exit_code" \
        "$REPO_ROOT" "$VENV_PYTHON" "$SERVER" "$PRINT_CONFIG" <<'PY'
import json
import sys

mode, ready, failed_step, exit_code, root, python, server, client = sys.argv[1:]
payload = {
    "schema_version": 1,
    "profile": "repository_full_eda",
    "mode": mode,
    "ready": ready == "true",
    "failed_step": failed_step or None,
    "exit_code": int(exit_code),
    "runtime": {
        "command": python,
        "args": [server],
        "cwd": root,
    },
}
if client:
    payload["requested_client_config"] = client
    common = {
        "command": python,
        "args": [server],
        "cwd": root,
    }
    configs = {}
    if client in {"codex", "all"}:
        configs["codex"] = {
            "target": "~/.codex/config.toml",
            "section": "mcp_servers.TraceWeave",
            **common,
        }
    if client in {"claude", "all"}:
        configs["claude"] = {
            "target": "~/.claude.json",
            "mcpServers": {
                "TraceWeave": {
                    "type": "stdio",
                    "command": python,
                    "args": [server],
                }
            },
        }
    if client in {"copilot", "all"}:
        configs["copilot_cli"] = {
            "target": "~/.copilot/mcp-config.json",
            "mcpServers": {
                "TraceWeave": {
                    "type": "local",
                    **common,
                    "tools": ["*"],
                }
            },
        }
        configs["vscode_copilot"] = {
            "target": ".vscode/mcp.json",
            "servers": {
                "TraceWeave": {
                    "type": "stdio",
                    **common,
                }
            },
        }
    payload["client_configs"] = configs
print(json.dumps(payload, sort_keys=True))
PY
}

fail_step() {
    failed_step="$1"
    exit_code="$2"
    if [ "$JSON_OUTPUT" -eq 1 ]; then
        emit_json false "$failed_step" "$exit_code"
    else
        printf '[install] ERROR: %s failed (exit %s)\n' \
            "$failed_step" "$exit_code" >&2
    fi
    exit "$exit_code"
}

repo_runtime_smoke() {
    [ -x "$VENV_PYTHON" ] || {
        printf '[install] ERROR: missing %s\n' "$VENV_PYTHON" >&2
        return 1
    }
    "$VENV_PYTHON" - "$REPO_ROOT" "$SERVER" <<'PY'
from pathlib import Path
import sys

repo_root = Path(sys.argv[1]).resolve()
server_path = Path(sys.argv[2]).resolve()
sys.path.insert(0, str(repo_root))

import config
import server

if Path(config.REPO_ROOT).resolve() != repo_root:
    raise SystemExit(
        f"config resolved {Path(config.REPO_ROOT).resolve()}, expected {repo_root}"
    )
if Path(server.__file__).resolve() != server_path:
    raise SystemExit(
        f"server resolved {Path(server.__file__).resolve()}, expected {server_path}"
    )
options = server.app.create_initialization_options()
if options.server_name != "traceweave":
    raise SystemExit(f"unexpected MCP server name: {options.server_name}")
print(
    f"repository MCP runtime ready: {options.server_name} "
    f"{options.server_version}"
)
PY
}

print_codex_config() {
    cat <<EOF
# Codex: merge into ~/.codex/config.toml
[mcp_servers.TraceWeave]
command = "$VENV_PYTHON"
args = ["$SERVER"]
cwd = "$REPO_ROOT"
EOF
}

print_claude_config() {
    "$VENV_PYTHON" - "$VENV_PYTHON" "$SERVER" <<'PY'
import json
import sys

print("# Claude Code: merge into the mcpServers object in ~/.claude.json")
print(json.dumps({
    "mcpServers": {
        "TraceWeave": {
            "type": "stdio",
            "command": sys.argv[1],
            "args": [sys.argv[2]],
        }
    }
}, indent=2))
PY
}

print_copilot_config() {
    "$VENV_PYTHON" - "$VENV_PYTHON" "$SERVER" "$REPO_ROOT" <<'PY'
import json
import sys

entry = {
    "type": "local",
    "command": sys.argv[1],
    "args": [sys.argv[2]],
    "cwd": sys.argv[3],
    "tools": ["*"],
}
print("# GitHub Copilot CLI: merge into ~/.copilot/mcp-config.json")
print(json.dumps({"mcpServers": {"TraceWeave": entry}}, indent=2))
print()
print("# VS Code Copilot: merge into .vscode/mcp.json")
vscode_entry = dict(entry)
vscode_entry["type"] = "stdio"
vscode_entry.pop("tools")
print(json.dumps({"servers": {"TraceWeave": vscode_entry}}, indent=2))
PY
}

print_client_config() {
    case "$PRINT_CONFIG" in
        codex) print_codex_config ;;
        claude) print_claude_config ;;
        copilot) print_copilot_config ;;
        all)
            print_codex_config
            printf '\n'
            print_claude_config
            printf '\n'
            print_copilot_config
            ;;
    esac
    cat <<'EOF'

# Command paths only. Preserve the site's required EDA and license environment
# as documented in README.md; this installer never prints or copies secrets.
EOF
}

for required_file in \
    "$SOURCE_GRAPH_SETUP" "$FSDB_SETUP" "$FSDB_VERIFY" "$SERVER"; do
    [ -f "$required_file" ] || fail_step "missing_repository_file" 1
done

if [ "$MODE" = "check" ]; then
    run_step "checking Source Graph environment" \
        bash "$SOURCE_GRAPH_SETUP" --check
    status=$?
    [ "$status" -eq 0 ] || fail_step "source_graph_check" "$status"
else
    run_step "installing Source Graph environment" bash "$SOURCE_GRAPH_SETUP"
    status=$?
    [ "$status" -eq 0 ] || fail_step "source_graph_setup" "$status"

    run_step "building repository-local FSDB support" bash "$FSDB_SETUP"
    status=$?
    [ "$status" -eq 0 ] || fail_step "fsdb_setup" "$status"
fi

run_step "verifying repository-local FSDB support" bash "$FSDB_VERIFY"
status=$?
[ "$status" -eq 0 ] || fail_step "fsdb_verify" "$status"

run_step "checking repository MCP runtime" repo_runtime_smoke
status=$?
[ "$status" -eq 0 ] || fail_step "repo_runtime_smoke" "$status"

if [ "$JSON_OUTPUT" -eq 1 ]; then
    emit_json true "" 0
else
    log "repository-local full EDA profile is ready"
    log "MCP command: $VENV_PYTHON $SERVER"
    log "Verdi NPI remains conditional on the site's KDB, pynpi, license, and local/LSF environment."
    if [ -n "$PRINT_CONFIG" ]; then
        printf '\n'
        print_client_config
    fi
fi
