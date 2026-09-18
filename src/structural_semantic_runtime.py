"""Budgeted semantic scans, with no cold frontend work in auto mode."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import asdict
import json
from pathlib import Path
import time

from .design_identity import _digest
from .hdl_suffixes import FRONTEND_HDL_SUFFIXES
from .source_graph_adapter import _build_compile_manifest
from .source_graph_runtime import _terminate_process_group
from .source_graph_session_runtime import _process_rss_kib
from .structural_semantics import SEMANTIC_CATEGORIES, SEMANTIC_RULE_VERSION, ScanLimits


def validate_semantic_options(args):
    mode = args.get("analysis_mode", "auto")
    if mode not in {"fast", "auto", "deep"}:
        raise ValueError("analysis_mode must be fast, auto, or deep")
    categories = args.get("semantic_categories", list(SEMANTIC_CATEGORIES))
    if not isinstance(categories, list) or not categories or any(c not in SEMANTIC_CATEGORIES for c in categories):
        raise ValueError("unsupported semantic_categories")
    scope = args.get("semantic_scope")
    if scope is not None and (not isinstance(scope, str) or not scope or "*" in scope or any(not p for p in scope.split("."))):
        raise ValueError("semantic_scope must be an exact instance path")
    timeout = args.get("semantic_timeout_sec", 15)
    rss_mib = args.get("semantic_max_rss_mib", 512)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 120:
        raise ValueError("semantic_timeout_sec must be in (0, 120]")
    if isinstance(rss_mib, bool) or not isinstance(rss_mib, int) or not 64 <= rss_mib <= 4096:
        raise ValueError("semantic_max_rss_mib must be in [64, 4096]")
    limits = asdict(ScanLimits())
    for key, maximum in (("max_instances", 100_000), ("max_facts", 50_000), ("max_ast_nodes", 5_000_000)):
        value = args.get("semantic_" + key, limits[key])
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise ValueError(f"semantic_{key} must be in [1, {maximum}]")
        limits[key] = value
    return mode, {"scope": scope, "categories": sorted(set(categories)),
                  "limits": limits, "timeout_sec": timeout, "max_rss_mib": rss_mib}


def build_semantic_payload(compile_log, compile_result, options, frontend_version):
    # Never consume the query adapter's log-only memoization without a hierarchy
    # snapshot. This is a new capture for a standalone scan, independent of the
    # concurrently running hierarchy tool.
    manifest, gaps, exclusions = _build_compile_manifest(compile_log, compile_result)
    argv = [*manifest.ordered_options, *(p for p in manifest.ordered_inputs if Path(p).suffix.lower() in FRONTEND_HDL_SUFFIXES)]
    for top in manifest.ordered_tops:
        argv += ["--top", top]
    return {**options, "frontend_args": argv, "frontend_version": frontend_version,
            "manifest": manifest.to_dict(), "compile_gaps": list(gaps), "exclusions": list(exclusions)}


async def run_isolated_scan(payload, python_bin):
    process = None
    communicate = None
    try:
        process = await asyncio.create_subprocess_exec(
            python_bin, str(Path(__file__).with_name("structural_semantic_worker.py")),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        communicate = asyncio.create_task(process.communicate(json.dumps(payload).encode()))
        deadline = time.monotonic() + payload["timeout_sec"]
        while not communicate.done():
            await asyncio.wait({communicate}, timeout=min(.05, max(0, deadline-time.monotonic())))
            rss = _process_rss_kib(process.pid)
            if rss is not None and rss > payload["max_rss_mib"] * 1024:
                return {"status": "partial", "gaps": ["worker_rss_limit"]}
            if time.monotonic() >= deadline and not communicate.done():
                return {"status": "partial", "gaps": ["worker_timeout"]}
        output, _ = communicate.result()
        if process.returncode or len(output) > 32 * 1024 * 1024:
            return {"status": "unavailable", "gaps": ["worker_failed_or_output_limit"]}
        result = json.loads(output)
        if result.get("metrics", {}).get("peak_rss_kib", 0) > payload["max_rss_mib"] * 1024:
            return {"status": "partial", "gaps": ["worker_rss_limit"]}
        return result
    except (OSError, ValueError):
        return {"status": "unavailable", "gaps": ["worker_unavailable"]}
    finally:
        if process is not None:
            await _terminate_process_group(process)
        if communicate is not None:
            if not communicate.done():
                communicate.cancel()
            await asyncio.gather(communicate, return_exceptions=True)


class SemanticScanRuntime:
    def __init__(self):
        self._cache = OrderedDict()
        self._bytes = 0
        self._admission = asyncio.Lock()

    async def scan(self, *, identity, mode, options, compile_log, compile_result, config, run_thread):
        if mode == "fast":
            return {"status": "not_run", "gaps": ["fast_mode"]}
        key = _digest({"identity": identity.digest, "options": options,
                       "frontend": config.frontend_version, "python": config.python_bin, "rules": SEMANTIC_RULE_VERSION})
        reusable = identity.complete and identity.include_resolution_complete
        if reusable and key in self._cache and await run_thread(identity.current):
            self._cache.move_to_end(key)
            result = json.loads(self._cache[key])
            result["cache_disposition"] = "hit_memory"
            result.setdefault("metrics", {})["frontend_build_count"] = 0
            return result
        if mode == "auto":
            return {"status": "not_run", "gaps": ["semantic_cache_miss"], "cache_disposition": "miss"}
        if not config.valid or not config.enabled:
            return {"status": "unavailable", "gaps": ["semantic_frontend_disabled_or_invalid"]}
        async with self._admission:
            if reusable and key in self._cache and await run_thread(identity.current):
                result = json.loads(self._cache[key])
                result["cache_disposition"] = "hit_after_admission"
                result.setdefault("metrics", {})["frontend_build_count"] = 0
                return result
            payload = await run_thread(lambda: build_semantic_payload(compile_log, compile_result, options, config.frontend_version))
            if not payload["manifest"]["ordered_tops"] or not payload["manifest"]["ordered_inputs"]:
                return {"status": "unavailable", "gaps": ["compile_inputs_or_top_missing"]}
            result = await run_isolated_scan(payload, config.python_bin)
            current = await run_thread(identity.current)
            if not current:
                result["status"] = "partial"
                result.setdefault("gaps", []).append("compile_identity_incomplete_or_changed")
            if not identity.include_resolution_complete:
                if result["status"] == "complete":
                    result["status"] = "partial"
                result.setdefault("gaps", []).append("semantic_include_identity_incomplete")
            if payload["compile_gaps"] or payload["exclusions"]:
                result["status"] = "partial" if result["status"] == "complete" else result["status"]
                result["gaps"] = sorted(set(result.get("gaps", []) + payload["compile_gaps"] + payload["exclusions"]))
            result["cache_disposition"] = "miss"
            encoded = json.dumps(result, separators=(",", ":")).encode()
            if reusable and current and result["status"] == "complete" and len(encoded) <= 32 * 1024 * 1024:
                old = self._cache.pop(key, None)
                self._bytes -= len(old) if old is not None else 0
                self._cache[key] = encoded
                self._bytes += len(encoded)
                while self._bytes > 32 * 1024 * 1024 or len(self._cache) > 4:
                    self._bytes -= len(self._cache.popitem(last=False)[1])
            return result
