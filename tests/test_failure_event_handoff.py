"""Request-local log reuse with real file changes and public fallback controls."""
import os
import threading

import pytest

import config
import server
from src import log_parser
from src.cancellation import OperationCancelled, push_cancel_event, pop_cancel_event
from src.native_query import NativeFallback
from tests.native_first_helpers import project, public
from tests.test_native_first_consumers import wave


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def parser_counter(tmp_path, monkeypatch):
    patterns = tmp_path / "patterns.yaml"
    patterns.write_text("patterns: []\n")
    monkeypatch.setattr(log_parser, "CUSTOM_PATTERNS_FILE", patterns)
    original = log_parser.SimLogParser.parse_failure_events
    calls = []
    def parse(owner):
        calls.append(owner.log_path)
        return original(owner)
    monkeypatch.setattr(log_parser.SimLogParser, "parse_failure_events", parse)
    return calls, patterns


@pytest.mark.parametrize("empty", [False, True])
def test_unchanged_observation_reuses_even_empty_events(tmp_path, parser_counter, empty):
    log = tmp_path / "sim.log"
    log.write_text("simulation complete\n" if empty else "error @ 10ps\n")
    first = log_parser.observe_failure_events(str(log), "vcs")
    second = log_parser.observe_failure_events(str(log), "vcs", previous=first)
    assert second.events == first.events
    assert len(parser_counter[0]) == 1
    # Starting another request without a handoff always parses again.
    log_parser.observe_failure_events(str(log), "vcs")
    assert len(parser_counter[0]) == 2


@pytest.mark.parametrize("change", ["content", "replacement", "alias", "patterns", "simulator", "unit"])
def test_changed_observation_reparses(tmp_path, parser_counter, change):
    calls, patterns = parser_counter
    target = tmp_path / "target.log"
    target.write_text("error @ 10ps\n")
    log = tmp_path / "sim.log"
    log.symlink_to(target)
    simulator, unit = ("verilator", "ps") if change == "unit" else ("vcs", None)
    first = log_parser.observe_failure_events(str(log), simulator, unit)
    before = target.stat()
    if change == "content":
        target.write_text("error @ 20ps\n")
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    elif change == "replacement":
        other = tmp_path / "other.log"
        other.write_text("error @ 20ps\n")
        os.utime(other, ns=(before.st_atime_ns, before.st_mtime_ns))
        other.replace(target)
    elif change == "alias":
        other = tmp_path / "other.log"
        other.write_text(target.read_text())
        log.unlink()
        log.symlink_to(other)
    elif change == "patterns":
        patterns.write_text("patterns: [] # changed\n")
    elif change == "simulator":
        simulator = "xcelium"
    else:
        unit = "ns"
    second = log_parser.observe_failure_events(str(log), simulator, unit, previous=first)
    assert len(calls) == 2
    if change in {"content", "replacement"}:
        assert second.events[0]["time_ps"] == 20


def test_change_during_parse_does_not_publish_a_reusable_observation(tmp_path, parser_counter, monkeypatch):
    log = tmp_path / "sim.log"
    log.write_text("error @ 10ps\n")
    original = log_parser.SimLogParser.parse_failure_events
    def parse(owner):
        events = original(owner)
        log.write_text("error @ 20ps\n")
        return events
    monkeypatch.setattr(log_parser.SimLogParser, "parse_failure_events", parse)
    first = log_parser.observe_failure_events(str(log), "vcs")
    assert first.identity is None
    second = log_parser.observe_failure_events(str(log), "vcs", previous=first)
    assert len(parser_counter[0]) == 2 and second.events[0]["time_ps"] == 20


def test_cancelled_handoff_cannot_return_cached_events(tmp_path, parser_counter):
    log = tmp_path / "sim.log"
    log.write_text("error @ 10ps\n")
    first = log_parser.observe_failure_events(str(log), "vcs")
    event = threading.Event()
    event.set()
    token = push_cancel_event(event)
    try:
        with pytest.raises(OperationCancelled):
            log_parser.observe_failure_events(str(log), "vcs", previous=first)
    finally:
        pop_cancel_event(token)


@pytest.mark.anyio
@pytest.mark.parametrize("changed", [False, True])
async def test_public_recommend_fallback_reuses_only_current_request_events(tmp_path, monkeypatch, parser_counter, changed):
    server.reset_session_state()
    monkeypatch.setattr(config, "TRACEWEAVE_CACHE_ROOT", tmp_path / "cache")
    monkeypatch.setenv("TRACEWEAVE_NPI_EXECUTION", "local")
    monkeypatch.setenv("TRACEWEAVE_CONNECTIVITY_ROUTE", "auto")
    _, compile_log, _ = project(tmp_path, candidate=False)
    sim_log = tmp_path / "sim.log"
    def write(time):
        sim_log.write_text(f'"/checker.sv", 3: top.u: started at 0ps failed at {time}ps\n')
    write(10)
    selected = []
    async def source(args, event):
        selected.append(event["time_ps"])
        if args.get("_native_query") is not None:
            if changed:
                before = sim_log.stat()
                write(20)
                os.utime(sim_log, ns=(before.st_atime_ns, before.st_mtime_ns))
            raise NativeFallback([dict(backend="verdi_npi", status="blocked", reason="npi_unavailable")])
        return {}, None
    monkeypatch.setattr(server, "_failure_source_context", source)
    args = dict(compile_log=str(compile_log), log_path=str(sim_log),
                wave_path=wave(tmp_path / "wave.vcd"), simulator="vcs")
    try:
        result = await public("recommend_failure_debug_next_steps", **args)
        assert not result.get("error_code"), result
        assert selected == [10, 20 if changed else 10]
        assert len(parser_counter[0]) == (2 if changed else 1)
        if not changed:
            result = await public("recommend_failure_debug_next_steps", **args)
            assert not result.get("error_code"), result
            assert len(parser_counter[0]) == 2
    finally:
        server.reset_session_state()
