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
