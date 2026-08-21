"""
config.py — 集中放置环境相关路径和解析行为常量
"""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sys

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

# Directory names that conventionally hold simulation build/run artifacts one
# level below a verification root (e.g. makefile flows that set
# WORK_DIR=$VERI_PATH/work). When get_sim_paths is pointed at a verification
# root whose immediate children are source/script dirs and whose artifacts live
# under one of these, discovery descends into it before giving up — but only if
# that child actually classifies as a case or shared root, so a non-artifact dir
# of the same name is never followed.
WORK_CONTAINER_NAMES = (
    "work",
    "sim",
    "sim_work",
    "work_dir",
    "csim",
    "scratch",
    "rundir",
    "run_dir",
)

# ═══════════════════════════════════════════════════════════════════
# 自定义报错格式配置文件路径
# ═══════════════════════════════════════════════════════════════════

# 相对于 TraceWeave/ 根目录
CUSTOM_PATTERNS_FILE = os.path.join(os.path.dirname(__file__), "custom_patterns.yaml")

# ═══════════════════════════════════════════════════════════════════
# 解析行为配置
# ═══════════════════════════════════════════════════════════════════

# UVM 严重级别：哪些级别需要解析（WARNING 不处理）
UVM_PARSE_LEVELS = {"UVM_ERROR", "UVM_FATAL"}

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

# search_signals 批量模式（keyword 传列表）单次允许的最大关键词数
SIGNAL_SEARCH_MAX_KEYWORDS = 16

# get_signal_transitions 单次返回的最大跳变数（dispatch 层截断，保留区间内
# 最早的 N 条；parser 内部调用方不受影响，仍拿全量）
TRANSITIONS_MAX_RETURNED = 1000

# get_signals_by_cycle 单次查询最大周期数
MAX_CYCLES_PER_QUERY = 256

# parse_sim_log 最多返回的 error group 数
DEFAULT_MAX_GROUPS = 20

# parse_sim_log 结果控制
DEFAULT_DETAIL_LEVEL = "summary"
DEFAULT_MAX_EVENTS_PER_GROUP = 3
AUTO_DOWNGRADE_THRESHOLD = 2000

# parse_sim_log 内嵌的 first_group_context 上下文窗口。
# 刻意远小于 get_error_context 的默认 100/100：first_group_context 是“顺手瞥一眼
# 首错现场”，在大 log 里无条件塞 200 行原始文本会占据整个返回（实测占 ~92%）。
# 需要更宽上下文时按需调 get_error_context（仍用 DEFAULT_LOG_CONTEXT_*）。
FIRST_GROUP_CONTEXT_BEFORE = 12
FIRST_GROUP_CONTEXT_AFTER = 12

# trace_x_source 默认追踪参数
DEFAULT_X_TRACE_MAX_DEPTH = 20
X_TRACE_MAX_BRANCH_FANOUT = 5

# UVM multi-line continuation collection
MAX_UVM_CONTINUATION_LINES = 200

# Log files larger than this skip multi-line aggregation (bytes)
MAX_LOG_FILE_SIZE_FOR_MULTILINE = 500 * 1024 * 1024  # 500 MB

# ═══════════════════════════════════════════════════════════════════
# Auto-KDB build (vericom + elabcom) for Xcelium flows
# ═══════════════════════════════════════════════════════════════════
#
# When the user runs Xcelium (xrun) there is no Verdi KDB by default, so
# the NPI backend cannot answer driver/load queries. TraceWeave can run
# `vericom -kdb` + `elabcom -elab kdb` itself, using the file list and
# defines parsed from the compile log, and cache the resulting KDB in a
# project-agnostic cache directory.


# Default on. Set TRACEWEAVE_AUTO_KDB=0 (or "false") to disable.
def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    val = raw.strip().lower()
    if val == "":
        return default
    return val not in ("0", "false", "no", "off")


AUTO_KDB_BUILD = _env_flag("TRACEWEAVE_AUTO_KDB", True)

# A Verdi elaborated KDB can retain a useful partial netlist even when
# elabcom recorded errors (for example unresolved VHDL or encrypted cells).
# Keep that capability enabled by default; callers still require a successful
# NPI load plus a non-empty/top-matching netlist self-check before trusting it.
NPI_ALLOW_DEGRADED_KDB = _env_flag(
    "TRACEWEAVE_NPI_ALLOW_DEGRADED_KDB",
    True,
)


# Cache root for TraceWeave-managed artifacts (generated KDBs, build
# scripts, build logs). Honour XDG_CACHE_HOME / TRACEWEAVE_CACHE_DIR
# before defaulting to ~/.cache/traceweave so no environment is
# hardcoded.
def _default_cache_root() -> Path:
    explicit = os.environ.get("TRACEWEAVE_CACHE_DIR")
    if explicit:
        return Path(explicit).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg) / "traceweave"
    return Path.home() / ".cache" / "traceweave"


TRACEWEAVE_CACHE_ROOT = _default_cache_root()
KDB_CACHE_SUBDIR = "kdb"

# Passive usage telemetry. Default off, local-only (no network). When enabled,
# appends one
# JSONL line per tool call under <cache>/telemetry/usage.jsonl so we can later
# quantify how often the auto-debug primitives (cursor/period/diff_first_
# divergence) actually get used on real workloads, instead of guessing.
# Opt in with TRACEWEAVE_TELEMETRY=1.
TELEMETRY_ENABLED = _env_flag("TRACEWEAVE_TELEMETRY", False)
TELEMETRY_SUBDIR = "telemetry"
TELEMETRY_FILENAME = "usage.jsonl"


def telemetry_log_path() -> Path:
    return TRACEWEAVE_CACHE_ROOT / TELEMETRY_SUBDIR / TELEMETRY_FILENAME


# Subprocess timeout (seconds) for vericom + elabcom each.
KDB_BUILD_TIMEOUT_SEC = int(os.environ.get("TRACEWEAVE_KDB_BUILD_TIMEOUT", "600"))


# ═══════════════════════════════════════════════════════════════════
# Compile-log hierarchy and bounded Source Graph bootstrap budgets
# ═══════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class HierarchyExecutionConfig:
    """Optional internal guardrails for full hierarchy construction.

    Zero keeps the corresponding limit disabled so existing installations do
    not acquire a new implicit deadline.  Operators with a stricter outer MCP
    watchdog can set an earlier internal timeout and receive a structured
    blocker while the server remains responsive.
    """

    timeout_sec: float = 0.0
    max_source_bytes: int = 0
    error_code: str | None = None

    @property
    def valid(self) -> bool:
        return self.error_code is None


def get_hierarchy_execution_config() -> HierarchyExecutionConfig:
    raw_timeout = os.environ.get("TRACEWEAVE_HIERARCHY_TIMEOUT", "0").strip()
    raw_source_bytes = os.environ.get(
        "TRACEWEAVE_HIERARCHY_MAX_SOURCE_BYTES", "0"
    ).strip()
    try:
        timeout_sec = float(raw_timeout)
        max_source_bytes = int(raw_source_bytes)
    except ValueError:
        return HierarchyExecutionConfig(error_code="hierarchy_config_invalid")
    if (
        timeout_sec < 0
        or timeout_sec > 86_400
        or max_source_bytes < 0
        or max_source_bytes > (1 << 63) - 1
    ):
        return HierarchyExecutionConfig(error_code="hierarchy_config_invalid")
    return HierarchyExecutionConfig(
        timeout_sec=timeout_sec,
        max_source_bytes=max_source_bytes,
    )


@dataclass(frozen=True)
class BoundedBootstrapConfig:
    """Hard limits for a single-endpoint hierarchy bootstrap."""

    timeout_sec: float = 24.0
    max_source_inputs: int = 128
    max_source_bytes: int = 64 * 1024 * 1024
    max_inventory_files: int = 16_384
    max_inventory_bytes: int = 1024 * 1024 * 1024
    max_include_depth: int = 64
    max_hierarchy_depth: int = 256
    error_code: str | None = None

    @property
    def valid(self) -> bool:
        return self.error_code is None


def get_bounded_bootstrap_config() -> BoundedBootstrapConfig:
    defaults = BoundedBootstrapConfig()
    raw_values = {
        "timeout_sec": os.environ.get(
            "TRACEWEAVE_BOOTSTRAP_TIMEOUT", str(defaults.timeout_sec)
        ).strip(),
        "max_source_inputs": os.environ.get(
            "TRACEWEAVE_BOOTSTRAP_MAX_SOURCE_INPUTS",
            str(defaults.max_source_inputs),
        ).strip(),
        "max_source_bytes": os.environ.get(
            "TRACEWEAVE_BOOTSTRAP_MAX_SOURCE_BYTES",
            str(defaults.max_source_bytes),
        ).strip(),
        "max_inventory_files": os.environ.get(
            "TRACEWEAVE_BOOTSTRAP_MAX_INVENTORY_FILES",
            str(defaults.max_inventory_files),
        ).strip(),
        "max_inventory_bytes": os.environ.get(
            "TRACEWEAVE_BOOTSTRAP_MAX_INVENTORY_BYTES",
            str(defaults.max_inventory_bytes),
        ).strip(),
        "max_include_depth": os.environ.get(
            "TRACEWEAVE_BOOTSTRAP_MAX_INCLUDE_DEPTH",
            str(defaults.max_include_depth),
        ).strip(),
        "max_hierarchy_depth": os.environ.get(
            "TRACEWEAVE_BOOTSTRAP_MAX_HIERARCHY_DEPTH",
            str(defaults.max_hierarchy_depth),
        ).strip(),
    }
    try:
        timeout_sec = float(raw_values.pop("timeout_sec"))
        integers = {name: int(value) for name, value in raw_values.items()}
    except ValueError:
        return BoundedBootstrapConfig(error_code="bootstrap_config_invalid")
    if (
        timeout_sec < 0.001
        or timeout_sec > 86_400
        or any(value < 1 or value > (1 << 63) - 1 for value in integers.values())
    ):
        return BoundedBootstrapConfig(error_code="bootstrap_config_invalid")
    return BoundedBootstrapConfig(timeout_sec=timeout_sec, **integers)


# ═══════════════════════════════════════════════════════════════════
# Source Graph on-demand execution policy
# ═══════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class ConnectivityRouteConfig:
    """Validated process-local route for public connectivity tools.

    ``auto`` preserves trusted NPI, Source Graph, then Legacy Static.
    ``source_graph`` explicitly skips NPI without hiding or mutating a usable
    KDB. Invalid input safely preserves ``auto`` with a fixed error label.
    """

    mode: str = "auto"
    error_code: str | None = None

    @property
    def valid(self) -> bool:
        return self.error_code is None


def get_connectivity_route_config() -> ConnectivityRouteConfig:
    raw_mode = os.environ.get("TRACEWEAVE_CONNECTIVITY_ROUTE", "auto").strip().lower()
    if raw_mode in {"", "auto"}:
        return ConnectivityRouteConfig()
    if raw_mode == "source_graph":
        return ConnectivityRouteConfig(mode="source_graph")
    return ConnectivityRouteConfig(error_code="connectivity_route_config_invalid")


@dataclass(frozen=True)
class SourceGraphExecutionConfig:
    """Validated process-local policy for the optional Source Graph worker.

    The parent process never imports the optional frontend.  ``python_bin`` is
    passed as one argv item to the isolated worker launcher; malformed values
    are represented by a fixed ``error_code`` so routing receipts can remain
    privacy-safe.
    """

    enabled: bool
    python_bin: str
    frontend_version: str
    timeout_sec: float
    error_code: str | None = None
    disk_cache_enabled: bool = False
    disk_cache_root: Path = TRACEWEAVE_CACHE_ROOT
    disk_cache_max_entries: int = 8
    disk_cache_max_bytes: int = 512 * 1024 * 1024
    frontier_max_instances: int = 128
    frontier_max_rounds: int = 4

    @property
    def valid(self) -> bool:
        return self.error_code is None


def get_source_graph_execution_config() -> SourceGraphExecutionConfig:
    """Read the lazy, process-local Source Graph execution policy.

    Source Graph is enabled by default, but absence of ``pyslang`` is an
    expected structured dependency blocker followed by Legacy Static routing.
    Sites with an isolated frontend install can point only this worker at it
    with ``TRACEWEAVE_SOURCE_GRAPH_PYTHON``; the MCP server interpreter remains
    dependency-free.
    """

    enabled = _env_flag("TRACEWEAVE_SOURCE_GRAPH", True)
    python_bin = os.environ.get(
        "TRACEWEAVE_SOURCE_GRAPH_PYTHON", sys.executable
    ).strip()
    frontend_version = os.environ.get(
        "TRACEWEAVE_SOURCE_GRAPH_FRONTEND_VERSION", "11.0.0"
    ).strip()
    raw_timeout = os.environ.get("TRACEWEAVE_SOURCE_GRAPH_TIMEOUT", "120").strip()
    disk_cache_enabled = _env_flag("TRACEWEAVE_SOURCE_GRAPH_DISK_CACHE", False)
    disk_cache_root = _default_cache_root()
    raw_disk_entries = os.environ.get(
        "TRACEWEAVE_SOURCE_GRAPH_DISK_CACHE_MAX_ENTRIES", "8"
    ).strip()
    raw_disk_bytes = os.environ.get(
        "TRACEWEAVE_SOURCE_GRAPH_DISK_CACHE_MAX_BYTES", str(512 * 1024 * 1024)
    ).strip()
    raw_frontier_instances = os.environ.get(
        "TRACEWEAVE_SOURCE_GRAPH_FRONTIER_MAX_INSTANCES", "128"
    ).strip()
    raw_frontier_rounds = os.environ.get(
        "TRACEWEAVE_SOURCE_GRAPH_FRONTIER_MAX_ROUNDS", "4"
    ).strip()

    try:
        timeout_sec = float(raw_timeout)
    except ValueError:
        timeout_sec = 120.0
        error_code = "source_graph_execution_config_invalid"
    else:
        error_code = None

    if not _valid_exec_token(python_bin):
        error_code = "source_graph_execution_config_invalid"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}", frontend_version):
        error_code = "source_graph_execution_config_invalid"
    if timeout_sec < 0.001 or timeout_sec > 86_400:
        error_code = "source_graph_execution_config_invalid"

    try:
        disk_cache_max_entries = int(raw_disk_entries)
        disk_cache_max_bytes = int(raw_disk_bytes)
    except ValueError:
        disk_cache_max_entries = 8
        disk_cache_max_bytes = 512 * 1024 * 1024
        if disk_cache_enabled:
            error_code = "source_graph_disk_cache_config_invalid"
    disk_cache_values_invalid = (
        disk_cache_max_entries < 1
        or disk_cache_max_entries > 1_000_000
        or disk_cache_max_bytes < 1
        or disk_cache_max_bytes > (1 << 63) - 1
        or not disk_cache_root.is_absolute()
        or ".." in disk_cache_root.parts
        or "\x00" in os.fspath(disk_cache_root)
    )
    if disk_cache_values_invalid:
        disk_cache_max_entries = 8
        disk_cache_max_bytes = 512 * 1024 * 1024
        if disk_cache_enabled:
            error_code = "source_graph_disk_cache_config_invalid"

    try:
        frontier_max_instances = int(raw_frontier_instances)
        frontier_max_rounds = int(raw_frontier_rounds)
    except ValueError:
        frontier_max_instances = 128
        frontier_max_rounds = 4
        error_code = "source_graph_frontier_config_invalid"
    if (
        frontier_max_instances < 1
        or frontier_max_instances > 4096
        or frontier_max_rounds < 1
        or frontier_max_rounds > 16
    ):
        frontier_max_instances = 128
        frontier_max_rounds = 4
        error_code = "source_graph_frontier_config_invalid"

    return SourceGraphExecutionConfig(
        enabled=enabled,
        python_bin=python_bin or sys.executable,
        frontend_version=frontend_version or "11.0.0",
        timeout_sec=timeout_sec,
        error_code=error_code,
        disk_cache_enabled=disk_cache_enabled,
        disk_cache_root=disk_cache_root,
        disk_cache_max_entries=disk_cache_max_entries,
        disk_cache_max_bytes=disk_cache_max_bytes,
        frontier_max_instances=frontier_max_instances,
        frontier_max_rounds=frontier_max_rounds,
    )


# ═══════════════════════════════════════════════════════════════════
# Verdi NPI execution policy
# ═══════════════════════════════════════════════════════════════════
#
# NPI runs locally by default. Some sites grant Verdi/NPI licenses only to
# scheduled compute nodes; those users can opt in to an LSF worker without
# changing any MCP tool argument.

_NPI_LSF_QUEUE_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,128}$")
_NPI_LSF_RESERVED_ARGS = {
    "-K",
    "-J",
    "-q",
    "-o",
    "-oo",
    "-e",
    "-eo",
    "-i",
}
_NPI_LSF_ALLOWED_EXTRA_FLAGS = {
    "-R",
    "-P",
    "-G",
    "-M",
    "-n",
    "-W",
    "-m",
    "-app",
    "-sp",
    "-sla",
    "-cwd",
    "-env",
    "-gpu",
}


@dataclass(frozen=True)
class NpiExecutionConfig:
    """Validated, identity-private Verdi/NPI execution configuration.

    ``error_code`` is always a fixed label. It deliberately never embeds the
    queue, executable, staging path, or malformed environment value so callers
    can surface it safely through MCP status.
    """

    mode: str
    queue: str | None
    bsub_bin: str
    bkill_bin: str
    python_bin: str
    timeout_sec: int
    staging_dir: Path
    extra_args: tuple[str, ...] = ()
    error_code: str | None = None
    # KDB generation runs two potentially long Verdi phases.  Keep its
    # scheduler deadline separate from the short connectivity-query deadline.
    kdb_timeout_sec: int = max(120, (2 * KDB_BUILD_TIMEOUT_SEC) + 60)

    @property
    def valid(self) -> bool:
        return self.error_code is None


def get_npi_execution_config() -> NpiExecutionConfig:
    """Read and validate the opt-in NPI execution policy from the environment.

    Supported modes:
      - ``local`` (default): preserve the existing in-process NPI behavior.
      - ``lsf``: submit licensed Verdi/NPI operations, including KDB builds,
        via ``bsub``.

    The worker guard always resolves to local. The LSF worker currently invokes
    the exact local NPI core directly, but the guard is defense in depth against
    future worker refactors accidentally calling normal backend selection.
    """

    default_staging = TRACEWEAVE_CACHE_ROOT / "npi_lsf"
    if _env_flag("TRACEWEAVE_NPI_WORKER", False):
        return NpiExecutionConfig(
            mode="local",
            queue=None,
            bsub_bin="bsub",
            bkill_bin="bkill",
            python_bin="python3.11",
            timeout_sec=120,
            staging_dir=default_staging,
        )

    raw_mode = os.environ.get("TRACEWEAVE_NPI_EXECUTION", "local").strip().lower()
    mode = raw_mode or "local"
    if mode == "local":
        return NpiExecutionConfig(
            mode="local",
            queue=None,
            bsub_bin="bsub",
            bkill_bin="bkill",
            python_bin="python3.11",
            timeout_sec=120,
            staging_dir=default_staging,
        )
    if mode != "lsf":
        return _invalid_npi_config("invalid", default_staging)

    # Keep scheduler configuration namespaced and unambiguous. Sites that
    # already define LSF_QUEUE can map it in shell startup before Codex starts:
    # export TRACEWEAVE_NPI_LSF_QUEUE="$LSF_QUEUE"
    queue = os.environ.get("TRACEWEAVE_NPI_LSF_QUEUE", "").strip()
    if not _NPI_LSF_QUEUE_RE.fullmatch(queue):
        return _invalid_npi_config("lsf", default_staging)

    bsub_bin = os.environ.get("TRACEWEAVE_NPI_LSF_BSUB", "bsub").strip()
    bkill_bin = os.environ.get("TRACEWEAVE_NPI_LSF_BKILL", "bkill").strip()
    python_bin = os.environ.get("TRACEWEAVE_NPI_LSF_PYTHON", "python3.11").strip()
    if not all(_valid_exec_token(item) for item in (bsub_bin, bkill_bin, python_bin)):
        return _invalid_npi_config("lsf", default_staging)

    raw_timeout = os.environ.get("TRACEWEAVE_NPI_LSF_TIMEOUT", "120").strip()
    try:
        timeout_sec = int(raw_timeout)
    except ValueError:
        return _invalid_npi_config("lsf", default_staging)
    if timeout_sec < 1 or timeout_sec > 86_400:
        return _invalid_npi_config("lsf", default_staging)

    default_kdb_timeout = max(120, (2 * KDB_BUILD_TIMEOUT_SEC) + 60)
    raw_kdb_timeout = os.environ.get(
        "TRACEWEAVE_NPI_LSF_KDB_TIMEOUT",
        str(default_kdb_timeout),
    ).strip()
    try:
        kdb_timeout_sec = int(raw_kdb_timeout)
    except ValueError:
        return _invalid_npi_config("lsf", default_staging)
    if kdb_timeout_sec < 1 or kdb_timeout_sec > 86_400:
        return _invalid_npi_config("lsf", default_staging)

    raw_staging = os.environ.get("TRACEWEAVE_NPI_LSF_STAGING_DIR", "").strip()
    staging_dir = Path(raw_staging).expanduser() if raw_staging else default_staging
    if not staging_dir.is_absolute():
        return _invalid_npi_config("lsf", default_staging)

    extra_args, extra_error = _parse_npi_lsf_extra_args()
    if extra_error:
        return _invalid_npi_config("lsf", default_staging)

    return NpiExecutionConfig(
        mode="lsf",
        queue=queue,
        bsub_bin=bsub_bin,
        bkill_bin=bkill_bin,
        python_bin=python_bin,
        timeout_sec=timeout_sec,
        staging_dir=staging_dir,
        extra_args=extra_args,
        kdb_timeout_sec=kdb_timeout_sec,
    )


def _invalid_npi_config(mode: str, staging_dir: Path) -> NpiExecutionConfig:
    return NpiExecutionConfig(
        mode=mode,
        queue=None,
        bsub_bin="bsub",
        bkill_bin="bkill",
        python_bin="python3.11",
        timeout_sec=120,
        staging_dir=staging_dir,
        error_code="npi_execution_config_invalid",
    )


def _valid_exec_token(value: str) -> bool:
    return _valid_argv_token(value) and not value.startswith("-")


def _valid_argv_token(value: str) -> bool:
    return bool(value) and not any(ch in value for ch in ("\x00", "\n", "\r"))


def _parse_npi_lsf_extra_args() -> tuple[tuple[str, ...], bool]:
    """Parse optional scheduler arguments as JSON argv, never shell text."""

    raw = os.environ.get("TRACEWEAVE_NPI_LSF_EXTRA_ARGS_JSON", "").strip()
    if not raw:
        return (), False
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return (), True
    if not isinstance(parsed, list) or not all(
        isinstance(item, str) for item in parsed
    ):
        return (), True
    args = tuple(parsed)
    if any(not _valid_argv_token(item) for item in args):
        return (), True
    if any(item in _NPI_LSF_RESERVED_ARGS for item in args):
        return (), True
    # Every allowed scheduler option in this channel takes one following value.
    # Rejecting bare/unrecognised tokens prevents bsub from treating config text
    # as the start of the remote command before TraceWeave's worker executable.
    if len(args) % 2:
        return (), True
    for idx in range(0, len(args), 2):
        if args[idx] not in _NPI_LSF_ALLOWED_EXTRA_FLAGS:
            return (), True
    return args, False


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
