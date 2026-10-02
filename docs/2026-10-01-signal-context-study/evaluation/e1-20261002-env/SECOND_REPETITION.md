# 第二轮检查点（20 / 32 run）

第二轮按 BA 顺序实际运行 10 个独立任务，全部达到 300 秒预算，没有模型完成事件或最终 usage。保留中途正确证据，但不以此计作完整成功。第三轮和 S1 继续使用冻结预算与判据。

本轮发生了实际增强调用：

- C1-2-B 的第 10、17 次 MCP 调用均来自真实 `verdi_npi`，查询 tx_q / tx_d 的按需依赖。共返回 9 条候选、1,915 字节紧凑依赖 JSON；第一包 3 条候选随后被采样，第二包 6 条在查询前已经采样过。前后相位判断正确，但任务仍超时。这里证明实际采用，不证明质量或成本收益。
- C4-2-B 的第 14 次调用请求 `recursive=true, max_depth=3, include_dependencies=true`，目标是 generated DMA 实例下的 `dma_write_resp_i.gnt`。Source Graph 返回 `source_graph_frontier_expansion_stalled`，最后是 Static / unsupported；依赖包明确为空并标 `dynamic_evidence_unavailable`。这是已观察到的递归路径限制，不是正向 Source Graph/NPI 受益样本，也不扩大 P0 的已验收范围。随后读到了系统总线 grant 常量源码，但未完成相反侧波形验证。

其他 B 任务没有请求依赖包。本轮 C2/C3/C5 获得了一些关键数据/相位证据，但没有完整交付；C4 两组都未补齐系统总线 grant 映射双侧证据。尚无新增正常行为误判出现在已保存的中途说明里，这不等于所有正常反例已完成验收。

累计 20 个 run 仅首轮 C1 两组完整交付。输入/schema/产品指纹审计继续通过。第一轮 C4 两组还各记录一次推荐工具的 `fst_timeout`；这些工具级 deadline 属于保留结果，不作为基础设施理由重跑。尚无可验收增强收益，不声明节省 token；18 个预算超时任务的真实总 token 缺失。
