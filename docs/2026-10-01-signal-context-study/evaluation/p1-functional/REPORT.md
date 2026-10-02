# P1 功能验收，尚待实际模型 A/B

2026-10-02，共同 P0 基线 `651a91c`。按需 `include_dependencies=true` 返回同一已选 backend/artifact 的一跳候选；默认不查询、不返回依赖包。包保留 data/control/clock/reset/set/feedback 角色、精确波形 selection、不同的源选位及 binding gap。相同 source/wave selection 只保留 wave，避免重复。没有自动采值、active branch 或根因判定。

预算：32 selections、256 个表达式节点、32 KiB 输出、10 秒协作式 deadline；原生 NPI 调用仍服从已有有界遍历及“返回后才能响应取消”的约束，未改线程/锁模型。额外失败只标 dependency gap，不改变原 driver 的已选 backend，也不改其覆盖结论。额外查询使用现有 dynamic_evidence / npi_dynamic / source_graph_dynamic，波形绑定复用 P0。

NPI：异步 pin 可证明 clock、set/reset 的类型及有效电平，不能证明原 RTL 异步赋值；因此保留 data pin 候选，继续暴露 async_control_value_unmodeled。EqComp/OpCell 的输入 pin 仍是结构依赖，把未知运算保留为 unsupported Expr，供 inventory 读取候选，动态求值仍未知并保持原 fallback。

`tests-final-polarity.txt`：430 passed，6 skipped。skip 项是显式要求新建 VCS/KDB 的 live fixture，本任务没有启用；另外既有 cc20 KDB 的只读 NPI 回归通过。角色、missing dump、预算、默认关闭、backend 不混用、LSF 新字段校验、取消/并发、TB alias guard、历史/divergence 均在回归中。

真实新 MCP：
- ot-retry/calls.jsonl：actual_backend=verdi_npi。tx_q 有 clk_i、rst_ni（native set pin，不能按名称猜赋值）、tx_d；tx_d 有 tx_enable、wr、tx_q、tick_baud_q、bit_cnt_q[3:0]、sreg_q[0]，全部 exact dump binding。未知 NPI 运算仍有 gap。初次 ot/ 的 tx_d 因未知运算整体提前停止，保留失败记录；修改后保留了安全的输入 pin 候选。
- x2/calls.jsonl：actual_backend=source_graph。wdata 绑定单个 dma_write_req_i[0].wdata 字段；gnt 为 literal 0，无伪造数据依赖；state_q 返回 rst_ni 控制候选和 state_d。Source Graph 的 coverage/temporal 缺口保留。

这些是功能证据。尚无模型效率/质量收益结论；NPI 与通用 driver 增强都保持“已实现待 A/B”。
