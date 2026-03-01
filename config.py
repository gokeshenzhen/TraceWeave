"""
config.py — 所有路径和文件名常量集中在此处
换项目时只需改这个文件，其他代码不用动
"""

import os

# ═══════════════════════════════════════════════════════════════════
# EDA 工具路径（与 ~/.bashrc 保持一致）
# ═══════════════════════════════════════════════════════════════════

VERDI_HOME = os.environ.get(
    "VERDI_HOME",
    "/home/eda/app/synopsys/verdi/W-2024.09-SP1"   # fallback 硬编码
)

# Verdi FSDB 解析库目录（优先用 linux64）
FSDB_LIB_DIR = os.path.join(VERDI_HOME, "share/FsdbReader/linux64")
FSDB_LIB_NSYS  = os.path.join(FSDB_LIB_DIR, "libnsys.so")
FSDB_LIB_NFFR  = os.path.join(FSDB_LIB_DIR, "libnffr.so")

# ═══════════════════════════════════════════════════════════════════
# 项目目录结构约定
# ═══════════════════════════════════════════════════════════════════

# verif/ 下的固定子目录名
VERIF_WORK_DIR      = "."             # 直接在项目根目录下
VERIF_TESTCASE_DIR  = "testcase"      
VERIF_SCRIPT_DIR    = "script"        
VERIF_TB_DIR        = "tb"            
VERIF_DUV_DIR       = "src"           # 适配当前 src 目录

# case_list 文件路径（相对 verif/）
CASE_LIST_FILE      = "flist.f"       # 借用 flist.f 作为 placeholder

# ═══════════════════════════════════════════════════════════════════
# 仿真输出文件名约定
# ═══════════════════════════════════════════════════════════════════

# 编译 + elab log
ELAB_LOG_NAME       = "logs/compile.log"

# 仿真 work 目录前缀（如果不想用 case 独立子目录，可设为空）
WORK_CASE_PREFIX    = ""

# 仿真 log 文件名
SIM_LOG_NAME        = "logs/simulate.log"

# 波形文件名
WAVE_FILE_NAME      = "waves.fsdb"

# ═══════════════════════════════════════════════════════════════════
# 自定义报错格式配置文件路径
# ═══════════════════════════════════════════════════════════════════

# 相对于 waveform_mcp/ 根目录
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

# search_signals 返回的最大结果数
SIGNAL_SEARCH_MAX_RESULTS = 100

# ═══════════════════════════════════════════════════════════════════
# 便捷函数：根据项目根目录 + case 名构造标准路径
# ═══════════════════════════════════════════════════════════════════

def get_elab_log(verif_root: str) -> str:
    """verif/work/elab.log"""
    return os.path.join(verif_root, VERIF_WORK_DIR, ELAB_LOG_NAME)

def get_sim_log(verif_root: str, case_name: str) -> str:
    """verif/work/work_<case>/irun.log"""
    return os.path.join(verif_root, VERIF_WORK_DIR,
                        WORK_CASE_PREFIX + case_name, SIM_LOG_NAME)

def get_wave_file(verif_root: str, case_name: str) -> str:
    """verif/work/work_<case>/top_tb.fsdb"""
    return os.path.join(verif_root, VERIF_WORK_DIR,
                        WORK_CASE_PREFIX + case_name, WAVE_FILE_NAME)

def get_case_list(verif_root: str) -> str:
    """verif/testcase/case_list"""
    return os.path.join(verif_root, CASE_LIST_FILE)

def get_work_case_dir(verif_root: str, case_name: str) -> str:
    """verif/work/work_<case>/"""
    return os.path.join(verif_root, VERIF_WORK_DIR,
                        WORK_CASE_PREFIX + case_name)
