# Real dual-design combinational trace fixture

Explicitly build and simulate with the existing VCS/Verdi installation:

```bash
bash tests/fixtures/dual_trace_npi/run.sh /absolute/new/output/directory
```

The output directory must not exist; its parent must exist. Each side gets a
source copy, independent compile log, KDB, executable, simulation log and real
FSDB. Sources use persistent output paths so replay does not depend on the
build checkout. This script never runs automatically in pytest.

The tops are `p4_s2_a_tb` and `p4_s2_b_tb`. Query each `result[7:0]` with an
explicit mapping between the tops. Both receive identical `data` and `mode`;
their combinational expressions XOR with different masks when mode is high.
The initial blocks only supply stimulus, not the queried dynamic writer.

| Time (ps) | data | mode | A result | B result |
|---|---|---|---|---|
| 5000 | a6 | 0 | a6 | a6 |
| 10000 | 3c | 0 | 3c | 3c |
| 20000 | 3c | 1 | 33 | cc |
| 30000 | a5 | 1 | aa | 55 |
| 40000 | a5 | 0 | a5 | a5 |

For event comparison on [5000, 35000] ps, the first difference is 20000 ps.
Both sides sample after that event, with no clock or sequential history. Equal
mapped inputs plus different expressions support a local-logic candidate;
they do not establish a unique root cause or the correctness of either mask.
Keep source-association, coverage and provenance labels as reported. This
fixture does not validate asynchronous/reset semantics, LSF or license release.
