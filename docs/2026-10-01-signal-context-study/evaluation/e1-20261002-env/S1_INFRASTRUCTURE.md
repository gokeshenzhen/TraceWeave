# S1 模型容量失败与完整配对重试

S1-1-A 在 116.133 秒内完成源码定位，actual_backend=verdi_npi，未展开依赖或波形取值。S1-1-B 在 126.618 秒以 exit_code=1 退出；原始 transcript 的 `error` 和 `turn.failed` 均为：`Selected model is at capacity. Please try a different model.` 此时只完成 discovery/hierarchy/scan/log，尚未请求 driver。

这是外部模型服务失败；原始配对完整保留，不作为 B 默认行为失败，也不混入核心 30 个预算结果。按冻结规则，在 [单独目录](../e1-s1-retry-20261002/manifest.json) 重跑完整 S1 A/B 配对，模型仍为 gpt-6-astra / max，提示、产品、schema、300 秒预算和缓存条件不变。不会因为结果改用其他模型或延长预算；若容量持续不足，如实保留未完成。
