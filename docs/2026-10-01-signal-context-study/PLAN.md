# 改动计划：有界单信号调试上下文

状态：本计划已按阶段执行。P0 当前案例已验收，P1 实际 A/B 未见可验收收益、待优化；详见 [最终状态](IMPLEMENTATION_STATUS.md)。以下保留原设计与验收边界。创建：2026-10-01；用户验收要求确认：2026-10-02。
下一轮从 [NEXT_ROUND.md](NEXT_ROUND.md) 执行兼容性与增量收益验证；以下原实施步骤不表示需要重做 P0。
依据：[五个案例与结论](README.md)、[机器可读案例](cases.json)、[原始工具结果](evidence/mcp-calls.jsonl)。
基线：`58075728654fc850a56b8909ac6d1bb3ace8a46e`。

## 用户确认的验收原则与交接入口

2026-10-02 用户明确：有待证明效率/组织收益的方案可以先实现，但实现后必须做 A/B，至少在当前案例上看到收益，才能认定收敛；`explain_signal_driver` 增强也属于这一类。确定性修复无需另做模型 A/B，但仍须正常功能回归、真实案例复测及必要的资源验证。

| 改动 | 类别 | 完成条件 |
| --- | --- | --- |
| P0-SAMPLING：局部周期读取、64 MiB 预算、相位与 clock-X/缺口边界 | 确定性修复 | 本节功能验收 + C1/C3 真实复测；无需模型 A/B |
| P0-BINDING：wave/design 绑定及 Source Graph generate/数组/成员解析 | 确定性修复 | 类型/路径/驱动事实回归 + C2/C4/C5 真实复测；无需模型 A/B |
| P1-CONTEXT / E1：NPI 对外返回可用性、explain_signal_driver 结构化依赖、采样候选、输出组织 | 收益待验证增强 | 先实现并通过事实正确性检查，再通过 E1 A/B；仅代码或测试完成不算收敛 |
| E2：若实现 inspect_signal_context 等组合工具 | 收益待验证增强 | 可先实现实验原型；必须相对已收敛 E1 再做增量 A/B，收益不能由 E1 代替 |

P0-BINDING 虽然会改善 driver 查询的解析结果，验收的是客观解析正确性；P1/E1 改的是 AI 如何消费 driver 输出，验收还须包含实际任务收益。其他新增的排序、预取、自动选信号或输出压缩若以“更高效”为理由加入，也按收益待验证项处理，不借 P0 名义跳过评测。

- A/B 分组、案例、重复次数、指标和收敛条件：[AB_ACCEPTANCE.md](AB_ACCEPTANCE.md)。
- 新 session 提示词、模型建议和执行交接：[SESSION_PROMPT.md](SESSION_PROMPT.md)。
- 状态记录：[IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md)。功能与实际模型评测证据已保存；历史 100 次调研调用仍不算 A/B 结果。

“无需模型 A/B”不等于无需测试；确定性修复若声称提速，仍遵守仓库的 before/after workload、资源及兼容性验证要求。不能因 E1/E2 没有测出收益，否定或回滚已经独立通过验收的 P0 修复。

## 目标与分工

输入一个已确认的信号、时间窗口和 compile context，可靠地得到：

1. 对应实例、赋值/端口绑定和源位置。
2. 有语义证据的直接 data/control 依赖，以及 clock/reset/反馈边界。
3. 这些信号在相关时钟沿前后的值、实际采样时刻和缺失原因。
4. 可继续调用的下一个 driver/观察目标。

TraceWeave 负责可重复执行的证据收集；AI 负责提出竞争假设、选择下一条链、结合设计预期判断异常。首版不输出“唯一根因”或“模块无错”。

实施顺序：**P0-SAMPLING → P0-BINDING → P1-CONTEXT 的 driver 依赖返回 → A/B 验收**。首选保留两步工作流：`explain_signal_driver` 返回依赖及可采样选位，AI 选择时间/相位后交给 `get_signals_by_cycle`。新增组合工具只作为后续实验，不是前三项的交付前提。

## P0-SAMPLING：公开可用的局部 cycle 模式

### 问题

- C3：9-cycle 请求先扫全历史时钟，估算解码预算在 650.27 μs 耗尽，目标位于 944 μs。
- C1：全局初始 clock X 使晚些时候稳定区域的 before 请求也失败。
- 当前错误把预算截断和 clock 不确定合在一起，建议“reduce num_cycles”对这些案例无效。

### 设计

复用 `src/cycle_query.py::sample_signals_on_edges` 及其边沿提取/采样内核、event pages、现有 selection/batch reader。局部边沿发现增加有界分页/扩窗控制，不建立第二套时钟或时间换算语义。

建议扩展现有 `get_signals_by_cycle`，加入显式局部索引模式。以下仅是待评审的接口草案：

```json
{
  "wave_path": "...",
  "clock_path": "...clk_cg",
  "signal_paths": ["...dma_state_q", "...dma_state_d", "...dma_done"],
  "start_time_ps": 944095000,
  "num_cycles": 9,
  "cycle_index_origin": "window",
  "sample_phase": "before"
}
```

具体读取顺序：

1. 验证时钟声明和波形边界，在 `start_time_ps` 处取得准确的前驱值及缺口状态。该动作可能访问较早的索引/压缩块，但不应向上层展开整个历史事件列表。
2. 从起点开始，在有界时间页中按实际跳变收集指定极性的边沿，收齐 N 个即停止；已给 `end_time_ps` 时绝不越过它。没有给终点时，波形结束、累计事件/字节预算和 deadline 仍是硬停止条件。扩窗不重置预算，不用估算周期伪造边沿。
3. 复用同一边沿向量批量读取所选信号，保留每个信号的前驱/初值和观察缺口。before 取严格边沿前值，after 使用明确 offset；还需读取 offset 所需的尾部范围。

**保留 64 MiB 估算解码预算。** 本项的目的是避免为 944 μs 附近 9 行采样物化从 0 开始的十多万条无关时钟事件。若目标窗口本身仍超过预算，也返回预算不足和已确认的部分数据；不通过抬高上限或无限分页保证“总能成功”。原生解压/I/O 成本依赖格式实现，不能仅凭局部 API 就承诺成本严格为 O(N)。

兼容性规则：

- 现有默认与 `start_cycle` 的全局编号语义保持原样；不默默把已知全局 cycle 改成局部 index。
- 新模式支持起始时间 + 有限 `num_cycles`，以及起止时间 + 输出 cap；`cycle=0` 表示此次局部序列第一个已确认边沿，并返回 `cycle_index_origin`。全局编号只有能够证明时才另行提供。旧默认仍是全局模式，调用者/工作流必须显式选择新局部模式才能使用这条读取路径。
- 请求有限窗口时不以 `0..EOF` 为必经步骤。边界可使用准确 predecessor / initial anchor；局部起点或窗口内的未知 clock、dump gap、同 timestamp 顺序歧义仍必须暴露。
- `before` 严格排除同一物理边沿时刻的所有事件；内部保留 fs。after 保留当前 offset 语义，明确显示 offset，不称为任意 delta-cycle 的“最终稳定值”。
- 先支持从起始时间向后取 N 个边沿及显式窗口。以 anchor 自动找前 N/后 N 个 cycle 的便捷输入可后续添加，使用有界分页/渐进扩窗；不得由平均 period 推算 gated/irregular clock 的缺失边沿。
- 新增固定错误/coverage 原因，区分事件/字节预算、窗口外、缺失 predecessor、clock unknown、recording gap、同 timestamp 歧义。返回有效区间/已读边沿数；不要把预算失败称为时钟硬件异常。
- 多信号共用同一边沿向量。保持取消检查、FSDB 全局锁和 active native group 生命周期；连接分析在 wave lock 外执行。

`before/after` 是相位，不是搜索方向。C1 实际请求从 7,074,050 ps 向后取 5 个边沿的 before 值；clock 在 0 ps 为 X，34,635 ps 变成 0，局部查询窗口已有确定前驱和稳定跳变。它既没有请求负时间，也没有在所查第 5 个周期遇到 X。

局部模式遇到不确定时钟的规则：

| 情况 | 返回与停止规则 |
| --- | --- |
| X 在请求窗口之前，起点前驱和窗内时钟已确定 | 正常返回局部数据；不声称从仿真起点的全局编号可知 |
| 前四个边沿可确认，第五个预期边沿附近时钟变 X | 返回前四行及 `partial`/clock-X 的时刻；不得跳过 X 后补五行并称为连续九个 cycle |
| 窗口起点的 clock 前驱仍是 X/缺失 | 该连续周期请求明确不完整，不猜第一个边沿；可另起一个恢复后的稳定窗口 |
| 时钟恢复为确定值 | 新窗口从确定的基值及随后可确认的 0→1/1→0 边沿重新计局部序号；X→1 不当作已证明的干净 0→1 边沿 |
| 数据信号为 X，但 clock 正常 | 保留行与 X 值；clock 周期序列本身不因此失效 |
| 未来“向前 N 周期”请求到达记录起点 | 返回历史不足，不发起负时间查询或补造数据 |

以上固定标签是语义草案，实际字段名与现有 coverage schema 对齐。首版连续序列在第一个缺口停下；如将来支持多个稳定片段，必须带 segment/gap 信息，不能拼成一个无缺口的 cycle 序列。

涉及：[cycle_query.py](../../src/cycle_query.py)、[server.py](../../server.py)、[schemas.py](../../src/schemas.py)、[waveform_batch.py](../../src/waveform_batch.py)、现有 FSDB/FST/VCD event-page 接口。原则上不需改 FSDB native ABI。

### 验收

- C3 的局部模式 before/after 各返回 9 行，与记录的内部 helper 对照及公开点读一致；证明没有先读整个 clock 历史。
- C1 从稳定局部窗口开始可采样；从初始 X 或仍跨越未知区间开始的请求继续明确不完整。
- 全局模式旧输出编号与现有调用兼容；新模式不会宣称全局边沿完整。
- 合成最小 fixture 覆盖 late window、gated clock、初始 X 后恢复、窗内 X、dump-off、0.1 ps 边沿、不同边沿共用 ps 标签、predecessor 正好在边界、读取预算耗尽、取消和并发。
- 针对 [test_cycle_query.py](../../tests/test_cycle_query.py)、[test_fst_sampling.py](../../tests/test_fst_sampling.py)、[test_fsdb_timescale.py](../../tests/test_fsdb_timescale.py)、[test_server.py](../../tests/test_server.py)、[test_server_concurrency.py](../../tests/test_server_concurrency.py) 运行有风险覆盖的回归。
- 以相同窗口和信号测新旧入口：结果一致性、读取事件数、耗时、峰值 RSS、返回字节。冷/热分别记录；内部 helper 的约 1 秒诊断值不是性能目标或加速比。

## P0-BINDING：把可见波形信号准确映射到语义对象

### 问题

C2/C5 暴露的路径形态同时包含：

- producer wrapper：`TOP.testharness` 与 design top `testharness`。
- generate：`dma_i_gen[0].dma_i`。
- packed 类型的数组坐标及字段：`int_master_req[4].wdata[31:0]`。

它们分别可能代表 dump scope、实例、generate block、对象索引和 packed 位选。仅按点拆分或删除方括号会丢失身份。现有回执还显示 `array_connectivity_unmodeled` 等下层缺口，需要分层处理。

### 设计

1. **身份绑定。** 在当前 compile snapshot、top 集合及 waveform declaration 下，建立独立的 design selection ↔ waveform selection 绑定，保留两侧名称。wrapper 变换只在有来源证据且唯一匹配时允许；也可接受调用者提供的显式绑定并验证。真实设计可能就叫 TOP，禁止无条件 strip。
2. **精确类型与坐标。** 复用 `dynamic_binding`、`packed_layout` 和 Connectivity IR 的 selection/packed-member 信息；保留 alias/storage identity、位宽、范围方向、packed/unpacked 维度。优先支持案例中的常量索引；动态索引没有足够证据时返回明确 frontier。
3. **有界 hierarchy 补足。** 从已证明的实例祖先进入 Slang elaborated scope 解析 generate/实例数组。只扩展目标相关路径，设置实例/边数/时间/内存上限。不把 lexical hierarchy 的缺失节点当“实例不存在”，也不通过全设计枚举掩盖问题。
4. **分层定位缺口。** 分别记录 root binding、instance resolution、object/member resolution、assignment projection 的状态。先修复路由再重跑 C2；若仍存在数组/赋值语义缺口，新增最小 fixture 定向补 projector/query，不能通过删 coverage gap 获得通过。
5. **统一供现有工具复用。** driver、load/path、X trace、divergence、packed-field 及拟议 context 使用同一绑定，不各自做字符串替换。跨设计 A/B mapping 与单个设计到波形的 binding 是两类关系，不能混用。

这是共享绑定和 Source Graph 路由/语义能力的增强。首轮以实际案例中的确定 elaboration 条件、generate 下标、常量数组索引和 packed struct 字段为通过标准；“路径里能写出名字”与“能证明它的 driver”分别验收。不承诺所有动态数组、运行时索引、interface 或 opaque IP 一次全部支持。尚不能证明的对象继续返回具体 frontier。

XHEEP 案例没有可用 KDB，NPI 未执行，因此本项不以它证明 NPI 查错。NPI 的改动另归 P1：增强 TraceWeave 的对象到可采样声明的适配和依赖返回，不替换已经正确定位源码的 Verdi 查询。

可能涉及：[source_graph_adapter.py](../../src/source_graph_adapter.py)、[hierarchy_provider.py](../../src/hierarchy_provider.py)、[connectivity_query.py](../../src/connectivity_query.py)、[slang_connectivity_projector.py](../../src/slang_connectivity_projector.py)、[dynamic_binding.py](../../src/dynamic_binding.py)、[packed_layout.py](../../src/packed_layout.py)、[divergence_routing.py](../../src/divergence_routing.py)。具体拆分由最小复现决定。

### 验收

- C2 的声明能绑定到准确实例和选定位；最终驱动查询能定位请求映射，并保留 XOR 右值/位映射证据。中间版本如仍失败，必须指出更具体的下一层缺口。
- C5 原始波形路径可以进入支持的语义节点，不再止于 `target_top_unresolved`。不要求忽略其他真实 frontier 或完整遍历设计。
- C4 能定位 grant 返回字段的常量驱动；同名其他 channel 不被误匹配。
- fixture 包含真 TOP 模块、多 top 歧义、escaped identifier、同名子模块、generate index、packed struct 数组、展开字段 dump/整 aggregate dump、升降序范围、alias 和源码变化。
- 保持 `trusted NPI → bounded Source Graph → Static`、单 backend/artifact provenance、身份校验、partial positive 与 incomplete negative 的区别。不得混接不同 backend 的半条链。
- 覆盖 [test_source_graph_adapter.py](../../tests/test_source_graph_adapter.py)、[test_source_graph_public_routing.py](../../tests/test_source_graph_public_routing.py)、[test_source_graph_trace_public_routing.py](../../tests/test_source_graph_trace_public_routing.py)、[test_dynamic_binding.py](../../tests/test_dynamic_binding.py)、[test_trace_divergence.py](../../tests/test_trace_divergence.py)，并保留缓存、取消、scope expansion 和预算回归。

## P1-CONTEXT：先增强 explain_signal_driver 的结构化依赖返回

### 先做内部复用验证

当前已有 `dynamic_evidence.Expr/Assignment`、`npi_dynamic.query_step`、`source_graph_dynamic.query_step`、`dynamic_observe.observe_step`。先用 C1/C4 的直接 driver 验证这些模块可返回什么、缺什么，再决定新增公共字段/工具的最小范围。

不能因 `explain_signal_driver.upstream_signals=[]` 就新造一个依赖分析器。也不通过把同一个波形伪装成 A/B 双方来调用 `trace_divergence`；应抽取现有路由/观察组件的公共能力。

### 首版证据契约与两步调用

先为 `explain_signal_driver` 增加按需依赖查询（例如 `include_dependencies=true`，名称待 schema 评审），默认关闭：不执行额外依赖展开，也不添加完整依赖包。新增包是结构化取证清单，不在 driver 查询中自动读取一整张周期值表。增加工具参数说明本身仍有工具描述成本，不能声称整项增强完全没有 token 开销。

- 每个条目保留 source selection、角色、可用的 wave selection、绑定状态和来源。角色至少区分 data、control、clock、reset/set、feedback/hold；常量无需伪造成波形信号。
- NPI 侧优先复用真实 net/pin 及 `npi_dynamic` 的类型化事实，把 NPI 单元/过程对象与波形声明明确区分。不能靠解析 `Always...Reg...` 的显示名称拼接 RTL 路径。未建模的角色、未 dump 信号及位映射歧义继续标缺口。
- 包中可给出 `sampling_candidates` 或预填 `next_actions`：已验证的 `clock_path`、`edge` 和可采样 `signal_paths`/selections。AI 选择时间窗口及 before/after，再调用 `get_signals_by_cycle`。多 clock 或纯组合 root 没有唯一 clock 时必须保留候选。
- 当前是否执行某个分支仍需波形值；结构化的 control/guard 依赖不等于 active branch 已被证明。先返回谓词/依赖，随后采值判断。
- 对 `state_q` 查询，一跳常只得到 `state_d`、clock/reset。获取 next-state 分支条件需要再展开 `state_d` 的组合驱动；由显式深度/预算控制，并在回执中标出，不能暗含无界递归。

下面的总契约描述 driver 依赖包与后续 waveform 结果的组合，不要求在第一次调用中同时返回所有字段。

一个上下文至少包括下列信息，字段名称在实现时与现有 schema 对齐：

| 信息 | 约束 |
| --- | --- |
| root / source / wave selection | 绑定证据、compile/wave 身份、选定位及完整路径 |
| source location / assignment | backend 来源、赋值或过程位置；有精确 span 才提供 span |
| dependency inventory | data、guard、clock、reset/set、feedback/hold、port binding；角色允许多值 |
| candidate vs active | 分开列潜在分支依赖与当前已验证的 active branch；未知 guard 保留候选 |
| observations | 每个边沿的物理时间、before/after、采样 offset、值和 value_status |
| sequential boundary | 条件触发边沿、用于决定 Q 的 pre-edge D/guard；不混用 Q 更新后的条件 |
| coverage / limits | 各阶段完整性、是否多驱动、未 dump、超预算、未建模异步控制及停止位置 |
| next actions | 可直接使用的精确信号/选位和查询参数；不自动给出根因结论 |

“附近”只作为可选源码片段，不作为依赖关系。enum 名称从有效语义/波形 metadata 取得；没有 enum 证据就显示整数，不能凭变量名推定 FSM 编码。

可先采用的依赖预算草案：默认一跳，最多 32 个可观察 selections；节点/表达式/返回字节/运行时间另设硬上限。周期数交由采样调用的现有 cap/新局部模式控制，案例中的 9 是请求数量，不是产品最大值。超限返回被省略类别和数量，不能用截断的集合证明独占驱动。

对 sequential root，默认依赖范围限于当前一跳。仅在调用者显式要求进一步展开时，再按预算查询一个直接 D 的组合 driver，以覆盖 state_d、enable 等常见结构；结果中显式计为额外一跳，不自动扩展所有分支或无界 fan-in。

### 输出成本与使用门槛（2026-10-02 澄清）

更多字段可能增加模型输入 token、后端计算及选择负担；少一次工具调用不能单独证明效率提升。前文完整契约/JSON 是信息语义示意，不是每次查询的全量默认响应。

- **默认不增加依赖包。** 只定位驱动源码的请求保留轻量路径。需要组织波形读取列表时，才显式要求依赖。
- **只给直接相关集合。** 不返回全 module 的“附近信号”，不默认递归；32 个 selection 是硬上限草案，不是每次应填满的返回数量。多余候选按明确作用域/展开层级分次查询。
- **角色代替任意排序。** 区分 data/control/clock/reset，保留唯一时钟或多时钟歧义；不要在没有时间证据时评选“最可疑控制信号”或称某个分支 active。
- **减少重复表达。** 在兼容前提下，避免把同一批长路径完整重复到 dependencies、sampling_candidates 和多个 next_actions 中。保留一种主要、可直接传参的表示；实际 source/wave 身份不同时不能为省 token 合并它们。原有公开字段不静默删除。
- **关键缺口不能压掉。** 后端来源、未 dump、绑定歧义、多驱动、覆盖不完整及显示截断必须保留。达到展示上限时说明省略；分析本身未完成时不能杜撰精确剩余候选数。
- **压缩与减少内容分别评估。** 当前通用 compact 输出白名单不含 explain_signal_driver；其现有机制也不等于语义相关性筛选。若要复用，需要额外适配/兼容验证，不能只去空白就宣称解决了 token 问题。先做最小按需字段，避免为压缩三个依赖引入新的句柄协议或庞大参数集合。
- **记录实际利用而非猜测模型注意力。** 观察返回依赖是否进入后续取值/条件检查、是否减少源码读取与名称搜索、是否支持了正确的排除判断。单凭最终回答未提及某字段，不能证明模型完全没使用它。

记录中的 UART tx_d 直接查询结果为 1,629 字节，递归查询为 5,042 字节（紧凑 JSON 序列化计数，非模型 token）。这是现有输出体积基线，不是新增依赖包的性能对照。P1 尚无效率提升结论。

### 公共接口决策门

先实现 `explain_signal_driver` 的按需依赖包 + 现有波形工具的两步流程，功能验证后进行 E1 A/B。若仍有明确组织/往返成本，可实现薄工具 `inspect_signal_context` 的实验原型，组合 driver-step、binding 和局部采样，再做 E2 A/B。组合工具仍不进入当前必做范围，但若实现了，就必须独立证明收益才算收敛；无需在做原型前先证明尚不存在的接口有收益。

拟议请求中的 clock 可由调用者指定，或由唯一、已建模的 sequential driver 推导。多时钟/多驱动时返回候选或分窗，不能按名字随便选时钟。对于纯组合 root，可直接接受有限时间窗口。

### 验收

- C1：能给出 tx_q/tx_d 的真实 data/control 候选和可读声明；消除 NPI 单元名到波形路径的人工猜测。至少对已支持分支提供当前/前后沿的正确相位证据。
- C3：显示 pre-edge done=1 与 post-edge done=0；不把正常状态转移标为异常。
- C4：以 state_d/done 为控制链起点提供下一跳；继续到 grant 映射需显式扩展，不承诺一次调用自动到达全局根因。
- 多 driver、TB 驱动、异步 reset、clock gating、X/Z、未 dump、端口/位段边界、后端切换均保留既有含义；部分证据可用不等于 complete。
- NPI 的 driver/load consumer-alias 防误归因和 native callback 上限必须保留。NPI 异步 reset 值目前未建模的事实继续暴露，不用 reset 名称猜常量。
- 复用 [test_dynamic_evidence.py](../../tests/test_dynamic_evidence.py)、[test_dynamic_binding.py](../../tests/test_dynamic_binding.py)、[test_npi_dynamic.py](../../tests/test_npi_dynamic.py)、[test_trace_divergence.py](../../tests/test_trace_divergence.py)，再增加少量 public contract 组合测试；避免重写全套同义测试。

## 评测方案：实现后验证收益与收敛

### 固定任务

以 cases.json 为起点，为每个案例制作两份材料：

- 提供给调试者的输入：compile/log/wave 路径、症状、允许的范围；不包含这里的预期答案、XOR/constant-grant 源码注释提示或内部诊断结果。
- 评审 oracle：本次记录的时间/关系/边界、正常反例以及工具不能回答的部分。控制组的标签不能代替本运行取证。

有注释提示的原始项目不能直接称为盲测 fixture。若需要严格盲测，应在另外授权的本地副本/可再分发最小 fixture 中去除答案提示，并记录与原样本的区别。

### 对照

P0 完成功能验收后冻结一个共同底座，再测试增强效果：

- E1-A：共同 P0 底座 + 原有 NPI/driver 输出与工作流。
- E1-B：同一底座 + 本次 NPI/explain_signal_driver 返回增强，依赖仍按需开启。
- E2-A（如做）：已经收敛的 E1。
- E2-B（如做）：E2-A + 新组合工具。

2026-10-01 的历史基线只用于问题复现和确定性回归，不与“所有修复都加完”的版本直接比较并把收益归给依赖包。若增强拆成多个影响独立的子项，可再做消融，不能只展示捆绑结果。

用户任务、模型/推理深度、预算、artifact 和非目标工具保持一致。A/B 唯一允许的工具描述差异是该增强真实需要的参数/字段说明，需记录并计入 token 成本；不能给 B 加根因提示或额外调试诀窍。具体执行按 [AB_ACCEPTANCE.md](AB_ACCEPTANCE.md)。

### 指标与通过条件

- 证据正确率：路径/选位/相位/值是否匹配；两侧是否实际验证；完整性与未检查范围是否如实报告。
- 正常案例误判：C1、C3、C5 不得归罪正常寄存器/FSM/对照映射。
- 故障定位：C2 找到数据改变边界，C4 找到 grant 丢失边界；不能只复述日志。
- 可用性：完成同等证据任务所需有效调用数、失败恢复数、回执字节、端到端时间、模型 token。人工错误单列，discovery/scan/setup 单列。
- 上下文收益：分别测新增依赖的响应 token、整个任务累计输入/输出 token（包含工具描述、源码和重复查询）、依赖后续利用率、无关候选数量和取错信号次数。利用率是行为代理指标，不能等同模型内部“看没看”；采样了无关信号也不算有效利用。
- 资源：冷/热耗时、峰值 RSS、native/解析事件量、超时/取消响应；FSDB 不得重叠 FFR 访问。
- 对依赖包的门槛：E1-B 相对于 E1-A，保持事实正确性和正常反例表现，并在现有案例上证明可重复的质量或效率收益。NPI 返回增强单列状态，必须有 C1 的真实 NPI 收益，不能只凭 Source Graph 案例宣布收敛。E2-B 还需相对于 E2-A 证明额外收益。未运行、无收益或收益来自混杂因素时，记录为待验证/待优化，不认定收敛。
- 默认策略门槛：覆盖“只找源码”“继续追数据驱动”“检查控制条件”三类任务。若依赖包增加总 token/时间却没有正确率或成功率收益，收缩字段/范围，或仅保留路径可用性修正；不得因为字段已实现就默认全量返回。

本次 100 次探索调用用于复现观察，不用作“理想最少调用数”，也不承诺调用数或运行时间的固定提升百分比。

## 交付拆分与边界

建议分成独立可评审改动：

1. 局部周期模式与错误原因，含相位/截断回归及真实案例复测。
2. Wave/design binding；再按最小复现拆分 generate、array/member 解析。
3. 复用 dynamic-step，为 `explain_signal_driver` 增加按需结构化依赖及可采样绑定，补 schema、兼容说明和两步工作流评测。
4. 如需要，可实现薄组合工具的实验原型；完成事实正确性验证后，再按 E2 对照证明独立收益。

每一项分别记录“未开始 / 实现中 / 已实现待功能验收 / 已实现待 A/B / 待优化 / 已收敛”。P0 通过其功能门槛即可收敛；E1/E2 必须附实际 A/B 报告。没有合适评测入口或运行额度时保留待验证状态，记录缺失条件；不拿固定脚本回放或人工已有结论冒充模型 A/B。

本轮仅落盘调研与计划。未修改产品代码、AGENTS.md、CLAUDE.md 或项目 RTL；未安装依赖、创建 KDB、重新仿真或执行提交。
