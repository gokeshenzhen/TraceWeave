# P0-BINDING：受限 generate 目标

2026-10-02，在 `e51af86` 上实施。每个新 MCP 的 loaded.json 保存导入模块 SHA，calls.jsonl 保存工作流、driver 与双侧采样原文。

按 lexical 缺口，仅对目标父路径做有界 Slang lookup；保留 proved_ancestor_chains，新增独立 elaboration_candidates（最多 64 个、每个深度 64），进入 artifact identity。只有 Slang 的真实 Instance 可进入 IR；没有 sibling 枚举。driver/load/path/trace 的规划和 trace scope guard 共用此规则。未解析候选继续有 gap，候选不能通过路径前缀复用另一实例。

首次实现被已有 artifact 契约拒绝，日志 tests.txt、x1/ 保留。随后显式扩展内部契约，版本 build/worker 3.3、artifact 1.2、adapter 3.14，避免把候选伪装成 lexical proof。最终 tests-final.txt：294 passed（类型/身份、实际 Slang generate、路由、重启、缓存、取消和 worker 回归）。

新 MCP：x1-retry、x2、x3 的三个目标均 resolved；DMA `dma_i_gen[0].dma_i.dma_state_q` 定位各运行 dma.sv:400，直接候选为 clk_cg/dma_state_d/rst_ni。总线目标继续定位各自源码，无同名 channel 误配。仍不宣称 exclusive_driver_proved；真实语义缺口均保留。

bus-row-checks.json：944175000 ps 的 before 样本，X1 写数据 16→17、grant 1→1；X2 写数据 16→16、grant 1→0；X3 写数据 16→16、grant 1→1。after 采用 +1 ps 并独立保存。X1 的 handshake sweep 仍 degraded，0 flags 不代表全局协议干净。历史 source→wave 身份未独立证明。

这一步完成 generate/对象可达性；共享 wave/design 绑定的更多入口、以及 C2/C4 的选位表达式证据仍待后续验收。此报告不作为 P1 或模型收益证据。
