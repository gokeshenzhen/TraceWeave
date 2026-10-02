**7145478 ps 的 tx_q 更新符合正常同步时序；在 7074050–7288500 ps 已查范围内，未发现此次更新异常。** 它在 `clk_i` 上升沿采样，与 `tick_baud_q` 下降同刻；tick 是组合逻辑的使能条件。

下表 before/after 分别表示同一时刻事件前、后的值：

| 时间（ps）／相位 | tick_baud_q | tx_d | tx_q | sreg_q | bit_cnt_q |
|---|---:|---:|---:|---:|---:|
| 7109764 before | 0 | 1 | 1 | 0x7da | 10 |
| 7109764 after | 1 | 0 | 1 | 0x7da | 10 |
| 7145478 before | 1 | 0 | 1 | 0x7da | 10 |
| 7145478 after | 0 | 0 | 0 | 0x7ed | 9 |

直接数据和控制条件均吻合：

- 窗口内 `rst_ni=1、tx_enable=1、wr=0`；更新前 `tick_baud_q=1、bit_cnt_q=10`，满足移位条件。
- 更新前 `sreg_q[0]=0`，所以 `tx_d=0`；同时 `sreg_d=0x7ed、bit_cnt_d=9`，随后一起被寄存。**更新后 sreg_q[0]=1，不能拿它解释本次采样。**
- 当前 `wr_data=0x00`，但 `wr=0`，没有重新装载。已核验的 [uart_tx.sv](/cache/Projects/opentitan/out/dvsim/master/uart-sim-vcs/default/fusesoc-work/src/lowrisc_ip_uart_0.1/rtl/uart_tx.sv:45) 明确在 `posedge clk_i` 执行 `tx_q <= tx_d`。

保留并核对了两个竞争解释：

1. **正常逐拍传播**：7074050 ps 的 `tick_baud_x16` 上升；7109764 ps 分频计数 `15→0`、tick 上升并生成 `tx_d=0`；7145478 ps 寄存输出。波形支持此解释。
2. **异常边沿或控制覆盖导致更新**：核对了相反侧的数据、reset、enable、wr 和输出传播，未见支持证据。局部时钟半周期均为 **17857 ps**；工具的 `transient` 标签对应正常时钟半周，不能据此判断毛刺。

双方已查边界：上游到 NCO 最高位、tick 输入及 FIFO 写入条件；下游到 `uart_tx.tx/core.tx_out`，两者在 7145478 ps 下降，`core.tx_out_q/core.tx` 在 **7181192 ps** 再延后一拍下降，override、两种 loopback 均为 0。tx_q 是此次传播的观测点，未发现故障起点。

限制：局部查询覆盖为 complete，7 个上升沿、无截断；`clock_edges_complete=false`，不作全历史结论。静态结果为摘要、语义扫描未运行，层次解析有缺口；日志零错误也不代表全局干净，因此未触发失败场景 handshake sweep。窗口前的数据装载未追溯；**编译路径已核验，但历史 source/wave 内容身份仍未独立证明。** 使用 18 次 MCP、2 次源码读取。