# TraceWeave 单信号上下文调研

日期：2026-10-01。基线提交：`58075728654fc850a56b8909ac6d1bb3ace8a46e`。

2026-10-02 方案澄清：局部采样继续保留读取预算，明确窗口内 clock-X 的停止边界；接口优先增强 `explain_signal_driver` 的依赖返回，再由 AI 调用周期采样。组合工具降为可选评测项，详见 [更新后的计划](PLAN.md)。以下案例与证据仍是 2026-10-01 的实测记录。

2026-10-02 用户确认验收原则：确定性的采样与 Source Graph 修复按功能回归和真实案例验收；NPI 返回增强、`explain_signal_driver` 依赖包可以先实现，但必须在实现后做 A/B，至少当前案例显示收益才算收敛。若实现组合工具，也须独立证明增量收益。**实现完成不等于收益已收敛。**

交接入口：[实施计划](PLAN.md) · [A/B 验收规则](AB_ACCEPTANCE.md) · [新 session 提示词与模型建议](SESSION_PROMPT.md) · [当前状态](IMPLEMENTATION_STATUS.md)。所有产品实现和 A/B 均尚未开始。

**建议补这一层，但先解决已经复现的采样与路径绑定问题，再复用现有动态依赖模块提供有界的单信号上下文。** 本次实操表明，AI 能组织“发现异常 → 读源码 → 找条件 → 查前后周期 → 检查另一侧”的流程；目前更明确的困难是部分确定性取证步骤失败，或返回结果还不能直接作为下一次波形查询的输入。

这次交付包括调研记录、五个案例、原始工具回执和[分阶段改动计划](PLAN.md)。生产代码与被分析项目均未修改，没有重新运行仿真。

## 1. 调研范围和证据边界

| 项目 / 保留运行 | 波形 | 时长 | 本次用途 |
| --- | --- | --- | --- |
| `~/Projects/opentitan` / UART smoke | FSDB，2,322 个信号，283,774 B | 428.85643 μs | 正常寄存器更新；真实 Verdi NPI 驱动查询 |
| `traceweave-xheep-X1` | FST，28,971 个声明，5,666,896 B | 1.55402 ms | DMA 数据错误、正常 FSM 完成 |
| `traceweave-xheep-X2` | FST，28,971 个声明，155,814,637 B | 50.00002 ms | DMA RUNNING 卡死 |
| `traceweave-xheep-X3` | FST，28,971 个声明，31,667,009 B | 9.11902 ms | 正常传输对照、现有 A/B 回溯能力 |

OpenTitan 产物位于 `/cache/Projects/opentitan/out/dvsim/master/uart-sim-vcs/`；X1/X2/X3 产物位于 `/cache/Projects/traceweave-verilator-acceptance/`。完整路径、源文件和 SHA-256 见 [artifact-manifest.json](evidence/artifact-manifest.json)。

方法：

- 每个运行先发现路径，再并行构建 hierarchy 和执行 structural scan；解析日志后，对 X1/X2 失败运行执行 handshake sweep。源文件先经过 compile-set lookup，再读取相关片段。
- 先用日志/波形建立候选，再检查驱动及其输入。未读取 `ground-truth.patch` / `restoration.patch`。沿信号读到的 RTL 含有 mutant 注释，因此这是工程能力调研，不是严格盲测；结论均另有波形支持。
- 这是一个 agent 对四份保留运行构造的五个调试场景，不是五个独立缺陷，也不是跨模型成功率或新旧工具 A/B 实验。
- XHEEP 的保留 `hdl-compile.log` 是 `verilator --xml-only` 命令记录，包含 file list、宏和 trace 设置；其时间晚于仿真。OpenTitan 运行也早于本次调研。当前 compile set 和内容指纹不能独立证明历史波形与当前源码完全同版；回溯回执也明确标注了这一点。
- FSDB/FST 均确认 1 ps 精度。文中 `edge - 1 ps` 是这几份产物的取样替代办法；一般实现必须用严格的 before-edge 事件语义，不能推广成固定减 1 ps。
- 所有“正常”“排除”仅针对列出的信号、时间和假设。

## 2. 五个案例

机器可读定义见 [cases.json](cases.json)。以下缩写仅为阅读方便；完整信号路径和实际调用参数都在该文件及回执中：

- `U` = `tb.dut.uart_core.uart_tx`
- `D` = `TOP.testharness.x_heep_system_i.core_v_mini_mcu_i.ao_peripheral_subsystem_i.dma_subsystem_i.dma_i_gen[0].dma_i`
- `B` = `TOP.testharness.x_heep_system_i.core_v_mini_mcu_i.system_bus_i`

### C1：OpenTitan UART，控制脉冲已清零，输出为什么仍更新？

问题：`U.tx_q` 在 7,145,478 ps 下降，但该边沿之后 `tick_baud_q=0`。候选是异常移位，或寄存器正确使用了边沿之前的条件。

| 实际采样 | tx_q | tx_d | tick_baud_q | bit_cnt_q |
| --- | --- | --- | --- | --- |
| 上一个边沿 7,109,764 ps 后 | 1 | 0 | 1 | 10 |
| 目标边沿前，7,145,477 ps | 1 | 0 | 1 | 10 |
| 目标边沿 7,145,478 ps 后 | 0 | 0 | 0 | 9 |
| 下一个边沿 7,181,192 ps 后 | 0 | 0 | 0 | 9 |

`wr=0`、`tx_enable=1`、`rst_ni=1`；源文件的组合条件是 `tick_baud_q && bit_cnt_q != 0`，寄存器在下一时钟沿更新。所查更新符合源码。检查了输出寄存器与输入数据/控制条件，不应从边沿后的 `tick=0` 推断非法移位。

现有工具表现：

- NPI 正确定位 `tx_q` 的 always_ff（45 行）和 `tx_d` 的 always_comb（返回 58 行）。
- 两次直接查询的 `upstream_signals=[]`。递归查询返回了 7 个 fan-in 事实，但其中路径是 NPI 单元/过程名称，例如 `...uart_tx.uart_tx`，不能直接当成可采样信号。
- AI 通过已验证的源文件补选了 `tx_d/sreg_q/bit_cnt_q/tick_baud_q/wr/tx_enable/rst_ni`。
- after-cycle 表成功；before-cycle 表因时钟最初为 X 而拒绝查询，尽管目标附近的时钟已稳定。读取局部时钟和边沿前定点值可以继续。

证据 ID：`ot_txq_driver`、`ot_txd_driver`、`ot_txd_recursive`、`ot_tx_cycles_after`、`ot_tx_cycles_before`、`ot_clock_initial`、`ot_clock_local`、`ot_tx_before_point`。

**值得下沉：可采样的类型化依赖集合，以及明确区分 before/after 的局部周期窗口。** 本次尚未实测 UART 的内部 NPI dynamic-step 能覆盖哪些依赖，不把公共接口空列表解释为底层完全没有能力。

### C2：X1 DMA 比较失败，数据在哪个边界变坏？

日志 56 行给出 element 0：expected `0x10`，actual `0x11`；58/60/62 行还有 4/8/12 号元素错误。比较行没有时间戳，软件退出发生在 1,553,035,000 ps，不能把退出时刻当作错误写入时刻。

通过请求和状态跳变找到实际传输：`data_out_req` 在 944,145,000 ps 拉高，第一笔写在 944,175,000 ps 的边沿具备 req/gnt。在该边沿前 1 ps：

| 已检查边界 | wdata |
| --- | --- |
| DMA channel 输出 | `0x10` |
| DMA subsystem / AO / core 输出 | `0x10` |
| `B.dma_write_req_i[0].wdata` | `0x10` |
| `B.int_master_req[4].wdata` | `0x11` |

同一时刻 `req=1`、`gnt=1`、`we=1`、`be=1`、地址 `0x10440`。已验证编译文件 `system_bus.sv:132–134` 的对应赋值包含 `wdata ^ 32'h1`。

因此，在本次检查范围内，数据变坏的位置已经定位到 system-bus 请求映射；上游 DMA 发送坏数据的假设被这个传输的波形排除。未继续重建内存写入到 CPU 读回的完整链路，也没有证明全设计只有这一处缺陷。

驱动查询暴露两层困难：

1. 完整波形路径以 `TOP.testharness` 开头，Source Graph 报 `source_graph_target_top_unresolved`。
2. 显式使用设计路径并给出 `top_hint=testharness` 后，DMA generated scope 和 `int_master_req[4].wdata` 仍遇到 `instance_not_in_projected_scope`。回执还包含数组连接、赋值目标等 semantic gaps，不能假定只删前缀就解决全部问题。

证据 ID：`x1_log_full`、`x1_req_history`、`x1_grant_history`、`x1_write_boundary`、`x1_system_boundary`、`x1_mapping_fact`、`x1_output_driver`、`x1_output_driver_design_path`、`x1_mapping_driver`。

**值得下沉：有证据的 waveform/design 路径绑定，及 generate、packed array/member 的有界语义解析。** 这些确定性步骤不宜让 AI 每次手工猜路径。

### C3：X1 FSM 已退出 RUNNING，done 却为 0

候选是假跳转，或完成条件在边沿前有效、更新后消失。主 FSM 使用 `clk_cg`，状态枚举为 READY=0、STARTING=1、RUNNING=2。

| 采样时刻 | state_q | state_d | dma_done | circular_mode |
| --- | --- | --- | --- | --- |
| 944,514,999 ps | RUNNING | READY | 1 | 0 |
| 944,515,001 ps | READY | READY | 0 | 0 |

结合 `RUNNING && dma_done && !circular_mode → READY`，所查转移正常。仅看更新后的一行会丢失真正触发跳转的条件。

同一运行还复现了独立的取证障碍：在 944,095,000 ps 附近只请求 9 个 cycle，公共 `get_signals_by_cycle` 的 before/after 都失败。局部时钟有完整边沿，定点读取也正常。

只读代码诊断发现：

- 公共入口先调用 `_full_clock_edges`，读取从 0 开始的整段时钟，再按请求窗口切片。
- `_read_before_transitions` 的估算解码体积上限是 64 MiB，另有 1,000,000 事件上限。
- 实际在 130,055 行、650,270,000 ps 处截断，尚未到目标窗口；没有 clock-X 或 recording-gap 标记。这里的 64 MiB 是预算估算，不是实测 RSS。
- 已有内部 `sample_signals_on_edges` 在同一 944,095,000–944,175,000 ps 窗口，成功返回 before/after 各 9 行，均无缺口。只读诊断单次耗时约 1.08 / 1.04 秒；这不是生产接口改动后的性能数据。

证据 ID：`x1_done_before`、`x1_done_after`、`x1_transfer_before`、`x1_transfer_after`、`x1_clock_local`。内部诊断见 [diagnostic-probes.json](evidence/diagnostic-probes.json)。

**优先复用已有局部采样内核。** 保留全局 cycle 编号的原有语义，另外提供明确的局部编号模式；不能将全局前缀不完整伪装成“全局 cycle 已知”。

### C4：X2 RUNNING 卡到超时，是否应修改状态机？

日志在 50,000,020,000 ps 超时。状态进入 RUNNING 后，直到波形结束没有退出；请求一直保持为 1。

候选：状态机没有响应完成条件；总线没有提供 grant；总线提供了 grant，但返回映射丢失它。

在 944,174,999 ps：

| 信号 | 值 |
| --- | --- |
| `B.int_master_resp[4].gnt` | 1 |
| `B.dma_write_resp_o[0].gnt` | 0 |
| `D.data_out_gnt` | 0 |
| `D.data_out_req` | 1 |
| `D.dma_done` | 0 |
| `D.dma_state_q / dma_state_d` | RUNNING / RUNNING |

接近超时时再次取样仍见相同关键关系。已验证的 `system_bus.sv` 对应返回映射将 grant 字段写为常量 0。总线内部已经授予请求，“consumer 从未 grant”的候选在所查时刻被排除；DMA 看到的条件不满足，不能据“卡在状态机”直接归罪 next-state 逻辑。

现有 `verify_window` 对 944,180,000–944,300,000 ps 的 12 个时钟沿检查 `state=RUNNING && !done && req && !gnt`，返回 `holds=true`、`vacuous=false`、`unknown_cycles=0`、完整覆盖。这里检查的是给定条件，不是 OBI 协议整体正确性。

证据 ID：`x2_log`、`x2_state_history`、`x2_req_history_corrected`、`x2_grant_history`、`x2_grant_mapping`、`x2_late_state`、`x2_window_control`。

**值得下沉的是从控制条件继续取证的路径；判断哪个假设成立仍需要 AI。** 状态机内部更多 FIFO/计数细节没有全部检查。深层 write-unit 信号未在搜索结果中找到，trace 配置含 `--trace-depth 6`；缺少 dump 应返回观察边界，不能生成其周期值。

### C5：X3 正常对照，以及现有 trace_divergence

X3 日志没有运行错误，记录 software_exit=0。相同传输位置、相同边沿前取样：

- 请求映射 `0x10 → 0x10`。
- grant 映射 `1 → 1`，DMA 也看到 grant=1。
- FSM 在 944,515,000 ps 回到 READY。

因此 C2/C4 的异常边界关系没有在这个对照传输中出现。这个结论不需要把 X3 宣称为经过独立证明的 golden design。

还实测了现有 `trace_divergence`：指定双方 compile context 和显式 scope mapping，比较 X1/X3 的 `B.int_master_req[4].wdata`，窗口 944,160,000–944,180,000 ps。它在该窗口第一个比较的 clock sample（944,165,001 ps）确认 `0x11 ≠ 0x10`，随后因 `source_graph_target_top_unresolved` 停在根节点，返回 partial/frontier。

第一轮调用因切换项目时旧 hierarchy handle 已失效而 blocked；按返回 prerequisite 重建后获得上述结果。这个生命周期恢复不是 RTL 驱动缺失，也不计为模型/产品调试失败。

证据 ID：`x3_log`、`x3_mapping_fact`、`x3_state_history`、`x1_x3_trace_divergence`、`x1_rebuild_hierarchy`、`x1_x3_trace_after_prereq`。

**现有自动回溯已经具备组织部分动态证据的能力；本例需要先打通绑定。** 该差异时刻仅是指定采样策略和局部窗口内的最早差异。

## 3. 应复用哪些现有能力

| 已有实现 | 本次判断 |
| --- | --- |
| `explain_signal_driver` / hierarchy handle tools | 已能提供定位和 backend provenance；NPI 公共结果不总能直接产出 wave read list |
| `dynamic_evidence.py` | 已有有界 typed Expr、Assignment、data/control 依赖，不再建立平行表达式体系 |
| `npi_dynamic.py` / `source_graph_dynamic.py` | 已有单步 driver、分支、clock 与 sequential boundary；部分 NPI 异步控制值明确未建模 |
| `dynamic_observe.py` | 已有 guard 取值、active branch、寄存器严格 before-edge 观察、hold 和缺口语义 |
| `dynamic_binding.py` / `packed_layout.py` | 已有精确 declaration/bit binding 和 packed field 解析；应扩展当前真实缺口 |
| `trace_divergence` / `trace_x_source` | 已有特定入口下的回溯；不必另建一个大而全的自动 debug engine |
| `sample_signals_on_edges` / `verify_window` | 局部窗口采样与条件检查已经可用，应作为组合底座 |

代码依据链接：[采样](../../src/cycle_query.py)、[动态契约](../../src/dynamic_evidence.py)、[NPI 单步](../../src/npi_dynamic.py)、[Source Graph 单步](../../src/source_graph_dynamic.py)、[动态观察](../../src/dynamic_observe.py)、[绑定](../../src/dynamic_binding.py)、[回溯路由](../../src/divergence_routing.py)。

“驱动源附近的信号”应优先定义为**赋值右值、分支条件、时钟/复位、反馈值和端口绑定**。源码相邻只是查阅线索，不能自动视为逻辑依赖。FSM 是这种机制的一个应用，不需要先做一个专用 FSM debug 引擎。

## 4. 优先级与暂不扩大的范围

| 优先级 | 改动方向 | 直接证据 |
| --- | --- | --- |
| P0 | 局部周期模式、before/after 相位、可区分的截断/时钟缺口原因 | C1、C3 |
| P0 | 波形与设计路径的可验证绑定；generate / packed array/member 定向解析 | C2、C5 |
| P1 | 复用 dynamic-step，按需返回依赖、源码位置与可采样选位，再由 AI 查询局部值；须做实现后 A/B | C1、C4 |
| 后续评估 | 更好的失败时间定位、OBI 语义适配、跨项目 context 生命周期便利性 | 当前有观察，但不足以与前三项合并为一个大改动 |

暂不做：全设计 fan-in 展开、把同 module 的所有信号扫入返回值、自动认定唯一 root cause、从未 dump 信号推算“实际值”、把 RTL 文本交给另一套不带类型/位宽证据的表达式求值器。

## 5. 基线、复现和局限

[evidence/mcp-calls.jsonl](evidence/mcp-calls.jsonl) 保存 100 次研究调用的参数和解析后的原始 JSON 结果；[call-index.json](evidence/call-index.json) 提供耗时、结果体积和错误索引。结果按完成记录顺序编号，**不能把并行调用耗时相加当作端到端调试时长**。elapsed 包括客户端等待和服务端排队；缓存没有统一清冷；未测 token、完整 RSS、人工选择时间或跨模型成功率。

其中两次是操作者无效请求：UART 猜测不存在的 `state_q`，以及 X2 将 `data_out_req` 误写成 `data_out_req_o`。保留回执并明确排除出产品缺陷统计。工具列表读取、源码阅读和会话初始 snapshot 不计入这 100 次。

扫描的限制必须保留：

- 四个运行的 structural scan 均完成 lexical 扫描；semantic 默认未运行，不代表语义检查通过。X1 在跨运行比较前另有一次 prerequisite 重建/扫描。
- X1 全波形 sweep 超时；缩到 0–10 μs 后再按建议提高 interface cap，最终检查 59 个、跳过 88 个，coverage=degraded。该窗口甚至早于 DMA 传输。
- X2 末尾 10 μs sweep 也是 59 个检查、88 个跳过、degraded。OBI req/ack 不应自动套用 valid/ready 的 hold 假设。
- 两个局部 sweep 都没有 flag，不能推出协议干净。X2 的通用 recommend 也超时；根因取证靠后续目标查询完成。

无需重新仿真即可复现内部采样对照：

```bash
.venv/bin/python docs/2026-10-01-signal-context-study/replay_sampling_probe.py
# 可选：同时复现全历史时钟读取被预算截断
.venv/bin/python docs/2026-10-01-signal-context-study/replay_sampling_probe.py --include-full-prefix
```

MCP 重放应使用 `cases.json` 中对应运行的路径，并从新鲜 discovery/hierarchy 开始；handle 和 cursor 只在服务进程中有效。跨运行比较时，在最后一次 discovery 后建立双方 hierarchy。原始调用中的历史 handle 不能直接假设仍可用。

本调研证明了**哪些机械步骤值得下沉以及为何需要**。它尚未证明新接口能提高多少模型正确率或减少多少调用；这些是[实施计划](PLAN.md)中的验收实验。

保存说明：本目录沿用仓库现有的 `docs/*` Git 忽略规则，文件已在本地落盘；未修改 `.gitignore`、暂存或提交文件。
