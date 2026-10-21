"""Bounded worker-local semantic declarations, never a lexical source scan.

The finite inventory is supported declarations in compilation/package/class
and elaborated scopes. Unelaborated definition bodies and generic declarations
retain explicit frontiers. Inventory, imports and usage have separate coverage;
an unsupported expression/scope can never turn into a negative UVM predicate.
Only primitives leave the worker. All locations bind through captured aliases.
"""
from __future__ import annotations

from collections import deque
from bisect import bisect_right
import hashlib
import json
from pathlib import Path

from .cancellation import check_cancelled

CATALOG_VERSION = 1
DECLARATION_DOMAIN = "compilation_declarations_and_elaborated_scopes"
USAGE_DOMAIN = "resolved_types_imports_and_expression_references"
_KINDS = {"Package": "package", "ClassType": "class", "GenericClassDef": "class",
          "TypeAlias": "typedef", "Variable": "variable", "Net": "variable",
          "ClassProperty": "variable", "FormalArgument": "variable", "Field": "variable",
          "Parameter": "parameter", "TypeParameter": "parameter", "EnumType": "enum"}
_SCOPE_KINDS = {"CompilationUnit", "Package", "ClassType", "InstanceBody", "GenerateBlock",
                "Subroutine", "StatementBlock", "PackedStructType", "UnpackedStructType",
                "PackedUnionType", "UnpackedUnionType"}
_NO_USAGE = {"EmptyMember", "Port", "MultiPort", "Genvar", "TransparentMember",
             "TimeScale", "DeferredMember", "EnumValue"}
_USAGE_EXPRESSIONS = {
    'IntegerLiteral', 'RealLiteral', 'TimeLiteral', 'UnbasedUnsizedIntegerLiteral',
    'NullLiteral', 'UnboundedLiteral', 'StringLiteral', 'NamedValue', 'HierarchicalValue',
    'UnaryOp', 'BinaryOp', 'ConditionalOp', 'Inside', 'Assignment', 'Concatenation',
    'Replication', 'Streaming', 'ElementSelect', 'RangeSelect', 'MemberAccess', 'Call',
    'Conversion', 'SimpleAssignmentPattern', 'StructuredAssignmentPattern',
    'ReplicatedAssignmentPattern', 'EmptyArgument', 'ValueRange', 'Dist', 'NewArray', 'MinTypMax',
}


def _declaration_identity(symbol, manager, binding, charge, kind_name):
    """Bounded native anchor, independent of retained files and line indexes.

    The provider adds the captured content-context identity to this key.
    Display locations and canonical specialization keys are separate facts.
    """
    charge()
    value = symbol.location
    if value is None or not value:
        return None
    try:
        original = manager.getFullyOriginalLoc(value)
        if not original:
            return None
        if str(manager.getBufferKind(original.buffer).name) not in {
                'DesignFile', 'IncludeFile', 'LibraryFile'}:
            return None
        owner = binding(str(manager.getFullPath(original.buffer)))
        offset = int(original.offset)
    except (ValueError, RuntimeError):
        return None
    if owner is None or offset < 0:
        return None
    return hashlib.sha256(json.dumps([
        'native_original_offset_v1', kind_name(symbol), str(symbol.lexicalPath),
        str(symbol.name), {'file': owner, 'offset': offset}], sort_keys=True).encode()).hexdigest()


def collect_source_catalog(projector, root):
    from .slang_connectivity_projector import _kind_name
    from pyslang.ast import AssertionExpr, Constraint, Expression, Pattern, Symbol, VisitAction

    search = projector.options.semantic_search
    files = {path: dict(symbols={}, inventory=set(), imports=set(), usage=set(),
                       imported=False, used=False) for path in search.source_files}
    aliases = dict(search.source_aliases)
    aliases.update({path: path for path in files})
    pending = deque()
    seen_scopes, owners = set(), []
    represented, definitions = set(), []
    visits = scope_visits = 0
    limited = False
    stop_reason = None
    coordinates = {}
    processed = set()
    coordinate_bytes = 0
    top_instances, semantic_tops = [], []
    classes = None

    class Limit(Exception):
        pass

    def charge():
        nonlocal visits
        check_cancelled()
        if visits >= search.max_members:
            raise Limit("source_catalog_visit_limit")
        visits += 1

    def binding(spelling):
        # No filesystem resolve, basename association or unobserved '..'.
        if spelling in aliases:
            return aliases[spelling]
        if not spelling:
            return None
        path = Path(spelling)
        if not path.is_absolute():
            spelling = str((projector.options.source_root or Path.cwd()) / path)
        return aliases.get(spelling)

    def coordinate(buffer, path):
        """Index a requested physical buffer once, under the shared envelope.

        A real declaration already supplies its native buffer. It need not
        wait for every unrelated expansion buffer to be inventoried first.
        """
        nonlocal coordinate_bytes
        existing = coordinates.get(buffer)
        if existing is not None:
            return existing
        charge()
        raw = projector.source_manager.getSourceText(buffer).encode('utf-8')
        required = len(raw) + 512
        if coordinate_bytes + required > search.source_max_bytes:
            raise Limit('source_catalog_coordinate_byte_limit')
        coordinate_bytes += required
        starts = [0]
        offset = raw.find(b'\n')
        while offset >= 0:
            charge()
            if coordinate_bytes + 64 > search.source_max_bytes:
                raise Limit('source_catalog_coordinate_byte_limit')
            coordinate_bytes += 64
            starts.append(offset + 1)
            offset = raw.find(b'\n', offset + 1)
        result = (starts, len(raw))
        coordinates[buffer] = result
        processed.add(path)
        return result

    def location(value, *, expanded=False):
        if value is None:
            return None
        manager = projector.source_manager
        try:
            original = (manager.getFullyExpandedLoc(value) if expanded
                        else manager.getFullyOriginalLoc(value))
            # Slang's display filename can be cwd-relative or affected by a
            # line directive. Its parsed buffer path supplies the real owner;
            # this is frontend evidence, not a live filesystem resolution.
            path = (binding(str(manager.getFullPath(original.buffer)))
                    or binding(str(manager.getFileName(original))))
            if path not in files:
                return None
            starts, size = coordinate(original.buffer, path)
            offset = int(original.offset)
            if not 0 <= offset <= size:
                return None
            line = bisect_right(starts, offset)
        except (ValueError, RuntimeError):
            return None
        if path is None or line < 1:
            return None
        return dict(file=path, line=line, column=offset - starts[line - 1] + 1)

    def source(symbol):
        return location(symbol.location)

    def source_owner(value):
        """Bind native ownership even when coordinates are outside this query.

        A confirmed source in another captured file is outside the requested
        declaration domain; it must not become an unbound in-domain location.
        """
        if value is None:
            return None
        try:
            original = projector.source_manager.getFullyOriginalLoc(value)
            return (binding(str(projector.source_manager.getFullPath(original.buffer)))
                    or binding(str(projector.source_manager.getFileName(original))))
        except (ValueError, RuntimeError):
            return None

    def identity(symbol, where=None):
        # Keep the ClassCollector callback shape; public `where` is never an
        # identity input because it is intentionally filtered to this query.
        return _declaration_identity(symbol, projector.source_manager, binding, charge, _kind_name)

    def gap(path, label, domains=("inventory", "imports", "usage")):
        targets = files.values() if path is None else (files[path],) if path in files else ()
        for row in targets:
            for domain in domains:
                row[domain].add(label)

    def is_uvm(symbol):
        if symbol is None or not hasattr(symbol, "lexicalPath"):
            return False
        name = str(symbol.lexicalPath)
        return name == "uvm_pkg" or name.startswith(("uvm_pkg::", "uvm_pkg."))

    def observe_type(value, path):
        # Actual canonical bindings include containers, composites and bases.
        # Retain native owners so a Python object identity cannot be reused.
        work, seen = [(value, 0)], set()
        while work:
            charge()
            item, depth = work.pop()
            if item is None:
                continue
            if depth > search.source_max_depth:
                raise Limit("source_catalog_depth_limit")
            if is_uvm(item) and path in files:
                files[path]["used"] = True
            marker = id(item)
            if marker in seen:
                continue
            seen.add(marker)
            owners.append(item)
            if item.isAlias:
                work.append((item.targetType.type, depth + 1))
                continue
            item = item.canonicalType
            if is_uvm(item) and path in files:
                files[path]["used"] = True
            kind = _kind_name(item)
            if item.isError:
                gap(path, "source_type_binding_unresolved", ("usage",))
            elif item.isArray:
                work.append((item.arrayElementType, depth + 1))
                if item.isAssociativeArray:
                    work.append((item.associativeIndexType, depth + 1))
            elif kind == "ClassType":
                if classes is not None:
                    classes.observe_type(item)
                work.append((item.baseClass, depth + 1))
                for interface in item.implementedInterfaces:
                    work.append((interface, depth + 1))
            elif item.isStruct or item.isPackedUnion or item.isUnpackedUnion:
                if not str(item.name):
                    gap(path, 'source_anonymous_aggregate_inventory_uncovered', ('inventory',))
                enqueue(item, identity(item), path, depth + 1)
                for field in item:
                    charge()
                    if hasattr(field, "type"):
                        work.append((field.type, depth + 1))
            elif item.isVirtualInterface or item.isCovergroup or item.isTypeRefType:
                gap(path, "source_type_usage_domain_uncovered", ("usage",))

    def declaration(symbol, scope_key, owner_file, *, scope_identity_unknown=False):
        kind, where = _kind_name(symbol), source(symbol)
        public = (_KINDS.get(kind) if kind != "Definition" else
                  str(symbol.definitionKind.name).lower())
        if kind == "Subroutine":
            public = str(symbol.subroutineKind.name).lower()
        if public is None or not str(symbol.name) or symbol.syntax is None:
            return
        if where is None:
            native_owner = source_owner(symbol.location)
            if native_owner is not None and native_owner not in files:
                return
            # Synthetic/compiler supplied symbols have no source declaration.
            if symbol.syntax is not None:
                gap(owner_file, "source_declaration_location_unbound", ("inventory",))
            return
        row = files.get(where["file"])
        if row is None:
            return
        key = identity(symbol, where)
        if key is None:
            gap(where['file'], 'source_declaration_identity_unbound', ('inventory',))
            if classes is not None:
                classes.gaps.add('source_class_declaration_identity_unbound')
            return
        if classes is not None:
            classes.declaration(symbol, scope_key)
        gaps = []
        if scope_identity_unknown:
            gaps.append('source_scope_identity_unbound')
            gap(where['file'], gaps[-1], ('inventory',))
            if classes is not None:
                classes.gaps.add(gaps[-1])
        if kind == "GenericClassDef":
            gaps.append("source_generic_class_members_not_enumerated")
            gap(where["file"], "source_generic_class_members_not_enumerated")
        row["symbols"].setdefault(key, dict(key=key, name=str(symbol.name),
            qualified_name=str(symbol.lexicalPath) or str(symbol.name), kind=public,
            location=where, scope_key=scope_key, declaration_key=key,
            type_key=key if bool(symbol.isType) else None,
            parameterization=[], external=False, gaps=gaps, declaration_status="declaration"))

    def observe(node, owner_file):
        charge()
        kind = _kind_name(node)
        where = (source(node) if isinstance(node, Symbol) else
                 location(node.sourceRange.start) if isinstance(node, Expression) else None)
        path = (where["file"] if where is not None else
                source_owner(node.location) if isinstance(node, Symbol) else
                source_owner(node.sourceRange.start) if isinstance(node, Expression) else owner_file)
        if path is None:
            path = owner_file
        native_location = (node.location if isinstance(node, Symbol) else
                           node.sourceRange.start if isinstance(node, Expression) else None)
        if native_location is not None and projector.source_manager.isMacroLoc(native_location):
            expanded = location(native_location, expanded=True)
            gap(expanded['file'] if expanded is not None else path,
                'source_macro_usage_domain_uncovered', ('usage', 'imports'))
        if kind in {"ExplicitImport", "WildcardImport"}:
            package = node.package
            if package is None:
                gap(path, "source_import_binding_unresolved", ("imports", "usage"))
            elif str(package.name) == "uvm_pkg" and path in files:
                files[path]["imported"] = files[path]["used"] = True
        if hasattr(node, "type"):
            observe_type(node.type, path)
        if kind == "TypeAlias":
            observe_type(node, path)
        elif kind == "TypeParameter":
            observe_type(node.targetType.type, path)
        elif kind == "ClassType":
            observe_type(node, path)
        if isinstance(node, Expression):
            if kind not in _USAGE_EXPRESSIONS:
                gap(path, 'source_expression_usage_domain_uncovered', ('usage',))
            if is_uvm(node.getSymbolReference()) and path in files:
                files[path]["used"] = True
            if kind == "Call" and not node.isSystemCall and is_uvm(node.subroutine) and path in files:
                files[path]["used"] = True
            if node.bad:
                gap(path, "source_expression_binding_unresolved", ("usage",))
        elif isinstance(node, (AssertionExpr, Constraint, Pattern)):
            gap(path, 'source_expression_usage_domain_uncovered', ('usage',))

    def enqueue(scope, scope_key, path, depth, key=None):
        pending.append((scope, scope_key, path, depth, key,
                        scope_key is None and key is None))

    if "class_inheritance" in search.source_capabilities:
        from .slang_class_catalog import ClassCollector
        def class_depth_limit():
            raise Limit("source_catalog_depth_limit")
        def class_parameter_limit():
            raise Limit("source_class_parameter_limit")
        def class_reserve(size):
            nonlocal coordinate_bytes
            if coordinate_bytes + size > search.source_max_bytes:
                raise Limit("source_catalog_coordinate_byte_limit")
            coordinate_bytes += size
        classes = ClassCollector(charge=charge, identity=identity, source=source,
                                 enqueue=enqueue, kind_name=_kind_name, files=files,
                                 max_depth=search.source_max_depth, depth_limit=class_depth_limit,
                                 parameter_limit=class_parameter_limit, reserve=class_reserve)

    def expression_walk(value, scope_key, path, depth, *, scope_identity_unknown=False):
        if value is None:
            return
        work = [(value, scope_key, depth, scope_identity_unknown)]
        while work:
            value, current_scope, level, current_scope_unknown = work.pop()
            if level > search.source_max_depth:
                raise Limit('source_catalog_depth_limit')
            def callback(node):
                observe(node, path)
                if hasattr(node, 'blockSymbol') and hasattr(node, 'body'):
                    block = node.blockSymbol
                    block_key = identity(block) if block is not None else current_scope
                    if block is not None:
                        enqueue(block, block_key, path, level + 1)
                    work.append((node.body, block_key, level + 1,
                                 block_key is None if block is not None else current_scope_unknown))
                    return VisitAction.Skip
                if isinstance(node, Symbol):
                    declaration(node, current_scope, path, scope_identity_unknown=current_scope_unknown)
                    if node.isScope and _kind_name(node) in _SCOPE_KINDS:
                        enqueue(node, identity(node), path, level + 1)
                        return VisitAction.Skip
                return VisitAction.Advance
            value.visit(callback)

    try:
        # This capability deliberately has no connectivity topology. Preserve
        # the frontend's actual top domain separately, before catalog limits
        # can interrupt declaration traversal. Retained names share the byte
        # budget with physical coordinate indexes; no instance facts are made.
        for instance in root.topInstances:
            charge()
            name = str(instance.hierarchicalPath)
            required = len(name.encode('utf-8')) + 64
            if coordinate_bytes + required > search.source_max_bytes:
                raise Limit('source_catalog_coordinate_byte_limit')
            coordinate_bytes += required
            semantic_tops.append(name)
            top_instances.append(instance)
        # Retain real namespace declarations before global buffer accounting
        # can consume the finite member budget. Locations lazily index only
        # requested native physical buffers, with the same byte/visit caps.
        definitions = list(root.compilation.getDefinitions())
        for symbol in definitions:
            charge()
            declaration(symbol, None, None)
        packages = []
        for package in root.compilation.getPackages():
            charge()
            declaration(package, None, None)
            packages.append(package)
        # Buffers prove empty/header files were processed; membership alone
        # cannot establish that the frontend parsed a file or its language.
        # Every buffer, including expansions, remains charged and cancellable.
        for buffer in projector.source_manager.getAllBuffers():
            charge()
            if str(projector.source_manager.getBufferKind(buffer).name) in {'Macro', 'MacroArg'}:
                continue
            path = binding(str(projector.source_manager.getFullPath(buffer)))
            if path is not None:
                processed.add(path)
            if path in files:
                coordinate(buffer, path)
        for path in files:
            if path not in processed:
                gap(path, "source_file_not_processed_by_frontend")
        for index, unit in enumerate(root.compilationUnits):
            enqueue(unit, None, None, 0, ("compilation_unit", index))
        for package in packages:
            enqueue(package, identity(package), None, 0)
        for instance in top_instances:
            charge()
            key = identity(instance.definition)
            represented.add(key)
            enqueue(instance.body, key, None, 0)
        while pending:
            charge()
            scope, scope_key, owner_file, depth, explicit_key, scope_identity_unknown = pending.popleft()
            if depth > search.source_max_depth:
                raise Limit("source_catalog_depth_limit")
            # CU ordinal, semantic source owner and actual elaborated path are
            # distinct. Never collapse empty-path compilation units.
            if explicit_key is not None:
                key = explicit_key
            else:
                anchor = identity(scope)
                # This fallback is private traversal state, never a public
                # declaration/scope key. `owners` prevents Python-ID reuse.
                key = (_kind_name(scope), anchor if anchor is not None else
                       ('unbound_native_scope', id(scope)), str(scope.hierarchicalPath))
            if key in seen_scopes:
                continue
            if scope_visits >= search.source_max_scopes:
                raise Limit("source_catalog_scope_limit")
            scope_visits += 1
            seen_scopes.add(key)
            owners.append(scope)
            for symbol in scope:
                charge()
                if _kind_name(scope) == 'Subroutine' and symbol in (scope.returnValVar, scope.thisVar):
                    continue
                kind, where = _kind_name(symbol), source(symbol)
                path = where["file"] if where is not None else source_owner(symbol.location) or owner_file
                declaration(symbol, scope_key, path, scope_identity_unknown=scope_identity_unknown)
                observe(symbol, path)
                if kind == "Instance":
                    definition_key = identity(symbol.definition)
                    represented.add(definition_key)
                    enqueue(symbol.body, definition_key, path, depth + 1)
                elif kind in {"InstanceArray", "GenerateBlockArray"}:
                    for element in (symbol.elements if kind == "InstanceArray" else symbol.entries):
                        charge()
                        if kind == "InstanceArray":
                            definition_key = identity(element.definition)
                            represented.add(definition_key)
                            enqueue(element.body, definition_key, path, depth + 1)
                        elif not element.isUninstantiated:
                            enqueue(element, scope_key, path, depth + 1)
                elif kind == "GenerateBlock":
                    if symbol.isUninstantiated:
                        gap(path, "source_unelaborated_generate_declarations")
                    else:
                        enqueue(symbol, scope_key, path, depth + 1)
                elif kind in _SCOPE_KINDS:
                    enqueue(symbol, identity(symbol), path, depth + 1)
                    if kind == "Subroutine":
                        observe_type(symbol.returnType, path)
                        symbol_key = identity(symbol)
                        expression_walk(symbol.body, symbol_key, path, depth + 1,
                                        scope_identity_unknown=symbol_key is None)
                elif kind == 'ProceduralBlock':
                    for block in symbol.blocks:
                        charge()
                        enqueue(block, identity(block), path, depth + 1)
                    expression_walk(symbol.body, scope_key, path, depth + 1,
                                    scope_identity_unknown=scope_identity_unknown)
                elif kind == 'ContinuousAssign':
                    expression_walk(symbol.assignment, scope_key, path, depth + 1,
                                    scope_identity_unknown=scope_identity_unknown)
                elif kind not in {*_KINDS, "Definition", "ExplicitImport", "WildcardImport", *_NO_USAGE}:
                    # An unknown compiler leaf with neither a source nor
                    # syntax nor captured owner is outside the source inventory.
                    # Real unbound syntax and captured-owner gaps stay unknown;
                    # containers and positive reference checks ran above.
                    if (where is not None or symbol.syntax is not None or path in files
                            or bool(symbol.isScope)):
                        gap(path, "source_symbol_domain_uncovered")
                if hasattr(symbol, "initializer"):
                    expression_walk(symbol.initializer, scope_key, path, depth + 1,
                                    scope_identity_unknown=scope_identity_unknown)
        for definition in definitions:
            charge()
            where = source(definition)
            if where is not None and identity(definition) not in represented:
                gap(where["file"], "source_definition_body_not_elaborated")
    except Limit as exc:
        limited, stop_reason = True, str(exc)
        gap(None, stop_reason)
    if projector.options.blocking_diagnostic_total:
        gap(None, "source_catalog_frontend_diagnostics")
    for exclusion in projector.options.exclusions:
        if exclusion.code in {"protected_region", "opaque_protected_ip", "encrypted_source", "compile_manifest_incomplete"}:
            gap(None, "source_catalog_opaque_or_incomplete_context")
    return dict(version=CATALOG_VERSION, capabilities=list(search.source_capabilities),
        source_files=list(search.source_files), semantic_tops=sorted(semantic_tops),
        declaration_domain=DECLARATION_DOMAIN,
        usage_domain=USAGE_DOMAIN, member_visits=visits, scope_visits=scope_visits,
        limited=limited, stop_reason=stop_reason, coordinate_bytes=coordinate_bytes,
        limits=dict(members=search.max_members, scopes=search.source_max_scopes, depth=search.source_max_depth,
                    coordinate_bytes=search.source_max_bytes),
        **({"classes": classes.payload(limited=limited,
            diagnostics=bool(projector.options.blocking_diagnostic_total),
            context_incomplete=any(exclusion.code in {
                "protected_region", "opaque_protected_ip", "encrypted_source", "compile_manifest_incomplete"}
                for exclusion in projector.options.exclusions))} if classes is not None else {}),
        files={path: dict(symbols=list(row["symbols"].values()),
            inventory_gaps=sorted(row["inventory"]), import_gaps=sorted(row["imports"]),
            usage_gaps=sorted(row["usage"]),
            gaps=sorted(row["inventory"] | row["imports"] | row["usage"]),
            complete=not row["inventory"],
            has_uvm_import=True if row["imported"] else None if row["imports"] else False,
            uses_uvm=True if row["used"] else None if row["usage"] else False)
            for path, row in files.items()})
