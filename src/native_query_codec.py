"""Private metadata-only wire format. No DesignContext or RTL bodies."""
from dataclasses import asdict, fields
import hashlib
import json

from pydantic import TypeAdapter

from .design_context_codec import _encoded
from .native_query import NativeQueryInputs, metadata_identity
from .compile_session_snapshot import CompileSessionSnapshot, FileContentSnapshot


def encode_native_inputs(inputs):
    if not isinstance(inputs, NativeQueryInputs):
        raise TypeError("native inputs required")
    value = {"version": 1, "inputs": asdict(inputs)}
    raw = _encoded(value)
    return {**value, "payload_sha256": hashlib.sha256(raw).hexdigest()}


def decode_native_inputs(payload):
    if not isinstance(payload, dict) or set(payload) != {"version", "inputs", "payload_sha256"} or payload["version"] != 1:
        raise ValueError("invalid native metadata transport")
    value = {k: payload[k] for k in ("version", "inputs")}
    if hashlib.sha256(_encoded(value)).hexdigest() != payload["payload_sha256"]:
        raise ValueError("native metadata payload changed")
    if set(payload["inputs"]) != {f.name for f in fields(NativeQueryInputs)}:
        raise ValueError("invalid native input fields")
    snapshot = payload["inputs"].get("metadata_snapshot")
    if snapshot is not None:
        if not isinstance(snapshot, dict) or set(snapshot) != {f.name for f in fields(CompileSessionSnapshot)}:
            raise ValueError("invalid metadata observation fields")
        for record in snapshot.get("records", ()):
            if not isinstance(record, dict) or set(record) != {f.name for f in fields(FileContentSnapshot)}:
                raise ValueError("invalid metadata file observation fields")
    inputs = TypeAdapter(NativeQueryInputs).validate_json(json.dumps(payload["inputs"]))
    if inputs.backend_preference != "auto" or not inputs.selected_top:
        raise ValueError("native worker requires selected auto inputs")
    if inputs.ordered_compile_logs:
        if not inputs.metadata_snapshot or not inputs.compile_result_json or not inputs.compile_metadata_identity_sha256:
            raise ValueError("native metadata observation missing")
        result = inputs.compile_result()
        if result.get("simulator") != inputs.simulator:
            raise ValueError("metadata simulator mismatch")
        if (len(inputs.ordered_compile_logs) > 17 or len(inputs.metadata_snapshot.records) > 32768
                or len(inputs.aliases) > 65536 or len(inputs.unavailable) > 32768
                or inputs.metadata_snapshot.total_bytes > 128 * 1024 * 1024):
            raise ValueError("native metadata observation exceeds capture limits")
        aliases = dict(inputs.aliases)
        allowed = set(inputs.ordered_compile_logs)
        allowed.update(item["path"] for item in (result.get("compile_evidence") or {}).get("filelists", []) if item.get("path"))
        allowed = {aliases.get(p, p) for p in allowed}
        if any(r.path not in allowed for r in inputs.metadata_snapshot.records):
            raise ValueError("native transport contains nonmetadata observations")
        expected = metadata_identity(inputs.ordered_compile_logs, inputs.simulator, inputs.selected_top,
            inputs.environment, inputs.aliases, inputs.unavailable, inputs.metadata_snapshot)
        if inputs.compile_metadata_identity_sha256 != expected:
            raise ValueError("native metadata identity mismatch")
    elif inputs.metadata_snapshot is not None or inputs.compile_result_json is not None or inputs.compile_metadata_identity_sha256 is not None:
        raise ValueError("pure KDB input cannot invent compile metadata")
    return inputs
