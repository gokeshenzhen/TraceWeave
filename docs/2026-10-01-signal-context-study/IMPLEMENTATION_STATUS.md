# 实施与验收状态

更新时间：2026-10-02。此文件是新 session 的状态入口，所有“通过”必须附实际证据链接。

接续实施已完成独立评估与基线（提交 `8acc369`），见 [ASSESSMENT.md](evaluation/p0-baseline/ASSESSMENT.md)。起点产品为 `5807572`；当时 214 项回归通过，C1/C3 采样缺陷与 X1 top 绑定失败用新 MCP 复现，C1 真实 NPI 可用。随后完成 P0-SAMPLING，见下表与功能报告。用户最新要求逐阶段验证并 commit 后再进行下一阶段。历史研究不作为新增增强收益证据。

| 项目 | 实现 | 功能 / 真实案例验证 | 模型 A/B | 是否收敛 |
| --- | --- | --- | --- | --- |
| P0-SAMPLING：局部周期、预算与相位/缺口 | 已实现 | 297 项回归 + 最终 15 项局部回归；C1/C3 真实新 MCP 与逐行点读核对 | 不要求 | 已收敛；[证据](evaluation/p0-sampling/REPORT.md) |
| P0-BINDING：身份 / generate / 数组成员解析 | 已实现（当前案例范围） | 361 项最终回归 + C2/C4/C5 双侧值、源码与选位表达式；[验收](evaluation/p0-binding-shared/REPORT.md) | 不要求 | 已收敛；保留真实语义/历史身份缺口 |
| E1 / P1-CONTEXT：NPI 返回可用性增强 | 已实现 | 430 passed / 6 skipped；C1 真实 NPI 候选已验证；[功能证据](evaluation/p1-functional/REPORT.md) | 已冻结 30 个任务 run + 2 个简单定位 run，开始执行；[manifest](evaluation/e1-20261002/manifest.json) | 已实现待 A/B |
| E1 / P1-CONTEXT：explain_signal_driver 按需依赖包 | 已实现 | 默认关闭、预算、单 backend、C1/C4 功能通过；[证据](evaluation/p1-functional/REPORT.md) | 已冻结 30 个任务 run + 2 个简单定位 run，开始执行；[manifest](evaluation/e1-20261002/manifest.json) | 已实现待 A/B |
| E2：薄组合工具（可选） | 未决定实施 | 未开始 | 一旦实施就必须；未开始 | 不适用，尚未实施 |

目前已完成：

- 四份保留运行、五个案例的调研。
- 保存 100 次研究 MCP 调用、artifact manifest 和采样诊断。
- 局部 helper 的 9-edge before/after 复现成功；这是基线诊断，不是产品修复或 A/B。
- [实施计划](PLAN.md)、[A/B 规则](AB_ACCEPTANCE.md)、[新 session 提示词](SESSION_PROMPT.md)。

正在执行：

- E1 首批因独立 MCP 缺少 EDA 环境、C1 NPI 加载失败而中断；[原始失败与报告](evaluation/e1-20261002/REPORT.md) 保留。临时 CLI 补充环境转发后，[真实 NPI 模型预检](evaluation/runner-npi-preflight/REPORT.md) 正常退出并确认 `actual_backend=verdi_npi`。产品、任务和正式预算不变，将重新冻结完整配对。
- `evaluation/collect_ab.py` 从原始事件提取真实 usage、调用、依赖后续查询和版本/schema 审计；模型完成与进程退出状态分开记录。此离线计量辅助不参与被测输入，也不替代人工证据评分。

目前未完成：

- P1-CONTEXT 的实际模型收益验收。
- 实际模型 A/B 的全部运行与评分；目前没有“效率提高”或“增强已收敛”的测量结论。

状态使用：

- P0：未开始 → 实现中 → 已实现待功能验收 → 已收敛；模型 A/B 不阻塞其验收。
- E1/E2：未开始 → 实现中 → 已实现待功能验收 → 已实现待 A/B → 已收敛 / 待优化。
- 缺运行入口、额度、artifact 或有效指标时保留未完成，不以计划/脚本存在代替执行结果。
- 记录每次版本、测试、评测报告与未解决问题；不要覆盖或删除失败实验。

交接注意：

- 历史代码基线是 `58075728654fc850a56b8909ac6d1bb3ace8a46e`；新 session 先核对当前 HEAD 和工作区，不 reset 用户修改。
- 案例路径/事实见 cases.json，历史 handle/cursor 不可假设仍有效。
- 本目录沿用 `docs/*` 的 Git 忽略规则；文件存在本地，但普通 git status 可能不显示它们。
- AGENTS.md / CLAUDE.md 未修改。本轮没有安装、生成新 KDB 或重新仿真；产品修复与证据按阶段提交。
