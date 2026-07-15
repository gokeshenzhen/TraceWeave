"""
waveform_batch.py
Batch waveform-read protocol shared by FSDB and VCD backends.

The protocol returns *value-change transitions* for a set of signals
over a time window in chronological order. Callers needing a value at
a specific time should use the existing ``get_value_at_time`` /
``get_signal_at_time`` paths — this protocol is a perf-oriented
alternative to "iterate N signals × M time points".

Both implementations return data in identical shape so that test
fixtures can assert byte-equal output across backends.
"""

from __future__ import annotations

import ctypes
from bisect import bisect_left
from typing import Any, Protocol, runtime_checkable

from .fsdb_parser import FSDBParser, _BUF_SIZE
from .vcd_parser import VCDParser


@runtime_checkable
class WaveformBatchReader(Protocol):
    """Single-pass batched value-change reader."""

    def values_in_window(
        self,
        signals: list[str],
        start_ps: int,
        end_ps: int,
    ) -> dict[str, Any]:
        """Return per-signal transitions inside ``[start_ps, end_ps]``.

        Result shape:
            {
              "start_ps": int,
              "end_ps":   int,
              "signals":  list[str]            # echoed
              "transitions": list[             # chronological
                  {"time_ps": int, "signal": str, "value": str}
              ],
              "missing":     list[str],        # signals not in design
              "truncated":   bool,
            }
        """
        ...


# ---------------------------------------------------------------------------
# FSDB implementation
# ---------------------------------------------------------------------------


class FSDBBatchReader:
    """FSDB batch reader using the time-based VC traverse handle."""

    def __init__(self, parser: FSDBParser):
        self._parser = parser

    def values_in_window(
        self,
        signals: list[str],
        start_ps: int,
        end_ps: int,
    ) -> dict[str, Any]:
        self._parser._open()
        if not signals:
            return _empty_result(signals, start_ps, end_ps)

        buf = self._parser._get_buf()
        encoded = [s.encode() for s in signals]
        c_paths = (ctypes.c_char_p * len(encoded))(*encoded)
        rc = self._parser._lib.fsdb_batch_window_transitions(
            self._parser._handle,
            c_paths,
            len(encoded),
            ctypes.c_uint64(start_ps),
            ctypes.c_uint64(end_ps),
            buf,
            _BUF_SIZE,
        )
        if rc == -4:
            raise self._parser._scale_unknown_error()
        if rc < 0:
            raise RuntimeError(f"fsdb_batch_window_transitions failed, rc={rc}")
        text = buf.value.decode()
        return _parse_batch_buf(text, signals, start_ps, end_ps, truncated=(rc == 0 and len(text) >= _BUF_SIZE - 1))


def _parse_batch_buf(
    text: str,
    requested: list[str],
    start_ps: int,
    end_ps: int,
    truncated: bool,
) -> dict[str, Any]:
    transitions: list[dict[str, Any]] = []
    missing: list[str] = []
    for raw in text.splitlines():
        if not raw:
            continue
        if raw.startswith("@SIGNAL\t"):
            continue
        if raw.startswith("@ERROR\t"):
            parts = raw.split("\t")
            if len(parts) >= 3 and parts[2] == "signal_not_found":
                missing.append(parts[1])
            continue
        parts = raw.split("\t")
        if len(parts) < 3:
            continue
        try:
            t_ps = int(parts[0])
        except ValueError:
            continue
        transitions.append({
            "time_ps": t_ps,
            "signal":  parts[1],
            "value":   parts[2],
        })
    return {
        "start_ps":   start_ps,
        "end_ps":     end_ps,
        "signals":    list(requested),
        "transitions": transitions,
        "missing":    missing,
        "truncated":  truncated,
    }


# ---------------------------------------------------------------------------
# VCD implementation (pure Python — no Verdi dependency)
# ---------------------------------------------------------------------------


class VCDBatchReader:
    """Pure-Python batch reader matching FSDBBatchReader's surface."""

    def __init__(self, parser: VCDParser):
        self._parser = parser

    def values_in_window(
        self,
        signals: list[str],
        start_ps: int,
        end_ps: int,
    ) -> dict[str, Any]:
        self._parser._ensure_parsed()  # type: ignore[attr-defined]
        if not signals:
            return _empty_result(signals, start_ps, end_ps)

        path_to_sym = self._parser._path_to_sym
        sym_transitions = self._parser._transitions

        merged: list[tuple[int, str, str]] = []
        missing: list[str] = []
        for path in signals:
            sym = path_to_sym.get(path)
            if sym is None:
                missing.append(path)
                continue
            tlist = sym_transitions.get(sym, [])
            if not tlist:
                continue
            times = [t for t, _ in tlist]
            lo = bisect_left(times, start_ps)
            for idx in range(lo, len(tlist)):
                t_ps, val = tlist[idx]
                if t_ps > end_ps:
                    break
                merged.append((t_ps, path, val))

        # Stable chronological sort to match FSDB's time-based walker
        # behaviour (FSDB's order is by physical position in the file
        # which for normal dumps is chronological).
        merged.sort(key=lambda item: (item[0], signals.index(item[1])))

        return {
            "start_ps":   start_ps,
            "end_ps":     end_ps,
            "signals":    list(signals),
            "transitions": [
                {"time_ps": t, "signal": s, "value": v} for (t, s, v) in merged
            ],
            "missing":    missing,
            "truncated":  False,
        }


def _empty_result(signals: list[str], start_ps: int, end_ps: int) -> dict[str, Any]:
    return {
        "start_ps":    start_ps,
        "end_ps":      end_ps,
        "signals":     list(signals),
        "transitions": [],
        "missing":     [],
        "truncated":   False,
    }


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def make_batch_reader(wave_path: str) -> WaveformBatchReader:
    """Return the right batch reader for a given waveform path."""
    lower = wave_path.lower()
    if lower.endswith(".fsdb"):
        return FSDBBatchReader(FSDBParser(wave_path))
    if lower.endswith(".vcd"):
        return VCDBatchReader(VCDParser(wave_path))
    raise ValueError(f"Unsupported waveform extension: {wave_path}")
