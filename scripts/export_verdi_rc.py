#!/usr/bin/env python3
"""Export one diagnosed issue to the caller's debug/ directory."""

import argparse
from datetime import datetime
from itertools import count
import json
import os
from pathlib import Path
import re
import sys
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from config import VERDI_RC_EXPORT
from src.fsdb_parser import FSDBParser
from src.scope_metadata import file_identity
from src.verdi_rc import render_verdi_rc


EXAMPLE = """Example JSON (stdin by default, or pass a JSON file):
{"issue": "read_mismatch", "fsdb": "/run/wave.fsdb",
 "groups": {"Read data": [{"path":"tb.actual[7:0]","role":"evidence"},
                           {"path":"tb.expected[7:0]","role":"evidence"}]},
 "changes": {"mux.sv:21 data[7:4] -> data[3:0]": [
   {"path":"tb.u0.data[7:0]", "slice":[7,4]}, {"path":"tb.u0.data[7:0]", "slice":[3,0]}]},
 "error_time": 1250000, "first_deviation": "1210ns"}
Times are integer picoseconds or unit strings (fs, ps, ns, us, ms, s).
Omit an unknown time. Use exact dump declarations or select a packed slice
in declared [left, right] order. For escaped names give literal hierarchy
components as a JSON list. Mark signals that support the final analysis with
role "evidence" (cyan). For each reported edit, put its file, line and change
in a changes key. Its rows default to "modified" (yellow). Use "evidence" for
an observed selector and "context" for orientation only. Modified takes color
priority across groups. An empty list keeps an edit with no mapped signal visible."""


DISABLED = {"status": "disabled", "reason": "TRACEWEAVE_VERDI_RC_EXPORT=0"}


def _unique_keys(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError(f"duplicate JSON key: {name!r}")
        result[name] = value
    return result


def _issue_slug(issue):
    if not isinstance(issue, str):
        raise ValueError("issue must be a short problem name")
    slug = re.sub(r"[^\w-]+", "_", issue).strip("_-")
    # Leave room for the timestamp and collision suffix on byte-limited filesystems.
    slug = slug.encode("utf-8")[:160].decode("utf-8", errors="ignore").rstrip("_-")
    if not slug:
        raise ValueError("issue must contain a letter or digit")
    return slug


def _publish(directory: Path, stem: str, view: str) -> Path:
    directory.mkdir(exist_ok=True)
    staging = directory / f".{uuid4().hex}.tmp"
    with staging.open("x", encoding="utf-8") as stream:
        try:
            stream.write(view)
            stream.flush()
            for number in count(1):
                suffix = "" if number == 1 else f"_{number}"
                target = directory / f"{stem}{suffix}.rc"
                try:
                    # Publish the complete inode without replacing another agent's report.
                    os.link(staging, target)
                except FileExistsError:
                    continue
                return target
        finally:
            staging.unlink()


def export_issue(spec: dict) -> dict:
    if not VERDI_RC_EXPORT:
        return DISABLED.copy()
    if not isinstance(spec, dict):
        raise ValueError("expected one JSON object per issue")
    unknown = spec.keys() - {"issue", "fsdb", "groups", "changes", "error_time", "first_deviation"}
    if unknown:
        raise ValueError(f"unknown fields: {', '.join(sorted(unknown))}")
    slug = _issue_slug(spec.get("issue"))
    if not isinstance(spec.get("fsdb"), str) or not spec["fsdb"]:
        raise ValueError("fsdb must identify the waveform used for this diagnosis")
    wave = Path(spec["fsdb"]).resolve(strict=True)
    if not wave.is_file():
        raise ValueError(f"expected an FSDB file: {wave}")
    identity = file_identity(wave)
    parser = FSDBParser(str(wave))
    try:
        header = parser.get_header()
        if not header["scale_fs_per_tick"]:
            raise ValueError("the selected FSDB has an unknown time scale")
        view = render_verdi_rc(
            spec.get("groups"), changes=spec.get("changes"), error_time=spec.get("error_time"),
            first_deviation=spec.get("first_deviation"),
            duration_ps=header["simulation_duration_ps"], check_signal=parser.get_signal_declaration,
        )
    finally:
        parser.close()
    if file_identity(wave) != identity:
        raise ValueError("FSDB changed during export; confirm the evidence in the current file")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    rc = _publish(Path.cwd() / "debug", f"{slug}_{timestamp}", view)
    return {"rc_path": str(rc), "fsdb": str(wave)}


def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__, epilog=EXAMPLE,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    cli.add_argument("manifest", nargs="?", default="-", help="JSON file, or - for stdin")
    args = cli.parse_args()
    if not VERDI_RC_EXPORT:
        print(json.dumps(DISABLED))
        return 0
    # The FFR runtime also writes to fd 1 at process exit. Reserve the original
    # stdout for the machine-readable receipt for the lifetime of this CLI process.
    with os.fdopen(os.dup(1), "w", encoding="utf-8") as output:
        os.dup2(2, 1)
        try:
            if args.manifest == "-":
                spec = json.load(sys.stdin, object_pairs_hook=_unique_keys)
            else:
                with Path(args.manifest).open(encoding="utf-8") as stream:
                    spec = json.load(stream, object_pairs_hook=_unique_keys)
            receipt = export_issue(spec)
        except (ValueError, KeyError, OSError, RuntimeError) as exc:
            cli.exit(1, f"export_verdi_rc: {exc}\n")
        print(json.dumps(receipt, ensure_ascii=False), file=output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
