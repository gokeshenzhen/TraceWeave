# E1 实际模型 A/B：增强待优化

本轮已实际执行五案例各三对独立模型任务。**NPI 返回增强和通用 driver 依赖包均没有达到预登记收益门槛，状态为“待优化”，额外依赖保持默认关闭。** P0 的局部周期、绑定和选位修复由独立功能证据验收，不把双方共享的修复收益归给新增返回。

核心 30 个 run 中，两组各完整交付 1/15 个任务，各有 14 次 300 秒预算超时。实际增强确实被调用，但采用它的任务均未完成。这个结果不证明增强在所有预算下无用；它足以否定本轮“已收敛”或“已证实省 token”的声明。按 [冻结规则](../../AB_ACCEPTANCE.md)，保留负结果并默认关闭，不继续挑选案例或调整门槛。

## 实验与证据

- 共同 P0：`651a91c`；增强产品提交：`010e6df`。双方使用同一产品 checkout；A 隐藏并拒绝 `include_dependencies`，B 暴露可选参数，没有只给 B 的采用提示。
- 模型：实际 `codex exec`，`gpt-6-astra`、`max`，CLI 0.160.0；每次全新模型上下文、临时工作目录和 MCP 进程。顺序 AB / BA / AB，Source Graph disk/session cache 关闭，OS cache 未控制。
- 每次 300 秒、40 MCP 调用、8 次 shell 源码读取；input 1,000,000 / output 20,000 token 为完成后的 usage 审计上限。中途超时缺失 usage 时，不能声称 token 预算已验证。
- [manifest](manifest.json)、[冻结评分标准](rubric.json)、[原始运行索引](runs.jsonl)、[逐 run 评分](grading.jsonl)、[提取指标](metrics.jsonl)、[汇总](summary.json)、[加载/schema 审计](audit.json)、[流程与源码读取审计](workflow-audit.json)。各 `runs/<id>/` 保留原始 transcript、完整工具返回、提示、工具 schema、加载模块指纹和最后消息。
- `final.md` 在超时运行中通常只是最后一条进度消息。只有实际模型完成事件、完整证据与交付结论才计作任务成功；不能把中途正确波形或源码片段计作完整交付。
- 初批独立 MCP 未继承 EDA 环境，真实 NPI 加载失败，原记录与 [报告](../e1-20261002/REPORT.md) 保留。临时 CLI 显式转发既有 EDA 环境后，经 [真实 NPI 模型预检](../runner-npi-preflight/REPORT.md) 再冻结本批。产品、案例提示、预算和判据不变，没有修改客户端配置。
- 另有 S1 初始 B 遇到外部模型容量错误，原始配对不混入有效比较；[失败原因](S1_INFRASTRUCTURE.md) 与 [完整同条件重试](../e1-s1-retry-20261002/REPORT.md) 单独保存。S1 重试两组均正确完成源码定位，没有新增依赖或波形取值。S1 不混入核心五案例的效率汇总。

## 逐案例结果

下表成功指行为任务完整交付；评分的四个子项仅记录已观察证据，不能累计成任务成功率。`strict_accepted` 是冻结案例 rubric 的标记，另有下文的公共流程偏差，不能解读成全实验流程合规。

| 案例 | A 完整成功 | B 完整成功 | A / B 超时 | B 的依赖采用 | 结果边界 |
| --- | ---: | ---: | ---: | --- | --- |
| C1 UART | 1/3 | 1/3 | 2 / 2 | 第二、三轮实际 NPI，共 3 次 | 首轮两组正确解释正常同步更新；B 首轮未调用 NPI/依赖，故不能算 NPI 收益 |
| C2 数据错误 | 0/3 | 0/3 | 3 / 3 | 第三轮 Source Graph，1 次 | 有运行取得传输时刻 16→17 及下游传播，但未完整交付结论与限制 |
| C3 完成相位 | 0/3 | 0/3 | 3 / 3 | 0 | 多次正确区分 done 的 pre/post 相位，随后继续扩大取证而超时 |
| C4 等待超时 | 0/3 | 0/3 | 3 / 3 | 第二轮请求 1 次，回退 Static | 没有补齐系统总线响应映射两侧波形；仅局部 req=1/grant=0 或置零源码不足以定根因 |
| C5 正常对照 | 0/3 | 0/3 | 3 / 3 | 0 | 多次得到正常有效数据/grant/状态转移证据，仍未完成最终范围与限制说明 |

[C1-1-A](runs/C1-1-A/final.md) 满足 C1 的全部四个 rubric 项；[C1-1-B](runs/C1-1-B/final.md) 的行为解释正确，使用编译集源码和双相位波形，但没有执行冻结 rubric 所要求的实际 NPI 查询。因此行为成功分别为 1/3、1/3，C1 严格后端条件为 A 1/3、B 0/3。不能把 B 的正确行为解释说成错误，也不能把另一轮的 NPI 使用移植成这一轮的收益证据。

核心未观察到新增的正常 FSM、UART 更新或 C5 数据映射误判；大多数任务没有完成最终输出，这不构成全部正常反例通过的保证。所有超时原样保留，没有因为中途答案看起来接近 oracle 而改为成功。

## 新增返回实际做了什么

15 个核心 B 运行中，4 个请求增强，共 5 个依赖包、13 条候选、3,782 字节序列化 JSON。字节计算为 `len(json.dumps(dependency_context).encode())`，不是模型 token。

| Run / MCP 调用序号 | 实际后端 | 候选 / 字节 | 观察到的后续使用 |
| --- | --- | ---: | --- |
| [C1-2-B #10、#17](runs/C1-2-B/calls.jsonl) | verdi_npi | 9 / 1,915 | 第一包 clock/set/data 随后被采样；第二包六项在返回前已采样，没有后续取值 |
| [C1-3-B #19](runs/C1-3-B/calls.jsonl) | verdi_npi | 3 / 695 | 三项此前均已采样，之后只有 clock storage 又被查询 |
| [C2-3-B #11](runs/C2-3-B/calls.jsonl) | source_graph | 1 / 895 | 精确 wdata 字段及 literal 1；随后查询该输入与输出两侧，保留 `driver_set_incomplete` |
| [C4-2-B #14](runs/C4-2-B/calls.jsonl) | static | 0 / 277 | 递归 generated DMA 响应路径的 Source Graph 扩展停止，明确 `dynamic_evidence_unavailable` |

C1 的 native set pin 和 `active_value=0` 是类型/极性事实，不能由 `rst_ni` 名字推断 RTL 异步赋值。包继续保留 `async_control_value_unmodeled`、`temporal_context_unavailable` 和未建模运算缺口。C4 的 `recursive=true, max_depth=3` 查询回执为 `source_graph_frontier_expansion_stalled`；这是一条已观察的路由限制，不能伪装为有效 Source Graph 依赖或扩大 P0 对所有递归 SV 路径的支持声明。

计量脚本的“使用”仅表示前后波形调用触及同一 storage 路径，保留实例/数组索引、忽略末尾 leaf 选位。13 条候选中 5 条有后续 storage 请求，其中 4 条此前未请求；这不是精确 bit/time 的消费证明，更不是因果或推理收益评分。没有把其余候选自动判成无关信号。

## 成本、错误与资源

| 核心 15 run / arm | A | B |
| --- | ---: | ---: |
| 完成 / 超时 | 1 / 14 | 1 / 14 |
| 整任务 usage 缺失 | 14 | 14 |
| 完成 MCP 调用 / 已发起调用 | 290 / 291 | 290 / 290 |
| 源码读取 shell 命令 | 57 | 56 |
| 名称搜索调用 | 45 | 48 |
| 顶层工具错误 | 3 | 3 |
| 工具响应序列化字节 | 7,860,112 | 9,050,179 |
| 已完成工具调用耗时之和 / 秒 | 429.459 | 465.206 |
| run wall-time 中位数 / 秒 | 300.016 | 300.016 |

工具字节/耗时覆盖不同的模型选择和取证范围，不是等工作量 microbenchmark；不能把响应差额全归因于额外 3,782 字节。wall-time 大量被 300 秒截断，也不能从相近中位数断言速度相同。源码读取最多 A 7、B 8 次，MCP 最多 25 次；没有触及 40-call 上限。唯一未完成 MCP 出现在 C4-3-A，保留为 interrupted call，没有误记成已完成取证。

六次顶层工具错误均为 C4 各轮的 `recommend_failure_debug_next_steps` → `fst_timeout: total FST request deadline exceeded`。这是实际工具 deadline 结果，不是重跑理由；原有工具错误、局部/全局 coverage 缺口均保留。完整五案例 token 不可算；没有用输出字节估算缺失 usage。

唯一双方均完整交付的核心配对：

| C1 第一轮 | input | cached input（已包含） | output | reasoning output（已包含） | input + output | 秒 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| A | 493,513 | 427,776 | 6,071 | 2,593 | 499,584 | 295.063 |
| B | 503,515 | 445,184 | 5,928 | 2,392 | 509,443 | 280.589 |

B 总 token 高 1.97%，且这一 B 没有使用依赖包，不能支持新返回节省 token。没有 2/3 成功配对或 ≥10% 的预登记效率收益，也没有任何案例达到质量路径的 2/3 配对优势。

整任务 RSS 没有独立统一采样；原始回执保留可获得的 native/worker RSS、事件量及操作阶段计量，不据此宣称增强降低内存。P0 的资源 before/after 在 [采样报告](../p0-sampling/REPORT.md) 单独记录，包含 C1 首次局部查询更慢的反例。

## 输入与流程审计及适用范围

[审计](audit.json) 确认每个 run 的提示匹配、MCP PID 不重复、已加载产品模块匹配冻结指纹。移除唯一目标参数后，所有工具 schema 相同；完整工具列表序列化 A 98,877 字节、B 99,252 字节，均原样保存。结束时重新核对全部产品/runner 及 12 份 compile/log/wave artifact，哈希与冻结 manifest 一致。过程中提交的是离线证据和审计辅助，产品与被测 runner 未改。

全部 32 个已结束运行都实际调用了 hierarchy 和 structural scan，compile_log 相同；但事件显示二者串行，没有执行提示要求的并行工作流。这个共同执行偏差保留在 `workflow-audit.json`，不能宣称完整遵守仓库流程或把全部耗时差异归因于参数。已观察的 shell 操作是源码/技能只读命令；每个源码绝对路径都能追溯到此前的 compile-set lookup。没有研究报告、oracle、Git 历史、网络或其他 agent 的读取/调用事件。

此外，源码原有 mutant 注释对双方相同可见，本实验不是严格盲测；历史编译与当前 source/wave 完全同版仍未独立证明。三对是工程起始规模，不是统计显著性保证。高超时率、较低增强采用率和公共流程偏差限制了本轮解释：结论是当前配置下没有取得可验收收益，而非对所有模型/预算作出否定。

## 验收处置与复现

- P0-SAMPLING / P0-BINDING：保留独立功能验收结论及真实案例范围，不将其计入 E1 增益。
- NPI 返回增强：已实现、功能通过、实际模型采用，但未证明可重复质量/效率收益，**待优化**。
- 通用 `explain_signal_driver` 依赖包：同样 **待优化**，继续 `include_dependencies=false` 默认；不自动读值或扩成全周期表。
- E2 未实现，也没有使用 E1 的结果代替其将来的增量验收。

若另开优化实验，应先明确能在预算内结束取证并交付结论的流程，处理已观察到的路径限制，再重新冻结同条件对照；不能在本轮结果上追改预算、挑选成功运行或宣称字段本身已带来收益。本轮在此保留负结果，不无限追加运行。

本批执行入口为 `evaluation/run_ab.py run evaluation/e1-20261002-env`（路径以本研究目录为基准），每个 run 的 `command.json` 保存实际完整命令。离线复核从仓库根目录运行：

```bash
.venv/bin/python docs/2026-10-01-signal-context-study/evaluation/collect_ab.py docs/2026-10-01-signal-context-study/evaluation/e1-20261002-env
.venv/bin/python docs/2026-10-01-signal-context-study/evaluation/audit_completed_ab.py docs/2026-10-01-signal-context-study/evaluation/e1-20261002-env
```

前者提取指标，后者校验冻结顺序、评分引用、源码读取时序与最终哈希；两者不重新调用模型、不改变原始结果，也不替代人工正确性评分。

交付前的 [完整性检查](delivery-checks.json) 覆盖本批与 S1 重试共 34 个实际模型运行：34 个不同 MCP PID、共同 schema 一致、JSON 全部可解析、已加载产品匹配、无残留评测 MCP 进程。`CLAUDE.md` 的真实目标 `AGENTS.md` 为 39,786 字符且未修改。
