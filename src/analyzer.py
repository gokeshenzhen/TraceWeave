"""
analyzer.py
聚焦单个报错分组与 failure_event 的联合分析器。
"""

from __future__ import annotations

import os
import re
from typing import Any

from config import (
    DEFAULT_EXTRA_TRANSITIONS,
    DEFAULT_LOG_CONTEXT_AFTER,
    DEFAULT_LOG_CONTEXT_BEFORE,
    DEFAULT_WAVE_WINDOW_PS,
)
from .compile_log_parser import parse_compile_log
from .log_parser import SimLogParser
from .problem_hints import problem_hints_from_event
from .schemas import ProblemHints
from .tb_hierarchy_builder import build_hierarchy


_STOPWORDS = {
    "error", "fatal", "uvm", "assertion", "failed", "failure", "reporter", "timeout",
    "expected", "got", "compare", "mismatch", "top", "tb", "module",
}
_HELPER_TOKENS = ("assert", "checker", "scoreboard", "monitor", "agent", "uvm", "reporter", "sva")
_DUT_TOKENS = ("dut", "core", "u_", "d_", "rtl", "design")
_ROLE_KEYWORDS = {
    "control": ("start", "stop", "enable", "sel", "mode", "ctrl", "control"),
    "state": ("state", "fsm", "phase"),
    "counter": ("cnt", "count", "round", "step", "idx", "index"),
    "input_reg": ("in_reg", "input_reg", "din_reg", "src_reg"),
    "output": ("out", "data_o", "resp", "result"),
    "output_reg": ("out_reg", "output_reg", "data_reg", "result_reg", "desin_reg"),
    "status": ("status", "busy", "done", "err", "error", "fail"),
    "handshake": ("valid", "ready", "req", "ack"),
}


class WaveformAnalyzer:
    def __init__(self, log_path: str, parser, simulator: str):
        self.log_path = log_path
        self.parser = parser
        self.simulator = simulator

    def analyze(
        self,
        signal_paths: list[str],
        group_index: int = 0,
        window_ps: int = DEFAULT_WAVE_WINDOW_PS,
        extra_transitions: int = DEFAULT_EXTRA_TRANSITIONS,
        log_before: int = DEFAULT_LOG_CONTEXT_BEFORE,
        log_after: int = DEFAULT_LOG_CONTEXT_AFTER,
    ) -> dict:
        log_parser = SimLogParser(self.log_path, self.simulator)
        log_result = log_parser.parse()
        groups = log_result.get("groups", [])
        events = log_parser.parse_failure_events()

        if not groups:
            return {
                "summary": log_result,
                "focused_group": None,
                "focused_event": None,
                "log_context": None,
                "wave_context": None,
                "remaining_groups": 0,
                "analysis_guide": {
                    "step1": "仿真 log 中未发现 ERROR 或 FATAL",
                },
                "problem_hints": problem_hints_from_event(None, None),
            }

        if group_index < 0 or group_index >= len(groups):
            raise IndexError(f"group_index {group_index} 超出范围，当前 groups={len(groups)}")

        focused_group = dict(groups[group_index])
        focused_event = _find_group_event(events, focused_group["sample_event_id"])
        first_time_ps = focused_group["first_time_ps"]

        log_context = log_parser.get_error_context(
            line=focused_group["first_line"],
            before=log_before,
            after=log_after,
        )
        wave_context = None
        if signal_paths and first_time_ps > 0:
            wave_context = self.parser.get_signals_around_time(
                signal_paths,
                first_time_ps,
                window_ps,
                extra_transitions,
            )

        return {
            "summary": log_result,
            "focused_group": focused_group,
            "focused_event": focused_event,
            "log_context": log_context,
            "wave_context": wave_context,
            "remaining_groups": len(groups) - group_index - 1,
            "signals_queried": signal_paths,
            "extra_transitions": extra_transitions,
            "analysis_guide": {
                "step1": "先看 focused_group 是否是最早出现且更接近 DUT 的报错类型",
                "step2": "结合 focused_event 的 source_file / instance_path 确定失败锚点",
                "step3": "在 wave_context 中核对信号中心值、窗口内跳变和窗口前历史",
                "step4": "若信号不够，再调用 recommend_failure_debug_next_steps 或 analyze_failure_event",
            },
            "problem_hints": problem_hints_from_event(focused_event, first_time_ps),
        }

    def analyze_failure_event(
        self,
        failure_event: dict[str, Any],
        wave_path: str,
        compile_log: str | None = None,
        top_hint: str | None = None,
    ) -> dict[str, Any]:
        hierarchy = _load_hierarchy(compile_log) if compile_log else None
        likely_instances = _rank_likely_instances(failure_event, hierarchy, top_hint)
        related_source_files = _rank_related_source_files(failure_event, hierarchy, likely_instances)
        recommended_signals = _recommend_signals(
            self.parser,
            failure_event,
            likely_instances,
            top_hint,
        )

        time_anchor = {
            "time_ps": failure_event.get("time_ps") or None,
            "kind": "exact" if failure_event.get("time_ps") is not None else "log_only",
            "log_line": failure_event.get("line"),
            "wave_path": wave_path,
        }

        reasoning = []
        if failure_event.get("instance_path"):
            reasoning.append(f"Failure instance hint came from log path {failure_event['instance_path']}.")
        if failure_event.get("source_file"):
            reasoning.append(f"Source correlation used {os.path.basename(failure_event['source_file'])}.")
        if recommended_signals:
            reasoning.append("Signal suggestions were ranked to prefer DUT-visible paths over checker internals.")

        return {
            "failure_event": failure_event,
            "time_anchor": time_anchor,
            "likely_instances": likely_instances,
            "recommended_signals": recommended_signals,
            "related_source_files": related_source_files,
            "reasoning_summary": reasoning,
        }

    def recommend_debug_next_steps(
        self,
        wave_path: str,
        compile_log: str | None = None,
        top_hint: str | None = None,
        structural_risks: list[dict[str, Any]] | None = None,
        problem_hints: dict[str, Any] | ProblemHints | None = None,
    ) -> dict[str, Any]:
        log_parser = SimLogParser(self.log_path, self.simulator)
        events = log_parser.parse_failure_events()
        if not events:
            return {
                "primary_failure_target": None,
                "recommended_signals": [],
                "recommended_instances": [],
                "correlated_structural_risks": [],
                "suspected_failure_class": "no_failure_detected",
                "why": ["仿真 log 中未发现可归一化的失败事件"],
            }

        ranked_events = sorted(events, key=_failure_priority_key)
        primary = ranked_events[0]
        event_analysis = self.analyze_failure_event(primary, wave_path, compile_log=compile_log, top_hint=top_hint)
        failure_class = _classify_failure(primary)
        normalized_hints = _normalize_problem_hints(problem_hints)
        correlated_risks = _rank_structural_risks(structural_risks or [], primary, normalized_hints)
        why = [
            "Selected the earliest failure with the strongest available timing/source anchor.",
            f"Failure classified heuristically as {failure_class}.",
        ]
        if event_analysis["likely_instances"]:
            why.append("Hierarchy ranking preferred DUT-facing instances over helper/checker nodes.")
        if correlated_risks:
            why.append("Structural scan risks were re-ranked against the primary failure instance and current symptom hints.")

        return {
            "primary_failure_target": primary,
            "recommended_signals": event_analysis["recommended_signals"],
            "recommended_instances": event_analysis["likely_instances"],
            "correlated_structural_risks": correlated_risks,
            "suspected_failure_class": failure_class,
            "recommendation_strategy": "role_rank_v2_structural",
            "failure_window_center_ps": primary.get("time_ps"),
            "why": why,
        }


def _find_group_event(events: list[dict[str, Any]], event_id: str | None) -> dict[str, Any] | None:
    for event in events:
        if event["event_id"] == event_id:
            return event
    return None
def _load_hierarchy(compile_log: str) -> dict[str, Any]:
    return build_hierarchy(parse_compile_log(compile_log, "auto"))


def _failure_priority_key(event: dict[str, Any]) -> tuple[int, int, int]:
    severity_score = 0 if event.get("severity") == "FATAL" else 1
    time_score = event.get("time_ps") if event.get("time_ps") is not None else 10**18
    return severity_score, time_score, event.get("line") or 10**18


def _classify_failure(event: dict[str, Any]) -> str:
    text = " ".join(
        [
            event.get("group_signature") or "",
            event.get("message_text") or "",
            event.get("instance_path") or "",
        ]
    ).lower()
    if any(token in text for token in ("assert", "protocol", "handshake", "ready", "valid")):
        return "assertion/protocol issue"
    if any(token in text for token in ("latency", "timeout", "cycle", "delay")):
        return "timing/latency"
    if any(token in text for token in ("expected", "got", "compare", "mismatch", "data")):
        return "data-path corruption"
    if any(token in text for token in ("scoreboard", "checker", "uvm_test_top", "monitor")):
        return "checker/testbench issue"
    return "control/handshake"


def _flatten_component_tree(tree: dict[str, Any], prefix: str = "") -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for name, payload in tree.items():
        instance_path = f"{prefix}.{name}" if prefix else name
        node = dict(payload)
        node["instance_path"] = instance_path
        nodes.append(node)
        children = payload.get("children", {})
        if children:
            nodes.extend(_flatten_component_tree(children, instance_path))
    return nodes


def _rank_likely_instances(
    failure_event: dict[str, Any],
    hierarchy: dict[str, Any] | None,
    top_hint: str | None,
) -> list[dict[str, Any]]:
    if hierarchy is None:
        if failure_event.get("instance_path"):
            return [{"instance_path": failure_event["instance_path"], "score": 10, "reason": "exact log instance"}]
        return []

    nodes = _flatten_component_tree(hierarchy.get("component_tree", {}))
    event_path = failure_event.get("instance_path") or ""
    top_module = top_hint or hierarchy.get("project", {}).get("top_module") or ""
    ranked = []
    for node in nodes:
        path = node["instance_path"]
        score = 0
        reasons = []
        if event_path and (path.endswith(event_path) or event_path.endswith(path)):
            score += 8
            reasons.append("path overlap with failure event")
        if top_module and path.startswith(top_module):
            score += 2
            reasons.append("under top module")
        role = _classify_path(path)
        if role == "dut":
            score += 3
            reasons.append("DUT-facing instance")
        elif role == "helper":
            score -= 2
        src = node.get("src") or ""
        if failure_event.get("source_file") and os.path.basename(src) == os.path.basename(failure_event["source_file"]):
            score += 3
            reasons.append("same source file basename")
        if score > 0:
            ranked.append(
                {
                    "instance_path": path,
                    "class": node.get("class"),
                    "src": src,
                    "score": score,
                    "reason": ", ".join(reasons),
                }
            )
    ranked.sort(key=lambda item: (-item["score"], item["instance_path"]))
    return ranked[:6]


def _rank_related_source_files(
    failure_event: dict[str, Any],
    hierarchy: dict[str, Any] | None,
    likely_instances: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    source_files: dict[str, dict[str, Any]] = {}
    if hierarchy:
        for items in hierarchy.get("files", {}).values():
            for item in items:
                source_files[item["path"]] = {"path": item["path"], "score": 0, "reason": []}
    if failure_event.get("source_file"):
        source_files.setdefault(
            failure_event["source_file"],
            {"path": failure_event["source_file"], "score": 0, "reason": []},
        )
        source_files[failure_event["source_file"]]["score"] += 8
        source_files[failure_event["source_file"]]["reason"].append("exact failure source")
    for instance in likely_instances:
        src = instance.get("src")
        if not src:
            continue
        source_files.setdefault(src, {"path": src, "score": 0, "reason": []})
        source_files[src]["score"] += max(1, instance["score"] // 2)
        source_files[src]["reason"].append(f"linked from {instance['instance_path']}")
    ranked = [
        {"path": path, "score": info["score"], "reason": ", ".join(info["reason"])}
        for path, info in source_files.items()
        if info["score"] > 0
    ]
    ranked.sort(key=lambda item: (-item["score"], item["path"]))
    return ranked[:6]


def _recommend_signals(
    parser,
    failure_event: dict[str, Any],
    likely_instances: list[dict[str, Any]],
    top_hint: str | None,
) -> list[dict[str, Any]]:
    keywords = _extract_keywords(failure_event)
    for instance in likely_instances[:3]:
        keywords.extend(part for part in instance["instance_path"].split(".") if part not in keywords)
    dedup_keywords = []
    seen = set()
    ordered_keywords = [top_hint, "dut", "req", "data", "valid", "ready"] + keywords
    dedup_keywords = []
    seen = set()
    for keyword in ordered_keywords:
        if not isinstance(keyword, str):
            continue
        keyword = keyword.strip()
        if not keyword or keyword in seen or len(keyword) < 2:
            continue
        seen.add(keyword)
        dedup_keywords.append(keyword)

    suggestions: dict[str, dict[str, Any]] = {}
    for keyword in dedup_keywords[:8]:
        try:
            result = parser.search_signals(keyword, 10)
        except Exception:
            continue
        for item in result.get("results", []):
            score_info = _score_signal_candidate(
                parser=parser,
                candidate=item,
                keyword=keyword,
                failure_event=failure_event,
                likely_instances=likely_instances,
                top_hint=top_hint,
            )
            existing = suggestions.get(item["path"])
            if existing is None or score_info["score"] > existing["score"]:
                suggestions[item["path"]] = {
                    "path": item["path"],
                    "name": item["name"],
                    "width": item.get("width", 0),
                    "score": score_info["score"],
                    "role": score_info["role"],
                    "reason_codes": score_info["reason_codes"],
                    "confidence": score_info["confidence"],
                }
    ranked = sorted(suggestions.values(), key=lambda item: (-item["score"], item["path"]))
    return ranked[:8]


def _extract_keywords(failure_event: dict[str, Any]) -> list[str]:
    keywords: list[str] = []
    instance_path = failure_event.get("instance_path") or ""
    keywords.extend(part for part in instance_path.split(".") if part)
    if failure_event.get("source_file"):
        keywords.append(os.path.splitext(os.path.basename(failure_event["source_file"]))[0])
    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", failure_event.get("message_text") or ""):
        lower = token.lower()
        if lower not in _STOPWORDS:
            keywords.append(token)
    for value in (failure_event.get("structured_fields") or {}).values():
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", value):
            keywords.extend(part for part in value.split(".") if part)
    return keywords


def _score_signal_candidate(
    parser,
    candidate: dict[str, Any],
    keyword: str,
    failure_event: dict[str, Any],
    likely_instances: list[dict[str, Any]],
    top_hint: str | None,
) -> dict[str, Any]:
    path = candidate["path"]
    name = candidate["name"]
    width = candidate.get("width", 0) or 0
    lower = path.lower()
    keyword_lower = keyword.lower()
    score = 0
    reason_codes: list[str] = []

    role = _infer_signal_role(name, path)
    if role != "status":
        score += 1
    if role != "control":
        pass
    if role != "unknown":
        score += 5
        reason_codes.append(f"role_{role}")

    if name.lower() == keyword_lower:
        score += 6
        reason_codes.append("exact_name_match")
    elif lower.endswith(f".{keyword_lower}"):
        score += 4
        reason_codes.append("suffix_name_match")
    elif keyword_lower in lower:
        score += 2
        reason_codes.append("keyword_match")

    instance_score, instance_reason = _score_instance_proximity(path, failure_event, likely_instances, top_hint)
    score += instance_score
    if instance_reason:
        reason_codes.append(instance_reason)

    activity_score, activity_reason = _score_activity_near_failure(parser, path, failure_event.get("time_ps"))
    score += activity_score
    if activity_reason:
        reason_codes.append(activity_reason)

    width_score, width_reason = _score_width_and_shape(name, width, role)
    score += width_score
    if width_reason:
        reason_codes.append(width_reason)

    path_role = _classify_path(path)
    if path_role == "dut":
        score += 3
        reason_codes.append("dut_facing_path")
    elif path_role == "helper":
        score -= 6
        reason_codes.append("helper_path_penalty")

    confidence = "heuristic" if score >= 10 else "low"
    return {
        "score": score,
        "role": role if role != "unknown" else "status",
        "reason_codes": reason_codes,
        "confidence": confidence,
    }


def _infer_signal_role(name: str, path: str) -> str:
    haystack = f"{path}.{name}".lower()
    for role, keywords in _ROLE_KEYWORDS.items():
        if any(keyword in haystack for keyword in keywords):
            return role
    return "unknown"


def _score_instance_proximity(
    path: str,
    failure_event: dict[str, Any],
    likely_instances: list[dict[str, Any]],
    top_hint: str | None,
) -> tuple[int, str | None]:
    lower = path.lower()
    event_instance = (failure_event.get("instance_path") or "").lower()
    if event_instance and (lower.startswith(event_instance + ".") or event_instance.startswith(lower.rsplit(".", 1)[0])):
        return 6, "same_instance"
    for instance in likely_instances[:3]:
        instance_path = instance["instance_path"].lower()
        if lower.startswith(instance_path + "."):
            return 5, "same_instance"
    if top_hint and lower.startswith(top_hint.lower()):
        return 2, "under_top_hint"
    return 0, None


def _score_activity_near_failure(parser, signal_path: str, failure_time_ps: int | None) -> tuple[int, str | None]:
    if failure_time_ps is None or not hasattr(parser, "get_signals_around_time"):
        return 0, None
    try:
        context = parser.get_signals_around_time([signal_path], failure_time_ps, DEFAULT_WAVE_WINDOW_PS, 2)
    except Exception:
        return 0, None
    signal_info = (context.get("signals") or {}).get(signal_path) or {}
    transitions = signal_info.get("transitions_in_window") or []
    if transitions:
        return 4, "active_near_failure"
    pre_transitions = signal_info.get("pre_window_transitions") or []
    if pre_transitions:
        return 1, "history_before_failure"
    return 0, None


def _score_width_and_shape(name: str, width: int, role: str) -> tuple[int, str | None]:
    lower = name.lower()
    if lower.endswith("_reg"):
        if role in {"input_reg", "output_reg"}:
            return 3, "registered_datapath"
        return 1, "registered_signal"
    if role in {"counter", "state"} and width > 1:
        return 2, "multi_bit_debug_signal"
    if role == "handshake" and width == 1:
        return 2, "single_bit_handshake"
    return 0, None


def _classify_path(path: str) -> str:
    lower = path.lower()
    if any(token in lower for token in _HELPER_TOKENS):
        return "helper"
    if any(token in lower for token in _DUT_TOKENS):
        return "dut"
    return "neutral"


def _normalize_problem_hints(problem_hints: dict[str, Any] | ProblemHints | None) -> ProblemHints | None:
    if problem_hints is None:
        return None
    if isinstance(problem_hints, ProblemHints):
        return problem_hints
    try:
        return ProblemHints.model_validate(problem_hints)
    except Exception:
        return None


def _rank_structural_risks(
    risks: list[dict[str, Any]],
    primary_event: dict[str, Any],
    problem_hints: ProblemHints | None,
) -> list[dict[str, Any]]:
    if not risks:
        return []

    instance_path = (primary_event.get("instance_path") or "").lower()
    ranked: list[dict[str, Any]] = []
    for risk in risks:
        score = 0
        reasons: list[str] = []
        module = risk.get("module")
        if isinstance(module, str) and module and module.lower() in instance_path:
            score += 10
            reasons.append("module appears in failure instance path")
        type_reason = _risk_type_hint_reason(risk.get("type"), problem_hints)
        if type_reason is not None:
            score += 5
            reasons.append(type_reason)
        if risk.get("risk_level") == "high":
            score += 2
            reasons.append("risk_level is high")
        if score <= 0:
            continue
        ranked.append(
            {
                "risk_type": risk.get("type"),
                "file": risk.get("file"),
                "line": risk.get("line"),
                "module": module,
                "risk_level": risk.get("risk_level"),
                "detail": risk.get("detail"),
                "relevance_score": score,
                "relevance_reasons": reasons,
            }
        )
    ranked.sort(key=lambda item: (-item["relevance_score"], item["file"], item["line"]))
    return ranked[:5]


def _risk_type_hint_reason(risk_type: Any, problem_hints: ProblemHints | None) -> str | None:
    if problem_hints is None or not isinstance(risk_type, str):
        return None
    if risk_type == "slice_overlap" and (problem_hints.has_x or problem_hints.has_z):
        return "slice_overlap correlates with has_x/has_z"
    if risk_type == "narrow_condition_injection" and problem_hints.error_pattern == "mismatch":
        return "narrow_condition_injection correlates with mismatch"
    if risk_type == "multi_drive" and problem_hints.has_x:
        return "multi_drive correlates with has_x"
    return None
