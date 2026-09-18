"""Content identity shared by structural products, without an HDL frontend.

Only digest/stat/include facts survive a call. Bodies remain owned by the
transient compile-source index. Literal includes are resolved on every capture
and validation so an earlier search candidate cannot silently shadow a hit.
Lexical input completeness is separate from semantic include completeness:
the current regex scanner consumes only its explicit source texts. A lexical
hit must never be promoted to proof of a reusable semantic compilation.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
import os
import re
import threading

from .cancellation import check_cancelled
from .compile_session_snapshot import FileContentSnapshot, read_source_content
from .source_graph_adapter import _compile_evidence_support_paths, _include_support_paths
from .sv_preprocessor import extract_preprocessor_options

IDENTITY_VERSION = "1"
_INCLUDE = re.compile(r'^\s*`include\s+([^\r\n]+)', re.MULTILINE)


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _resolve(name: str, directories: tuple[str, ...]) -> str | None:
    for directory in directories:
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate):
            return os.path.realpath(candidate)
    return None


@dataclass(frozen=True)
class DesignIdentity:
    digest: str
    records: tuple[tuple[str, FileContentSnapshot], ...]
    includes: tuple[tuple[str, tuple[str, ...], str | None], ...]
    issues: tuple[str, ...]
    include_resolution_complete: bool = True

    @property
    def complete(self) -> bool:
        return not self.issues and bool(self.records)

    def current(self) -> bool:
        if not self.complete:
            return False
        for path, record in self.records:
            check_cancelled()
            if os.path.realpath(path) != record.path or not record.current():
                return False
        return all(_resolve(name, dirs) == chosen for name, dirs, chosen in self.includes)


class DesignIdentityReader:
    """Bounded digest memoization; stat validates previously hashed bytes."""

    def __init__(self, max_entries: int = 32768, max_bytes: int = 16 * 1024 * 1024):
        self._facts: OrderedDict[str, tuple] = OrderedDict()
        self._lock = threading.Lock()
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._bytes = 0

    def _read(self, path, reader):
        canonical = os.path.realpath(path)
        with self._lock:
            fact = self._facts.get(canonical)
            if fact and fact[0].current():
                self._facts.move_to_end(canonical)
                return fact[:3]
        content = reader(path)
        includes = tuple(m.group(1).strip() for m in _INCLUDE.finditer(content.text))
        record = content.snapshot
        if record is not None and not content.issue_codes and record.current():
            size = 1024 + len(canonical.encode()) * 2 + sum(len(s.encode()) * 4 + 64 for s in includes)
            with self._lock:
                old = self._facts.pop(canonical, None)
                if old:
                    self._bytes -= old[3]
                if size <= self.max_bytes:
                    self._facts[canonical] = (record, includes, content.issue_codes, size)
                    self._bytes += size
                while self._facts and (len(self._facts) > self.max_entries or self._bytes > self.max_bytes):
                    self._bytes -= self._facts.popitem(last=False)[1][3]
        return record, includes, content.issue_codes

    def capture(self, compile_log: str, compile_result: dict, *, reader=read_source_content) -> DesignIdentity:
        check_cancelled()
        inputs = [str(f["path"]) for f in compile_result.get("files", {}).get("user", []) if f.get("path")]
        issues = set()
        includes_complete = True
        if not inputs or compile_result.get("parse_warnings"):
            issues.add("compile_inputs_incomplete")
        options = extract_preprocessor_options(compile_result, inputs[0] if inputs else compile_log)
        if not options.complete:
            issues.add("compile_options_incomplete")
        support = _compile_evidence_support_paths(compile_result) | _include_support_paths(compile_result)
        pending = list(dict.fromkeys([compile_log, *inputs, *sorted(map(str, support))]))
        seen = set()
        records = []
        resolutions = []
        total_bytes = 0
        while pending:
            check_cancelled()
            path = pending.pop()
            if path in seen:
                continue
            seen.add(path)
            if len(seen) > 32768 or total_bytes > 128 * 1024 * 1024:
                issues.add("identity_budget_exceeded")
                break
            try:
                if os.stat(path).st_size + total_bytes > 128 * 1024 * 1024:
                    issues.add("identity_budget_exceeded")
                    break
                record, includes, read_issues = self._read(path, reader)
            except OSError:
                issues.add("input_unavailable")
                continue
            issues.update(read_issues)
            if record is None:
                issues.add("input_unavailable")
                continue
            records.append((path, record))
            total_bytes += record.size
            directories = (os.path.dirname(os.path.abspath(path)), *options.include_dirs)
            for directive in includes:
                match = re.match(r'"([^"\n]+)"', directive)
                if match is None:
                    includes_complete = False
                    continue
                name = match.group(1)
                chosen = _resolve(name, directories)
                resolutions.append((name, directories, chosen))
                if chosen is None:
                    includes_complete = False
                else:
                    pending.append(chosen)
        records.sort(key=lambda item: item[0])
        digest = _digest({
            "version": IDENTITY_VERSION, "context": compile_result,
            "options": [options.macros, options.include_dirs],
            "content": [(p, r.path, r.sha256) for p, r in records],
            "include_resolution": resolutions,
        })
        result = DesignIdentity(digest, tuple(records), tuple(resolutions), tuple(sorted(issues)), includes_complete)
        if result.complete and not result.current():
            return DesignIdentity(digest, result.records, result.includes, ("input_changed_during_capture",), includes_complete)
        return result
