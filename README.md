# Waveform Analysis MCP

## Architecture

- Architecture map: `docs/architecture.md`
- New session bootstrap: read `AGENTS.md` first, then follow its first-read file list
- Fast path for code understanding:
  - `server.py`
  - `config.py`
  - `src/analyzer.py`
  - `src/log_parser.py`
  - `src/fsdb_parser.py`

## 文件结构

```
waveform_mcp/
├── config.py               ← 环境变量、默认行为和发现规则常量
├── server.py               ← MCP 主入口
├── custom_patterns.yaml    ← 工程师自定义报错格式（不改代码，改此文件）
├── requirements.txt
└── src/
    ├── path_discovery.py   ← 仿真产物路径自动发现
    ├── vcd_parser.py       ← VCD 纯 Python 解析
    ├── fsdb_parser.py      ← FSDB 信号值查询（libnffr.so）
    ├── fsdb_signal_index.py← FSDB 信号路径搜索（scope 树索引，GB 级友好）
    ├── log_parser.py       ← failure_event 归一化、group 摘要和 run diff
    ├── analyzer.py         ← failure_event + 波形 + hierarchy 联合分析与推荐
    └── signal_driver.py    ← 从波形信号路径回溯最可能的 RTL 驱动位置
```

---

## 安装

需要 Python `3.11+`。

```bash
pip install mcp pyyaml --user
```

如果需要 FSDB 解析，有两种运行时来源：

- 仓库本地 runtime：`third_party/verdi_runtime/linux64/libnsys.so` 和 `libnffr.so`
- 外部 Verdi 安装：通过 `VERDI_HOME` 提供 `share/FsdbReader/linux64`

如果两者都没有，本 MCP 仍可工作，但**不支持 FSDB 解析**；后续工作流应优先使用 `.vcd` 波形。

准备仓库本地 runtime：
```bash
export VERDI_HOME=/tools/synopsys/verdi/O-2018.09-SP2-11
bash scripts/link_verdi_runtime.sh
```

目录约定见 [`third_party/verdi_runtime/README.md`](/home/robin/Projects/mcp/waveform_mcp/third_party/verdi_runtime/README.md)。

验证 FSDB runtime 可以加载：
```bash
python3 -c "
import ctypes, os
d = 'third_party/verdi_runtime/linux64'
ctypes.CDLL(d + '/libnsys.so', ctypes.RTLD_GLOBAL)
ctypes.CDLL(d + '/libnffr.so')
print('FSDB runtime 加载 OK')
"
```

---

## Client Setup

### Generic MCP Client

任何支持 stdio transport 的 MCP client 都可以接这个 server。最小接入要素是：

- command: `python3`
- 建议实际使用 `python3.11`
- args: `["/home/robin/Projects/mcp/waveform_mcp/server.py"]`
- env: 如果不提供本地 `third_party/verdi_runtime/linux64`，则至少显式提供 `VERDI_HOME`。没有这两者时仅支持 VCD，不支持 FSDB。

如果客户端本身支持 server instructions，它会直接拿到本仓库内置的标准调试 workflow；否则也可以按下面的 `Standard MCP Workflow` 手动编排工具调用。

### Claude Code

编辑 `~/.claude.json`，添加 mcpServers 段：

```json
{
  "mcpServers": {
    "waveform": {
      "command": "python3.11",
      "args": ["/home/robin/Projects/mcp/waveform_mcp/server.py"],
      "env": {
        "VERDI_HOME": "/tools/synopsys/verdi/O-2018.09-SP2-11",
        "VCS_HOME":   "/tools/synopsys/vcs/O-2018.09-SP2-11",
        "XLM_ROOT":   "/tools/cadence/XCELIUM1803",
        "PATH": "/tools/synopsys/verdi/O-2018.09-SP2-11/bin:/tools/synopsys/vcs/O-2018.09-SP2-11/bin:/tools/cadence/XCELIUM1803/tools/bin:/usr/local/bin:/usr/bin:/bin"
      }
    }
  }
}
```

> 必须在 env 里显式写环境变量，Claude Code 不会自动 source ~/.bashrc

配置后验证：
```bash
claude mcp list
# 应显示 waveform (connected)
```

### Codex

编辑 `~/.codex/config.toml`，添加以下配置：

```toml
[mcp_servers.waveform]
command = "python3.11"
args = ["/home/robin/Projects/mcp/waveform_mcp/server.py"]
cwd = "/home/robin/Projects/mcp/waveform_mcp"

[mcp_servers.waveform.env]
VERDI_HOME = "/tools/synopsys/verdi/O-2018.09-SP2-11"
VCS_HOME   = "/tools/synopsys/vcs/O-2018.09-SP2-11"
XLM_ROOT   = "/tools/cadence/XCELIUM1803"
PATH       = "/tools/synopsys/verdi/O-2018.09-SP2-11/bin:/tools/synopsys/vcs/O-2018.09-SP2-11/bin:/tools/cadence/XCELIUM1803/tools/bin:/usr/local/bin:/usr/bin:/bin"
```

如果 `~/.codex/config.toml` 已存在其他内容，只追加 `mcp_servers.waveform` 这一段即可，不要覆盖已有配置。

配置后验证：

```bash
codex mcp list
# 应显示 waveform，且 Status 为 enabled
```

建议再做一次功能验证：

1. 在一个包含 `verif/`、sim log 和 wave 的工程目录启动 `codex`
2. 直接提一个明确的波形调试请求，例如“调用 waveform MCP，先用 get_sim_paths 看这个 case 的日志和波形”
3. 确认执行日志里实际出现了 `get_sim_paths`、`parse_sim_log`、`search_signals` 等 MCP tool 调用，而不是只用 shell 手工读文件

---

## Standard MCP Workflow

这是当前 MCP server 实际支持的通用调试链路，适用于 Codex、Claude Code 等支持 MCP 的客户端：

1. 调用 `get_sim_paths(verif_root, case_name?)`，自动发现 `compile_logs` / `sim_logs` / `wave_files` / `simulator`
   返回里还包含 `discovery_mode` 和可能的 `case_dir`
2. 选 `phase == "elaborate"` 的 compile log，调用 `build_tb_hierarchy`
3. 如果 `sim_logs` 非空，用 `sim_logs[0].path` 和 `simulator` 调用 `parse_sim_log`
   当前返回不仅有 `groups`，还包含版本字段、runtime-only 计数器、标准化后的 `failure_events`、时间归一化字段，以及 rerun diff hints
4. 选择波形文件：
   如果 `fsdb_runtime.enabled == false`，优先选 `.vcd`；否则可用 `.fsdb` 或 `.vcd`
5. 优先走 failure-event 中心流：
   - 优先使用 `failure_events[0].time_ps` 作为波形时间锚点
   - 用 `failure_events[0]` 或选中的 event 调用 `analyze_failure_event`
   - 或直接调用 `recommend_failure_debug_next_steps`
6. 需要指定信号和单 group 快照时，再用 `search_signals` + `analyze_failures`
7. 如 `parse_sim_log` 返回 `previous_log_detected == true`，优先考虑调用 `diff_sim_failure_results`
8. 波形上看到可疑信号后，可调用 `explain_signal_driver`
9. 必要时补充 `get_error_context`、`get_signal_transitions`、`get_signals_around_time`、`get_signal_at_time`、`get_waveform_summary`

推荐的默认顺序：

1. `get_sim_paths`
2. `build_tb_hierarchy`
3. `parse_sim_log`
4. `recommend_failure_debug_next_steps` 或 `analyze_failure_event`
5. 必要时 `search_signals` + `analyze_failures`
6. 波形异常时 `explain_signal_driver`
7. 迭代调试时 `diff_sim_failure_results`

### Client Integration Example

客户端如果额外具备工程构建、RTL 读取、testcase 修改能力，可以在上面的 MCP 工作流外层再包一层自动化循环。例如：

1. 读取工程 RTL / testbench / vplan
2. 生成或修改 testcase / RTL
3. 运行仿真命令，例如 `make SV_CASE=case0`
4. 回到上面的 MCP 工作流做日志和波形分析
5. 根据分析结果继续修改并重跑

---

## 工具速查

| 工具 | 典型使用场景 |
|------|-------------|
| `get_sim_paths` | 第一步，自动发现 compile/sim/wave 路径，或列出可用 case |
| `parse_sim_log` | 快速拿到 group 摘要、标准化 `failure_events`、时间归一化字段和 rerun hints |
| `diff_sim_failure_results` | 比较两次仿真的已解决 / 持续 / 新增失败 |
| `search_signals` | 从 RTL 信号名找波形完整路径；`.fsdb` 可用性受 `fsdb_runtime.enabled` 约束 |
| `analyze_failures` | 核心：报错 + 波形联合分析；`.fsdb` 可用性受 `fsdb_runtime.enabled` 约束 |
| `analyze_failure_event` | 从单个 `failure_event` 出发，联动实例、信号和源码候选 |
| `recommend_failure_debug_next_steps` | 用户只说“调这个失败”时，给出默认优先看哪个失败/信号/实例，并附 role-based 排名理由 |
| `explain_signal_driver` | 从可疑波形信号回溯最可能的 RTL driver 位置和驱动类型 |
| `get_signal_at_time` | 查特定时刻单个信号值；`.fsdb` 可用性受 `fsdb_runtime.enabled` 约束 |
| `get_signal_transitions` | 查信号完整跳变历史；`.fsdb` 可用性受 `fsdb_runtime.enabled` 约束 |
| `get_signals_around_time` | 查多个信号在某时刻的快照；`.fsdb` 可用性受 `fsdb_runtime.enabled` 约束 |
| `get_waveform_summary` | 查波形文件基本信息；`.fsdb` 可用性受 `fsdb_runtime.enabled` 约束 |

### `parse_sim_log` 关键字段

- `schema_version` / `contract_version` / `failure_events_schema_version`：响应和 `failure_events` 的版本信息
- `parser_capabilities`：当前 parser build 支持的结构化提取能力
- `runtime_total_errors` / `runtime_fatal_count` / `runtime_error_count`：runtime-only 顶层计数器
- `parser_capabilities` 当前可包含 `mixed_log_detection`，表示 mixed compile+runtime log 也只返回 runtime failures
- `failure_events[].raw_time`：log 中原始时间 token
- `failure_events[].raw_time_unit`：归一化后的单位，可能是 `ps/ns/us/ms/s/ticks/unknown`
- `failure_events[].time_ps`：归一化后的 ps 时间；缺失时为 `null`
- `failure_events[].time_parse_status`：`exact` / `inferred` / `missing`
- `failure_events[].log_phase`：当前固定为 `runtime`
- `failure_events[].failure_source`：失败来源分类，如 `assertion` / `scoreboard` / `checker`
- `failure_events[].failure_mechanism`：失败机理分类，如 `protocol` / `mismatch` / `timeout`
- `failure_events[].transaction_hint`：从 log 文本或结构化字段提取的事务提示
- `failure_events[].expected` / `failure_events[].actual`：优先从结构化字段，其次从 message 中提取
- `failure_events[].missing_fields`：当前 event 里相关但未成功提取的一等字段
- `failure_events[].field_provenance`：每个非空一等字段的来源，取值为 `observed` / `derived` / `heuristic`
- `previous_log_detected` / `candidate_previous_logs` / `suggested_followup_tool`：rerun-aware diff hint

### `recommend_failure_debug_next_steps` 关键新增字段

- `recommendation_strategy`：当前为 `role_rank_v1`
- `failure_window_center_ps`：本次推荐所围绕的失败时间
- `recommended_signals[]` 额外包含 `role`、`reason_codes`、`confidence`

---

## 添加自定义报错格式

编辑 `custom_patterns.yaml`，在 `patterns:` 下追加：

```yaml
patterns:
  - name: my_bus_checker
    severity: ERROR
    description: "自定义总线协议 checker"
    regex: 'BUS_ERROR\s+\[(?P<message>[^\]]+)\]\s+src=(?P<source_file>\S+)\s+line=(?P<source_line>\d+)\s+inst=(?P<instance_path>\S+)\s+@\s+(?P<time>[\d.]+)\s*(?P<time_unit>ns|ps)'
```

建议至少包含命名捕获组 `(?P<message>...)`，并尽量补充：

- `(?P<time>...)` / `(?P<time_unit>...)`
- `(?P<source_file>...)` / `(?P<source_line>...)`
- `(?P<instance_path>...)`

这些字段会直接进入标准化 `failure_event`，提升 `analyze_failure_event` 和 `recommend_failure_debug_next_steps` 的效果。

修改后**无需重启**，下次调用 `parse_sim_log` 时自动生效。

---

## 路径发现说明

`get_sim_paths` 不再依赖硬编码的 `verif/work/work_<case>/` 目录结构，而是按目录语义自动识别：

```python
COMPILE_LOG_PATTERNS = ["*comp*.log", "*elab*.log"]
SIM_LOG_PATTERNS = ["*run*.log", "xm*.log", "sim*.log", "vcs.log"]
WAVE_PATTERNS = ["*.fsdb", "*.vcd"]
```

返回结果包含：

- `discovery_mode`：`root_dir` / `case_dir` / `unknown`
- `case_dir`：当前锁定的 case 目录；没有锁定时为 `null`
- `compile_logs` / `sim_logs` / `wave_files`：候选文件列表
- `simulator`：根据找到的日志自动识别 `vcs` 或 `xcelium`
- `fsdb_runtime`：当前是否支持 FSDB 解析，以及 runtime 来源
- `available_cases`：当输入是 root 目录且不传 `case_name` 时返回可用 case 目录
- `hints`：缺失文件、空日志、过小波形、老文件等可操作提示

当前自动发现的核心规则：

- 如果 `verif_root` 目录本身直接包含 sim log 或 waveform，则视为 `case_dir`
- 如果 `verif_root` 本身不含 sim/wave，但一级子目录里有，则视为 `root_dir`
- `sim_logs` / `wave_files` 始终严格绑定到一个 case，不会再混入其他 case 的文件
- `compile_logs` 允许共享编译回退：
  - `root_dir + case_name`：先 root 顶层，再目标 case 本地
  - `case_dir`：先当前目录，再父目录顶层
- 如果目录结构无法识别，会返回 `discovery_mode = "unknown"` 和明确 `hints`

最常见的返回示例：

```json
{
  "verif_root": "/path/to/verif/work",
  "case_name": "case0",
  "config_source": "auto",
  "discovery_mode": "root_dir",
  "case_dir": "/path/to/verif/work/work_case0",
  "simulator": "xcelium",
  "fsdb_runtime": {
    "enabled": true,
    "source": "verdi_home",
    "lib_dir": "/tools/.../share/FsdbReader/linux64",
    "missing_libs": [],
    "message": "Using FSDB runtime from VERDI_HOME=/tools/..."
  },
  "compile_logs": [
    {
      "path": "/path/to/verif/work/elab.log",
      "phase": "elaborate",
      "size": 52340,
      "mtime": "2026-03-14 10:30:00",
      "age_hours": 2.5
    }
  ],
  "sim_logs": [
    {
      "path": "/path/to/verif/work/work_case0/irun.log",
      "size": 88000,
      "mtime": "2026-03-14 11:00:00",
      "age_hours": 2.0
    }
  ],
  "wave_files": [
    {
      "path": "/path/to/verif/work/work_case0/top_tb.fsdb",
      "format": "fsdb",
      "size": 1200000000,
      "mtime": "2026-03-14 11:00:00",
      "age_hours": 2.0
    }
  ],
  "available_cases": [],
  "hints": []
}
```

如果项目结构固定，也可以在 `verif/.mcp.yaml` 中显式指定模板：

```yaml
compile_log: work/elab.log
case_dir: work/work_{case}
sim_log: irun.log
wave_file: top_tb.fsdb
```

注意：

- `root_dir` 场景下不传 `case_name` 时，`sim_logs` / `wave_files` 会保持为空，客户端应先看 `available_cases`
- 如果传入的是某个 case 目录，`case_name` 可以省略
- `fsdb_runtime.enabled == false` 时，`.fsdb` 文件虽然仍会被发现，但后续工具调用应优先选 `.vcd`
- `build_wrapper.sh` 只在重新编译 `libfsdb_wrapper.so` 时需要 `VERDI_HOME`

---

## 单元测试

### 测试文件结构

```
tests/
├── conftest.py          ← pytest 路径配置，自动加载
├── test_log_parser.py   ← log 解析器测试
├── test_fsdb_parser.py  ← FSDB 波形解析器测试
└── test_analyzer.py     ← 联合分析器端到端测试
```

### 各测试文件职责

**`test_log_parser.py`**
- 不依赖任何外部文件，内置真实 log 片段直接测试
- 覆盖：VCS assertion fail 正则、Xcelium assertion fail 正则、UVM_ERROR/FATAL 解析、时间单位换算（ns→ps）、报错按时间排序
- 同时用真实 `run.log` 做集成验证，断言 UVM_ERROR 数量与 log 末尾汇总一致

**`test_fsdb_parser.py`**
- 依赖真实 `top_tb.fsdb`，验证 C++ wrapper 调用链路完整
- 覆盖：信号搜索（关键字匹配、大小写不敏感、不存在信号返回空）、指定时刻值查询（断言已知时刻的已知值）、跳变列表（数量、排序、字段完整性）、多信号时间窗口查询

**`test_analyzer.py`**
- 端到端测试，log + FSDB 联合分析全流程
- 覆盖：返回结构完整性、每条报错都有波形快照和完整历史、历史数据时间戳不超过报错时刻、具体报错内容和信号值正确性

### 运行方式

```bash
# 安装 pytest（只需一次）
pip3.11 install pytest --user

# 在 waveform_mcp/ 目录下运行全部测试
cd /home/robin/Projects/mcp/waveform_mcp
python3.11 -m pytest tests/ -v

# 只跑某个文件
python3.11 -m pytest tests/test_log_parser.py -v

# 只跑某个测试类
python3.11 -m pytest tests/test_fsdb_parser.py::TestGetTransitions -v
```

### 修改代码后的标准流程

```
修改代码
    ↓
python3.11 -m pytest tests/ -v
    ↓
全部 passed → 重启客户端 → 让客户端重新连接 MCP
    ↓
有 FAILED  → 看报错信息 → 修复代码 → 重新跑测试
```

### 时间单位换算备忘

测试中写断言时注意：

| log 中的时间 | 换算结果 |
|---|---|
| `1661.000 ns` | `1661000 ps`（× 1000）|
| `270000 ps` | `270000 ps`（不变）|
| `270 NS`（Xcelium）| `270000 ps`（× 1000）|
