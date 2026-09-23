# SV / Verilog 表达式查询

更新：2026-09-23。复用现有 MCP 工具，不新增表达式工具或持久化虚拟信号。
普通字符串仍表示实际信号路径；公式必须放在显式 `expr` 对象中。

## 直接运行客户端示例

在仓库根目录运行 `.venv/bin/python scripts/run_expression_examples.py`。
脚本使用仓库自带的 `examples/expressions/demo.vcd`，启动真实 MCP stdio 连接，
先用 `tools/list` 的 schema 验证输入，再调用现有工具并比较手工计算的预期。
不需要仿真器。运行环境需有仓库的 MCP、anyio、jsonschema 依赖。

| 示例 | 工具 | 预期 |
|---|---|---|
| 动态 bit：`a[i] + e` | `get_signal_at_time` | 5ps 时为 6 |
| 动态数组：`mem[i]`，显式元素和类型 | `get_signal_transitions` | 0、10、20ps 时分别为 17、34、51 |
| 显式类型和常量：`a[i -: W]`，W=2 | `get_signals_by_cycle` | 四拍为 2、1、2、X；下标每拍重算 |
| 条件窗口：`a[i] == 1'b1` | `verify_window` | `holds=false`，有反例，无 unknown cycle |

四个工具的主说明和 `inputSchema.examples` 提供同一组完整参数，来自
`src/expression_examples.py`。将示例 `wave_path` 替换为上述 VCD 的绝对路径即可调用。
脚本输出实际参数和检查结果。第四拍 X 是波形中实际的未知下标；该例的
`coverage_status=complete`，展示覆盖完整不等于值已知。这里验证客户端通路，
不代表 AI Agent 的自主使用效果。

## 点查询与周期查询

```json
{
  "wave_path": "/path/to/run.fsdb",
  "signal_path": {
    "expr": "a[b] + c",
    "typing": "wave_bits",
    "bindings": {
      "a": "tb.dut.a[7:0]",
      "b": "tb.dut.b[2:0]",
      "c": "tb.dut.c[7:0]"
    }
  },
  "time_ps": "100ns"
}
```

以上传给 `get_signal_at_time`。若 a=8'b00001000、b=3、c=5，得到 6；
b 改为 4 后得到 5。返回 `expressions` receipt，记录推导类型、真实依赖、
本次选中的位和下标证据。原来的 y 信号查询继续返回 y 的实际记录值。

将同一个表达式对象放进 `get_signals_by_cycle.signal_paths`，即可每拍重算；
`clock_path` 可使用真实时钟。表达式不会固定第一次看到的下标。

周期查询用 `start_cycle` 或 `start_time_ps` 指定起点，用 `num_cycles` 或
`end_time_ps` 指定范围；每一对参数只能选一个。单次最多返回 256 拍，需读取
`effective_num_cycles` / `capped` 并继续查询剩余范围。`sample_offset_ps` 只能为
非负整数，默认在边沿后 1ps 采样。需要某个边沿前的点值时，可向
`get_signals_around_time` 提供相应的明确时间。

## 类型与精确绑定

- 默认 `typing: "semantic"`。公开查询可通过 `types` 提供类型；当前公开接口
  不会为任意公式自动启动编译前端。没有类型时返回明确缺口。
- `typing: "wave_bits"` 是调用方选择的 unsigned 四态向量视图。波形宽度
  不能证明原 RTL 的 signed、数组维度或结构体布局。
- `bindings` 将公式名称对应到真实 dump 路径或已有 `{path,bits}` 固定选择。
  `scope` 可以提供名称前缀。使用 `search_signals` 返回的精确路径，尤其是 FSDB。
- `types` 可描述 `width`、`signed`、`two_state`、`packed`、`unpacked`、`members`；
  `width` 为单个 packed 元素的总位宽。维度使用 `[left,right]`，成员使用
  `{name,lsb,type}`，`lsb` 为 packed 布局内偏移。已解析的类型也可用于命名 cast。
- `constants` 是显式常量表达式绑定。不会展开宏、执行函数或遍历工程猜参数。
  RTL 自动回溯则使用匹配编译上下文中的 Slang/NPI 事实。

例如有符号右移：

```json
{
  "expr": "a >>> shift",
  "bindings": {"a": "tb.a[7:0]", "shift": "tb.shift[2:0]"},
  "types": {
    "a": {"width": 8, "signed": true, "packed": [[7, 0]]},
    "shift": {"width": 3, "packed": [[2, 0]]}
  }
}
```

## 固定数组与字段

数组使用实际元素的稀疏映射，不枚举整个声明范围：

```json
{
  "expr": "mem[idx] + 8'd1",
  "bindings": {
    "idx": "tb.idx[2:0]",
    "mem": {"elements": [
      {"indices": [2], "signal": "tb.mem[2][7:0]"},
      {"indices": [3], "signal": "tb.mem[3][7:0]"}
    ]}
  },
  "types": {
    "idx": {"width": 3, "packed": [[2, 0]]},
    "mem": {"width": 8, "packed": [[7, 0]], "unpacked": [[0, 7]]}
  }
}
```

idx=2/3 时只读取选中的元素；idx=4 在声明内但没有 dump 映射，返回缺失证据。
`scope` 只补全名称前缀，不会自动发现数组元素。即使使用 `wave_bits`，
unpacked 数组仍需要 `elements` 和 `types` 中的维度；范围边界使用 JSON 整数，
例如 `[[0,7]]`，不能写成字符串 `[["0","7"]]`。
越界和未知索引按元素的二态/四态类型产生默认值，并保留下标诊断，
不能把未 dump 等同于实际观测 X。

多维 unpacked 使用多个维度和 `indices: [i,j]`；多维 packed 使用完整
`packed` 维度。packed struct/union 通过 `members` 提供布局后可写
`entries[i].data[j]`。packed 结构体数组的 `members` 描述最内层结构体，
`packed` 的最后一维为该结构体的扁平位范围。例如三个 56-bit 结构体用
`width:168, packed:[[2,0],[55,0]]`，字段偏移在每个 56-bit 元素内计算。
每个映射必须对应实际声明或明确固定选择；不会通过
拼接相似名称的片段补成一个不存在的 aggregate。只有字段被 dump 时，可把
该真实字段作为独立绑定查询；自动数组读取需要可验证的完整元素映射。

RTL 自动分析保留数组形状，按语义元素身份核对精确 dump 声明；同时识别 VCD
的 escaped 元素名称。历史回溯可以读元素值，但会在存储边界停止，不从写操作
重建未 dump 内存。generate/实例数组下标仍是展开后的常量身份。

## 运算覆盖与优先级

下表从高到低列出主要运算；括号可以显式分组。

| 层级 | 运算 |
|---|---|
| 选择、转换 | `a[i]`、`a[base +: W]`、`a[base -: W]`、字段、合法 integral cast |
| 单目、归约 | `+ - ! ~ & ~& \| ~\| ^ ~^ ^~` |
| 幂 | `**`，左结合 |
| 乘除 | `* / %` |
| 加减 | `+ -` |
| 移位 | `<< >> <<< >>>` |
| 关系、集合 | `< <= > >= inside` |
| 相等 | `== != === !== ==? !=?` |
| 按位 | `&`，随后 `^ ~^ ^~`，随后 `\|` |
| 逻辑 | `&&`，随后 `\|\|` |
| 条件 | `?:`，右结合 |
| 逻辑蕴含、等价 | `-> <->`，右结合；这里不是时序断言 |

还支持拼接 `{a,b}`、常量次数复制 `{N{a}}`、固定形状 streaming
`{<<4{a}}` / `{>>{a,b}}`、有限 scalar 集合/范围的 `inside`。
白名单为 `$signed/$unsigned`、`$clog2/$isunknown/$countones/$countbits/$onehot/$onehot0`，
以及类型查询 `$bits/$size/$left/$right/$low/$high/$increment/$dimensions/$unpacked_dimensions`。
类型查询不读取第一个参数的当前值；显式提供类型后，该参数可以是未 dump 的变量或类型名，
例如 `$bits(mem)`、`$bits(int)`。`$size/$left/$right/$low/$high/$increment` 的第二个参数
可以是动态维度表达式，每次观察都会重算：

```json
{
  "expr": "$size(mem, dim)",
  "bindings": {"dim": "tb.dim[2:0]"},
  "types": {
    "mem": {"width": 8, "packed": [[7, 0]], "unpacked": [[1, 2], [0, 2]]},
    "dim": {"width": 3}
  }
}
```

dim 为 1、2、3 时分别得到 2、3、8；只采样 dim，无需读取 mem 元素。
未知维度和超出维度范围返回 X，并分别记录 `dimension_unknown`、`dimension_out_of_range`。
part-select 宽度、复制次数、streaming slice size 必须是合法常量。

遵循定宽、溢出、符号扩展、四态和上下文位宽规则。未知条件的 `?:` 可以逐位合并，
即使结果确定也不证明唯一分支。输出 `dec` 按表达式 signed 类型解释，`bin` 保留原始位；
协议 ID/长度按字段位模式解释。

## 工具接入

| 入口 | 表达式位置与行为 |
|---|---|
| `get_signal_at_time` | `signal_path` |
| `get_signal_transitions` | `signal_path`；依赖事件合并后的派生变化 |
| `get_signals_around_time` | `signal_paths`；中心值及派生历史 |
| `get_signals_by_cycle` | `signal_paths`、一 bit `clock_path` |
| `diff_first_divergence` | `signal_a` / `signal_b` 独立绑定、独立类型身份 |
| `period` | 一 bit `signal`；依据真实 fs 事件计算周期 |
| `verify_window` | 条件、前/后件、序列观测值与一 bit 时钟；旧条件列表仍为 AND |
| `inspect_handshake` | valid/ready/control/payload 等信号角色 |
| `reconstruct_transactions` | 已有通道角色、ID/长度/数据字段等信号输入 |
| `inspect_tlul` | 时钟、reset 与显式映射字段 |
| `trace_divergence` / `trace_x_source` 历史模式 | 仍以真实信号为根，从 RTL 提取表达式 |

`verify_window` 的条件可以直接用 `{ "expr": "a[idx] == expected || bypass", ... }`，
类型和绑定规则与点查询一致；也可在旧 `{signal,op,value}` 的 `signal` 中放表达式。
保留模板、vacuous 和窗口末端不确定性。未知后件会记为 inconclusive；
不能用 partial coverage 下的 `holds=true` 排除问题。

协议工具维持原采样相位（事务/TL-UL 为严格边沿前），不把派生数据归给一个虚构驱动。
违规使用 `violating_expression` / `expression_key` 并列出真实依赖后续动作。
clock/valid/ready/reset 要求一 bit，`valid_htrans` 要求两 bit。

## 时间、证据和边界

派生事件使用原始 fs 时间收齐同一时间组后求值，不声称复原模拟器所有 delta-cycle。
窗口前值的 `predecessor_kind="dependency_anchor"` 表示依赖锚点，不是最近一次输出变化。
未完成时间组、缺失依赖、截断和观测 X/Z 分别记录。一 bit 派生时钟遇到组内变化、
未知或覆盖不足时不能证明边沿；`period` 不返回伪造周期。

`expressions` 包含 `kind="derived"`、类型来源、实际采样依赖与 `coverage_status`。
`observations_truncated` 是证据展示上限，与实际读取覆盖截断分开。
`complete` 表示观察覆盖完整，不表示表达式为真或结果没有 X/Z；`partial` 和
`not_observed` 不能用来排除问题。实际观测 X/Z 与未 dump、缺失元素分别记录。
自动回溯保留两侧实际值和推导值、control/index/data 角色、触发边沿与采样时刻；
选中数据不同会记 `selected_dependency_changed`，索引差异会记 `index_difference`。
NPI 缺少确定运算/类型/顺序事实时整条重启到 Slang，不混合后端来源。

初始上限：表达式文本 16,384 字符，256 节点、32 层、结果 4,096 bit；
类型最多 8 个维度，每个稀疏映射最多 128 个元素，每请求最多 128 个表达式。
事件读取另受 262,144 事件 / 64 MiB 预算、工具采样和位数预算限制，支持取消。
每个 receipt 最多保留 128 次观察及有界位数证据；超限会明确标记。

不执行赋值、自增/自减、任意用户函数/任务/DPI、类/队列/关联或运行时变长数组、
实数/字符串计算、完整过程块解释或跨时刻公式。普通查询不增加副作用，也不会
自动寻找未提供的编译上下文。`search_signals` 和 driver/load/path 查询继续处理真实对象。

## 客户端错误恢复

工具仍在 MCP 文本内容中返回 JSON。先检查 `error` 字段，不要仅凭传输成功或
MCP `isError=false` 判定求值成功。表达式错误保留 `error`，并增加稳定的
`error_code`、具体 `reason`、已知时的 `operand` / `parameter` / `position`，
以及 `recovery: {action,message}`。`position` 是表达式文本的零起始字符偏移。
MCP 层直接拒绝不符合 inputSchema 的参数时，请先按该 schema 修正；请求尚未进入求值器。

| error_code | 客户端下一步 |
|---|---|
| `expression_type_unresolved` | 提供相关操作数的声明类型；只有明确需要 unsigned 向量视图时才选择 `wave_bits` |
| `expression_signal_unresolved` | 对当前波形调用 `search_signals`，修正 `bindings`；不要猜测未 dump 值 |
| `expression_shape_invalid` | 核对 packed/unpacked 范围、成员偏移、总宽度和稀疏元素映射 |
| `expression_syntax_invalid` / `expression_unsupported` | 修正值表达式；不执行过程语句、任意函数或副作用 |
| `expression_constant_required` | 明确提供切片宽度、复制次数等常量；不把动态宽度改称受支持 |
| `expression_control_width_invalid` | 使用符合角色位宽的表达式；Boolean 控制可写显式比较 |
| `expression_limit_exceeded` | 拆分公式/依赖、减少宽度，或缩小采样窗口/周期数 |
| `expression_input_invalid` / `expression_binding_invalid` / `expression_operand_invalid` | 按 `parameter` / `operand` 修正字段、固定选择或运算数 |
| `expression_sampling_unavailable` | 检查波形版本和读取能力；点查询不能证明缺失的事件顺序 |

当 `reason="dynamic_expression_limit"` 时，超限的是表达式语法或展开后的树复杂度。
减少嵌套三元运算、冗余括号或公式规模；仅缩小时间窗无效。采样数量超限才通过
缩小窗口或周期数恢复。预算不因重试而放宽。

修正请求后再重试。数组中未映射的选中元素等缺失观察可能返回正常结果加
`coverage_status="partial"` 和 `gaps`，并非所有证据不足都抛输入错误。
