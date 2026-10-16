"""Bounded private transport for an immutable prepared design observation.

Only captured metadata, digests and path observations cross the boundary. The
transport digest detects a changed message; it proves neither current inputs
nor source/KDB association. A receiving worker must validate current inputs
before native work and again before publishing its result. No handle, source
body or frontend state is serialized, and decoding performs no filesystem I/O.
"""
from __future__ import annotations

from dataclasses import asdict, fields
import hashlib
import io
import json
import re

from pydantic import TypeAdapter

from .cancellation import check_cancelled
from .compile_session_snapshot import CompileSessionSnapshot, FileContentSnapshot
from .design_context import DesignContext, IncludeResolution


CONTEXT_WIRE_VERSION = 1
MAX_CONTEXT_WIRE_BYTES = 8 * 1024 * 1024
_ADAPTER = TypeAdapter(DesignContext)
_SHA256 = re.compile(r"[0-9a-f]{64}")


class ContextTransportLimit(ValueError):
    """The bounded transport cannot represent this prepared observation."""


def _encoded(value):
    encoder = json.JSONEncoder(sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    output, size = io.BytesIO(), 0
    for part in encoder.iterencode(value):
        check_cancelled()
        size += len(part)  # ensure_ascii: character and encoded byte counts agree.
        if size > MAX_CONTEXT_WIRE_BYTES:
            raise ContextTransportLimit("design_context_transport_byte_limit")
        output.write(part.encode("ascii"))
    return output.getvalue()


def _shape(value, cls):
    if not isinstance(value, dict) or set(value) != {item.name for item in fields(cls)}:
        raise ValueError("invalid design context transport fields")


def _validate_observation(context):
    """Validate portable field invariants without reinterpreting design identity.

    Explicit historical recovery has its own identity derivation, so a worker
    must not replace that identity with the ordinary preparation hash formula.
    The parent binds the opaque context identity to this complete payload hash.
    """
    if context.simulator not in {"vcs", "xcelium", "verilator"}:
        raise ValueError("unsupported context simulator")
    if context.backend_preference not in {"auto", "source"}:
        raise ValueError("unsupported context backend preference")
    if not _SHA256.fullmatch(context.identity_sha256) or not _SHA256.fullmatch(context.snapshot.content_fingerprint_sha256):
        raise ValueError("invalid context fingerprint")
    if not context.ordered_compile_logs or any(not path for path in context.ordered_compile_logs):
        raise ValueError("context compile logs must be explicit")
    if context.selected_top is not None and not context.selected_top:
        raise ValueError("selected context top must be nonempty")
    if not isinstance(json.loads(context.compile_result_json), dict):
        raise ValueError("captured compile record must be an object")
    paths = set()
    for record in context.snapshot.records:
        check_cancelled()
        if not record.path or record.path in paths or record.size < 0 or not _SHA256.fullmatch(record.sha256):
            raise ValueError("invalid or duplicate captured source record")
        paths.add(record.path)
    for observations in (context.aliases, context.unavailable, context.path_observations,
                         context.environment, context.replay_environment):
        seen = set()
        for key, _ in observations:
            check_cancelled()
            if not key or key in seen:
                raise ValueError("invalid or duplicate context observation")
            seen.add(key)
    for _, observation in (*context.unavailable, *context.path_observations):
        check_cancelled()
        if (len(observation) not in {2, 6} or not isinstance(observation[0], str) or not observation[0]
                or any(type(value) is not int for value in observation[1:])):
            raise ValueError("invalid captured path availability")
    for resolution in context.include_resolutions:
        check_cancelled()
        if ((resolution.selected is None) != (resolution.selected_spelling is None)
                or (resolution.selected_spelling is not None
                    and resolution.selected_spelling not in resolution.candidates)):
            raise ValueError("invalid captured include selection")


def encode_design_context(context: DesignContext) -> dict:
    """Copy bounded JSON-only metadata; never persist it or read source files."""
    check_cancelled()
    if not isinstance(context, DesignContext):
        raise TypeError("a prepared DesignContext is required")
    raw = _encoded(asdict(context))
    # Validate even hand-constructed dataclasses; annotations alone do not
    # stop mutable or incorrectly typed fields from entering a worker request.
    validated = _ADAPTER.validate_json(raw, strict=True)
    _validate_observation(validated)
    return {"version": CONTEXT_WIRE_VERSION, "context": json.loads(raw),
            "payload_sha256": hashlib.sha256(raw).hexdigest()}


def decode_design_context(payload) -> DesignContext:
    check_cancelled()
    if not isinstance(payload, dict) or set(payload) != {"version", "context", "payload_sha256"}:
        raise ValueError("invalid design context transport envelope")
    if type(payload["version"]) is not int or payload["version"] != CONTEXT_WIRE_VERSION:
        raise ValueError("unsupported design context transport version")
    expected = payload["payload_sha256"]
    if not isinstance(expected, str) or not _SHA256.fullmatch(expected):
        raise ValueError("invalid design context transport digest")
    value = payload["context"]
    raw = _encoded(value)
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("design context transport changed")
    # The dataclass adapter can otherwise ignore extra fields. Check each
    # nested record explicitly before materializing immutable tuple fields.
    _shape(value, DesignContext)
    _shape(value["snapshot"], CompileSessionSnapshot)
    for item in value["snapshot"]["records"]:
        check_cancelled()
        _shape(item, FileContentSnapshot)
    for item in value["include_resolutions"]:
        check_cancelled()
        _shape(item, IncludeResolution)
    context = _ADAPTER.validate_json(raw, strict=True)
    _validate_observation(context)
    check_cancelled()
    return context
