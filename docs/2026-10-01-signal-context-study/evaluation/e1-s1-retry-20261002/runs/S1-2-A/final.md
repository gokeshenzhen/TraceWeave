`tb.dut.uart_core.uart_tx.tx_q` 的驱动位于 [uart_tx.sv 第 45 行](/cache/Projects/opentitan/out/dvsim/master/uart-sim-vcs/default/fusesoc-work/src/lowrisc_ip_uart_0.1/rtl/uart_tx.sv:45) 的 `always_ff` 块，赋值位置为：

- 第 **49** 行：`tx_q <= 1'b1;`
- 第 **53** 行：`tx_q <= tx_d;`

编译路径已核验；TraceWeave NPI 精确定位，当前非递归查询完整、未截断。仅确认驱动位置，未分析时序或行为；历史 source/wave 身份仍未独立证明。