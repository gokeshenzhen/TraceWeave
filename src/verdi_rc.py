"""Render an nWave signal RC for the FSDB already open in the viewer."""

from collections.abc import Callable
from fractions import Fraction
from functools import cache
import re


_UNIT_FS = {"fs": 1, "ps": 10**3, "ns": 10**6, "us": 10**9, "ms": 10**12, "s": 10**15}


def _time_fs(value: object) -> int:
    if type(value) is int and value >= 0:
        return value * 1000  # TraceWeave's integer time fields are in ps.
    if isinstance(value, str):
        match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*(fs|ps|ns|us|ms|s)", value.strip())
        if match:
            time = Fraction(match[1]) * _UNIT_FS[match[2]]
            if time.denominator == 1:
                return int(time)
    raise ValueError("time must be non-negative integer ps or a unit literal precise to 1fs")


def _time_lines(error_time, first_deviation, duration_ps: int) -> list[str]:
    anchors = {}
    for name, value in (("reported_error", error_time), ("first_deviation", first_deviation)):
        if value is not None:
            anchors[name] = _time_fs(value)
    if not anchors:
        raise ValueError("supply a confirmed error_time or first_deviation")
    if max(anchors.values()) > duration_ps * 1000:
        raise ValueError("evidence time is beyond the end of the selected FSDB")

    # RC coordinates are in windowTimeUnit, independent of file ticks and GUI preferences.
    sub_ps = any(time % 1000 for time in anchors.values())
    unit, fs_per_unit = ("fs", 1) if sub_ps else ("ps", 1000)
    times = {name: time // fs_per_unit for name, time in anchors.items()}
    first, last = min(times.values()), max(times.values())
    padding = max(10 * _UNIT_FS["ns"] // fs_per_unit, (last - first) // 10)
    left = max(0, first - padding)
    right = min(duration_ps * 1000 // fs_per_unit, last + padding)
    lines = [f"windowTimeUnit 1{unit}"]
    if right > left:
        lines.append(f"zoom {left} {right} 1{unit[0]}")
    cursor = times.get("reported_error", first)
    lines.extend([f"cursor {cursor}", f"marker {times.get('first_deviation', cursor)}"])
    for name, time in times.items():
        color = "ID_RED5" if name == "reported_error" else "ID_YELLOW8"
        lines.append(f"userMarker {time} {name} {color} long_dashed")
    return lines


def _signal_parts(signal: object) -> list[str]:
    if isinstance(signal, str):
        if "\\" in signal:
            raise ValueError(f"give escaped signal {signal!r} as a list of hierarchy components")
        signal = signal.split(".")
    if not isinstance(signal, list) or not signal:
        raise ValueError("each signal must be a full dump path or a list of hierarchy components")
    for part in signal:
        if not isinstance(part, str) or not part or not part.isprintable():
            raise ValueError(f"invalid signal component {part!r}")
        if not part.startswith("\\") and any(c in part for c in (' ', '.', '/', '\\', '"')):
            raise ValueError(f"invalid unescaped signal component {part!r}")
    return signal


def _rc_component(part: str) -> str:
    # nWave terminates an escaped HDL identifier with a space before the next '/'.
    if part.startswith("\\") and " " not in part:
        return part + " "
    return part


def _label(text: str) -> str:
    return '"' + text.replace('"', r'\"') + '"'


def _signal_row(signal, declaration, default_role):
    role = default_role
    bounds = None
    if isinstance(signal, dict):
        if "path" not in signal or signal.keys() - {"path", "slice", "role"}:
            raise ValueError("a signal object needs an exact path and accepts optional slice and role")
        role = signal.get("role", default_role)
        if role not in ("evidence", "modified", "context"):
            raise ValueError("signal role must be evidence, modified or context")
        if "slice" in signal:
            bounds = signal["slice"]
            if not isinstance(bounds, list) or len(bounds) != 2 or any(type(n) is not int for n in bounds):
                raise ValueError("slice must be two integer indices [left, right]")
        signal = signal["path"]
    parts = _signal_parts(signal)
    path = ".".join(parts)
    metadata = declaration(path)
    components = [_rc_component(part) for part in parts]
    if bounds is not None:
        left, right = bounds
        declared = metadata["declared_range"]
        first, last = declared["left"], declared["right"]
        if min(bounds) < min(first, last) or max(bounds) > max(first, last):
            raise ValueError(f"slice {bounds} is outside {path}")
        if left != right and (left < right) != (first < last):
            raise ValueError(f"slice {bounds} reverses the declared direction of {path}")
        # An escaped identifier ends in a space. Only a following range is syntax.
        leaf = re.sub(r"\[-?\d+(?::-?\d+)?\]$", "", components[-1])
        components[-1] = leaf + (f"[{left}]" if left == right else f"[{left}:{right}]")
    scope = "/".join(components[:-1]).rstrip() or "/"
    return scope, "/" + "/".join(components), role


def _collect_group(name, signals, declaration, colors, *, changed):
    if not isinstance(name, str) or not name.strip() or not name.isprintable():
        raise ValueError("each group needs a printable name")
    if not isinstance(signals, list) or (not changed and not signals):
        raise ValueError(f"group {name!r} needs a list of signals (non-empty for function groups)")
    title = f"RTL change - {name}" if changed else name
    if not signals:
        title += " (no mapped waveform signal)"
    default_role = "modified" if changed else "context"
    scopes = {}
    for signal in signals:
        scope, path, role = _signal_row(signal, declaration, default_role)
        scopes.setdefault(scope, {})[path] = None
        if role == "modified":
            colors[path] = "ID_YELLOW5"
        elif role == "evidence":
            colors.setdefault(path, "ID_CYAN5")
    return title, scopes


def render_verdi_rc(groups: dict, *, changes: dict | None = None, error_time=None,
                    first_deviation=None, duration_ps: int, check_signal: Callable[[str], object]) -> str:
    """Keep source edits first and apply each signal's strongest role across groups."""
    lines = ["Magic 271485", "Revision Verdi_V-2023.12-SP2", ""]
    lines.extend(_time_lines(error_time, first_deviation, duration_ps))
    if not isinstance(groups, dict) or not groups:
        raise ValueError("groups must map function names to non-empty lists of signals")
    if changes is not None and not isinstance(changes, dict):
        raise ValueError("changes must map source edit locations and descriptions to signal lists")
    declaration = cache(check_signal)
    colors = {}
    display_groups = []
    for changed, sections in ((True, changes or {}), (False, groups)):
        for name, signals in sections.items():
            display_groups.append(_collect_group(name, signals, declaration, colors, changed=changed))
    for title, scopes in display_groups:
        lines.extend(["", f"addGroup {_label(title)}"])
        for scope, paths in scopes.items():
            label = _label(scope)
            lines.append(f"addSubGroup {label}")
            for path in paths:
                color = f"-c {colors[path]} " if path in colors else ""
                lines.append(f"addSignal {color}-h 15 {path}")
            lines.append(f"endSubGroup {label}")
    return "\n".join(lines) + "\n"
