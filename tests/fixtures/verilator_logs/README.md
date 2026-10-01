These small logs are captured output, generated on 2026-10-01 with Verilator
5.034. Absolute paths and source line numbers are preserved as detector evidence.
They are not locations of the injected RTL fault.

* X-HEEP: upstream 0823ac67c455f76339a9630a1a4bc1c642ec149d,
  CV32E40P, NtoM bus, four RAM banks, original `example_dma` checker.
  `xheep_fail.log` flips bit 0 only at DMA write bus mapping; software exits 1
  while the C++ host exits 0. `xheep_timeout.log` blocks DMA grant reception.
  `xheep_pass.log` restores the mapping. PRINTF_IN_SIM is enabled and a separate
  halfcycle exit observer prints its FST dump time in ps. UART and native output
  are mechanically concatenated; UART comparisons have no reliable timestamps.
* OpenTitan: local Earlgrey 1d1c37e3c411ef81f844332b893c7991ae7892af,
  `//sw/device/tests:aes_smoketest_sim_verilator`, complete chip_sim_tb execution.
  Fail flips bit 0 of the first AES output readback word, leaving the known-answer
  checker unchanged. Pass restores it. Native and UART output are concatenated.
  The software detector is aes_testutils.c, the SV detector sw_test_status_if.sv.
* `cocotb_warnings.log`: two unmodified bounded excerpts of Corundum's mixed
  compile/run log. `ERROR:cocotb:` wraps compiler warnings. The runtime banner
  occurs at original line 8265. This excerpt carries no PASS oracle.
* `led_normal.log`: VexRiscv LED output, no checker or usable simulation time.
* `native_fail.log`: a real consistency assertion with mutated DUT data.
  Copy mutant.sv to native.sv and build using:
  `verilator --cc --exe --build --assert --no-timing --top-module native -j 4 native.sv main.cpp`.
  Running obj_dir/Vnative produces the error and paired stop footer (host 134).
  Copy golden.sv to native.sv and repeat to recover host 0. The C++ context is
  1250ps but SV $time is quantized to 1000ps; do not infer this from a wave header.
* `cocotb_fail.log`: actual runtime excerpt using cocotb 1.9.2. Compile mutant.sv
  with top `dut`, the cocotb Verilator runner and test_data.py (copy
  test_data.py.txt to the external run directory). Timer(1.25ns)
  triggers the original Python expected-data assertion. The XML result contains
  a failure although the runner host returns 0. Warning/build output is omitted.

Full SoC build commands, source/binary hashes, independent ground-truth patches,
FSTs, unit evidence, and MCP receipts remain local acceptance artifacts rather
than repository fixtures. These excerpts test formats, not standalone SoC builds.
