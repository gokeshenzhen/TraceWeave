"""Copied Language declaration observations, shared within one file query.

No bounded definition-by-file lookup has been verified for this adapter. An
explicit semantic file query walks observed compilation namespaces under the request
budget. It does not construct a hierarchy tree, call eager L1 lists, parse source
text, or promote Text tokens to semantic declarations. This global mapping cost
is real; copying a local file view is not evidence of local-only native work.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json

from .hierarchy_source_ir import HierarchyBudgetExceeded, semantic_fact_id
from .hierarchy_source_symbols import SourceSymbol
from .npi_hierarchy_source import _LanguageIterator, _Namespaces, _NAMESPACES, NpiHierarchyUnavailable


class FileInventory:
    """A request-local bounded fact index, never retained across native owners."""

    def __init__(self, provider):
        self.provider = provider
        self.rows, self.by_id = [], {}
        self.target_paths = getattr(provider, "_file_query_paths", None)
        self.examined_symbols = 0
        self.observed_fingerprints = {}
        self.exhausted, self.closed = False, False
        self._gaps = {"npi_uninstantiated_declaration_inventory_unverified",
                      "npi_file_variable_inventory_unverified", "npi_file_import_inventory_unverified",
                      "npi_uvm_usage_inventory_unverified"}
        self.walk = _Namespaces(provider, root_gap="npi_file_namespace_roots_unavailable")
        self.generator = self._declarations()

    @property
    def gaps(self):
        return tuple(sorted(self._gaps | set(self.walk.gaps)))

    def _id(self, kind, key):
        return semantic_fact_id(self.provider.origin, self.provider.artifact_identity, kind, key)

    def _namespace_symbol(self, handle, native_kind, path):
        provider, kind = self.provider, _NAMESPACES[native_kind]
        if kind in {"module", "interface", "program"}:
            scope = provider.project_scope(handle, native_kind)
            if scope.definition_id is None:
                self._gaps.add("npi_file_definition_unavailable")
                return None, False
            # Match the instance service's conservative specialization ID.
            # def_name is not an instance path or proof of a unique template.
            return SourceSymbol(
                scope.definition_id, scope.definition_name, scope.definition_name, kind,
                location=scope.definition_location, declaration_id=scope.definition_id,
                parameterization=scope.parameterization,
                declaration_status="specialization",
                gaps=tuple(set(scope.gaps) | {"npi_definition_scope_identity_unverified",
                                              "npi_definition_specialization_identity_unverified"}),
            ), False
        if kind == "class":
            fact = provider.project_class(handle)
            used = (fact.base_status == "resolved" and
                    fact.base_qualified_name.startswith(("uvm_pkg.", "uvm_pkg::")))
            return fact.symbol, used
        if kind not in {"package", "function", "task"}:
            return None, False
        name = provider.scalar(handle, "name")
        if not isinstance(name, str) or not name:
            self._gaps.add("npi_file_symbol_name_unavailable")
            return None, False
        parent = provider.one(handle, "scope_handle")
        parent_path = provider.scalar(parent, "full_name") if parent is not None else None
        parent_kind = provider.scalar(parent, "type") if parent is not None else None
        scope_id = (self._id(_NAMESPACES[parent_kind], parent_path)
                    if parent_kind in _NAMESPACES and isinstance(parent_path, str) and parent_path else None)
        identity = self._id(kind, path)
        return SourceSymbol(identity, name, path, kind, location=provider._location(handle),
                            scope_id=scope_id, declaration_id=identity,
                            declaration_status="namespace_observation",
                            gaps=("npi_source_declaration_explicitness_unverified",)), False

    def _typedef(self, handle, expected_scope):
        provider = self.provider
        name = provider.scalar(handle, "name")
        native_kind = provider.scalar(handle, "type")
        parent = provider.one(handle, "scope_handle")
        parent_path = provider.scalar(parent, "full_name") if parent is not None else None
        parent_kind = provider.scalar(parent, "type") if parent is not None else None
        if (not isinstance(name, str) or not name or parent_path != expected_scope
                or parent_kind not in _NAMESPACES):
            self._gaps.add("npi_typedef_binding_unavailable")
            return None
        if not isinstance(native_kind, str) or not native_kind.endswith("Typespec"):
            self._gaps.add("npi_typedef_kind_unmodeled")
            return None
        # Typespec.full_name can legitimately be null. This qualification uses
        # its actual scope handle and the Typedef relation, not a file token or
        # a guess about the nearest textual scope. Same-name typedefs in other
        # elaborated namespaces keep distinct identities.
        qualified = parent_path + "::" + name
        # Identity encodes the actual relation components separately. Escaped
        # identifiers can themselves contain dots or qualification punctuation.
        key = json.dumps([parent_path, name], ensure_ascii=True, separators=(",", ":"))
        identity = self._id("typedef", key)
        return SourceSymbol(identity, name, qualified, "typedef", location=provider._location(handle),
                            scope_id=self._id(_NAMESPACES[parent_kind], parent_path),
                            declaration_id=identity, type_id=self._id(native_kind, key),
                            gaps=("npi_typedef_layout_not_projected",))

    def _declarations(self):
        try:
            for handle, native_kind, path in self.walk:
                symbol, uses_uvm = self._namespace_symbol(handle, native_kind, path)
                if symbol is not None:
                    yield symbol, uses_uvm
                iterator = _LanguageIterator(self.provider, self.provider.low.Typedef, handle)
                try:
                    while True:
                        item = iterator.read()
                        if item is None:
                            # Absence of an iterator was not verified as a
                            # complete negative for every namespace kind.
                            if not iterator.exhausted:
                                self._gaps.add("npi_typedef_inventory_unverified")
                            break
                        symbol = self._typedef(item, path)
                        if symbol is not None:
                            yield symbol, False
                finally:
                    self.provider.close_iterator(iterator)
        finally:
            self.walk.close()

    def read(self):
        """Copy one unique declaration, charging retained cache memory/work."""
        self.provider.check()
        if self.exhausted or self.closed:
            return None
        while True:
            try:
                symbol, uses_uvm = next(self.generator)
            except StopIteration:
                self.exhausted = True
                return None
            # A targeted file query retains only its own copied facts. Native
            # calls and inspected namespaces still consume their original work
            # budgets, and discarded declarations retain a separate work cap.
            self.examined_symbols += 1
            if self.target_paths is not None and self.examined_symbols > self.provider.budget.max_symbols:
                raise HierarchyBudgetExceeded("symbol_limit")
            if symbol.location is None:
                # There is no basis for assigning this declaration to any
                # requested compile member. Keep the missing mapping visible.
                self._gaps.add("npi_file_symbol_ownership_unavailable")
                continue
            if self.target_paths is not None:
                # Dropping a body must not hide an identity observed with two
                # different file bindings. Keep a charged, fixed-size digest
                # of every located observation, including non-target files.
                fingerprint = hashlib.sha256(json.dumps(
                    [asdict(symbol), uses_uvm], sort_keys=True, ensure_ascii=True,
                    separators=(",", ":"),
                ).encode()).digest()
                previous = self.observed_fingerprints.get(symbol.symbol_id)
                if previous is not None:
                    if previous != fingerprint:
                        raise NpiHierarchyUnavailable("npi_file_symbol_identity_conflict")
                else:
                    self.provider.budget.reserve_bytes(256 + 4 * len(symbol.symbol_id) + len(fingerprint))
                    self.observed_fingerprints[symbol.symbol_id] = fingerprint
            if self.target_paths is not None and symbol.location.file not in self.target_paths:
                continue
            row = symbol, uses_uvm
            existing = self.by_id.get(symbol.symbol_id)
            if existing is not None:
                if existing != row:
                    raise NpiHierarchyUnavailable("npi_file_symbol_identity_conflict")
                self._gaps.add("npi_file_symbol_alias_unverified")
                continue
            self.provider.budget.admit_symbol(symbol)
            self.rows.append(row)
            self.by_id[symbol.symbol_id] = row
            return row

    def close(self):
        if self.closed:
            return
        self.closed = True
        if not self.exhausted:
            self._gaps.add("npi_file_inventory_interrupted")
        try:
            self.generator.close()
        finally:
            # A generator closed before its first next never enters finally.
            self.walk.close()


class FileSymbols:
    complete = False  # Uninstantiated/import/variable coverage remains unproven.
    has_uvm_import = None
    work_scope = "global_namespaces_bounded"

    def __init__(self, inventory, entry):
        self.inventory, self.entry, self.index = inventory, entry, 0
        self.uses_uvm = None

    @property
    def gaps(self):
        return self.inventory.gaps

    def __iter__(self):
        return self

    def __next__(self):
        inventory = self.inventory
        while True:
            inventory.provider.check()
            if self.index < len(inventory.rows):
                row = inventory.rows[self.index]
            else:
                row = inventory.read()
                if row is None:
                    raise StopIteration
            self.index += 1
            symbol, uses_uvm = row
            if symbol.location.file == self.entry.path or symbol.location.file in self.entry.aliases:
                if uses_uvm:
                    self.uses_uvm = True
                return symbol
