"""Private identity boundaries, bounded retention, and persisted-field filtering."""

import json

import pytest

import config
from src import usage_telemetry as ut
from src.telemetry_context import ArtifactRegistry, Attribution


def discovery(wave):
    return {"formal_root": str(wave.parent), "wave_files": [{"path": str(wave)}]}


def test_aliases_share_identity_and_retarget_invalidates_inflight(tmp_path):
    wave = tmp_path / "original.vcd"
    wave.write_text("version one")
    other = tmp_path / "other.vcd"
    other.write_text("version two")
    alias = tmp_path / "alias.vcd"
    alias.symlink_to(wave)
    registry = ArtifactRegistry()
    owner = registry.discover("get_formal_paths", discovery(wave))
    context = registry.capture({"wave_path": str(alias)})
    assert context.session_id == owner.session_id
    alias.unlink()
    alias.symlink_to(other)
    assert context.finish().status == "artifact_changed"
    assert registry.capture({"wave_path": str(alias)}).status == "unresolved"


@pytest.mark.parametrize("limits", [{"max_owners": 1}, {"max_files": 1}])
def test_capacity_does_not_forget_conflicts_and_reassign(tmp_path, limits):
    registry = ArtifactRegistry(**limits)
    for i in range(2):
        folder = tmp_path / str(i)
        folder.mkdir()
        wave = folder / "wave.vcd"
        wave.write_text("fixture")
        context = registry.discover("get_formal_paths", discovery(wave))
    assert context.status == "capacity"
    assert not registry._owners
    assert registry.capture({"wave_path": str(wave)}).status == "capacity"
    assert registry.discover("get_formal_paths", discovery(wave)).status == "capacity"


def test_new_artifacts_do_not_split_owner_but_changes_do(tmp_path):
    a, b = tmp_path / "a.vcd", tmp_path / "b.vcd"
    a.write_text("first")
    b.write_text("second")
    registry = ArtifactRegistry()
    original = registry.discover("get_formal_paths", discovery(a))
    appended = registry.discover("get_formal_paths", discovery(b))
    assert original.session_id == appended.session_id
    a.write_text("updated first")
    updated = registry.discover("get_formal_paths", discovery(a))
    assert original.session_id != updated.session_id
    # A generation change requires fresh discovery of the other artifacts.
    assert registry.capture({"wave_path": str(b)}).status == "unresolved"


def test_compare_requires_both_wave_owners_and_prioritizes_wave_over_log(tmp_path):
    registry = ArtifactRegistry()
    waves = []
    for name in ("a", "b"):
        folder = tmp_path / name
        folder.mkdir()
        wave = folder / "wave.vcd"
        wave.write_text("fixture")
        registry.discover("get_formal_paths", discovery(wave))
        waves.append(str(wave))
    assert registry.capture({"wave_path_a": waves[0], "wave_path_b": waves[1]}).status == "multiple_owners"
    context = registry.capture({"wave_path": waves[0], "compile_log": waves[1]})
    assert context.session_id == registry.capture({"wave_path": waves[0]}).session_id


def test_new_fields_are_filtered_on_write_and_aggregation(tmp_path, monkeypatch):
    log = tmp_path / "telemetry" / "usage.jsonl"
    monkeypatch.setattr(config, "TELEMETRY_ENABLED", True)
    monkeypatch.setattr(config, "telemetry_log_path", lambda: log)
    private = "/private/top.secret"
    ut.record_call("get_signal_at_time", {"mode": private, "edge": "posedge"},
                   result_bytes=1, ok=True, case=private,
                   attribution=Attribution(domain=private, session_id="a" * 64),
                   readback={"kind": "point", "returned_sample_count": 1,
                             "requested_signal_count": private, "returned_timepoint_count": True,
                             "path": private, "signal": "top.secret", "hash": "b" * 64,
                             "value": "0x42", "exception": "private exception"})
    raw = log.read_text()
    record = json.loads(raw)
    assert record["artifact_domain"] == "unknown"
    assert record["session_id"] is None and record["case"] is None
    assert record["flags"] == {"edge": "posedge"}
    assert record["readback"] == {"kind": "point", "returned_sample_count": 1}
    for secret in (private, "top.secret", "a" * 64, "b" * 64, "0x42", "private exception"):
        assert secret not in raw
    record["artifact_domain"] = [private]
    record["session_id"] = private
    record["readback"] = {"kind": private, "returned_sample_count": -1}
    report = ut.aggregate([record])
    assert report["total_sessions"] == 0
    assert report["artifact_usage"]["unknown"]["returned_sample_count"] == 0
    assert private not in json.dumps(report["artifact_usage"])


def test_disabled_attribution_never_touches_registry(monkeypatch):
    monkeypatch.setattr(config, "TELEMETRY_ENABLED", False)

    def forbidden(*args):
        pytest.fail("disabled telemetry must not inspect file identities")

    monkeypatch.setattr(ut._artifact_registry, "capture", forbidden)
    monkeypatch.setattr(ut._artifact_registry, "discover", forbidden)
    assert ut.begin_call({"wave_path": "/private/missing"}).domain == "unknown"
    assert ut.note_discovery("get_formal_paths", {}).domain == "unknown"


def test_identity_failure_is_best_effort(monkeypatch):
    monkeypatch.setattr(config, "TELEMETRY_ENABLED", True)

    def failed(*args):
        raise OSError("private path and exception text")

    monkeypatch.setattr(ut._artifact_registry, "capture", failed)
    assert ut.begin_call({}).public_fields() == {
        "artifact_domain": "unknown", "session_id": None, "attribution_status": "unavailable",
    }


def test_report_distinguishes_metadata_and_readback_without_causal_claims():
    from scripts.telemetry_report import render

    record = {"tool": "get_formal_paths", "result_bytes": 10, "ok": True,
              **Attribution("formal", "matched", "a" * 32).public_fields()}
    report = ut.aggregate([record])
    text = render(report)
    assert "metadata-only=1 with-state=0" in text
    assert "SDK/client validation failures are excluded" in text
    assert "does not establish model adoption" in text


def test_new_unattributed_source_graph_calls_do_not_invent_sessions():
    rows = [{"tool": "explain_signal_driver", "result_bytes": 1, "ok": True,
             "diagnostics": {"source_graph_cache_tier": "disk", "source_graph_disk_hit_count": 1},
             **Attribution().public_fields()} for _ in range(2)]
    report = ut.aggregate(rows)
    graph = report["source_graph"]
    assert graph["calls_with_metrics"] == graph["unattributed_calls"] == 2
    assert graph["sessions_with_metrics"] == 0
    assert graph["cache_tiers"]["disk"]["sessions"] == 0
    assert graph["by_tool"]["explain_signal_driver"]["sessions"] == 0
    assert graph["query_frequency"]["adjacent_call_pairs"] == 0
    assert graph["disk"]["exact_hit_rate"] == 1.0
