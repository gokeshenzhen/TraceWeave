"""Lossless grouping of Source Graph statement rows after bounded analysis."""

from copy import deepcopy
import json

from .cancellation import check_cancelled


def group_driver_evidence(rows: list[dict]) -> list[dict]:
    """Share identical metadata, retaining every location and input row order.

    Only known writer identities can group. Missing identity is not one common
    process. The legacy source_line/source_column name the first evidence row.
    No display or analysis limit is imposed here.
    """
    groups = {}
    semantic_encodings = {}
    for index, row in enumerate(rows):
        check_cancelled()
        common = {k: v for k, v in row.items() if k not in {"source_line", "source_column"}}
        # Backend rows already share immutable semantic dictionaries. Encode
        # each shared object once, while retaining value-based equality for
        # distinct but equal dictionaries. This does not key semantics by ID.
        semantics = common.pop("statement_semantics", None)
        semantic_key = None
        if semantics is not None:
            identity = id(semantics)
            if identity not in semantic_encodings:
                semantic_encodings[identity] = json.dumps(semantics, sort_keys=True, separators=(",", ":"))
            semantic_key = semantic_encodings[identity]
        key = ((json.dumps(common, sort_keys=True, separators=(",", ":")),
                "statement_semantics" in row, semantic_key)
               if row.get("structural_driver_id") else index)
        if key not in groups:
            groups[key] = {**row, "evidence_index": index,
                           "statement_count": 0, "statement_evidence": []}
        group = groups[key]
        if row.get("source_line") is not None:
            evidence = {k: row[k] for k in ("source_line", "source_column") if k in row}
            group["statement_evidence"].append({**evidence, "evidence_index": index})
            group["statement_count"] += 1
    return list(groups.values())


def expand_driver_evidence(groups: list[dict]) -> list[dict]:
    """Restore the statement rows without source files, handles or a server."""
    rows = []
    for group in groups:
        check_cancelled()
        common = {k: deepcopy(v) for k, v in group.items()
                  if k not in {"statement_evidence", "statement_count", "evidence_index"}}
        if group.get("statement_evidence"):
            common.pop("source_line", None)
            common.pop("source_column", None)
        evidence = group.get("statement_evidence") or [{"evidence_index": group["evidence_index"]}]
        for item in evidence:
            rows.append((item["evidence_index"], {**common, **{
                k: v for k, v in item.items() if k != "evidence_index"}}))
    return [row for _, row in sorted(rows, key=lambda item: item[0])]
