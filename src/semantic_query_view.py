"""Once-validated immutable query indexes over one producer artifact.

Producer work limits authenticate the stored projection; they are not a charge
against a later reader. The runtime accounts these retained indexes separately
from the raw IR. Public providers charge the facts they actually visit/copy.
"""
from __future__ import annotations

from collections import defaultdict
from threading import RLock
from types import MappingProxyType
import sys

from .slang_hierarchy_codec import validate_hierarchy_artifact
from .slang_source_codec import validate_source_catalog_artifact


def _freeze(value, checkpoint):
    checkpoint()
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item, checkpoint) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item, checkpoint) for item in value)
    return value


def _size(value, seen, checkpoint):
    checkpoint()
    if id(value) in seen:
        return 0
    seen.add(id(value))
    result = sys.getsizeof(value)
    if isinstance(value, dict):
        result += sum(_size(key, seen, checkpoint) + _size(item, seen, checkpoint)
                      for key, item in value.items())
    elif isinstance(value, (list, tuple)):
        result += sum(_size(item, seen, checkpoint) for item in value)
    return result


class SemanticQueryView:
    """Private immutable facts plus one bounded ownership-validation memo."""
    def __init__(self, ir, scope, checkpoint):
        self.scope, self.search, self.top = scope, scope.semantic_search, scope.top
        validator = (validate_hierarchy_artifact if self.search.purpose == 'hierarchy'
                     else validate_source_catalog_artifact)
        payload = validator(ir, scope, checkpoint=checkpoint, return_payload=True)
        # Conservatively include immutable container replacement and index overhead.
        retained = 2 * _size(payload, set(), checkpoint)
        self.payload = _freeze(payload, checkpoint)
        self.by_path, self.children = MappingProxyType({}), MappingProxyType({})
        if self.search.purpose == 'hierarchy':
            rows, children = {}, defaultdict(list)
            for row in self.payload['rows']:
                checkpoint()
                rows[row['path']] = row
                children[row['parent_path']].append(row)
            self.by_path = MappingProxyType(rows)
            self.children = MappingProxyType({key: tuple(value) for key, value in children.items()})
            self.complete_children = frozenset(payload['complete_children'])
            levels, exhausted = {}, set()
            for path, row in rows.items():
                checkpoint()
                if path in self.search.roots:
                    levels[path] = 0
                elif row['parent_path'] in levels:
                    levels[path] = levels[row['parent_path']] + 1
            for path in reversed(rows):
                checkpoint()
                if path in self.complete_children and all(
                        child['path'] in exhausted for child in self.children.get(path, ())):
                    exhausted.add(path)
            self.levels, self.exhausted = MappingProxyType(levels), frozenset(exhausted)
            retained += 512 * len(rows)
        references, required = set(), set()
        if self.search.purpose == 'hierarchy':
            for row in self.payload['rows']:
                checkpoint()
                references.update(row[field]['file'] for field in ('location', 'definition_location')
                                  if row[field] is not None)
        else:
            for path, row in self.payload['files'].items():
                checkpoint()
                if (row['symbols'] or row['complete'] or row['uses_uvm'] is False
                        or row['has_uvm_import'] is False):
                    required.add(path)
            classes = self.payload.get('classes')
            if classes is not None:
                for row in classes['rows']:
                    checkpoint(); references.add(row['location']['file'])
        self.referenced_files, self.required_files = frozenset(references), frozenset(required)
        retained += 256 * (len(references) + len(required))
        self.cache_bytes = retained + 512
        self._ownership_lock = RLock()
        self._ownership_token = None

    def covers_hierarchy(self, requested):
        """Use validated native parent links, attempted depth and child proofs."""
        return self._hierarchy_reuse_decision(requested)[0]

    def _hierarchy_reuse_decision(self, requested):
        """Private fixed reason for bounded observer diagnostics; no retained trace."""
        other = requested.semantic_search
        if self.search.purpose != 'hierarchy' or other is None or other.purpose != 'hierarchy':
            return False, 'purpose_mismatch'
        left, right = self.scope.to_dict(), requested.to_dict()
        if (not self.scope.coverage_boundary.explicit or not requested.coverage_boundary.explicit
                or not set(requested.capabilities).issubset(self.scope.capabilities)
                or self.scope.coverage_boundary.objective_exclusions != requested.coverage_boundary.objective_exclusions):
            return False, 'coverage_policy_mismatch'
        for value in (left, right):
            for key in ('capabilities', 'proved_ancestor_chains', 'proved_lcas',
                        'projection_instance_paths', 'elaboration_candidates', 'coverage_boundary'):
                value.pop(key)
            # Assignment candidates guide connectivity projection; hierarchy
            # projection consumes roots and proves actual parent membership.
            for key in ('roots', 'depth', 'hierarchy_mode', 'max_instances', 'max_members', 'hierarchy_max_bytes',
                        'assignment_candidates'):
                value['semantic_search'].pop(key, None)
        if left != right:
            return False, 'semantic_context_mismatch'
        # A requested chain/boundary must consist of actual stored scope facts.
        atoms = (*requested.projection_instance_paths, *requested.coverage_boundary.instance_paths,
                 *(path for chain in requested.proved_ancestor_chains for path in chain))
        if any(path not in self.by_path for path in atoms):
            return False, 'scope_fact_missing'
        for root in other.roots:
            if root not in self.levels:
                if root in self.by_path and other.hierarchy_mode == 'bounded' and other.depth == 0:
                    continue  # A captured ancestor is still an authoritative positive.
                return False, 'root_not_captured'
            if root in self.exhausted:
                continue
            if other.hierarchy_mode == 'complete' or self.levels[root] + other.depth > self.search.depth:
                return False, 'requested_depth_uncovered'
        return True, 'stored_native_scope_coverage'

    def bind_ownership(self, context, checkpoint):
        checkpoint()
        if context.selected_top != self.top:
            raise ValueError('semantic artifact selected top differs from captured context')
        token = getattr(context, 'identity_sha256', None)
        # Production contexts are immutable content identities. Test/manual contexts
        # without that contract are checked each time rather than trusted by id().
        with self._ownership_lock:
            if token is not None and token == self._ownership_token:
                return
            index = getattr(context, 'semantic_ownership_index', None)
            if index is None:
                captured = set()
                for record in context.snapshot.records:
                    checkpoint(); captured.add(record.path)
                aliases = dict(context.aliases)
                unavailable = {aliases.get(path, path) for path, _ in context.unavailable}
            else:
                captured, aliases, unavailable = index
            checkpoint()
            pairs = (self.search.hierarchy_aliases if self.search.purpose == 'hierarchy'
                     else self.search.source_aliases)
            if any(aliases.get(spelling) != path for spelling, path in pairs):
                raise ValueError('semantic artifact aliases differ from captured ownership')
            # Codecs already checked every location against its declared file
            # and alias domain. These retained file sets prove captured ownership
            # without walking every stored row again, even on the first query.
            for path in self.referenced_files | self.required_files:
                checkpoint()
                if path not in captured:
                    raise ValueError('semantic declaration is outside captured inputs')
            if self.search.purpose == 'source_catalog':
                for path in self.payload['files']:
                    checkpoint()
                    if path not in captured and path not in unavailable:
                        raise ValueError('source catalog file is outside captured inputs')
            checkpoint()
            self._ownership_token = token


def semantic_query_view(entry, checkpoint=lambda: None):
    """Runtime entries eagerly own a view; manual fixture entries remain valid."""
    view = getattr(entry, 'semantic_view', None)
    if view is not None:
        return view
    receipt = getattr(entry, 'artifact_scope_receipt', None)
    scope = receipt.scope if receipt is not None else entry.artifact_identity.scope
    if scope.semantic_search is None or scope.semantic_search.purpose not in {'hierarchy', 'source_catalog'}:
        return None
    view = SemanticQueryView(entry.ir, scope, checkpoint)
    # SimpleNamespace fixture entries support caching; frozen runtime entries
    # install their view at construction and do not enter this fallback.
    try:
        entry.semantic_view = view
    except (AttributeError, TypeError):
        pass
    return view
