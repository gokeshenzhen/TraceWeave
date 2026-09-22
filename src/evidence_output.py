"""Response-local, lossless presentation after analysis and its display limits.

Only identical TL-UL selection receipts are factored. Their containing channel,
transaction, coverage, identity and time facts remain inline. No waveform data
is read and no object is retained between calls.
"""
from __future__ import annotations

from copy import deepcopy
import json

from pydantic import BaseModel

from .cancellation import check_cancelled
from .schemas import COMPACT_OUTPUT_TOOLS, CompactEvidenceResult, SelectionEvidenceReference


def _field(value, key):
    # SchemaModel.get() materializes the entire model; never use it to inspect
    # one field of a large transaction result.
    if isinstance(value, BaseModel):
        return getattr(value, key, None)
    return value.get(key) if isinstance(value, dict) else None


def selection_projection(tool: str, result: BaseModel | dict) -> tuple[list, dict]:
    check_cancelled()
    if tool not in COMPACT_OUTPUT_TOOLS:
        raise ValueError("compact_output_tool_unsupported")
    references = []
    exclude = {}
    selections = _field(result, "selections")
    if tool == "inspect_tlul" and selections:
        # Three fixed candidates: no unbounded intern table or whole-tree search.
        paths = (("channels", "a"), ("channels", "d"), ("transactions",))
        for path in paths:
            check_cancelled()
            parent = result
            for key in path:
                parent = _field(parent, key)
            if _field(parent, "selections") == selections:
                target = exclude
                for key in path:
                    target = target.setdefault(key, {})
                target["selections"] = True
                references.append(SelectionEvidenceReference(
                    path="/result/" + "/".join(path) + "/selections"))
    check_cancelled()
    return references, exclude


def compact_evidence_result(tool: str, result: BaseModel | dict) -> CompactEvidenceResult:
    references, exclude = selection_projection(tool, result)
    if isinstance(result, BaseModel):
        payload = result.model_dump(mode="json", exclude_none=True, exclude=exclude)
    else:
        payload = deepcopy(result)
        for ref in references:
            parent = payload
            for key in ref.path.split("/")[2:-1]:
                parent = parent[key]
            del parent["selections"]
    return CompactEvidenceResult(tool=tool, result=payload, references=references)


def serialize_compact_result(tool: str, result: BaseModel | dict) -> str:
    if isinstance(result, BaseModel):
        references, exclude = selection_projection(tool, result)
        # Encode validated facts directly. Do not allocate a second tree of
        # every retained transaction just to remove three selection receipts.
        body = result.model_dump_json(exclude_none=True, exclude=exclude)
        metadata = CompactEvidenceResult.model_fields["format"].default
        refs = json.dumps([ref.model_dump() for ref in references], separators=(",", ":"))
        text = (f'{{"format":{json.dumps(metadata)},"tool":{json.dumps(tool)},'
                f'"result":{body},"references":{refs}}}')
    else:
        text = compact_evidence_result(tool, result).model_dump_json()
    check_cancelled()
    return text


def expand_compact_result(payload: dict | CompactEvidenceResult) -> dict:
    """Restore the full JSON value without a server, file access or live handles.

    Invalid, duplicate or dangling references raise instead of returning an empty
    evidence list. Copies keep mutations of one expanded receipt local.
    """
    compact = CompactEvidenceResult.model_validate(payload)
    result = deepcopy(compact.result)
    for ref in compact.references:
        parent = result
        for key in ref.path.split("/")[2:-1]:
            parent = parent[key]
        parent["selections"] = deepcopy(result["selections"])
    return result
