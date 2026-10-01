"""Bounded, evidence-based Verilator runtime formats (not arbitrary host errors).

Native bracket timestamps have no universal unit. Only a caller-verified
native_time_unit resolves them. Explicit timestamps always keep their own unit.
Software UART diagnostics remain untimed; an exit observer is a separate event.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from pathlib import PurePath
from typing import Any, Callable

from config import MAX_UVM_CONTINUATION_LINES
from .cancellation import check_cancelled

UNITS = {"fs": -3, "ps": 0, "ns": 3, "us": 6, "ms": 9, "s": 12}
_NUM = r"\d+(?:\.\d+)?"
_UNIT = r"fs|ps|ns|us|ms|s"
_LOGGER = re.compile(r"^(?:ERROR|WARNING|INFO|DEBUG|CRITICAL):cocotb:\s?")
_HOST = re.compile(r"^\[(\d{4}-\d\d-\d\d[T ][^\]]+)\]\s*")
_NATIVE = re.compile(rf"^\s*(?:\[({_NUM})\s*({_UNIT})?\]\s*)?%Error:\s+(.+?):(\d+):\s+(.*)$", re.I)
_ASSERT = re.compile(r"^Assertion failed in ([\w.$\[\]-]+):\s*(.*)$")
_EXPLICIT = re.compile(rf"(?:\btime\s*=\s*|@\s*|\[)({_NUM})\s*({_UNIT})\b", re.I)
_BARE = re.compile(rf"(?:\btime\s*=\s*|@\s*)({_NUM})(?![\w.])", re.I)
_OBSERVER = re.compile(rf"^\[TRACEWEAVE_(XHEEP|OPENTITAN)\]\s+time=({_NUM})\s+({_UNIT})\s+event=(\w+)(?:\s+value=(\d+))?\s*$", re.I)
_EXIT = re.compile(r"^Program Finished with value (\d+)\s*$")
_DMA = re.compile(r"^\s*\[(\d+)\]\s+Expected:\s*([0-9a-f]+)\s+Got\s*:\s*([0-9a-f]+)\s*$", re.I)
_OT = re.compile(r"^[IE]\d+\s+([^\s:]+):(\d+)\]\s+(.*)$")
_OT_ROW = re.compile(r"CHECK-fail:\s*\[(\d+)\]\s+got:\s*(0x[0-9a-f]+);\s+want:\s*(0x[0-9a-f]+)", re.I)
_COCOTB = re.compile(rf"^\s*({_NUM})\s*({_UNIT})\s+(?:INFO|ERROR|WARNING|CRITICAL)\s+cocotb\.regression\s+(\S+)\s+failed(?:\s|$)", re.I)
_PY_SOURCE = re.compile(r'^\s*File "([^"]+)", line (\d+),')
_SOURCE_ECHO = re.compile(r'^\s*(?:\d+\s*\|\s*|\$(?:error|fatal|display|write)\b|(?:puts|echo|printf|print)\b)')


def time_fields(raw: str | None = None, unit: str | None = None,
                native_time_unit: str | None = None) -> dict[str, Any]:
    """Ceil exact decimal physical time to public integer ps, without floats."""
    result = {"raw_time": raw, "raw_time_unit": unit, "time_ps": None,
              "time_parse_status": "missing" if raw is None else "unresolved"}
    if raw is None or len(raw) > 128:
        return result
    effective = unit.lower() if unit else native_time_unit
    if effective not in UNITS:
        return result
    try:
        number = Decimal(raw)
    except InvalidOperation:
        return result
    if not number.is_finite() or number < 0:
        return result
    sign, digits, exponent = number.as_tuple()
    coefficient = int(''.join(str(d) for d in digits))
    power = exponent + UNITS[effective]
    result.update(raw_time_unit=effective,
                  time_ps=coefficient * 10**power if power >= 0 else
                  (coefficient + 10**(-power) - 1) // 10**(-power),
                  time_parse_status="exact" if unit else "inferred")
    return result


def extract_time(line: str, native_time_unit: str | None = None) -> dict[str, Any]:
    explicit = _EXPLICIT.search(line)
    if explicit:
        return time_fields(explicit[1], explicit[2].lower())
    bare = _BARE.search(line)
    return time_fields(bare[1], native_time_unit=native_time_unit) if bare else time_fields()


def unwrap(line: str) -> tuple[str, str | None]:
    line = _LOGGER.sub('', line.rstrip('\r\n'))
    host = _HOST.match(line)
    return (line[host.end():], host[1]) if host else (line, None)


def parse_errors(lines: list[str], native_time_unit: str | None,
                 multiline: bool, fallback: Callable) -> dict[int, Any]:
    # Import at call time to preserve the existing ParsedError/event contracts.
    from .log_parser import ParsedError
    errors = {}
    observed_exits: set[int] = set()
    observed_timeout = False
    dma_rows_seen = False
    previous_native = None
    limit = MAX_UVM_CONTINUATION_LINES if multiline else 0
    for i, raw in enumerate(lines):
        check_cancelled()
        line, host = unwrap(raw)
        line = line.lstrip('\r')
        error = None
        fields: dict[str, Any] = {}
        common = {"line_num": i + 1, "severity": "ERROR", "time_ps": None,
                  "message": line.strip()}
        native = _NATIVE.match(line)
        if native:
            assertion = _ASSERT.match(native[5])
            stop = native[5].strip() == 'Verilog $stop'
            # A plain compiler %Error (syntax, options, missing input) is not runtime.
            if assertion or stop or re.match(r'^Verilog \$(?:error|fatal)\b', native[5]):
                location = (PurePath(native[3]).name, int(native[4]))
                if stop and previous_native == location:
                    continue  # sole paired termination footer; no broad same-time dedup
                scope = assertion[1] if assertion else None
                message = assertion[2] if assertion else native[5]
                signature = f"ASSERTION_FAIL: {scope}" if scope else f"VERILATOR_RUNTIME: {native[5].split(':')[0]}"
                common.update(group_signature=signature, source_file=native[3],
                              source_line=int(native[4]), instance_path=scope,
                              message=message, **time_fields(native[1], native[2], native_time_unit))
                fields = {"parser_format": "verilator_native", "detection_location": True}
                if native[1] and not native[2] and native_time_unit:
                    fields['native_time_unit_source'] = 'caller_verified'
                error = ParsedError(**common)
                previous_native = location
            else:
                previous_native = None
        elif (observer := _OBSERVER.match(line)):
            profile, stamp, unit, event, value = observer.groups()
            if profile.upper() == 'XHEEP' and event == 'software_exit':
                observed_exits.add(int(value or 0))
                if int(value or 0):
                    common.update(group_signature='XHEEP: software_exit',
                                  **time_fields(stamp, unit.lower()))
                    fields = {"software_exit_value": int(value), "time_anchor_kind": "software_exit",
                              "parser_format": "traceweave_xheep_observer"}
                    error = ParsedError(**common)
            elif profile.upper() == 'XHEEP' and event == 'timeout':
                observed_timeout = True
                common.update(group_signature='XHEEP: timeout', **time_fields(stamp, unit.lower()))
                fields = {"time_anchor_kind": "termination", "parser_format": "traceweave_xheep_observer"}
                error = ParsedError(**common)
            # OpenTitan finish/stop observers alone are not failure oracles.
        elif (exit_match := _EXIT.match(line)):
            value = int(exit_match[1])
            if value and value not in observed_exits:
                common['group_signature'] = 'XHEEP: software_exit'
                fields = {"software_exit_value": value, "parser_format": "xheep"}
                error = ParsedError(**common)
        elif (dma := _DMA.match(line)):
            if int(dma[2], 16) != int(dma[3], 16):
                dma_rows_seen = True
                common['group_signature'] = 'XHEEP: DMA comparison'
                fields = {"expected": '0x' + dma[2], "actual": '0x' + dma[3],
                          "element_index": int(dma[1]), "checker": "example_dma",
                          "parser_format": "xheep"}
                error = ParsedError(**common)
        elif (aggregate := re.match(r'^DMA failure: (\d+) errors out of (\d+) elements checked\s*$', line.strip())):
            if int(aggregate[1]) and not dma_rows_seen:
                common['group_signature'] = 'XHEEP: DMA comparison'
                fields = {"comparison_errors": int(aggregate[1]), "elements_checked": int(aggregate[2]),
                          "checker": "example_dma", "parser_format": "xheep"}
                error = ParsedError(**common)
        elif line.strip() == 'Simulation was terminated before program finished' and not observed_timeout:
            common['group_signature'] = 'XHEEP: timeout'
            fields = {"time_anchor_kind": "termination", "parser_format": "xheep"}
            error = ParsedError(**common)
        elif (ot := _OT.match(line)):
            row = _OT_ROW.search(ot[3])
            if row and int(row[2], 16) != int(row[3], 16):
                common.update(group_signature=f'OPENTITAN: CHECK_ARRAYS_EQ {ot[1]}:{ot[2]}',
                              source_file=ot[1], source_line=int(ot[2]))
                fields = {"expected": row[3], "actual": row[2], "element_index": int(row[1]),
                          "parser_format": "opentitan_software", "detection_location": True}
                error = ParsedError(**common)
            elif ot[3].strip() == 'FAIL!':
                common.update(group_signature='OPENTITAN: software_fail',
                              source_file=ot[1], source_line=int(ot[2]))
                fields = {"parser_format": "opentitan_software", "detection_location": True}
                error = ParsedError(**common)
        elif (coco := _COCOTB.match(line)):
            continuation = []
            source_file = source_line = None
            for following in lines[i + 1:i + 1 + limit]:
                check_cancelled()
                text, _ = unwrap(following)
                if not text.strip() or not text[:1].isspace() or re.match(r'^\s*\d+(?:\.\d+)?\s*(?:fs|ps|ns|us|ms|s)\s+', text):
                    break
                continuation.append(text)
                if (source := _PY_SOURCE.match(text)):
                    source_file, source_line = source[1], int(source[2])
            common.update(group_signature=f'COCOTB: {coco[3]}',
                          message='\n'.join([line.strip(), *continuation]),
                          source_file=source_file, source_line=source_line,
                          **time_fields(coco[1], coco[2].lower()))
            fields = {"test_name": coco[3], "parser_format": "cocotb_runtime",
                      "continuation_truncated": len(continuation) == limit and limit > 0}
            for text in continuation:
                if 'AssertionError:' in text:
                    from .log_parser import _extract_expected_actual
                    expected, actual, _ = _extract_expected_actual(text, {})
                    if expected is not None:
                        fields['expected'] = expected
                    if actual is not None:
                        fields['actual'] = actual
            error = ParsedError(**common)
        elif not _SOURCE_ECHO.match(line) and not line.lstrip().startswith(('%Warning', '%Error', '^')):
            error = fallback(line, i + 1)
        if error:
            error.structured_fields = {**(error.structured_fields or {}), **fields}
            if host:
                error.structured_fields['host_timestamp'] = host
            errors[i] = error
        # A footer is paired only with the immediately preceding runtime record.
        if not native and line.strip() and line.strip() != 'Aborting...':
            previous_native = None
    return errors
