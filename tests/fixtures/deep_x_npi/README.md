# Deep-hierarchy X propagation: Static vs. Verdi NPI

This fixture is patterned after the APB UART verification project at
`/home/robin/Projects/vcs-verification-of-apb-based-uart-master-core`.
It keeps the characteristic hierarchy:

```text
uart_deep_x_tb
└── u_apb_bridge
    └── u_uart
        └── u_control
            └── u_rx_channel
                └── u_rx_fifo
                    └── u_storage_bank
                        └── u_x_cell
```

All seven module boundaries use positional port bindings. The leaf register
is reset to a known value, becomes `8'hxx` for one clock in the middle of the
run, and then recovers. The observed signal is:

```text
uart_deep_x_tb.apb_prdata
```

Run:

```bash
./run.sh
```

The script uses VCS `-kdb` rather than `-kdb=only`: a fresh fixture needs both
the runnable `simv` and the Verdi KDB, while `-kdb=only` creates only the latter.
With `VERDI_HOME` set, VCS `-debug_access+all` loads the Verdi dumper used to
emit the FSDB. Each run invalidates VCS's generated incremental timestamp so
`compile.log` always retains the source-file parsing records TraceWeave needs.

Artifacts are written to the ignored `work/` directory:

- `compile.log`
- `sim.log`
- `deep_x.fsdb`
- `simv.daidir/kdb.elab++`

The intended comparison is:

1. Legacy Static cannot infer that the top-level `apb_prdata` is driven by a
   positional child output, so it stops at the top module.
2. FSDB supplies the values and X timing, while Verdi NPI independently uses
   the elaborated KDB and `fan_in_reg_list()` to cross every positional
   boundary and identify `u_x_cell`'s sequential driver.
3. `trace_x_source` must return one backend-consistent chain. If NPI internally
   falls back at any node, TraceWeave discards that partial chain and reruns
   the whole trace with Static.

## Observed comparison

Measured with VCS/Verdi V-2023.12-SP2:

- FSDB: `apb_prdata[7:0]` and leaf `data_q[7:0]` become `xxxxxxxx` at
  65,000 ps and recover to `8'h3c` at 75,000 ps.
- Static: `driver_status=partial`, `driver_kind=unknown`, `source_line=null`;
  X tracing stops at depth 0 with `driver_unresolved`.
- Verdi NPI: `actual_backend=verdi_npi`, `confidence=exact`; fan-in crosses
  the positional hierarchy and resolves the `always_ff` driver in
  `rtl/deep_uart_x.sv:20`.
