"""Read the recorded FIFO pause regression with the open-source TraceWeave waveform tools."""

import argparse
from pathlib import Path
import re
import sys

CASE = Path(__file__).resolve().parent
sys.path.insert(0, str(CASE.parents[1]))
from src.cycle_query import get_signals_by_cycle
from src.vcd_parser import VCDParser
from src.verify_condition import inspect_handshake

HOLD_FAILURE = "valid_dropped_before_handshake"
PORTS = ("rst", "pause_req", "pause_ack", "s_axis_tvalid", "s_axis_tready", "s_axis_tdata",
         "s_axis_tlast", "m_axis_tvalid", "m_axis_tready", "m_axis_tdata", "m_axis_tlast")
BEATS = [(0xa1, 0), (0xa2, 1), (0xb1, 1)]


def transfers(rows, interface):
    return [r for r in rows if r[f"{interface}_tvalid"] == 1 and r[f"{interface}_tready"] == 1]


def inspect_run(directory):
    wave = str(directory / "wave.vcd")
    markers = re.findall(r"^AXIS_FIFO_PAUSE (PASS|FAIL) (\S+) time_ps=(\d+)$",
                         (directory / "sim.log").read_text(errors="replace"), re.MULTILINE)
    if len(markers) != 1:
        raise ValueError(f"Expected exactly one checker verdict in {directory}/sim.log")
    status, detail, time_ps = markers[0]

    reader = VCDParser(wave)
    scope = "TOP.tb" if "TOP" in reader.get_summary()["top_modules"] else "tb"
    clock = f"{scope}.clk"
    paths = [f"{scope}.{name}" for name in PORTS]
    sampled = get_signals_by_cycle(reader, clock, paths, num_cycles=32, sample_phase="before")
    if (sampled["signal_errors"] or sampled["transition_data_truncated"]
            or sampled["num_cycles_returned"] != sampled["total_edges_found"]):
        raise ValueError(f"Incomplete cycle samples in {wave}")
    rows = []
    for cycle in sampled["cycles"]:
        ports = {port: cycle["signals"][path].get("dec") for port, path in zip(PORTS, paths)}
        if ports["rst"] == 0:
            rows.append({"time_ps": cycle["time_ps"], **ports})
    if not rows:
        raise ValueError(f"No rising edges after reset in {wave}")

    handshake = inspect_handshake(
        get_parser=lambda _: reader, wave_path=wave, clock=clock,
        valid=f"{scope}.m_axis_tvalid", ready=f"{scope}.m_axis_tready",
        payload=[f"{scope}.m_axis_tdata", f"{scope}.m_axis_tlast"], start_ps=rows[0]["time_ps"],
    )
    if (handshake["reason"] or handshake["signal_errors"] or handshake["unknown_sample_cycles"]
            or handshake["transition_data_truncated"]
            or handshake["coverage"]["sampling_gaps"] or not handshake["payload_hold_checked"]):
        raise ValueError(f"Incomplete handshake coverage in {wave}")

    print(f"\n{directory.name}: {status} {detail} at {time_ps} ps")
    print("Rising edge (ps)   pause_req  pause_ack   TVALID  TREADY  TDATA  TLAST")
    previous = None
    for r in rows:
        state = (r["pause_req"], r["pause_ack"], r["m_axis_tvalid"], r["m_axis_tready"],
                 r["m_axis_tdata"], r["m_axis_tlast"])
        if state != previous or (r["m_axis_tvalid"] and r["m_axis_tready"]):
            fields = ["?" if v is None else str(v) for v in state]
            if state[4] is not None:
                fields[4] = f"0x{state[4]:02x}"
            print(f"{r['time_ps']:>16}   {fields[0]:>9}  {fields[1]:>9}   "
                  f"{fields[2]:>6}  {fields[3]:>6}  {fields[4]:>5}  {fields[5]:>5}")
        previous = state

    input_beats = [(r["s_axis_tdata"], r["s_axis_tlast"]) for r in transfers(rows, "s_axis")]
    outgoing = transfers(rows, "m_axis")
    if status == "FAIL" and detail == HOLD_FAILURE:
        witness = next((f for f in handshake["findings"]
                        if f["type"] == "premature_valid_deassertion"
                        and f["time_ps"] == int(time_ps) and f["accepted_before_deassert"] is False), None)
        if not witness or input_beats != BEATS or outgoing:
            raise ValueError(f"Waveform does not confirm the checker failure in {wave}")
        print(f"inspect_handshake: {witness['type']} at {witness['time_ps']} ps, "
              f"stall began at {witness['stall_begin_ps']} ps, accepted_before_deassert=false")
    if status == "PASS":
        output_beats = [(r["m_axis_tdata"], r["m_axis_tlast"]) for r in outgoing]
        if (detail != "beats=3" or input_beats != BEATS or output_beats != BEATS
                or handshake["valid_deassert_violations"] or handshake["payload_hold_violations"]
                or not handshake["stall_count"]):
            raise ValueError(f"Waveform does not confirm the checker pass in {wave}")
        _, frame_end, resumed = outgoing
        paused = any(frame_end["time_ps"] < r["time_ps"] < resumed["time_ps"]
                     and (r["pause_req"], r["pause_ack"], r["m_axis_tvalid"], r["m_axis_tready"]) == (1, 1, 0, 1)
                     for r in rows)
        if not paused or frame_end["pause_req"] != 1 or (resumed["pause_req"], resumed["pause_ack"]) != (0, 0):
            raise ValueError(f"Expected a frame boundary, an acknowledged pause, and a resumed transfer in {wave}")
        print("Accepted 0xa1, 0xa2 (TLAST), then 0xb1 (TLAST) after pause and resume.")
    return detail if status == "FAIL" else status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", nargs="?", type=Path, default=CASE / "recorded",
                        help="directory containing before/ and repaired/, each with wave.vcd and sim.log")
    args = parser.parse_args()
    if inspect_run(args.directory / "before") != HOLD_FAILURE:
        raise ValueError("The baseline did not reproduce the expected failure")
    if inspect_run(args.directory / "repaired") != "PASS":
        raise ValueError("The repaired run failed the regression")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        sys.exit(str(error))
