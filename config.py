"""
config.py — 集中放置环境相关路径和解析行为常量
"""

import os
from pathlib import Path

# ═══════════════════════════════════════════════════════════════════
# EDA 工具路径（与 ~/.bashrc 保持一致）
# ═══════════════════════════════════════════════════════════════════

REPO_ROOT = Path(__file__).resolve().parent
# FSDB runtime 优先级：
# 1. 仓库本地 third_party/verdi_runtime/linux64
# 2. VERDI_HOME/share/FsdbReader/linux64
LOCAL_FSDB_RUNTIME_DIR = REPO_ROOT / "third_party" / "verdi_runtime" / "linux64"
FSDB_REQUIRED_LIBS = ("libnsys.so", "libnffr.so")

# ═══════════════════════════════════════════════════════════════════
# 仿真路径自动发现配置
# ═══════════════════════════════════════════════════════════════════

COMPILE_LOG_PATTERNS = ["*comp*.log", "*elab*.log"]
SIM_LOG_PATTERNS = ["*run*.log", "xm*.log", "sim*.log", "vcs.log"]
WAVE_PATTERNS = ["*.fsdb", "*.vcd"]

MCP_CONFIG_FILE = ".mcp.yaml"
DISCOVER_MAX_DEPTH_CASE = 1
DISCOVER_MAX_DEPTH_ROOT = 2
CASE_DIR_MAX_DEPTH = 3

# ═══════════════════════════════════════════════════════════════════
# 自定义报错格式配置文件路径
# ═══════════════════════════════════════════════════════════════════

# 相对于 TraceWeave/ 根目录
CUSTOM_PATTERNS_FILE = os.path.join(
    os.path.dirname(__file__), "custom_patterns.yaml"
)

# ═══════════════════════════════════════════════════════════════════
# 解析行为配置
# ═══════════════════════════════════════════════════════════════════

# UVM 严重级别：哪些级别需要解析（WARNING 不处理）
UVM_PARSE_LEVELS    = {"UVM_ERROR", "UVM_FATAL"}

# analyze_assertion_failures 默认波形窗口（ps）
DEFAULT_WAVE_WINDOW_PS = 2000

# get_signals_around_time 默认额外回溯的跳变数
DEFAULT_EXTRA_TRANSITIONS = 5

# get_signals_around_time 单次调用允许的最大窗口（以时钟周期数为单位）
# 与 MAX_CYCLES_PER_QUERY 对齐——超过这个范围就应该改用 get_signals_by_cycle
MAX_WAVE_WINDOW_CYCLES = 256

# 时钟自动检测失败时的兜底窗口上限（无 1-bit clock 可推算周期时使用）
FALLBACK_WAVE_WINDOW_PS = 50_000_000  # 50 us

# 时钟周期自动检测的采样预算（足以推算中位数，无论频率高低）
CLOCK_DETECT_SAMPLE_PS = 50_000_000

# get_error_context 默认上下文行数
DEFAULT_LOG_CONTEXT_BEFORE = 100
DEFAULT_LOG_CONTEXT_AFTER = 100

# search_signals 返回的最大结果数
SIGNAL_SEARCH_MAX_RESULTS = 100

# get_signals_by_cycle 单次查询最大周期数
MAX_CYCLES_PER_QUERY = 256

# parse_sim_log 最多返回的 error group 数
DEFAULT_MAX_GROUPS = 20

# parse_sim_log 结果控制
DEFAULT_DETAIL_LEVEL = "summary"
DEFAULT_MAX_EVENTS_PER_GROUP = 3
AUTO_DOWNGRADE_THRESHOLD = 2000

# trace_x_source 默认追踪参数
DEFAULT_X_TRACE_MAX_DEPTH = 20
X_TRACE_MAX_BRANCH_FANOUT = 5

# UVM multi-line continuation collection
MAX_UVM_CONTINUATION_LINES = 200

# Log files larger than this skip multi-line aggregation (bytes)
MAX_LOG_FILE_SIZE_FOR_MULTILINE = 500 * 1024 * 1024  # 500 MB


def get_fsdb_runtime_info() -> dict:
    local_dir = LOCAL_FSDB_RUNTIME_DIR
    local_missing = _missing_fsdb_libs(local_dir)
    if not local_missing:
        return {
            "enabled": True,
            "source": "local_runtime",
            "lib_dir": str(local_dir),
            "missing_libs": [],
            "message": f"Using bundled FSDB runtime from {local_dir}",
        }

    verdi_home = os.environ.get("VERDI_HOME")
    if verdi_home:
        verdi_lib_dir = Path(verdi_home) / "share" / "FsdbReader" / "linux64"
        verdi_missing = _missing_fsdb_libs(verdi_lib_dir)
        if not verdi_missing:
            return {
                "enabled": True,
                "source": "verdi_home",
                "lib_dir": str(verdi_lib_dir),
                "missing_libs": [],
                "message": f"Using FSDB runtime from VERDI_HOME={verdi_home}",
            }
        return {
            "enabled": False,
            "source": "verdi_home",
            "lib_dir": str(verdi_lib_dir),
            "missing_libs": verdi_missing,
            "message": (
                f"VERDI_HOME is set to {verdi_home}, but required FSDB libs are missing: "
                f"{', '.join(verdi_missing)}"
            ),
        }

    return {
        "enabled": False,
        "source": None,
        "lib_dir": None,
        "missing_libs": list(FSDB_REQUIRED_LIBS),
        "message": (
            "FSDB runtime unavailable: provide VERDI_HOME or place libnsys.so/libnffr.so under "
            f"{LOCAL_FSDB_RUNTIME_DIR}"
        ),
    }


def _missing_fsdb_libs(lib_dir: Path) -> list[str]:
    if not lib_dir.is_dir():
        return list(FSDB_REQUIRED_LIBS)
    return [lib for lib in FSDB_REQUIRED_LIBS if not (lib_dir / lib).exists()]
