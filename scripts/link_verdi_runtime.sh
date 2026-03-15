#!/bin/bash
# link_verdi_runtime.sh
# 在仓库内创建 Verdi FSDB runtime 软链接，供 libfsdb_wrapper.so 的相对 RPATH 使用

set -euo pipefail

if [ -z "${VERDI_HOME:-}" ]; then
    echo "ERROR: VERDI_HOME is required."
    echo "Example: export VERDI_HOME=/tools/synopsys/verdi/O-2018.09-SP2-11"
    exit 1
fi

SRC_DIR="$VERDI_HOME/share/FsdbReader/linux64"
DST_DIR="$(cd "$(dirname "$0")/.." && pwd)/third_party/verdi_runtime/linux64"

for lib in libnsys.so libnffr.so; do
    if [ ! -f "$SRC_DIR/$lib" ]; then
        echo "ERROR: missing $SRC_DIR/$lib"
        exit 1
    fi
done

mkdir -p "$DST_DIR"

for lib in libnsys.so libnffr.so; do
    ln -sfn "$SRC_DIR/$lib" "$DST_DIR/$lib"
    echo "linked $DST_DIR/$lib -> $SRC_DIR/$lib"
done

echo "Verdi FSDB runtime is ready under $DST_DIR"
