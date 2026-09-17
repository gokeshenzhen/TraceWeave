"""Independent, hand-authored timing/structure oracles for the trace benchmark."""

from dataclasses import dataclass, field


@dataclass
class Workload:
    name: str
    rtl: str
    widths: dict[str, int]
    events: list[tuple[int, dict[str, int | str]]]
    root_time: int
    nodes: list[tuple[str, str]]
    samples: dict[str, tuple[int, str, int]]
    controls: tuple[str, ...] = ()
    triggers: dict[str, int] = field(default_factory=dict)
    missing: tuple[str, ...] = ()
    edge_count: int | None = None
    end: int = 30
    allow_npi_fallback: bool = False

    @property
    def pairs(self):
        return [{"a": "top." + a, "b": "top." + b} for a, b in self.nodes[1:]]

    def waveform(self):
        codes = {
            name: f"v{i}"
            for i, name in enumerate(self.widths)
            if name not in self.missing
        }
        lines = ["$timescale 1ps $end", "$scope module top $end"]
        lines += [
            f"$var wire {self.widths[name]} {code} {name} $end"
            for name, code in codes.items()
        ]
        lines += ["$upscope $end", "$enddefinitions $end"]
        for time, changes in self.events:
            lines.append(f"#{time}")
            for name, value in changes.items():
                if name in codes:
                    bits = (
                        value
                        if isinstance(value, str)
                        else format(value, f"0{self.widths[name]}b")
                    )
                    lines.append(f"b{bits} {codes[name]}")
        lines.append(f"#{self.end}")
        return "\n".join(lines) + "\n"


def workloads():
    # Two independent lanes. All expected times/values below are explicit;
    # neither the comparison nor backtrace implementation supplies the oracle.
    header = "module top(input logic clk, en, gate, input logic [7:0] a,b,"
    register = Workload(
        "register",
        header + "output logic [7:0] qa,qb);\n"
        "always_ff @(posedge clk) if(en) qa<=a;\n"
        "always @(posedge clk) if(en) qb<=b; endmodule\n",
        {"clk": 1, "en": 1, "gate": 1, "a": 8, "b": 8, "qa": 8, "qb": 8},
        [
            (0, dict(clk=0, en=1, gate=1, a=0x55, b=0x55, qa=0, qb=0)),
            (3, dict(b=0x6A)),
            (5, dict(clk=1, qa=0x55, qb=0x6A)),
            (10, dict(clk=0)),
        ],
        5,
        [("qa", "qb"), ("a", "b")],
        {"a": (5, "before", 0x55), "b": (5, "before", 0x6A), "en": (5, "before", 1)},
        ("clk", "en"),
        {"qa": 5},
        end=20,
    )
    combinational = Workload(
        "combinational",
        header + "output wire [7:0] qa,qb); wire [7:0] ma,mb;\n"
        "assign ma=gate?a:8'h00; assign mb=gate?b:8'h00;\n"
        "assign qa=gate?ma:8'hff; assign qb=gate?mb:8'hff; endmodule\n",
        {"gate": 1, "a": 8, "b": 8, "ma": 8, "mb": 8, "qa": 8, "qb": 8},
        [
            (0, dict(gate=1, a=0x55, b=0x55, ma=0x55, mb=0x55, qa=0x55, qb=0x55)),
            (3, dict(b=0x6A, mb=0x6A, qb=0x6A)),
        ],
        3,
        [("qa", "qb"), ("ma", "mb"), ("a", "b")],
        {"a": (3, "after", 0x55), "b": (3, "after", 0x6A), "gate": (3, "after", 1)},
        ("gate",),
        edge_count=2,
    )
    pipeline = Workload(
        "pipeline",
        header + "output logic [7:0] qa,qb); logic [7:0] ma,mb;\n"
        "always_ff @(posedge clk) begin if(en) begin ma<=a; mb<=b; end\n"
        "qa<=ma; qb<=mb; end endmodule\n",
        {"clk": 1, "en": 1, "a": 8, "b": 8, "ma": 8, "mb": 8, "qa": 8, "qb": 8},
        [
            (0, dict(clk=0, en=1, a=0x55, b=0x55, ma=0, mb=0, qa=0, qb=0)),
            (3, dict(b=0x6A)),
            (5, dict(clk=1, ma=0x55, mb=0x6A)),
            (8, dict(en=0)),
            (10, dict(clk=0)),
            (15, dict(clk=1, qa=0x55, qb=0x6A)),
            (20, dict(clk=0)),
            (25, dict(clk=1)),
        ],
        15,
        [("qa", "qb"), ("ma", "mb"), ("a", "b")],
        {
            "a": (5, "before", 0x55),
            "b": (5, "before", 0x6A),
            "ma": (15, "before", 0x55),
            "mb": (15, "before", 0x6A),
            "en": (5, "before", 1),
        },
        ("clk", "en"),
        {"qa": 15, "ma": 5},
        edge_count=2,
    )
    alternatives = ",".join(f"n{i}" for i in range(16))
    select_inputs = ",".join(f"s{i}" for i in range(15))
    tail = "n15"
    for i in reversed(range(15)):
        tail = f"(s{i}?n{i}:{tail})"
    fanin = Workload(
        "irrelevant_fanin",
        header + f"input logic {select_inputs}, input logic [7:0] {alternatives},"
        "output wire [7:0] qa,qb);\n"
        f"assign qa=en?a:{tail}; assign qb=en?b:{tail}; endmodule\n",
        {
            "en": 1,
            "a": 8,
            "b": 8,
            "qa": 8,
            "qb": 8,
            **{f"s{i}": 1 for i in range(15)},
            **{f"n{i}": 8 for i in range(16)},
        },
        [
            (
                0,
                dict(
                    en=1,
                    a=0x55,
                    b=0x55,
                    qa=0x55,
                    qb=0x55,
                    **{f"s{i}": "x" for i in range(15)},
                    **{f"n{i}": "xxxxxxxx" for i in range(16)},
                ),
            ),
            (3, dict(b=0x6A, qb=0x6A)),
        ],
        3,
        [("qa", "qb"), ("a", "b")],
        {"a": (3, "after", 0x55), "b": (3, "after", 0x6A), "en": (3, "after", 1)},
        ("en",),
        edge_count=1,
        allow_npi_fallback=True,
    )
    shared = Workload(
        "shared_upstream",
        header + "output wire [15:0] qa,qb); wire [7:0] ma,mb,na,nb;\n"
        "assign ma=gate?a:8'h00; assign mb=gate?b:8'h00;\n"
        "assign na=gate?a:8'hff; assign nb=gate?b:8'hff;\n"
        "assign qa={ma,na}; assign qb={mb,nb}; endmodule\n",
        {
            "gate": 1,
            "a": 8,
            "b": 8,
            "ma": 8,
            "mb": 8,
            "na": 8,
            "nb": 8,
            "qa": 16,
            "qb": 16,
        },
        [
            (
                0,
                dict(
                    gate=1,
                    a=0x55,
                    b=0x55,
                    ma=0x55,
                    mb=0x55,
                    na=0x55,
                    nb=0x55,
                    qa=0x5555,
                    qb=0x5555,
                ),
            ),
            (3, dict(b=0x6A, mb=0x6A, nb=0x6A, qb=0x6A6A)),
        ],
        3,
        [("qa", "qb"), ("ma", "mb"), ("na", "nb"), ("a", "b")],
        {"a": (3, "after", 0x55), "b": (3, "after", 0x6A), "gate": (3, "after", 1)},
        ("gate",),
        edge_count=4,
    )
    missing = Workload(
        "missing_dump",
        register.rtl,
        register.widths,
        register.events,
        5,
        [("qa", "qb")],
        {"a": (5, "before", 0x55), "en": (5, "before", 1)},
        ("clk", "en"),
        {"qa": 5},
        ("b",),
        end=20,
    )
    # Even unavailable waveform dependencies retain explicit caller mappings.
    return {
        case.name: case
        for case in (register, combinational, pipeline, fanin, shared, missing)
    }
