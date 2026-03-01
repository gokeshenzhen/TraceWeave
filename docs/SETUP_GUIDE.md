# Waveform MCP 环境配置指南

本文档针对当前虚拟机环境 (Linux 4.18, Python 3.11)。

## ✓ 已完成的配置

### 1. 环境检测结果
- **VERDI_HOME**: `/home/eda/app/synopsys/verdi/W-2024.09-SP1`
- **VCS_HOME**: `/home/eda/app/synopsys/vcs/W-2024.09-SP1`
- **Python 版本**: 3.11
- **FSDB 库**: 已验证可加载（需预先加载 libz.so.1）

### 2. 已更新的文件
- [`config.py`](config.py:12): 更新 VERDI_HOME 默认路径
- [`build_wrapper.sh`](build_wrapper.sh:8): 更新编译脚本默认路径
- [`libfsdb_wrapper.so`](libfsdb_wrapper.so): 已成功编译 FSDB C++ wrapper
- [`requirements.txt`](requirements.txt): 创建依赖列表

### 3. 已安装的依赖
```bash
python3.11 -m pip install mcp pyyaml --user
```
状态：✓ 已安装

---

## 下一步：集成到 Claude Code

### 方法 1：全局配置（推荐）
编辑或创建 `~/.claude.json`，复制以下内容：

```json
{
  "mcpServers": {
    "waveform": {
      "command": "python3.11",
      "args": ["/home/eda/project/waveform_mcp/server.py"],
      "env": {
        "VERDI_HOME": "/home/eda/app/synopsys/verdi/W-2024.09-SP1",
        "VCS_HOME": "/home/eda/app/synopsys/vcs/W-2024.09-SP1",
        "PATH": "/home/eda/app/synopsys/verdi/W-2024.09-SP1/bin:/home/eda/app/synopsys/vcs/W-2024.09-SP1/bin:/usr/local/bin:/usr/bin:/bin"
      }
    }
  }
}
```

也可以直接复制项目中的示例文件：
```bash
cp /home/eda/project/waveform_mcp/claude_config_example.json ~/.claude.json
```

### 方法 2：直接启动测试
在终端中手动启动 MCP 服务器：
```bash
cd /home/eda/project/waveform_mcp
python3.11 server.py
```

---

## 验证安装

### 1. 验证 Python 依赖
```bash
python3.11 -c "from mcp.server import Server; print('✓ MCP Server 可用')"
python3.11 -c "import yaml; print('✓ PyYAML 可用')"
```

### 2. 验证 FSDB 库加载
```bash
python3.11 -c "
import ctypes, os
ctypes.CDLL('libz.so.1', ctypes.RTLD_GLOBAL)
lib_dir = '/home/eda/app/synopsys/verdi/W-2024.09-SP1/share/FsdbReader/linux64'
ctypes.CDLL(lib_dir + '/libnsys.so', ctypes.RTLD_GLOBAL)
ctypes.CDLL(lib_dir + '/libnffr.so')
print('✓ FSDB 库加载成功')
"
```

### 3. 验证 wrapper 编译
```bash
ls -lh /home/eda/project/waveform_mcp/libfsdb_wrapper.so
nm -D /home/eda/project/waveform_mcp/libfsdb_wrapper.so | grep fsdb_
```

---

## 运行单元测试

```bash
cd /home/eda/project/waveform_mcp

# 安装 pytest（如果需要）
python3.11 -m pip install pytest --user

# 运行全部测试
python3.11 -m pytest tests/ -v

# 只测试 log 解析器（不需要波形文件）
python3.11 -m pytest tests/test_log_parser.py -v
```

**注意**: `test_fsdb_parser.py` 和 `test_analyzer.py` 需要真实的 FSDB 波形文件。

---

## 使用示例

一旦配置到 Claude Code，你可以通过对话使用：

```
你: "解析 /path/to/verif/work/work_case0/irun.log，看有多少个错误"

Claude: [自动调用 parse_sim_log 工具]
```

```
你: "在 /path/to/top_tb.fsdb 中搜索包含 'clk' 的信号"

Claude: [自动调用 search_signals 工具]
```

```
你: "分析 case0 的仿真失败，波形是 /path/to/top_tb.fsdb"

Claude: [自动调用 analyze_failures，联合 log 和波形分析]
```

---

## 项目适配

如果要在新的芯片验证项目中使用，只需修改 [`config.py`](config.py:1)：

```python
# 如果你的项目波形文件名不同
WAVE_FILE_NAME = "your_tb.fsdb"  # 默认是 top_tb.fsdb

# 如果仿真 log 名不同
SIM_LOG_NAME = "sim.log"         # 默认是 irun.log

# 如果 work 目录结构不同
WORK_CASE_PREFIX = "build_"      # 默认是 work_
```

---

## 故障排查

### 问题 1: "No module named 'mcp'"
```bash
python3.11 -m pip install mcp pyyaml --user
# 确保使用 python3.11，不是 python3 或 python3.6
```

### 问题 2: "libfsdb_wrapper.so: cannot open shared object file"
```bash
cd /home/eda/project/waveform_mcp
bash build_wrapper.sh
```

### 问题 3: "undefined symbol: gzopen"
这已在代码中修复，FSDB parser 会自动预加载 libz.so.1。

### 问题 4: 测试失败
确保：
1. 测试波形文件存在（默认路径在 pytest.ini 中配置）
2. VERDI_HOME 环境变量已设置
3. 当前用户有权限访问 Verdi 安装目录

---

## 技术细节

### 依赖加载顺序
FSDB 解析时的库加载顺序很重要：
```
libz.so.1 (GLOBAL) → libnsys.so (GLOBAL) → libnffr.so → libfsdb_wrapper.so
```

这在 [`src/fsdb_parser.py`](src/fsdb_parser.py:24-34) 的 `_load_wrapper()` 中已自动处理。

### 支持的波形格式
- **VCD**: 纯 Python 解析，无外部依赖
- **FSDB**: 通过 Verdi libnffr.so + C++ wrapper

### 支持的仿真器
- VCS (Synopsys)
- Xcelium (Cadence)
- 自定义（通过 custom_patterns.yaml 扩展）
