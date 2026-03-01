# Waveform Analysis MCP (精简版)

用于芯片验证的仿真 Log 与波形联合分析助手。

## 必须保留的文件清单

1.  **运行核心**：
    *   `server.py`: MCP 服务入口。
    *   `src/`: 核心解析逻辑（Log, VCD, FSDB, 分析器）。
    *   `config.py`: 唯一需要根据项目修改的配置文件。
    *   `custom_patterns.yaml`: 自定义报错正则匹配。

2.  **安装与移植必须**：
    *   `fsdb_wrapper.cpp`: FSDB 查询的 C++ 源码。
    *   `build_wrapper.sh`: 换环境后重新编译所需的脚本。
    *   `verify_setup.py`: 验证新环境依赖和库加载是否成功的脚本。
    *   `requirements.txt`: Python 依赖清单。

## 快速使用

- **换项目**：修改 `config.py` 中的路径约定。
- **换环境/移植**：
  1. 安装依赖：`pip install -r requirements.txt`
  2. 编译核心库：`bash build_wrapper.sh`
  3. 验证环境：`python3 verify_setup.py`
  4. 配置集成：将此目录下的 `server.py` 路径及环境变量加入你的 AI 助手（Claude/Gemini/Cursor）配置中。
