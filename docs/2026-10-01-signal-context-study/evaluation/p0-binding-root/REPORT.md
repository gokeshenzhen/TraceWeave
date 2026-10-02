# P0-BINDING：根路径绑定的中间验收

该阶段只修复 explain_signal_driver 入口的根绑定，整个 P0-BINDING 尚未收敛。

新增共用 `wave_design_binding.RootBinding`：必须有 Verilator compile context、唯一已记录 top、准确 waveform declaration 和当前 wave identity，才映射 `TOP.<top>`；真实 TOP、多 top、错误 top_hint、缺失声明和 escaped-name 歧义不改名。返回保留 wave/source 两种名称；索引和字段原样交给语义层，不猜位选，不声称历史源码同版。默认 backend 优先级没有变化，wave metadata 查询与连接查询分开持锁。

[新 MCP X1 回执](x1/calls.jsonl) 的三个目标都从 `source_graph_target_top_unresolved` 推进到 `source_graph_instance_not_in_projected_scope`。这是中间定位进展，**不是驱动解析成功**；generate / 固定数组 / packed 字段和其他调用入口的接入仍待下一阶段。最终新增原始输入 signal_path 的保留，并补合法裸 vector alias 的准确声明校验；该最终小改动由定向回归验证。

[测试](tests.txt) 覆盖根绑定边界、Source Graph 公共路由、并发和既有 dynamic binding。未安装依赖、未改 RTL、未重新仿真或新建 KDB。P1 依赖增强及模型 A/B 仍未开始。
