# conftest.py
# pytest 配置，确保 TraceWeave 根目录在 Python 路径中

import sys
import os

# 把 TraceWeave/ 根目录加入路径
ROOT = os.path.dirname(os.path.dirname(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
