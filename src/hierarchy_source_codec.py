"""Versioned source-result validation shared by local and worker boundaries.

Nulls and absent fields retain their different meanings. Trees are validated
iteratively so allowed hierarchy depth does not hit a model recursion limit.
This boundary checks receipt consistency; it does not prove provider facts.
"""
from __future__ import annotations

import json

from . import schemas
from .cancellation import check_cancelled


def _tree(rows, *, classes=False):
    if not isinstance(rows, list) or len(rows) > 5000:
        raise ValueError("a source forest must be a bounded list")
    pending = [(row, None, 0) for row in reversed(rows)]
    seen, paths, count = set(), set(), 0
    while pending:
        check_cancelled()
        row, parent, depth = pending.pop()
        if depth > 256 or count >= 5000:
            raise ValueError("source tree exceeds output bounds")
        node = (schemas.SourceClassNode if classes else schemas.SourceScopeNode).model_validate(row)
        identity = node.symbol_id if classes else node.scope_id
        if identity in seen:
            raise ValueError("duplicate source tree identity")
        seen.add(identity)
        count += 1
        if not classes:
            if node.path in paths:
                raise ValueError("duplicate source tree path")
            paths.add(node.path)
        if parent is not None:
            if classes:
                if node.base_status != "resolved" or node.base_symbol_id != parent.symbol_id:
                    raise ValueError("class child does not bind its displayed base")
            elif node.parent != parent.path:
                raise ValueError("scope child does not bind its displayed parent")
        if not classes:
            if node.child_count_lower_bound < len(node.children):
                raise ValueError("scope child count omits rendered children")
            if node.child_count is not None and node.child_count != node.child_count_lower_bound:
                raise ValueError("exact child count disagrees with its lower bound")
        if len(node.children) + len(pending) + count > 5000:
            raise ValueError("source tree exceeds output bounds")
        pending.extend((child, node, depth + 1) for child in reversed(node.children))
    return count


def _counts(result, key):
    total = result.get(key)
    if result.get("count_status") == "exact":
        if total is None or total != result.get("count_lower_bound"):
            raise ValueError("exact query count requires its proved total")
    elif total is not None:
        raise ValueError("a lower bound cannot supply an exact total")


def _classes(result, roots):
    if _tree(roots, classes=True) != result["total"]:
        raise ValueError("class total must count rendered nodes")
    complete, count = result["class_inventory_complete"], result["class_inventory_count"]
    if complete != (count is not None):
        raise ValueError("class inventory count requires complete inventory")
    if count is not None and count != result["class_inventory_lower_bound"]:
        raise ValueError("class count disagrees with its lower bound")
    if result["analysis_limits"] and complete:
        raise ValueError("limited class inventory cannot be complete")
    for candidate in result.get("candidates", []):
        if set(candidate) != {"symbol_id", "qualified_name"} or not all(candidate.values()):
            raise ValueError("invalid class disambiguation candidate")
    if len(result.get("candidates", [])) > 5000:
        raise ValueError("class candidates exceed output bounds")


def _section(result):
    data, section = result["data"], result["section"]
    if data is None:
        if result["export_complete"]:
            raise ValueError("an absent section cannot be a complete export")
        return
    if section in {"compile_result", "include_tree", "filelist_tree"}:
        if not isinstance(data, dict) or result["facts_origin"] != "compile_metadata":
            raise ValueError("compile sections must contain captured metadata")
    elif section == "component_tree_full":
        if _tree([data]) - 1 > result["count_lower_bound"]:
            raise ValueError("descendant count omits rendered scopes")
        _counts(result, "total_descendants")
    elif section == "class_hierarchy_full":
        _classes(result, data)
    else:
        if not isinstance(data, list) or len(data) > 5000:
            raise ValueError("file/interface exports must be bounded lists")
        model = schemas.SourceFileRow if section == "files_full" else schemas.SourceSymbolRow
        seen = set()
        for row in data:
            check_cancelled()
            fact = model.model_validate(row)
            identity = fact.file_id if section == "files_full" else fact.symbol_id
            if identity in seen or (section == "interfaces" and fact.kind != "interface"):
                raise ValueError("invalid or duplicate section member")
            seen.add(identity)
        if len(data) > result["count_lower_bound"]:
            raise ValueError("section count omits rendered facts")
        _counts(result, "total")


def validate_source_result(operation, result):
    """Return a validated copy; preserve explicit nulls and missing fields."""
    models = {
        "get_tb_subtree": schemas.SourceSubtreeResult,
        "find_tb_instance": schemas.SourceInstanceResult,
        "lookup_tb_files": schemas.SourceFileLookupResult,
        "get_tb_file_detail": schemas.SourceFileDetailResult,
        "get_tb_class_hierarchy": schemas.SourceClassResult,
        "dump_tb_section": schemas.SourceSectionResult,
        "resolve_packed_fields": schemas.SourcePackedLayoutResult,
    }
    check_cancelled()
    if operation not in models or not isinstance(result, dict):
        raise ValueError("unsupported source query result")
    if type(result.get("schema_version")) is not int or result["schema_version"] != 2:
        raise ValueError("an explicit version 2 source result is required")
    model = models[operation]
    if result.get("error") in {"hierarchy_query_incomplete", "semantic_provider_unavailable"}:
        model = schemas.SourceQueryEnvelope
    elif operation == "get_tb_file_detail" and result.get("error") in {
        "file_not_in_compile_set", "file_query_incomplete",
    }:
        model = schemas.SourceFileMissResult
    model.model_validate(result)
    # model_dump also recursively visits Any-valued trees and imposes its own
    # depth guard. Copy the JSON-native payload directly after shape checking;
    # this preserves explicit nulls/absence without inserting model defaults.
    payload = json.loads(json.dumps(result, ensure_ascii=True, allow_nan=False))
    if model in {schemas.SourceQueryEnvelope, schemas.SourceFileMissResult}:
        return payload
    if operation == "get_tb_subtree":
        if payload["node"] is not None:
            if payload["node"].get("path") != payload["root"]:
                raise ValueError("subtree node does not match its root")
            if _tree([payload["node"]]) - 1 > payload["count_lower_bound"]:
                raise ValueError("descendant count omits rendered scopes")
        _counts(payload, "total_descendants")
    elif operation == "find_tb_instance":
        hits = payload["hits"]
        if (len({row["scope_id"] for row in hits}) != len(hits)
                or len({row["path"] for row in hits}) != len(hits)):
            raise ValueError("duplicate instance hit")
        if len(hits) > 5000 or len(hits) > payload["count_lower_bound"]:
            raise ValueError("instance count omits rendered hits")
        _counts(payload, "total")
    elif operation == "lookup_tb_files":
        if len({row["file_id"] for row in payload["matches"]}) != len(payload["matches"]):
            raise ValueError("duplicate file match")
        if len(payload["matches"]) > 5000 or len(payload["matches"]) > payload["count_lower_bound"]:
            raise ValueError("file count omits rendered matches")
        _counts(payload, "total")
    elif operation == "get_tb_class_hierarchy":
        _classes(payload, payload["roots"])
    elif operation == "dump_tb_section":
        _section(payload)
    check_cancelled()
    return payload
