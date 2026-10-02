# P0-SAMPLING 功能验收

结论：显式局部周期入口的确定性修复通过；不属于 E1 收益，也不是模型 A/B。

实现以 `cycle_index_origin="window"` 启用，要求起始时间；默认 global 的编号与读取行为保留。复用 event pages、GroupCursor、现有极性规则、列采样和 transaction 累计预算。硬上限为 64 MiB 估算解码字节、100 万事件、64 个扩展时间窗、30 秒 deadline。时钟出现第一个 X、记录缺口或同刻歧义即停止，不跨越后补齐。角色/依赖包尚未实施。

证据：

- [297 项回归通过](tests.txt)，最后仅增加零行结果的 `zero_coverage` 标签后，[15 项局部回归](final-local-tests.txt) 通过。覆盖局部/全局兼容、before/after、初始与窗内 X、FST dump-off、100 fs 边沿、重复 ps 标签、相同时间冲突、有限窗口与输出上限、EOF offset、选择/表达式、累计预算、取消及 native group 释放；现有 server 并发/锁回归保留。
- [C1 新 MCP](verified-ot/calls.jsonl)：before/after 各 5 行。7145478 ps 边沿前 tx_q=1、tx_d=0、tick=1，沿后 tx_q=0、tick=0。
- [C3 新 MCP](verified-x1/calls.jsonl)：944095000 ps 起的 before/after 各 9 行；完成窗口各 7 行，944515000 ps 前 state_q=2/done=1，后 state_q=0/done=0。两者均为局部 complete，明确不声称全局编号完整。
- [逐行核对](real-row-checks.json)由 [verify_sampling.py](../verify_sampling.py) 实际运行：42 行、172 个信号值与独立点读一致，整表与已有局部 helper 一致。真实产物都是 1 ps；亚皮秒语义由独立 fixture 覆盖。
- MCP 每次新建服务，`verified-*/loaded.json` 保存实际入口/模块 hash；`tools.json` 保存接口。全局旧错误保留在 [x1/calls.jsonl](x1/calls.jsonl)，证明没有通过默改默认行为修复。
- [失败的表达式回归](failed-expression-regression.txt) 保留：初版局部 clock 只接受裸 0/1，漏了合法 VCD `b0/b1` 编码；改为共用 `bit_value` 后通过全部回归。

## 资源实测

[sampling_probe.py](../sampling_probe.py) 对相同 3 个信号、同一窗口、同一格式顺序运行；每格为新 Python 进程内 first / warm 两次。计时包含首次 parser 打开，不刷新 OS cache，未并行运行 workload。global/window 共用当前 checkout，global 路径与基线一致。记录的是返回 page records（含 anchor），不是磁盘 I/O 或模型 token。旧 global 错误与新成功没有相同输出语义，不能称同结果加速比。

| 工作负载 | global 秒 first/warm | window 秒 first/warm | page records global → window | VmHWM KiB global → window（first） |
|---|---:|---:|---:|---:|
| X1 before，9 cycles | 3.925 / 3.914，失败 | 1.240 / 1.053，成功 | 131072 → 27 | 91568 → 36728 |
| X1 after，9 cycles | 3.927 / 3.905，失败 | 1.236 / 1.054，成功 | 131072 → 27 | 91252 → 36616 |
| C1 before，5 cycles | 0.096 / 0.062，失败 | 0.213 / 0.004，成功 | 24015 → 19 | 55172 → 60332 |
| C1 after，5 cycles | 0.086 / 0.002，成功 | 0.210 / 0.004，成功 | legacy 未计量 → 19 | 122872 → 58844 |

完整数据在 [measurements/](measurements/)。X1 新调用不展开从 0 起的时钟历史；C1 首次局部 before/after 更慢，before 的峰值内存也略增。FSDB 初始化、原生 group 装载仍有成本；VCD 仍先解析整个文件，FST 仍可能解压较大原生块。64 MiB 是累计解码预算，不是进程 RSS 上限，不承诺原生成本 O(N)。

局限：before 的 sample_time_fs 表示严格排除的边沿边界；after 表示 offset 观察时刻，不保证所有 delta 已稳定。旧 wrapper 亚皮秒不具备顺序证据时返回 frontier。整数 ps 输入不能从同一 ps 标签中间分页。局部 sweep 的 degraded 和源/历史编译身份限制均未被这些采样结果消除。
