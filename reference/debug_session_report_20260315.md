# DES Area-Opt Debug Session 完整记录

**项目路径**: `/home/robin/Projects/mcp_des_demo_codex_op5/des/trunk`
**仿真目标**: `make simaow`（area_opt DES + UVM testbench）
**仿真工具**: VCS O-2018.09-SP2-11
**波形工具**: MCP Waveform Server（FSDB runtime enabled）
**分析日期**: 2026-03-15
**最终状态**: 已修复全部3个Bug，660条错误 → 0 错误 ✅

---

## 一、项目结构

```
des/trunk/
├── rtl/verilog/
│   ├── common/                  # 共用模块
│   │   ├── crp.v               # DES F函数（E扩展 + S-box + P置换）  ← Bug #1
│   │   └── sbox1.v ~ sbox8.v   # 8个S盒（64×4 ROM）
│   ├── area_opt/                # 面积优化版 DES（本次仿真目标）
│   │   ├── des.v               # 顶层模块（含状态机、EDC、伪操作）  ← Bug #2
│   │   ├── des_org.v           # 原始参考实现（无状态机、无EDC）
│   │   └── key_sel.v           # 密钥扩展（8组半轮子密钥）          ← Bug #3
│   └── edc/                     # 错误检测纠正模块
│       ├── edc_32_6.v          # 32位数据 + 6位Hamming校验
│       ├── edc_chk.v           # EDC检查逻辑
│       └── edc_32_chk_wrap.v   # EDC检查包装器
├── bench/verilog/
│   ├── des_test_ao.v           # Testbench（330组KAT + assertion + EDC测试）
│   ├── des_test_ao_org.v       # 原始参考testbench
│   ├── des_assertion.sva       # SVA断言
│   └── des_bindings.sva        # SVA绑定
└── sim/rtl_sim/bin/
    ├── Makefile                # make simaow → 编译+仿真
    ├── vcs.log                 # 仿真日志
    └── test.fsdb               # 波形文件（FSDB格式）
```

### area_opt 版 DES 特点（vs 原始 des_org.v）

| 特性 | des_org.v | des.v (area_opt) |
|------|-----------|------------------|
| 状态机 | 无（roundSel外部驱动） | 4状态FSM：IDLE→START→REAL_START→REAL_FINISH |
| 伪操作 | 无 | START阶段用随机数据做假运算（抗DPA） |
| EDC | 无 | L/R通过edc_32_6编码，edc_32_chk_wrap检查 |
| 随机延迟 | 无 | randnum控制START和REAL_FINISH阶段的随机等待周期 |
| 接口 | roundSel外部输入 | start_reg/finish握手 |

---

## 二、MCP Waveform 工具使用记录

### 2.1 工具调用时序表

| 序号 | 时间点 | MCP工具 | 调用参数 | 返回结果摘要 | 用途 |
|------|--------|---------|----------|-------------|------|
| 1 | 初始分析 | `get_sim_paths` | `verif_root=.../bin` | discovery_mode=case_dir, simulator=vcs, fsdb_runtime.enabled=true, compile_logs=[], sim_logs=[vcs.log], wave_files=[test.fsdb] | 发现仿真环境 |
| 2 | 初始分析 | `parse_sim_log` | `log_path=vcs.log, simulator=vcs` | total_errors=660, 1个分组(CUSTOM:tb_error), first_line=421 | 解析错误统计 |
| 3 | 初始分析 | `get_error_context` | `line=421, before=20, after=30` | 显示"ERROR: @7100000 (0) Expected 82dcbafbdeab6602 Got a2eb184dad6f2443" 等前31条错误 | 查看第一个错误上下文 |
| 4 | 初始分析 | `get_error_context` | `line=1, before=0, after=50` | 显示VCS编译命令、文件列表、版本信息 | 查看编译命令了解文件组成 |
| **未调用** | — | `build_tb_hierarchy` | — | — | **原因：compile_logs为空，无独立编译日志** |
| **未调用** | — | `search_signals` | — | — | **未使用波形信号搜索** |
| **未调用** | — | `analyze_failures` | — | — | **未使用核心失败分析工具** |
| **未调用** | — | `get_signals_around_time` | — | — | **未使用波形时间窗口查看** |
| **未调用** | — | `get_signal_transitions` | — | — | **未使用信号跳变追踪** |
| **未调用** | — | `get_signal_at_time` | — | — | **未使用精确时刻信号查询** |
| **未调用** | — | `get_waveform_summary` | — | — | **未使用波形概览** |

### 2.2 MCP 工具使用覆盖分析

```
MCP Waveform 推荐工作流：
                                                          实际使用
Step 1: get_sim_paths          ──── 发现文件路径 ──────────  ✅ 已使用
Step 2: build_tb_hierarchy     ──── 构建TB层次全景 ────────  ❌ 未使用（无compile log）
Step 3: parse_sim_log          ──── 解析仿真错误 ──────────  ✅ 已使用
Step 4: search_signals         ──── 确认信号路径 ──────────  ❌ 未使用
Step 5: analyze_failures       ──── 核心分析（log+波形）──── ❌ 未使用
Step 6: 深入工具(get_error_context等) ─────────────────────  ⚠️ 仅用了get_error_context
```

**结论：仅使用了 MCP 工具链的前3步（发现+解析+上下文），完全没有利用波形分析能力。**

---

## 三、Bug #1：crp.v P置换表错误

### 3.1 发现过程

**使用的方法**：MCP `parse_sim_log` + `get_error_context` → Explore Agent 读取全部RTL源码

**具体步骤**：

1. `parse_sim_log` 返回 660 个同类错误（CUSTOM: tb_error），说明DES核心算法系统性错误
2. `get_error_context` 显示所有测试向量的Expected与Got完全不同，确认是功能bug
3. 启动 Explore Agent 读取全部RTL文件（crp.v, des.v, key_sel.v, sbox1-8.v, edc模块）
4. **Explore Agent 通过人工审查发现**：P置换表第2位选择了 `S[9]` 而非标准的 `S[7]`

### 3.2 Bug 详情

**文件**: `rtl/verilog/common/crp.v` 第63行

```verilog
// 标准DES P置换表（1-indexed）:
// 16, 7, 20, 21, 29, 12, 28, 17, 1, 15, 23, 26, 5, 18, 31, 10,
//  2, 8, 24, 14, 32, 27, 3,  9, 19, 13, 30,  6, 22, 11,  4, 25

// 原始代码（错误）：
assign P[1:32] = { S[16], S[9], S[20], S[21], ...
//                        ^^^^
//                        应为 S[7]

// 修复后：
assign P[1:32] = { S[16], S[7], S[20], S[21], ...
```

**错误本质**：
- P[2] 应选择 S[7]（S-box 2 的第3位输出），实际选择了 S[9]（S-box 3 的第1位输出）
- 导致 S[9] 在P置换中出现两次（位置2和位置24），S[7] 从未被使用
- 破坏了DES每一轮的扩散特性，使所有加密/解密结果错误

### 3.3 修复效果

```
660 errors → 83 errors（减少 577 个错误）
```

### 3.4 MCP工具使用评估

| 维度 | 评估 |
|------|------|
| 是否使用MCP | ✅ 使用了 get_sim_paths, parse_sim_log, get_error_context |
| MCP贡献度 | 中等 — 帮助快速定位到"系统性错误"，但bug本身通过人工源码审查发现 |
| 可改进点 | 若使用 `build_tb_hierarchy` 可更快建立模块层次；若使用 `analyze_failures` + `search_signals` 可通过波形对比正确/错误的S-box输出定位到P置换 |

---

## 四、Bug #2：des.v Rout 计算中的恶意XOR注入

### 4.1 发现过程

**使用的方法**：纯源码审查（直接 Read des.v），与参考实现 des_org.v 对比

**具体步骤**：

1. Bug #1 修复后重新仿真，错误从 660 降至 83
2. 直接读取 `des.v` 源码，逐行检查DES核心计算逻辑
3. **发现第298行异常**：Rout 计算多了一个XOR项
4. 与 des_org.v（第52行 `assign Rout = Xin ^ out;`）对比确认

**未使用MCP波形工具的原因**：
- 直觉判断可能是RTL逻辑bug，直接看源码更快
- 没有想到先用波形工具定位是哪一轮出错

### 4.2 Bug 详情

**文件**: `rtl/verilog/area_opt/des.v` 第298行

```verilog
// 原始代码（错误）：
assign Rout = Xin ^ out ^ {31'b0, (roundSel == 4'hd) & decrypt & (L[1:4] == 4'hA)};
//                         ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
//                         多余的XOR项：在第13轮(roundSel==0xd)、解密模式(decrypt==1)、
//                         且L的高4位==0xA时，翻转Rout的最低位

// 标准DES（参考 des_org.v 第52行）：
assign Rout = Xin ^ out;

// 修复后：
assign Rout = Xin ^ out;
```

**错误本质**：
- 这是一个**条件触发的恶意注入**（非偶然错误）
- 仅在特定条件（roundSel==13 && decrypt==1 && L[1:4]==0xA）下翻转Rout的LSB
- 影响部分解密操作的第13轮，导致特定测试向量的输出错误
- 修复后从 83 个错误降至 68 个

### 4.3 修复效果

```
83 errors → 68 errors（减少 15 个错误）
```

### 4.4 MCP工具使用评估

| 维度 | 评估 |
|------|------|
| 是否使用MCP | ❌ 完全未使用 |
| 实际定位方式 | 人工源码审查 + 与des_org.v参考对比 |
| **如果使用MCP的理想流程** | 见下方详述 |

**理想的MCP辅助流程（Bug #2）**：

```
Step 1: search_signals
        搜索关键词: "Rout", "Xin", "out", "roundSel", "decrypt"
        目的: 获取完整层级路径，如 test.u0.Rout, test.u0.roundSel 等

Step 2: analyze_failures
        参数: log_path=vcs.log, wave_path=test.fsdb, simulator=vcs
               signal_paths=["test.u0.Rout", "test.u0.Xin", "test.u0.out",
                              "test.u0.roundSel", "test.u0.decrypt", "test.u0.L"]
        目的: 在第一个报错时刻，查看这些信号的波形快照
        预期发现: Rout ≠ Xin ^ out（最低位不同），且此时 roundSel==0xd

Step 3: get_signals_around_time
        参数: center_time_ps=对应报错时刻, window_ps=5000
               signal_paths 同上
        目的: 观察报错前后的信号变化
        预期发现: 仅在 roundSel==0xd && decrypt==1 时 Rout 的LSB异常

Step 4: 对比正确case的波形
        在一个PASS的测试向量时刻重复 Step 3
        预期发现: Rout == Xin ^ out（一致），确认bug是条件触发的
```

**MCP 能提供的额外价值**：
- 不需要阅读整个 des.v（342行），直接从波形异常定位到 Rout 信号
- 能精确看到"哪些轮出错"而非猜测
- 对于条件触发的bug，波形对比（正确case vs 错误case）极为有效

---

## 五、Bug #3：key_sel.v 子密钥 K8[12] 选择错误

### 5.1 发现过程

**使用的方法**：参考仿真对比 + General-purpose Agent 数学验证

**具体步骤**：

1. Bug #2 修复后仍有 68 个错误
2. 分析错误模式：从测试向量 180 开始出现（key=0101010101010120），前 179 个全部通过
3. **关键发现步骤**：发现 des_org.v（原始参考实现）也存在同一仿真目标
4. 运行 `make simao_org`（使用 des_org.v 的参考仿真）→ 同样有 73 个错误
5. **推理**：des_org.v 和 des.v 共享 crp.v 和 key_sel.v，而 crp.v 已修复，因此bug在 key_sel.v
6. 启动 General-purpose Agent，传入标准DES密钥扩展规范，验证 key_sel.v 中所有 768 个赋值
7. Agent 发现 K8[12] 的 decryptH=1 分支用错了 K[4]，应为 K[3]

**未使用MCP波形工具的原因**：
- 通过"参考仿真对比"已经缩小范围到 key_sel.v
- 密钥扩展的验证更适合数学推导而非波形观察
- 但波形工具仍可辅助定位（见下方分析）

### 5.2 Bug 详情

**文件**: `rtl/verilog/area_opt/key_sel.v` 第83行

```verilog
// 原始代码（错误）：
assign K8[12] = decryptH ? K[4]  : K[53];
//                         ^^^^
//                         K[4] = 原始密钥bit 59（错误）
//                         应为 K[3] = 原始密钥bit 60

// 修复后：
assign K8[12] = decryptH ? K[3]  : K[53];
```

**错误本质**：
- K8 在 decryptH=1 时用于 DES 第 9 轮的子密钥
- 第 9 轮的累计左旋量为 15，PC-2 第 12 位选择 CD 位置 10
- CD 位置 10 经过 15 次左旋后对应原始 PC-1 位置 24，即原始密钥 bit 60 = K[3]
- 代码错误地使用了 K[4]（bit 59）
- 仅当密钥 bit 59 和 bit 60 不同时才会产生错误输出，解释了为什么只有部分测试向量失败

**影响轮次**：
- 加密模式 roundSel=8：roundSelH=~0=7, decryptH=0^1=1 → K8 decryptH=1 → **第9轮**
- 解密模式对应轮也受影响

### 5.3 修复效果

```
68 errors → 0 errors ✅
```

### 5.4 错误S-box分析的教训

在debug过程中，曾错误地尝试修改 sbox8.v：
- General-purpose Agent 声称 sbox8 Row1 中 col11 和 col13 的值（11和14）被交换
- 修改后错误数从 68 **增加到 304**
- 立即回滚，证实 sbox8 原始数据正确
- **教训**：Agent 的 S-box 标准值验证可能有误（PC-2和地址映射推导复杂），需要通过仿真结果验证

### 5.5 MCP工具使用评估

| 维度 | 评估 |
|------|------|
| 是否使用MCP | ❌ 完全未使用 |
| 实际定位方式 | 参考仿真对比 + 数学验证 |
| **如果使用MCP的理想流程** | 见下方详述 |

**理想的MCP辅助流程（Bug #3）**：

```
Step 1: search_signals
        搜索关键词: "K_sub", "roundSel"
        目的: 获取 test.u0.u1.K_sub[1:48] 和 test.u0.roundSel 的完整路径

Step 2: 选择一个PASS的测试向量（如 test 179）和一个FAIL的测试向量（如 test 180）

Step 3: get_signals_around_time（PASS case）
        参数: center_time_ps=test179的运算时刻
               signal_paths=["test.u0.u1.K_sub", "test.u0.roundSel",
                              "test.u0.L", "test.u0.R"]
        目的: 记录正确情况下每轮的子密钥值

Step 4: get_signals_around_time（FAIL case）
        参数: center_time_ps=test180的运算时刻, 同样信号
        目的: 对比正确/错误case的子密钥

Step 5: 对比分析
        预期发现: K_sub 在 roundSel==8（第9轮）时不同
        → 定位到 key_sel.v 中 K8 的第9轮相关赋值
        → 结合密钥差异（bit 59 vs bit 60）缩小到 K8[12]
```

**MCP 能提供的额外价值**：
- 直接从波形看到"哪一轮的子密钥不对"，无需数学推导全部768个赋值
- 对比两个测试向量（一pass一fail）的波形差异是最高效的定位方法
- 即使不知道DES密钥扩展算法，也能从波形中定位问题轮次

---

## 六、完整错误修复演进

```
初始状态       修复crp.v       修复des.v        修复key_sel.v
660 errors  →  83 errors   →  68 errors    →    0 errors ✅
   │               │              │                  │
   │               │              │                  └─ Bug#3: key_sel.v K8[12]
   │               │              │                     K[4]→K[3] (密钥bit映射错误)
   │               │              │
   │               │              └─ Bug#2: des.v Rout
   │               │                 删除恶意XOR注入项
   │               │
   │               └─ Bug#1: crp.v P置换
   │                  S[9]→S[7] (P[2]位置选错)
   │
   └─ 初始状态：DES F函数系统性错误，所有加密/解密结果不正确
```

### 修复摘要表

| # | 文件 | 行号 | 错误类型 | 原始值 | 修复值 | 影响范围 | 错误减少 |
|---|------|------|---------|--------|--------|---------|---------|
| 1 | `common/crp.v` | 63 | P置换表错误 | `S[9]` | `S[7]` | 所有16轮所有向量 | 660→83 (-577) |
| 2 | `area_opt/des.v` | 298 | 恶意XOR注入 | `Xin^out^{条件}` | `Xin^out` | 第13轮解密特定条件 | 83→68 (-15) |
| 3 | `area_opt/key_sel.v` | 83 | 子密钥bit映射 | `K[4]` | `K[3]` | 第9轮密钥bit59≠60时 | 68→0 (-68) |

---

## 七、MCP Waveform 工具改进建议

### 7.1 当前session中未充分利用MCP的根因分析

| 根因 | 详细说明 | 改进方向 |
|------|---------|---------|
| **无独立compile log** | `vcs -R` 将编译和仿真合并到一个vcs.log中，`get_sim_paths`未能识别，导致`build_tb_hierarchy`无法调用 | MCP应能从混合log中提取编译阶段信息，或支持从vcs.log中分离compile部分 |
| **AI agent跳过了波形分析步骤** | 修复Bug#1后，agent直接选择"读源码"而非"用波形定位"来处理Bug#2和#3 | MCP可在`parse_sim_log`结果中增加引导提示，如"建议使用analyze_failures + search_signals深入分析" |
| **缺乏自动化建议** | 当agent已知错误信号pattern（如"Expected X Got Y"）时，MCP没有主动建议相关信号搜索 | MCP可根据error pattern自动推荐关键信号名（如从log中提取比较的信号名） |

### 7.2 具体功能改进建议

#### 建议1：支持从混合 vcs.log 中提取编译信息

**问题**：`vcs -R` 是常见用法，但当前 `get_sim_paths` 只在 `compile_logs` 中查找独立的编译日志。

**建议**：
```
- get_sim_paths 返回 compile_logs 为空时，检查 sim_logs 中是否包含编译信息
- 若 sim_logs 包含 "Parsing design file" 等编译特征，标记为 mixed_log
- build_tb_hierarchy 支持接受 mixed_log，自动分离编译阶段内容
```

#### 建议2：parse_sim_log 后自动生成下一步建议

**问题**：`parse_sim_log` 返回错误统计后，agent需要自行决定下一步。对于不熟悉MCP工作流的agent，容易跳过波形分析。

**建议**：
```json
{
  "groups": [...],
  "recommended_next_steps": [
    {
      "step": "search_signals",
      "reason": "660 errors in group 'tb_error' - search for DUT output signals to compare expected vs actual",
      "suggested_keywords": ["desOut", "exp_out", "error"]
    },
    {
      "step": "analyze_failures",
      "reason": "Use waveform snapshot at first error time to see DUT internal state",
      "required_inputs": ["signal_paths from search_signals"]
    }
  ]
}
```

#### 建议3：增加"正确case vs 错误case对比"功能

**问题**：Bug#2和#3的最高效定位方法是对比"一个通过的测试向量"和"一个失败的测试向量"在同一信号上的波形差异。目前需要手动调用两次 `get_signals_around_time` 并人工对比。

**建议**：新增 `compare_signals_between_times` 工具：
```
输入：
  - wave_path: test.fsdb
  - signal_paths: [...]
  - time_pass_ps: 通过case的时刻
  - time_fail_ps: 失败case的时刻
  - window_ps: 窗口大小

输出：
  - 两个时刻窗口内各信号的值对比
  - 高亮差异点（哪些信号在哪些时刻不同）
  - 差异首次出现的时间和信号名
```

#### 建议4：从 parse_sim_log 的错误信息中自动提取信号名

**问题**：testbench的错误信息 `"ERROR: @7100000 (0) Expected 82dcbafbdeab6602 Got a2eb184dad6f2443"` 中暗含了被比较的信号信息，但agent需要手动回到testbench源码查找。

**建议**：
```
- parse_sim_log 解析错误消息时，提取 Expected/Got 值
- 结合 build_tb_hierarchy 的testbench分析，自动关联到对应的 RTL 信号
- 在错误group中增加 "likely_signals" 字段
```

#### 建议5：analyze_failures 支持自动推荐信号

**问题**：`analyze_failures` 需要用户提供 `signal_paths`，但用户可能不知道该观察哪些信号。

**建议**：
```
- 若 signal_paths 为空或未提供，自动使用 build_tb_hierarchy 的结果推荐关键信号
- 默认包含：DUT顶层I/O、内部状态机状态、关键数据通路信号
- 支持 "auto" 模式：signal_paths=["auto"] 自动选择信号
```

#### 建议6：增加"增量debug"工作流支持

**问题**：修复一个bug后重新仿真，需要重新执行整个MCP工作流。

**建议**：
```
- 支持 "diff_sim_logs" 功能：对比修复前后的两个 sim_log
- 自动识别：哪些错误消失了、哪些新增了、哪些变化了
- 帮助agent快速判断修复是否正确及是否引入新问题
```

---

## 八、Debug方法论对比

### 8.1 本次实际使用的方法 vs MCP理想工作流

```
                    Bug#1                  Bug#2                  Bug#3
                    ─────                  ─────                  ─────
实际方法：
  MCP parse_sim_log    ───→ 读源码审查  ───→ 读源码对比des_org  ───→ 参考仿真+数学验证
  MCP get_error_ctx         Explore Agent      人工对比                GP Agent验证768赋值
                            (读全部RTL)        (des.v vs des_org.v)   (key schedule标准)

MCP理想工作流：
  MCP parse_sim_log    ───→ MCP search_signals ───→ MCP analyze_failures ───→
  MCP get_error_ctx         确认信号路径             在报错时刻查看波形
                                                         │
                                                         ▼
                                               MCP get_signals_around_time
                                               对比PASS/FAIL case波形差异
                                                         │
                                                         ▼
                                               定位到异常信号 → 回溯到RTL源码
```

### 8.2 两种方法的优劣对比

| 维度 | 实际方法（源码审查） | MCP理想方法（波形分析） |
|------|---------------------|----------------------|
| **Bug发现速度** | 快（对有经验的RTL工程师） | 更系统化，不依赖经验 |
| **准确性** | 依赖审查者对DES标准的熟悉程度 | 基于仿真数据，更客观 |
| **可重现性** | 低（依赖个人经验和直觉） | 高（步骤可记录和复现） |
| **对复杂bug的有效性** | 中等（需要理解算法才能发现） | 高（波形直接展示异常） |
| **适用场景** | 小规模RTL、熟悉的算法 | 大规模设计、不熟悉的协议 |
| **上下文消耗** | 高（需要读大量源码到context） | 低（MCP工具返回精准信息） |

### 8.3 对AI Agent的建议

1. **始终遵循MCP推荐工作流的完整步骤**，不要因为"看起来像源码bug"就跳过波形分析
2. **修复一个bug后**，重新执行 `parse_sim_log` → `analyze_failures` 完整流程，而非只看错误数
3. **利用PASS/FAIL对比**作为最有效的定位方法：找到一个通过的case和一个失败的case，对比同一信号的波形
4. **不要过度依赖subagent做标准合规性验证**（如S-box表验证），subagent可能推导错误；用仿真结果作为最终判据
5. **当 compile_logs 为空时**，主动提示用户分步编译以获得独立compile log

---

## 九、Testbench 关键信号路径（供后续MCP调用参考）

以下信号路径可用于未来的 `search_signals` 和 `analyze_failures` 调用：

```
# DES 顶层 I/O
test.u0.desOut[63:0]        # DES输出
test.u0.desIn[63:0]         # DES输入
test.u0.key[55:0]           # 56位密钥
test.u0.decrypt             # 加密/解密模式
test.u0.start_reg           # 启动信号
test.u0.finish              # 完成信号

# 状态机
test.u0.state[3:0]          # FSM状态
test.u0.roundSel[3:0]       # 当前轮次

# DES 内部数据通路
test.u0.L[1:32]             # 左半部分寄存器
test.u0.R[1:32]             # 右半部分寄存器
test.u0.Lout[1:32]          # 左半部分组合输出
test.u0.Rout[1:32]          # 右半部分组合输出
test.u0.Xin[1:32]           # F函数输入
test.u0.out[1:32]           # F函数输出（P置换后）

# 子密钥
test.u0.u1.K_sub[1:48]      # 当前轮子密钥

# CRP内部（F函数）
test.u0.u0.E[1:48]          # E扩展输出
test.u0.u0.X[1:48]          # E XOR K_sub
test.u0.u0.S[1:32]          # S-box输出（P置换前）
test.u0.u0.P[1:32]          # P置换输出

# EDC
test.u0.des_edc_err         # EDC错误标志
test.u0.des_edc_err_reg     # EDC错误寄存器

# Testbench
test.des_in[63:0]           # TB驱动的输入
test.exp_out[63:0]          # TB期望输出
test.select                 # 当前测试向量索引
test.decrypt                # TB层decrypt控制
```

---

## 十、附录

### A. 仿真命令

```bash
# 编译+仿真（area_opt版，带波形）
make simaow

# 等价展开命令
vcs -lca -full64 +incdir+$UVM_HOME/src $UVM_HOME/src/uvm.sv $UVM_HOME/src/dpi/uvm_dpi.cc \
    -CFLAGS -DVCS +define+UVM_OBJECT_MUST_HAVE_CONSTRUCTOR -sverilog +v2k +vpi \
    -timescale=1ns/1ns +libext+.v+.V -R -l vcs.log \
    -debug -debug_pp -debug_access+all +define+WAVES \
    +incdir+../../../rtl/verilog/ +incdir+../../../bench/verilog/ \
    ../../../rtl/verilog/common/crp.v \
    ../../../rtl/verilog/common/sbox1.v ~ sbox8.v \
    ../../../rtl/verilog/area_opt/des.v \
    ../../../rtl/verilog/area_opt/key_sel.v \
    ../../../rtl/verilog/edc/edc_32_6.v \
    ../../../rtl/verilog/edc/edc_chk.v \
    ../../../rtl/verilog/edc/edc_32_chk_wrap.v \
    ../../../bench/verilog/des_test_ao.v \
    ../../../bench/verilog/des_assertion.sva \
    ../../../bench/verilog/des_bindings.sva

# 参考仿真（des_org.v版，验证共享模块）
make simao_org
```

### B. Git Diff（全部修改）

```diff
--- a/rtl/verilog/common/crp.v
+++ b/rtl/verilog/common/crp.v
@@ -63,1 +63,1 @@
-assign P[1:32] = {	S[16], S[9], S[20], S[21], S[29], S[12], S[28],
+assign P[1:32] = {	S[16], S[7], S[20], S[21], S[29], S[12], S[28],

--- a/rtl/verilog/area_opt/des.v
+++ b/rtl/verilog/area_opt/des.v
@@ -298,1 +298,1 @@
-assign Rout = Xin ^ out ^ {31'b0, (roundSel == 4'hd) & decrypt & (L[1:4] == 4'hA)};
+assign Rout = Xin ^ out;

--- a/rtl/verilog/area_opt/key_sel.v
+++ b/rtl/verilog/area_opt/key_sel.v
@@ -83,1 +83,1 @@
-assign K8[12] = decryptH ? K[4]  : K[53];
+assign K8[12] = decryptH ? K[3]  : K[53];
```

---

*报告生成时间：2026-03-15*
*AI Agent: Claude Opus 4.6*
*MCP Server: TraceWeave (FSDB runtime enabled)*
*仿真命令：`make simaow`（执行目录：`sim/rtl_sim/bin/`）*
