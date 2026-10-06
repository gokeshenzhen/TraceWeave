"""Benchmark sizes/timings describe one complete public result, including wire."""
import asyncio

from scripts import benchmark_source_graph_driver_evidence as baseline
from tests.test_source_graph_driver_processes import FIXTURE


def test_existing_benchmark_records_encoding_and_actual_wire(tmp_path):
    report = asyncio.run(baseline.measure(FIXTURE, False, 1,
        presentation_repeats=1, memory=False, output_directory=tmp_path))
    assert report.get('presentation'), 'benchmark does not measure the same captured result codec'
    assert report.get('wire'), 'backend JSON is not final MCP TextContent'
    import json
    from pathlib import Path
    from tests.driver_compact_fixtures import canonical
    from src.evidence_output import expand_compact_result
    p = report['presentation']
    assert p['strict_roundtrip'] and p['statement_roundtrip']
    assert p['codec_extra_analysis_calls'] == 0
    assert p['bytes']['compact'] <= .35 * p['bytes']['minified']
    for name, key in [('full', 'pretty'), ('compact', 'compact')]:
        text = Path(p['captures'][name]).read_text()
        assert len(text) == p['characters'][key]
        assert len(text.encode()) == p['bytes'][key]
    assert p['counts']['chain_locations'] == 512 and p['counts']['provenance_locations'] == 640
    wire = report['wire']
    assert wire['client_status'] == 'not_verified'
    assert wire['samples'][0]['frontend_launch_count'] >= 1
    assert wire['samples'][1]['frontend_launch_count'] == 0
    assert all(row['artifact_attempt_count'] >= 1 for row in wire['samples'])
    full = json.loads(Path(wire['presentation']['captures']['full']).read_text())
    restored = expand_compact_result(Path(wire['presentation']['captures']['compact']).read_text())
    assert canonical(restored) == canonical(full)
    missing = [row for row in restored['bit_provenance'] if 5 in row['target_bits']]
    assert missing and all(row['resolution'] == 'unknown' for row in missing)
    assert all('hierarchy_projection_scoped' in row['reason_codes'] for row in missing)
    assert not restored['claim_semantics']['negative_claim_allowed']
    assert all(full.get(key) for key in ('backend_status', 'hierarchy_recovery', 'dependency_context', 'wave_design_binding'))
    assert wire['samples'][-1]['text_content_bytes'] == wire['presentation']['bytes']['compact']


def test_unique_guards_and_low_codec_budget_remain_measurable(tmp_path):
    import json
    from pathlib import Path
    from scripts.driver_compact_measurement import guards_source, short_path_model, measure_presentation
    from src.driver_evidence_codec import EncodeLimits
    report = measure_presentation(short_path_model(guards_source(3), 't.q'), tmp_path,
                                  repeats=1, memory=False, limits=EncodeLimits(memo_bytes=0))
    assert report['counts']['chain_groups'] == 4
    assert report['codec_extra_analysis_calls'] == 0
    wire = json.loads(Path(report['captures']['compact']).read_text())
    assert not any(wire['evidence_tables'].values())
    assert report['strict_roundtrip'] and report['statement_roundtrip']


def test_memory_peak_does_not_use_inherited_fork_exec_high_water(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from scripts import driver_compact_measurement as measurement
    from tests.driver_compact_fixtures import driver_fixture
    path = tmp_path / 'public.json'
    path.write_text(driver_fixture('fixed')[0].model_dump_json(exclude_none=True))
    monkeypatch.setattr(measurement.resource, 'getrusage', lambda *_: SimpleNamespace(ru_maxrss=10**12))
    result = measurement.memory_sample(path, 'compact', measurement.EncodeLimits())
    assert result['process_peak_rss_kib'] < 10**12
    assert result['process_peak_rss_kib'] >= result['input_resident_rss_kib']


def test_wire_benchmark_keeps_actual_fallback_under_depth_budget(tmp_path):
    report = asyncio.run(baseline.measure(FIXTURE, False, 1, query_limits={'max_depth': 0},
        presentation_repeats=1, memory=False, output_directory=tmp_path))
    assert report['presentation']['strict_roundtrip'] and report['wire']['presentation']['strict_roundtrip']
    assert all(row['actual_backend'] in {'source_graph', 'static'} for row in report['wire']['samples'])
