# 独立模型 NPI 环境预检

临时 CLI 的 MCP 配置显式转发父进程已有的五项 EDA 变量名；没有修改用户配置、变量值或产品代码。

实际 GPT-6-Astra / max 独立任务正常退出，耗时 122.392 秒，真实 input 218,509、output 1,970（reasoning 657 已包含在 output 中）。6 个 MCP 调用覆盖 discovery、并行 hierarchy/scan、log 和真实 driver。

`runs/pilot-1-B/calls.jsonl` 第 6 次查询确认 `actual_backend=verdi_npi`。依赖包得到 clk_i（clock）、rst_ni（native set pin / active_value=0）、tx_d（data），三者均绑定；保留异步赋值与时序上下文缺口。这是基础设施和功能预检，不分析 C1 波形行为、不计入正式 A/B 收益。完整 prompt、CLI、schema、加载指纹、调用与 usage 均已保存。
