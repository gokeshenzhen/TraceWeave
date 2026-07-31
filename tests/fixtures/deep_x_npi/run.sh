#!/usr/bin/env bash
set -euo pipefail

fixture_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
work_dir="${fixture_dir}/work"
: "${VERDI_HOME:?VERDI_HOME must point to a Verdi installation}"

mkdir -p "${work_dir}"
cd "${work_dir}"

# TraceWeave derives the compiled source set from VCS parsing records.  VCS's
# incremental no-op path overwrites compile.log with only "design hasn't
# changed", so invalidate its generated timestamp before every fixture run.
rm -f "${work_dir}/simv.daidir/.vcs.timestamp"

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
