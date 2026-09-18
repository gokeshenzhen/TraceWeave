#!/usr/bin/env python3
"""Isolated license-free structural pass; stdout is a bounded JSON receipt."""
from __future__ import annotations

import importlib.metadata
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.source_graph_worker import (  # noqa: E402
    SemanticFrontendSession, _configure_driver, _diagnostics_payload, _read_rss_kib,
)
from src.structural_semantics import ScanLimits, scan_semantic_session  # noqa: E402


def build_session(payload):
    from pyslang import driver as driver_module
    version = importlib.metadata.version("pyslang")
    if version != payload["frontend_version"]:
        raise ValueError("frontend_version_mismatch")
    driver = _configure_driver(driver_module, payload["frontend_args"])
    if not driver.parseAllSources():
        raise ValueError("frontend_parse_failed")
    compilation = driver.createCompilation()
    root = compilation.getRoot()
    return SemanticFrontendSession(driver, compilation, root,
                                   _diagnostics_payload(driver, list(compilation.getAllDiagnostics())), version)


def execute(payload):
    started = time.perf_counter()
    session = build_session(payload)
    frontend_ms = (time.perf_counter() - started) * 1000
    result = scan_semantic_session(session, scope=payload.get("scope"),
                                   categories=payload["categories"], limits=ScanLimits(**payload["limits"]))
    result["metrics"] = {"frontend_ms": frontend_ms, "total_ms": (time.perf_counter()-started)*1000,
                         "frontend_build_count": 1, "peak_rss_kib": _read_rss_kib()[1]}
    return result


if __name__ == "__main__":
    try:
        result = execute(json.load(sys.stdin))
    except ImportError:
        result = {"status": "unavailable", "gaps": ["frontend_unavailable"]}
    except Exception as exc:
        result = {"status": "unavailable", "gaps": ["frontend_build_failed"], "error_type": type(exc).__name__}
    print(json.dumps(result, separators=(",", ":")))
