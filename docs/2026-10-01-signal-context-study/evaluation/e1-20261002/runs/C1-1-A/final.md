**结论：7145478 ps 的 `tx_q: 1→0` 符合正常同步更新。** 它与 `tick_baud_q` 下降同一时间发生，但触发源是 `clk_i` 上升沿，使用的是边沿前已经准备好的 `tx_d=0`。

下表为时钟上升沿 **before → after**，after 取边沿后 1 ps：

| 时间（ps） | tick_baud_q | tx_d | tx_q | sreg_q | bit_cnt_q |
|---:|---|---|---|---|---|
| 7109764 | 0→1 | 1→0 | 1→1 | 0x7da→0x7da | 10→10 |
| 7145478 | 1→0 | 0→0 | 1→0 | 0x7da→0x7ed | 10→9 |

7145478 ps 边沿前，`rst_ni=1、tx_enable=1、wr=0、tick_baud_q=1、bit_cnt_q=10`，满足移位条件；`sreg_q[0]=0`，因此 `tx_d=0、sreg_d=0x7ed、bit_cnt_d=9`。波形与已核验编译路径的 [uart_tx.sv](/cache/Projects/opentitan/out/dvsim/master/uart-sim-vcs/default/fusesoc-work/src/lowrisc_ip_uart_0.1/rtl/uart_tx.sv:45) 一致。**边沿后 `sreg_q[0]=1` 是移位后的下一位，不能用来判断本次应发送 1。**

双方已查边界及竞争解释：

- **上游起点：**7074050 ps，NCO 的 `0x0f18a + 0x2572 = 0x116fc`，产生 `tick_baud_x16`；7109764 ps 分频计数 `15→0`，产生 `tick_baud_q`，进而准备 `tx_d`。
- **下游传播：**7145478 ps，`uart_tx.tx`、父层 `tx_out` 随 `tx_q` 变低；7181192 ps，`tx_out_q`、父层 `tx` 再变低。覆盖及两种 loopback 控制均为 0。
- **竞争解释一：错误边沿或局部时钟异常。**本窗时钟周期恒为 35714 ps，更新落在上升沿，未支持该解释。
- **竞争解释二：数据错误或复位、禁用、写入覆盖。**直接数据计算吻合，窗口内复位释放、发送使能和 `wr=0` 均稳定，未支持该解释。所见现象是正常传播，未发现局部故障起点。

限制：结论仅覆盖 **7074050–7288500 ps**。7 个上升沿的局部覆盖完整、数据未截断；全局 `clock_edges_complete=false` 不代表本窗时钟异常。结构扫描仅词法覆盖完整，语义扫描未运行且告警展示不全，不能据此宣称整体干净。日志未检出 error/fatal，因此未触发失败 handshake sweep。**历史 source/wave 身份仍未独立证明**，更早装载数据及完整帧正确性未核验。本次使用 17 次 MCP、3 次源码读取。