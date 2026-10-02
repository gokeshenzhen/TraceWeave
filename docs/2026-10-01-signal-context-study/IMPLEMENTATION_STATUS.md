# 实施与验收状态

更新时间：2026-10-02。此文件是新 session 的状态入口，所有“通过”必须附实际证据链接。

接续实施已完成独立评估与基线（提交 `8acc369`），见 [ASSESSMENT.md](evaluation/p0-baseline/ASSESSMENT.md)。起点产品为 `5807572`；当时 214 项回归通过，C1/C3 采样缺陷与 X1 top 绑定失败用新 MCP 复现，C1 真实 NPI 可用。随后完成 P0-SAMPLING，见下表与功能报告。用户最新要求逐阶段验证并 commit 后再进行下一阶段。历史研究不作为新增增强收益证据。

| 项目 | 实现 | 功能 / 真实案例验证 | 模型 A/B | 是否收敛 |
| --- | --- | --- | --- | --- |
| P0-SAMPLING：局部周期、预算与相位/缺口 | 已实现 | 297 项回归 + 最终 15 项局部回归；C1/C3 真实新 MCP 与逐行点读核对 | 不要求 | 已收敛；[证据](evaluation/p0-sampling/REPORT.md) |
| P0-BINDING：身份 / generate / 数组成员解析 | 实现中；driver 根绑定已落地，语义补足待实施 | 根绑定回归及 X1 新 MCP；已越过 top blocker，仍在 instance/array frontier；[证据](evaluation/p0-binding-root/REPORT.md) | 不要求 | 否 |
| E1 / P1-CONTEXT：NPI 返回可用性增强 | 未开始 | 未开始 | 必须；C1 真实 NPI 未开始 | 否 |
| E1 / P1-CONTEXT：explain_signal_driver 按需依赖包 | 未开始 | 未开始 | 必须；未开始 | 否 |
| E2：薄组合工具（可选） | 未决定实施 | 未开始 | 一旦实施就必须；未开始 | 不适用，尚未实施 |

目前已完成：

- 四份保留运行、五个案例的调研。
- 保存 100 次研究 MCP 调用、artifact manifest 和采样诊断。
- 局部 helper 的 9-edge before/after 复现成功；这是基线诊断，不是产品修复或 A/B。
- [实施计划](PLAN.md)、[A/B 规则](AB_ACCEPTANCE.md)、[新 session 提示词](SESSION_PROMPT.md)。

目前未完成：

- P0-BINDING 与 P1-CONTEXT 的产品变更、回归及真实案例验收。
- 实际模型 A/B；没有任何“效率提高”或“增强已收敛”的测量结论。

状态使用：

- P0：未开始 → 实现中 → 已实现待功能验收 → 已收敛；模型 A/B 不阻塞其验收。
- E1/E2：未开始 → 实现中 → 已实现待功能验收 → 已实现待 A/B → 已收敛 / 待优化。
- 缺运行入口、额度、artifact 或有效指标时保留未完成，不以计划/脚本存在代替执行结果。
- 记录每次版本、测试、评测报告与未解决问题；不要覆盖或删除失败实验。

交接注意：

- 历史代码基线是 `58075728654fc850a56b8909ac6d1bb3ace8a46e`；新 session 先核对当前 HEAD 和工作区，不 reset 用户修改。
- 案例路径/事实见 cases.json，历史 handle/cursor 不可假设仍有效。
- 本目录沿用 `docs/*` 的 Git 忽略规则；文件存在本地，但普通 git status 可能不显示它们。
- AGENTS.md / CLAUDE.md 未修改，产品代码未修改。本轮没有安装、生成新 KDB、重新仿真或提交。
