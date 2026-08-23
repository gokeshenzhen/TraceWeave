from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
import subprocess
import sys

import pytest

from src.slang_connectivity_projector import (
    SLANG_FRONTEND_NAME,
    SLANG_FRONTEND_VERSION,
)
from src.source_graph_contract import SourceGraphSemanticContext
from src.source_graph_runtime import (
    IsolatedSourceGraphProcessRunner,
    PrepareStatus,
    WorkerBuildResult,
)
from src.source_graph_session_runtime import PersistentSourceGraphProcessRunner
from tests.test_source_graph_runtime import _request


ROOT = Path(__file__).resolve().parents[1]
FRONTEND_PYTHON = ROOT / ".venv/bin/python"


def _context_request(label: str = "session"):
    request = _request(label=label)
    artifact = request.artifact_identity
    return replace(
        request,
        artifact=replace(
            artifact,
            semantic_context=SourceGraphSemanticContext(scope=artifact.scope),
        ),
    )


def _slang_context_request(label: str = "session"):
    request = _context_request(label)
    source = replace(
        request.identity,
        frontend_name=SLANG_FRONTEND_NAME,
        frontend_version=SLANG_FRONTEND_VERSION,
    )
    return replace(
        request,
        identity=source,
        artifact=replace(request.artifact_identity, source=source),
    )


def _fake_worker(
    tmp_path: Path,
    *,
    delay_seconds: float = 0.0,
    reported_rss_peak_kib: int | None = None,
    valid_acknowledgement: bool = True,
) -> Path:
    script = tmp_path / "fake_session_worker.py"
    script.write_text(
        f"""
import argparse
import base64
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, {str(ROOT)!r})
from src.source_graph_contract import (
    SOURCE_GRAPH_WORKER_PROTOCOL_VERSION,
    SourceGraphArtifactBuildRequest,
    SourceGraphArtifactScopeReceipt,
)
from tests.connectivity_ir_fixtures import build_hand_ir

parser = argparse.ArgumentParser()
parser.add_argument('--directory', type=Path, required=True)
args = parser.parse_args()
for line in sys.stdin:
    command = json.loads(line)
    token = command['token']
    request_path = args.directory / f'{{token}}.request.json'
    response_path = args.directory / f'{{token}}.response.json'
    envelope = json.loads(request_path.read_text())
    request = SourceGraphArtifactBuildRequest.from_dict(envelope['request'])
    time.sleep({delay_seconds!r})
    ir = build_hand_ir()
    payload = {{
        'protocol_version': SOURCE_GRAPH_WORKER_PROTOCOL_VERSION,
        'status': 'ready',
        'ir_json_base64': base64.b64encode(ir.to_json_bytes()).decode('ascii'),
        'ir_fingerprint_sha256': ir.fingerprint_sha256(),
        'scope_receipt': SourceGraphArtifactScopeReceipt(
            scope=request.scope,
            coverage_status=ir.coverage.status,
        ).to_dict(),
        'metrics': {{
            'wall_time_ms': 1.0,
            'ir_bytes': len(ir.to_json_bytes()),
            'rss_peak_kib': {reported_rss_peak_kib!r},
        }},
        'fallback_used': False,
    }}
    temporary = response_path.with_suffix('.tmp')
    temporary.write_text(json.dumps(payload))
    os.replace(temporary, response_path)
    print(token if {valid_acknowledgement!r} else 'invalid', flush=True)
""",
        encoding="utf-8",
    )
    return script


def _crashing_worker(tmp_path: Path) -> Path:
    script = tmp_path / "crashing_session_worker.py"
    script.write_text(
        """
import sys

for unused_line in sys.stdin:
    raise SystemExit(7)
""",
        encoding="utf-8",
    )
    return script


def _runner(tmp_path: Path, script: Path, *, ttl: float = 1.0):
    staging = tmp_path / "staging"
    staging.mkdir()
    return PersistentSourceGraphProcessRunner(
        python_executable=sys.executable,
        worker_script=script,
        working_directory=ROOT,
        staging_directory=staging,
        idle_ttl_seconds=ttl,
        max_rss_kib=64 * 1024,
    ), staging


@pytest.mark.anyio
async def test_persistent_runner_reuses_one_exact_context_and_cleans_up(tmp_path):
    runner, staging = _runner(tmp_path, _fake_worker(tmp_path))
    request = _context_request()

    first = await runner.run(
        request,
        timeout_seconds=2.0,
        cancel_event=asyncio.Event(),
    )
    second = await runner.run(
        request,
        timeout_seconds=2.0,
        cancel_event=asyncio.Event(),
    )

    assert first.status is PrepareStatus.READY
    assert second.status is PrepareStatus.READY
    assert first.ir_fingerprint_sha256 == second.ir_fingerprint_sha256
    assert first.metrics.frontend_launch_count == 1
    assert first.metrics.semantic_session_miss_count == 1
    assert second.metrics.frontend_launch_count == 0
    assert second.metrics.semantic_session_hit_count == 1
    assert runner.active is True
    await runner.close()
    assert list(staging.iterdir()) == []


@pytest.mark.anyio
async def test_persistent_runner_restarts_on_exact_context_change(tmp_path):
    runner, _ = _runner(tmp_path, _fake_worker(tmp_path))

    first = await runner.run(
        _context_request("first"),
        timeout_seconds=2.0,
        cancel_event=asyncio.Event(),
    )
    changed = await runner.run(
        _context_request("changed"),
        timeout_seconds=2.0,
        cancel_event=asyncio.Event(),
    )

    assert first.status is PrepareStatus.READY
    assert changed.status is PrepareStatus.READY
    assert changed.metrics.frontend_launch_count == 1
    assert changed.metrics.semantic_session_miss_count == 1
    assert changed.metrics.semantic_session_restart_count == 1
    await runner.close()


@pytest.mark.anyio
async def test_persistent_runner_idle_ttl_evicts_before_next_request(tmp_path):
    runner, _ = _runner(tmp_path, _fake_worker(tmp_path), ttl=0.02)
    request = _context_request()

    first = await runner.run(
        request,
        timeout_seconds=2.0,
        cancel_event=asyncio.Event(),
    )
    await asyncio.sleep(0.08)
    second = await runner.run(
        request,
        timeout_seconds=2.0,
        cancel_event=asyncio.Event(),
    )

    assert first.status is PrepareStatus.READY
    assert runner.active is True
    assert second.metrics.semantic_session_eviction_count == 1
    assert second.metrics.semantic_session_miss_count == 1
    await runner.close()


@pytest.mark.anyio
async def test_persistent_runner_cancellation_kills_whole_session(tmp_path):
    runner, _ = _runner(
        tmp_path,
        _fake_worker(tmp_path, delay_seconds=10.0),
    )
    cancel_event = asyncio.Event()
    task = asyncio.create_task(
        runner.run(
            _context_request(),
            timeout_seconds=2.0,
            cancel_event=cancel_event,
        )
    )
    await asyncio.sleep(0.05)
    cancel_event.set()

    result = await task

    assert result.status is PrepareStatus.CANCELLED
    assert result.metrics.cancel_to_exit_ms is not None
    assert runner.active is False


@pytest.mark.anyio
async def test_persistent_runner_timeout_kills_whole_session(tmp_path):
    runner, _ = _runner(
        tmp_path,
        _fake_worker(tmp_path, delay_seconds=10.0),
    )

    result = await runner.run(
        _context_request(),
        timeout_seconds=0.05,
        cancel_event=asyncio.Event(),
    )

    assert result.status is PrepareStatus.TIMED_OUT
    assert result.blocker.code == "worker_timeout"
    assert runner.active is False


@pytest.mark.anyio
async def test_persistent_runner_crash_kills_whole_session(tmp_path):
    runner, staging = _runner(tmp_path, _crashing_worker(tmp_path))

    result = await runner.run(
        _context_request(),
        timeout_seconds=2.0,
        cancel_event=asyncio.Event(),
    )

    assert result.status is PrepareStatus.WORKER_CRASH
    assert result.blocker.code == "worker_exit_failure"
    assert runner.active is False
    assert list(staging.iterdir()) == []


@pytest.mark.anyio
async def test_persistent_runner_protocol_error_kills_whole_session(tmp_path):
    runner, staging = _runner(
        tmp_path,
        _fake_worker(tmp_path, valid_acknowledgement=False),
    )

    result = await runner.run(
        _context_request(),
        timeout_seconds=2.0,
        cancel_event=asyncio.Event(),
    )

    assert result.status is PrepareStatus.INVALID_RESPONSE
    assert result.blocker.code == "worker_response_invalid"
    assert runner.active is False
    assert list(staging.iterdir()) == []


@pytest.mark.anyio
async def test_persistent_runner_rss_limit_kills_whole_session(
    tmp_path,
    monkeypatch,
):
    async def exceeded(unused_pid, max_rss_kib):
        return max_rss_kib + 1

    monkeypatch.setattr(
        "src.source_graph_session_runtime._wait_for_rss_limit",
        exceeded,
    )
    runner, _ = _runner(tmp_path, _fake_worker(tmp_path))

    result = await runner.run(
        _context_request(),
        timeout_seconds=2.0,
        cancel_event=asyncio.Event(),
    )

    assert result.status is PrepareStatus.BUILD_FAILED
    assert result.blocker.code == "semantic_session_rss_limit"
    assert result.metrics.semantic_session_eviction_count == 1
    assert runner.active is False


@pytest.mark.anyio
async def test_persistent_runner_rejects_reported_transient_rss_peak(tmp_path):
    runner, _ = _runner(
        tmp_path,
        _fake_worker(tmp_path, reported_rss_peak_kib=64 * 1024 + 1),
    )

    result = await runner.run(
        _context_request(),
        timeout_seconds=2.0,
        cancel_event=asyncio.Event(),
    )

    assert result.status is PrepareStatus.BUILD_FAILED
    assert result.blocker.code == "semantic_session_rss_limit"
    assert result.metrics.rss_peak_kib == 64 * 1024 + 1
    assert result.metrics.semantic_session_eviction_count == 1
    assert runner.active is False


class _FallbackRunner:
    def __init__(self):
        self.calls = 0

    async def run(self, request, *, timeout_seconds, cancel_event):
        self.calls += 1
        return WorkerBuildResult.failed(
            PrepareStatus.DEPENDENCY_BLOCKED,
            code="fallback_called",
            stage="worker_process",
        )


@pytest.mark.anyio
async def test_request_without_semantic_context_keeps_one_shot_route(tmp_path):
    fallback = _FallbackRunner()
    runner = PersistentSourceGraphProcessRunner(
        python_executable=sys.executable,
        worker_script=_fake_worker(tmp_path),
        working_directory=ROOT,
        idle_ttl_seconds=1.0,
        max_rss_kib=64 * 1024,
        one_shot_runner=fallback,
    )

    result = await runner.run(
        _request(),
        timeout_seconds=2.0,
        cancel_event=asyncio.Event(),
    )

    assert result.blocker.code == "fallback_called"
    assert fallback.calls == 1
    assert runner.active is False


def _pinned_frontend_available() -> bool:
    if not FRONTEND_PYTHON.is_file():
        return False
    completed = subprocess.run(
        [
            str(FRONTEND_PYTHON),
            "-c",
            (
                "import importlib.metadata; "
                "print(importlib.metadata.version('pyslang'))"
            ),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.returncode == 0 and completed.stdout.strip() == "11.0.0"


@pytest.mark.anyio
async def test_real_persistent_worker_matches_session_aware_one_shot(tmp_path):
    if not _pinned_frontend_available():
        pytest.skip("pinned Source Graph frontend is unavailable")
    request = _slang_context_request()
    staging = tmp_path / "persistent"
    one_shot_staging = tmp_path / "one_shot"
    staging.mkdir()
    one_shot_staging.mkdir()
    runner = PersistentSourceGraphProcessRunner(
        python_executable=FRONTEND_PYTHON,
        working_directory=ROOT,
        staging_directory=staging,
        idle_ttl_seconds=2.0,
        max_rss_kib=768 * 1024,
    )
    one_shot = IsolatedSourceGraphProcessRunner(
        python_executable=FRONTEND_PYTHON,
        working_directory=ROOT,
        staging_directory=one_shot_staging,
    )

    first = await runner.run(
        request,
        timeout_seconds=20.0,
        cancel_event=asyncio.Event(),
    )
    reused = await runner.run(
        request,
        timeout_seconds=20.0,
        cancel_event=asyncio.Event(),
    )
    isolated = await one_shot.run(
        request,
        timeout_seconds=20.0,
        cancel_event=asyncio.Event(),
    )

    assert first.status is PrepareStatus.READY
    assert reused.status is PrepareStatus.READY
    assert isolated.status is PrepareStatus.READY
    assert first.ir_fingerprint_sha256 == reused.ir_fingerprint_sha256
    assert first.ir_fingerprint_sha256 == isolated.ir_fingerprint_sha256
    assert first.scope_receipt == reused.scope_receipt == isolated.scope_receipt
    assert reused.metrics.frontend_launch_count == 0
    assert reused.metrics.semantic_session_hit_count == 1
    await runner.close()
    assert list(staging.iterdir()) == []
    assert list(one_shot_staging.iterdir()) == []
