#!/usr/bin/env bash
set -euo pipefail

fixture_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
work_dir="${fixture_dir}/work"
: "${VERDI_HOME:?VERDI_HOME must point to a Verdi installation}"

mkdir -p "${work_dir}"
cd "${work_dir}"

vcs \
  -full64 \
  -sverilog \
  -timescale=1ns/1ps \
  -debug_access+all \
  -kdb \
  "${fixture_dir}/rtl/deep_uart_x.sv" \
  "${fixture_dir}/tb/deep_x_tb.sv" \
  -top uart_deep_x_tb \
  -o simv \
  -l compile.log

rm -f "${work_dir}/deep_x.fsdb"
./simv -l sim.log
