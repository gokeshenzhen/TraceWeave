# 🐙 TraceWeave

<p align="right">
  <a href="README.md">English</a> · <strong>简体中文</strong>
</p>

<p align="center">
  <img src="assets/logo.png" alt="TraceWeave" width="160">
</p>

<p align="center">
  <strong>面向仿真失败调试的 MCP 服务器,基于日志解析与波形分析</strong>
</p>

<p align="center">
  <a href="https://github.com/gokeshenzhen/TraceWeave/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/gokeshenzhen/TraceWeave/ci.yml?branch=main&style=for-the-badge" alt="CI status"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg?style=for-the-badge" alt="MIT License"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.11%2B-blue?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.11+"></a>
  <a href="https://github.com/gokeshenzhen/TraceWeave/stargazers"><img src="https://img.shields.io/github/stars/gokeshenzhen/TraceWeave?style=for-the-badge" alt="Stars"></a>
</p>

<h2 align="center">波形日志根因分析 MCP,不想再调试,就用 TraceWeave。</h2>

TraceWeave 的特色:有 Verdi license 时启用 KDB/NPI 获得更精确的跨层级 driver/load/connectivity 分析;没有 license 时也可用内置 Static 后端、日志解析、VCD/FSDB 波形读取继续定位问题;支持 driver 回溯、load/fanout 查找、指定时刻取值、指定周期数采样、任意信号窗口查询、轻量化 X/Z trace、结构风险扫描、失败分组对比,并给 MCP 客户端输出结构化的下一步调试建议。

<p align="center">
  <img src="assets/onepage.png" alt="TraceWeave 工作流概览" width="900">
</p>

<p align="center"><sub>工作流示意图;实际时序与加速比取决于工程规模与波形可用性。</sub></p>

TraceWeave 是一个面向工作流的调试服务器,而不是一组零散的解析器。它包含:

- 带会话状态、工作流约束和推荐调用顺序的 MCP 服务器
- 编译日志、仿真日志、波形产物的路径自动发现
- 基于编译日志的层次结构构建,以及源码感知的驱动关联
- VCD 与 FSDB 波形后端,支持信号搜索
- 以失败为中心的下一步建议、结构风险扫描、X/Z 传播追踪
- 为 MCP 客户端设计的结构化输出 schema

[架构](docs/architecture.md) · [安装](#安装) · [客户端配置](#客户端配置) · [标准 MCP 工作流](#标准-mcp-工作流) · [工具速查](#工具速查) · [测试](#测试) · [微信](#微信)

## TraceWeave 在什么场景最有用

TraceWeave 并非万能加速器,这一点我们如实说明。在与"只读源码和文本日志的强 LLM"
做盲测对比时:

- **当 RTL 可读、且 bug 是源码层面可见的逻辑错误时**,LLM 直接读源码就已经很快。
  此时 TraceWeave 主要是用波形*佐证*假设,`scan_structural_risks` 还能静态点到出错
  行——有用,但这不是它的护城河。
- **当答案不在可读源码里时,TraceWeave 才成为决定性、甚至唯一的定位手段:**
  - 设计是**加密/保护 IP**,或规模大到无法肉眼通读,bug 既读不到也 grep 不出;或
  - 故障是**时序 / 握手 / X / 连线类、没有静态特征**的 bug,且**症状不透明**
    (超时、卡死、分叉——日志里没有任何数值规律)。

  这些情况下,时钟采样的波形事实——周期对齐采样、`inspect_handshake`、
  `suggest_protocol_bundles`、`sweep_handshakes`、`reconstruct_transactions`、
  `verify_window`、`diff_first_divergence`、`period`、`trace_x_source`、结构扫描——能直接定位到出问题
  的那一级和那一刻,而读源码或 grep 根本够不着。读源码是个很强的基线;TraceWeave
  的价值在于 **不透明症状,以及不可读或超大的设计。**

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
    ├── verdi_npi_backend.py      # NPI 后端实现的 driver/load 解析
    ├── kdb_builder.py            # 为 Xcelium 流程自动构建 Verdi KDB
    ├── structural_scanner.py
    ├── x_trace.py
    ├── cycle_query.py
    ├── schemas.py
    ├── problem_hints.py
    ├── hierarchy_handles.py      # HandleStore + build_tb_hierarchy 的内容寻址 handle
    ├── handle_tools.py           # get_tb_subtree / lookup_tb_files / find_tb_instance / ...
    ├── cursor_store.py           # 命名的进程内时间锚(cursor_set/list/delete)
    ├── timespec.py               # 将 @cursor / 带单位字面量(12.34ns)解析为 ps
    ├── verify_condition.py       # diff_first_divergence、period、inspect_handshake
    ├── window_verify.py          # verify_window:时钟窗口上的时序谓词
    ├── handshake_suggest.py      # suggest_handshakes / suggest_protocol_bundles
    ├── handshake_sweep.py        # sweep_handshakes:全设计握手异常扫描
    ├── txn_reconstruct.py        # reconstruct_transactions:id 关联的事务层
    └── usage_telemetry.py        # 仅本地的逐调用使用遥测(可关闭)
```

## 安装

TraceWeave 需要 Python `3.11+`。

```bash
pip install mcp pyyaml --user
```

要使用 FSDB,需要以下任一运行时:

- 仓库本地运行时:`third_party/verdi_runtime/linux64/libnsys.so` 与 `libnffr.so`
- 外部 Verdi 安装,通过 `VERDI_HOME/share/FsdbReader/linux64` 暴露

如果两者都不可用,TraceWeave 仍可运行,但 FSDB 解析会被禁用,工作流应优先使用 `.vcd` 波形。

启用 FSDB 支持(将 Verdi 运行时链接到仓库并构建 `libfsdb_wrapper.so`,一步完成):

```bash
# 示例 —— 请替换为你所在站点的 Verdi 安装路径
export VERDI_HOME=/tools/synopsys/verdi/O-2018.09-SP2-11
bash scripts/setup_fsdb.sh
```

验证运行时与 wrapper 是否能正确加载。该脚本不依赖 `$VERDI_HOME`,在已具备仓库本地产物的任何主机上都可以运行:

```bash
bash scripts/verify_fsdb.sh
```

## 客户端配置

### 通用 MCP 客户端

任何支持 stdio 传输的 MCP 客户端都能连接本服务器。最小配置:

- command:`python3.11`
- args:`["/home/robin/Projects/mcp/TraceWeave/server.py"]`
- env:如果需要 FSDB,提供仓库本地 `third_party/verdi_runtime/linux64` 或者 `VERDI_HOME`

如果客户端支持 server instructions,可以直接遵循内置工作流;否则参考下方手动工作流。

### Claude Code

Claude Code 与 Codex 都不会把你交互式 shell 的环境变量带入 spawn 出来的 MCP stdio 服务器,所以必须显式列出服务器需要的所有变量 —— 工具根目录,以及 `dlopen` 链(最容易遗漏的是 `LD_LIBRARY_PATH`;一旦缺失,NPI 会静默回退到 Static,`trace_signal_path` 会返回 `found: false`)。

在 `~/.claude.json` 中添加:

```json
{
  "mcpServers": {
    "TraceWeave": {
      "command": "python3.11",
      "args": ["/home/robin/Projects/mcp/TraceWeave/server.py"],
      "env": {
        "VERDI_HOME": "/tools/synopsys/verdi/V-2023.12-SP2",
        "NOVAS_HOME": "/tools/synopsys/verdi/V-2023.12-SP2",
        "VCS_HOME": "/tools/synopsys/vcs/V-2023.12-SP2",
        "XLM_ROOT": "/tools/cadence/XCELIUM2603",
        "CDS_INST_DIR": "/tools/cadence/XCELIUM2603",
        "SNPSLMD_LICENSE_FILE": "27000@synopsys-license.example.com",
        "LM_LICENSE_FILE": "5280@license-server.example.com",
        "CDS_LICENSE_FILE": "5280@cadence-license.example.com",
        "LD_LIBRARY_PATH": "/tools/synopsys/verdi/V-2023.12-SP2/share/PLI/IUS/LINUX64:/tools/synopsys/verdi/V-2023.12-SP2/share/PLI/VCS/LINUX64",
        "PATH": "/tools/synopsys/verdi/V-2023.12-SP2/bin:/tools/synopsys/vcs/V-2023.12-SP2/bin:/tools/synopsys/vcs/V-2023.12-SP2/amd64/bin:/tools/cadence/XCELIUM2603/tools/bin/64bit:/tools/cadence/XCELIUM2603/tools/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin"
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

思路与 Claude Code 相同 —— 全部显式列出。在 `~/.codex/config.toml` 中添加:

```toml
[mcp_servers.TraceWeave]
command = "python3.11"
args = ["/home/robin/Projects/mcp/TraceWeave/server.py"]
cwd = "/home/robin/Projects/mcp/TraceWeave"
env = {
  VERDI_HOME      = "/tools/synopsys/verdi/V-2023.12-SP2",
  NOVAS_HOME      = "/tools/synopsys/verdi/V-2023.12-SP2",
  VCS_HOME        = "/tools/synopsys/vcs/V-2023.12-SP2",
  XLM_ROOT        = "/tools/cadence/XCELIUM2603",
  CDS_INST_DIR    = "/tools/cadence/XCELIUM2603",
  SNPSLMD_LICENSE_FILE = "27000@synopsys-license.example.com",
  LM_LICENSE_FILE     = "28000@license-server.example.com",
  CDS_LICENSE_FILE    = "28000@cadence-license.example.com",
  LD_LIBRARY_PATH = "/tools/synopsys/verdi/V-2023.12-SP2/share/PLI/IUS/LINUX64:/tools/synopsys/verdi/V-2023.12-SP2/share/PLI/VCS/LINUX64",
  PATH            = "/tools/synopsys/verdi/V-2023.12-SP2/bin:/tools/synopsys/vcs/V-2023.12-SP2/bin:/tools/synopsys/vcs/V-2023.12-SP2/amd64/bin:/tools/cadence/XCELIUM2603/tools/bin/64bit:/tools/cadence/XCELIUM2603/tools/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin"
}
```

验证连接:

```bash
codex mcp list
# 应该显示 TraceWeave,Status: enabled
```

### 功能性验证

任一客户端连接成功后,运行一次端到端冒烟测试:

1. 在包含仿真日志与波形文件的工程目录里启动 `codex` 或 `claude`。
2. 直接发起波形调试请求,例如:"调用 TraceWeave MCP。先用 `get_sim_paths` 列出这个 case 的 logs 与 waves。"
3. 确认执行日志里出现真实的 MCP 工具调用,如 `get_sim_paths`、`parse_sim_log`、`search_signals`,而不是只通过 shell 命令手动读文件。

## 标准 MCP 工作流

这是仿真日志与波形调试的默认工作流:

1. 调用 `get_sim_paths(verif_root, case_name?)`。
2. 选择 `phase == "elaborate"` 的编译日志。
3. 在同一个编译日志上并行运行 `build_tb_hierarchy` 与 `scan_structural_risks`。
4. 如果有仿真日志,调用 `parse_sim_log`。
5. 使用 `recommend_failure_debug_next_steps` 或 `analyze_failure_event`。
6. 当需要针对显式信号的波形快照时,使用 `search_signals` 与 `analyze_failures`。
7. 对于更深入的调查,使用 `explain_signal_driver`、`trace_x_source` 或 `get_signals_by_cycle`。
8. 任何时候都可以使用 `get_diagnostic_snapshot` 查看可复用的缓存会话状态。

关键工作流规则:

- `scan_structural_risks` 是默认工作流的一部分,除非用户明确要求跳过,否则不应省略。
- `build_tb_hierarchy` 与 `scan_structural_risks` 必须使用同一个 `compile_log`。
- 优先使用 `parse_sim_log` 返回的 `failure_events[].time_ps` 作为波形时间锚点。
- 当 `fsdb_runtime.enabled == false` 时,优先选择 `.vcd` 而非 `.fsdb`。

## 工具速查

### 会话概览

- `get_diagnostic_snapshot`:只读地汇总缓存会话数据并给出下一步建议

### 路径与层次结构

- `get_sim_paths`:发现编译日志、仿真日志、波形、仿真器、case
- `build_tb_hierarchy`:服务端构建 testbench 层次结构;返回精简载荷(project、stats、深度 2 的 tree skeleton、interfaces、ambiguous_basenames、`hierarchy_handle`)。完整数据通过下方的 handle 工具按需获取。
- `scan_structural_risks`:扫描编译过的 RTL/TB 源码中的结构风险模式

### 层次结构 Handle 工具

下列工具均接收 `build_tb_hierarchy` 返回的 `hierarchy_handle`。当 handle 过期或未知时返回 `{"error": "handle_expired"}`;此时重新运行 `build_tb_hierarchy` 即可刷新。

- `get_tb_subtree(handle, root="", depth=1, max_nodes=500)`:从指定 dotted 实例路径切出 component_tree 子树。
- `lookup_tb_files(handle, ...)`:按客观扫描事实(`basename`、`name_contains`、`path_contains`、`has_module`、`contains_uvm`、`file_type`)查询编译文件集。至少需要一个过滤条件。对 `ambiguous_basenames` 中的多版本文件用 `basename=...` 精确消歧。
- `find_tb_instance(handle, path=... | module=...)`:按精确路径或某模块的所有实例定位。
- `get_tb_file_detail(handle, path)`:返回单个编译文件中定义的符号。未知路径返回 `file_not_in_compile_set` 与基于 basename 相似度的 `did_you_mean` 建议 —— 读取 RTL 前先核实文件确实在编译集中。
- `get_tb_class_hierarchy(handle, root_class?, depth=-1)`:从编译集扫描构建的 UVM/SV 类继承树。
- `dump_tb_section(handle, section)`:逃生通道,返回完整的原始 `compile_result`、`include_tree`、`filelist_tree`、`interfaces`、`files_full`、`component_tree_full` 或 `class_hierarchy_full`。优先使用上面的定向工具。

### 日志分析

- `parse_sim_log`:解析并归一化运行时失败,输出分组摘要与 `failure_events`;同时返回 `log_snapshot_id`,用于仿真器覆盖同名日志后的前后对比。
- `diff_sim_failure_results`:按路径或 `base_snapshot_id` / `new_snapshot_id` 对比两次仿真运行。若前一次已对同一路径调用过 `parse_sim_log`,后续只传 `new_log_path` 时会自动使用上一轮解析快照作为 baseline。
- `get_error_context`:抽取指定行号附近的原始日志上下文

### 波形分析

- `search_signals`:解析完整层次化信号路径。每条结果还附带 `direction`(`input`/`output`/`inout`/`implicit`/`null`)与 `var_type`(`wire`/`reg`/`integer`/`real`/`parameter`/…),客户端无需额外工具就能在指定 scope 内过滤端口/线网/变量。**FSDB** 两个字段都会填;**VCD** 只填 `var_type`,`direction` 返回 `null`(VCD 格式不编码端口方向)
- `get_signal_at_time`:查询信号在指定时间点的值
- `get_signal_transitions`:取出某段时间内信号的所有跳变
- `get_signals_around_time`:取出失败时间点附近的上下文
- `get_signals_by_cycle`:按时钟沿逐周期采样信号
- `get_waveform_summary`:返回波形元数据

### 游标与验证原语

`get_signal_at_time`、`get_signal_transitions`、`get_signals_around_time`、`trace_x_source`、`diff_first_divergence` 的时间入参接受 **TimeSpec**:整数(ps)、游标引用 `@<name>`、或带单位的字面量(如 `12.34ns` / `5us`)。

- `cursor_set(name, time_ps, note?)` / `cursor_list()` / `cursor_delete(name)`:命名的、进程内的时间锚。定位到某时刻的工具(如 `diff_first_divergence`、`period`)会自动注册一个游标,后续可用 `@<name>` 引用,免去跨调用复制 ps 时间戳。游标不持久化——server 重启即丢。
- `diff_first_divergence(wave_path_a, signal_a, wave_path_b, signal_b, ...)`:两个波形信号首次取值不相等的时刻——可跨两个波形(如 passing vs failing run),也可在同一波形内(两个本应相等的信号,如 lockstep / shadow 寄存器)。在分叉处自动注册游标。要求两侧都是被 dump 的波形信号(它不与软件参考模型比对)。
- `period(wave_path, signal, edge?, ...)`:测信号边沿的主导周期,并标出第一个偏离该周期的拍(off-beat),自动注册为游标。用于"这个信号本应周期性——节奏第一次在哪里破"(时钟、strobe、定速 valid)。
- `suggest_handshakes(wave_path, scope?, ...)`:扫描波形,提出可直接使用的 `inspect_handshake` bundle —— 按 scope 与 stem 配对 `*valid`/`*ready`、找到时钟、归组通道 payload 总线。先跑它,就不用手攒 `{clock, valid, ready, payload}`。覆盖 AXI/通用 valid-ready 与 req/ack。
- `suggest_protocol_bundles(wave_path, protocol=ahb|apb, scope?, ...)`:扫描没有字面 `valid` 的协议 bundle。AHB candidate 会返回可直接传给 `inspect_handshake` 的 `valid_htrans`、`ready` 与 payload;APB candidate 返回 `psel`/`penable`/`pready` 事实,并明确标出 `inspect_handshake` 仍需要 `psel && penable` 的派生 valid 信号。方向标签只来自 discovery 层的机械事实(`initiator_side` / `responder_side` / `unknown`),推不出或冲突时返回 unknown,不硬猜。
- `inspect_handshake(wave_path, clock, valid, ready, payload?, ...)`:对时钟化 valid/ready 握手逐拍分类 —— stall 连续段(valid 高、ready 低)、最长/超阈值 stall、背压失衡(ready 高、valid 低),以及给了 `payload` 时的保持违例(transfer 仍在 stall 期间 payload 发生变化)。协议无关:AXI `*valid`/`*ready`、通用 valid-ready 流、credit 接口。AHB 没有字面 valid —— 传 `valid_htrans=<htrans 路径>`(及 `htrans_rule`:`active`=NONSEQ/SEQ,或 `non_idle`)即可派生出 valid(`payload`=haddr/hwrite/hsize,它们在 hready 低时必须保持)。在第一个问题处自动注册游标(保持违例 > 长 stall > 最长 stall)。给出在 scoreboard 日志里不留值规律的协议时序事实。
- `sweep_handshakes(wave_path, scope?, ...)`:全设计握手**异常扫描** —— 一次调用发现每个 valid/ready 接口并逐个在窗口内 inspect,返回一张对比事实表(各接口的 stall、死锁特征、payload 保持、背压),按透明的机械键排序。用于不透明的全局症状(timeout/hang)、当你还不知道众多接口里哪个出问题时;它把 N 次 `suggest_handshakes`+`inspect_handshake` 往返压缩成一次。返回事实而非根因裁决——按症状自行重排。在背压流水线上,最长 stall 排序给出的是传播前沿,根因则在 stall→断粮(starvation)的边界。当发现的接口数超过 `max_interfaces`(默认 64)时会(响亮地)置 `truncated=true`。
- `verify_window(wave_path, clock, mode, predicate | antecedent+consequent, ...)`:在时钟窗口上求值一个时序谓词,返回精确的 `holds` 判定 + 具体的见证/反例(周期 + 采样值)。是模板而非 DSL:一个 *term* 是 `{signal, op, value}`(`op`:eq/ne/gt/ge/lt/le/is_x/is_known);一个 *predicate* 是 term 列表(隐式 AND);`mode` 为 `always` / `never` / `eventually` / `implication`(A ⊦→ B 在 N 周期内)。x/z 周期报为 `unknown`(绝不静默当通过),响应窗口越过 trace 末尾的 implication 报为 `inconclusive`(绝不静默当失败)。用于一次调用证实或证伪一个 RTL 推断。
- `reconstruct_transactions(wave_path, clock, req_valid, req_ready, cmp_valid, cmp_ready, ...)`:从两个握手通道重构 id 关联的请求/响应事务 —— 按 `id` 字段把被接受的请求 beat 配对到完成 beat,返回每笔事务的延迟与聚合事实(outstanding 曲线含 per-id 峰值、乱序、unmatched=挂死特征)。一个通用核,而非每协议一个工具:AXI 读 = AR→R(`req_id`=arid,`cmp_id`=rid,`cmp_last`=rlast);AXI 写 = AW→B 外加可选的无索引 W 数据通道(`data_valid`/`data_ready`/`data_last` + `data_fields`)。`req_id`/`cmp_id` 可选 —— 两者都省略即为无索引的在序流(AXI-Lite、APB),按 FIFO 顺序配对。可选的 `reset` 会清空在途状态,使跨 reset 的事务不被误报为挂死。`latency` 是分布而非"异常值"裁决;支持跨 id 的乱序完成。

### 深入分析

- `analyze_failures`:聚焦某个分组失败,返回日志与波形上下文
- `analyze_failure_event`:针对一个 `failure_event`,给可能的实例、源文件、信号排序
- `recommend_failure_debug_next_steps`:返回默认的下一步调试目标
- `explain_signal_driver`:把波形信号回溯到可能的 RTL 驱动逻辑
- `find_signal_loads`:列出信号的消费者(fanout)—— 模块输入端口、RHS 使用、always 块敏感列表
- `trace_signal_path`:在 elaborated netlist 中查找两个信号之间的连通路径(仅 NPI)。返回的是连通性,**不是**时序意义上的 driver 方向 —— driver 语义请用 `explain_signal_driver`。没有 Verdi KDB 时该工具会返回 `unsupported_reason="static_backend_no_path_api"`,因为源码正则无法诚实地还原 `sig_to_sig_conn_list`;此时回退到 `explain_signal_driver` + `find_signal_loads`。
- `trace_x_source`:向上游追溯 X/Z 传播
- `build_kdb`:从已解析的编译日志自动构建 Verdi KDB(vericom + elabcom)。当仿真器是 Xcelium(xrun)且 NPI 后端报告无 KDB 时使用。输出缓存到 `TRACEWEAVE_CACHE_DIR`(默认 `~/.cache/traceweave/kdb/<hash>/`);缓存命中则跳过 Verdi 重跑。KDB 旁边会写出一个可运行的 `build.sh` 便于检查或手动复现。需要 `VERDI_HOME` 中含有 `bin/vericom` 与 `bin/elabcom`。

当检测到 KDB 时,`explain_signal_driver`、`find_signal_loads`、`trace_signal_path` 会自动启用 Verdi NPI 后端。前两个在 NPI 不可用时透明回退到基于源码正则的 Static 后端;`trace_signal_path` 是 NPI-only,会返回结构化的 `unsupported_reason` 而不是给出近似结果,因为 `sig_to_sig_conn_list` 没有诚实的 Static 等价实现。NPI 是更深、更准确的路径:它使用 `fan_in_reg_list` / `sig_to_sig_conn_list` 在 elaborated netlist 上行走,因此能跨越实例端口边界、interface 位置绑定与 assign 链,这些 Static 都跟不过去。当 KDB 存在时,`build_tb_hierarchy` 还会把 component-tree 每个节点的 `source_file` / `source_line` 覆盖为 NPI 给出的 elaborated `file:line`;`find_driver` / `find_loads` 中受影响的 hop 会带上 `source_info_origin: "npi"` 标签,便于消费者区分 NPI 标注条目与编译日志衍生条目。结果信封里带一个 `backend_status` 块,包含当前后端、KDB 流程,以及按仿真器给出的 `kdb_hint`。

对 VCS 流程,获取 KDB 的最低成本方式是用 `-kdb=only` 重编 —— hint 会给出完整命令。对 Xcelium 流程没有原生 KDB;`get_diagnostic_snapshot` 会把 `build_kdb` 列在 `missing_steps` 中,LLM agent 可以按需触发。设置 `TRACEWEAVE_AUTO_KDB=0` 可关闭自动构建提示。

### 使用遥测

TraceWeave 会为每次工具调用向 `$TRACEWEAVE_CACHE_DIR/telemetry/usage.jsonl`(默认 `~/.cache/traceweave/telemetry/`)追加一行 JSONL —— 工具名、参数的 *键* 与少量标量 flag(绝不记参数值或路径)、结果大小、延迟,以及锚定到每次 `get_sim_paths` case 的 session id。**仅本地**(不发送到任何地方),用于量化哪些工具真正被用到。默认开启;设置 `TRACEWEAVE_TELEMETRY=0` 可关闭。用 `python scripts/telemetry_report.py` 汇总。

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
