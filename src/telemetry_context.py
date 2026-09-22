"""Bounded, process-local artifact attribution. Private identities never leave here.

Discovery registers exact files, not directory-wide ownership. Stat identities
detect ordinary updates/replacements; they are not content hashes or proof of
waveform semantics. Unknown and conflicting claims never use a latest session.
"""

from dataclasses import dataclass, field
import os
import stat
import threading
import uuid


DOMAINS = frozenset({"simulation", "formal", "unknown"})
STATUSES = frozenset({
    "matched", "unresolved", "ambiguous", "artifact_changed", "multiple_owners",
    "capacity", "unavailable",
})


def field_value(obj, name, default=None):
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def _snapshot(path):
    canonical = os.path.realpath(path)
    info = os.stat(canonical)
    if not stat.S_ISREG(info.st_mode):
        raise OSError("not_regular_file")
    return canonical, (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@dataclass(frozen=True)
class Attribution:
    domain: str = "unknown"
    status: str = "unresolved"
    session_id: str | None = None
    _checks: tuple = field(default=(), repr=False)

    def finish(self):
        """A concurrent discovery cannot reassign a request already in flight."""
        for path, identity in self._checks:
            try:
                if _snapshot(path) == identity:
                    continue
            except OSError:
                pass
            return Attribution(status="artifact_changed")
        return self

    def public_fields(self):
        # Independently constrain everything persisted, even for direct callers.
        if (not isinstance(self.domain, str) or self.domain not in DOMAINS
                or not isinstance(self.status, str) or self.status not in STATUSES):
            return Attribution().public_fields()
        sid = self.session_id
        if sid is not None and (
            not isinstance(sid, str) or len(sid) != 32
            or any(c not in "0123456789abcdef" for c in sid)
        ):
            return Attribution().public_fields()
        if self.status != "matched" or self.domain == "unknown":
            sid = None
        return {"artifact_domain": self.domain, "attribution_status": self.status, "session_id": sid}


@dataclass
class _Owner:
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    files: dict = field(default_factory=dict)


class ArtifactRegistry:
    """Fail closed at capacity: eviction must not erase an ambiguous claim.

    The caps apply to all retained owners/bindings, including old generations.
    At capacity, release the maps and stay unknown until the server restarts.
    """

    def __init__(self, max_owners=128, max_files=4096):
        self._owners = {}
        self._max_owners, self._max_files = max_owners, max_files
        self._saturated = False
        self._lock = threading.Lock()

    def _capacity(self):
        self._owners.clear()
        self._saturated = True
        return Attribution(status="capacity")

    def discover(self, tool, result):
        domain = "formal" if tool == "get_formal_paths" else "simulation"
        root = field_value(result, "formal_root" if domain == "formal" else "case_dir")
        if not root:
            return Attribution(domain=domain)
        root = os.path.realpath(root)
        roots = {root}
        if domain == "formal":
            roots = {os.path.realpath(field_value(p, "path")) for p in field_value(result, "projects", [])}
            selected = field_value(result, "selected_project_dir")
            if selected:
                roots.add(os.path.realpath(selected))
            roots = roots or {root}
        with self._lock:
            if self._saturated:
                return Attribution(status="capacity")
            if len(roots) > self._max_owners:
                return self._capacity()
            groups = {(domain, path): {} for path in roots}
            roles = ("formal_logs", "wave_files") if domain == "formal" else (
                "compile_logs", "sim_logs", "wave_files"
            )
            examined = 0
            for role in roles:
                for entry in field_value(result, role, []):
                    examined += 1
                    if examined > self._max_files:
                        return self._capacity()
                    try:
                        path, version = _snapshot(field_value(entry, "path"))
                    except OSError:
                        continue
                    enclosing = [p for p in roots if os.path.commonpath((p, path)) == p]
                    owner = max(enclosing, key=len) if enclosing else root
                    groups.setdefault((domain, owner), {})[path] = version
            if len(self._owners.keys() | groups.keys()) > self._max_owners:
                return self._capacity()
            for key, files in groups.items():
                owner = self._owners.setdefault(key, _Owner())
                if any(p in owner.files and owner.files[p] != v for p, v in files.items()):
                    owner = self._owners[key] = _Owner()
                owner.files.update(files)
            if sum(len(o.files) for o in self._owners.values()) > self._max_files:
                return self._capacity()
            if len(groups) != 1:
                return Attribution(domain=domain, status="multiple_owners")
            return Attribution(domain, "matched", self._owners[next(iter(groups))].session_id)

    def capture(self, arguments):
        # A wave query may also carry compile context. The requested wave owns
        # this attribution; never let the most recent compile context replace it.
        paths = [arguments[k] for k in ("wave_path", "wave_path_a", "wave_path_b") if arguments.get(k)]
        if not paths:
            paths = [arguments[k] for k in (
                "log_path", "compile_log", "base_log_path", "new_log_path",
            ) if arguments.get(k)]
        with self._lock:
            if self._saturated:
                return Attribution(status="capacity")
            if not paths:
                return Attribution()
            checks, matched = [], set()
            for requested in paths:
                try:
                    path, version = _snapshot(requested)
                except OSError:
                    return Attribution(status="unavailable")
                claims = {key for key, owner in self._owners.items() if owner.files.get(path) == version}
                if not claims:
                    changed = any(path in o.files for o in self._owners.values())
                    return Attribution(status="artifact_changed" if changed else "unresolved")
                if len(claims) != 1:
                    return Attribution(status="ambiguous")
                matched.update(claims)
                checks.append((requested, (path, version)))
            if len(matched) != 1:
                return Attribution(status="multiple_owners")
            key = matched.pop()
            return Attribution(key[0], "matched", self._owners[key].session_id, tuple(checks))
