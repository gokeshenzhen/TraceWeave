"""Readback attribution through the real handler, with telemetry isolated locally."""

import importlib
import json

import anyio
import pytest

import config
import server
from src import usage_telemetry as ut
from src.readback_probe import PROBE_VCD


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def telemetry(tmp_path, monkeypatch):
    importlib.reload(ut)
    server.reset_session_state()
    log = tmp_path / "telemetry" / "usage.jsonl"
    monkeypatch.setattr(config, "TELEMETRY_ENABLED", True)
    monkeypatch.setattr(config, "telemetry_log_path", lambda: log)
    yield log
    server.reset_session_state()
    importlib.reload(ut)


def wave_at(root):
    root.mkdir(parents=True, exist_ok=True)
    wave = root / "private_witness.vcd"
    wave.write_text(PROBE_VCD)
    return wave


async def discover(wave, domain="formal"):
    tool, key = ("get_formal_paths", "formal_root") if domain == "formal" else (
        "get_sim_paths", "verif_root"
    )
    return await server.call_tool(tool, {key: str(wave.parent), "wave_file": str(wave)})


async def point(wave):
    result = await server.call_tool("get_signal_at_time", {
        "wave_path": str(wave), "signal_path": "readback_probe.data", "time_ps": 0,
    })
    assert json.loads(result[0].text)["value"]["dec"] == 1


def records(log):
    return [json.loads(line) for line in log.read_text().splitlines()]


@pytest.mark.anyio
async def test_formal_only_repeat_and_distinct_projects(telemetry, tmp_path):
    a, b = [wave_at(tmp_path / name) for name in ("private_a", "private_b")]
    await discover(a)
    await point(a)
    await discover(a)
    await discover(b)
    await point(b)
    await point(a)
    rows = records(telemetry)
    assert all(r["artifact_domain"] == "formal" for r in rows)
    assert rows[0]["session_id"]
    assert rows[0]["session_id"] == rows[1]["session_id"] == rows[2]["session_id"] == rows[5]["session_id"]
    assert rows[3]["session_id"] == rows[4]["session_id"] != rows[0]["session_id"]
    assert all(r["case"] is None for r in rows)
    assert "private_" not in telemetry.read_text()
    assert "readback_probe" not in telemetry.read_text()


@pytest.mark.anyio
async def test_mixed_domains_use_requested_wave_not_latest_discovery(telemetry, tmp_path):
    sim, formal = [wave_at(tmp_path / name) for name in ("sim", "formal")]
    await discover(sim, "simulation")
    await discover(formal)
    await point(sim)
    await point(formal)
    rows = records(telemetry)
    assert [r["artifact_domain"] for r in rows] == ["simulation", "formal", "simulation", "formal"]
    assert rows[0]["session_id"] == rows[2]["session_id"]
    assert rows[1]["session_id"] == rows[3]["session_id"]
    assert rows[0]["session_id"] != rows[1]["session_id"]


@pytest.mark.anyio
async def test_unknown_changed_and_ambiguous_files_do_not_inherit_session(telemetry, tmp_path):
    wave = wave_at(tmp_path / "run")
    await point(wave)
    await discover(wave)
    wave.write_text(PROBE_VCD + "\n")
    await point(wave)
    await discover(wave)
    await point(wave)
    await discover(wave, "simulation")
    await point(wave)
    rows = records(telemetry)
    for i, reason in ((0, "unresolved"), (2, "artifact_changed"), (6, "ambiguous")):
        assert rows[i]["artifact_domain"] == "unknown"
        assert rows[i]["attribution_status"] == reason
        assert rows[i]["session_id"] is None
    assert rows[1]["session_id"] != rows[3]["session_id"] == rows[4]["session_id"]
    report = ut.aggregate(rows)
    assert report["total_sessions"] == 3
    assert report["artifact_usage"]["unknown"]["unattributed_calls"] == 3


@pytest.mark.anyio
async def test_multi_project_discovery_keeps_each_project_identity(telemetry, tmp_path):
    a, b = [wave_at(tmp_path / name) for name in ("a", "b")]
    for wave in (a, b):
        (wave.parent / "jg_console.log").write_text("fixture\n")
    await discover(a)
    await server.call_tool("get_formal_paths", {"formal_root": str(tmp_path)})
    await point(a)
    await point(b)
    rows = records(telemetry)
    assert rows[1]["attribution_status"] == "multiple_owners"
    assert rows[1]["session_id"] is None
    assert rows[0]["session_id"] == rows[2]["session_id"] != rows[3]["session_id"]


@pytest.mark.anyio
@pytest.mark.parametrize("change_file", [False, True])
async def test_inflight_read_pins_identity_and_checks_file_version(
    telemetry, tmp_path, monkeypatch, change_file,
):
    a, b = [wave_at(tmp_path / name) for name in ("a", "b")]
    await discover(a)
    entered, release = anyio.Event(), anyio.Event()
    original = server._dispatch

    async def delayed(name, args):
        if name == "get_signal_at_time":
            entered.set()
            await release.wait()
        return await original(name, args)

    monkeypatch.setattr(server, "_dispatch", delayed)
    async with anyio.create_task_group() as group:
        group.start_soon(point, a)
        await entered.wait()
        await discover(b)
        if change_file:
            a.write_text(PROBE_VCD + "\n")
        release.set()
    rows = records(telemetry)
    if change_file:
        assert rows[-1]["attribution_status"] == "artifact_changed"
        assert rows[-1]["session_id"] is None
    else:
        assert rows[-1]["session_id"] == rows[0]["session_id"] != rows[1]["session_id"]


@pytest.mark.anyio
async def test_counts_real_values_without_persisting_them(telemetry, tmp_path):
    wave = wave_at(tmp_path / "read")
    metadata_wave = wave_at(tmp_path / "metadata")
    await discover(metadata_wave)
    await server.call_tool("search_signals", {"wave_path": str(metadata_wave), "keyword": "data"})
    await discover(wave)
    await point(wave)
    await server.call_tool("get_signals_around_time", {
        "wave_path": str(wave), "signal_paths": [
            "readback_probe.data", "readback_probe.unknown", "readback_probe.late", "missing",
        ], "center_time_ps": 0, "window_ps": 0, "extra_transitions": 0,
        "return_mode": "values_only",
    })
    await server.call_tool("get_signals_by_cycle", {
        "wave_path": str(wave), "signal_paths": ["readback_probe.data"],
        "clock_path": "readback_probe.clk", "num_cycles": 3,
    })
    rows = records(telemetry)
    assert rows[-2]["readback"] == {
        "kind": "batch_point", "requested_signal_count": 4,
        "returned_sample_count": 2, "returned_timepoint_count": 1,
    }
    assert rows[-1]["readback"]["returned_sample_count"] == 2
    report = ut.aggregate(rows)["artifact_usage"]["formal"]
    assert report["sessions_with_readback"] == 1
    assert report["metadata_only_sessions"] == 1
    assert report["returned_sample_count"] == 5
    assert report["point_calls"] == report["batch_point_calls"] == report["cycle_calls"] == 1
    assert "b10xz" not in telemetry.read_text()


@pytest.mark.anyio
async def test_disabled_or_failed_telemetry_does_not_break_readback(telemetry, tmp_path, monkeypatch):
    wave = wave_at(tmp_path / "run")
    monkeypatch.setattr(config, "TELEMETRY_ENABLED", False)
    await discover(wave)
    await point(wave)
    assert not telemetry.exists()
    monkeypatch.setattr(config, "TELEMETRY_ENABLED", True)

    def disk_failure():
        raise OSError("private disk exception")

    monkeypatch.setattr(config, "telemetry_log_path", disk_failure)
    await discover(wave)
    await point(wave)
    assert not telemetry.exists()


@pytest.mark.anyio
async def test_cancelled_read_keeps_attribution_without_claiming_samples(telemetry, tmp_path, monkeypatch):
    wave = wave_at(tmp_path / "run")
    await discover(wave)

    async def waiting_dispatch(name, args):
        await anyio.sleep_forever()

    monkeypatch.setattr(server, "_dispatch", waiting_dispatch)
    with anyio.move_on_after(0.01) as scope:
        await server.call_tool("get_signal_at_time", {
            "wave_path": str(wave), "signal_path": "readback_probe.data", "time_ps": 0,
        })
    assert scope.cancel_called
    rows = records(telemetry)
    assert rows[0]["session_id"] == rows[1]["session_id"]
    assert rows[1]["error_code"] == "cancelled" and rows[1]["ok"] is False
    assert rows[1]["readback"]["returned_sample_count"] == 0
