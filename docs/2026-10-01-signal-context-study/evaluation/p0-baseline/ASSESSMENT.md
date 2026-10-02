# 独立评估与基线

2026-10-02；开始 HEAD：`58075728654fc850a56b8909ac6d1bb3ace8a46e`，工作区干净。
本阶段只增加评估、复测脚本和证据，尚未修改产品实现。最新用户要求逐阶段 commit，覆盖 SESSION_PROMPT 中旧的“不提交”。

- P0-SAMPLING 的描述与实现一致：`get_signals_by_cycle` 无条件进入 `_full_clock_edges`；`sample_signals_on_edges` 已共享严格 before、整数 fs 和 selection 内核。新增入口必须显式选择 window origin，不能改变默认 global 编号。
- 可以进一步复用 `transaction_sampling` 已有累计事件/解码字节预算；局部时钟读取仍需有界分页，FST 的准备阶段会物化所给时间窗，因此不能仅在 0..EOF reader 上提前终止迭代。初始读取页应限时间，后续不重置预算。
- 当前 `source_graph_adapter._selected_top` 按字符串首段选 top；新进程 X1 三次查询均在 `source_graph_target_top_unresolved` 停下。`connectivity_query` 仅解析末尾 packed selection；projector 对 fixed array 仍保留 `array_connectivity_unmodeled`。需分层修复与验收，不能删除这些 gap 冒充支持。
- P1 继续复用 typed dynamic-step。C1 两次实际 NPI 查询都 resolved，是真实 NPI 的功能/A/B 前提；尚未证明 dynamic-step 覆盖或新增依赖有收益。
- `codex exec` 本地入口可用（0.160.0）；尚未核验独立运行的模型额度与 usage。正式 E1 仍须先验收共同 P0，再冻结任务/预算/顺序/判据。可选 E2 暂不实施。

实际执行：

- [定向回归](tests.txt)：214 passed（cycle、FST sampling、FSDB timescale、server、concurrency）。
- [X1 新 MCP](x1-retry/calls.jsonl)：global before/after 九周期均报 incomplete clock；点读确认所选 state/done 前后值可读。扫描 lexical complete / semantic not_run；局部 sweep 59 inspected / degraded / 0 flagged，无全局 clean 结论。
- [C1 新 MCP](ot/calls.jsonl)：global before 失败、after 5 行；真实 NPI 的 tx_q/tx_d resolved。
- 每个新 MCP 有 `loaded.json`（入口、PID、cwd、实际导入模块文件/hash）及 `tools.json`，每次都启动新的本地 server，自动 KDB 关闭。worker 仍由该 checkout 的既有 runtime 启动；基线未发生可用 Source Graph build。
- [X1 读取探针](x1-global-before.json)：读取 131072 条 page records（包括前驱），预算内物化前缀不足以到达目标窗口；耗时约 4.1 秒。与 MCP 重放有并行运行，时间仅诊断，不作为受控速度比较。
- [C1 读取探针](ot-global-before.json)：24015 records，初始 clock-X 阻止晚期窗口。`ru_maxrss` 在本环境可能含 exec 前继承峰值，后续另记录 `/proc/self/status` VmHWM/VmRSS，不据此声称省内存。

失败保留：`x1/` 是首轮脚本运行，完成周期错误复现后，操作者把单信号 point 参数误写成 `signal_paths`，脚本解析错误返回时退出；不是产品失败。修正后的完整运行独立保存在 `x1-retry/`，原文件保留。

这些记录只用于确定性基线；不是模型 A/B，也不支持增强已收敛的结论。
