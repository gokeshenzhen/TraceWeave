"""
vcd_parser.py
Pure-Python VCD parser with no external dependencies.
The public API matches FSDBParser.
"""

import re
from bisect import bisect_left, bisect_right
from operator import itemgetter
from itertools import islice
from pathlib import Path
from .cancellation import check_cancelled
from .scope_metadata import (PAGE_ITEMS, PAGE_BYTES, PAGE_SCAN, ScopeCursor,
                             ScopeIdentityChanged, file_identity, normalize_scope, page_limits)

from src.waveform_hints import annotate_signal_search_result, normalize_vcd_producer
from src.clock_edge_cache import ClockCacheToken

_transition_time = itemgetter(0)


class VCDParser:
    def __init__(self, file_path: str):
        self.file_path = file_path
        self._clock_cache_token = ClockCacheToken()
        self._parsed          = False
        # fs per VCD time unit. Integer fs (not integer ps) so a sub-ps
        # $timescale like 100fs stays exact instead of truncating to 0 and
        # collapsing every timestamp to t=0.
        self._timescale_fs    = 1000        # default 1ps when no $timescale
        self._timescale_raw   = None        # raw $timescale text, e.g. "100fs"
        self._signals: dict   = {}          # symbol → {path, width}
        self._path_to_sym: dict = {}        # full_path → symbol
        self._range_aliases: set[str] = set()  # exact ranges hidden from search when a base alias exists
        self._transitions: dict = {}        # symbol → [(time_ps, value)]
        self._end_time_ps     = 0
        self._top_modules: list = []
        self._producer_hint: str | None = None
        self._producer_evidence: str | None = None
        self._scope_ends: dict[str, tuple[int, ...]] = {}
        self._scope_index = None
        self._scope_epoch = object()
        self._parsed_identity = None

    # ── Public API ────────────────────────────────────────────────

    def get_value_at_time(self, signal_path: str, time_ps: int) -> dict:
        self._ensure_parsed()
        sym   = self._resolve(signal_path)
        trans = self._transitions.get(sym, [])
        value = _value_at(trans, time_ps)
        return {
            "signal":  signal_path,
            "time_ps": time_ps,
            "time_ns": time_ps / 1000,
            "value":   _enrich_value(value),
        }

    def get_transitions(self, signal_path: str,
                        start_ps: int = 0, end_ps: int = -1) -> dict:
        self._ensure_parsed()
        sym   = self._resolve(signal_path)
        trans = self._transitions.get(sym, [])
        if end_ps == -1:
            end_ps = self._end_time_ps
        lo = bisect_left(trans, start_ps, key=_transition_time)
        hi = bisect_right(trans, end_ps, key=_transition_time)
        filtered = trans[lo:hi]
        predecessor = trans[lo - 1] if lo > 0 else None
        return {
            "signal":           signal_path,
            "start_ps":         start_ps,
            "end_ps":           end_ps,
            "transition_count": len(filtered),
            "transitions": [{"time_ps": t, "time_ns": t / 1000, "value": _enrich_value(v)}
                            for t, v in filtered],
            "predecessor": (
                {
                    "time_ps": predecessor[0],
                    "time_ns": predecessor[0] / 1000,
                    "value": _enrich_value(predecessor[1]),
                }
                if predecessor is not None
                else None
            ),
        }

    def get_signals_around_time(self, signal_paths: list,
                                center_ps: int, window_ps: int = 500,
                                extra_transitions: int = 5) -> dict:
        self._ensure_parsed()
        start_ps = max(0, center_ps - window_ps)
        end_ps   = center_ps + window_ps
        result   = {}
        for path in signal_paths:
            try:
                sym   = self._resolve(path)
                trans = self._transitions.get(sym, [])
                lo = bisect_left(trans, start_ps, key=_transition_time)
                hi = bisect_right(trans, end_ps, key=_transition_time)
                filtered = trans[lo:hi]
                # extra_transitions=0 must mean ZERO pre-window history: a bare
                # [-extra_transitions:] slice is the full list when extra is 0
                # ([-0:] == [0:]), which leaked the entire pre-window history.
                # The FSDB wrapper already guards `extra_transitions > 0`.
                if extra_transitions > 0:
                    pre_window = trans[max(0, lo - extra_transitions):lo]
                else:
                    pre_window = []
                result[path] = {
                    "value_at_center":       _enrich_value(_value_at(trans, center_ps)),
                    "transitions_in_window": [{"time_ps": t, "time_ns": t / 1000, "value": _enrich_value(v)}
                                              for t, v in filtered],
                    "pre_window_transitions": [{"time_ps": t, "time_ns": t / 1000, "value": _enrich_value(v)}
                                               for t, v in pre_window],
                }
            except Exception as e:
                result[path] = {"error": str(e)}
        return {
            "center_time_ps": center_ps,
            "center_time_ns": center_ps / 1000,
            "window_ps":      window_ps,
            "extra_transitions": extra_transitions,
            "signals":        result,
            "truncated":      False,
        }

    def get_header(self) -> dict:
        self._ensure_parsed()
        if self._end_time_ps == 0:
            for transitions in self._transitions.values():
                if transitions:
                    self._end_time_ps = max(self._end_time_ps, transitions[-1][0])
        return {
            "file":                   self.file_path,
            "format":                 "VCD",
            "timescale_ps":           self._timescale_fs / 1000,
            "scale_unit":             self._timescale_raw or "1ps(assumed)",
            "scale_fs_per_tick":      self._timescale_fs,
            "simulation_duration_ps": self._end_time_ps,
            "simulation_duration_ns": self._end_time_ps / 1000,
            "total_signals":          len(self._signals),
            "producer_hint":           self._producer_hint,
            "producer_evidence":       self._producer_evidence,
        }

    def get_summary(self) -> dict:
        return {**self.get_header(), "top_modules": list(self._top_modules),
                "sample_signals": list(islice(self._path_to_sym, 20))}

    def enumerate_scope_page(self, scope: str | None = None, *, cursor=None,
                             direct: bool = False, max_items: int = PAGE_ITEMS,
                             max_bytes: int = PAGE_BYTES, max_visited: int = PAGE_SCAN) -> dict:
        """Page over references to existing declarations, without full metadata copies."""
        check_cancelled()
        scope = normalize_scope(scope)
        if "\0" in scope:
            raise ValueError("scope contains NUL")
        max_items, max_bytes, max_visited = page_limits(max_items, max_bytes, max_visited)
        self._ensure_parsed()
        try:
            identity = file_identity(self.file_path)
        except OSError as exc:
            raise ScopeIdentityChanged("waveform disappeared during scope enumeration") from exc
        if identity != self._parsed_identity:
            raise ScopeIdentityChanged("VCD changed after parsing; obtain a new parser")
        if cursor is not None:
            if not isinstance(cursor, ScopeCursor):
                raise ValueError("invalid scope cursor")
            cursor.validate(self._scope_epoch, identity, scope, direct)
        if self._scope_index is None:
            self._scope_index = tuple(sorted(p for p in self._path_to_sym if p not in self._range_aliases))
        check_cancelled()
        paths = self._scope_index
        prefix = scope + "." if scope else ""
        index = bisect_left(paths, cursor.next_path if cursor else prefix)
        rows, visited, nbytes, reason = [], 0, 0, None
        while index < len(paths) and paths[index].startswith(prefix):
            check_cancelled()
            path = paths[index]
            if len(rows) >= max_items:
                reason = "page_items"
                break
            if visited >= max_visited:
                reason = "page_scan"
                break
            visited += 1
            ends = self._scope_ends[path]
            if scope and len(scope) not in ends:
                index += 1
                continue
            if direct and ends and ends[-1] > len(scope):
                child_end = next(end for end in ends if end > len(scope))
                index = bisect_left(paths, path[:child_end] + "/", lo=index + 1)
                continue
            size = 20 + len(path.encode())
            if size > max_bytes - nbytes:
                reason = "page_bytes"
                break
            parent_end = ends[-1] if ends else 0
            signal = self._signals[self._path_to_sym[path]]
            rows.append({"path": path, "name": path[parent_end + bool(parent_end):],
                         "scope": path[:parent_end], "width": signal["width"],
                         "direction": None, "var_type": signal.get("var_type")})
            nbytes += size
            index += 1
        if file_identity(self.file_path) != identity:
            raise ScopeIdentityChanged("VCD changed during scope enumeration")
        complete = index == len(paths) or not paths[index].startswith(prefix)
        return {"results": rows, "complete": complete,
                "cursor": None if complete else ScopeCursor(self._scope_epoch, identity, scope, direct, paths[index]),
                "visited": visited, "bytes_returned": nbytes, "mode": "vcd_scope_v1",
                "stop_reason": None if complete else reason}

    def search_signals(self, keyword: str, max_results: int = 100) -> dict:
        """Search signals in a VCD using the in-memory path index.

        Each result carries `var_type` (wire/reg/integer/real/parameter/...) when
        the source `$var` declaration provided one. VCD format does not carry
        port direction, so `direction` is always None for VCD waveforms — clients
        that need input/output/inout filtering must use FSDB.
        """
        self._ensure_parsed()
        kw = keyword.lower()
        matched = [
            {"path": p, "name": p.split(".")[-1],
             "width": self._signals[s]["width"],
             "direction": None,
             "var_type": self._signals[s].get("var_type") or None}
            for p, s in self._path_to_sym.items()
            if kw in p.lower() and (p not in self._range_aliases or "[" in kw)
        ]
        matched.sort(key=lambda item: (-_signal_rank(item["path"], kw), item["path"]))
        total_matched = len(matched)
        matched = matched[:max_results]
        return annotate_signal_search_result({
            "keyword":        keyword,
            "total_matched":  total_matched,
            "truncated": total_matched > len(matched),
            "results":        matched,
        })

    def get_signal_width(self, signal_path: str) -> int:
        self._ensure_parsed()
        sym = self._resolve(signal_path)
        return int(self._signals[sym]["width"])

    # ── Internal ────────────────────────────────────────────────────

    def _ensure_parsed(self):
        if not self._parsed:
            identity = file_identity(self.file_path)
            self._parse()
            if file_identity(self.file_path) != identity:
                raise ScopeIdentityChanged("VCD changed while parsing")
            self._parsed_identity = identity
            self._parsed = True

    def _resolve(self, signal_path: str) -> str:
        if signal_path in self._path_to_sym:
            return self._path_to_sym[signal_path]
        for full, sym in self._path_to_sym.items():
            if full.endswith("." + signal_path) or full == signal_path:
                return sym
        sample = list(self._path_to_sym.keys())[:5]
        raise KeyError(f"Signal not found: '{signal_path}'. Example paths: {sample}")

    def _parse(self):
        if not Path(self.file_path).exists():
            raise FileNotFoundError(f"VCD file does not exist: {self.file_path}")
        with open(self.file_path, "r", errors="replace") as f:
            content = f.read()

        version = re.search(r'\$version\s+(.*?)\s*\$end', content, re.DOTALL)
        if version:
            self._producer_hint = normalize_vcd_producer(version.group(1))
            if self._producer_hint is not None:
                self._producer_evidence = "vcd_version_header"

        # timescale
        ts = re.search(r'\$timescale\s+(.*?)\s*\$end', content, re.DOTALL)
        if ts:
            self._timescale_raw = ts.group(1).strip()
            self._timescale_fs = _parse_timescale_fs(self._timescale_raw)

        scope_stack   = []
        scope_ends = ()
        current_ps    = 0
        tokens        = content.split()
        ranged_declarations: dict[str, set[str]] = {}
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok == "$scope":
                scope_name = tokens[i + 2] if i + 2 < len(tokens) else "unknown"
                scope_stack.append(scope_name)
                scope_ends += ((scope_ends[-1] + 1 if scope_ends else 0) + len(scope_name),)
                if len(scope_stack) == 1 and scope_name not in self._top_modules:
                    self._top_modules.append(scope_name)
                i += 4
            elif tok == "$upscope":
                if scope_stack:
                    scope_stack.pop()
                    scope_ends = scope_ends[:-1]
                i += 2
            elif tok == "$var":
                # $var <var_type> <size> <id> <reference> [index/range] $end
                # var_type is the language-level type (wire/reg/integer/real/parameter/...).
                # VCD has no port direction, so direction stays None at higher layers.
                var_type = tokens[i + 1] if i + 1 < len(tokens) else ""
                width  = int(tokens[i + 2]) if tokens[i + 2].isdigit() else 1
                symbol = tokens[i + 3]
                name   = tokens[i + 4]
                end = tokens.index("$end", i + 5)
                selection = "".join(tokens[i + 5:end])
                if selection and not re.fullmatch(r"\[-?\d+(?::-?\d+)?\]", selection):
                    raise ValueError("Unsupported VCD declaration selection")
                base = ".".join(scope_stack + [name])
                full = base + selection
                if full in self._path_to_sym and self._path_to_sym[full] != symbol:
                    raise ValueError(f"Conflicting VCD declarations for '{full}'")
                if selection:
                    ranged_declarations.setdefault(base, set()).add(full)
                self._signals[symbol]     = {"path": full, "width": width, "var_type": var_type}
                self._path_to_sym[full]   = symbol
                self._scope_ends[full] = scope_ends
                self._transitions.setdefault(symbol, [])
                i = end + 1
            elif tok.startswith("#"):
                try:
                    # ceil to ps, matching the FSDB wrapper's convention: a
                    # sub-ps transition time is reported as the next integer
                    # ps, so querying at a reported timestamp always lands
                    # at-or-after the transition. Exact for >=1ps timescales.
                    ticks = int(tok[1:])
                    current_ps = (ticks * self._timescale_fs + 999) // 1000
                    self._end_time_ps = max(self._end_time_ps, current_ps)
                except ValueError:
                    pass
                i += 1
            elif tok.startswith(("b", "B")):
                val = tok
                if i + 1 < len(tokens):
                    sym = tokens[i + 1]
                    if sym in self._transitions:
                        self._transitions[sym].append((current_ps, val))
                i += 2
            elif len(tok) >= 2 and tok[0] in "01xXzZ":
                val = tok[0]
                sym = tok[1:]
                if sym in self._transitions:
                    self._transitions[sym].append((current_ps, val))
                i += 1
            else:
                i += 1

        # Preserve the traditional bare-name access for one dumped vector.
        # A separately dumped bit is never a scalar alias of the whole bus;
        # multiple slices also cannot supply one unambiguous whole-bus value.
        for base, paths in ranged_declarations.items():
            if len(paths) == 1 and base not in self._path_to_sym:
                full = next(iter(paths))
                if ":" in full[len(base):]:
                    self._path_to_sym[base] = self._path_to_sym[full]
                    self._scope_ends[base] = self._scope_ends[full]
                    self._range_aliases.add(full)


# ── Utility ────────────────────────────────────────────────────────

def _value_at(transitions: list, time_ps: int):
    """Search existing records in O(log N), without copying their timestamps.

    Search only the time key: rounded sub-ps events and multiple changes in
    one tick may have equal timestamps and must retain their file order.
    """
    if not transitions:
        return None
    idx = bisect_right(transitions, time_ps, key=_transition_time) - 1
    if idx < 0:
        return None
    return transitions[idx][1]


def _enrich_value(binary_str: str | None) -> dict | None:
    if binary_str is None:
        return None
    result = {"bin": binary_str}
    normalized = binary_str.strip()
    if not normalized or any(c in normalized for c in "xXzZu?"):
        result["hex"] = None
        result["dec"] = None
        return result
    if normalized.startswith(("b", "B")):
        normalized = normalized[1:]
        result["bin"] = normalized
    try:
        val = int(normalized, 2)
    except ValueError:
        result["hex"] = None
        result["dec"] = None
        return result
    width = len(normalized)
    hex_width = max(1, (width + 3) // 4)
    result["hex"] = f"0x{val:0{hex_width}x}"
    result["dec"] = val
    return result


def _parse_timescale_fs(ts_str: str) -> int:
    """Parse a $timescale directive into integer fs per time unit.

    Integer fs is the internal base unit: integer ps would truncate a sub-ps
    timescale like 100fs to 0 and multiply every timestamp by zero. "s" must
    stay last so "fs"/"ps"/"ns"/"us"/"ms" match their own suffix first.
    """
    ts_str = ts_str.strip().replace(" ", "")
    units  = {"fs": 1, "ps": 1000, "ns": 1_000_000, "us": 10**9,
               "ms": 10**12, "s": 10**15}
    for unit, mult in units.items():
        if ts_str.endswith(unit):
            try:
                return round(float(ts_str[:-len(unit)]) * mult)
            except ValueError:
                pass
    return 1000


def _signal_rank(path: str, keyword: str) -> int:
    lower = path.lower()
    score = 0
    if path.split(".")[-1].lower() == keyword:
        score += 8
    elif lower.endswith(f".{keyword}"):
        score += 6
    elif keyword in lower:
        score += 3
    if any(token in lower for token in ("dut", "core", "rtl", "design")):
        score += 4
    if any(token in lower for token in ("assert", "checker", "scoreboard", "uvm", "monitor")):
        score -= 3
    return score
