#!/usr/bin/env bash
# Explicit real-EDA fixture creation. Never invoked by the default test suite.
set -euo pipefail

if [[ $# != 1 ]]; then
  echo "Usage: bash run.sh NEW_OUTPUT_DIRECTORY" >&2
  exit 2
fi
: "${VERDI_HOME:?VERDI_HOME must point to the existing Verdi installation}"
fixture_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# A fresh directory prevents incremental no-op logs or stale simulation data.
mkdir -- "$1"
output_dir="$(cd "$1" && pwd)"
for side in a b; do
  mkdir -- "${output_dir}/${side}"
  cp -- "${fixture_dir}/${side}.sv" "${output_dir}/${side}/${side}.sv"
  (
    cd "${output_dir}/${side}"
    # Capture sources at persistent paths, independent of a temporary checkout.
    cmd=(vcs -full64 -sverilog -timescale=1ns/1ps -debug_access+all -kdb
         "${output_dir}/${side}/${side}.sv" -top "p4_s9_${side}_tb"
         -o simv -l compile.log)
    printf '%q ' "${cmd[@]}" > build-command.txt
    printf '\n' >> build-command.txt
    "${cmd[@]}"
    ./simv -l sim.log
  )
done
