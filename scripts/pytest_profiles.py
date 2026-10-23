"""Pytest selection/report plugin, also used by a plain pytest invocation."""
from __future__ import annotations

import json
import os
from pathlib import Path
import time
from collections import Counter

import pytest
from scripts.test_profiles import PROFILES, allowed_skip, membership


def pytest_addoption(parser):
    group = parser.getgroup('traceweave regression')
    group.addoption('--tw-profile', choices=tuple(PROFILES), default='portable')
    group.addoption('--tw-report', type=str)
    group.addoption('--tw-plan-only', action='store_true')
    group.addoption('--tw-cache-dir', type=str)
    group.addoption('--tw-external-representative', type=str)


def pytest_configure(config):
    config._tw_started = time.monotonic()
    config._tw_report = {'profile': config.getoption('--tw-profile'),
                         'selected': [], 'excluded': [], 'outcomes': [],
                         'excluded_modules': [], 'phase_times_sec': {},
                         'collection_issues': [], 'expected_skips': [], 'unexpected_skips': []}
    for name in ('TRACEWEAVE_RUN_EDA_INTEGRATION', 'TRACEWEAVE_TEST_NPI_DYNAMIC',
                 'TRACEWEAVE_TEST_EXPRESSION_ORACLE'):
        os.environ[name] = '1' if config.getoption('--tw-profile') in ('eda', 'all') else '0'


def pytest_ignore_collect(collection_path, config):
    if config.getoption('--tw-profile') != 'stress' or not collection_path.name.startswith('test_'):
        return None
    from scripts.test_profiles import CATALOG
    names = {Path(k.split('::')[0]).name for k, v in CATALOG.items() if v == 'stress'}
    if config.getoption('--tw-external-representative'):
        names.add('test_slang_hierarchy_representative.py')
    if collection_path.suffix == '.py' and collection_path.name not in names:
        config._tw_report['excluded_modules'].append(str(collection_path))
        return True
    return None


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    selected, excluded, layers = [], [], {}
    profile = config.getoption('--tw-profile')
    for item in items:
        layer = membership(item.nodeid, fixtures=item.fixturenames,
                           eda_marker=item.get_closest_marker('eda_integration') is not None)
        entry = {'nodeid': item.nodeid, 'layer': layer, 'fixtures': sorted(item.fixturenames)}
        layers[item.nodeid] = layer
        if layer in PROFILES[profile] or (layer == 'external' and config.getoption('--tw-external-representative')):
            selected.append(item)
            config._tw_report['selected'].append(entry)
        else:
            excluded.append(item)
    # Run the three layers serially, each node exactly once.
    selected.sort(key=lambda item: ('portable', 'eda', 'stress', 'external').index(layers[item.nodeid]))
    items[:] = selected
    config.hook.pytest_deselected(items=excluded)


def pytest_deselected(items):
    if _active is None:
        return
    for item in items:
        layer = membership(item.nodeid, fixtures=item.fixturenames,
                           eda_marker=item.get_closest_marker('eda_integration') is not None)
        _active._tw_report['excluded'].append(dict(nodeid=item.nodeid, layer=layer,
            reason='pytest_filter' if layer in PROFILES[_active.getoption('--tw-profile')] else 'outside_profile'))


def pytest_collectreport(report):
    # Pytest does not pass config to this hook. The session hook installs it.
    if _active is not None and (report.failed or report.skipped):
        _active._tw_report['collection_issues'].append(
            {'nodeid': report.nodeid, 'outcome': report.outcome, 'reason': str(report.longrepr)})


_active = None


def pytest_sessionstart(session):
    global _active
    _active = session.config


def pytest_collection_finish(session):
    config = session.config
    if config.option.collectonly or config.getoption('--tw-plan-only'):
        return
    from scripts.test_fixtures import prepare_for_tests
    try:
        start = time.monotonic()
        config._tw_report['fixtures'] = prepare_for_tests(
            config._tw_report['selected'], config.getoption('--tw-cache-dir'))
    except Exception as exc:
        config._tw_report['preparation_error'] = str(exc)
        raise pytest.UsageError('fixture preparation failed: ' + str(exc)) from exc
    finally:
        config._tw_report['fixture_preparation_sec'] = time.monotonic()-start


def pytest_runtest_logreport(report):
    if _active is None:
        return
    result = _active._tw_report
    times = result['phase_times_sec']
    times[report.when] = times.get(report.when, 0) + report.duration
    if report.when == 'call' or report.failed or report.skipped:
        result['outcomes'].append({'nodeid': report.nodeid, 'when': report.when,
                                   'outcome': report.outcome, 'duration_sec': report.duration})
    if report.skipped:
        reason = str(report.longrepr[2] if isinstance(report.longrepr, tuple) else report.longrepr).removeprefix('Skipped: ')
        key = 'expected_skips' if allowed_skip(report.nodeid, reason) else 'unexpected_skips'
        result[key].append({'nodeid': report.nodeid, 'reason': reason})


def pytest_sessionfinish(session, exitstatus):
    global _active
    config = session.config
    result = config._tw_report
    planning = config.option.collectonly or config.getoption('--tw-plan-only')
    executed = {r['nodeid'] for r in result['outcomes'] if r['when'] == 'call'}
    expected = {r['nodeid'] for r in result['expected_skips']}
    result['unexecuted'] = sorted(r['nodeid'] for r in result['selected'] if r['nodeid'] not in executed | expected)
    result['summary'] = dict(selected=len(result['selected']), excluded=len(result['excluded']),
        executed=len(executed), expected_skips=len(expected), unexpected_skips=len(result['unexpected_skips']),
        collection_issues=len(result['collection_issues']),
        call_outcomes=dict(Counter(r['outcome'] for r in result['outcomes'] if r['when'] == 'call')))
    result['layers'] = {layer: dict(selected=sum(r['layer'] == layer for r in result['selected']),
        executed=sum(r['layer'] == layer and r['nodeid'] in executed for r in result['selected']))
        for layer in ('portable', 'eda', 'stress', 'external')}
    incomplete = (not result['selected'] or result['unexecuted'] or result['unexpected_skips'] or result['collection_issues'])
    if incomplete and not planning and exitstatus == 0:
        session.exitstatus = 1
    result.update(status='planned' if planning else 'passed' if session.exitstatus == 0 else 'incomplete',
                  exit_code=int(session.exitstatus), duration_sec=time.monotonic() - config._tw_started)
    destination = config.getoption('--tw-report')
    if destination:
        Path(destination).write_text(json.dumps(result, indent=2) + '\n')
    _active = None


def pytest_terminal_summary(terminalreporter):
    r = terminalreporter.config._tw_report
    terminalreporter.write_sep('-', f"TraceWeave {r['profile']}: {len(r['selected'])} selected, "
                                  f"{len(r['excluded'])} excluded; {len(r['unexpected_skips'])} unexpected skips")
