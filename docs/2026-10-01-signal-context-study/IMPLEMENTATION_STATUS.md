# 实施与验收状态

更新时间：2026-10-02。首轮独立评估、分阶段实现、功能复测和实际模型 A/B 已完成；**P0 在当前案例范围已收敛，NPI 返回增强与 driver 依赖包均待优化，默认关闭额外依赖。** 没有将共享 P0 的收益归给新增返回。

| 项目 | 实现 | 功能 / 真实案例验证 | 模型 A/B | 验收状态 |
| --- | --- | --- | --- | --- |
| P0-SAMPLING：局部周期、预算与相位/缺口 | 已实现；默认 global 不变，显式 window | 297 项回归 + 最终 15 项局部回归；C1/C3 新 MCP、42 行 / 172 值独立点读、资源实测；[证据](evaluation/p0-sampling/REPORT.md) | 不要求 | 已收敛（已测试语义与资源边界） |
| P0-BINDING：身份 / generate / 数组成员解析 | 已实现（当前有界案例范围） | 361 项最终回归；C2/C4/C5 双侧波形、源码与精确选位；[证据](evaluation/p0-binding-shared/REPORT.md) | 不要求 | 已收敛；保留真实语义/历史身份缺口 |
| P1 / E1：NPI 返回增强 | 已实现 | 430 passed / 6 skipped；C1 真实 NPI clock/set/data/control 候选；[功能证据](evaluation/p1-functional/REPORT.md) | 实际 C1 三对；两次 B 运行采用增强但超时；[最终报告](evaluation/e1-20261002-env/REPORT.md) | **待优化**，无可重复收益证据 |
| P1 / E1：explain_signal_driver 按需依赖包 | 已实现；默认关闭，一跳与硬预算 | 单 backend、绑定、缺口、预算、取消/并发和只读 NPI 回归通过；[证据](evaluation/p1-functional/REPORT.md) | 实际五案例各三对 + S1；未达到质量或效率门槛 | **待优化**，不能称已收敛 |
| E2：薄组合工具（可选） | 未实施 | 不适用 | 未执行；若实施须另做增量 A/B | 非本轮交付前提 |

## 独立评估与确定性修复

起点产品为 `5807572`，评估与基线提交 `8acc369`，见 [ASSESSMENT.md](evaluation/p0-baseline/ASSESSMENT.md)。当时 214 项回归通过，并用新 MCP 复现 C1/C3 采样缺陷和 X1 top 绑定失败；真实 NPI 可用。实施沿用已有采样、动态证据和连接后端模块，没有无条件删除 TOP、猜位选、删 coverage gap 或改 backend 优先级。

用户要求按阶段验证、commit 后再进入下一实现；主要产品提交为：

- `5b62a34`：有界局部周期。
- `f6d8da4`：验证 Verilator root 绑定。
- `e51af86`：类型化 packed array/member 绑定。
- `5be9bd6`：精确 generated target。
- `651a91c`：共享类型/波形 selection，作为双方共同 P0 底座。
- `010e6df`：默认关闭的一跳 driver 依赖包及 NPI 适配。

后续提交分别保存 runner、环境修正、计量/评分与完整原始证据。没有在正式模型批次中修改产品或被测 runner。6 个跳过测试依赖新建 VCS/KDB fixture，本任务没有启用；既有 cc20 KDB 的只读回归实际通过。

## 实际模型实验结论

正式核心 30 个 run 使用相同 `gpt-6-astra / max`、提示、P0、只读产物、300 秒预算与工具权限。每次独立模型/MCP 会话，顺序 AB/BA/AB。A 隐藏并拒绝增强参数，B 仅暴露按需参数，没有额外采用提示。

- 核心行为任务两组各成功 1/15（均首轮 C1），各有 14 次预算超时。28 个超时任务缺失真实 usage，不以 JSON 字节代替 token。
- 首轮 C1-B 行为解释正确，但没有使用 NPI/依赖；不能归为增强收益。它的总 token 为 509,443，A 为 499,584，仅这一成功配对也没有省 token。
- 15 个 B 中 4 个实际请求依赖，共 5 包：NPI 3 次、Source Graph 1 次、Static 不可用 1 次；采用增强的任务均未完整交付。
- C4 的 generated DMA 响应递归查询存在 `source_graph_frontier_expansion_stalled` 限制；没有扩张 P0 的已验收支持范围。各轮 C4 的推荐工具还出现 FST deadline 错误，保留为实际结果。
- 记录到共同流程偏差：hierarchy/scan 串行而非提示要求的并行。compile_log 相同，源码读取有此前 compile-set lookup；schema/提示/产品指纹一致。不能声称完整流程合规或把时间差全归因于增强。
- S1 初始 B 遇到模型容量错误，保留 [失败记录](evaluation/e1-20261002-env/S1_INFRASTRUCTURE.md) 后重跑完整配对。[重试两组](evaluation/e1-s1-retry-20261002/REPORT.md) 都以真实 NPI 完成源码定位，没有额外依赖或波形值调查；简单任务的默认关闭行为通过。

本次环境修正后的实验共实际启动 34 个 run：30 核心 + S1 初始 2 个 + S1 重试 2 个。核心没有因超时重跑；S1 初始配对单列，不与重试拼接。更早独立 MCP 缺 EDA 环境的 [失败批次](evaluation/e1-20261002/REPORT.md) 同样保留，经 [真实 NPI 模型预检](evaluation/runner-npi-preflight/REPORT.md) 才重新冻结正式批次。

[最终 REPORT](evaluation/e1-20261002-env/REPORT.md) 汇总逐案例结果、正常反例、成本、采用情况、流程偏差与归因限制；其目录含 manifest、raw transcript/calls、loaded/schema、grading、metrics、summary 与最终哈希审计。[第一轮](evaluation/e1-20261002-env/FIRST_REPETITION.md)、[第二轮](evaluation/e1-20261002-env/SECOND_REPETITION.md)、[第三轮](evaluation/e1-20261002-env/THIRD_REPETITION.md) 检查点保留，不覆盖失败历史。

## 下一轮交接（计划已记录，尚未执行）

用户计划在新 session 继续验证及决定是否回滚。当前不整体回滚 P1；保持默认关闭，按 [NEXT_ROUND.md](NEXT_ROUND.md) 依次执行真正 P0 vs P1-off 兼容对照、流程校准、C1/C2 定点实际模型 A/B，有可重复信号后再进入完整五案例自然采用验收。P1 状态仍为待优化，不因计划文件存在改变。

本次代码复核确认依赖入口默认直接返回，但共享 `npi_dynamic.query_step` 的修改并不由该参数隔离；上一轮两组都加载新产品，需补真正旧基线差分。可测实验无收益时按最小产品范围撤回，证据不足时记未验证；无论哪种结果都保留 P0 与全部旧实验记录。

[新 session 提示词](SESSION_PROMPT.md) 已重写为下一轮入口，主实施建议 GPT-6-Astra / high，被测 A/B 暂时维持 gpt-6-astra / max。模型深度选择不代表收益实测。本次仅整理和提交交接文档，未启动新的实验或修改产品。

## 保留限制与后续边界

P0 支持当前已测试案例和有界结构，不承诺全部 SystemVerilog 或所有递归路径。局部 64 MiB 是累计估算解码预算，不是进程 RSS 上限；C1 首次局部查询并非普遍更快，完整资源条件见采样报告。P1 的 native 取消仍在有界调用返回后响应，不改变 FSDB 全局锁与现有调度语义。

原始 RTL 的 mutant 注释使实验不是严格盲测；当前源码与历史波形的精确编译身份仍未独立证明。大量超时限制了模型收益评估，但不允许因此改变原门槛或用功能测试宣布增强收敛。按验收规则，本轮保留负结果、增强默认关闭；没有必要无限追加运行以证明原方案正确。若后续优化，另冻结版本、预算、流程和配对，不能复用本轮 P0 成功作为新增字段收益。

AGENTS.md / CLAUDE.md 未修改；没有安装依赖、创建新 KDB、重仿真、修改外部 RTL、推送或修改用户 MCP 客户端配置。研究目录受 Git 忽略规则影响，证据已按用户授权强制加入并分阶段提交；后续接手先核对 HEAD 与工作区，不 reset 用户改动。
