# P0-BINDING：packed 数组成员

2026-10-02，在 `f6d8da4` 上实施；实际导入代码 SHA 见每个运行的 loaded.json。

已按 Slang elaborated 类型投影 packed 数组元素和 struct/union 字段；精确别名优先于末尾位选。支持升降序、非零/负下标、嵌套 packed 数组；每个 aggregate 最多 1024 个别名、65536 个映射 bit，超限保留 gap。未改动 unpacked/dynamic 数组语义，未删除任何既有 coverage gap。projector/query 版本递增以隔离旧缓存。

验证：`tests-retry.txt` 记录 246 passed，包括五项真实 Slang 类型回归及现有查询、契约、公共路由回归。首次命令指定不存在的 test_source_graph_worker.py，未执行测试，原日志保存在 tests.txt。

新 MCP 真实复测（工作流和调用原文分别在 x1/x2/x3/calls.jsonl）：

| 运行 | int_master_req[4].wdata | dma_write_resp_o[0].gnt | DMA generate 状态 |
| --- | --- | --- | --- |
| X1 | resolved，system_bus.sv:132，32 bit | resolved，:165，1 bit | unsupported |
| X2 | resolved，:131，32 bit | resolved，:160，1 bit | unsupported |
| X3 | resolved，:131，32 bit | resolved，:159，1 bit | unsupported |

以上是来源/选位修复证据，不是独占驱动或全设计正确结论：所有 positive 回执仍有 `search_exhaustive=false` 和语义缺口。C2 的 XOR、C4 的常量右值仍需后续表达式/真实双侧取证验收；当前阶段不将整项 P0-BINDING 标为收敛。X3 附一周期前后相位取值；其他运行在下一阶段使用同一脚本复测完整双侧值。

资源：X1 总线冷建 IR 2372260 bytes、worker RSS peak 221800 KiB，单次构建 2.13 s；这是类型别名增加后的成本记录，不是加速声明。无重仿真、外部 RTL 修改或新 KDB。源码是否为历史 waveform 的精确编译版本仍未独立证明。
