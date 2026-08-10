#!/usr/bin/env bash
# Create a repository-local Python environment with TraceWeave's base runtime
# and the pinned pyslang frontend used by Source Graph.
#
# Usage:
#   bash scripts/setup_source_graph.sh
#   bash scripts/setup_source_graph.sh --check
#
# The script never edits shell startup files or MCP client configuration.
# It is safe to rerun after git pull; pip reconciles the pinned requirements.
#
# Exit codes:
#   0  environment is ready
#   1  prerequisite or environment check failed
#   2  virtual-environment creation or dependency installation failed

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV_DIR="$REPO_ROOT/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"
REQUIREMENTS="$REPO_ROOT/requirements-source-graph.txt"
SERVER="$REPO_ROOT/server.py"
SETUP_PYTHON="${TRACEWEAVE_SETUP_PYTHON:-python3.11}"
EXPECTED_PYSLANG_VERSION="11.0.0"
MODE="install"

log()  { printf '[setup_source_graph] %s\n' "$*"; }
fail() { printf '[setup_source_graph] ERROR: %s\n' "$1" >&2; exit "${2:-1}"; }

usage() {
    cat <<'EOF'
Usage: bash scripts/setup_source_graph.sh [--check]

Without arguments, create/update .venv and install the complete TraceWeave
Source Graph runtime. --check is strictly read-only and only verifies the
existing .venv.

Optional environment:
  TRACEWEAVE_SETUP_PYTHON  Python used to create .venv (default: python3.11)
EOF
}

case "${1:-}" in
    "") ;;
    --check) MODE="check" ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; fail "unknown argument: $1" ;;
esac

verify_environment() {
    [ -x "$VENV_PYTHON" ] || fail \
        "environment not found at $VENV_DIR; run: bash scripts/setup_source_graph.sh"

    "$VENV_PYTHON" -c '
import importlib.metadata
import sys

if sys.version_info < (3, 11):
    raise SystemExit(f"Python 3.11+ required, found {sys.version.split()[0]}")

import mcp  # noqa: F401
import yaml  # noqa: F401
from pyslang import driver

expected = sys.argv[1]
actual = importlib.metadata.version("pyslang")
if actual != expected:
    raise SystemExit(f"pyslang version mismatch: expected {expected}, found {actual}")
mcp_version = importlib.metadata.version("mcp")
pyyaml_version = importlib.metadata.version("PyYAML")

print(f"Python {sys.version.split()[0]}")
print(f"mcp {mcp_version}")
print(f"PyYAML {pyyaml_version}")
print(f"pyslang {actual}")
print(f"pyslang driver {driver.Driver.__name__}")
' "$EXPECTED_PYSLANG_VERSION"
}

if [ "$MODE" = "check" ]; then
    log "checking $VENV_DIR"
    verify_environment
    log "environment is ready"
    exit 0
fi

command -v "$SETUP_PYTHON" >/dev/null 2>&1 || fail \
    "$SETUP_PYTHON not found; install Python 3.11+ or set TRACEWEAVE_SETUP_PYTHON"

"$SETUP_PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
    || fail "$SETUP_PYTHON must be Python 3.11 or newer"

[ -f "$REQUIREMENTS" ] || fail "missing $REQUIREMENTS"
[ -f "$SERVER" ] || fail "missing $SERVER"

if [ ! -d "$VENV_DIR" ]; then
    log "creating $VENV_DIR with $SETUP_PYTHON"
    "$SETUP_PYTHON" -m venv "$VENV_DIR" \
        || fail "failed to create $VENV_DIR" 2
elif [ ! -x "$VENV_PYTHON" ]; then
    fail "$VENV_DIR exists but $VENV_PYTHON is missing or not executable"
else
    log "reusing $VENV_DIR"
fi

log "installing pinned Source Graph runtime"
"$VENV_PYTHON" -m pip install \
    --disable-pip-version-check \
    --only-binary=pyslang \
    -r "$REQUIREMENTS" \
    || fail "dependency installation failed" 2

verify_environment

log "Source Graph environment is ready"
log "MCP Python: $VENV_PYTHON"
log "TraceWeave server: $SERVER"
cat <<EOF

Register it only in the MCP clients you use (the setup script does not edit
user configuration):

  codex mcp add TraceWeave -- "$VENV_PYTHON" "$SERVER"
  claude mcp add --scope user TraceWeave -- "$VENV_PYTHON" "$SERVER"

If TraceWeave is already registered, update its command to the MCP Python shown
above instead of adding a duplicate entry. Restart or reconnect the MCP client
after changing its configuration.
EOF
