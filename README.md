# Waveform Analysis MCP

芯片验证仿真 Log 与波形联合分析助手。

## 项目结构 (精简版)

```text
waveform_mcp/
├── src/                # 核心解析器代码 (Log, VCD, FSDB)
├── docs/               # 所有的文档、配置模板和集成指南
├── scripts/            # 编译、环境验证和核心功能测试脚本
├── tests/              # 自动化测试套件 (Unit & Integration)
├── config.py           # 核心配置文件 (换项目只需改这里)
├── server.py           # MCP 服务入口
├── fsdb_wrapper.cpp    # FSDB 高性能查询 C++ 源码
└── requirements.txt    # Python 依赖
```

## 快速查阅

- **配置指南**: 请查看 [`docs/SETUP_GUIDE.md`](docs/SETUP_GUIDE.md)
- **虚拟机环境适配说明**: 请查看 [`docs/README_CURRENT_ENV.md`](docs/README_CURRENT_ENV.md)
- **如何开始**:
  1. 编译库: `bash scripts/build_wrapper.sh`
  2. 验证环境: `python3.11 scripts/verify_setup.py`
  3. 集成: 将 `docs/claude_config_example.json` 复制到 `~/.claude.json`
