"""Small immutable facts for on-demand hierarchy/source queries.

These records deliberately do not require a full hierarchy dictionary. A scope
may be a real generate block without a module definition; its parent is supplied
by the semantic provider, never inferred by splitting a dotted name. Frontend
objects and native handles must not cross this boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re
import time
from typing import Literal

from .cancellation import check_cancelled
from .connectivity_ir import SourceLocation


HIERARCHY_SOURCE_VERSION = 1
HIERARCHY_TOOL_SCHEMA_VERSION = 2
FactsOrigin = Literal["verdi_npi", "slang", "static_inference"]
ScopeKind = Literal["module", "interface", "program", "generate", "instance_array"]


def semantic_fact_id(origin: str, identity: str, kind: str, key: str) -> str:
    """Provider/artifact-local ID; existing IR IDs may be supplied unchanged."""
    if not all(isinstance(item, str) and item for item in (origin, identity, kind, key)):
        raise ValueError("fact identity components must be nonempty strings")
    encoded = json.dumps([origin, identity, kind, key], ensure_ascii=True).encode()
    return "hs_" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class HierarchyScope:
    scope_id: str
    path: str
    name: str
    kind: ScopeKind
    parent_path: str | None
    definition_id: str | None = None
    definition_name: str | None = None
    location: SourceLocation | None = None
    definition_location: SourceLocation | None = None
    parameterization: tuple[tuple[str, str], ...] = ()
    gaps: tuple[str, ...] = ()

    def __post_init__(self):
        if not all(isinstance(value, str) and value for value in (self.scope_id, self.path, self.name)):
            raise ValueError("scope ID, path and name must be nonempty strings")
        if self.kind not in {"module", "interface", "program", "generate", "instance_array"}:
            raise ValueError("unsupported hierarchy scope kind")
        if self.parent_path is not None and (
            not isinstance(self.parent_path, str) or not self.parent_path or self.parent_path == self.path
        ):
            raise ValueError("invalid scope parent")
        if (self.definition_id is None) != (self.definition_name is None):
            raise ValueError("definition identity and name must be supplied together")
        if any(value is not None and (not isinstance(value, str) or not value)
               for value in (self.definition_id, self.definition_name)):
            raise ValueError("definition identity and name must be nonempty")
        if any(value is not None and not isinstance(value, SourceLocation)
               for value in (self.location, self.definition_location)):
            raise ValueError("scope locations must be immutable source locations")
        if any(not isinstance(pair, (list, tuple)) for pair in self.parameterization):
            raise ValueError("invalid parameter evidence")
        parameters = tuple(tuple(pair) for pair in self.parameterization)
        if any(len(pair) != 2 or not all(isinstance(x, str) for x in pair) or not pair[0]
               for pair in parameters):
            raise ValueError("invalid parameter evidence")
        if len({pair[0] for pair in parameters}) != len(parameters):
            raise ValueError("duplicate parameter evidence")
        object.__setattr__(self, "parameterization", parameters)
        if any(not isinstance(gap, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", gap) for gap in self.gaps):
            raise ValueError("gaps must be fixed labels")
        object.__setattr__(self, "gaps", tuple(sorted(set(self.gaps))))

    def row(self) -> dict:
        """A fresh public-compatible row; locations keep their different roles."""
        return {
            "scope_id": self.scope_id, "path": self.path, "inst": self.name,
            "kind": self.kind, "parent": self.parent_path,
            "definition_id": self.definition_id, "module": self.definition_name,
            "source_file": self.location.file if self.location else None,
            "source_line": self.location.line if self.location else None,
            "definition_file": self.definition_location.file if self.definition_location else None,
            "definition_line": self.definition_location.line if self.definition_location else None,
            "parameterization": dict(self.parameterization), "gaps": list(self.gaps),
        }


@dataclass(frozen=True)
class ScopeLookup:
    fact: HierarchyScope | None
    complete: bool
    gaps: tuple[str, ...] = ()

    def __post_init__(self):
        if self.fact is not None and not isinstance(self.fact, HierarchyScope):
            raise ValueError("lookup must contain an immutable scope fact")
        if not isinstance(self.complete, bool):
            raise ValueError("lookup completeness must be explicit")
        if any(not isinstance(gap, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", gap) for gap in self.gaps):
            raise ValueError("gaps must be fixed labels")
        object.__setattr__(self, "gaps", tuple(sorted(set(self.gaps))))


class HierarchyBudgetExceeded(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class HierarchyQueryBudget:
    """One request's work limits, including provider calls and returned facts.

    Providers checkpoint around native calls and charge each call separately.
    A synchronous native call cannot be interrupted midway; a late completion
    is rejected at the following checkpoint. Cleanup must run without spending
    this exhausted budget. This is not a bound on a frontend's own allocations.
    """

    def __init__(self, *, timeout_sec: float = 30, max_scopes: int = 10000,
                 max_calls: int = 100000, max_bytes: int = 8 * 1024 * 1024,
                 max_depth: int = 256, max_symbols: int = 10000, max_files: int = 10000):
        if isinstance(timeout_sec, bool) or not isinstance(timeout_sec, (int, float)) or not math.isfinite(timeout_sec) or not 0 < timeout_sec <= 300:
            raise ValueError("timeout_sec must be finite and in (0, 300]")
        for name, value, ceiling in (("max_scopes", max_scopes, 100000),
                                    ("max_calls", max_calls, 1000000),
                                    ("max_bytes", max_bytes, 64 * 1024 * 1024),
                                    ("max_depth", max_depth, 256),
                                    ("max_symbols", max_symbols, 100000),
                                    ("max_files", max_files, 100000)):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= ceiling:
                raise ValueError(f"invalid {name}")
        self.started = time.monotonic()
        self.deadline = self.started + timeout_sec
        self.max_scopes, self.max_calls = max_scopes, max_calls
        self.max_bytes, self.max_depth = max_bytes, max_depth
        self.max_symbols, self.symbol_count = max_symbols, 0
        self.max_files, self.file_count = max_files, 0
        self.scope_count = self.call_count = self.fact_bytes = 0

    def checkpoint(self):
        check_cancelled()
        if time.monotonic() >= self.deadline:
            raise HierarchyBudgetExceeded("timeout_sec")

    def call(self):
        self.checkpoint()
        if self.call_count >= self.max_calls:
            raise HierarchyBudgetExceeded("native_call_limit")
        self.call_count += 1

    def admit(self, fact: HierarchyScope, depth: int):
        self.checkpoint()
        if depth > self.max_depth:
            raise HierarchyBudgetExceeded("scope_depth_limit")
        if self.scope_count >= self.max_scopes:
            raise HierarchyBudgetExceeded("scope_limit")
        # Conservative retained-fact estimate, charged even for undisplayed
        # scopes during an explicit exact count. No source body is retained.
        size = 512 + 4 * len(json.dumps(fact.row(), ensure_ascii=True))
        if self.fact_bytes + size > self.max_bytes:
            raise HierarchyBudgetExceeded("fact_byte_limit")
        self.scope_count += 1
        self.fact_bytes += size

    def metrics(self) -> dict:
        return {"scope_count": self.scope_count, "native_call_count": self.call_count,
                "symbol_count": self.symbol_count,
                "file_count": self.file_count,
                "estimated_fact_bytes": self.fact_bytes,
                "query_ms": (time.monotonic() - self.started) * 1000}

    def admit_symbol(self, fact):
        """Charge a copied declaration, including any resolved base reference."""
        self.checkpoint()
        if self.symbol_count >= self.max_symbols:
            raise HierarchyBudgetExceeded("symbol_limit")
        size = 512 + 4 * len(json.dumps(fact.row(), ensure_ascii=True))
        if self.fact_bytes + size > self.max_bytes:
            raise HierarchyBudgetExceeded("fact_byte_limit")
        self.symbol_count += 1
        self.fact_bytes += size

    def admit_namespace(self, kind: str, path: str, depth: int):
        """Charge an inspected namespace even when it contains no declarations.

        Class/file inventories can walk semantic scopes without materializing
        instance facts. Their visited identities still consume scope and byte
        budgets; an empty declaration stream is not free work.
        """
        self.checkpoint()
        if depth > self.max_depth:
            raise HierarchyBudgetExceeded("scope_depth_limit")
        if self.scope_count >= self.max_scopes:
            raise HierarchyBudgetExceeded("scope_limit")
        size = 256 + 4 * len(json.dumps([kind, path], ensure_ascii=True))
        if self.fact_bytes + size > self.max_bytes:
            raise HierarchyBudgetExceeded("fact_byte_limit")
        self.scope_count += 1
        self.fact_bytes += size

    def admit_file(self, fact):
        """Charge a copied compile-file record in a source query."""
        self.checkpoint()
        if self.file_count >= self.max_files:
            raise HierarchyBudgetExceeded("file_limit")
        size = 512 + 4 * len(json.dumps(fact.row(), ensure_ascii=True))
        if self.fact_bytes + size > self.max_bytes:
            raise HierarchyBudgetExceeded("fact_byte_limit")
        self.file_count += 1
        self.fact_bytes += size

    def admit_payload(self, value):
        """Charge a copied metadata export without inventing semantic facts."""
        self.checkpoint()
        size = 512 + 4 * len(json.dumps(value, ensure_ascii=True))
        self.reserve_bytes(size)

    def reserve_bytes(self, size):
        """Reserve an existing conservative metadata/fact allocation estimate."""
        self.checkpoint()
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError("invalid retained byte estimate")
        if self.fact_bytes + size > self.max_bytes:
            raise HierarchyBudgetExceeded("fact_byte_limit")
        self.fact_bytes += size
