#!/usr/bin/env bash
# verify_fsdb.sh
# Verifies that FSDB support is functional in the current environment, without
# requiring $VERDI_HOME — only the repo-local artefacts produced by
# scripts/setup_fsdb.sh are inspected.
#
# Checks:
#   1. third_party/verdi_runtime/linux64/{libnsys.so,libnffr.so} exist & load
#   2. libfsdb_wrapper.so exists, links cleanly, and exports fsdb_* symbols
#   3. (best effort) Python `import` of src.fsdb_parser succeeds
#
# Usage: bash scripts/verify_fsdb.sh
# Exit codes:
#   0  all checks passed
#   1  a check failed (see message)

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

RUNTIME_DIR="$REPO_ROOT/third_party/verdi_runtime/linux64"
WRAPPER="$REPO_ROOT/libfsdb_wrapper.so"

pass() { printf '  [ OK ] %s\n' "$*"; }
fail() { printf '  [FAIL] %s\n' "$*" >&2; FAILED=1; }

FAILED=0

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
    for cand in python3.11 python3 python; do
        if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
    done
fi
[ -n "$PY" ] || { echo "ERROR: no python interpreter found (set \$PYTHON)" >&2; exit 1; }

echo "[verify_fsdb] repo root = $REPO_ROOT"
echo "[verify_fsdb] python    = $PY ($($PY --version 2>&1))"
echo

# --- Check 1: runtime libraries present & loadable --------------------------
echo "[1/3] Verdi FSDB runtime"
for lib in libnsys.so libnffr.so; do
    if [ -e "$RUNTIME_DIR/$lib" ]; then
        pass "found $RUNTIME_DIR/$lib"
    else
        fail "missing $RUNTIME_DIR/$lib  (run: bash scripts/setup_fsdb.sh)"
    fi
done

if [ "$FAILED" -eq 0 ]; then
    "$PY" - "$RUNTIME_DIR" <<'PYEOF'
import ctypes, ctypes.util, os, sys
d = sys.argv[1]
# libnffr depends on zlib (gzflush etc.) — preload it globally so the
# subsequent CDLL can resolve those symbols on hosts where libz isn't
# already pulled in by the interpreter.
zname = ctypes.util.find_library("z") or "libz.so.1"
try:
    ctypes.CDLL(zname, ctypes.RTLD_GLOBAL)
except OSError as e:
    print(f"  [FAIL] could not preload zlib ({zname}): {e}", file=sys.stderr)
    sys.exit(1)
try:
    ctypes.CDLL(os.path.join(d, "libnsys.so"), ctypes.RTLD_GLOBAL)
    ctypes.CDLL(os.path.join(d, "libnffr.so"))
except OSError as e:
    print(f"  [FAIL] ctypes load failed: {e}", file=sys.stderr)
    sys.exit(1)
print("  [ OK ] libnsys.so + libnffr.so loaded via ctypes")
PYEOF
    [ $? -eq 0 ] || FAILED=1
fi
echo

# --- Check 2: wrapper present, links, exports fsdb_* symbols ----------------
echo "[2/3] libfsdb_wrapper.so"
if [ ! -f "$WRAPPER" ]; then
    fail "missing $WRAPPER  (run: bash scripts/setup_fsdb.sh)"
else
    pass "found $WRAPPER"

    if command -v ldd >/dev/null 2>&1; then
        if ldd "$WRAPPER" 2>&1 | grep -q "not found"; then
            echo "  [FAIL] unresolved shared libraries:" >&2
            ldd "$WRAPPER" | grep "not found" >&2
            FAILED=1
        else
            pass "ldd reports all dependencies resolved"
        fi
    fi

    if command -v nm >/dev/null 2>&1; then
        SYMS=$(nm -D --defined-only "$WRAPPER" 2>/dev/null | awk '$2=="T" && $3 ~ /^fsdb_/ {print $3}')
        if [ -n "$SYMS" ]; then
            COUNT=$(printf '%s\n' "$SYMS" | wc -l)
            pass "$COUNT exported fsdb_* symbols"
        else
            fail "no exported fsdb_* symbols found in $WRAPPER"
        fi
    fi

    "$PY" - "$WRAPPER" <<'PYEOF'
import ctypes, sys
try:
    ctypes.CDLL(sys.argv[1])
except OSError as e:
    print(f"  [FAIL] ctypes load of wrapper failed: {e}", file=sys.stderr)
    sys.exit(1)
print("  [ OK ] libfsdb_wrapper.so loaded via ctypes")
PYEOF
    [ $? -eq 0 ] || FAILED=1
fi
echo

# --- Check 3: Python parser importable --------------------------------------
echo "[3/3] Python FSDB parser import"
"$PY" - <<'PYEOF'
import sys
sys.path.insert(0, "src")
try:
    import fsdb_parser  # noqa: F401
except Exception as e:
    print(f"  [WARN] could not import src/fsdb_parser.py: {e}")
    sys.exit(0)  # not fatal: parser may be optional in some checkouts
print("  [ OK ] src/fsdb_parser imported")
PYEOF
echo

if [ "$FAILED" -ne 0 ]; then
    echo "[verify_fsdb] FAILED — see messages above."
    exit 1
fi
echo "[verify_fsdb] All checks passed. FSDB support is ready."
