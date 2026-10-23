"""Regression cache ownership, invalidation, publication and concurrency."""
import concurrent.futures
import json
from pathlib import Path
import sys
import time

import pytest
from scripts.test_fixtures import FixtureCache, run


def test_identical_consumers_serialize_and_corruption_rebuilds(tmp_path):
    cache = FixtureCache(tmp_path/'cache')
    calls = []
    def build(path):
        calls.append(path)
        time.sleep(.02)
        (path/'output').write_text('expected')
        return {'records': 12}
    def consume():
        return cache.get('small', {'version': 1}, build)
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        paths = list(pool.map(lambda _: consume(), range(2)))
    assert paths[0] == paths[1] and len(calls) == 1
    (paths[0]/'output').write_text('corrupt')
    consume()
    assert len(calls) == 2
    (paths[0]/'manifest.json').write_text('broken json')
    consume()
    assert len(calls) == 3
    cache.get('small', {'version': 2}, build)
    assert len(calls) == 4


def test_failure_and_cancel_never_publish_or_clean_user_data(tmp_path):
    user = tmp_path/'user'; user.mkdir(); (user/'keep').write_text('untouched')
    with pytest.raises(ValueError, match='ownership'):
        FixtureCache(user)
    cache = FixtureCache(tmp_path/'cache')
    def broken(path):
        (path/'diagnostic.log').write_text('partial')
        raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        cache.get('interrupted', {}, broken)
    assert not list(cache.root.rglob('manifest.json'))
    assert (user/'keep').read_text() == 'untouched'
    assert list(cache.root.rglob('diagnostic.log'))


def test_subprocess_errors_timeout_and_diagnostics(tmp_path):
    with pytest.raises(RuntimeError, match='exited 3'):
        run([sys.executable, '-c', 'raise SystemExit(3)'], tmp_path)
    import subprocess
    with pytest.raises(subprocess.TimeoutExpired):
        run([sys.executable, '-c', 'import time; time.sleep(30)'], tmp_path, timeout=.05)
    assert (tmp_path/'build.log').is_file()


def test_untrusted_manifest_cannot_validate_outside_cache(tmp_path):
    cache = FixtureCache(tmp_path/'cache')
    calls = []
    def build(path):
        calls.append(1); (path/'data').write_text('good'); return {}
    p = cache.get('test', {}, build)
    manifest = json.loads((p/'manifest.json').read_text())
    manifest['artifacts']['../outside'] = '0'
    (p/'manifest.json').write_text(json.dumps(manifest))
    cache.get('test', {}, build)
    assert len(calls) == 2


def test_missing_override_fails_without_generating(tmp_path, monkeypatch):
    cache = FixtureCache(tmp_path/'cache')
    monkeypatch.setenv('TW_POINT_READ_FSDB', str(tmp_path/'missing.fsdb'))
    with pytest.raises(FileNotFoundError):
        cache.wave('TW_POINT_READ_FSDB', lambda: pytest.fail('must not fall back'))
    assert not list(cache.root.rglob('manifest.json'))


def test_concurrent_first_cache_users_share_owner_and_build(tmp_path):
    calls = []
    def consume(_):
        cache = FixtureCache(tmp_path/'cache')
        def build(path):
            calls.append(1); (path/'data').write_text('complete'); return {}
        return cache.get('shared', {}, build)
    with concurrent.futures.ThreadPoolExecutor(4) as pool:
        results = list(pool.map(consume, range(4)))
    assert len(calls) == 1 and len(set(results)) == 1


def test_termination_stops_owned_generator_without_publishing(tmp_path):
    import os
    import subprocess
    root = Path(__file__).resolve().parents[1]
    child = 'import os,time,pathlib; pathlib.Path("child.pid").write_text(str(os.getpid())); time.sleep(30)'
    program = ('from scripts.test_fixtures import FixtureCache,run; import sys; '
               f'cache=FixtureCache({str(tmp_path/"cache")!r}); '
               f'cache.get("cancel", {{}}, lambda p: run([sys.executable,"-c",{child!r}],p))')
    proc = subprocess.Popen([sys.executable, '-c', program], cwd=root,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic()+5
        while time.monotonic() < deadline and not list(tmp_path.rglob('child.pid')):
            time.sleep(.01)
        pidfile, = tmp_path.rglob('child.pid')
        child_pid = int(pidfile.read_text())
        proc.terminate(); proc.wait(timeout=10)
        assert proc.returncode != 0
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
        assert not list(tmp_path.rglob('manifest.json'))
    finally:
        if proc.poll() is None:
            proc.kill(); proc.wait()
