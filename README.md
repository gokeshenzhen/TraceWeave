# Waveform Analysis MCP

## 文件结构

```
waveform_mcp/
├── config.py               ← ★ 所有路径/文件名常量，换项目只改这里
├── server.py               ← MCP 主入口
├── custom_patterns.yaml    ← 工程师自定义报错格式（不改代码，改此文件）
├── requirements.txt
└── src/
    ├── vcd_parser.py       ← VCD 纯 Python 解析
    ├── fsdb_parser.py      ← FSDB 信号值查询（libnffr.so）
    ├── fsdb_signal_index.py← FSDB 信号路径搜索（scope 树索引，GB 级友好）
    ├── log_parser.py       ← VCS+Xcelium assertion fail + UVM_ERROR/FATAL
    └── analyzer.py         ← log + 波形联合分析
```

---

## 安装

```bash
pip install mcp pyyaml --user
```

验证 Verdi 库可以加载：
```bash
source ~/.bashrc
python3 -c "
import ctypes, os
d = os.environ['VERDI_HOME'] + '/share/FsdbReader/linux64'
ctypes.CDLL(d + '/libnsys.so', ctypes.RTLD_GLOBAL)
ctypes.CDLL(d + '/libnffr.so')
print('Verdi 库加载 OK')
"
```

---

## Claude Code 全局配置

编辑 `~/.claude.json`，添加 mcpServers 段：

```json
{
  "mcpServers": {
    "waveform": {
      "command": "python3",
      "args": ["/home/robin/Projects/waveform_mcp/server.py"],
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

---

## Claude Code 自主 debug 工作流

当你说：**"帮我开发 case0，自主 debug 直到通过"**

Claude 会自动执行以下循环：

```
1. 读 eda-environment skill  →  知道工具路径
2. 读 design/ 下 RTL 代码    →  理解设计逻辑
3. 读 vplan                  →  知道 case0 验证目标
4. 生成 testcase/case0.sv
5. 调用 bash: make SV_CASE=case0
6. 调用 MCP: get_sim_paths(verif_root, "case0")  →  拿到 log/波形路径
7. 调用 MCP: parse_sim_log(irun.log)              →  提取所有报错
8. 调用 MCP: search_signals(top_tb.fsdb, "s_bits") →  找完整信号路径
9. 调用 MCP: analyze_failures(log, fsdb, signals)  →  波形 + 报错联合分析
10. 根据分析结果修改 case0.sv 或 RTL
11. 重新执行 make，回到步骤 6
    直到 parse_sim_log 返回 total_errors = 0
```

---

## 工具速查

| 工具 | 典型使用场景 |
|------|-------------|
| `get_sim_paths` | 第一步，拿所有标准路径 |
| `parse_sim_log` | 快速看有没有报错 |
| `search_signals` | 从 RTL 信号名找波形完整路径 |
| `analyze_failures` | 核心：报错 + 波形联合分析 |
| `get_signal_at_time` | 查特定时刻单个信号值 |
| `get_signal_transitions` | 查信号完整跳变历史 |
| `get_signals_around_time` | 查多个信号在某时刻的快照 |
| `get_waveform_summary` | 查波形文件基本信息 |

---

## 添加自定义报错格式

编辑 `custom_patterns.yaml`，在 `patterns:` 下追加：

```yaml
patterns:
  - name: my_bus_checker
    severity: ERROR
    description: "自定义总线协议 checker"
    regex: 'BUS_ERROR\s+\[(?P<message>[^\]]+)\]\s+@\s+(?P<time>[\d.]+)\s*(?P<time_unit>ns|ps)'
```

必须包含命名捕获组 `(?P<message>...)` 和可选的 `(?P<time>...)` `(?P<time_unit>...)`。
修改后**无需重启**，下次调用 `parse_sim_log` 时自动生效。

---

## config.py 说明

换项目时只需修改 `config.py` 中对应的常量：

```python
# 如果波形文件名不同
WAVE_FILE_NAME = "top_tb.fsdb"   # ← 改这里

# 如果仿真 log 名不同
SIM_LOG_NAME = "irun.log"        # ← 改这里

# 如果 work 目录前缀不同
WORK_CASE_PREFIX = "work_"       # ← 改这里
```

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
全部 passed → 重启 Claude Code → 让 Claude 使用 MCP
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
