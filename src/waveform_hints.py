"""Non-semantic waveform producer and tool-signal hints."""

from __future__ import annotations


_TOOL_PSEUDO_SIGNAL_ROLES = {
    ":jasper_formal_clock": "formal_clock",
    ":jasper_formal_reset": "formal_reset",
}


def normalize_vcd_producer(version_text: str | None) -> str | None:
    """Return a fixed producer label without exposing raw VCD header text."""

    if version_text and "jaspergold" in version_text.lower():
        return "jaspergold"
    return None


def tool_pseudo_signal_role(path: str | None) -> str | None:
    if not path:
        return None
    leaf = str(path).rsplit(".", 1)[-1]
    return _TOOL_PSEUDO_SIGNAL_ROLES.get(leaf)


def is_tool_pseudo_signal(path: str | None) -> bool:
    return tool_pseudo_signal_role(path) is not None


VCD_DIRECTION_HINT = (
    "VCD port direction metadata is unavailable; direction=null is unknown. "
    "Use compiled source or an elaborated backend to establish direction."
)


def annotate_signal_search_result(result: dict, *, wave_format: str | None = None) -> dict:
    """Add role hints only to exact known pseudo-signals.

    Ordinary signal rows remain byte-for-byte compatible.
    """

    annotated_rows = []
    for row in result.get("results", []):
        role = tool_pseudo_signal_role(row.get("path"))
        if role is None:
            annotated_rows.append(row)
            continue
        annotated_rows.append(
            {
                **row,
                "is_tool_pseudo_signal": True,
                "tool_pseudo_role": role,
            }
        )
    annotated = {**result, "results": annotated_rows}
    if wave_format == "vcd":
        annotated["hint"] = " ".join(filter(None, (result.get("hint"), VCD_DIRECTION_HINT)))
    return annotated
