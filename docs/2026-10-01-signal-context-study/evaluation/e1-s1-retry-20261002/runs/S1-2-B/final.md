`tb.dut.uart_core.uart_tx.tx_q` 的驱动位于 [uart_tx.sv:45](/cache/Projects/opentitan/out/dvsim/master/uart-sim-vcs/default/fusesoc-work/src/lowrisc_ip_uart_0.1/rtl/uart_tx.sv:45) 的 `always_ff` 块：

- **49 行**：`tx_q <= 1'b1;`
- **53 行**：`tx_q <= tx_d;`

已通过 TraceWeave 核验编译路径、实例和直接驱动，并对照源码；直接驱动查询完整、未截断。第 32 行 `assign tx = tx_q` 是读取端。

仅完成源码定位。历史 source/wave 身份一致性仍未独立证明，未分析时序或行为。