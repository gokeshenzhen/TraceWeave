#!/usr/bin/env bash
# setup_fsdb.sh
# One-shot FSDB enablement: link the Verdi runtime into the repo, then build
# libfsdb_wrapper.so. Designed to be portable across sites — the only required
# input is a working Verdi installation exposed via $VERDI_HOME.
#
# Usage:
#   export VERDI_HOME=/path/to/verdi/<version>
#   bash scripts/setup_fsdb.sh
#
# Exit codes:
#   0  setup succeeded
#   1  prerequisite missing (env / tool / file)
#   2  a sub-step (link or build) failed

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

log()  { printf '[setup_fsdb] %s\n' "$*"; }
fail() { printf '[setup_fsdb] ERROR: %s\n' "$*" >&2; exit "${2:-1}"; }

# --- Environment checks -----------------------------------------------------
case "$(uname -s)" in
    Linux) ;;
    *) fail "FSDB runtime is only shipped for Linux (uname -s = $(uname -s))." ;;
esac

if [ -z "${VERDI_HOME:-}" ]; then
    fail "VERDI_HOME is not set. Export it to your Verdi install root, e.g.:
       export VERDI_HOME=/tools/synopsys/verdi/<version>"
fi

if [ ! -d "$VERDI_HOME/share/FsdbReader/linux64" ]; then
    fail "VERDI_HOME=$VERDI_HOME does not look like a Verdi install
       (missing share/FsdbReader/linux64). Point VERDI_HOME at the Verdi root
       that contains share/FsdbReader/linux64/{libnsys.so,libnffr.so}."
fi

command -v g++ >/dev/null 2>&1 || fail "g++ not found in PATH. Install a C++11-capable g++ (e.g. devtoolset / build-essential)."

BUILD_SCRIPT="$REPO_ROOT/build_wrapper.sh"
[ -f "$BUILD_SCRIPT" ] || fail "missing $BUILD_SCRIPT"

log "VERDI_HOME = $VERDI_HOME"
log "repo root  = $REPO_ROOT"

# --- Step 1: link runtime ---------------------------------------------------
log "Step 1/2: linking Verdi FSDB runtime into third_party/verdi_runtime/linux64"
SRC_DIR="$VERDI_HOME/share/FsdbReader/linux64"
DST_DIR="$REPO_ROOT/third_party/verdi_runtime/linux64"
mkdir -p "$DST_DIR"
for lib in libnsys.so libnffr.so; do
    [ -f "$SRC_DIR/$lib" ] || fail "missing $SRC_DIR/$lib in this Verdi install"
    ln -sfn "$SRC_DIR/$lib" "$DST_DIR/$lib"
    log "  linked $DST_DIR/$lib -> $SRC_DIR/$lib"
done

# --- Step 2: build wrapper --------------------------------------------------
log "Step 2/2: building libfsdb_wrapper.so"
if ! bash "$BUILD_SCRIPT"; then
    fail "build_wrapper.sh failed; see output above." 2
fi

log "FSDB setup complete."
log "Next: bash scripts/verify_fsdb.sh"
