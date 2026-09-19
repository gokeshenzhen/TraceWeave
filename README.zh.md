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
| 怀疑 tie 值、悬空输入或 magic word 条件 | 静态扫描源码，提取常量连接、未连接输入和常量比较等线索，供进一步核查；无需运行仿真或提供波形 |
| SoC 层级深、模块和接口多 | 按需浏览层次、查找实例与源码，追踪信号的驱动、消费者和连通路径 |
| 想验证一个调试假设 | 按周期采样，检查时序条件、握手保持和事务完成情况，取得具体证据 |

支持 VCS / Xcelium 仿真日志及 VCD / FSDB 波形。已有 formal 导出波形同样可查询；当前支持自动发现 JasperGold 产物。

信号追踪（驱动、负载与连通路径查询）默认采用 **Verdi NPI → Source Graph → 基础静态分析（Legacy Static）** 三级路由：优先查询已展开的 KDB；NPI 不可用或无法提供可信结果时，尝试免商业 license 的 Source Graph，必要时再按支持范围回退到基础静态分析。

面向大型设计的能力与已验证的部分规模：

- **层次与源码按需浏览**：服务端建立并保留层次和文件索引，助手按实例、子树或文件获取局部结果，减少大型 SoC 的上下文开销。已用 **50,500 个逻辑实例**的合成设计验证层次构建与局部查询；初次构建仍需扫描编译记录和源码。
- **批量握手检查**：`sweep_handshakes` 自动发现 AHB / valid-ready 接口，检查停顿、数据/控制保持和 valid/HTRANS 提前撤销等行为。已有 **78,817 个总信号、2.59 ms 波形时长**的设计记录：发现 49 个候选接口，涉及 262 个时钟/协议信号，约 **4.65 分钟**检查了 35 个接口；另有 14 个跳过，属于部分覆盖。实际耗时和覆盖范围取决于波形、接口类型与资源限制。
- **GiB 级日志解析**：`parse_sim_log` 已通过 **1 GiB、2,097,152 行**合成日志验证，识别了分布在文件开头、中间和末尾的全部 4 条报错。

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

### 自定义运行期报错格式

`parse_sim_log` 已内置标准 `UVM_ERROR` / `UVM_FATAL` 和 VCS / Xcelium 断言失败的解析，并提供通用 `ERROR` 匹配。对于项目自定义的 checker、scoreboard 或 `$display` 输出，可以在 [custom_patterns.yaml](custom_patterns.yaml) 中添加 Python 正则表达式，无需修改 Python 代码。日志不必包含 `UVM_ERROR`，甚至不必包含 `ERROR`。

如果日志使用同一个标签，后面的内容每次不同，只匹配标签就够了：

```text
MY_CHECK_FAIL @ 12.5 ns expected=0x12 actual=0x34
MY_CHECK_FAIL @ 20 ns timeout waiting for response
```

将默认的 `patterns: []` 替换为以下内容；如果已有规则，在原来的 `patterns` 列表中追加即可：

```yaml
patterns:
  - name: my_checker
    severity: ERROR
    regex: '^MY_CHECK_FAIL'
```

`^` 表示行首；标签之后可以是不同的内容，无需继续写正则。如果标签前面还有时间戳或其他前缀，改用 `regex: 'MY_CHECK_FAIL'` 即可在整行中查找标签。
- `name` 标识失败分组。`severity` 默认为 `ERROR`，也支持 `FATAL` 和 `WARNING`；匹配到的自定义 warning 也会计入运行期失败统计。`description` 是供维护者阅读的说明。
- `regex` 逐行匹配，建议用 YAML 单引号保留反斜杠。解析顺序是内置断言和 UVM 格式、自定义规则、通用 `ERROR` 匹配；自定义规则按列表顺序取第一条命中。已识别为编译或展开诊断的记录仍会被过滤。

上面的 `expected=0x12 actual=0x34` 已经能自动识别，用简单的标签规则就够了，无需命名捕获组。如果日志改用自己的字段名：

```text
MY_CHECK_FAIL @ 12.5 ns want=0x12 have=0x34
```

`regex: '^MY_CHECK_FAIL'` 仍能识别报错并保留整行消息。如果还希望把 `want` 和 `have` 的值单独提取为期望值和实际值，将上面规则中的 `regex` 替换为：

```yaml
regex: '^MY_CHECK_FAIL.*want=(?P<expected>\S+)\s+have=(?P<actual>\S+)'
```

仓库安装默认读取根目录的 `custom_patterns.yaml`。如果希望单独维护项目规则，或使用 PyPI 安装，将上面的 YAML 保存为自己的配置文件，并向 MCP 服务进程传入其绝对路径：

```bash
export TRACEWEAVE_CUSTOM_PATTERNS_FILE="/absolute/path/to/custom_patterns.yaml"
```

该设置会替换默认的自定义规则文件，内置格式继续生效。客户端需要继承或显式传入这个变量；更改变量后重启或重新连接服务，再解析日志。只修改已选中的 YAML 内容时，下次调用 `parse_sim_log` 就会重新加载。

## 工具速查

通常只需描述调试目标，由助手选择工具。下表按用途列出全部工具；具体参数由 MCP 工具定义提供。

<table>
  <thead>
    <tr>
      <th>类别</th>
      <th>工具名</th>
      <th>说明</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td rowspan="3">会话与产物</td>
      <td><code>get_sim_paths</code></td>
      <td>发现仿真 case、编译日志、运行日志与波形</td>
    </tr>
    <tr>
      <td><code>get_formal_paths</code></td>
      <td>发现 formal 工程、日志及导出的波形，当前支持 JasperGold</td>
    </tr>
    <tr>
      <td><code>get_diagnostic_snapshot</code></td>
      <td>查看当前已收集的调试信息与待完成步骤</td>
    </tr>
    <tr>
      <td rowspan="7">层次与源码</td>
      <td><code>build_tb_hierarchy</code></td>
      <td>根据编译记录建立 RTL / testbench 层次视图</td>
    </tr>
    <tr>
      <td><code>get_tb_subtree</code></td>
      <td>查看指定实例的局部层次</td>
    </tr>
    <tr>
      <td><code>find_tb_instance</code></td>
      <td>按路径或模块名查找实例</td>
    </tr>
    <tr>
      <td><code>lookup_tb_files</code></td>
      <td>在实际编译文件集中查找源码</td>
    </tr>
    <tr>
      <td><code>get_tb_file_detail</code></td>
      <td>查看源码文件定义的模块、接口和类</td>
    </tr>
    <tr>
      <td><code>get_tb_class_hierarchy</code></td>
      <td>查看 UVM / SystemVerilog 类继承关系</td>
    </tr>
    <tr>
      <td><code>dump_tb_section</code></td>
      <td>获取指定部分的完整层次分析数据</td>
    </tr>
    <tr>
      <td rowspan="6">日志与失败</td>
      <td><code>parse_sim_log</code></td>
      <td>将运行失败归类，提取时间与错误摘要</td>
    </tr>
    <tr>
      <td><code>get_error_context</code></td>
      <td>查看错误附近的原始日志</td>
    </tr>
    <tr>
      <td><code>diff_sim_failure_results</code></td>
      <td>比较两次运行中新增、持续和已消失的失败</td>
    </tr>
    <tr>
      <td><code>analyze_failures</code></td>
      <td>汇总某组失败的日志与波形上下文</td>
    </tr>
    <tr>
      <td><code>analyze_failure_event</code></td>
      <td>从一个失败事件定位相关实例、信号与源码候选</td>
    </tr>
    <tr>
      <td><code>recommend_failure_debug_next_steps</code></td>
      <td>推荐下一步值得调查的目标与工具调用</td>
    </tr>
    <tr>
      <td>静态结构扫描</td>
      <td><code>scan_structural_risks</code></td>
      <td>静态扫描源码中的可疑结构；语义模式可检查 tie、悬空输入与常量比较</td>
    </tr>
    <tr>
      <td rowspan="4">信号追踪</td>
      <td><code>explain_signal_driver</code></td>
      <td>追踪信号的驱动来源与相关 RTL</td>
    </tr>
    <tr>
      <td><code>find_signal_loads</code></td>
      <td>查找信号的消费者与影响范围</td>
    </tr>
    <tr>
      <td><code>trace_signal_path</code></td>
      <td>查询两个信号之间的结构连通路径</td>
    </tr>
    <tr>
      <td><code>trace_x_source</code></td>
      <td>沿上游驱动追踪 X/Z 的传播来源</td>
    </tr>
    <tr>
      <td rowspan="6">波形查询</td>
      <td><code>get_waveform_summary</code></td>
      <td>查看波形格式、时长、时间刻度和顶层模块</td>
    </tr>
    <tr>
      <td><code>search_signals</code></td>
      <td>根据名称查找完整信号路径</td>
    </tr>
    <tr>
      <td><code>get_signal_at_time</code></td>
      <td>读取指定时刻的信号值</td>
    </tr>
    <tr>
      <td><code>get_signal_transitions</code></td>
      <td>查看时间窗口内的信号跳变</td>
    </tr>
    <tr>
      <td><code>get_signals_around_time</code></td>
      <td>查看某时刻前后多个信号的变化</td>
    </tr>
    <tr>
      <td><code>get_signals_by_cycle</code></td>
      <td>按时钟沿逐周期采样多个信号</td>
    </tr>
    <tr>
      <td rowspan="4">差异与时序</td>
      <td><code>diff_first_divergence</code></td>
      <td>查找两个信号首次观测到的已知值差异</td>
    </tr>
    <tr>
      <td><code>trace_divergence</code></td>
      <td>验证波形差异，追踪两侧相关的数据、控制与历史状态</td>
    </tr>
    <tr>
      <td><code>period</code></td>
      <td>检查信号周期与节拍异常</td>
    </tr>
    <tr>
      <td><code>verify_window</code></td>
      <td>在波形窗口中验证时序条件，返回具体证据</td>
    </tr>
    <tr>
      <td rowspan="5">协议与事务</td>
      <td><code>suggest_handshakes</code></td>
      <td>发现 valid/ready 接口及检查所需的信号组合</td>
    </tr>
    <tr>
      <td><code>suggest_protocol_bundles</code></td>
      <td>发现 AHB / APB 接口信号组合</td>
    </tr>
    <tr>
      <td><code>sweep_handshakes</code></td>
      <td>扫描全设计中发现的 valid/ready 与 AHB 接口，汇总异常</td>
    </tr>
    <tr>
      <td><code>inspect_handshake</code></td>
      <td>检查指定接口的停顿、保持和握手异常</td>
    </tr>
    <tr>
      <td><code>reconstruct_transactions</code></td>
      <td>重建请求与响应事务，查看延迟、未完成请求和顺序</td>
    </tr>
    <tr>
      <td rowspan="3">时间游标</td>
      <td><code>cursor_set</code></td>
      <td>为关键时刻命名，便于后续查询复用</td>
    </tr>
    <tr>
      <td><code>cursor_list</code></td>
      <td>列出当前会话的时间游标</td>
    </tr>
    <tr>
      <td><code>cursor_delete</code></td>
      <td>删除时间游标</td>
    </tr>
    <tr>
      <td>EDA 集成</td>
      <td><code>build_kdb</code></td>
      <td>从编译记录构建并缓存 Verdi KDB，供 NPI 查询使用</td>
    </tr>
  </tbody>
</table>

## 常见问题

**没有商业 license 也能用吗？**

日志分析、VCD 查询、静态结构扫描和 Source Graph 不需要商业 license。**直接查询已有波形中的信号值或跳变，不需要 NPI license**：VCD 使用内置解析器，FSDB 使用本地 Verdi FSDB Reader 库和 wrapper。Verdi NPI 信号追踪和 KDB 构建需要相应的 EDA 环境与 license。

**信号追踪结果的准确性如何判断？**

NPI 基于与当前设计匹配的已展开 KDB；Source Graph 从源码构建语义连接图。在编译上下文完整、目标语义受支持，且查询满足**覆盖完整、解析精确、未截断**时，返回结果可作为**当前查询范围内的精确结构连接事实**，用于驱动、负载和连通路径分析。

**没有仿真结果，也能做静态结构扫描吗？**

可以。`scan_structural_risks` 对源码做静态分析，无需运行仿真，也不需要仿真运行日志或 VCD / FSDB 波形。当前接口需要提供编译/展开日志（`compile_log`），并保证对应的源码和 include 文件可访问，因此可以在编译/展开之后、仿真运行之前使用。

**静态结构扫描的 auto / deep 怎么选？**

默认 `auto` 执行源码文本规则扫描，并复用已有的适用语义结果；需要新做常量连接、未连接输入等语义检查时，告诉助手使用 `deep`。这是每次调用的 `analysis_mode` 参数，无需修改客户端配置。扫描发现的是待核查线索，正常 tie-off 或协议常量也可能被列出。

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
