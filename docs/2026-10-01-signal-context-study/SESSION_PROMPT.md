# 新 session 提示词与模型建议

更新时间：2026-10-02。当前只有调研与实施/验收计划，产品代码、功能回归和模型 A/B 均未开始。新 session 先读 [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md)，不要把历史研究调用当作修复或收益证据。

## 模型与推理深度

建议主实施/评审 session 继续使用 **GPT-6-Astra，推理深度 max**；E1/E2 的 A、B 两组也固定相同模型和深度。请在新 session 的模型设置中选择，提示词本身不保证改变运行配置。

本任务涉及局部周期语义、NPI/Source Graph 适配、缓存/取消/锁约束和实验归因，延续当前设置可以减少交接变量。这个选择是任务判断与实验控制，**没有实测证明 max 比 high 更适合本项目**。本轮先比较工具能力；若以后要降成本，另做模型/深度对照，不同时改变工具与推理深度。

已核对本地可用模型元数据。官方 [GPT-6-Astra 模型文档](https://developers.openai.com/api/docs/models/gpt-6-astra) 列出了 max 支持；官方 [reasoning effort 部署建议](https://developers.openai.com/api/docs/guides/deployment-checklist#set-up-reasoningeffort) 要求结合实际评测权衡更高深度的质量、时延和成本。这里没有模型胜率或成本最优结论。

## 可直接复制：先评估，再分阶段实施与验收

```text
请在 /home/robin/Projects/mcp/TraceWeave 工作，接续单信号调试上下文研究。先独立评估计划与当前代码是否一致，再按验证后的最小范围分阶段实施、测试和验收；不要把计划描述当作已经实现的能力。

先遵守仓库 AGENTS.md，并读取：
1. docs/2026-10-01-signal-context-study/IMPLEMENTATION_STATUS.md
2. docs/2026-10-01-signal-context-study/PLAN.md
3. docs/2026-10-01-signal-context-study/AB_ACCEPTANCE.md
4. docs/2026-10-01-signal-context-study/README.md 和 cases.json
原始回执、artifact manifest 和诊断脚本按案例定向读取，避免把全部研究 transcript 灌入上下文。

开始时核对当前 HEAD、工作区变化、相关文件和现有实现。历史研究基线为 58075728654fc850a56b8909ac6d1bb3ace8a46e，禁止 reset 用户改动。验证实际 MCP 服务/worker 加载的代码版本，不能只改文件后继续测旧服务。发现计划与代码或新证据不符时，先记录偏差并修订对应设计，不照抄假设。

实施顺序与验收边界：
- P0-SAMPLING：复用现有局部采样内核，提供显式局部 cycle 模式，保留默认全局编号语义与 64 MiB 预算。before/after 是相位，不是往前/往后搜索。稳定局部窗口不应被早期 clock-X 阻断；窗口内 X/缺口必须诚实停止或返回部分连续前缀，不跳过后补齐。用 C1/C3、风险回归和读取量/资源证据验收，不要求模型 A/B。
- P0-BINDING：按真实声明、类型和 elaboration 证据修复 wave/design 绑定，以及案例涉及的 generate、常量数组索引和 packed struct 字段。禁止无条件 strip TOP、按字符串猜位选或删除真实 coverage gap。用 C2/C4/C5 和定向回归验收，不要求模型 A/B，不承诺一次支持所有 SystemVerilog 结构。
- P1-CONTEXT / E1：允许先实现 NPI 返回适配与 explain_signal_driver 的按需依赖包，然后做功能验证和实际模型 A/B。优先复用 dynamic_evidence、npi_dynamic、source_graph_dynamic、dynamic_binding 等已有模块；返回 data/control/clock/reset、精确可采样 selection 与缺口，由 AI 选择窗口/相位后调用 get_signals_by_cycle。默认关闭额外依赖，默认一跳，有硬预算；不在 driver 查询中自动返回完整周期值表。
- E2：inspect_signal_context 等组合工具不是核心交付前提。若确有理由实现原型，必须相对于 E1 再做独立增量 A/B；E1 的收益不能代替它的验收。

保持既有 backend 优先级、同一 backend/artifact 的事实来源、TB-driver 防误归因、NPI 遍历上限、FSDB 全局锁、取消/超时与 partial/coverage 语义。先复现和建立可测基线，再做影响较大的实现；测试覆盖真实行为风险，不堆叠同义测试。

A/B 是用户要求的收敛条件：
- 两组使用相同、已验收的 P0 底座；只改变被测返回增强。不能把采样/路径修复的收益算到新增 JSON 上。
- 按 AB_ACCEPTANCE.md，在正式运行前冻结任务、判据、预算与版本。建议起点为五个现有案例、每例每组 3 次；以实际运行资源评估该规模，任何调整在正式运行前记录，不能看结果后挑门槛。
- 模型固定 GPT-6-Astra，reasoning effort=max，两组相同；独立新上下文与 MCP 会话，顺序运行，保存失败和未使用新字段的结果。
- 实现者可读研究报告；被测 agent 不得得到报告答案、oracle、历史分析 transcript 或额外暗示。两组相同症状与调试纪律，不能只引导 B 使用新参数。原始源码中的 mutant 注释限制必须保留说明。
- 记录整项任务 token、耗时、调用/恢复、依赖实际使用，以及正确证据和正常反例误判；字段变多、调用变少或单测通过都不能单独证明收益。没有真实 usage 时，不以 JSON 字节冒充 token。
- NPI 和 explain_signal_driver 增强分别记录适用案例与结果；NPI 必须有 C1 的真实 NPI 受益证据，不能借用 Source Graph 的收益宣布收敛。捆绑实验只能证明整体效果，需要独立结论时另做消融。
- 实现完成但 A/B 未执行，记“已实现待 A/B”；没有可重复收益，记“待优化”。至少当前案例满足预先约定的质量或效率收益门槛，才能记“已收敛”。P0 已独立验收的修复不受此阻塞。
- 缺独立 runner、可用模型额度、真实 NPI 前提或关键计量能力时，如实留下待验证项；脚本回放不能代替模型 A/B，不把计划中的运行写成已完成。

对仿真产物的分析遵守仓库 MCP 工作流；运行本地 EDA 工具时按 eda-environment 技能。优先复用现有只读产物，不为本任务重新仿真、改被分析项目 RTL、安装依赖或新建 KDB；确有缺项时先记录与解释必要性。不要提交、推送或修改用户客户端配置。

持续更新 IMPLEMENTATION_STATUS.md。将功能复测、A/B manifest、原始 run/transcript、逐案例评分和 REPORT.md 保存到本研究目录下的 evaluation/<experiment-id>/。最终分别报告改了什么、测了什么、哪些已收敛、哪些仍待 A/B/优化以及兼容性/资源限制。这个 docs 子目录受现有 Git 忽略规则影响，确认文件确实落盘；不要因 git status 干净就认定文件不存在。
```

如果新 session 只做方案评审，在上面提示词末尾追加：

```text
本 session 只评估并修订实施计划、风险和验收方案，暂不修改产品代码或启动模型 A/B。请给出可实施结论与必要的计划修订。
```

这两种入口都不把可选 E2 变成必做项，也不允许用“已写测试/评测脚本”替代实际验收。
