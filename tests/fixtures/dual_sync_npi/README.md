# Dual synchronous native fixture

Opt-in only: `bash run.sh NEW_ABSOLUTE_OUTPUT_DIRECTORY` with the existing VCS
and Verdi environment. Each side gets independent source, compile log, KDB,
simulation log and FSDB. Never overwrite an accepted fixture.

The two-register pipeline has no reset or initial assignments in the DUT.
The clock rises at 5, 15, 25, 35 and 45 ns. Data changes at 12 ns from zero to
A=0x3c / B=0xc3. At 15 ns q0 captures data and q1 captures the previous zero;
at 25 ns q1 captures q0. Result is q1 XOR mask; mask is 0x0f until 40 ns.
Thus result is equal 0x0f at 16 ns and different 0x33/0xcc at 26 ns.
Tracing q1 at 26 ns samples q0 before 25 ns; tracing q0 before 25 ns must
exclude that edge and sample data before 15 ns. Dump ends at 46 ns.

This is a bounded synchronous semantic oracle, not a unique system root cause
or a test of asynchronous reset, delta-cycle order or license release.
