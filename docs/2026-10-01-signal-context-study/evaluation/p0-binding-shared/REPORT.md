# P0-BINDING：共享绑定与选位证据验收

2026-10-02，基于 `5be9bd6`。P0-BINDING 在当前五案例所需的有界范围完成确定性验收；这不是完整 SystemVerilog 支持声明。

修改：constant packed selections 保留语义位映射；单个字段查询从同一 assignment 的 RHS 中选取对应位，避免以整个 aggregate 代替字段。wave_design_binding 使用同一 IR 的 packed aliases 在完整 aggregate dump / 展开字段 dump 间绑定，验证范围、storage identity 并拒绝歧义。没有 suffix 搜索、无条件 TOP 删除或去除真实 gap。

根绑定复用于 driver、显式 wave_path 的 load/path、snapshot X trace、history/divergence 动态路由和 packed-field 入口。动态路由用 design 名称访问单个语义 artifact，观察阶段用准确 wave 声明；改变 waveform identity 后不得继续使用该绑定。packed-field 仍保持原有严格类型 coverage 门槛；未 dump aggregate 且没有可唯一映射的单字段时仍返回缺口。

验证：tests-final.txt 记录 **361 passed**，覆盖类型/别名、共享动态路由、文件身份变化、X history/snapshot、divergence、公共路由、server 锁/取消。首次回归因新增 schema 的 Optional 未导入而失败，已修正为项目惯用的 `| None`，tests.txt 保留；tests-retry.txt 为随后 360 passed。

新 MCP `x1/calls.jsonl`：driver 三目标 resolved；load 返回实际 Source Graph 消费事实；路径查询 found；snapshot 观察状态为非 X。工作流 scan lexical complete、semantic not_run，sweep degraded；没有以 0 flagged 宣称干净。

内部功能探针（probe_selected.py，**不是模型评测**）也从新进程执行 discovery/hierarchy+scan/log/sweep。selected-x1/selected.json 保存 C2 的目标 32-bit RHS、XOR 与常量 1、真实字段绑定和取值：944174999 ps 由上游 16 求得 17，与新 MCP before 样本一致。selected-x2/selected.json 保存 C4 的目标 gnt RHS 为常量 0，没有读入其他字段作为 grant 依赖；该运行波形 grant 两侧 1→0。X3 的正常 16→16 / 1→1 与 generate driver 证据见上一阶段。

所有内部观测仍包含 driver_set_incomplete，当前 source 与历史 waveform 的精确编译身份仍未独立证明。不外推独占驱动、全局协议或所有波形名称形态。未重仿真、未修改外部 RTL、未创建 KDB。

冻结原则：后续 E1 A/B 两边必须包含此提交及全部已验收 P0；本文采样/路径成功均不计入依赖返回增强的收益。
