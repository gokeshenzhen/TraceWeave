# 中断的首次 E1：MCP 环境缺失

此批次保留为失败实验，不用于增强收敛。冻结产品与共同 P0 没有变化；任务和 300 秒预算没有因为结果而调整。

实际启动 3 个独立 run：C1-1-A、C1-1-B、C2-1-A。前两个达到进程 wall-time 上限，第三个因发现基础设施问题而中断。原始状态见 runs.jsonl，原始调用和模型事件保留在 runs/。冻结 runner 副本保存于 runner-snapshot/，指纹对应 manifest.json。

- C1-1-A：17 个 MCP 调用，模型完成事件和答案存在，真实总 token 为 547,547；进程仍达到 300 秒上限。答案正确区分 UART 前后相位，但没有 driver/NPI 调用，不能证明 NPI 增强收益。完成事件与进程退出之间的具体原因没有足够时间戳证据，保留 timeout。
- C1-1-B：20 个 MCP 调用，未产生模型完成事件，真实总 token 缺失。第 15 次调用启用了依赖包，却得到 Source Graph，回执明确记录 NPI 尝试失败 `npi_load_failed`；这不满足 C1 的真实 NPI 前提。已有相位取证只是部分进展，不能作为完整任务成功。
- C2-1-A：基础设施中断，不能评分为软件超时或成功；保留中断时的 transcript。

进程环境检查（infrastructure-processes.json）发现，父 runner 有 VERDI_HOME、NOVAS_HOME、LD_LIBRARY_PATH、SNPSLMD_LICENSE_FILE、LM_LICENSE_FILE，而独立 Codex 启动的 MCP 五项均缺失。只记录变量存在与否，没有记录 license 值。先前基础设施 pilot 只测 diagnostic，未覆盖真实 NPI 加载，未能发现此问题。

修复限于临时 CLI 的 MCP `env_vars` 转发；不编辑客户端配置、产品代码、预算或任务。接下来用独立模型执行真实 NPI 查询预检，成功后重新冻结并重跑完整配对。此次失败不能被静默删除，也不将已观察到的软件超时改写成成功。Source Graph 的结果不替代 NPI 验收。
