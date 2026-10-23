"""Language-model hierarchy access inside one exclusive native query owner.

NPI's high-level relationship getters eagerly build lists. This adapter uses
iterate/scan directly, closes an unfinished iterator exactly once, and copies
ordinary facts before clearing all Language handles at the owner boundary.
It must run synchronously on the thread that owns the loaded NPI design; no
Language handles may be held by another query or retained after this owner.
"""
from __future__ import annotations

from contextlib import contextmanager
import sys
import threading
import time

from .connectivity_ir import SourceLocation
from .hierarchy_source_ir import HierarchyBudgetExceeded, HierarchyScope, ScopeLookup, semantic_fact_id
from .hierarchy_source_symbols import ClassFact, SourceSymbol


_KINDS = {"npiModule": "module", "npiInterface": "interface", "npiProgram": "program",
          "npiGenScope": "generate", "npiModuleArray": "instance_array",
          "npiInterfaceArray": "instance_array", "npiProgramArray": "instance_array"}
_NON_INSTANCE_SCOPES = {"npiClassDefn", "npiTask", "npiFunction", "npiNamedBegin", "npiNamedFork"}
_NAMESPACES = dict(_KINDS, npiPackage="package", npiClassDefn="class", npiTask="task",
                   npiFunction="function", npiNamedBegin="named_begin", npiNamedFork="named_fork")
_OWNER_LOCK = threading.Lock()


class NpiHierarchyUnavailable(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class _LanguageIterator:
    def __init__(self, owner, relation, reference):
        self.owner, self.raw, self.exhausted = owner, None, False
        owner.check()
        owner.budget.call()
        self.raw = owner.low.iterate(relation, reference.cps_obj if reference is not None else None)
        if self.raw is not None:
            owner.iterators.add(self)
        owner.budget.checkpoint()

    def read(self):
        self.owner.check()
        if self.raw is None:
            return None
        self.owner.budget.call()
        try:
            raw = self.owner.low.scan(self.raw)
        except BaseException:
            # Its validity after a failed native scan is unknown. Let the
            # owner-wide cleanup reclaim it, without a possibly invalid release.
            self.raw = None
            self.owner.iterators.discard(self)
            raise
        if raw is None:
            self.raw = None
            self.exhausted = True
            self.owner.iterators.discard(self)  # scan(None) already freed it.
        self.owner.budget.checkpoint()
        return self.owner.wrap(raw)

    def close(self):
        raw, self.raw = self.raw, None
        self.owner.iterators.discard(self)
        if raw is not None:
            self.owner.require_owner()
            self.owner.iterator_releases += 1
            if self.owner.low.release_handle(raw) != 1:
                raise NpiHierarchyUnavailable("npi_iterator_cleanup_unconfirmed")


class _Children:
    def __init__(self, provider, scope, iterator):
        self.provider, self.scope, self.iterator = provider, scope, iterator
        self.complete = False
        self.gaps = ()

    def __iter__(self):
        return self

    def __next__(self):
        if self.iterator is None:
            self.gaps = ("npi_array_container_children_unavailable",)
            raise StopIteration
        while True:
            handle = self.iterator.read()
            if handle is None:
                # InternalScope is a documented child relation for these
                # scope kinds. This says nothing about Instance(ref), Module,
                # ClassDefn or other relationships that may be unsupported.
                self.complete = not self.gaps and not self.provider.degraded
                if self.provider.degraded:
                    self.gaps = tuple(sorted(set(self.gaps) | {"npi_degraded_design"}))
                raise StopIteration
            kind = self.provider.scalar(handle, "type")
            if kind in _NON_INSTANCE_SCOPES:
                continue
            if kind not in _KINDS:
                self.gaps = tuple(sorted(set(self.gaps) | {"npi_child_scope_kind_unmodeled"}))
                continue
            fact = self.provider.project_scope(handle, kind)
            if fact.parent_path != self.scope.path:
                raise NpiHierarchyUnavailable("npi_scope_parent_mismatch")
            return fact


class _Namespaces:
    """One bounded explicit namespace walk; never builds an instance tree."""

    def __init__(self, provider, *, gaps=(), root_gap="npi_namespace_roots_unavailable"):
        self.provider = provider
        self.complete = False
        self.gaps = tuple(gaps)
        if provider.degraded:
            self.add_gap("npi_degraded_design")
        self.seen, self.pending = set(), None
        iterator = _LanguageIterator(provider, provider.low.Instance, None)
        self.stack = [(iterator, None, 0)]
        if iterator.raw is None:
            self.add_gap(root_gap)

    def add_gap(self, gap):
        self.gaps = tuple(sorted(set(self.gaps) | {gap}))

    def __iter__(self):
        return self

    def __next__(self):
        provider = self.provider
        while True:
            provider.check()
            if self.pending is not None:
                handle, path, depth = self.pending
                self.pending = None
                iterator = _LanguageIterator(provider, provider.low.InternalScope, handle)
                self.stack.append((iterator, path, depth + 1))
            if not self.stack:
                # Exhausting observed Language scopes is not proof that the
                # KDB exposes every source declaration or specialization.
                raise StopIteration
            iterator, parent, depth = self.stack[-1]
            handle = iterator.read()
            if handle is None:
                self.stack.pop()
                continue
            kind, path = provider.scalar(handle, "type"), provider.scalar(handle, "full_name")
            if not isinstance(path, str) or not path or not isinstance(kind, str) or not kind:
                raise NpiHierarchyUnavailable("npi_namespace_identity_unavailable")
            provider.budget.admit_namespace(kind, path, depth)
            key = kind, path
            if key in self.seen:
                self.add_gap("npi_namespace_alias_or_cycle")
                continue
            self.seen.add(key)
            if kind not in _NAMESPACES:
                self.add_gap("npi_namespace_kind_unmodeled")
                continue
            parent_handle = provider.one(handle, "scope_handle")
            actual_parent = (provider.scalar(parent_handle, "full_name")
                             if parent_handle is not None else None)
            if actual_parent != parent:
                raise NpiHierarchyUnavailable("npi_namespace_parent_mismatch")
            if _NAMESPACES[kind] == "instance_array":
                self.add_gap("npi_array_container_children_unavailable")
                continue
            self.pending = handle, path, depth
            return handle, kind, path

    def close(self):
        self.pending = None
        first_error = None
        while self.stack:
            iterator, _, _ = self.stack.pop()
            try:
                self.provider.close_iterator(iterator)
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error


class _Classes(_Namespaces):
    """Explicit class inventory over observed compilation namespaces."""

    def __init__(self, provider):
        super().__init__(provider,
                         gaps=("npi_uninstantiated_declaration_inventory_unverified",
                               "npi_class_specialization_inventory_unverified"),
                         root_gap="npi_class_roots_unavailable")
        self.root_hint = getattr(provider, "_class_query_root", None)
        self.seed_checked = False
        self.seed_fact = None

    def __next__(self):
        if not self.seed_checked:
            self.seed_checked = True
            # An explicit qualified root can be observed before unrelated
            # global namespaces consume the inventory budget. It remains an
            # exact SDK lookup, not a source-name/nearest-package inference.
            if isinstance(self.root_hint, str) and "." in self.root_hint:
                handle = self.provider.lookup(self.root_hint)
                if (handle is not None and self.provider.scalar(handle, "type") == "npiClassDefn"
                        and self.provider.scalar(handle, "full_name") == self.root_hint):
                    self.provider.budget.admit_namespace("npiClassDefn", self.root_hint, 0)
                    fact = self.provider.project_class(handle)
                    self.seed_fact = fact
                    return fact
        while True:
            handle, kind, _ = super().__next__()
            if kind == "npiClassDefn":
                fact = self.provider.project_class(handle)
                if self.seed_fact is not None and fact.symbol.symbol_id == self.seed_fact.symbol.symbol_id:
                    if fact != self.seed_fact:
                        raise NpiHierarchyUnavailable("npi_class_identity_conflict")
                    continue
                return fact


class NpiHierarchySourceProvider:
    origin = "verdi_npi"

    def __init__(self, lang, low, util, *, top, artifact_identity, budget,
                 require_owner, degraded=False):
        self.lang, self.low, self.util = lang, low, util
        self.top, self.artifact_identity = top, artifact_identity
        self.budget, self.require_owner = budget, require_owner
        self.degraded = degraded
        self.thread = threading.get_ident()
        self.active = True
        self.iterators = set()
        self.handle_count = self.iterator_releases = 0
        self._released_handle_count = 0
        self.handle_peak = 0
        self.handle_batches = 1
        self.cleanup_ms = 0.0
        self.cleanup_error = None
        self._top_handle = None
        self._file_inventory = None

    def check(self):
        if not self.active or threading.get_ident() != self.thread:
            raise NpiHierarchyUnavailable("npi_language_owner_unavailable")
        self.require_owner()
        self.budget.checkpoint()

    def wrap(self, raw):
        if raw is None:
            return None
        self._admit_handle()
        return self.util.handle(raw)

    def _admit_handle(self):
        self.handle_count += 1
        live = self.handle_count - self._released_handle_count
        self.handle_peak = max(self.handle_peak, live)
        if live > 16384:
            raise HierarchyBudgetExceeded('native_handle_limit')

    def recycle_scan_handles(self):
        """Scan-only safe point: all scope frames/iterators have been copied."""
        self.check()
        if self.iterators or self._file_inventory is not None:
            raise NpiHierarchyUnavailable('npi_scan_recycle_unsafe')
        self.budget.call()
        try:
            if self.lang.release_all_handles() != 1:
                raise NpiHierarchyUnavailable('npi_language_cleanup_unconfirmed')
        except BaseException as exc:
            self.cleanup_error = exc
            raise
        self._top_handle = None
        self._released_handle_count = self.handle_count
        self.handle_batches += 1
        self.require_owner()
        self.budget.checkpoint()

    def scalar(self, handle, name):
        self.check()
        self.budget.call()
        value = getattr(handle, name)()
        self.budget.checkpoint()
        if value is not None and not isinstance(value, (str, bool, int, float)):
            raise NpiHierarchyUnavailable("npi_property_not_scalar")
        if isinstance(value, str) and len(value) > 16384:
            raise HierarchyBudgetExceeded("native_property_size_limit")
        return value

    def one(self, handle, name):
        self.check()
        self.budget.call()
        value = getattr(handle, name)()
        self.budget.checkpoint()
        if value is not None:
            self._admit_handle()
        return value

    def lookup(self, path):
        self.check()
        self.budget.call()
        raw = self.low.handle_by_name(path, None)
        self.budget.checkpoint()
        return self.wrap(raw)

    def _location(self, handle, *, definition=False):
        file = self.scalar(handle, "def_file" if definition else "file")
        line = self.scalar(handle, "def_line_no" if definition else "line_no")
        if isinstance(file, str) and file and isinstance(line, int) and not isinstance(line, bool) and line >= 1:
            return SourceLocation(file, line)
        return None

    def _parameters(self, handle):
        parameters, gaps = [], set()
        iterator = _LanguageIterator(self, self.low.Parameter, handle)
        try:
            for _ in range(129):
                item = iterator.read()
                if item is None:
                    break
                if len(parameters) >= 128:
                    gaps.add("npi_parameter_limit")
                    break
                name = self.scalar(item, "name")
                value = self.scalar(item, "value")
                if (not isinstance(name, str) or not name or value is None
                        or value == getattr(self.util, "GET_VALUE_ERROR", "FAILED_TO_GET_VALUE")):
                    gaps.add("npi_parameter_value_unavailable")
                    continue
                if any(key == name for key, _ in parameters):
                    gaps.add("npi_parameter_name_ambiguous")
                    continue
                parameters.append((name, str(value)))
            else:
                # Invalid or duplicate entries still consume the scan budget.
                # A capped iterator has not proved the parameter inventory ends.
                gaps.add("npi_parameter_limit")
        finally:
            self.close_iterator(iterator)
        # The SDK fixture proves value parameters only. Do not share a
        # specialization identity on an unverified type-parameter inventory.
        gaps.add("npi_type_parameter_inventory_unverified")
        return tuple(parameters), gaps

    def project_scope(self, handle, native_kind):
        kind = _KINDS[native_kind]
        path, name = self.scalar(handle, "full_name"), self.scalar(handle, "name")
        if not isinstance(path, str) or not path or not isinstance(name, str) or not name:
            raise NpiHierarchyUnavailable("npi_scope_identity_unavailable")
        parent_handle = self.one(handle, "scope_handle")
        parent = self.scalar(parent_handle, "full_name") if parent_handle is not None else None
        if parent is not None and (not isinstance(parent, str) or not parent):
            raise NpiHierarchyUnavailable("npi_scope_parent_unavailable")
        location = self._location(handle)
        gaps = set()
        if location is None:
            gaps.add("npi_instance_location_unavailable")
        if parent is None and path != self.top:
            gaps.add("npi_scope_parent_unavailable")
        definition_name = definition_id = definition_location = None
        parameters = ()
        if kind in {"module", "interface", "program"}:
            definition_name = self.scalar(handle, "def_name")
            if not isinstance(definition_name, str) or not definition_name:
                definition_name = None
                gaps.add("npi_definition_unavailable")
            definition_location = self._location(handle, definition=True)
            if definition_location is None:
                gaps.add("npi_definition_location_unavailable")
            parameters, parameter_gaps = self._parameters(handle)
            gaps.update(parameter_gaps)
            if definition_name is not None:
                # Conservative instance-qualified specialization: do not merge
                # definitions when type-parameter evidence is incomplete.
                definition_id = semantic_fact_id(self.origin, self.artifact_identity, "definition", path)
        return HierarchyScope(
            semantic_fact_id(self.origin, self.artifact_identity, kind, path), path, name, kind, parent,
            definition_id, definition_name, location, definition_location, parameters, tuple(gaps),
        )

    def _base_value_parameters(self, typespec):
        """Observe explicit constant actuals on this extends Typespec only.

        ParamAssign -> Lhs/Rhs is the documented Language relation. A base
        declaration's default Parameter values are never actual overrides.
        This bounded positive inventory does not establish a specialized
        class type identity or prove the absence of type parameters.
        """
        relation = getattr(self.low, "ParamAssign", None)
        gaps = {"npi_class_base_specialization_unverified"}
        if relation is None:
            return (), "not_observed", tuple(gaps | {"npi_class_base_parameters_unavailable"})
        pairs, seen_names, partial = [], set(), False
        iterator = _LanguageIterator(self, relation, typespec)
        try:
            for _ in range(129):
                assignment = iterator.read()
                if assignment is None:
                    break
                if len(pairs) >= 128:
                    partial = True
                    gaps.add("npi_class_base_parameter_limit")
                    break
                lhs, rhs = self.one(assignment, "lhs_handle"), self.one(assignment, "rhs_handle")
                if lhs is not None and self.scalar(lhs, "type") == "npiRefObj":
                    lhs = self.one(lhs, "actual_handle")
                if lhs is None or self.scalar(lhs, "type") != "npiParameter":
                    partial = True
                    gaps.add("npi_class_base_parameter_unmodeled")
                    continue
                name = self.scalar(lhs, "name")
                if not isinstance(name, str) or not name:
                    partial = True
                    gaps.add("npi_class_base_parameter_unavailable")
                    continue
                # Unknown RHS observations still participate in conflict
                # detection; a later constant must not hide that assignment.
                if name in seen_names:
                    raise NpiHierarchyUnavailable("npi_class_base_parameter_conflict")
                self.budget.reserve_bytes(128 + 4 * len(name))
                seen_names.add(name)
                if rhs is None or self.scalar(rhs, "type") != "npiConstant":
                    partial = True
                    gaps.add("npi_class_base_parameter_unmodeled")
                    continue
                value = self.scalar(rhs, "value")
                if (value is None or str(value) == ""
                        or value == getattr(self.util, "GET_VALUE_ERROR", "FAILED_TO_GET_VALUE")):
                    partial = True
                    gaps.add("npi_class_base_parameter_unavailable")
                    continue
                self.budget.reserve_bytes(128 + 4 * (len(name) + len(str(value))))
                pairs.append((name, str(value)))
            else:
                partial = True
                gaps.add("npi_class_base_parameter_limit")
        finally:
            self.close_iterator(iterator)
        status = "partial" if partial else "observed" if pairs else "not_observed"
        return tuple(pairs), status, tuple(sorted(gaps))

    def project_class(self, handle):
        path, name = self.scalar(handle, "full_name"), self.scalar(handle, "name")
        if not isinstance(path, str) or not path or not isinstance(name, str) or not name:
            raise NpiHierarchyUnavailable("npi_class_identity_unavailable")
        identity = semantic_fact_id(self.origin, self.artifact_identity, "class", path)
        parent = self.one(handle, "scope_handle")
        scope_id, gaps = None, set()
        if parent is not None:
            parent_kind, parent_path = self.scalar(parent, "type"), self.scalar(parent, "full_name")
            if parent_kind in _NAMESPACES and isinstance(parent_path, str) and parent_path:
                scope_id = semantic_fact_id(self.origin, self.artifact_identity,
                                           _NAMESPACES[parent_kind], parent_path)
            else:
                gaps.add("npi_class_scope_identity_unavailable")
        location = self._location(handle)
        if location is None:
            gaps.add("npi_class_location_unavailable")
        parameters, parameter_gaps = self._parameters(handle)
        gaps.update(parameter_gaps)
        # Native full_name distinguishes the observed declaration. Until the
        # specialization API is verified, do not coalesce it with a generic
        # source template or infer a type identity from parameter spellings.
        gaps.add("npi_class_specialization_identity_unverified")
        symbol = SourceSymbol(identity, name, path, "class", location=location,
                              scope_id=scope_id, declaration_id=identity,
                              parameterization=parameters, gaps=tuple(gaps))
        extends = self.one(handle, "extends_handle")
        if extends is None:
            return ClassFact(symbol, "none")
        if self.scalar(extends, "type") == "npiClassTypespec":
            base = self.one(extends, "class_defn_handle")
            if base is not None and self.scalar(base, "type") == "npiClassDefn":
                base_path = self.scalar(base, "full_name")
                if isinstance(base_path, str) and base_path:
                    actuals, actual_status, binding_gaps = self._base_value_parameters(extends)
                    return ClassFact(symbol, "resolved",
                                     semantic_fact_id(self.origin, self.artifact_identity, "class", base_path),
                                     base_path, gaps=binding_gaps, base_value_parameters=actuals,
                                     base_parameter_status=actual_status)
        # A Typespec's bare name is not a qualified binding. Keep it out of
        # base_qualified_name rather than matching it against package classes.
        return ClassFact(symbol, "unresolved", gaps=("npi_class_base_binding_unavailable",))

    def _require_top(self):
        if self._top_handle is None:
            handle = self.lookup(self.top)
            if (handle is None or self.scalar(handle, "full_name") != self.top
                    or self.scalar(handle, "type") not in {"npiModule", "npiInterface", "npiProgram"}):
                raise NpiHierarchyUnavailable("npi_selected_top_unavailable")
            self._top_handle = handle

    def _under_top(self, handle):
        seen = set()
        for _ in range(self.budget.max_depth + 1):
            path = self.scalar(handle, "full_name")
            if path == self.top:
                return True
            if not isinstance(path, str) or not path or path in seen:
                return False
            seen.add(path)
            handle = self.one(handle, "scope_handle")
            if handle is None:
                return False
        raise HierarchyBudgetExceeded("scope_depth_limit")

    def lookup_scope(self, path, budget):
        if budget is not self.budget:
            raise ValueError("a native owner uses one query budget")
        self._require_top()
        handle = self._top_handle if path == self.top else self.lookup(path)
        if handle is None:
            return ScopeLookup(None, not self.degraded,
                               ("npi_degraded_design",) if self.degraded else ())
        if self.scalar(handle, "full_name") != path:
            return ScopeLookup(None, False, ("npi_exact_path_mismatch",))
        kind = self.scalar(handle, "type")
        if kind not in _KINDS:
            return ScopeLookup(None, False, ("npi_target_is_not_hierarchy_scope",))
        if not self._under_top(handle):
            # In particular, a ModuleArray pseudo-handle may have no scope.
            # Its spelling alone cannot establish a parent or design binding.
            return ScopeLookup(None, False, ("npi_scope_ancestor_unresolved",))
        fact = self.project_scope(handle, kind)
        return ScopeLookup(fact, not self.degraded,
                           ("npi_degraded_design",) if self.degraded else ())

    @contextmanager
    def open_children(self, scope, budget):
        if budget is not self.budget:
            raise ValueError("a native owner uses one query budget")
        self.check()
        if scope.kind == "instance_array":
            # Array elements are real direct children of the surrounding HDL
            # scope in the verified SDK. Do not invent parent edges to an
            # array pseudo-handle whose own scope/child APIs are unavailable.
            yield _Children(self, scope, None)
            return
        handle = self.lookup(scope.path)
        if handle is None or self.scalar(handle, "full_name") != scope.path:
            raise NpiHierarchyUnavailable("npi_scope_changed_during_query")
        iterator = _LanguageIterator(self, self.low.InternalScope, handle)
        try:
            yield _Children(self, scope, iterator)
        finally:
            self.close_iterator(iterator)

    @contextmanager
    def open_classes(self, budget):
        if budget is not self.budget:
            raise ValueError("a native owner uses one query budget")
        self._require_top()
        stream = _Classes(self)
        try:
            yield stream
        finally:
            stream.close()

    @contextmanager
    def open_file_symbols(self, entry, budget):
        if budget is not self.budget:
            raise ValueError("a native owner uses one query budget")
        self._require_top()
        from .npi_hierarchy_files import FileInventory, FileSymbols
        if self._file_inventory is None:
            self._file_inventory = FileInventory(self)
        stream = FileSymbols(self._file_inventory, entry)
        try:
            yield stream
        finally:
            # An interrupted consumer cannot leave a suspended native walk.
            # Exhausted inventories contain copied facts only and are reusable
            # by subsequent files in this same exclusive query owner.
            if not self._file_inventory.exhausted:
                active_error = sys.exc_info()[1]
                try:
                    self._file_inventory.close()
                except BaseException as exc:
                    if self.cleanup_error is None:
                        self.cleanup_error = exc
                    if active_error is None:
                        raise

    def close_iterator(self, iterator):
        active_error = sys.exc_info()[1]
        try:
            iterator.close()
        except BaseException as exc:
            if self.cleanup_error is None:
                self.cleanup_error = exc
            if active_error is None:
                raise

    def close(self):
        if threading.get_ident() != self.thread:
            raise NpiHierarchyUnavailable("npi_language_owner_unavailable")
        if not self.active:
            return
        started = time.monotonic()
        first_error = self.cleanup_error
        try:
            # No cancellation/deadline check here. This callback checks only
            # loaded-design ownership. Handles from a replaced design may
            # already be invalid and must never be passed back to release.
            self.require_owner()
        except BaseException:
            for iterator in self.iterators:
                iterator.raw = None
            self.iterators.clear()
            try:
                if self._file_inventory is not None:
                    self._file_inventory.close()
            except BaseException as exc:
                # Preserve the ownership failure. No invalid native handle
                # may be released, and secondary cleanup cannot replace it.
                if self.cleanup_error is None:
                    self.cleanup_error = exc
            finally:
                self._file_inventory = None
                self._top_handle = None
                self.active = False
                self.cleanup_ms = (time.monotonic() - started) * 1000
            raise
        try:
            if self._file_inventory is not None:
                try:
                    self._file_inventory.close()
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
                self._file_inventory = None
            for iterator in tuple(self.iterators):
                try:
                    iterator.close()
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
            try:
                self._top_handle = None
                if self.lang.release_all_handles() != 1:
                    raise NpiHierarchyUnavailable("npi_language_cleanup_unconfirmed")
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        finally:
            self.active = False
            self.iterators.clear()
            first_error = first_error or self.cleanup_error
            self.cleanup_ms = (time.monotonic() - started) * 1000
        if first_error is not None:
            raise first_error


@contextmanager
def language_query_owner(*, top, artifact_identity, budget, require_owner,
                         cleanup_failed, degraded=False, prepare_owner=None):
    """Exclusive query boundary; caller owns/serializes the loaded native design.

    The optional prepare_owner callback may load the caller's design only
    after exclusivity is acquired. No waveform lock or native handle survives
    the request. Nested/concurrent Language owners are rejected before loading,
    importing or freeing another owner's resources.
    require_owner checks generation/thread only, including during cleanup;
    cancellation and deadlines belong to the budget checkpoints.
    """
    if not _OWNER_LOCK.acquire(blocking=False):
        raise NpiHierarchyUnavailable("npi_language_owner_busy")
    owner = None
    try:
        budget.checkpoint()
        if prepare_owner is not None:
            prepare_owner()
        require_owner()
        budget.checkpoint()
        from pynpi import lang, lang_l0, lang_l0_util
        owner = NpiHierarchySourceProvider(lang, lang_l0, lang_l0_util, top=top,
                                          artifact_identity=artifact_identity, budget=budget,
                                          require_owner=require_owner,
                                          degraded=degraded() if callable(degraded) else degraded)
        try:
            yield owner
        finally:
            active_error = sys.exc_info()[1]
            try:
                owner.close()
            except BaseException:
                cleanup_failed()
                if active_error is None:
                    raise
    finally:
        _OWNER_LOCK.release()
