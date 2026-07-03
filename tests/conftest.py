# conftest.py
# pytest 配置，确保 TraceWeave 根目录在 Python 路径中

import sys
import os

# 测试绝不能写入真实的用户遥测文件：走 server.call_tool 的测试（如
# TestCallToolErrors）会触发真实的 record_call 落盘，曾把每次 pytest 运行
# 都变成 ~/.cache/traceweave/telemetry/usage.jsonl 里的一个假"失败会话"，
# 污染使用统计。必须在任何测试 import config 之前设置（TELEMETRY_ENABLED
# 在 config import 时读取），且无条件覆盖外部环境。
os.environ["TRACEWEAVE_TELEMETRY"] = "0"

# 把 TraceWeave/ 根目录加入路径
ROOT = os.path.dirname(os.path.dirname(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
