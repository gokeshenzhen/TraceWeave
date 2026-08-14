# PR：允许 TraceWeave 的 Verdi NPI 后端使用带 elaboration error 的 KDB（部分可用 / degraded 模式）

- **目标代码**：TraceWeave 当前本地 `main` 架构
- **状态**：degraded-KDB 消费端、路由策略、LSF 协议、
  schema、测试和配套用户文档已实现，并由 commit `00228a2`
  提交。`build_kdb` 失败产物是否作为 degraded candidate 发布，
  仍有意留作后续工作，见 §7 和 §8。
- **读者**：无需预先了解问题；本文应足以说明原始故障、当前实现、明确
  边界和后续工作。
- **证据说明**：调查来自一个仅能在 LSF 执行节点获取 NPI license 的真实
  私有设计。本文已替换所有站点和设计专有名称。§2～§4 记录历史证据，
  §5 描述当前实现，§6 汇总不依赖私有 KDB 的 mock 验证。

> **历史背景**：最初的用户反馈基于较旧的远端版本。当时本地尚未推送的
> 代码已经引入 Source Graph 路由、`trace_x_source`、LSF worker receipt、
> `build_kdb` 和更严格的 public provenance 检查。因此不能把旧版补丁直接
> 套到当前代码上。§2～§4 保留经过泛化的现场证据；§5～§8 给出针对当前
> 架构的实现结论、保留边界和后续工作。

---

## 实现提交信息

| 字段 | 内容 |
|---|---|
| 完整 SHA | `00228a247737145a4850cb72d1c2e7256c81acbe` |
| 短 SHA | `00228a2` |
| 标题 | `feat(npi): support degraded KDB queries` |
| 分支 | `main` |
| 提交时间 | `2026-08-14T15:39:22-07:00` |
| 变更规模 | 19 个 tracked 文件，1331 行新增，100 行删除 |

该 commit 包含 §5 描述的 degraded-KDB 消费端实现、LSF
quality/provenance 修复、public routing、X-trace 整链重跑、schema、
hierarchy overlay、回归测试和 README/架构文档。

该 commit 有意不包含 §8 的 `build_kdb` producer-side action items；
它们仍是后续工作，不应被解读为已交付能力。本 PR 记录文档由紧随
其后的独立 docs commit 加入，以避免 commit 自身 SHA 的循环引用。
详细验证结果见 §6。

---

## 0. 摘要

旧远端版本把所有 `npisys.load_design(...) != 1` 都视为硬失败。当前实现
将 KDB 分成三种状态：

| 状态 | 准入条件 | 对外信任策略 |
|---|---|---|
| clean | `load_design == 1` | 保持原有 clean NPI 行为 |
| degraded | `load_design == 0`、存在 `.hasElabcomError`、允许 degraded 模式，并且 requested top 通过非空/匹配的 netlist 自检 | 只信任正向事实：driver 已解析、loads 非空或 path 已找到；全局 coverage 标成 partial |
| unusable | 其他返回码、缺少 error marker、top 为空或不匹配、native 调用异常，或者显式关闭 degraded 模式 | 继续使用 Source Graph，必要时再使用 Legacy Static |

elaboration error 数量只用于诊断，不作为准入阈值。8 个 error 既不会被
自动接受，也不会被自动拒绝；真正的判断依据是 NPI 是否能为 requested top
加载一个通过自检的部分 netlist。

对于 degraded KDB，负向或不完整结果不能证明“对象/连接不存在”，因此继续
走当前生产路由：

```text
Verdi NPI -> Source Graph -> Legacy Static
```

`trace_x_source` 更严格：只要 degraded NPI 链中的任意 driver 查询变得
不确定，就丢弃整条部分 NPI 链，从用户最初请求的 signal 重新使用 Source
Graph 追踪；如果仍不能安全完成，再由 Static 从原始 signal 完整重算。
最终 payload 不会混合不同 connectivity backend 的事实。

这与“Verdi 吃一个有 error 的 filelist 后，未碰到错误区域时 trace/load
仍可能可用”的用户经验一致：已成功 elaboration 的区域仍有价值，但查询
碰到缺失区域时不能把负结果当成完整结论。

本阶段是一个**消费端改动**：可以消费工程、仿真器或用户已经生成的、带
error marker 的 KDB，但不会把 TraceWeave 自己调用 `build_kdb` 产生的
非零退出产物提升为正常缓存。该生产端扩展列在 §8。

---

## 1. 当前后端架构

`find_signal_loads`、`explain_signal_driver`、`trace_signal_path`
以及 `trace_x_source` 使用以下生产路由：

```text
可信 Verdi NPI -> 有界按需 Source Graph -> Legacy Static
```

发现 KDB 后，本地模式选择 `VerdiNpiBackend`；如果 NPI license 只能在
执行节点获得，则选择 LSF connectivity backend。NPI 结果必须同时通过：

1. 该 operation 的可用性判断；
2. 单一 backend provenance 检查。

只要任一条件不成立，public dispatcher 就进入下一个 backend，并且不会把
前一个 backend 的部分 payload 拼接到最终结果中。

原始现场证据来自 LSF 模式：

```bash
export TRACEWEAVE_NPI_EXECUTION=lsf
export TRACEWEAVE_NPI_LSF_QUEUE="<licensed-queue>"
```

local 和 LSF 使用相同的 KDB 准入语义。区别是 LSF 中真正调用 NPI 的是
compute-node worker，因此 worker 还必须把实际的 clean/degraded load
quality 传回 parent。

关键文件：

- `src/verdi_backend.py`：KDB 探测、候选优先级、error marker/log 元数据。
- `src/verdi_npi_backend.py`：NPI 加载、自检和 driver/load/path 查询。
- `src/npi_lsf.py`：LSF transport、worker 协议和结果校验。
- `src/npi_worker.py`：计算节点上的 worker 入口。
- `src/connectivity_backend.py`：backend 选择和 Static backend。
- `server.py`：public 路由、结果可用性判断和 backend receipt。
- `src/schemas.py`：`BackendStatus` 及各 operation 的公开 schema。
- `src/kdb_builder.py`：`build_kdb` 生产端和缓存发布策略。
- `config.py`：NPI/LSF/degraded 配置。

---

## 2. 旧远端版本上的用户可见现象

在原始反馈对应的旧版本中，对任意 signal 调用
`find_signal_loads`（driver/path 同理）可能返回：

```json
{
  "loads": [],
  "completeness": "shallow_only",
  "stopped_at": "no_static_load_found",
  "backend_status": {
    "backend": "verdi_npi",
    "actual_backend": "static",
    "fallback_reason": "npi_lsf_npi_unavailable",
    "worker_status": "npi_unavailable",
    "scheduler_status": "completed",
    "kdb_path": ".../<build_dir>/simv.daidir/kdb.elab++"
  }
}
```

这里 KDB 已经找到，LSF job 也成功调度并结束，但 worker 报
`npi_unavailable`，导致整个查询退回 Static。用户看到空 loads 后会认为
“这个工程完全不能使用 NPI”，而实际上 KDB 中大部分已经成功 elaboration
的区域仍可被 NPI 查询。

local 模式下的等价现象是：

```text
actual_backend="static"
fallback_reason="npi_load_failed"
```

---

## 3. 旧版本的根因

### 3.1 `_ensure_loaded` 把 `rc != 1` 全部当成硬失败

旧远端版本的 `src/verdi_npi_backend.py` 包含以下判断；这已经不是当前代码：

```python
# 旧远端版本，仅用于解释历史根因
old_kdb, old_top = self._loaded_kdb, self._loaded_top
rc = npisys.load_design([
    "traceweave_npi",
    "-simflow", "-dbdir", kdb_path,
    "-top", top,
])
if rc != 1:
    if old_kdb and old_top:
        npisys.load_design([
            "traceweave_npi",
            "-simflow", "-dbdir", old_kdb,
            "-top", old_top,
        ])
    return False
```

因此 `rc == 0` 即使已经产生可查询的部分 netlist，也会直接返回
`False`。driver/load/path 三个入口随后统一进入
`npi_load_failed` fallback；LSF worker 则把它映射成
`worker_status="npi_unavailable"`。

### 3.2 现场 KDB 为什么返回 `rc == 0`

现场 KDB 有 8 个 elaboration error，模式完全一致：SystemVerilog 中实例化
的 VHDL view 没有被当前 elabcom 流程定义，因此相应 cell 没有 view。

KDB 的 `<kdb_path>/elabcomLog/compiler.log` 中包含类似记录；
`.hasElabcomError` marker 指向该日志。下面保留工具原始错误格式，名称和
路径已经泛化：

```text
*Error* view <vhdl_view_A> is not defined for instance <inst_1> (<sv_file_a>.sv:NNNN)
*Error* view <vhdl_view_B> is not defined for instance <inst_2> (<sv_file_b>.sv:NN)
*Error* view <vhdl_view_C> is not defined for instance <inst_3> (<sv_file_c>.sv:NNN)
*Error* view <vhdl_view_C> is not defined for instance <inst_4> (<sv_file_c>.sv:NNN)
*Error* view <vhdl_view_D> is not defined for instance <inst_5> (<sv_file_d>.sv:NN)
*Error* view <vhdl_view_A> is not defined for instance <inst_6> (<sv_file_e>.sv:NNN)
*Error* view <vhdl_view_A> is not defined for instance <inst_7> (<sv_file_f>.sv:NNN)
*Error* view <vhdl_view_A> is not defined for instance <inst_8> (<sv_file_f>.sv:NNN)
Total   8 error(s),   0 warning(s)
```

这类问题也可能来自 encrypted/black-box IP、部分缺失的 library 或只编译了
一部分 design unit。

本 PR 不修复 KDB 本身，也不把 VHDL 自动加入 elaboration。它解决的是：
在 KDB 已经有错误的前提下，不要把已成功 elaboration 的全部区域一起丢弃。

### 3.3 现场问题不是 LSF 或 license 故障

调查逐项排除了：

- LSF 正常：job 能提交、dispatch 并完成。
- license 正常：compute node 上可以导入 `pynpi`，
  `npisys.init` 成功，Synopsys license banner 正常输出。
- 真正触发 fallback 的位置是 `load_design` 返回 `rc == 0` 后，被旧版
  `if rc != 1: return False` 截断。

---

## 4. 历史现场证据：三个 NPI 能力都能查询已 elaboration 的子集

调查者在 compute node 上复用 TraceWeave 的 `VerdiNpiBackend`，直接调用
`load_design`，在确认 `rc == 0` 后绕过旧 gate，再调用三个 private
NPI 查询。泛化后的结果如下：

| 能力 | 测试 signal | 原始查询结果 |
|---|---|---|
| `find_signal_loads` | 纯 SV AHB matrix 的 clock | 约 85 个真实 loads |
| `find_signal_loads` | 另一个纯 SV block 的 clock | 约 66 个真实 loads |
| `explain_signal_driver` | 同一纯 SV clock，`recursive=True` | `driver_status="resolved"`，约 32～33 hops，包含真实 `source_file:line` 和跨 instance fan-in |
| `trace_signal_path` | 纯 SV block clock 到 child clock | 找到 2-hop path，包含 source location |
| 不可追踪区域 | 靠近缺失 VHDL cell 的 signal | 设置 `stopped_at="signal_path_unresolved_in_npi"`，不崩溃，也不破坏后续查询 |

一个 driver hop 的泛化示例：

```json
{
  "depth": 1,
  "signal_path": "tb_top.<if_inst>.<vif>",
  "driver_kind": "always_ff",
  "source_file": ".../<uvc>/<agent>/<if>.sv",
  "source_line": 40,
  "source_info_origin": "npi",
  "backend_confidence": "exact",
  "expression_summary": "always_ff driver via Reg.<net> at line 40"
}
```

结论不是“`rc == 0` 的 KDB 一定可用”，而是：

> `rc == 0` 只能证明 elaboration 报过错，不能单独证明生成的部分 netlist
> 完全不可用。是否允许查询，还必须通过 marker 和 requested-top 自检。

历史 private probe 对成功事实报告了 `completeness="exact"`。当前 public
contract 更保守：具体的 driver/load/path hop 可以保留 fact-level exact
confidence，但 degraded loads 和 backend attempt 的全局 coverage 必须是
`approximate/partial`。部分 netlist 返回的空结果或 unresolved 不能作为
“不存在”的证据。

### 4.1 手工复现实验

生产代码不需要依赖私有设计。CI 中的 mock 测试已经覆盖相同机制。若现场有
一个带 error 但仍产生部分 netlist 的 KDB，也可以手工验证。

前提：

- `VERDI_HOME` 已设置；
- 加载 KDB 的 Verdi 版本与生成 KDB 的版本兼容；
- LSF 模式下，脚本、TraceWeave、KDB 和 staging 目录在 submission/compute
  node 上必须具有相同绝对路径；node-local `/tmp` 不可用于跨节点传递。

下面的脚本只用于调查，直接修改 backend private state，不是受支持的生产
API：

```python
import os
import sys

sys.path.insert(0, "<repo-root>")
os.environ["TRACEWEAVE_NPI_WORKER"] = "1"

from src.verdi_npi_backend import (
    VerdiNpiBackend,
    _import_pynpi,
    _silence_native_stdio,
    _NPI_INITIALIZED_IDS,
)

kdb = "<path-to>/simv.daidir/kdb.elab++"
top = "tb_top"

be = VerdiNpiBackend()
mods = _import_pynpi()
be._npi_modules = mods
npisys, netlist = mods

with _silence_native_stdio():
    npisys.init(["traceweave_npi"])
    _NPI_INITIALIZED_IDS.add(id(npisys))
    rc = npisys.load_design([
        "traceweave_npi",
        "-simflow", "-dbdir", kdb,
        "-top", top,
    ])

# 历史实验的关键：即使 rc == 0，也临时把 private state 标成 ready。
be._state = "ready"
be._loaded_kdb = kdb
be._loaded_top = top

print("rc =", rc, "(1=OK, 0=has elab error)")

signal = "<full.hier.path.to.a.pure_sv_block>.clk"
loads = be._npi_find_loads(signal, {}, kdb, top, True, None)
print("loads:", len(loads["loads"]))
print(
    "driver:",
    be._npi_find_driver(signal, "", top, recursive=True)["driver_status"],
)

child = signal.replace(".clk", ".<child_inst>.clk")
path = be._npi_find_path(signal, child, expand_assigns=False)
print("path found:", path.get("found"))
```

LSF 交互式运行示例：

```csh
bsub -I -q <licensed-queue> <repo-root>/.venv/bin/python <shared-dir>/probe.py
```

期望至少看到：

```text
rc = 0
loads: <大于 0>
driver: resolved
path found: True
```

---

## 5. 当前实现

### 5.1 KDB 探测和产物分类

`src/verdi_backend.py` 现在会把带 `.hasElabcomError` 的
`kdb.elab++` 保留为 degraded **candidate**，而不是在 probe 阶段直接
丢弃。候选选择保持保守：

- clean elaborated KDB 优先于 degraded KDB；
- clean TraceWeave cache 可以优先于 degraded project-local KDB；
- 对 Xcelium/vericom flow，degraded elaborated KDB 比 source-only
  `*.lib++` 更有查询价值，因为后者不能提供 elaborated-netlist NPI 查询；
- 设置 `TRACEWEAVE_NPI_ALLOW_DEGRADED_KDB=0` 后恢复 clean-only 选择，
  并产生固定路由原因 `npi_degraded_kdb_disabled`。

marker 和 error log 采用有界、best-effort 读取。能够解析时提取
`Total N error(s)` 和 log 路径；任何读取或格式异常都不会抛到主查询路径，
error count 也永远不作为准入阈值。

`kdb_validation_status="elaboration_error"` 表示 probe 看到的 artifact
事实。此时 `kdb_degraded` 仍然是 `false`；只有 NPI 实际加载部分
netlist 并通过自检后，后者才会变成 `true`。

### 5.2 NPI 加载准入和状态管理

`src/verdi_npi_backend.py` 实现的精确策略是：

```text
load_design == 1
    -> clean success

load_design == 0
    + degraded 模式已启用
    + 存在 .hasElabcomError
    + get_top_inst_list() 成功且非空
    + 在 top 名称可读取时，requested top 必须匹配
    -> degraded success

其他返回码、空 top、top 不匹配或 native 异常
    -> unusable
    -> 在可恢复的 case-switch 失败路径上，恢复之前加载的 KDB/top/quality
```

backend 保存：

- `_loaded_degraded`；
- degraded error count/log；
- 最近一次成功 public query 对应的 KDB-status receipt。

加载新 case 失败时，会尽可能恢复旧 case 的 clean/degraded 状态，而不是只
恢复 KDB 路径。degraded loads 查询的整体
`completeness="approximate"`；具体 load、driver 或 path hop 仍可保持
fact-level exact confidence。

### 5.3 只信任正向事实

degraded KDB 下，public dispatcher 的 operation-specific 判断如下：

| Operation | 可接受的 degraded NPI 结果 | 视为不确定并继续 fallback 的结果 |
|---|---|---|
| `explain_signal_driver` | `driver_status="resolved"` | `testbench_driven`、unsupported、unresolved、异常或任何非 resolved 状态 |
| `find_signal_loads` | loads 非空，整体 completeness 为 exact/approximate | loads 为空，或者查询 stopped/incomplete |
| `trace_signal_path` | `found=true` 且 path 全部来自 NPI | `from_not_found`、`to_not_found`、`not_connected`、调用失败或其他负结果 |

可接受的 degraded 正结果使用：

```text
actual_backend="verdi_npi"
coverage_status="partial"
```

不确定结果使用固定原因：

```text
npi_degraded_result_inconclusive
```

然后进入 Source Graph，必要时再进入 Static。clean KDB 的原有负结果语义
保持不变。

attempt/fallback receipt 可以记录尝试过的多个 backend，但最终 payload
中的 connectivity 事实只来自一个 backend。

### 5.4 `trace_x_source`：丢弃整链并从原始 signal 重跑

X trace 会连续执行多个 driver lookup。假设 degraded NPI 先成功解析一段，
随后碰到无法解析的区域：

```text
原始 X signal A <- NPI 成功解析 B <- NPI 对下一跳返回不确定
```

实现会丢弃 `A <- B`，从原始 signal `A` 使用 Source Graph 重新追踪。
这里的“重跑”不表示：

- 重新运行仿真；
- 重新生成 KDB；
- 对同一个 degraded NPI 查询简单重试；
- 把 Source Graph 的 tail 接到 NPI prefix 后面。

如果 Source Graph 也不能安全完成，Static 会再次从 `A` 完整重算。

public result 会报告：

- `trace_restarted`；
- `whole_trace_restart_count`；
- `whole_trace_restart_reasons`；
- 尝试过的 backend；
- 最终 `actual_backend`。

fallback backend 仍可能追不到完整根因。因此 whole-chain restart 保证的是
provenance 和结果一致性，不是“换一个 backend 必然成功”。

### 5.5 LSF worker 的加载质量和 NPI 来源标记修复

LSF worker 是实际调用 NPI 的进程，因此现在会在 versioned worker envelope
中返回：

```text
kdb_load_quality: clean | degraded
```

该字段位于 driver/load/path operation schema 之外。parent 收到后，再结合
有界读取的 KDB error metadata，做出与 local mode 相同的 public trust
判断。

实现审阅还发现一个独立的当前架构问题：private NPI loads/path 结果缺少
明确的顶层 backend identity。经过 schema default 后，一个真实 NPI 结果
可能被误标成 Static；public single-provenance guard 随后会正确地拒绝这个
来源矛盾的结果，造成有效 NPI 结果被误丢弃。

现在已经：

- 为 driver、loads、path 顶层显式写入 `backend="verdi_npi"`；
- 为 path hop 和相应具体事实写入 NPI backend provenance；
- 要求 LSF worker 拒绝 validated top-level backend 不是
  `verdi_npi` 的 operation 结果。

该修复不改变 NPI 的 connectivity 计算，只修正结果来源标记以及由错误来源
标记引起的 public fallback。

### 5.6 公开状态、层次结构源信息覆盖和配置

`BackendStatus` 新增：

```text
kdb_degraded: bool
kdb_error_count: int | null
kdb_error_log: string | null
```

两个状态需要明确区分：

- `kdb_validation_status="elaboration_error"`：probe 到的 KDB artifact
  存在 Verdi error marker；
- `kdb_degraded=true`：NPI 已经实际把该 artifact 作为 requested top 的
  可用部分 netlist 加载，并完成了当前 operation。

`build_tb_hierarchy` 可以采用 degraded KDB 提供的正向 source-location
事实，但会标记：

```text
source_info_overlay="npi_partial"
source_info_overlay_reason="npi_degraded_kdb"
```

它不会把该 overlay 表述成全局完整。

degraded consumption 默认开启。如需恢复旧版 clean-only 行为，在启动 MCP
server 前设置：

```bash
export TRACEWEAVE_NPI_ALLOW_DEGRADED_KDB=0
```

escape-hatch 决策及固定 fallback reason 会保留在 public receipt 中。

---

## 6. 验证结果和剩余证据缺口

当前回归测试覆盖：

- degraded candidate 探测、clean candidate 优先级、有界 error-count 解析
  和 escape hatch；
- `load_design == 0` 配合 8-error 日志、可用 top、空 top、requested-top
  mismatch、native exception 和 clean load；
- degraded 正/负 driver、loads、path 的 public 路由；
- LSF clean/degraded quality 传输和三种 operation 的 NPI provenance；
- X trace 整条 NPI 链丢弃并从原始 signal 重跑；
- hierarchy 的 partial source overlay 和 public schema；
- clean KDB 路由行为不变。

当前工作区已执行：

- 全量测试：`1413 passed, 36 skipped`；
- 最后一次 probe-status 小调整后的受影响测试：`29 passed`；
- 所有改动 Python 文件通过 Ruff；
- `git diff --check` 通过；
- Python compile check 通过。

测试环境实际执行了已安装 Verdi/NPI 的 clean-load integration path。
不需要私有 KDB 的 mock 测试覆盖 degraded 8-error 行为和路由判断。

仍有一个证据缺口：尚未对用户真实的私有 8-error KDB 运行 local 或 LSF
端到端 degraded 验证。真实 acceptance 应至少验证：

1. 一个 resolved driver；
2. 一个非空 loads；
3. 一个 found path；
4. 上述正结果的 `actual_backend="verdi_npi"`、
   `kdb_degraded=true` 和 partial coverage；
5. 一个故意选择的 unresolved 查询能够继续 fallback，并产生正确 receipt。

---

## 7. 有意保留的 `build_kdb` 边界

这里的“发布”是指：把生成的 artifact 提升为 TraceWeave 正常的
content-addressed KDB cache，使后续 probe 自动选择它。它不是 Git commit、
push 或对外上传。

`src/kdb_builder.py` 当前的生产端 contract 保持不变：

```text
vericom + elabcom 成功
    -> 写入 state.json，status=ok
    -> 把临时目录原子 rename 到
       $TRACEWEAVE_CACHE_DIR/kdb/<hash>/
    -> 把 kdb.elab++ 发布为正常 cache candidate

vericom 或 elabcom 返回非零
    -> 写入 state.json，status=failed
    -> 把临时目录 rename 到 .failed-<hash>/
    -> 保留 build.sh 和日志供检查
    -> 不修改已有成功 cache entry
    -> 不把失败输出当作正常 KDB candidate
```

因此，当前 degraded consumer 可以使用仿真器、工程或用户已经存在的
error-marked KDB，但不会自动使用一次失败的 TraceWeave `build_kdb`
调用所产生的部分 `kdb.elab++`。

对用户报告的 8-error 场景，需要区分：

- 8 个 error 来自工程已经存在的 KDB：只要 license 可得、NPI 返回
  `load_design == 0` 且 requested-top 自检通过，就可以使用 degraded
  NPI 的正向事实；
- 8 个 error 来自 TraceWeave `build_kdb`，并且 `elabcom` 返回非零：
  产物仍留在 `.failed-<hash>/`，不会自动交给 NPI。

第一阶段保留这个边界的原因：

- builder 非零退出不保证存在 `kdb.elab++`；
- 即使 KDB 目录存在，也不保证 partial netlist 非空、requested top 匹配或
  与当前 NPI 版本兼容；
- build host 可能没有 NPI license，只有 LSF compute node 能做决定性的
  self-check；
- 把所有 failed build 都当成正常 cache success 会破坏 cache 语义，并可能
  覆盖或降低已有 clean entry 的优先级。

失败 artifact 是隔离保留，不是删除；仍可用于读取日志和复现构建。

---

## 8. 后续工作项：让 `build_kdb` 产生的 degraded 产物可被安全消费

以下项目不属于已经完成的消费端阶段：

- [ ] **增加明确的生产端状态。** 区分 `ok`、
  `degraded_candidate` 和 `failed`。只有非零 `elabcom` 结束后，预期
  `kdb.elab++` 和 `.hasElabcomError` 都存在，才可记录成
  `degraded_candidate`；否则仍是 `failed`。
- [ ] **将 candidate 与 clean cache 分开保存。** 使用稳定、私有、
  content-addressed 且 submission/LSF compute node 均可见的路径。未经
  验证的 candidate 不能替换或优先于已有 clean cache。
- [ ] **复用当前真实 NPI 准入 gate。** 在提供任何事实前，必须满足
  `load_design == 0`、error marker 和相同的 requested-top 自检。不能因
  error count 较小就发布或信任 candidate。
- [ ] **定义 local 与 LSF 的验证过程。** local 模式可以直接验证；
  LSF-only 模式必须由 licensed worker 验证，再把
  clean/degraded/unusable quality 返回 parent。需要决定该 receipt 是否
  缓存，或者每个新 worker process 都重新验证，同时避免跨节点可变状态竞争。
- [ ] **扩展 `build_kdb` result contract。** 将“candidate 已生成”和
  “NPI 已验证”表示为两个独立事实，例如：

  ```text
  status="degraded_candidate"
  validation_status="usable|unusable|not_run"
  ```

  同时返回有界 error metadata；未经验证的 candidate 不能被称为成功 KDB。
- [ ] **更新 probe 优先级和 lifecycle。** 目标顺序是 clean
  project/cache KDB、通过准入检查的 degraded project/cache candidate、
  然后 no NPI。需要定义 atomic replacement、并发 build、stale candidate
  清理以及诊断日志保留规则。
- [ ] **增加 producer-to-consumer 回归。** 覆盖：非零 `elabcom` 且无
  KDB、有 marker 的可用 partial KDB、空/错误 top、损坏或版本不兼容输出、
  已存在 clean cache、重复构建、local 验证和 LSF 验证。
- [ ] **运行真实 degraded-KDB acceptance。** 优先建立无私有依赖的
  mixed-language/black-box fixture，同时在已授权的 8-error case 上验证
  driver/load/path 正结果、负结果 fallback、error receipt 和 clean-cache
  preference。
- [ ] **根据真实证据决定 rollout。** 决定 producer-side candidate 是复用
  `TRACEWEAVE_NPI_ALLOW_DEGRADED_KDB`，还是首个版本使用单独 opt-in。
  必须继续提供 clean-only escape hatch。

把 VHDL library 加入 `vericom`/`elabcom`、修复 encrypted-IP 环境或
修正 source filelist 是正交工作：它们可能消除 KDB error；本节
工作项只负责让仍然是 partial 的生成产物能够被安全消费。

---

## 附录 A：原始方案与当前实现的差异

原始方案的核心方向是正确的：区分“完全加载失败”和“elaboration 有 error
但部分 netlist 可用”。不过针对当前架构，最终实现比原始方案更保守：

| 原始方案倾向 | 当前实现 |
|---|---|
| 对一般 `rc != 1` 尝试 degraded | 只允许 `rc == 0`；其他返回码仍失败 |
| 非空 `get_top_inst_list()` 即可 | 名称可读取时还必须匹配 requested top |
| degraded 查询仍可报告整体 exact | 具体正事实可 exact，但全局 loads/attempt coverage 为 approximate/partial |
| 不可解析 signal 直接返回带解释的 NPI 负结果 | degraded 负结果不可信，继续 Source Graph/Static |
| LSF 可由 parent 根据 marker 推断 degraded | worker 显式传递实际 clean/degraded load quality |
| degraded metadata 可临时放进 operation result | metadata 保持在 operation schema 之外，通过专门 receipt 传递 |
| 只需修改 NPI gate | 同时修复 loads/path 顶层及 path-hop provenance |
| 未覆盖多步 X trace | 任一步不确定即丢弃整条 NPI 链，从原始 signal 重跑 |

原始 acceptance 中“degraded loads 的整体
`completeness == "exact"`”不再是当前 public contract。当前正确预期是：

```text
具体正向 NPI fact：可以是 exact
degraded loads 整体 completeness：approximate
degraded backend attempt coverage：partial
degraded 空/负结果：inconclusive，继续 fallback
```

---

## 附录 B：泛化后的快速事实

- 现场 KDB：`<build_dir>/simv.daidir/kdb.elab++`，有 N 个 elaboration
  error，`load_design` 返回 `rc == 0`。
- error log：同一 KDB 下的 `elabcomLog/compiler.log`；
  `.hasElabcomError` marker 指向它。
- LSF 环境：

  ```bash
  export TRACEWEAVE_NPI_EXECUTION=lsf
  export TRACEWEAVE_NPI_LSF_QUEUE="<licensed-queue>"
  export TRACEWEAVE_NPI_LSF_PYTHON="<repo-root>/.venv/bin/python"
  ```

- `VERDI_HOME` 应指向与 KDB 兼容的 Verdi 版本。
- KDB、TraceWeave 安装和 staging 目录必须在 submission/compute node 上
  以相同绝对路径可见。
- degraded consumer 开关：

  ```bash
  export TRACEWEAVE_NPI_ALLOW_DEGRADED_KDB=0
  ```

  默认值为开启；设置为 `0` 可恢复 clean-only 行为。
