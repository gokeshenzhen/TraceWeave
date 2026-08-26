# 🐙 TraceWeave

<p align="right">
  <a href="README.md">English</a> · <strong>简体中文</strong>
</p>

<p align="center">
  <img src="assets/logo.png" alt="TraceWeave" width="160">
</p>

<p align="center">
  <strong>面向证据链与工作流的 RTL 仿真调试 MCP 服务器</strong>
</p>

<p align="center">
  <a href="https://github.com/gokeshenzhen/TraceWeave/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/gokeshenzhen/TraceWeave/ci.yml?branch=main&style=for-the-badge" alt="CI status"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg?style=for-the-badge" alt="MIT License"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.11%2B-blue?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.11+"></a>
  <a href="https://github.com/gokeshenzhen/TraceWeave/stargazers"><img src="https://img.shields.io/github/stars/gokeshenzhen/TraceWeave?style=for-the-badge" alt="Stars"></a>
</p>

<h2 align="center">把编译证据、仿真日志、波形、源码与展开后的连线关系串成一条可验证的调试工作流。</h2>

TraceWeave 将本地 VCS/Xcelium 仿真产物组织成一条有引导的调查路径：自动发现本次运行实际使用的编译日志、仿真日志和 VCD/FSDB 波形；构建已编译设计的层次结构和独立的结构风险视图；归一化失败事件；执行全设计运行期握手扫描；并推荐下一步可直接调用的证据采集动作。

面对 driver、load、结构路径和 X/Z 源头问题，TraceWeave 按保留证据来源的后端阶梯执行：有可用 KDB 时优先使用可信 Verdi NPI；NPI 不可用或结论不充分时使用按需、有界的 Slang Source Graph；最后才回退 Legacy Static。结果显式报告 backend provenance、coverage、truncation 和 fallback 状态，不把局部证据包装成确定结论。

<p align="center">
  <img src="assets/onepage.png" alt="TraceWeave 工作流概览" width="900">
</p>

<p align="center"><sub>默认工作流与 connectivity 路由示意图；所有结论都受输出中 coverage 范围的约束。</sub></p>

TraceWeave 是一个面向工作流的调试服务器，而不是一组零散的解析器。它包含：

- 从产物发现、并行层次/结构分析、失败解析、运行期协议扫描到聚焦验证的 MCP 引导工作流
- 基于编译证据的层次构建、handle 式按需浏览和源码感知的结构分析
- VCD/FSDB 指定时刻、跳变、时间窗、周期、首次分叉和节拍查询
- 全设计握手扫描、定向协议检查、时序谓词验证和事务重建
- 通过 `trusted NPI -> bounded Source Graph -> Legacy Static` 完成 driver/load/path/X 追踪
- 面向 MCP 客户端的结构化下一步动作，以及 coverage、provenance、truncation 和资源回执

[架构](docs/architecture.md) · [安装](#安装) · [客户端配置](#客户端配置) · [标准 MCP 工作流](#标准-mcp-工作流) · [工具速查](#工具速查) · [测试](#测试) · [微信](#微信)

## TraceWeave 在什么场景最有用

TraceWeave 的最大价值，是把分散在多个仿真产物中的证据关联起来，而不是只读取一行
显而易见的 RTL。它尤其适合：

- **不透明的运行期症状**，例如超时、卡死、scoreboard mismatch、X/Z 传播、首次分叉
  或节拍异常。波形查询和协议/事务分析可以定位第一个异常时刻、接口或 beat。
- **跨层级因果追踪**，需要沿 port、interface、assignment、driver 和 consumer 追查
  一个可疑信号的来源与影响范围。
- **大型或多接口设计**，通过全设计握手扫描以及有界的 hierarchy/Source Graph scope，
  把原本开放式的搜索收敛到可检查的范围。
- **可证伪的假设验证**，需要从 `verify_window`、首次分叉、period、handshake 或事务重建
  中取得具体 witness 或 counterexample。
- **受 license 或执行环境限制的场景**，无 NPI 时仍可用 Source Graph 获得语义连线证据；
  有可用 KDB 时则可进一步使用本地或 LSF 执行的 Verdi NPI。

对于小型可读 block 中明显的源码局部逻辑错误，直接阅读源码和日志可能更快。
TraceWeave 也无法揭示所有可用源码、日志、波形和 KDB 都没有暴露的行为；面对
protected IP，它只能沿可见边界、波形或 elaborated database 已暴露的证据继续追踪。

## 架构

- 架构地图:`docs/architecture.md`
- 新会话启动:先读 `AGENTS.md`,再按其中的 first-read 文件列表展开
- 快速理解代码的捷径:
  - `server.py`
  - `config.py`
  - `src/analyzer.py`
  - `src/log_parser.py`
  - `src/fsdb_parser.py`

## 仓库结构

```text
TraceWeave/
├── config.py                 # 环境相关常量与发现规则
├── server.py                 # MCP 入口、会话状态、工作流约束
├── custom_patterns.yaml      # 用户可扩展的日志匹配模式
├── fsdb_wrapper.cpp          # 原生 FSDB wrapper 源码
├── build_wrapper.sh          # 构建 libfsdb_wrapper.so
├── scripts/                  # setup_fsdb.sh / verify_fsdb.sh
├── tests/                    # 单元与集成测试
└── src/
    ├── path_discovery.py
    ├── compile_log_parser.py
    ├── tb_hierarchy_builder.py
    ├── vcd_parser.py
    ├── fsdb_parser.py
    ├── fsdb_signal_index.py
    ├── waveform_batch.py         # FSDB+VCD 时间窗多信号批量读取
    ├── log_parser.py
    ├── analyzer.py
    ├── signal_driver.py
    ├── signal_load.py            # Load/fanout 查找,Static + NPI
    ├── connectivity_backend.py   # ConnectivityBackend 协议 + select_backend
    ├── verdi_backend.py          # KDB / license 探测 + kdb_hint 生成
    ├── verdi_npi_backend.py      # NPI 后端实现的 driver/load/path 解析
    ├── npi_lsf.py                # 可选 LSF transport + Verdi/NPI worker 协议
    ├── npi_worker.py             # 执行节点 Verdi/NPI worker 入口
    ├── kdb_builder.py            # 为 Xcelium 流程自动构建 Verdi KDB
    ├── structural_scanner.py
    ├── x_trace.py
    ├── cycle_query.py
    ├── schemas.py
    ├── problem_hints.py
    ├── hierarchy_provider.py     # 有界 lexical/semantic instance-binding 视图
    ├── hierarchy_handles.py      # HandleStore + build_tb_hierarchy 的内容寻址 handle
    ├── handle_tools.py           # get_tb_subtree / lookup_tb_files / find_tb_instance / ...
    ├── cursor_store.py           # 命名的进程内时间锚(cursor_set/list/delete)
    ├── timespec.py               # 将 @cursor / 带单位字面量(12.34ns)解析为 ps
    ├── verify_condition.py       # diff_first_divergence、period、inspect_handshake
    ├── window_verify.py          # verify_window:时钟窗口上的时序谓词
    ├── handshake_suggest.py      # suggest_handshakes / suggest_protocol_bundles
    ├── handshake_sweep.py        # sweep_handshakes:全设计握手异常扫描
    ├── txn_reconstruct.py        # reconstruct_transactions:id 关联的事务层
    ├── cancellation.py           # worker 线程波形扫描的协作取消检查点
    └── usage_telemetry.py        # 仅本地的逐调用使用遥测(默认关闭,显式开启)
```

## 安装

TraceWeave 需要 Python `3.11+`。

推荐安装方式会包含 Source Graph 使用的固定版本 `pyslang` frontend，并把全部
Python package 放进仓库本地 `.venv`：

```bash
bash scripts/setup_source_graph.sh
```

该脚本会把 `requirements-source-graph.txt`（MCP runtime、PyYAML 和
`pyslang==11.0.0`）安装进 `.venv`。首次 clone 后运行一次；以后 pull 若改动了该
requirements 文件，再运行一次即可。脚本可重复执行，不会修改 shell 或 MCP client
配置；成功后会打印解释器绝对路径，以及可选的 Codex / Claude 注册命令。只读检查
模式不会执行安装：

```bash
bash scripts/setup_source_graph.sh --check
```

若只需要不包含 Source Graph frontend 的最小安装：

```bash
python3.11 -m pip install "mcp==1.27.0" pyyaml --user
```

要使用 FSDB,需要以下任一运行时:

- 仓库本地运行时:`third_party/verdi_runtime/linux64/libnsys.so` 与 `libnffr.so`
- 外部 Verdi 安装,通过 `VERDI_HOME/share/FsdbReader/linux64` 暴露

如果两者都不可用,TraceWeave 仍可运行,但 FSDB 解析会被禁用,工作流应优先使用 `.vcd` 波形。

启用 FSDB 支持(将 Verdi 运行时链接到仓库并构建 `libfsdb_wrapper.so`,一步完成):

```bash
# 示例 —— 请替换为你所在站点的 Verdi 安装路径
export VERDI_HOME=/path/to/verdi
bash scripts/setup_fsdb.sh
```

> **git pull 之后**:`libfsdb_wrapper.so` 是本地构建产物,不入库。若拉取的更新改动了 `fsdb_wrapper.cpp`,首次 FSDB 查询会报 *"libfsdb_wrapper.so is outdated"* —— 重跑 `bash build_wrapper.sh` 并重连 MCP server 即可。这是有意的 fail-loud 设计:过期的 wrapper 可能静默返回错位的时间戳。重建也是启用 `sweep_handshakes` 可选 FSDB transition-group 优化的必要步骤。

验证运行时与 wrapper 是否能正确加载。该脚本不依赖 `$VERDI_HOME`,在已具备仓库本地产物的任何主机上都可以运行:

```bash
bash scripts/verify_fsdb.sh
```

## 客户端配置

### 通用 MCP 客户端

任何支持 stdio 传输的 MCP 客户端都能连接本服务器。最小配置:

- command:运行 `scripts/setup_source_graph.sh` 后使用 `<TRACEWEAVE_HOME>/.venv/bin/python`（若另行管理不含 Source Graph 的最小环境，仍可使用 `python3.11`）
- args:`["<TRACEWEAVE_HOME>/server.py"]`
- env:如果需要 FSDB,提供仓库本地 `third_party/verdi_runtime/linux64` 或者 `VERDI_HOME`

如果客户端支持 server instructions,可以直接遵循内置工作流;否则参考下方手动工作流。

### Claude Code

环境变量是否进入 MCP server,取决于 MCP client 本身如何启动以及它采用的环境
转发策略。在一个从 terminal 启动的 `tcsh`/LSF 实测环境中,Claude Code 将 shell
配置的 LSF、Verdi 和 license 变量传给了 TraceWeave,无需单独维护 MCP 环境变量
清单即可完成远端 NPI driver/load/path 查询。IDE/GUI 启动或其他 client 配置不一定
继承相同环境。为了让 Claude Code 配置确定可复现,应显式列出 server 需要的变量
—— 工具根目录,以及 `dlopen` 链(最容易遗漏的是 `LD_LIBRARY_PATH`;一旦缺失,NPI
会静默回退到 Static,`trace_signal_path` 会返回 `found: false`)。

在 `~/.claude.json` 中添加:

```json
{
  "mcpServers": {
    "TraceWeave": {
      "command": "<TRACEWEAVE_HOME>/.venv/bin/python",
      "args": ["<TRACEWEAVE_HOME>/server.py"],
      "env": {
        "VERDI_HOME": "<verdi-install>",
        "NOVAS_HOME": "<verdi-install>",
        "VCS_HOME": "<vcs-install>",
        "XLM_ROOT": "<xcelium-install>",
        "CDS_INST_DIR": "<xcelium-install>",
        "SNPSLMD_LICENSE_FILE": "xxxx@s-license.example.com",
        "LM_LICENSE_FILE": "xxxx@s-license-server.example.com",
        "CDS_LICENSE_FILE": "xxxx@c-license.example.com",
        "LD_LIBRARY_PATH": "<library-path>",
        "PATH": "<path>"
      }
    }
  }
}
```

验证连接:

```bash
claude mcp list
# 应该显示 TraceWeave (connected)
```

### Codex

Codex 可以通过两种方式为 TraceWeave MCP server 提供环境变量:

- 固定值放在 `[mcp_servers.TraceWeave.env]` 中,适合工具和 license 路径稳定,
  或 Codex 不是从已配置 terminal 启动的环境。
- 通过 `env_vars` 允许并转发 Codex 父进程已经继承的变量,适合由 `.bashrc`、
  `.tcshrc` 或站点 setup 脚本统一管理的 EDA 环境。

对同一个变量请选择其中一种来源,不要同时配置在 `env` 和 `env_vars` 中。这与
[Codex 官方 MCP 配置](https://developers.openai.com/codex/mcp/)一致。下面是在
`~/.codex/config.toml` 中使用固定值的示例:

```toml
[mcp_servers.TraceWeave]
command = "<TRACEWEAVE_HOME>/.venv/bin/python"
args = ["<TRACEWEAVE_HOME>/server.py"]
cwd = "<TRACEWEAVE_HOME>"

[mcp_servers.TraceWeave.env]
VERDI_HOME = "<verdi-install>"
NOVAS_HOME = "<verdi-install>"
VCS_HOME = "<vcs-install>"
XLM_ROOT = "<xcelium-install>"
CDS_INST_DIR = "<xcelium-install>"
SNPSLMD_LICENSE_FILE = "xxxx@s-license.example.com"
LM_LICENSE_FILE = "xxxx@s-license-server.example.com"
CDS_LICENSE_FILE = "xxxx@c-license.example.com"
LD_LIBRARY_PATH = "<library-path>"
PATH = "<path>"
```

如果 EDA 环境由站点 setup 脚本管理,不要把展开后的值复制到 `env`。应从完成环境
设置的 terminal 启动 Codex,并改用下面 LSF-only 小节中的 shell 环境转发方式。

验证连接:

```bash
codex mcp list
# 应该显示 TraceWeave,Status: enabled
```

### 仅执行节点可用的 NPI License

部分 EDA 环境只允许调度到执行节点的进程获取 Verdi/NPI license。TraceWeave
默认仍在本地执行 NPI；这类环境可显式开启 LSF:

```bash
export TRACEWEAVE_NPI_EXECUTION=lsf
export TRACEWEAVE_NPI_LSF_QUEUE="digital"
```

这里的 `digital` 只是示例,请替换为该用户所属团队可获取 license 的队列。
TraceWeave 只读取命名空间明确的 `TRACEWEAVE_NPI_LSF_QUEUE`,不会创建、覆盖
或解释公司通用的 `LSF_QUEUE`。如果站点原本已经导出了 `LSF_QUEUE`,用户也可以
选择映射这个已有值:

```bash
export TRACEWEAVE_NPI_LSF_QUEUE="$LSF_QUEUE"
```

`tcsh`:

```tcsh
setenv TRACEWEAVE_NPI_EXECUTION lsf
setenv TRACEWEAVE_NPI_LSF_QUEUE "digital"
```

只有在 `LSF_QUEUE` 已经存在时,也可以写成:

```tcsh
setenv TRACEWEAVE_NPI_LSF_QUEUE "$LSF_QUEUE"
```

只有 MCP client 把对应 shell 环境传给 TraceWeave server 时,`.bashrc` /
`.tcshrc` 中的设置才会生效。在实测的 terminal 启动环境中,Claude Code 能直接
完成 LSF 上的 NPI driver/load/path 查询;Codex 则必须在 `env_vars` 中列出站点所需
变量,否则 NPI 尝试会失败。

下面的 Codex 配置适用于父 shell 已经建立好 EDA 环境的情况,它是前面 Codex 小节
中固定值 EDA 配置的替代方案。清单来自一个实际 LSF/EGO 站点;其他站点应按自己的
setup 增减变量,并且不要在 `env` 中重复同名变量:

```toml
[mcp_servers.TraceWeave]
command = "<TRACEWEAVE_HOME>/.venv/bin/python"
args = ["<TRACEWEAVE_HOME>/server.py"]
cwd = "<TRACEWEAVE_HOME>"
env_vars = [
  "TRACEWEAVE_NPI_LSF_QUEUE",

  "LSF_ENVDIR",
  "LSF_BINDIR",
  "LSF_SERVERDIR",
  "LSF_LIBDIR",
  "PATH",

  "EGO_TOP",
  "EGO_BINDIR",
  "EGO_CONFDIR",
  "EGO_ESRVDIR",
  "EGO_LIBDIR",
  "EGO_LOCAL_CONFDIR",
  "EGO_SERVERDIR",

  "VERDI_HOME",
  "LD_LIBRARY_PATH",

  "LM_LICENSE_FILE",
  "SNPSLMD_LICENSE_FILE",
]

[mcp_servers.TraceWeave.env]
TRACEWEAVE_NPI_EXECUTION = "lsf"
```

Codex 会原样复制 `[mcp_servers.TraceWeave.env]` 中的值，因此不要写
`TRACEWEAVE_NPI_LSF_QUEUE = "$LSF_QUEUE"`；转发 Codex 父进程已经继承的
且由 shell 提前展开好的变量应使用 `env_vars`。如果 Codex 父进程没有继承
shell 环境,就不要把 queue 放入 `env_vars`,而应直接在
`[mcp_servers.TraceWeave.env]` 下设置固定值
`TRACEWEAVE_NPI_LSF_QUEUE = "digital"`。如果有些 EDA 值有意固定在 `env` 中,
则应从 `env_vars` 中删除这些同名变量。

在实测的 terminal 启动 Claude Code 环境中,如果 shell 已经导出了这两个
namespaced 变量和完整站点环境,则不需要额外的 MCP 环境 map。为了获得确定可复现
的配置,或者 client 没有继承该 shell 时,可把下面两个固定值合并进已有 TraceWeave
server 的 `"env"` 对象中(`digital` 替换成用户自己的队列):

```json
{
  "TRACEWEAVE_NPI_EXECUTION": "lsf",
  "TRACEWEAVE_NPI_LSF_QUEUE": "digital"
}
```

JSON 中也是字面值,不要在这个静态 map 里写 `"$LSF_QUEUE"`。

开启后，`explain_signal_driver`、`find_signal_loads`、
`trace_signal_path`、`trace_x_source`，以及每个 `build_kdb` cache miss 或强制
rebuild，都会提交短生命周期的 `bsub -K` worker。精确 KDB cache hit、日志解析、
波形读取、结构扫描、KDB 探测与 Static 分析仍在本地执行，因为它们不会启动需要
license 的 Verdi 可执行程序。connectivity worker 失败或超时后，driver/load/path
查询先尝试本地 Source Graph，若有 blocker 或结论不充分再回退 Legacy Static；
KDB build worker 失败时则**不会**偷偷回退到本地 `vericom`/`elabcom`，而是返回固定
失败回执。Static 仍没有 path API，因此最终 path fallback 会明确返回 unsupported。路由通过固定的 `backend_status` 状态字段说明原因，
不会返回队列、主机、命令或 license 细节。

重启或重新连接 MCP server 后,让 AI agent 运行一次显式 connectivity 操作并
报告 `backend_status`。LSF NPI 成功时应看到 `execution_mode="lsf"`、
`scheduler_status="completed"`、`worker_status="completed"` 与
`actual_backend="verdi_npi"`。否则应检查 `fallback_reason`;Static fallback
不是 exact NPI 结果。

对于 Xcelium 的 KDB cache miss，`build_kdb` 会在顶层返回同样的
`execution_mode` / `scheduler_status` / `worker_status` / `fallback_reason`
字段。远端构建成功时是 `execution_mode="lsf"` 且两个 status 都为
`"completed"`；cache hit 时两个 status 都为 `"not_started"`，表示没有启动
需要 license 的进程。

带 elaboration error 标记的 KDB 也可能成功完成 worker。此时
`actual_backend="verdi_npi"` 会同时带有 `kdb_degraded=true`；应结合 NPI
attempt 的 `coverage_status="partial"` 以及 `kdb_error_count` /
`kdb_error_log` 判断，不能只凭 scheduler completed 推断 elaboration 完整。

可选配置:

```bash
export TRACEWEAVE_NPI_LSF_TIMEOUT=120
export TRACEWEAVE_NPI_LSF_KDB_TIMEOUT=1260
export TRACEWEAVE_NPI_LSF_BSUB=/path/to/bsub
export TRACEWEAVE_NPI_LSF_BKILL=/path/to/bkill
export TRACEWEAVE_NPI_LSF_PYTHON=/path/to/python3.11
export TRACEWEAVE_NPI_LSF_STAGING_DIR=/shared/private/traceweave-npi
export TRACEWEAVE_NPI_LSF_EXTRA_ARGS_JSON='["-R", "select[...]"]'
```

compile log、所有 source/include 输入、TraceWeave 安装目录、staging 目录与
`TRACEWEAVE_CACHE_DIR`（包括生成的 KDB）必须在提交节点和执行节点上以相同绝对路径
可见。远端成功后，parent 会验证返回的 KDB 路径确实可见；否则返回
`npi_lsf_artifact_unavailable`。staging 默认位于 TraceWeave cache root 下；cache
非共享时必须显式设置。`TRACEWEAVE_NPI_LSF_TIMEOUT` 控制较短的 connectivity job；
`TRACEWEAVE_NPI_LSF_KDB_TIMEOUT` 单独限制排队等待加两段 KDB 构建的总时间（默认
1260 秒）。额外调度参数采用受限的 option/value JSON argv，而不是 shell 文本。

### 按需 Source Graph

`explain_signal_driver`、`find_signal_loads`、`trace_signal_path` 与
`trace_x_source` 使用生产路由
`Verdi NPI -> Source Graph -> Legacy Static`。Source Graph 是惰性、进程内生命周期：
第一个满足条件的请求才启动隔离的短生命周期 frontend worker；成功的 scoped IR
进入 MCP server 的 bounded memory cache；同 key 的并发 cold 请求共享一次 build。
artifact identity 与 query target 分离，QueryIdentity 仍绑定准确 target。默认配置下，
它不会在启动时 build 或扫描 cache、不使用 disk persistence、不持有 FSDB/VCD wave
lock，也不会把 `pyslang` 导入 MCP server 进程。

对 `trace_x_source`，可信 NPI 结果保持 authoritative。NPI 内部 fallback 会丢弃
部分 propagation chain，并用一个 bounded Source Graph artifact 从 root 重跑。
同一 proved scope 内的多个 driver target 复用该 artifact；新 X-bearing target 若要求
扩 scope，只加入 hierarchy 精确证明的 ancestor union，并丢弃较小 artifact 的旧链后
再次从 root 重跑。build/query 失败、不安全 scope 解释或 coverage-incomplete negative
会触发整条 Static 重算；cancellation 不会继续下一个 backend。

对 path 请求，adapter 必须证明两个 hierarchy ancestor chain 属于同一个 top，只投影
两条 chain 经最低公共祖先形成的 union；不会枚举无关 sibling 或完整设计。查询只沿受支持的
IR binding 与组合依赖返回 deterministic shortest-hop 结构路径。BFS 队列只保存当前
selection，每个首次发现的 state 只保存一个 predecessor hop，命中后才回建一次路径；不会在
frontier 的每个元素里复制完整路径前缀。partial 正结果仍是 partial；只有
coverage-complete 的负结果才是 `not_connected`，inconclusive 或 truncated 负结果会继续
回退到 Static 的结构化 unsupported。`expand_assigns` 只控制是否展示真实的 IR/source
assignment evidence，不改变端点是否连通。

`build_tb_hierarchy` 读取源码与已解析 include 时，会同步捕获一份私有、不可变的
compile-session snapshot；其中只包含内容摘要、stat identity、字节数和固定标签语义
marker，绝不把源码正文写入 hierarchy 结果。当默认并行的 `build_tb_hierarchy` 与
`scan_structural_risks` 针对同一个 compile identity 重叠执行时，进程内 transient
single-flight index 让两者复用同一次物理读取及其精确 digest/stat/marker facts。该索引容量
有界，最后一个 active call 释放后立即清空全部源码正文，不进入 handle、磁盘或跨请求
handoff。Source Graph 首次请求会复用所有仍然 current 的记录而不再打开
对应文件，并在回执中标记
`fingerprint_cache_disposition=miss_reused_compile_session`；hierarchy 没有读到的 support
input 仍按原路径读取并哈希；这也包括 simulator/frontend replay 阶段才补入的工具库输入
（例如 VCS `-ntb_opts uvm` 展开的 `uvm_pkg.sv`），它们不属于工程 hierarchy 证据。
原始工程输入仍必须全部存在 current snapshot record。后续请求复用容量有界的进程内
manifest，并标记 `hit_session_snapshot`。每条复用记录都会重新校验 stat；若源码已变化，
旧 hierarchy 与 manifest 的组合会以 `compile_session_snapshot_changed` 阻断，必须先重新
编译并刷新 hierarchy。compile log 变化或 hierarchy handle 刷新也会使 snapshot 失效。

对于大型且完整的 Verilog/SystemVerilog manifest，adapter 可以直接使用该 handle 已保存的
hierarchy scan 事实推导 compile-input closure：包括已证明 ancestor definition、显式 compile
top/bind top、package import/qualified package reference，以及按编译顺序出现的宏
define/undef。Slang 仍然负责真正的解析与 elaboration；这个 planner 不是另一个编译器。
完整 ordered manifest 与全量内容 fingerprint 继续作为 artifact 的失效 identity，隔离 worker
只接收保持原顺序的 closure，并只 elaboration 当前请求的 design top。依赖证明缺失或歧义、
manifest 不完整/混合语言、重复输入、或 closure 超出预算时都会安全保留 full replay。
adapter 回执仅在 `manifest.compile_projection` 下公开固定标签和计数。任何实际裁剪都会增加
`compile_projection_pruned_inputs`，因此图的 coverage 明确为 `inconclusive`：IR 已证明的
driver/load/path 正事实仍可使用，但空结果绝不能证明 `no_driver`、`no_load` 或
`not_connected`。

对于深层 recursive driver 查询，或显式深度大于 1 的 load 查询，大型 manifest 的首个
projection 可以在第一次构建时纳入目标叶实例的相邻 sibling，避免先执行一次必然过窄的查询
再重建。这是有界准入策略，不是通用 subtree 展开：parent 与全部 direct child 都必须由
hierarchy 证明；最多新增 32 个实例和 24 个 closure input；base closure 达到 32 个 input
后，输入增长不得超过 25%；同时仍受
`TRACEWEAVE_SOURCE_GRAPH_FRONTIER_MAX_INSTANCES` 限制。浅层查询、full-manifest replay、
bounded bootstrap、无法证明的 hierarchy 或成本更高的形状都保持精确 ancestor artifact。
如果运行时证据仍要求新的 frontier，预先纳入的 parent 会保留在下一次精确 ancestor union
中，不会把两个 artifact 的 scope 或 facts 混在一起。该策略只改变 preparation 调度；
coverage exclusion、fingerprint、single-artifact provenance、公开输入与结果 schema 均不变。

同一个已证明 parent 下的连续深层查询还可以选择复用一个 bounded Slang semantic session，
同时继续为每个 scope 发布独立的窄 IR。该加速器受门禁保护，默认关闭：

```bash
export TRACEWEAVE_SOURCE_GRAPH_SEMANTIC_SESSION=1
export TRACEWEAVE_SOURCE_GRAPH_SEMANTIC_SESSION_IDLE_TTL=60
export TRACEWEAVE_SOURCE_GRAPH_SEMANTIC_SESSION_MAX_RSS_BYTES=805306368
export TRACEWEAVE_SOURCE_GRAPH_SEMANTIC_SESSION_MAX_INSTANCES=64
export TRACEWEAVE_SOURCE_GRAPH_SEMANTIC_SESSION_MAX_INPUTS=256
```

adapter 保持 artifact scope 不变，并另行绑定一个满足 instance/input 上限的已证明 parent
context。单个隔离 child 最多保留一个精确的 source/options/top/snapshot context；第二个满足
条件的 scope 直接从该 root 投影，不重复 parse/elaboration。context 变化会先重启 child；空闲
60 秒自动 eviction；cancel、timeout、crash、protocol failure，或实时/回执 RSS 任一超过硬上限，
都会销毁完整 session，且不发布半成品 artifact。落在 context 外的 frontier 仍走既有 one-shot
worker，同时保留 parent session 供后续 sibling scope 使用。只有 compact scoped IR 可以进入
memory 或可选 disk cache；Slang state 不进入 MCP 进程，也不落盘。新增的纯数值回执报告 session
hit/miss/restart/eviction 与 frontend launch。无法取得完整 bounded context 的请求继续使用默认
one-shot 行为。

改变默认策略前应运行 `scripts/soak_source_graph_semantic_session.py`。它接收外部提供的
20--100 条互不重复的精确深层 driver/load 查询，在两个 fresh process 中对比当前 one-shot
lifecycle 与生产 persistent runner。仅含聚合信息的报告会校验 fact/status/coverage 等价性、
launch/reuse count、整段与首查询 latency、break-even 序号、tail latency、RSS 上限/增长、失败与
eviction。单个设计即使通过，仍会输出 `default_on_authorized=false`；只有多个有代表性的适用设计
及真实查询频率证据，才足以承担常驻 frontend 进程。如果 bounded adjacent expansion 已经生成
一个可复用 compact artifact，workload 会被标为
`not_needed_existing_artifact_scope`，不会把普通 memory-cache hit 冒充 semantic-session hit。
完整调用参数见脚本的 `--help`。

对于 source compile 与 elaboration 分属多个日志的 VCS 流程，可以显式构造同一个上下文。
建议把包含源文件顺序的 compile log 保持为 primary（结构扫描也使用它），再按 build 顺序
提供其余 source/elaboration 日志：

```text
build_tb_hierarchy(
  compile_log=".../comp.log",
  supplementary_compile_logs=[".../vhdl_comp.log", ".../elab.log"],
  simulator="vcs",
)
```

生成的 handle、hierarchy snapshot 与 Source Graph compile fingerprint 都覆盖全部日志以及
全部有序 source/support input。后续 connectivity 工具的参数不变，仍传同一个 primary
`compile_log`。若 simulator/top 冲突、日志重复、source order 不完整或存在实质性 parse
warning，manifest 会保持保守/incomplete，不会拼出一个并不存在的合成命令。

#### 大编译集与 bounded bootstrap

完整 hierarchy 仍是默认路径，因为 hierarchy 浏览与全设计分析需要这张 testbench 全景图。
现在 compile log 采用流式解析；handle 只保留每个文件的紧凑扫描事实，不再保存源码正文。
精简结果新增数值型 `build_metrics`，包括源码文件数/字节数、各阶段耗时、RSS 采样、
`source_text_bytes_retained=0`，以及隐私安全的 compile-session snapshot
文件数/字节数/完整性和有界 preprocessor/source-index counters；
`scan_structural_risks` 在 `scan_metrics` 中公开相同的隐私安全索引事实。这些计数会区分 physical source load、
source/masked-text cache hit 与 bytes、logical expansion、comment-mask fast path，以及
plain expansion-line fast path、exact/LRU include resolution 的 hit、miss、entry 和
eviction；绝不暴露 path、include name、macro 或源码内容。Source Graph manifest receipt
还会在同一隐私边界下报告摘要复用/读取的文件数、字节数和冲突数。

重复 module/UVM descendants 在内部使用 template object DAG，同时保持原有 nested-dict
hierarchy 与 handle-tool schema。逻辑统计仍按每条实例路径计数，但 memoized summary 和
handle 不再为每个 parent 复制相同 subtree。metrics 会区分 logical/physical nodes、allocation、
cache hit 与 reuse；本地 NPI 写入实例专属 `file:line` 时采用按路径 copy-on-write，不会把一个
实例的 provenance 串到共享 template 的 sibling。

Hierarchy edge 是正向证据，不是 elaboration 猜测。完整扫描的候选与已接纳节点携带固定的
origin/status/gap metadata。显式或隐式 generate、实例数组与 bind statement 只保留为诊断候选，
不会展平成虚假的 child path；duplicate definition 只保留 ambiguous parent edge，不猜 source
或 descendants。parameter override 的直接 edge 仍可安全保留，同时记录 compatibility tree
未物化 specialization。Source Graph 会把查询祖先链上的有效 gap 带入 receipt 与 coverage
boundary，阻止不完整上下文产生 complete negative；parameter-only gap 属于 informational，
因为 Slang 会自行完成 specialization。build metrics 只用数值和固定 label 报告 candidate、
unresolved edge 与 duplicate-definition counts。

Source Graph 内部已不再直接依赖 compatibility tree 的具体形状。
`hierarchy_provider.py` 提供有界的 O(depth) scope lookup、精确
instance-to-definition binding 和带上限的 direct-child 读取。默认 compile-log provider
只包装 `component_tree`，不会导入 Slang；每个已准备好的 Connectivity IR 则在现有 query
engine 索引之上惰性提供 semantic provider，使 generate scope、instance array 与 parameter
specialization 保留 elaborated `InstanceDecl` binding，同时不复制另一份完整 hierarchy，也不
重复建立 path dict。provider-local stable instance ID 受 immutable design identity 限定；公开
hierarchy 与 Source Graph receipt schema 保持不变。

在具备 license 的开发机上，可以用
`scripts/benchmark_hierarchy_provider_soc.py` 离线比较该 Slang provider 与有界 NPI
oracle；两侧在独立 fresh process 中运行。NPI 侧只对目标的 dotted prefix 做 exact
`get_inst()`（默认最多 256、硬上限 1,024），不遍历 top 或 sibling；其 partial fragment
不能支持 exhaustive negative hierarchy claim。benchmark 只输出 hash、count、timing 与 RSS，
不输出 signal/source/instance 名。这只是 opt-in 开发工具，不会由 `build_tb_hierarchy`
调用，也不改变生产 backend route。

配套的 `scripts/benchmark_connectivity_differential_soc.py` 用一份有界
driver/load/path corpus 对比 direct NPI 与 Source Graph。两侧分别运行在 fresh process
中，且都不能进入生产 fallback chain。Source Graph 每个 query attempt 只准备一个 bounded
artifact；projection 不完整时保留明确 coverage fact，不通过 dynamic expansion 或 Static
结果掩盖。报告不包含 query 原文、signal/scope/source path 或 expression，只输出 SHA-256
evidence anchor、count、固定 status、timing、cache metric 与 RSS；driver/load 行还会保留
纯数字和固定标签的资源边界回执，使截断可测但不泄露设计身份。Source Graph 非穷尽时，
NPI-only facts 归类为 coverage-explained；只有 Source Graph coverage 穷尽时才归类为
unexpected。Source Graph-only facts 与 path reachability 差异单独保留，因为 NPI 是重要参考，
不是绝对正确的 oracle。

输入是一份最多 64 个语义查询的 bounded JSON corpus：

```json
{
  "schema_version": "1.0",
  "queries": [
    {"operation": "driver", "signal_path": "tb.dut.result", "recursive": true},
    {"operation": "loads", "signal_path": "tb.dut.request", "max_depth": 1},
    {"operation": "path", "from_signal": "tb.dut.a", "to_signal": "tb.dut.b"}
  ]
}
```

只在已授权且有 license 的开发机上运行：

```bash
python3.11 scripts/benchmark_connectivity_differential_soc.py \
  --compile-log <compile.log> --corpus <queries.json> --top <top>
```

该 benchmark 只用于开发验证，不会由 MCP tool 调用，也不会选择、提升或压制任何生产
backend。

完整 scanner 在不削弱 preprocessing proof 的前提下消除重复工作：不在 block comment 中且
不含 `/` 的行直接跳过逐字符 mask；结构 token 收集前用同一字符串语法一次性移除 quoted
string；simulator 记录的 include edge 先形成无歧义 basename index，再进入目录搜索。正向
include resolution 进入 4,096-entry LRU，未解析 include 从不缓存。definition regex 只接受
水平缩进，避免 `^\s*` 在展开 header 的数千空行之间反复回溯。在含 directive 的
compilation unit 内，comment-aware 后没有反引号的 active line 会同时跳过 directive 与
hierarchy-macro recognizer；文件元数据 regex 只在匹配所需字面量存在时运行。expanded/trusted
structural view 要覆盖 root-local instance facts 时，也不会先解析再丢弃 root instance list。
basename 有歧义时仍按原 include-dir 顺序解析；所有优化都保持每个 compilation unit 独立
macro state、cancellation checkpoint、compact snapshot 与公开 hierarchy schema。

完整构建有两个默认关闭的可选 guardrail。若站点外层 MCP watchdog 为固定时限，可以把
内部阈值设得更早，从而收到结构化 blocker，而不是只看到 client/process 被终止：

```bash
export TRACEWEAVE_HIERARCHY_TIMEOUT=20
export TRACEWEAVE_HIERARCHY_MAX_SOURCE_BYTES=1073741824
```

transient 共享源码层默认开启，并有独立上限。容量不足会安全回到原 reader，不会阻断工具，
也不会发布部分 hierarchy：

```bash
export TRACEWEAVE_COMPILE_SOURCE_INDEX=1
export TRACEWEAVE_COMPILE_SOURCE_INDEX_MAX_BYTES=134217728
export TRACEWEAVE_COMPILE_SOURCE_INDEX_MAX_FILES=32768
```

设置 `TRACEWEAVE_COMPILE_SOURCE_INDEX=0` 可关闭共享；非法或非正数上限只会禁用该优化，
并以固定 `compile_source_index_config_invalid` disposition 呈现。

触发阈值时返回 `build_status="blocked"`、固定 `blocker`，且不生成
`hierarchy_handle`；它不会把部分 hierarchy 冒充成全景图。紧凑 compile context 会保留在
最多四项的进程内 cache 中，随后可以对单个 endpoint 显式启用 bounded bootstrap：

```text
find_signal_loads(
  signal_path="top.u_agent.gate_en",
  compile_log=".../comp.log",
  supplementary_compile_logs=[".../elab.log"],
  simulator="vcs",
  allow_bounded_bootstrap=true,
)
```

Bootstrap 不是 `build_tb_hierarchy` 的替代品。它只开放给
`explain_signal_driver` 与 `find_signal_loads`，保持 NPI 优先级，只检查 simulator 记录的
有序输入（绝不搜索文件系统），证明 top 到 target 的实例链以及 package/include 和
preprocessor 上下文，对实际选中的每个输入做内容指纹，并从 bootstrap replay 中移除
可能重新展开大库的 `-v`/`-y`。遇到 `uvm_pkg` import 时只记录明确的
`uvm_dynamic_connectivity` exclusion，不展开整套 simulator UVM library。Source Graph 证明的正事实可以
使用，但始终是 scoped：`coverage_status=inconclusive`、`exhaustive_search=false`、
`negative_claim_allowed=false`。当预处理上下文不完整时，回执只公开固定且不含敏感信息的
`preprocessor_issue_categories`。若目标实例链在不确定边界之前已被完整证明，则可携带
`bootstrap_include_context_incomplete` 继续；若未解析上下文可能隐藏剩余实例段，则以
`bootstrap_include_context_unproved` 停止。回执不会包含路径、宏值或源码片段。若
目标直接命中 generate/array/bind 候选，也不会把它提升为扁平 ancestor chain，而是以
`bootstrap_hierarchy_edge_unproved` 和固定 hierarchy coverage exclusion 停止。若
scope/build/query 无法证明，bootstrap 返回诚实的无事实
回执，不再启动它原本就是为了避免的全源码 Legacy Static 扫描。正常 full-hierarchy 路径
仍保持既有的 Source Graph-to-Static fallback。
若同一 identity 的 full-design source index 已经 active，bootstrap 可以复用；若没有，则回执
标记 `miss_no_active_session`，继续使用原来的 bounded reader，绝不会为单 endpoint 主动预读
整个工程。

Bootstrap 的限制都是硬上限，可分别配置（字节值使用纯整数）。默认值为用户所报的
3,843 个源码场景留出了充足余量。内部 timeout 设为 24 秒，有意低于该环境观测到的
27 秒外层终止点，以便 TraceWeave 在被中断前返回结构化 blocker：

```bash
export TRACEWEAVE_BOOTSTRAP_TIMEOUT=24
export TRACEWEAVE_BOOTSTRAP_MAX_SOURCE_INPUTS=128
export TRACEWEAVE_BOOTSTRAP_MAX_SOURCE_BYTES=67108864
export TRACEWEAVE_BOOTSTRAP_MAX_INVENTORY_FILES=16384
export TRACEWEAVE_BOOTSTRAP_MAX_INVENTORY_BYTES=1073741824
export TRACEWEAVE_BOOTSTRAP_MAX_INCLUDE_DEPTH=64
export TRACEWEAVE_BOOTSTRAP_MAX_HIERARCHY_DEPTH=256
```

可用下面两条命令复现用户所报规模的基准：

```bash
python3.11 scripts/benchmark_hierarchy_bootstrap.py --mode hierarchy
python3.11 scripts/benchmark_hierarchy_bootstrap.py --mode bootstrap
```

要在真实 compile log 上执行同形 before/after 测量，可使用下面的 compile-log-only benchmark。
它默认关闭可选 NPI source overlay，输出中不含路径，并报告 structural result hash、hierarchy
计数、phase timing、RSS 与 preprocessor counters。split compile/elaboration flow 可重复传入
`--supplementary-compile-log`；只有专门测量独立 licensed overlay 时才使用
`--npi-source-overlay`。

```bash
python3.11 scripts/benchmark_tb_hierarchy.py \
  --compile-log /path/to/build.log --simulator vcs
```

默认并行源码分析的另一半可用配套 structural benchmark 测量。它不输出路径或 finding 正文，
只报告 wall/RSS、逻辑源码打开次数与字节数、分类计数，以及用于 before/after 等价校验的完整
结果哈希。

```bash
python3.11 scripts/benchmark_structural_scan.py \
  --compile-log /path/to/build.log --simulator vcs
```

若要直接测量共享层，可使用组合 benchmark。它在 fresh process 中并行运行两个公开工具，
交替 enabled/disabled trial 顺序，并报告读取放大、wall/RSS 及两份完整语义哈希：

```bash
python3.11 scripts/benchmark_compile_source_index.py \
  --compile-log /path/to/build.log --simulator vcs --repeats 3
```

当所选 top 和查询区域能由 Verilog/SystemVerilog frontend elaboration 时，包含
Verilog、SystemVerilog 与 VHDL 的工程仍可进入 Source Graph。
编译命令与 filelist 共用同一套大小写不敏感的后缀策略：`.v`、`.vh`、`.sv`、`.svh`、
`.svi`、`.sva`、`.svl` 作为普通 frontend 文本输入；`.svp` 仍按精确顺序进入内容指纹、
worker 请求和 KDB build 输入，但按 protected unit 处理——层次/结构正则扫描不会读取其载荷，
Source Graph coverage 会报告 `protected_region`，不会声称能够看见加密 IP 内部。
VHDL 文件继续参与内容身份但不传给 Slang；coverage 会报告 `opaque_vhdl_boundary`
（以及未投影文件计数）。因此 frontend
diagnostic 或不透明 VHDL 区域会禁止穷尽式负结论，但不会丢弃已证明的正向 driver/load/path
事实：有正向事实的查询仍以 Source Graph 返回并携带 `positive_fact_confidence`，只有没有
可证明事实的 inconclusive 查询才继续降级到 Legacy Static。本阶段不投影 VHDL 内部；若
所选 elaboration top 本身就是 VHDL，也仍不在本阶段的支持范围内。

Source Graph 默认启用。若 MCP Python 没有 optional frontend，会记录 dependency blocker
并继续 Legacy Static。推荐安装方式会把 `pyslang==11.0.0` 安装进启动 MCP server
所使用的同一个仓库本地解释器：

```bash
bash scripts/setup_source_graph.sh
# 将 MCP command 配置为 <TRACEWEAVE_HOME>/.venv/bin/python
```

这一路径不需要额外设置 Source Graph 环境变量：默认策略已经启用、预期 frontend
版本为 `11.0.0`，并使用 MCP 解释器启动隔离 worker。只有明确需要把 native frontend
放入另一套 pinned Python 环境的站点，才需要配置：

```bash
export TRACEWEAVE_SOURCE_GRAPH=1
export TRACEWEAVE_SOURCE_GRAPH_PYTHON=/path/to/pyslang-11.0.0/bin/python
export TRACEWEAVE_SOURCE_GRAPH_FRONTEND_VERSION=11.0.0
export TRACEWEAVE_SOURCE_GRAPH_TIMEOUT=120
```

若站点编译 wrapper 会加入一个已经确认只影响 runtime 的私有 plusarg，可用精确本地白名单
将它从 frontend replay 中排除：

```bash
export TRACEWEAVE_SOURCE_GRAPH_RUNTIME_PLUSARGS_JSON='["+PROJECT+RUNTIME_MODE"]'
```

该值必须是最多 256 项的 JSON list，每项按大小写精确匹配；不支持 prefix 或 wildcard。
未知选项仍然 fail-closed，具有语义影响的 `+define+`、`+incdir+`、`+libext+` 不能加入白名单。
私有 token 文本不会出现在公开回执中，但该策略会参与 cache identity。修改后需重启或重新连接
MCP server。

`TRACEWEAVE_SOURCE_GRAPH_TIMEOUT` 是有限的 worker 秒级时限（范围
`0.001..86400`），默认值仍为 120。每次实际进入 prepare 的回执都会以
`source_graph.effective_timeout_sec` 报告校验后的有效值。即使 compile manifest
不完整，精确相同且时间重叠的 build 也只共享当前正在运行的 worker；该 artifact 仍不会写入
内存或磁盘 cache。成功且具备内容锚点的不完整 build 可以为下一个精确相同的 artifact 请求
保留一次有界 session handoff（包括相同的有效 timeout）：最多 1 个 entry、512 MiB、60 秒，
命中后立即移除，不做 dominating-scope 搜索。它仍报告
`cache_disposition="bypass_incomplete_key"`，并以
`artifact_reuse="session_handoff"`、`cache_tier="handoff"` 明确区分。缺少内容身份、snapshot
不完整、scope 非显式、artifact 超限、过期，以及 failed/timed-out/cancelled build 都会正常
重建。取消一个仍在运行的 waiter 不会影响其他 waiter；全部 waiter 都取消时才终止 worker。

当已有可用 KDB，但需要专门验证 Source Graph 的 driver、load、path 或 X-trace
路径时，可以显式选择：

```bash
export TRACEWEAVE_CONNECTIVITY_ROUTE=source_graph
```

这不会重命名、移动或破坏 KDB；上述四个公共 connectivity 工具以及
`build_tb_hierarchy` 的可选 file/line overlay 都不会构造或调用 NPI。层次拓扑仍来自
compile log，`project` 回执会报告 `source_info_overlay="compile_log"` 和
`source_info_overlay_reason="npi_skipped_by_policy"`。Source Graph 无法安全回答时仍按
正常规则整体 fallback 到 Static；connectivity 回执会保留
`kdb_validation_status="usable"`，报告 `connectivity_route="source_graph"`，并把
NPI attempt 记为 `status="skipped"`、`reason="npi_skipped_by_policy"`。取消该变量或
设为 `auto` 即恢复默认的 trusted NPI -> Source Graph -> Legacy Static 路由。非法值
不会悄悄改变路由，而是保持 `auto` 并报告固定的
`connectivity_route_config_invalid`。

在大型参数化 SoC 中，bounded frontend 允许 compile hierarchy 中的候选实例被当前
generate specialization 消除：它会记录 inconclusive 的
`focused_instance_not_elaborated` coverage gap，并继续投影真实 elaborated 的实例。packed
select 会先与声明范围核对再展开，因此 inactive 分支中的 unsigned 参数下溢不会在 Python
侧构造巨大的 range。X-trace 对父级 net 的查询若为 inconclusive，可以把该父级的直接子实例
作为 bounded frontier；TraceWeave 会按精确 ancestor union 重建 artifact，并从原始 X 信号
重新开始。扩展仍受 `TRACEWEAVE_SOURCE_GRAPH_FRONTIER_MAX_INSTANCES` 限制；超过上限会诚实
fallback，不会枚举整个设计。

driver 恢复依据逐 bit mapping，而不是“整条总线必须精确重合”的启发式。例如
`.instr_rdata_i({8'h0, instr_rdata_core})` 会分别报告常量驱动的高 8 bit 和信号驱动的低
24 bit。partial coverage 下已经证明的正向分段仍可使用；只有 complete artifact 才能断言
未覆盖分段没有 driver。

Source Graph 通过新增的 `claim_semantics` 回执显式区分这些语义。既有 `confidence` 字段保持
兼容，仍是“正向证据 × 全局 artifact coverage”的保守合成值；调用方应分别读取：

- `positive_fact_confidence`：已经返回的正向 source fact 本身有多可靠；
- `target_bit_coverage`：请求的 driver/load bits 是否全部得到解析；
- `global_coverage_status`：bounded artifact 的全局覆盖，包括与本目标无关的 unsupported construct；
- `exhaustive_search`：本次操作是否穷尽了受支持搜索空间（正向 path 只返回第一条已证明路径，
  因而不是穷尽枚举）；
- `exclusive_driver_proved`：每个请求 bit 的 driver 集合是否已穷尽且不存在重叠多驱动；
- `negative_claim_allowed`：能否可靠地说“不存在 driver/load/path”。

因此，大型 SoC 结果可以仍显示旧字段 `confidence="partial"`、
`coverage_status="inconclusive"`，同时给出 `positive_fact_confidence="exact"` 和
`target_bit_coverage="complete"`。这表示返回的逐 bit driver 可以使用，但只有
`exclusive_driver_proved=true` 才能称它为唯一 driver；空结果也只有在
`negative_claim_allowed=true` 时才能解释为“不存在”。`trace_x_source` 会把同一回执保留在
每个 Source Graph chain node 上。

即使大型 IR 已经构建完成，warm driver/load 图遍历也有独立资源硬上限。固定默认值为：
4,096 个 visited states、16,384 条 inspected IR edges、256 个唯一 matches，以及 4,096
个 expansion frontiers。遍历会在 state 与 edge 边界检查协作式取消，并在选择有界结果前对
索引做稳定排序，因此同一 canonical IR 会保留同一批 facts。任一上限触发时都会设置
`query_truncated=true`、对应的 `*_truncated` 标志和 `query_*_limit` coverage gap，并强制
`coverage_status="inconclusive"`。已经返回的 facts 仍是已证明的正向事实，但
`exhaustive_search=false`；driver 的 `exclusive_driver_proved=false`，且
`negative_claim_allowed=false`。因此高 fanout load 列表绝不能被描述为完整枚举。本切片不
改变公开 MCP 输入。

合成查询 benchmark 会让每种模式运行在独立新进程中，并报告 query/serialization 时间、
结果字节数、RSS、实际 limits 和结果稳定性：

```bash
python3.11 scripts/benchmark_source_graph_query.py --fanout 50000 --mode bounded
python3.11 scripts/benchmark_source_graph_query.py --fanout 50000 --mode full
```

signal 到 instance 的解析也已与设计总规模解耦：query engine 从最深到最浅检查 dotted
hierarchy prefix，并直接查询 instance table，不再先排序再扫描全设计实例。宽总线 load
匹配则为每个 match 只构造一次请求 bit membership set，同时继续保留 ascending range 与
concat mapping 所需的有序 bit tuple。以上都是内部改动，公开 path、bit 顺序、回执和 schema
均不变。以下命令可复现 30k-instance 与 4,096-bit workload：

```bash
python3.11 scripts/benchmark_connectivity_query_indexes.py \
  --workload instance-resolution --size 30000 --repeats 100
python3.11 scripts/benchmark_connectivity_query_indexes.py \
  --workload wide-load --size 4096 --repeats 100
python3.11 scripts/benchmark_connectivity_query_indexes.py \
  --workload path-chain --size 4096 --repeats 10
python3.11 scripts/benchmark_connectivity_query_indexes.py \
  --workload path-comb --size 4096 --repeats 10
```

IR 当前仍使用显式有序 bit tuple。更大范围的 interval/segment 重写会涉及 schema、cache
version 与正确性成本，因此留到真实 workload 证明仍有必要时再实施。两个 path workload
分别暴露深路径 CPU 成本和队列共享前缀的内存放大，因此 path search 的存储优化可以与
instance 或 packed-bit 表示解耦评估。
在 Linux 4.18、CPython 3.11.13、AMD Ryzen 7 5700G 上，对 4,096-edge workload 做三组
fresh-process 配对复测：`path-chain` 的跨进程中位数从 39.47 降到 24.78 ms（1.59x），
`path-comb` 从 77.12 降到 20.87 ms（3.69x）。`path-comb` 的 median maximum RSS 从
63,900 降到 31,464 KiB；单链则因用 predecessor table 交换一个持续增长的 prefix，峰值从
32,456 增到 33,584 KiB。前后 result fingerprint、status、visited state、traversed edge 与
truncation receipt 完全一致。真实 OpenTitan 的两条 fact 短路径基本不变（0.452 对
0.438 ms）。10 万实例解析中位数仍为 0.0037 ms；即使合成 65,536-bit load 也为
75.6 ms，而公开结果本身已达 3.44 MB。因此 numeric stable ID、hierarchy trie 与
interval-bit IR 继续保持 evidence-gated，不纳入本次改动。

可选的 exact content-addressed disk cache 能在 MCP 重启后复用已验证的 scoped IR，
但默认保持关闭：

```bash
export TRACEWEAVE_SOURCE_GRAPH_DISK_CACHE=1
export TRACEWEAVE_SOURCE_GRAPH_DISK_CACHE_MAX_ENTRIES=8
export TRACEWEAVE_SOURCE_GRAPH_DISK_CACHE_MAX_BYTES=536870912
```

它沿用 `TRACEWEAVE_CACHE_DIR` 根目录，并写入私有 namespace
`source_graph/disk-v1/`。进程内 exact/dominating cache 永远先查，memory hit 不执行
disk I/O；memory miss 只做一次直接寻址的 exact-artifact lookup，不在启动时扫描，也不做
disk-level dominating-scope 搜索。每个 fresh process 仍会在 disk lookup 前，对全部有序
source/support inputs、options、tops 与 compile/hierarchy snapshots 做完整内容哈希和验证。
verified hit 会跳过 frontend worker、创建新的 query engine，并进入 memory cache。未知版本、
截断、损坏或版本不匹配的 entry 都只产生固定原因 safe miss，随后走正常 cold build；不会被
解释为 connectivity negative 或 Static 结果。

memory-level dominating reuse 可以跨越两个不同的 dependency closure，但必须同时满足明确的
fail-closed 包含关系证明：完整 compile manifest、options、tops、frontend/schema version
以及 compile/hierarchy snapshot 全部精确相同；cached ordered input set 必须包含本次请求的
全部 inputs；其已证明 hierarchy scope 与 objective exclusions 也必须支配本次请求。反向 subset、
source/snapshot/version 变化、重复输入 manifest 或不受支配的 sibling scope 都继续 miss。
返回 payload 仍只来自一个 cached artifact。每个 projected artifact 在契约层都必须标记
`compile_projection_pruned_inputs`，所以 coverage 始终保持 inconclusive，只有已证明的 positive
fact 可以复用；disk lookup 仍保持 exact-only。可通过以下命令
复现双 scope orchestration benchmark：

```bash
python3.11 scripts/benchmark_source_graph_scope_reuse.py \
  --delay-ms 50 --repeats 5
```

要在同一个满足准入条件的 SoC target 上比较历史 reactive 两次构建与新的首 artifact 策略，
请让两种 strategy 各自在 fresh process 中运行。报告包含 plan 大小、prepare/build/load 时间、
worker 与 parent peak RSS、cache bytes，以及最终公开查询结果的 hash：

```bash
python3.11 scripts/benchmark_source_graph_initial_scope.py \
  --compile-log /path/to/build.log --simulator vcs \
  --signal tb.dut.path.to.signal --operation driver --max-depth 20 \
  --strategy reactive-sequence
python3.11 scripts/benchmark_source_graph_initial_scope.py \
  --compile-log /path/to/build.log --simulator vcs \
  --signal tb.dut.path.to.signal --operation driver --max-depth 20 \
  --strategy bounded-adjacent
```

若要在任意 SoC 目录结构上做可复现的跨重启观察，可使用
`scripts/soak_source_graph_soc.py`。它显式接收 verification root、compile log、sim log、
waveform、top，以及外部 JSON 格式的 public driver/load/path 查询列表，不内置 DVSim、FuseSoC
或 Bazel 目录约定。每个样本都是 fresh process；除非显式传入 `--resume`，脚本会拒绝非空 cache
root。原始 cache/telemetry 只留在 owner-private root 下，可选 `--output` 仅写数值和固定 label
聚合。完整调用方式见该脚本的 `--help`。

持久化的 canonical ConnectivityIR 可能包含 protected-IP 派生结构信息。TraceWeave 对
namespace/entry 使用 owner-only 目录与文件权限（`0700`/`0600`），拒绝 symlink 和
non-regular entry file，使用 atomic publish，并且只在局部 lookup/publish/maintenance 路径
执行确定性的 entry/byte 容量维护。请选择可信的本地 `TRACEWEAVE_CACHE_DIR`，不要把这一
opt-in cache 放在不可信或共享文件系统。failed、timed-out 或 cancelled build 不会发布。

按标准工作流先运行 `build_tb_hierarchy`，使请求拥有精确的 compile/hierarchy handle。
`backend_status` 会报告 `selected_backend`、`attempted_backend`、
`actual_backend`、有序 `attempted_backends` 链，以及包含固定 blocker label、coverage、
build/compile/IR fingerprint、cache disposition 和数值资源指标的 Source Graph 回执。
新增的固定 label 会区分 `memory`、`disk`、`build`、一次性 `handoff` tier 和 disk
validation outcome；
回执不会暴露 cache path 或 entry name。
partial/inconclusive coverage 下的正结果仍是 partial；只有 complete coverage 才能确定
`not_connected`。正常 full-hierarchy 路径的 fallback 会由 Legacy Static 整体重算结果，
因此 payload provenance 不会混合。上文所述显式 bootstrap-only 例外会抑制这次无界 Static
重扫，并且不能据此给出负结论。

### 功能性验证

任一客户端连接成功后,运行一次端到端冒烟测试:

1. 在包含仿真日志与波形文件的工程目录里启动 `codex` 或 `claude`。
2. 直接发起波形调试请求,例如:"调用 TraceWeave MCP。先用 `get_sim_paths` 列出这个 case 的 logs 与 waves。"
3. 确认执行日志里出现真实的 MCP 工具调用,如 `get_sim_paths`、`parse_sim_log`、`search_signals`,而不是只通过 shell 命令手动读文件。

## 标准 MCP 工作流

这是仿真日志与波形调试的默认工作流:

1. 调用 `get_sim_paths(verif_root, case_name?)`。对于非标准布局,还可显式传入 `sim_log` / `wave_file` / `compile_log` 路径;给定的字段按原样采用,省略的字段仍会自动发现(`sim_log` 路径还会锚定其所在 case 目录,据此发现对应波形与编译/elab 日志)。显式路径可为绝对或相对——相对路径会按 `verif_root` 及其各级祖先解析(因此相对仓库根的路径也可用),仍找不到时按文件名回收。
2. 完整 single-log 流程选择 `phase == "elaborate"` 的编译日志；split VCS source-compile/elaboration 流程以 source-compile log 为 primary，并保留有序 companion logs 作为 `supplementary_compile_logs`。
3. 在同一个 primary compile log 上并行运行带 supplements 的 `build_tb_hierarchy` 与 `scan_structural_risks`。
4. 如果有仿真日志,调用 `parse_sim_log`;然后在失败且有波形的运行上调用 `sweep_handshakes` 做一次全设计协议健康扫描(default-flow 步骤,相当于运行期的 `scan_structural_risks`)。
5. 使用 `recommend_failure_debug_next_steps` 或 `analyze_failure_event`。
6. 当需要针对显式信号的波形快照时,使用 `search_signals` 与 `analyze_failures`。
7. 对于更深入的调查,使用 `explain_signal_driver`、`trace_x_source` 或 `get_signals_by_cycle`。
8. 任何时候都可以使用 `get_diagnostic_snapshot` 查看可复用的缓存会话状态。

关键工作流规则:

- `scan_structural_risks` 是默认工作流的一部分,除非用户明确要求跳过,否则不应省略。
- `build_tb_hierarchy` 与 `scan_structural_risks` 必须使用同一个 `compile_log`。
- 必须读取结构扫描的 `coverage_status`:只有 `complete` 且 `total_risks=0` 才表示受支持的源码集已扫描且无发现。`zero_coverage` 表示没有扫描到受支持的 Verilog/SystemVerilog 文件,`degraded` 表示源码集不完整或 parser 已降级;两者都不是“扫描干净”。
- 优先使用 `parse_sim_log` 返回的 `failure_events[].time_ps` 作为波形时间锚点。
- 当 `fsdb_runtime.enabled == false` 时,优先选择 `.vcd` 而非 `.fsdb`。

## 工具速查

### 会话概览

- `get_diagnostic_snapshot`:只读地汇总缓存会话数据并给出下一步建议;镜像 `parse_sim_log` 的 `protocol_symptom_hint`,使 scoreboard 失败在会话开始时就浮现协议健康检查指针

### 路径与层次结构

- `get_sim_paths`:发现编译日志、仿真日志、波形、仿真器、case。可选的显式 `sim_log` / `wave_file` / `compile_log` 覆盖优先于自动发现,省略的字段仍会被发现(以 `sim_log`/`wave_file` 所在目录为锚点)
- `build_tb_hierarchy`:流式读取编译证据并在服务端构建完整 testbench 层次结构，不保留源码正文；返回精简载荷(project、stats、深度 2 的 tree skeleton、interfaces、ambiguous_basenames、`build_metrics`、`hierarchy_handle`)。split VCS 流程可在这里一次性传入有序的 `supplementary_compile_logs`;后续 connectivity 查询仍使用 primary `compile_log`。配置的资源 guard 被触发时返回 `build_status="blocked"` 且没有 handle；成功构建的完整数据通过下方 handle 工具按需获取。
- `scan_structural_risks`:在无 waveform lock、可协作取消的 worker 中扫描编译过的 RTL/TB 源码结构风险;返回 `eligible_file_count`、`files_scanned`、`coverage_status` 与 `coverage_warnings`,避免把零覆盖或部分覆盖误读为“扫描干净”

### 层次结构 Handle 工具

下列工具均接收 `build_tb_hierarchy` 返回的 `hierarchy_handle`。当 handle 过期或未知时返回 `{"error": "handle_expired"}`;此时重新运行 `build_tb_hierarchy` 即可刷新。

- `get_tb_subtree(handle, root="", depth=1, max_nodes=500)`:从指定 dotted 实例路径切出 component_tree 子树。
- `lookup_tb_files(handle, ...)`:按客观扫描事实(`basename`、`name_contains`、`path_contains`、`has_module`、`contains_uvm`、`file_type`)查询编译文件集。至少需要一个过滤条件。对 `ambiguous_basenames` 中的多版本文件用 `basename=...` 精确消歧。
- `find_tb_instance(handle, path=... | module=...)`:按精确路径或某模块的所有实例定位。
- `get_tb_file_detail(handle, path)`:返回单个编译文件中定义的符号。未知路径返回 `file_not_in_compile_set` 与基于 basename 相似度的 `did_you_mean` 建议 —— 读取 RTL 前先核实文件确实在编译集中。
- `get_tb_class_hierarchy(handle, root_class?, depth=-1)`:从编译集扫描构建的 UVM/SV 类继承树。
- `dump_tb_section(handle, section)`:逃生通道,返回完整的原始 `compile_result`、`include_tree`、`filelist_tree`、`interfaces`、`files_full`、`component_tree_full` 或 `class_hierarchy_full`。优先使用上面的定向工具。

### 日志分析

- `parse_sim_log`:解析并归一化运行时失败,输出分组摘要与 `failure_events`;同时返回 `log_snapshot_id`,用于仿真器覆盖同名日志后的前后对比。`candidate_previous_logs` 只包含通过有限头尾窗口获得仿真证据的同目录旧日志;compile/elaboration/build 日志与证据不足的 helper 日志会被排除。当失败是 scoreboard/数据比对类时,会设置 `protocol_symptom_hint` —— 一个边界安全的指针,提醒在逐行读 RTL 前先跑一次 `sweep_handshakes` 检查所有接口的总线协议健康;它从不断言协议类型或具体信号。
- `diff_sim_failure_results`:按路径或 `base_snapshot_id` / `new_snapshot_id` 对比两次仿真运行。若前一次已对同一路径调用过 `parse_sim_log`,后续只传 `new_log_path` 时会自动使用上一轮解析快照作为 baseline。
- `get_error_context`:抽取指定行号附近的原始日志上下文

### 波形分析

- `search_signals`:解析完整层次化信号路径。`keyword` 接受单个字符串或**关键词列表**(最多 16 个)——传列表可把多次查找合并成一次调用(每个关键词一个结果条目,按输入顺序),不必连续发起多次单关键词搜索。每条结果还附带 `direction`(`input`/`output`/`inout`/`implicit`/`null`)与 `var_type`(`wire`/`reg`/`integer`/`real`/`parameter`/…),客户端无需额外工具就能在指定 scope 内过滤端口/线网/变量。**FSDB** 两个字段都会填;**VCD** 只填 `var_type`,`direction` 返回 `null`(VCD 格式不编码端口方向)
- `get_signal_at_time`:查询信号在指定时间点的值
- `get_signal_transitions`:取出严格闭区间 `[start_time_ps,end_time_ps]` 内的信号跳变;FSDB 与 VCD 都不会把更早的时间戳混入 `transitions`。窗口起点前最后一次值变化通过独立的 `predecessor` 字段返回,供按时钟采样的内部逻辑识别窗口首个跳变方向。单次最多返回 `max_transitions` 条(默认 1000,保留区间内最早的);被截断时置 `truncated: true` 并附 `hint`。若有界的 native FSDB 输出也发生截断,会设置 `transition_count_is_lower_bound=true`,此时应收窄时间区间以取得完整数据;否则 `transition_count` 是区间内总数,显式返回上限可按需调大
- `get_signals_around_time`:取出失败时间点附近的上下文。`transitions_in_window` 是严格闭区间列表;`pre_window_transitions` 只包含更早的值变化,按 `extra_transitions` 截断,且 FSDB/VCD 均按时间正序返回。若某个 `value_at_center` 是**亚周期瞬变**(时钟边沿的组合毛刺、同一周期内又 settle 回去——如互连 mux 在每个边沿 ~1ns 重置成 idle),会通过 `transient_note` + 逐信号的 `center_transient`/`center_settles_to` 标注出来,避免把边沿采到的毛刺当成稳定的协议值。`return_mode="values_only"` 保留多信号原子采样但剥离转换列表(每个信号只返回 `value_at_center` + `window_transition_count` + 瞬变标注)——适合跨多条 trace 比较同一时刻的紧凑模式。`extra_transitions=0` 严格生效:不返回任何窗口前历史。
- `get_signals_by_cycle`:按时钟沿逐周期采样信号
- `get_waveform_summary`:返回波形元数据。内含时间刻度自检字段:`scale_unit`(从波形文件头读出的刻度,如 `100fs`/`1ps`/`1ns`;读不到时为 `unknown`)与 `scale_fs_per_tick`——所有工具输出的时间戳都是按该系数换算后的真实皮秒,绝不是文件内部的 tick 计数。刻度读不到时 summary 附带 `scale_warning`,且该波形上所有时间型查询都会明确报错,绝不静默假设 1ps 刻度

### 游标与验证原语

`get_signal_at_time`、`get_signal_transitions`、`get_signals_around_time`、`trace_x_source`、`diff_first_divergence` 的时间入参接受 **TimeSpec**:整数(ps)、游标引用 `@<name>`、或带单位的字面量(如 `12.34ns` / `5us`)。

- `cursor_set(name, time_ps, note?)` / `cursor_list()` / `cursor_delete(name)`:命名的、进程内的时间锚。定位到某时刻的工具(如 `diff_first_divergence`、`period`)会自动注册一个游标,后续可用 `@<name>` 引用,免去跨调用复制 ps 时间戳。游标不持久化——server 重启即丢。
- `diff_first_divergence(wave_path_a, signal_a, wave_path_b, signal_b, ...)`:两个波形信号首次取值不相等的时刻——可跨两个波形(如 passing vs failing run),也可在同一波形内(两个本应相等的信号,如 lockstep / shadow 寄存器)。在分叉处自动注册游标。要求两侧都是被 dump 的波形信号(它不与软件参考模型比对)。
- `period(wave_path, signal, edge?, ...)`:测信号边沿的主导周期,并标出第一个偏离该周期的拍(off-beat),自动注册为游标。用于"这个信号本应周期性——节奏第一次在哪里破"(时钟、strobe、定速 valid)。
- `suggest_handshakes(wave_path, scope?, ...)`:扫描波形,提出可直接使用的 `inspect_handshake` bundle —— 按 scope 与 stem 配对 `*valid`/`*ready`、找到时钟、归组通道 payload 总线。先跑它,就不用手攒 `{clock, valid, ready, payload}`。覆盖 AXI/通用 valid-ready 与 req/ack。当什么都没找到时,会用一个轻量名字探测(`htrans`→AHB、`psel`+`penable`→APB)把空结果提示升级成可直接复制粘贴的 `suggest_protocol_bundles` 调用。
- `suggest_protocol_bundles(wave_path, protocol=ahb|apb, scope?, ...)`:扫描没有字面 `valid` 的协议 bundle。AHB candidate 会返回可直接传给 `inspect_handshake` 的 `valid_htrans`、`ready` 与 payload;APB candidate 返回 `psel`/`penable`/`pready` 事实,并明确标出 `inspect_handshake` 仍需要 `psel && penable` 的派生 valid 信号。对 AHB candidate,结果还会返回 `next_step` 字段 —— 每个接口一条可直接复制粘贴的 `inspect_handshake(...)` 调用,因为 discovery 只定位 bundle,真正的分析是跑 `inspect_handshake`。方向标签只来自 discovery 层的机械事实(`initiator_side` / `responder_side` / `unknown`),推不出或冲突时返回 unknown,不硬猜。
- `inspect_handshake(wave_path, clock, valid, ready, payload?, ...)`:对时钟化 valid/ready 握手逐拍分类 —— stall 连续段(valid 高、ready 低)、最长/超阈值 stall、背压失衡(ready 高、valid 低),以及给了 `payload` 时的保持违例(transfer 仍在 stall 期间 payload 发生变化)。还会标记**valid 过早撤销**(`check_valid_hold`,默认开):一拍被 stall 的 transfer(valid 高、ready 低)在下一拍 valid/htrans 变为非激活——在 ready/HREADY 到来之前——即 master 不等握手就把 transfer 丢掉(AHB master 不等 HREADY 的 bug)。它不需要 `payload`,且能抓到 payload-hold 结构上抓不到的情形:1 拍的 stall(`max_stall_cycles==1`)根本没机会让 payload 变化,而 htrans(派生 valid)又不在 payload 里。协议无关:AXI `*valid`/`*ready`、通用 valid-ready 流、credit 接口。AHB 没有字面 valid —— 传 `valid_htrans=<htrans 路径>`(及 `htrans_rule`:`active`=NONSEQ/SEQ,或 `non_idle`)即可派生出 valid(`payload`=haddr/hwrite/hsize,它们在 hready 低时必须保持;HWDATA/HRDATA 作为数据相位信号被排除,因此保持检查不会因地址/数据相位错位而误报)。AHB 接口还会跑第三项检查 **x_while_valid**:在派生 valid 已置位的边沿上,若某条控制信号为 x/z,则标记——这是一笔携带未知地址/控制字段的活跃 transfer;对字面 valid 接口该检查关闭,因为其 payload 可能是合法为 x 的数据通道(否则会误报)。另有一项**写数据相位保持**检查(传 `hwrite` + `write_data`=HWDATA):校验 HWDATA 在写数据相位 wait state(HREADY 低)期间保持不变,否则为 `write_data_hold_violation`;这是比地址相位 valid 晚一拍的数据相位窗口,htrans-keyed 的 payload-hold 结构上看不到它(也正因如此把 HWDATA 从 `payload` 排除)。它**只在生产者(initiator/master)接口可靠**——consumer/slave 接口的 HWDATA 是互连 mux 的组合输出、在时钟边沿有 ~1 拍毛刺,边沿采样会读成伪变化;所以 `suggest_protocol_bundles` 只给 initiator-side bundle 挂 `hwrite`/`write_data`。AHB 结果还带一个 `protocol_semantics` 回执,说明本结果各项指标哪些忠实、哪些被抑制(valid-hold 忠实;`ready_without_valid` 是空闲总线、非违例;payload-hold 仅地址相位),且 valid 过早撤销的 finding 带 `accepted_before_deassert=False`(被丢弃的 beat 从未被接受)——于是真阳性无法被当作 AHB 流水线重叠搪塞掉。返回 `coverage` 事实,说明实际跑了哪些检查(`stall_checked`、`backpressure_checked`、`payload_hold_checked`/partial、`valid_hold_checked`、`x_while_valid_checked`),但不标协议侧别。在第一个问题处自动注册游标(x-while-valid > 保持违例 > valid 过早撤销 > 长 stall > 最长 stall)。有 finding 时设置 `violating_signal`(payload-hold 时为那个保持信号;x-while-valid 时为那条为 x 的控制字段;valid 过早撤销时为那条 valid/htrans;普通 stall 时为 `null`)和指向 `explain_signal_driver` 的 `next_actions` 链接。对**单面**违例(x-while-valid、payload-hold、valid 过早撤销)还会返回结构化的 `attribution`——`violating_side=valid_driver`、`exonerated_side=ready_driver`——因为二者都是 valid-驱动方义务的违反(payload 跟 valid 同源,只有 producer 能在 stall 中改 payload 或在被接收前撤 valid),responder/ready 侧**不可能**造成它们:别一头钻进 slave driver/monitor。这是协议角色,不是从波形读 ownership:valid-驱动方是该 channel 的 producer(AXI AW/AR/W 上是 master,R/B 上是 slave;AHB htrans 恒为 master)——`explain_signal_driver` 跑在 `valid` 上即落到真正的实例。普通 stall 是真正两面的,所以 `attribution` 留空、链接指向 `ready`。给出在 scoreboard 日志里不留值规律的协议时序事实。
- `sweep_handshakes(wave_path, scope?, ...)`:全设计握手**异常扫描** —— 一次调用发现每个 valid/ready 接口**以及每个 AHB 接口**(htrans 派生 valid)并逐个在窗口内 inspect,返回一张对比事实表(各接口的 `kind`=`valid_ready`/`ahb`、stall、死锁特征、x-while-valid、payload 保持、写数据保持、valid 过早撤销、背压),按透明的机械键排序(`ahb` 行的 `ready_without_valid` 从 `flags` 与排序中剔除——它是空闲总线、不是背压)。共享同一时钟的接口只读取一次时钟 transition、提取一次边沿并复用采样时刻;跨接口共享的信号只缓存到最后一个消费者,唯一 payload 不留存。FSDB 会用有界 native group 一次加载同一时钟组的信号,但仍让每个信号独立复用单次调用的输出缓冲区;旧 wrapper、超过保守默认上限 16 个信号的组或 native begin 失败都会自动退回旧路径(检查 RSS 后可用 `TRACEWEAVE_FSDB_GROUP_MAX_SIGNALS` 调整)。该优化只改变执行成本,不改变 MCP 接口、事实表、截断回执或覆盖语义。这是 scoreboard 失败提示现在主推的一键协议健康检查(APB 除外 —— 需要派生 valid;clocking-block 的 `*_cb` 镜像 scope 会被剔除 —— 它们没有自己的时钟、与父接口冗余)。用于不透明的全局症状(timeout/hang)、当你还不知道众多接口里哪个出问题时;它把 N 次 `suggest_handshakes`+`inspect_handshake` 往返压缩成一次。返回事实而非根因裁决——按症状自行重排。在背压流水线上,最长 stall 排序给出的是传播前沿,根因则在 stall→断粮(starvation)的边界。结果带 `coverage_status`(`complete`/`truncated`/`zero_coverage`/`degraded`)和 `coverage_warnings`;只有覆盖完整时,`flagged_count=0` 才有"已检查接口未见异常"的含义。scoped `zero_coverage` 可去掉 `scope` 或改用父 scope,`truncated` 可提高 `max_interfaces`;但 unscoped 且零接口的结果虽仍明确“不是协议通过”,不会再用相同参数重复调用。`degraded` 只有在 action 改变 scope/window/edge/cap 时才重试,否则 recommendation 返回缺失的 dump/clock/window 前置条件。当发现的接口数超过 `max_interfaces`(默认 64)时会(响亮地)置 `truncated=true`。即使 `runtime_protocol_findings=[]`,`recommend_failure_debug_next_steps` 也会在 `runtime_protocol_coverage` 中保留这些覆盖事实。
- `verify_window(wave_path, clock, mode, predicate | antecedent+consequent, ...)`:在时钟窗口上求值一个时序谓词,返回精确的 `holds` 判定 + 具体的见证/反例(周期 + 采样值)。是模板而非 DSL:一个 *term* 是 `{signal, op, value}`(`op`:eq/ne/gt/ge/lt/le/is_x/is_known);一个 *predicate* 是 term 列表(隐式 AND);`mode` 为 `always` / `never` / `eventually` / `implication`(A ⊦→ B 在 N 周期内)/ `sequence`(单个信号的逐 accepted-beat 增量——地址步进检查;`predicate` 是 accepted-beat 门,`delta`=`{signal,value,op?,modulo?,restart_when?}`,其中 `modulo` 吸收 WRAP 回环、`restart_when` 在 burst 起始重置,均由你提供,工具保持不解码 burst)。`implication` 带一个 `overlap` 标志:默认 `true`(`|->`,响应窗口含 A 当拍)或 `false`(`|=>`,窗口从**下一拍**起)——对**保持/稳定性**类属性用 `overlap=false`("B 下一拍必须仍成立",如 AHB `HTRANS`/valid 在 wait state 期间保持:`(htrans==2 && hready==0) |=> htrans==2`),因为 A 在当拍已蕴含 B。若用 `overlap=true` 求这类属性会得到**空洞通过(vacuous pass)**(B 在 A 当拍已为真,窗口根本没用上)——结果会标 `vacuous=true` 并给出醒目的 `VACUOUS PASS` 警告,避免把 `holds=true` 误当排除证据;请改用 `overlap=false`。x/z 周期报为 `unknown`(绝不静默当通过),响应窗口越过 trace 末尾的 implication 报为 `inconclusive`(绝不静默当失败)。`sequence` 违例时设置 `violating_signal` + 指向 `explain_signal_driver` 的 `next_actions` 链接(总线事实不自判 master/slave)。用于一次调用证实或证伪一个 RTL 推断。
- `reconstruct_transactions(wave_path, clock, req_valid, req_ready, cmp_valid, cmp_ready, ...)`:从两个握手通道重构 id 关联的请求/响应事务 —— 按 `id` 字段把被接受的请求 beat 配对到完成 beat,返回每笔事务的延迟与聚合事实(outstanding 曲线含 per-id 峰值、乱序、unmatched=挂死特征)。一个通用核,而非每协议一个工具:AXI 读 = AR→R(`req_id`=arid,`cmp_id`=rid,`cmp_last`=rlast);AXI 写 = AW→B 外加可选的无索引 W 数据通道(`data_valid`/`data_ready`/`data_last` + `data_fields`)。`req_id`/`cmp_id` 可选 —— 两者都省略即为无索引的在序流(AXI-Lite、APB),按 FIFO 顺序配对。可选的 `reset` 会清空在途状态,使跨 reset 的事务不被误报为挂死。可选的 `req_len`(AxLEN)会把每笔事务的 `beat_count` 与 `req_len+1` 比对——不符(LAST 早到/晚到、丢 beat、多 beat)即真实的 burst 长度违例,逐笔(`expected_beats`、`beat_count_mismatch`)并以 `beat_count_mismatch_count` 给出(len 为 x/z 则不检查;不传 `req_len` 则计数为 0 = 未检查,非"干净"裁决)。`latency` 是分布而非"异常值"裁决;支持跨 id 的乱序完成。

FSDB 覆盖说明:native transition 输出有缓冲区上限。若被检查的时钟或信号
只能返回前缀,对应握手行会设置 `transition_data_truncated`,全扫会增加
`transition_truncated_count`,且 `coverage_status` 不会是 `complete`;此时零
finding 只适用于已返回的前缀。请收窄时间窗口做完整的定向检查。

### 深入分析

- `analyze_failures`:聚焦某个分组失败,返回日志与波形上下文
- `analyze_failure_event`:针对一个 `failure_event`,给可能的实例、源文件、信号排序
- `recommend_failure_debug_next_steps`:返回默认的下一步调试目标
- `explain_signal_driver`:把波形信号回溯到可能的 RTL 驱动逻辑
- `find_signal_loads`:列出信号的消费者(fanout)—— 模块输入端口、RHS 使用、always 块敏感列表
- `trace_signal_path`:查找两个信号之间的结构连通路径。可信 NPI 证据优先；否则 bounded dual-endpoint Source Graph 只沿受支持的 IR binding 与组合依赖查询。只有 complete coverage 才能确定 `not_connected`；inconclusive 负结果最终返回 `unsupported_reason="static_backend_no_path_api"`。返回的是连通性，**不是**时序意义上的 driver 方向 —— driver 语义请用 `explain_signal_driver`。
- `trace_x_source`:按 `可信 NPI -> 单个 bounded Source Graph artifact -> 整条 Static 重算` 向上游追溯 X/Z。wave lock 只覆盖波形读取；任何 backend 切换或已证明的 scope 扩展都会丢弃部分链并从原始 signal 重跑，因此返回链不会混合 backend 或 artifact provenance。`backend_status` 会报告有序尝试、重启原因、Source Graph fingerprint/coverage 与选中/实际 backend；`trace_restarted=true` 明确表示发生了整条 trace 重试。NPI 的 `testbench_driven`、源码行号和 driver-vs-load 交叉校验证据会保留在终止 trace 节点上。
- `build_kdb`:从已解析的编译日志自动构建 Verdi KDB(vericom + elabcom)。当仿真器是 Xcelium(xrun)且 NPI 后端报告无 KDB 时使用。输出缓存到 `TRACEWEAVE_CACHE_DIR`(默认 `~/.cache/traceweave/kdb/<hash>/`);缓存命中则跳过 Verdi 重跑。设置 `TRACEWEAVE_NPI_EXECUTION=lsf` 后，每个 cache miss/强制 rebuild 都在 LSF 上执行，且失败时不会静默回退到本地 license 构建。KDB 旁边会写出一个可运行的 `build.sh` 便于检查或手动复现。需要 `VERDI_HOME` 中含有 `bin/vericom` 与 `bin/elabcom`。

当检测到 KDB 时,`explain_signal_driver`、`find_signal_loads`、`trace_signal_path` 和 `trace_x_source` 会自动启用 Verdi NPI 后端。可信 NPI 结果保持最高优先级；否则 TraceWeave 会先尝试 bounded on-demand Source Graph，再由 Legacy Static 整体重算 fallback 结果或 trace。Static 没有诚实的 `sig_to_sig_conn_list` 等价实现，因此 inconclusive Source Graph path 最终会返回结构化 unsupported，而不会给出近似结论。X-trace 在 backend 或 artifact 改变时始终从原始 signal 重跑。NPI 仍是更深的路径:它使用 `fan_in_reg_list` / `sig_to_sig_conn_list` 在 elaborated netlist 上行走,因此能跨越 Source Graph 明确投影范围之外的实例端口边界、interface 位置绑定与 assign 链。在 **local NPI execution mode** 下，`build_tb_hierarchy` 也可以用 elaborated NPI 证据增强 component-tree 的 `source_file` / `source_line`。这项可选增强有独立资源门禁：默认 `auto` 只接受 clean KDB 且 compile 已证明的实例路径不超过 4,096 条，并只对这些路径调用 `netlist.get_inst()`；degraded KDB 或更大设计保留 compile-log provenance，以固定原因跳过，且不会加载 NPI。设置 `TRACEWEAVE_HIERARCHY_NPI_SOURCE_OVERLAY=force` 可显式对 degraded/更大设计启用定向增强（仍有 100,000 路径硬上限），设置为 `off` 可完全关闭。这一设置不改变 driver/load/path 查询的 NPI 优先级。LSF 初始范围不会让 `build_tb_hierarchy` 隐式提交 batch job,因此在 LSF 模式下 hierarchy 的 source 信息仍来自 compile log。`find_driver` / `find_loads` 中受影响的 hop 会带上 `source_info_origin: "npi"` 或 `"source_graph"`，Static 则保持 compile-log-derived；Source Graph path hop 同样只携带 IR 支持的 scope/source/edge evidence。结果信封里的 `backend_status` 会给出 selected/attempted/actual backend、有序 fallback 链、Source Graph coverage/build回执、KDB 流程与按仿真器给出的 `kdb_hint`。NPI 深但并非万无一失:当它对某条 net 能报出的**唯一**驱动同时也是该 net 的一个 LOAD(interface 切片别名,或一个读取该 net 的寄存器)时,说明根本没有 RTL 驱动,真正的驱动是 testbench/行为级的——经 virtual interface + clocking block 写值的 UVM driver,RTL 寄存器 fan-in 看不到它。`explain_signal_driver` 用 driver-vs-loads 交叉校验识别这种矛盾,返回 `driver_status="testbench_driven"`(附 `cross_check.conflict` 回执),而**不会**把那个 load 当成 "exact" 驱动返回——于是 AHB master 的 HTRANS/HADDR 会把你指向 TB driver/BFM,而不是一个只是读总线的 DUT 互连寄存器。

recursive NPI driver 查询保留 Verdi 原生语义，但不再允许
`fan_in_reg_list()` 物化无界组合锥。TraceWeave 在遍历前注册官方 `FAN_IN`
callback，最多准入 4,096 个 native state，并把公开结果限制为 32 条 driver fact。
NPI 与 Source Graph driver 统一返回 backend-neutral `traversal` 回执：returned/output
count、visited/state limit、可用时的 callback count、截断、`search_exhaustive`、固定
`incomplete_reasons` 与 `continuation_supported=false`。有界正向前缀仍可使用，但会标成
`driver_status="partial"`；只有 `search_exhaustive=true` 才能支持完整 driver-set 结论。
callback API 不可用或注册失败时不会偷偷恢复 whole-cone 调用，而是把已有 direct driver
fact 标成 coverage-incomplete。成功、失败和取消路径都会 reset callback；由于 pynpi 的
callback 是全局状态，注册与遍历会串行化。`trace_x_source` 会在终止节点保留该回执，并用
`driver_traversal_incomplete` 停止，而不是把某一个有界候选提升为排他根因。

对 load 查询，NPI 使用 `net.load_list()` 的直接消费者；遇到向外的 child output 时，
只通过成对 parent net 跨越层次，不再调用会先物化整个组合锥的 native
`fan_out_reg_list()`。所有 backend 的公开 load 输出统一最多 256 项，并返回
`enumeration` 回执，明确 returned count、limit、截断/穷尽性，以及
`continuation_supported=false`。因此正向前缀仍可使用，但不会被误读成全部 loads；未来分页
必须使用能绑定 exact artifact 与工作状态的 backend-neutral cursor。

带有 Verdi `.hasElabcomError` 标记的 `kdb.elab++` 在没有 clean elaborated
KDB 时会作为 degraded NPI candidate。error 数量不是阈值：TraceWeave 只在
`load_design` 返回 `0`、`get_top_inst_list()` 非空，并且可检查时能看到请求的
top 的情况下接受 partial netlist。其他返回码、空或 top 不匹配的 netlist、损坏
KDB、license/import 失败仍按正常 fallback 处理。

degraded 模式只信正向证据，不信穷尽性的负结论。找到明确 driver、有返回事实的 bounded
partial driver、非空 loads 或 found path 时可以返回 NPI；attempt 会标记
`coverage_status="partial"`，loads
整体 completeness 为 `approximate`，即使每个已返回 hop 仍可保持 exact NPI
confidence。driver 未解析、空 loads、`testbench_driven` 判断以及
not-found/not-connected path 会继续进入 Source Graph，再到 Legacy Static。
`trace_x_source` 遇到第一处这类 inconclusive lookup 时会丢弃整条 partial NPI
chain，并从原始 signal 重跑。公共状态保留
`kdb_validation_status="elaboration_error"` 这一 artifact 事实；实际成功加载
partial netlist 后再增加 `kdb_degraded=true`、`kdb_error_count` 和
`kdb_error_log`。显式强制启用独立 hierarchy overlay 策略时，这种 KDB 成功提供的
source 增强会报告 `source_info_overlay="npi_partial"`；默认 hierarchy 策略会跳过它。

该行为默认开启。若要恢复只接受 clean KDB 的旧策略，请在启动 MCP server 前设置
下列变量，然后重启或重新连接：

```bash
export TRACEWEAVE_NPI_ALLOW_DEGRADED_KDB=0
```

第一阶段只支持用户/项目中已经存在的 degraded KDB。`build_kdb` 仍把非零
`elabcom` exit 视为构建失败，不会把该失败产物发布进正常 cache。

对 VCS 流程,获取 KDB 的最低成本方式是用 `-kdb=only` 重编 —— hint 会给出完整命令。对 Xcelium 流程没有原生 KDB;`get_diagnostic_snapshot` 会把 `build_kdb` 列在 `missing_steps` 中,LLM agent 可以按需触发。设置 `TRACEWEAVE_AUTO_KDB=0` 可关闭自动构建提示。

### 使用遥测

启用后，TraceWeave 会为每次工具调用向 `$TRACEWEAVE_CACHE_DIR/telemetry/usage.jsonl`(默认 `~/.cache/traceweave/telemetry/`)追加一行 JSONL —— 工具名、参数的 *键* 与少量标量 flag(绝不记参数值或路径)、结果大小、延迟、锚定到每次 `get_sim_paths` case 的 session id,以及失败调用的分类 `error_code`(错误码或异常类名,绝不记错误消息)。**仅本地**(不发送到任何地方),用于量化哪些工具真正被用到。每次追加都会把 telemetry 目录/JSONL 收紧为 owner-only `0700`/`0600`，包括先前受宽松 umask 影响的已有文件。普通用户默认没有设置 `TRACEWEAVE_TELEMETRY`,此时记录功能关闭,也不会创建 telemetry 文件。需要主动开启时,应在 MCP server 启动前设置 `TRACEWEAVE_TELEMETRY=1`;修改变量后需重启或重新连接 MCP server。

Source Graph 调用会通过第二层独立校验的 numeric/fixed-label allowlist 持久化 `memory`/`disk`/`build`/`handoff` tier 与 process resource aggregates。启用 opt-in semantic session 后，还会记录 frontend launch 与 session hit/miss/restart/eviction count；启用 opt-in disk cache 后，同一条记录还会包含 exact disk hit/miss/corrupt/build-skip、frontend launch、lookup/read/validate/write/publish/eviction timing 和 artifact bytes/entry count。它绝不持久化 artifact fingerprint、cache/source/wave path、signal/scope/value、diagnostic 或 exception text。运行 `python3.11 scripts/telemetry_report.py` 可查看按 tool/session 的使用率，以及 Source Graph tier count、exact disk hit rate、validation outcome、build/skip、bytes/entries/evictions 和各 tier latency p50/p95。query-frequency 区还会给出每个 case 中带指标的 Source Graph 调用数，以及默认 60 秒 session 窗口内相邻同 case 调用的数量；后者只是 reuse opportunity 的上界，同一个 case 并不能证明两次调用会选择同一个 eligible semantic context。加 `--json` 输出机器可读结果。正式 operational soak 应使用新的 private `TRACEWEAVE_CACHE_DIR` 获得隔离的观察窗口。

## 测试

在仓库根目录运行完整测试套件:

```bash
python3.11 -m pytest
```

只跑单个文件:

```bash
python3.11 -m pytest tests/test_server.py
```

只跑单个用例:

```bash
python3.11 -m pytest tests/test_server.py -k diagnostic_snapshot
```

推荐的修改流程:

1. 修改代码。
2. 先跑相关的测试。
3. 涉及共享行为时再跑完整套件。
4. 重启 MCP 客户端,让它重新连接到更新后的服务器。

## 微信

关注微信公众号:

<p align="center">
  <img src="assets/QR.png" alt="微信公众号二维码" width="200">
</p>
