from __future__ import annotations

import json
from pathlib import Path
import stat
import subprocess
import sys
import threading

import pytest
from pydantic import ValidationError

import server
from config import NpiExecutionConfig, get_npi_execution_config
from src import cancellation, schemas
from src.cancellation import OperationCancelled
from src.npi_lsf import (
    FindDriverWorkerRequest,
    FindLoadsWorkerRequest,
    FindPathWorkerRequest,
    LsfConnectivityBackend,
    LsfExecutionResult,
    LsfNpiTransport,
    NPI_EXECUTION_STATUS_KEY,
    WorkerError,
    WorkerSuccess,
    WorkerUnavailable,
    _read_worker_response,
    execute_worker_request,
    parse_worker_request_bytes,
    write_worker_response,
)


_NPI_ENV_VARS = (
    "TRACEWEAVE_NPI_EXECUTION",
    "TRACEWEAVE_NPI_LSF_QUEUE",
    "LSF_QUEUE",
    "TRACEWEAVE_NPI_LSF_BSUB",
    "TRACEWEAVE_NPI_LSF_BKILL",
    "TRACEWEAVE_NPI_LSF_PYTHON",
    "TRACEWEAVE_NPI_LSF_TIMEOUT",
    "TRACEWEAVE_NPI_LSF_STAGING_DIR",
    "TRACEWEAVE_NPI_LSF_EXTRA_ARGS_JSON",
    "TRACEWEAVE_NPI_WORKER",
    "TRACEWEAVE_NPI_ALLOW_DEGRADED_KDB",
)


def _clear_npi_env(monkeypatch):
    for name in _NPI_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _lsf_config(tmp_path: Path, **overrides) -> NpiExecutionConfig:
    values = {
        "mode": "lsf",
        "queue": "licensed_q",
        "bsub_bin": "bsub",
        "bkill_bin": "bkill",
        "python_bin": "python3.11",
        "timeout_sec": 120,
        "staging_dir": tmp_path,
        "extra_args": (),
        "error_code": None,
    }
    values.update(overrides)
    return NpiExecutionConfig(**values)


class _ExactBackend:
    def __init__(self, *, load_ok=True, load_quality="clean"):
        self.load_ok = load_ok
        self.kdb_load_quality = load_quality

    def _ensure_loaded(self, kdb_path, top):
        return self.load_ok

    def _npi_find_driver(self, signal_path, wave_path, top, *, recursive):
        return {
            "signal_path": signal_path,
            "wave_path": wave_path,
            "resolved_rtl_name": signal_path.rsplit(".", 1)[-1],
            "resolved_module": top,
            "driver_status": "resolved",
            "driver_kind": "always_ff",
            "recursive": recursive,
            "backend": "verdi_npi",
        }

    def _npi_find_loads(
        self,
        signal_path,
        compile_result,
        kdb_path,
        top,
        include_expr,
        kind_filter,
    ):
        return {
            "signal_path": signal_path,
            "resolved_rtl_name": signal_path.rsplit(".", 1)[-1],
            "resolved_module": top,
            "loads": [],
            "completeness": "exact",
            "stopped_at": "no_npi_loads",
            "unsupported_reason": None,
            "backend": "verdi_npi",
        }

    def _npi_find_path(self, from_signal, to_signal, *, expand_assigns):
        return {
            "from_signal": from_signal,
            "to_signal": to_signal,
            "found": False,
            "hops": 0,
            "path": [],
            "expand_assigns": expand_assigns,
            "unsupported_reason": "not_connected",
            "backend": "verdi_npi",
        }


class _FakeFallback:
    def __init__(self):
        self.calls = []

    def find_driver(self, signal_path, wave_path, compile_log, **kwargs):
        self.calls.append(("driver", signal_path))
        return {
            "signal_path": signal_path,
            "wave_path": wave_path,
            "resolved_rtl_name": signal_path.rsplit(".", 1)[-1],
            "driver_status": "unsupported",
            "recursive": kwargs.get("recursive", False),
            "backend": "static",
        }

    def find_loads(self, signal_path, compile_log, **kwargs):
        self.calls.append(("loads", signal_path))
        return {
            "signal_path": signal_path,
            "resolved_rtl_name": signal_path.rsplit(".", 1)[-1],
            "loads": [],
            "completeness": "shallow_only",
        }

    def find_path(self, from_signal, to_signal, compile_log, **kwargs):
        self.calls.append(("path", from_signal, to_signal))
        return {
            "from_signal": from_signal,
            "to_signal": to_signal,
            "found": False,
            "hops": 0,
            "path": [],
            "expand_assigns": kwargs.get("expand_assigns", False),
            "unsupported_reason": "static_backend_no_path_api",
        }


class _FakeTransport:
    def __init__(self, outcome):
        self.outcome = outcome
        self.requests = []

    def execute(self, request):
        self.requests.append(request)
        return self.outcome


def _make_compile_log(tmp_path: Path) -> str:
    log = tmp_path / "comp.log"
    log.write_text(
        "Command: vcs -kdb top.sv\nTop Level Modules:\n       top_tb\n",
        encoding="utf-8",
    )
    (tmp_path / "simv.daidir" / "kdb.elab++").mkdir(parents=True)
    return str(log)


def test_npi_execution_config_defaults_local(monkeypatch):
    _clear_npi_env(monkeypatch)
    config = get_npi_execution_config()
    assert config.valid
    assert config.mode == "local"
    assert config.queue is None


def test_npi_execution_config_accepts_valid_lsf(monkeypatch, tmp_path):
    _clear_npi_env(monkeypatch)
    monkeypatch.setenv("TRACEWEAVE_NPI_EXECUTION", "lsf")
    monkeypatch.setenv("TRACEWEAVE_NPI_LSF_QUEUE", "verdi_q")
    monkeypatch.setenv("TRACEWEAVE_NPI_LSF_TIMEOUT", "300")
    monkeypatch.setenv("TRACEWEAVE_NPI_LSF_STAGING_DIR", str(tmp_path))
    monkeypatch.setenv(
        "TRACEWEAVE_NPI_LSF_EXTRA_ARGS_JSON",
        json.dumps(["-R", "select[mem>8000]"]),
    )

    config = get_npi_execution_config()

    assert config.valid
    assert config.mode == "lsf"
    assert config.queue == "verdi_q"
    assert config.timeout_sec == 300
    assert config.extra_args == ("-R", "select[mem>8000]")


def test_npi_execution_config_ignores_generic_lsf_queue(
    monkeypatch,
    tmp_path,
):
    _clear_npi_env(monkeypatch)
    monkeypatch.setenv("TRACEWEAVE_NPI_EXECUTION", "lsf")
    monkeypatch.setenv("LSF_QUEUE", "digital_verification")
    monkeypatch.setenv("TRACEWEAVE_NPI_LSF_STAGING_DIR", str(tmp_path))

    config = get_npi_execution_config()

    assert not config.valid
    assert config.queue is None
    assert config.error_code == "npi_execution_config_invalid"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("TRACEWEAVE_NPI_EXECUTION", "unknown"),
        ("TRACEWEAVE_NPI_LSF_QUEUE", "bad queue"),
        ("TRACEWEAVE_NPI_LSF_TIMEOUT", "NaN"),
        ("TRACEWEAVE_NPI_LSF_PYTHON", "-V"),
        ("TRACEWEAVE_NPI_LSF_EXTRA_ARGS_JSON", '["-q", "other"]'),
        ("TRACEWEAVE_NPI_LSF_EXTRA_ARGS_JSON", '["echo", "unexpected"]'),
    ],
)
def test_npi_execution_config_rejects_invalid_values(
    monkeypatch,
    tmp_path,
    name,
    value,
):
    _clear_npi_env(monkeypatch)
    monkeypatch.setenv("TRACEWEAVE_NPI_EXECUTION", "lsf")
    monkeypatch.setenv("TRACEWEAVE_NPI_LSF_QUEUE", "verdi_q")
    monkeypatch.setenv("TRACEWEAVE_NPI_LSF_STAGING_DIR", str(tmp_path))
    monkeypatch.setenv(name, value)

    config = get_npi_execution_config()

    assert not config.valid
    assert config.error_code == "npi_execution_config_invalid"


def test_npi_worker_guard_forces_local(monkeypatch):
    _clear_npi_env(monkeypatch)
    monkeypatch.setenv("TRACEWEAVE_NPI_EXECUTION", "lsf")
    monkeypatch.setenv("TRACEWEAVE_NPI_LSF_QUEUE", "verdi_q")
    monkeypatch.setenv("TRACEWEAVE_NPI_WORKER", "1")
    assert get_npi_execution_config().mode == "local"


@pytest.mark.parametrize(
    "worker_request",
    [
        FindDriverWorkerRequest(
            kdb_path="/shared/kdb.elab++",
            top="top_tb",
            signal_path="top_tb.dut.q",
            recursive=True,
        ),
        FindLoadsWorkerRequest(
            kdb_path="/shared/kdb.elab++",
            top="top_tb",
            signal_path="top_tb.dut.q",
        ),
        FindPathWorkerRequest(
            kdb_path="/shared/kdb.elab++",
            top="top_tb",
            from_signal="top_tb.dut.a",
            to_signal="top_tb.dut.b",
        ),
    ],
)
def test_worker_executes_all_operations_without_static_fallback(worker_request):
    response = execute_worker_request(worker_request, backend=_ExactBackend())
    assert isinstance(response, WorkerSuccess)
    assert response.status == "ok"
    assert "backend_status" not in response.result


def test_worker_reports_npi_unavailable_without_static_answer():
    request = FindDriverWorkerRequest(
        kdb_path="/shared/kdb.elab++",
        top="top_tb",
        signal_path="top_tb.dut.q",
    )
    response = execute_worker_request(
        request,
        backend=_ExactBackend(load_ok=False),
    )
    assert isinstance(response, WorkerUnavailable)
    assert response.error_code == "npi_load_failed"


def test_worker_reports_degraded_load_quality_and_keeps_npi_provenance():
    request = FindLoadsWorkerRequest(
        kdb_path="/shared/kdb.elab++",
        top="top_tb",
        signal_path="top_tb.dut.q",
    )

    response = execute_worker_request(
        request,
        backend=_ExactBackend(load_quality="degraded"),
    )

    assert isinstance(response, WorkerSuccess)
    assert response.kdb_load_quality == "degraded"
    assert response.result["backend"] == "verdi_npi"


def test_worker_protocol_rejects_malformed_request():
    with pytest.raises((ValidationError, ValueError)):
        parse_worker_request_bytes(
            b'{"protocol_version":"1.0","operation":"find_driver"}'
        )


def test_worker_response_write_is_private_and_atomic(tmp_path):
    path = tmp_path / "response.json"
    write_worker_response(
        path,
        WorkerError(
            error_code="worker_internal_error",
            stage="query",
        ),
    )
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert isinstance(_read_worker_response(path), WorkerError)
    assert not list(tmp_path.glob(".response_*.tmp"))


def test_worker_script_bootstraps_from_arbitrary_cwd(tmp_path):
    request_path = tmp_path / "request.json"
    response_path = tmp_path / "response.json"
    request_path.write_text("{}", encoding="utf-8")
    worker_path = Path(__file__).resolve().parents[1] / "src" / "npi_worker.py"

    proc = subprocess.run(
        [
            sys.executable,
            str(worker_path),
            "--request",
            str(request_path),
            "--response",
            str(response_path),
        ],
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )

    assert proc.returncode == 2
    assert isinstance(_read_worker_response(response_path), WorkerError)
    assert proc.stdout == b""


def test_lsf_transport_builds_argv_without_shell(monkeypatch, tmp_path):
    captured = {}

    class FakePopen:
        returncode = 0

        def __init__(self, argv, **kwargs):
            captured["argv"] = list(argv)
            captured["kwargs"] = kwargs
            response_path = Path(argv[argv.index("--response") + 1])
            request_path = Path(argv[argv.index("--request") + 1])
            captured["request_mode"] = stat.S_IMODE(request_path.stat().st_mode)
            captured["temp_mode"] = stat.S_IMODE(request_path.parent.stat().st_mode)
            write_worker_response(
                response_path,
                WorkerSuccess(
                    result={
                        "signal_path": "top_tb.q",
                        "wave_path": "",
                        "resolved_rtl_name": "q",
                        "driver_status": "resolved",
                        "backend": "verdi_npi",
                    }
                ),
            )

        def poll(self):
            return 0

    monkeypatch.setattr("src.npi_lsf.subprocess.Popen", FakePopen)
    config = _lsf_config(
        tmp_path,
        extra_args=("-R", "select[mem>8000]"),
    )
    request = FindDriverWorkerRequest(
        kdb_path="/shared/kdb.elab++",
        top="top_tb",
        signal_path="top_tb.q",
    )

    outcome = LsfNpiTransport(config).execute(request)

    assert outcome.result is not None
    assert outcome.result["signal_path"] == "top_tb.q"
    assert outcome.result["backend"] == "verdi_npi"
    assert outcome.scheduler_status == "completed"
    assert captured["kwargs"]["shell"] is False
    assert captured["request_mode"] == 0o600
    assert captured["temp_mode"] == 0o700
    assert captured["argv"][:4] == [
        "bsub",
        "-R",
        "select[mem>8000]",
        "-K",
    ]
    assert captured["argv"][captured["argv"].index("-q") + 1] == "licensed_q"
    job_index = captured["argv"].index("-J")
    assert captured["argv"][job_index + 2 : job_index + 4] == [
        "-o",
        "/dev/null",
    ]
    assert captured["argv"][job_index + 4] == "python3.11"


def test_lsf_transport_missing_bsub_returns_submit_failure(
    monkeypatch,
    tmp_path,
):
    def missing_bsub(*args, **kwargs):
        raise FileNotFoundError("bsub")

    monkeypatch.setattr("src.npi_lsf.subprocess.Popen", missing_bsub)
    request = FindDriverWorkerRequest(
        kdb_path="/shared/kdb.elab++",
        top="top_tb",
        signal_path="top_tb.q",
    )

    outcome = LsfNpiTransport(_lsf_config(tmp_path)).execute(request)

    assert outcome.result is None
    assert outcome.scheduler_status == "failed"
    assert outcome.worker_status == "failed"
    assert outcome.fallback_reason == "npi_lsf_submit_failed"


def test_lsf_transport_rejects_malformed_response(monkeypatch, tmp_path):
    class FakePopen:
        returncode = 0

        def __init__(self, argv, **kwargs):
            response_path = Path(argv[argv.index("--response") + 1])
            response_path.write_text("{broken", encoding="utf-8")

        def poll(self):
            return 0

    monkeypatch.setattr("src.npi_lsf.subprocess.Popen", FakePopen)
    request = FindDriverWorkerRequest(
        kdb_path="/shared/kdb.elab++",
        top="top_tb",
        signal_path="top_tb.q",
    )
    outcome = LsfNpiTransport(_lsf_config(tmp_path)).execute(request)
    assert outcome.result is None
    assert outcome.scheduler_status == "completed"
    assert outcome.worker_status == "failed"
    assert outcome.fallback_reason == "npi_lsf_worker_failed"


def test_lsf_transport_rejects_invalid_operation_result(
    monkeypatch,
    tmp_path,
):
    class FakePopen:
        returncode = 0

        def __init__(self, argv, **kwargs):
            response_path = Path(argv[argv.index("--response") + 1])
            write_worker_response(
                response_path,
                WorkerSuccess(result={"answer": "wrong schema"}),
            )

        def poll(self):
            return 0

    monkeypatch.setattr("src.npi_lsf.subprocess.Popen", FakePopen)
    request = FindDriverWorkerRequest(
        kdb_path="/shared/kdb.elab++",
        top="top_tb",
        signal_path="top_tb.q",
    )

    outcome = LsfNpiTransport(_lsf_config(tmp_path)).execute(request)

    assert outcome.result is None
    assert outcome.scheduler_status == "completed"
    assert outcome.worker_status == "failed"
    assert outcome.fallback_reason == "npi_lsf_worker_failed"


def test_lsf_transport_classifies_worker_error_after_scheduler_completion(
    monkeypatch,
    tmp_path,
):
    class FakePopen:
        returncode = 2

        def __init__(self, argv, **kwargs):
            response_path = Path(argv[argv.index("--response") + 1])
            write_worker_response(
                response_path,
                WorkerError(
                    error_code="worker_internal_error",
                    stage="query",
                ),
            )

        def poll(self):
            return self.returncode

    monkeypatch.setattr("src.npi_lsf.subprocess.Popen", FakePopen)
    request = FindDriverWorkerRequest(
        kdb_path="/shared/kdb.elab++",
        top="top_tb",
        signal_path="top_tb.q",
    )

    outcome = LsfNpiTransport(_lsf_config(tmp_path)).execute(request)

    assert outcome.result is None
    assert outcome.scheduler_status == "completed"
    assert outcome.worker_status == "failed"
    assert outcome.fallback_reason == "npi_lsf_worker_failed"


def test_lsf_transport_timeout_cancels_named_job(monkeypatch, tmp_path):
    calls = []

    class FakePopen:
        returncode = None

        def __init__(self, argv, **kwargs):
            self.argv = argv
            self.terminated = False

        def poll(self):
            return None if not self.terminated else -15

        def terminate(self):
            self.terminated = True

        def wait(self, timeout):
            self.returncode = -15
            return self.returncode

    times = iter([0.0, 2.0])
    monkeypatch.setattr("src.npi_lsf.subprocess.Popen", FakePopen)
    monkeypatch.setattr("src.npi_lsf.time.monotonic", lambda: next(times))
    monkeypatch.setattr("src.npi_lsf.time.sleep", lambda _: None)
    monkeypatch.setattr(
        "src.npi_lsf.subprocess.run",
        lambda argv, **kwargs: calls.append((list(argv), kwargs)),
    )
    request = FindDriverWorkerRequest(
        kdb_path="/shared/kdb.elab++",
        top="top_tb",
        signal_path="top_tb.q",
    )

    outcome = LsfNpiTransport(_lsf_config(tmp_path, timeout_sec=1)).execute(request)

    assert outcome.scheduler_status == "timed_out"
    assert outcome.fallback_reason == "npi_lsf_timeout"
    assert calls
    assert calls[0][0][0:2] == ["bkill", "-J"]


def test_lsf_transport_request_cancellation_cancels_named_job(
    monkeypatch,
    tmp_path,
):
    calls = []

    class FakePopen:
        returncode = None

        def __init__(self, argv, **kwargs):
            self.terminated = False
            self.first_poll = True

        def poll(self):
            if self.first_poll:
                self.first_poll = False
                event.set()
            return -15 if self.terminated else None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout):
            self.returncode = -15
            return self.returncode

    monkeypatch.setattr("src.npi_lsf.subprocess.Popen", FakePopen)
    monkeypatch.setattr(
        "src.npi_lsf.subprocess.run",
        lambda argv, **kwargs: calls.append((list(argv), kwargs)),
    )
    event = threading.Event()
    token = cancellation.push_cancel_event(event)
    request = FindDriverWorkerRequest(
        kdb_path="/shared/kdb.elab++",
        top="top_tb",
        signal_path="top_tb.q",
    )
    try:
        with pytest.raises(OperationCancelled):
            LsfNpiTransport(_lsf_config(tmp_path)).execute(request)
    finally:
        cancellation.pop_cancel_event(token)

    assert calls
    assert calls[0][0][0:2] == ["bkill", "-J"]


def test_lsf_transport_pre_cancelled_request_never_submits(
    monkeypatch,
    tmp_path,
):
    def unexpected_popen(*args, **kwargs):
        raise AssertionError("pre-cancelled request must not submit")

    monkeypatch.setattr("src.npi_lsf.subprocess.Popen", unexpected_popen)
    event = threading.Event()
    event.set()
    token = cancellation.push_cancel_event(event)
    request = FindDriverWorkerRequest(
        kdb_path="/shared/kdb.elab++",
        top="top_tb",
        signal_path="top_tb.q",
    )
    try:
        with pytest.raises(OperationCancelled):
            LsfNpiTransport(_lsf_config(tmp_path)).execute(request)
    finally:
        cancellation.pop_cancel_event(token)


def test_lsf_backend_returns_exact_driver_and_receipt(tmp_path):
    compile_log = _make_compile_log(tmp_path)
    exact = {
        "signal_path": "top_tb.q",
        "wave_path": "",
        "resolved_rtl_name": "q",
        "driver_status": "resolved",
        "recursive": False,
        "backend": "verdi_npi",
    }
    transport = _FakeTransport(
        LsfExecutionResult(
            result=exact,
            scheduler_status="completed",
            worker_status="completed",
        )
    )
    backend = LsfConnectivityBackend(
        _lsf_config(tmp_path),
        fallback=_FakeFallback(),
        transport=transport,
    )

    result = backend.find_driver(
        "top_tb.q",
        "/shared/wave.fsdb",
        compile_log,
        simulator="vcs",
    )

    assert result["backend"] == "verdi_npi"
    assert result["wave_path"] == "/shared/wave.fsdb"
    assert result[NPI_EXECUTION_STATUS_KEY] == {
        "execution_mode": "lsf",
        "scheduler_status": "completed",
        "worker_status": "completed",
    }
    assert isinstance(transport.requests[0], FindDriverWorkerRequest)


def test_lsf_backend_records_degraded_kdb_status_from_worker(tmp_path):
    compile_log = _make_compile_log(tmp_path)
    kdb = tmp_path / "simv.daidir" / "kdb.elab++"
    error_log = kdb / "elabcomLog" / "compiler.log"
    error_log.parent.mkdir()
    error_log.write_text("Total   8 error(s), 0 warning(s)\n")
    (kdb / ".hasElabcomError").write_text("elabcomLog/compiler.log\n")
    transport = _FakeTransport(
        LsfExecutionResult(
            result={
                "signal_path": "top_tb.q",
                "resolved_rtl_name": "q",
                "resolved_module": "top_tb",
                "loads": [
                    {
                        "load_path": "top_tb.u_sink",
                        "kind": "module_input",
                        "backend": "verdi_npi",
                        "confidence": "exact",
                    }
                ],
                "completeness": "approximate",
                "backend": "verdi_npi",
            },
            scheduler_status="completed",
            worker_status="completed",
            kdb_load_quality="degraded",
        )
    )
    backend = LsfConnectivityBackend(
        _lsf_config(tmp_path),
        fallback=_FakeFallback(),
        transport=transport,
    )

    result = backend.find_loads("top_tb.q", compile_log, simulator="vcs")

    assert result["backend"] == "verdi_npi"
    assert backend.kdb_status == {
        "load_quality": "degraded",
        "error_count": 8,
        "error_log": str(error_log),
    }


def test_lsf_backend_worker_failure_falls_back_with_fixed_reason(tmp_path):
    compile_log = _make_compile_log(tmp_path)
    fallback = _FakeFallback()
    transport = _FakeTransport(
        LsfExecutionResult(
            result=None,
            scheduler_status="failed",
            worker_status="failed",
            fallback_reason="npi_lsf_worker_failed",
        )
    )
    backend = LsfConnectivityBackend(
        _lsf_config(tmp_path),
        fallback=fallback,
        transport=transport,
    )

    result = backend.find_path(
        "top_tb.a",
        "top_tb.b",
        compile_log,
        simulator="vcs",
    )

    assert result["_npi_fallback_reason"] == "npi_lsf_worker_failed"
    assert result[NPI_EXECUTION_STATUS_KEY]["scheduler_status"] == "failed"
    assert fallback.calls == [("path", "top_tb.a", "top_tb.b")]


def test_lsf_backend_invalid_config_does_not_submit(tmp_path):
    config = _lsf_config(
        tmp_path,
        mode="invalid",
        queue=None,
        error_code="npi_execution_config_invalid",
    )

    class NeverTransport:
        def execute(self, request):
            raise AssertionError("invalid config must not submit")

    backend = LsfConnectivityBackend(
        config,
        fallback=_FakeFallback(),
        transport=NeverTransport(),
    )
    result = backend.find_driver(
        "top_tb.q",
        "/shared/wave.fsdb",
        "/missing/compile.log",
        simulator="vcs",
    )
    assert result["_npi_fallback_reason"] == "npi_lsf_config_invalid"
    assert result[NPI_EXECUTION_STATUS_KEY]["scheduler_status"] == "not_started"


def test_public_receipt_contains_no_scheduler_identity():
    result = {
        "_npi_fallback_reason": "npi_lsf_worker_failed",
        NPI_EXECUTION_STATUS_KEY: {
            "execution_mode": "lsf",
            "scheduler_status": "failed",
            "worker_status": "failed",
        },
    }
    payload = json.dumps(result, sort_keys=True)
    assert "licensed_q" not in payload
    assert "bsub" not in payload
    assert "private_user_name" not in payload


def test_server_finalizes_lsf_receipt_and_strips_internal_fields():
    class Backend:
        name = "verdi_npi"
        execution_mode = "lsf"

    result = {
        "_npi_fallback_reason": "npi_lsf_worker_failed",
        NPI_EXECUTION_STATUS_KEY: {
            "execution_mode": "lsf",
            "scheduler_status": "failed",
            "worker_status": "failed",
        },
    }
    status, actual = server._finalize_connectivity_backend_status(
        result,
        {
            "simulator": "vcs",
            "backend": "static",
            "parser_match": "approximate",
            "kdb_path": None,
            "kdb_flow": "none",
            "kdb_hint": None,
        },
        Backend(),
    )

    validated = schemas.BackendStatus.model_validate(status)
    assert actual == "static"
    assert validated.execution_mode == "lsf"
    assert validated.scheduler_status == "failed"
    assert "_npi_fallback_reason" not in result
    assert NPI_EXECUTION_STATUS_KEY not in result
