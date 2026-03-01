#!/bin/bash
# build_wrapper.sh
# 编译 fsdb_wrapper.cpp → libfsdb_wrapper.so
# 使用方法：在 waveform_mcp/ 目录下执行 bash build_wrapper.sh

set -e

VERDI_HOME=${VERDI_HOME:-/home/eda/app/synopsys/verdi/W-2024.09-SP1}
INC_DIR="$VERDI_HOME/share/FsdbReader"
LIB_DIR="$VERDI_HOME/share/FsdbReader/linux64"
OUT="libfsdb_wrapper.so"
SRC="fsdb_wrapper.cpp"

echo "VERDI_HOME = $VERDI_HOME"
echo "INC_DIR    = $INC_DIR"
echo "LIB_DIR    = $LIB_DIR"
echo ""

g++ -shared -fPIC -std=c++11 \
    -I"$INC_DIR" \
    -o "$OUT" \
    "$SRC" \
    -L"$LIB_DIR" \
    -lnffr -lnsys /usr/lib64/libz.so.1 \
    -Wl,-rpath,"$LIB_DIR"

echo ""
echo "编译成功：$OUT"
echo "验证符号："
nm -D "$OUT" | grep " T fsdb_" | c++filt
