# S1 源码定位：完整配对重试

初始 S1-B 在 driver 查询前遇到外部模型容量错误，见 [失败记录](../e1-20261002-env/S1_INFRASTRUCTURE.md)。本目录是按冻结规则执行的完整 A/B 重试，两组模型、max 深度、提示、P0、工具 schema 差异、300 秒预算和缓存条件均保持不变；不与原 A 拼接成一对。

两组都完成任务：实际 `verdi_npi` 定位 `uart_tx.sv:45` 的 `always_ff`，交叉核对已验证的编译源码，给出赋值行 49 / 53 和历史身份限制。两组均 8 次 MCP 调用、0 次额外依赖请求、0 次波形值/周期/握手检查；B 显式 `include_dependencies=false`，返回中没有 `dependency_context`。**当前简单任务没有被升级成行为取证。**

| 指标 | A | B |
| --- | ---: | ---: |
| elapsed 秒 | 127.943 | 193.681 |
| input tokens | 278,319 | 280,979 |
| cached input（包含在 input） | 250,368 | 251,776 |
| output tokens | 1,928 | 1,921 |
| reasoning output（包含在 output） | 614 | 640 |
| input + output | 280,247 | 282,900 |

这是默认关闭的行为检查，不是新的效率收益案例。单对 B 更慢且 token 略高，不作统计或新增依赖因果归因，也不混入核心五案例的成本。

[manifest](manifest.json)、[运行索引](runs.jsonl)、[评分](grading.jsonl)、[指标](metrics.jsonl)、[schema/版本审计](audit.json)、[流程/最终哈希审计](workflow-audit.json) 和每组原始文件完整保留。提示与冻结产品匹配，新 MCP PID 各不相同。与核心实验一样，hierarchy/scan 实际串行，这是流程偏差；源码只读命令的路径均有此前 lookup。没有创建 KDB、重新仿真或修改外部源码。

执行命令（仓库根目录）：

```bash
.venv/bin/python docs/2026-10-01-signal-context-study/evaluation/run_ab.py run docs/2026-10-01-signal-context-study/evaluation/e1-s1-retry-20261002
```

它只因基础设施失败重试完整小任务配对；核心 30 个 run 的 28 次预算超时没有重跑或删除。
