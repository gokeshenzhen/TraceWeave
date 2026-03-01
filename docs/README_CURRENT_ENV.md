# 当前环境配置摘要

## 环境信息
- **操作系统**: Linux 4.18
- **Python**: 3.11 (`/usr/bin/python3.11`)
- **VERDI_HOME**: `/home/eda/app/synopsys/verdi/W-2024.09-SP1`
- **VCS_HOME**: `/home/eda/app/synopsys/vcs/W-2024.09-SP1`

## 已完成配置

### 1. ✓ 配置文件更新
- [`config.py`](config.py:12) - 更新 VERDI_HOME 默认路径
- [`build_wrapper.sh`](build_wrapper.sh:8) - 更新编译脚本默认路径

### 2. ✓ 依赖安装
```bash
python3.11 -m pip install mcp pyyaml --user
```

### 3. ✓ FSDB Wrapper 编译
```bash
bash build_wrapper.sh
# 生成：libfsdb_wrapper.so
```

### 4. ✓ 环境验证
```bash
python3.11 verify_setup.py
# 结果：所有检查通过 ✓
```

## 快速开始

### 方式 1：集成到 Claude Code
```bash
# 复制配置文件到 Claude Code
cp claude_config_example.json ~/.claude.json

# 重启 Claude Code 或执行
claude mcp list
# 应该看到 waveform (connected)
```

### 方式 2：命令行测试
```bash
cd /home/eda/project/waveform_mcp

# 启动 MCP 服务器（会监听 stdio）
python3.11 server.py
```

## 可用工具

| 工具名称 | 功能说明 |
|---------|---------|
| `get_sim_paths` | 获取项目所有标准路径（log、波形等） |
| `parse_sim_log` | 解析仿真 log（VCS/Xcelium） |
| `search_signals` | 在波形中搜索信号路径 |
| `get_signal_at_time` | 查询信号在特定时刻的值 |
| `get_signal_transitions` | 获取信号跳变历史 |
| `get_signals_around_time` | 获取多信号时间窗口快照 |
| `analyze_failures` | **核心**：log + 波形联合分析 |

## 使用示例

在 Claude Code 中，你可以直接对话：

**示例 1：解析仿真 log**
```
你：解析 /path/to/verif/work/work_case0/irun.log，看有什么错误
```

**示例 2：搜索信号**
```
你：在 /path/to/top_tb.fsdb 中搜索包含 'clk' 的信号
```

**示例 3：自动 debug**
```
你：分析 case0 的失败原因
    - verif 目录：/home/user/project/verif
    - 关注信号：top_tb.dut.state, top_tb.dut.error_flag
```

Claude 会自动调用 `get_sim_paths` → `parse_sim_log` → `search_signals` → `analyze_failures`，生成根因分析报告。

## 文件说明

| 文件 | 说明 |
|------|------|
| [`config.py`](config.py:1) | **核心配置**：换项目只改这里 |
| [`server.py`](server.py:1) | MCP 服务器主入口 |
| [`custom_patterns.yaml`](custom_patterns.yaml:1) | 自定义报错格式（无需改代码） |
| [`requirements.txt`](requirements.txt:1) | Python 依赖列表 |
| [`verify_setup.py`](verify_setup.py:1) | 环境验证脚本 |
| [`claude_config_example.json`](claude_config_example.json:1) | Claude Code 配置示例 |
| [`SETUP_GUIDE.md`](SETUP_GUIDE.md:1) | 详细安装与故障排查指南 |
| `src/` | 核心解析器模块 |
| `tests/` | 单元测试 |

## 验证命令

```bash
# 完整环境验证
python3.11 verify_setup.py

# 快速验证依赖
python3.11 -c "from mcp.server import Server; import yaml; print('OK')"

# 验证 FSDB 库
python3.11 -c "
import ctypes, os
ctypes.CDLL('libz.so.1', ctypes.RTLD_GLOBAL)
lib_dir = '/home/eda/app/synopsys/verdi/W-2024.09-SP1/share/FsdbReader/linux64'
ctypes.CDLL(lib_dir + '/libnsys.so', ctypes.RTLD_GLOBAL)
ctypes.CDLL(lib_dir + '/libnffr.so')
print('FSDB OK')
"
```

## 下一步

1. **配置 Claude Code**
   ```bash
   cp claude_config_example.json ~/.claude.json
   ```

2. **准备测试数据**（可选）
   - 准备一个 VCS/Xcelium 的仿真 log
   - 准备对应的 FSDB/VCD 波形文件

3. **开始使用**
   - 在 Claude Code 中尝试解析 log
   - 让 Claude 自动分析仿真失败原因

## 故障排查

详见 [`SETUP_GUIDE.md`](SETUP_GUIDE.md:1)

## 原始文档

完整的使用说明和工作流程见 [`README.md`](README.md:1)
