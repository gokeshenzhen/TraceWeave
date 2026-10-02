# 新 session：P1 兼容性、增量收益与回滚决策

更新时间：2026-10-02。上一轮已结束，P0 当前案例已验收，P1 无可验收收益、待优化。本次只更新交接文件，没有执行下一轮测试、产品修改或回滚。新执行方案见 [NEXT_ROUND.md](NEXT_ROUND.md)，实际进展见 [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md)。不要按原计划重新实现 P0。

## 模型与推理深度

| 用途 | 建议模型 / 深度 | 理由 |
| --- | --- | --- |
| 新主 session：读代码、做差分、改 runner、审查评分与取舍 | **GPT-6.1-Sol / high** | 本轮主要是诊断、代码推理与实验控制；以 high 为工作起点，不假设 max 必然有更高收益 |
| 实际 A/B 的被测独立 agent | **gpt-6-astra / max**，两组相同 | 延续上一轮实验控制；主 session 的 high 不应被 runner 自动继承成新的实验深度 |

这是工程选择，不是模型优劣实测。2026-10-02 已检查本地 `models_cache.json`，gpt-6.1-sol 与 gpt-6-astra 均列有 high/max；该元数据不保证运行时额度或服务可用。官方 [GPT-6.1-Sol 模型文档](https://developers.openai.com/api/docs/models/gpt-6.1-sol) 确认 high/max 受支持，并面向复杂代码等任务；[部署建议](https://developers.openai.com/api/docs/guides/deployment-checklist#set-up-reasoningeffort) 将 medium/high 用于诊断和代码推理，更高深度需要用质量、时间和成本证据权衡。

按用户本次模型偏好，主 session 改为 GPT-6.1-Sol / high；实际被测模型继续单独控制。主 session 用 high 是建议，尚无本项目 Sol/Astra 或 high/max 对照证明其最优。评测保留 max 是控制变量，不是宣称 max 更好，也不把上轮超时直接归因于 max。若之后需要比较深度，另开独立实验，不能和 P1 收益同时改变。

请在新 session 的模型设置里选择主 session 的模型/深度；提示词本身不保证改变实际配置。正式评测核对 runner 命令、manifest 和实际模型记录，不静默换模型或深度。如果之后也将被测模型换成 Sol，须另冻结实验、两组一起更换；不能用旧 Astra 的 A 组与新 Sol 的 B 组比较工具收益。

## 可直接复制的完整提示词

```text
请在 /home/robin/Projects/mcp/TraceWeave 接续 P1 下一轮验证与回滚决策。遵守 AGENTS.md，先独立核对当前代码和计划，再按门槛分阶段执行；不要重新实现已验收 P0，也不要原样重跑上一轮 30 个模型任务。

先读取：
1. docs/2026-10-01-signal-context-study/IMPLEMENTATION_STATUS.md
2. docs/2026-10-01-signal-context-study/NEXT_ROUND.md
3. docs/2026-10-01-signal-context-study/AB_ACCEPTANCE.md
4. docs/2026-10-01-signal-context-study/evaluation/e1-20261002-env/REPORT.md
5. docs/2026-10-01-signal-context-study/SESSION_PROMPT.md
按需要定向读取 cases.json、原始回执、产品 diff 和相关测试，不把全部历史 transcript 灌入上下文。

现状：P0 基线 651a91c 已在当前案例范围验收；P1 产品提交 010e6df，定向回归 430 passed / 6 skipped，但实际模型收益未达门槛。上轮 A/B 都加载新产品代码，只隔离依赖参数，尚不能替代真正的 P0 与 P1-off 兼容性对照。依赖包默认关闭，共用 npi_dynamic 改动仍需审查。

执行顺序：
- 先用隔离 checkout/worktree 对比真正 P0 与当前 P1-off，验证原 driver 和共享 NPI 动态调用者的返回、后端、coverage、资源与取消/锁语义；发现实质回归先最小修复、隔离或撤回受影响产品改动。
- 修正并验证 runner 的实际并行准备、取证停止、结论收尾和真实 usage 保存；用 A-only 小规模预跑校准共同预算。正式实验前冻结新版本/预算/判据，旧结果不改。
- 用 C1 真实 NPI 与 C2 可用 Source Graph 入口，各三对独立模型定点 A/B：同一初始 driver 查询，A 原返回，B 加依赖包，再由模型自由取证。它只诊断收到包后的增量价值，不替代自然采用验收。
- 定点有可重复收益后，再做五案例各三对和 S1 的完整自然采用实验；否则按 NEXT_ROUND.md 停止扩大，并给出有证据的保留/隔离/最小撤回结论。评测基础设施或计量仍不足时，如实记未验证，不能当作 P1 无效。

双方始终共用相同 P0；C4 等确定性修复若实施，也必须先验收再进入双方底座，收益不得归给 P1。保留原质量/效率门槛，NPI 必须有真实 C1 NPI 受益证据，功能通过、字段被用或定点成功不能单独宣布整个工作流收敛。

主实施 session 建议 GPT-6.1-Sol / high；实际被测 A/B 暂时固定 gpt-6-astra / max，不从主 session 继承 high，不因容量错误静默换模型。评测 agent 必须是独立新上下文、新 MCP，不给研究答案/oracle/历史模型思路，不在本会话扮演 A/B。

保持 P1 默认关闭，冻结无关功能扩展，不做 E2。继续复用只读产物与已有 KDB，不重仿真、不修改被分析 RTL、不新建 KDB、不安装依赖、不修改用户 MCP 配置、不推送。禁止 reset 用户改动；如撤回 P1，只撤产品中的无收益或退化部分，保留 P0、证据和必要回归，不机械 revert 含证据的整个提交。

每个逻辑改动/阶段完成必要验证后立即 commit，再进入下一实现阶段。持续更新 IMPLEMENTATION_STATUS.md，把差分、预跑、定点和完整评测分别保存到新的 evaluation 目录；保留失败、超时、未采用增强及缺失 usage。该目录被 Git 忽略，要检查 git add -f 后确实纳入提交。

最终分别报告 P1-off 兼容性、NPI 收益、通用依赖包收益和回滚取舍。目标是得到可验证的产品决策，不是必须证明 P1 有用。
```

精简入口也可直接要求“先读取本文件与 NEXT_ROUND.md，并按其中顺序执行”。本文件是给实施/评审 session 的，不能作为被测模型输入，因为它包含历史结果和评审信息。
