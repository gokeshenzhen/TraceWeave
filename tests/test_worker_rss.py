"""Native RSS must describe this executable, including released transients."""
from __future__ import annotations

import io
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from src import source_graph_worker as worker


@pytest.mark.parametrize("status, expected", [
    ("VmRSS: 4096 kB\nVmHWM: 8192 kB\n", (4096, 8192)),
    ("VmHWM: 8192 kB\nVmRSS: 4096 kB\n", (4096, 8192)),
    ("VmRSS: 8192 kB\nVmHWM: 4096 kB\n", (8192, 8192)),
    ("VmRSS: bad kB\nVmHWM: 8192 kB\n", (None, 8192)),
])
def test_proc_address_space_peak_does_not_use_inherited_resource_peak(monkeypatch, status, expected):
    monkeypatch.setattr("builtins.open", lambda *args, **kwargs: io.StringIO(status))
    def forbidden(*args):
        raise AssertionError("trusted VmHWM must not use the inherited resource peak")
    monkeypatch.setattr(worker.resource, "getrusage", forbidden)
    assert worker._read_rss_kib() == expected


@pytest.mark.parametrize("status, current", [
    ("VmRSS: 4096 kB\n", 4096),
    ("VmRSS: 4096 kB\nVmHWM: -1 kB\n", 4096),
    ("VmRSS: 4096 kB\nVmHWM: 8192 MB\n", 4096),
    ("VmRSS: invalid kB\nVmHWM: invalid kB\n", None),
])
def test_missing_proc_peak_uses_conservative_resource_fallback(monkeypatch, status, current):
    monkeypatch.setattr("builtins.open", lambda *args, **kwargs: io.StringIO(status))
    monkeypatch.setattr(worker.sys, "platform", "linux")
    monkeypatch.setattr(worker.resource, "getrusage", lambda *args: SimpleNamespace(ru_maxrss=12345))
    assert worker._read_rss_kib() == (current, 12345)


def test_unavailable_rss_is_unknown_and_darwin_resource_units_are_bytes(monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError("no native observation")
    monkeypatch.setattr("builtins.open", unavailable)
    monkeypatch.setattr(worker.resource, "getrusage", unavailable)
    assert worker._read_rss_kib() == (None, None)
    monkeypatch.setattr(worker.sys, "platform", "darwin")
    monkeypatch.setattr(worker.resource, "getrusage", lambda *args: SimpleNamespace(ru_maxrss=4097))
    assert worker._read_rss_kib() == (None, 5)
    monkeypatch.setattr(worker.resource, "getrusage", lambda *args: SimpleNamespace(ru_maxrss=-1))
    assert worker._read_rss_kib() == (None, None)


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux /proc address-space high water marks")
def test_exec_child_excludes_real_parent_high_water_mark(tmp_path):
    root = Path(__file__).resolve().parents[1]
    evidence = tmp_path / "exec_rss.jsonl"
    child = r'''
import json, os, resource, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from src.source_graph_worker import _read_rss_kib
status_before = Path('/proc/self/status').read_text()
current, peak = _read_rss_kib()
record = {'phase':'child_after_exec', 'pid':os.getpid(),
          'stat':Path('/proc/self/stat').read_text(), 'status_before':status_before,
          'status_after':Path('/proc/self/status').read_text(),
          'current_kib':current, 'peak_kib':peak,
          'resource_peak_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}
with open(sys.argv[2], 'a') as stream: stream.write(json.dumps(record) + '\n')
'''
    parent = r'''
import gc, json, os, resource, sys
from pathlib import Path
def record(phase):
    with open(sys.argv[2], 'a') as stream:
        stream.write(json.dumps({'phase':phase, 'pid':os.getpid(),
            'stat':Path('/proc/self/stat').read_text(), 'status':Path('/proc/self/status').read_text(),
            'resource_peak_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}) + '\n')
record('parent_before')
memory = bytearray(192 * 1024 * 1024)
memory[::4096] = b'x' * len(memory[::4096])
record('parent_allocated')
del memory
gc.collect()
record('parent_released')
os.execv(sys.executable, [sys.executable, '-c', sys.argv[3], sys.argv[1], sys.argv[2]])
'''
    completed = subprocess.run([sys.executable, "-c", parent, str(root), str(evidence), child],
                               cwd=root, capture_output=True, text=True, timeout=20)
    (tmp_path / "exec_child_exit.json").write_text(json.dumps({
        "returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}))
    assert completed.returncode == 0, completed.stderr
    records = [json.loads(line) for line in evidence.read_text().splitlines()]
    assert [r["phase"] for r in records] == ["parent_before", "parent_allocated", "parent_released", "child_after_exec"]
    assert len({r["pid"] for r in records}) == 1
    def parent_current(record):
        return next(int(line.split()[1]) for line in record["status"].splitlines()
                    if line.startswith("VmRSS:"))
    assert parent_current(records[1]) > parent_current(records[0]) + 128 * 1024
    assert parent_current(records[2]) < parent_current(records[1]) - 128 * 1024
    inherited, actual = records[-2], records[-1]
    assert inherited["resource_peak_kib"] >= 192 * 1024
    assert actual["peak_kib"] >= actual["current_kib"] > 0
    assert actual["peak_kib"] < inherited["resource_peak_kib"] - 64 * 1024
    assert actual["resource_peak_kib"] >= inherited["resource_peak_kib"]
    assert actual["peak_kib"] < actual["resource_peak_kib"]
