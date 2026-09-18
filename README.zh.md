# 🐙 TraceWeave

<!-- mcp-name: io.github.gokeshenzhen/traceweave -->

<p align="right">
  <a href="README.md">English</a> · <strong>简体中文</strong>
</p>

<p align="center">
  <img src="assets/logo.png" alt="TraceWeave" width="160">
</p>

<p align="center">
  <strong>证据链驱动的仿真调试 MCP 服务器</strong>
</p>

<p align="center">
  <a href="https://github.com/gokeshenzhen/TraceWeave/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/gokeshenzhen/TraceWeave/ci.yml?branch=main&style=for-the-badge" alt="CI status"></a>
  <a href="https://pypi.org/project/traceweave-mcp/"><img src="https://img.shields.io/pypi/v/traceweave-mcp?style=for-the-badge" alt="PyPI version"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg?style=for-the-badge" alt="MIT License"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.11%2B-blue?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.11+"></a>
  <a href="https://github.com/gokeshenzhen/TraceWeave/stargazers"><img src="https://img.shields.io/github/stars/gokeshenzhen/TraceWeave?style=for-the-badge" alt="Stars"></a>
</p>

TraceWeave 是面向 RTL / SoC 调试的 MCP 服务器。它把编译记录、仿真日志、VCD/FSDB 波形和 RTL 源码接入 AI 助手，帮助你从“仿真失败了”逐步定位到异常时刻、相关信号和驱动逻辑。

支持 Claude Code、Codex、Copilot 等 MCP 客户端。你用自然语言描述问题，助手通过工具查证、追踪和验证，返回可以复查的调试证据。

[适用场景](#适用场景) · [安装](#安装) · [客户端配置](#客户端配置) · [开始调试](#开始调试) · [工具速查](#工具速查) · [常见问题](#常见问题) · [文档与反馈](#文档与反馈)

<p align="center">
  <img src="assets/onepage.png" alt="TraceWeave 从日志、波形到源码追踪的调试工作流" width="900">
</p>

## 适用场景

| 遇到的问题 | TraceWeave 能帮助你做什么 |
|---|---|
| 仿真超时、卡死，不知道从哪里查起 | 汇总失败日志，扫描多接口握手行为，缩小异常接口和时间窗口 |
| Scoreboard mismatch、两次运行结果不同 | 对比失败记录与波形，定位已观测到的差异，并追踪两侧数据和控制来源 |
| 信号出现 X/Z | 查看异常前后的波形，沿驱动关系追踪未知值的传播路径 |
| 怀疑 tie 值、悬空输入或 magic word 条件 | 扫描源码结构，提取常量连接、未连接输入和常量比较等线索，供进一步核查 |
| SoC 层级深、模块和接口多 | 按需浏览层次、查找实例与源码，追踪信号的驱动、消费者和连通路径 |
| 想验证一个调试假设 | 按周期采样，检查时序条件、握手保持和事务完成情况，取得具体证据 |

支持 VCS / Xcelium 仿真日志及 VCD / FSDB 波形。结构查询可使用无需商业 license 的 Source Graph，也可接入 Verdi NPI。已有 formal 导出波形同样可查询；当前支持自动发现 JasperGold 产物。

## 安装

需要 **Python 3.11+**。按你的工作环境选择安装方式：

| 安装方式 | 适用场景 |
|---|---|
| 仓库安装 | 在已有 Verdi 环境的仿真主机上使用 FSDB、Source Graph，以及可选的 NPI / LSF |
| PyPI 安装 | 分析日志、VCD，或使用无需商业 license 的 Source Graph |

### 仓库安装

```bash
git clone https://github.com/gokeshenzhen/TraceWeave.git
cd TraceWeave
export VERDI_HOME=/path/to/verdi
bash scripts/install.sh
```

安装器准备 Python 环境、Source Graph 和 FSDB 读取组件，并检查运行环境。它不会修改 shell 启动文件或 MCP 客户端配置；NPI 仍需站点提供相应的运行环境和 license。

已有安装可先执行只读检查：

```bash
bash scripts/install.sh --check
```

### PyPI 安装

```bash
python3.11 -m pip install "traceweave-mcp[source-graph]"
traceweave-mcp --doctor
```

如果只需要日志、VCD 和基础静态分析，可安装 `traceweave-mcp`，省略 `[source-graph]`。PyPI 包不包含 FSDB 读取组件；需要 FSDB 时请选择仓库安装。

## 客户端配置

仓库安装后，可生成带绝对路径的客户端配置模板：

| 客户端 | 生成配置模板 |
|---|---|
| Claude Code | `bash scripts/install.sh --print-config claude` |
| Codex | `bash scripts/install.sh --print-config codex` |
| Copilot | `bash scripts/install.sh --print-config copilot` |

命令只打印模板。将输出添加到对应客户端的 MCP 配置中，补充所需的站点 EDA 环境变量，然后重新连接服务器。完整示例见[客户端配置说明](docs/architecture.md#client-configuration-reference)。

其他支持 stdio 的 MCP 客户端也可使用以下连接参数：

| 安装方式 | `command` | `args` |
|---|---|---|
| 仓库安装 | `<仓库绝对路径>/.venv/bin/python` | `["<仓库绝对路径>/server.py"]` |
| PyPI 安装 | `traceweave-mcp`，或该命令的绝对路径 | `[]` |

### 仅执行节点可用的 NPI License

如果 Verdi/NPI license 只能在 LSF 执行节点使用，让 MCP 服务进程获得以下设置，将 `digital` 换成你的队列：

```bash
export TRACEWEAVE_NPI_EXECUTION=lsf
export TRACEWEAVE_NPI_LSF_QUEUE="digital"
```

客户端需要继承或显式传入这些变量；相关工程文件与 TraceWeave 安装、缓存目录须在提交节点和执行节点以相同绝对路径可见。完整的 bash / tcsh、客户端环境转发和验证方法见 [LSF 配置说明](docs/architecture.md#lsf-only-npi-licenses)。

## 开始调试

连接客户端后，可以直接这样提问，把路径换成你的工程目录：

> 请用 TraceWeave 分析 `/path/to/verif` 下 `my_case` 的仿真失败。先查看日志、设计结构和接口握手，再追踪可疑信号；结论请附上时间点、信号和源码依据。

也可以从一个具体问题开始：

> 比较这两份波形中 `tb.dut.result` 的差异，并追踪两侧产生差异的逻辑。

> 用 deep 模式扫描这份编译日志对应的设计，检查可疑的 tie 值、悬空输入和常量比较条件。

助手的默认调查流程是：

1. **找到本次运行的产物**：发现编译日志、仿真日志与波形。
2. **建立设计上下文**：使用同一份编译日志，并行构建层次与扫描结构风险。
3. **缩小问题范围**：解析失败；对于有波形的失败运行，扫描握手异常。
4. **追踪并验证**：查看相关信号、驱动和消费者，用时间窗或周期检查验证假设。
5. **对比修复结果**：比较下一次运行的失败记录和波形变化。

首次连接时，可先要求助手调用 `get_sim_paths`，确认客户端能够执行真实的 MCP 工具调用。完整流程见[调试工作流](docs/workflow.md)。

## 工具速查

通常只需描述调试目标，由助手选择工具。下表按用途列出全部工具；具体参数由 MCP 工具定义提供。

| 类别 | 工具名 | 说明 |
|---|---|---|
| 会话与产物 | `get_sim_paths` | 发现仿真 case、编译日志、运行日志与波形 |
| 会话与产物 | `get_formal_paths` | 发现 formal 工程、日志及导出的波形，当前支持 JasperGold |
| 会话与产物 | `get_diagnostic_snapshot` | 查看当前已收集的调试信息与待完成步骤 |
| 层次与源码 | `build_tb_hierarchy` | 根据编译记录建立 RTL / testbench 层次视图 |
| 层次与源码 | `get_tb_subtree` | 查看指定实例的局部层次 |
| 层次与源码 | `find_tb_instance` | 按路径或模块名查找实例 |
| 层次与源码 | `lookup_tb_files` | 在实际编译文件集中查找源码 |
| 层次与源码 | `get_tb_file_detail` | 查看源码文件定义的模块、接口和类 |
| 层次与源码 | `get_tb_class_hierarchy` | 查看 UVM / SystemVerilog 类继承关系 |
| 层次与源码 | `dump_tb_section` | 获取指定部分的完整层次分析数据 |
| 日志与失败 | `parse_sim_log` | 将运行失败归类，提取时间与错误摘要 |
| 日志与失败 | `get_error_context` | 查看错误附近的原始日志 |
| 日志与失败 | `diff_sim_failure_results` | 比较两次运行中新增、持续和已消失的失败 |
| 日志与失败 | `analyze_failures` | 汇总某组失败的日志与波形上下文 |
| 日志与失败 | `analyze_failure_event` | 从一个失败事件定位相关实例、信号与源码候选 |
| 日志与失败 | `recommend_failure_debug_next_steps` | 推荐下一步值得调查的目标与工具调用 |
| 结构与追踪 | `scan_structural_risks` | 扫描可疑结构；语义模式可检查 tie、悬空输入与常量比较 |
| 结构与追踪 | `explain_signal_driver` | 追踪信号的驱动来源与相关 RTL |
| 结构与追踪 | `find_signal_loads` | 查找信号的消费者与影响范围 |
| 结构与追踪 | `trace_signal_path` | 查询两个信号之间的结构连通路径 |
| 结构与追踪 | `trace_x_source` | 沿上游驱动追踪 X/Z 的传播来源 |
| 波形查询 | `get_waveform_summary` | 查看波形格式、时长、时间刻度和顶层模块 |
| 波形查询 | `search_signals` | 根据名称查找完整信号路径 |
| 波形查询 | `get_signal_at_time` | 读取指定时刻的信号值 |
| 波形查询 | `get_signal_transitions` | 查看时间窗口内的信号跳变 |
| 波形查询 | `get_signals_around_time` | 查看某时刻前后多个信号的变化 |
| 波形查询 | `get_signals_by_cycle` | 按时钟沿逐周期采样多个信号 |
| 差异与时序 | `diff_first_divergence` | 查找两个信号首次观测到的已知值差异 |
| 差异与时序 | `trace_divergence` | 验证波形差异，追踪两侧相关的数据、控制与历史状态 |
| 差异与时序 | `period` | 检查信号周期与节拍异常 |
| 差异与时序 | `verify_window` | 在波形窗口中验证时序条件，返回具体证据 |
| 协议与事务 | `suggest_handshakes` | 发现 valid/ready 接口及检查所需的信号组合 |
| 协议与事务 | `suggest_protocol_bundles` | 发现 AHB / APB 接口信号组合 |
| 协议与事务 | `sweep_handshakes` | 扫描全设计中发现的 valid/ready 与 AHB 接口，汇总异常 |
| 协议与事务 | `inspect_handshake` | 检查指定接口的停顿、保持和握手异常 |
| 协议与事务 | `reconstruct_transactions` | 重建请求与响应事务，查看延迟、未完成请求和顺序 |
| 时间游标 | `cursor_set` | 为关键时刻命名，便于后续查询复用 |
| 时间游标 | `cursor_list` | 列出当前会话的时间游标 |
| 时间游标 | `cursor_delete` | 删除时间游标 |
| EDA 集成 | `build_kdb` | 从编译记录构建并缓存 Verdi KDB，供 NPI 查询使用 |

## 常见问题

**没有商业 license 也能用吗？**

日志分析、VCD 查询和 Source Graph 不需要商业 license。FSDB 读取需要本地 Verdi 读取库；Verdi NPI 和 KDB 构建需要相应的 EDA 环境与 license。NPI 不可用时，结构查询会尝试 Source Graph，再按支持范围回退到基础静态分析。

**结构扫描的 auto / deep 怎么选？**

默认 `auto` 执行静态扫描，并复用已有的适用语义结果；需要新做语义检查时，告诉助手使用 `deep`。这是每次调用的 `analysis_mode` 参数，无需修改客户端配置。扫描发现的是待核查线索，正常 tie-off 或协议常量也可能被列出。

**扫描没有发现，就说明设计没有问题吗？**

结论取决于实际覆盖范围。缺少信号、超出资源预算或不支持的结构都会限制分析；零覆盖或部分覆盖时，零发现不代表设计无问题。工具会返回覆盖与截断信息，供助手判断是否需要缩小范围继续检查。

**能分析 formal 波形吗？**

可以查询已导出的 VCD / FSDB，并自动发现 JasperGold 产物。Property 结果、反例类型和可达性语义仍需由 formal 工具或用户提供。

**设计数据在哪里处理？**

文件解析与 EDA 查询在本地或配置的 LSF 执行节点完成；返回的调试证据会进入所用 AI 客户端的上下文。TraceWeave 的使用遥测默认关闭，启用后仅写入本地文件。

## 文档与反馈

| 想进一步了解 | 入口 |
|---|---|
| 标准调试步骤与工具配合 | [调试工作流](docs/workflow.md) |
| 如何用证据验证根因 | [调试准则](docs/debug-discipline.md) |
| 架构、后端能力与资源边界 | [架构文档](docs/architecture.md) |
| 完整环境配置与高级选项 | [配置参考](docs/architecture.md#client-configuration-reference) |
| 报告问题或提出建议 | [GitHub Issues](https://github.com/gokeshenzhen/TraceWeave/issues) |

参与开发前请阅读 [AGENTS.md](AGENTS.md)。在准备好测试依赖的环境中，可于仓库根目录运行 `python3.11 -m pytest`。

TraceWeave 使用 [MIT License](LICENSE)。

## 微信

关注微信公众号，了解项目进展与调试实践：

<p align="center">
  <img src="assets/QR.png" alt="微信公众号二维码" width="200">
</p>
