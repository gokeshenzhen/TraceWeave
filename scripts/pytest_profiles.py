"""Pytest selection/report plugin, also used by a plain pytest invocation."""
from __future__ import annotations

import json
import os
from pathlib import Path
import time

import pytest
from scripts.test_profiles import PROFILES, allowed_skip, membership


def pytest_addoption(parser):
    group = parser.getgroup('traceweave regression')
    group.addoption('--tw-profile', choices=tuple(PROFILES), default='portable')
    group.addoption('--tw-report', type=str)
    group.addoption('--tw-plan-only', action='store_true')
    group.addoption('--tw-cache-dir', type=str)


def pytest_configure(config):
    config._tw_started = time.monotonic()
    config._tw_report = {'profile': config.getoption('--tw-profile'),
                         'selected': [], 'excluded': [], 'outcomes': [],
                         'collection_issues': [], 'expected_skips': [], 'unexpected_skips': []}
    for name in ('TRACEWEAVE_RUN_EDA_INTEGRATION', 'TRACEWEAVE_TEST_NPI_DYNAMIC',
                 'TRACEWEAVE_TEST_EXPRESSION_ORACLE'):
        os.environ[name] = '1' if config.getoption('--tw-profile') in ('eda', 'all') else '0'


def pytest_collection_modifyitems(config, items):
    selected, excluded, layers = [], [], {}
    profile = config.getoption('--tw-profile')
    for item in items:
        layer = membership(item.nodeid, fixtures=item.fixturenames,
                           eda_marker=item.get_closest_marker('eda_integration') is not None)
        entry = {'nodeid': item.nodeid, 'layer': layer}
        layers[item.nodeid] = layer
        if layer in PROFILES[profile]:
            selected.append(item)
            config._tw_report['selected'].append(entry)
        else:
            excluded.append(item)
            config._tw_report['excluded'].append(entry)
    # Run the three layers serially, each node exactly once.
    selected.sort(key=lambda item: ('portable', 'eda', 'stress').index(layers[item.nodeid]))
    items[:] = selected
    config.hook.pytest_deselected(items=excluded)


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
        config._tw_report['fixtures'] = prepare_for_tests(
            config._tw_report['selected'], config.getoption('--tw-cache-dir'))
    except Exception as exc:
        config._tw_report['preparation_error'] = str(exc)
        raise pytest.UsageError('fixture preparation failed: ' + str(exc)) from exc


def pytest_runtest_logreport(report):
    if _active is None:
        return
    result = _active._tw_report
    if report.when == 'call' or report.failed or report.skipped:
        result['outcomes'].append({'nodeid': report.nodeid, 'when': report.when,
                                   'outcome': report.outcome, 'duration_sec': report.duration})
    if report.skipped:
        reason = str(report.longrepr[2]).removeprefix('Skipped: ')
        key = 'expected_skips' if allowed_skip(report.nodeid, reason) else 'unexpected_skips'
        result[key].append({'nodeid': report.nodeid, 'reason': reason})


def pytest_sessionfinish(session, exitstatus):
    global _active
    config = session.config
    result = config._tw_report
    planning = config.option.collectonly or config.getoption('--tw-plan-only')
    incomplete = (not result['selected'] or result['unexpected_skips'] or result['collection_issues'])
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
