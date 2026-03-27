#!/bin/bash
# build_wrapper.sh
# 编译 fsdb_wrapper.cpp → libfsdb_wrapper.so
# 使用方法：在 TraceWeave/ 目录下执行 bash build_wrapper.sh

set -e

if [ -z "$VERDI_HOME" ]; then
    echo "ERROR: build_wrapper.sh requires VERDI_HOME for Verdi headers and link-time libraries."
    echo "Set VERDI_HOME before running this script."
    exit 1
fi

INC_DIR="$VERDI_HOME/share/FsdbReader"
LIB_DIR="$VERDI_HOME/share/FsdbReader/linux64"
OUT="libfsdb_wrapper.so"
SRC="fsdb_wrapper.cpp"
RUNTIME_RPATH='$ORIGIN/third_party/verdi_runtime/linux64'

echo "VERDI_HOME = $VERDI_HOME"
echo "INC_DIR    = $INC_DIR"
echo "LIB_DIR    = $LIB_DIR"
echo "RPATH      = $RUNTIME_RPATH"
echo ""

g++ -shared -fPIC -std=c++11 \
    -I"$INC_DIR" \
    -o "$OUT" \
    "$SRC" \
    -L"$LIB_DIR" \
    -lnffr -lnsys /usr/lib64/libz.so.1 \
    -Wl,-rpath,"$RUNTIME_RPATH"

echo ""
echo "编译成功：$OUT"
echo "验证符号："
nm -D "$OUT" | grep " T fsdb_" | c++filt
