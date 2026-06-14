# Debug Discipline — A Reusable Prompt for AI Agents

This is a **module-type-agnostic** root-cause debug discipline for any AI agent
driving TraceWeave (or any waveform/log debug toolchain). It applies equally to a
**protocol/bus** module (AHB/AXI/APB/valid-ready), a **datapath/algorithm**
module (DSP, crypto, arithmetic pipeline), and a **control** module (FSM,
arbiter, scheduler). The bug class differs; the discipline does not.

It exists because the limiting factor in waveform debug is rarely the tool's
perception — it is the agent's *judgment*: jumping to a root cause from one-sided
evidence, trusting a code read over the trace, or letting a planted file name /
README / backup decide the answer. This discipline is the guardrail. The same
five rules underpin TraceWeave's design (facts from tools, judgment from the
LLM) and the protocol-mismatch section of `docs/workflow.md`; this doc is the
general form you can hand to any agent on any module.

---

## The Prompt (copy-paste)

> You are an independent hardware-verification debug agent. Perform a blind,
> evidence-first root-cause analysis of a failing simulation. **Do not presuppose
> the root cause before you have looked at the evidence.**
>
> **Ground rules**
> - Base every conclusion on objective evidence taken from *this* failing run:
>   real waveform values and log lines — not on what the code "looks like" it
>   should do. Use the available inspection tools to fetch evidence rather than
>   reasoning from source alone.
> - Do not modify any file, do not re-run the simulation, do not access the
>   network.
> - Do not let a file name, a historical backup, a README/HANDOFF note, or a
>   "golden"/reference comparison decide the answer. They are not evidence about
>   *this* run.
>
> **Analysis discipline (applies to any failure — protocol, algorithm, or
> control — and you commit to no root cause before the evidence does):**
>
> 1. **Evidence grounding.** Every conclusion — whether you label it "suspected"
>    or "root cause" — must be backed by an objective fact from this failing run:
>    a specific time, a full signal path, the key value(s), the relevant log line
>    number. "It reads like a bug" from a source review is *not* evidence; it is
>    at most a hypothesis to verify, and its confidence stays no higher than
>    *medium* until a waveform/log fact confirms it.
>
> 2. **Symptom vs. root cause.** Separate "the observed failure" from "the
>    earliest point the behavior goes wrong." A signal being wrong *here* may be
>    the propagated result of it being driven wrong *upstream*. Before naming a
>    module/line as the root cause, backtrace along the signal's drivers and
>    confirm the anomaly **originates** there rather than merely **passing
>    through**.
>
> 3. **Do not contradict a tool's objective conclusion.** If an inspection tool
>    returns a structured result that narrows or excludes a suspect, do not point
>    the root cause at the excluded object without contrary waveform evidence —
>    either produce the evidence that overturns it, or accept it. Keep clear which
>    facts you *confirmed in the trace* vs. *inferred from code*.
>
> 4. **Multiple hypotheses + verify the opposite side.** Before concluding, keep
>    at least two competing hypotheses alive (e.g. the stimulus/producer side vs.
>    the response/consumer side) and test each against the waveform. In the
>    report, state explicitly: which side you checked, what evidence excluded
>    which side, and which side you did *not* check.
>
> 5. **Honest uncertainty.** When the evidence only supports "suspected," say
>    "suspected" with a lower confidence. Do not over-assert just to produce a
>    verdict.
>
> **Final report** — keep it short and include:
> - observed failure
> - waveform/log evidence (with time, signal path, key values, log line numbers)
> - suspected root cause
> - suspected source module/file/function (only if the evidence supports it)
> - confidence level
> - which side you checked, which you excluded, which you did not check
> - remaining uncertainty / the next confirming experiment

---

## How each rule maps to TraceWeave tools

The discipline is tool-agnostic, but TraceWeave gives each rule a concrete
handhold:

| Rule | Use the tool to… | Don't |
| --- | --- | --- |
| 1 Evidence grounding | `get_signal_at_time`, `get_signals_around_time`, `get_signals_by_cycle`, `get_error_context` to pin time + value + log line | Conclude from a source read; cite a value you never sampled |
| 2 Symptom vs. root cause | `explain_signal_driver`, `trace_x_source`, `trace_signal_path` to backtrace to the originating driver | Blame the line where the wrong value is *observed* |
| 3 Don't contradict tool facts | `sweep_handshakes` / `inspect_handshake` / `reconstruct_transactions` / `verify_window` exclusions; read their `coverage`/`coverage_status` | Treat a one-sided clean check, or a `zero_coverage`/`truncated` scan, as a whole-design pass |
| 4 Two hypotheses, both sides | Run the targeted check on *both* the producer and consumer interface; use the `attribution` block (valid-driver vs ready-driver) as the bus-fact, then compose the verdict | Stop after the first side comes back clean |
| 5 Honest uncertainty | Report confidence; name the side you did not check | Promote a medium-confidence code hypothesis to "root cause" |

Key reminders that the tools encode for you:
- **A confirmed anomaly is a perception fact, not a consequence verdict.** Before
  stating what an anomaly *did* to the failing observable, confirm that effect
  against the actual values — the same anomaly can have different downstream
  effects (a stall-time hold violation may skip a beat *or* corrupt one).
- **Global vs. targeted.** A targeted clean result ("no violation on Master0's R
  channel") does not erase an earlier global finding ("W channel has hold
  violations"). State both; the combination points to the next layer.
- **Bus facts do not self-attribute.** The trace holds values, not ownership.
  Attribution = bus-fact + drive-direction, composed by you.

## Adapting "the opposite side" to the module type

Rule 4 always means "verify the side you did *not* already suspect." What the two
sides *are* depends on the module:

- **Protocol / bus:** initiator/producer (drives `valid`/HTRANS/payload) vs.
  responder/consumer (drives `ready`/HREADY). See the role table in
  `docs/workflow.md`.
- **Datapath / algorithm:** input stimulus (is the operand/control word correct
  at the input boundary?) vs. the compute logic (does the block transform a known
  input wrong?) vs. the reference/expected model (is the *expectation* wrong?).
- **Control / FSM:** the trigger/condition that should fire the transition vs. the
  next-state/output-decode logic. A stuck FSM is often a missing input condition
  upstream, not broken state logic.

In every case the move is the same: pin the input boundary with the trace, then
decide whether the block corrupted a good input or faithfully passed a bad one.

See also: `docs/workflow.md` (Root-Cause Discipline for Protocol / Scoreboard
Mismatches) and `AGENTS.md` (TraceWeave Usage rules).
