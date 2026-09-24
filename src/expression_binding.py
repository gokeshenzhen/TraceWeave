"""Exact, request-local declaration and sparse array bindings for expressions."""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from pydantic import ValidationError

from .expression_errors import ExpressionError, input_validation_issues
from .cancellation import check_cancelled
from .connectivity_ir import BitRange
from .dynamic_evidence import Expr, evaluate, MAX_EXPR_NODES
from .expression_parser import Compiler, Type, Typed
from .schemas import WaveformExpression, WaveformSelection, ExpressionArray, ExpressionElement
from .waveform_selection import Projection, SelectionParser


def integral_type(raw, *, depth=0, budget=None):
    budget = [MAX_EXPR_NODES] if budget is None else budget
    budget[0] -= 1
    if depth > 8 or budget[0] < 0:
        raise ExpressionError('expression_type_limit')
    members = tuple((m.name, m.lsb, integral_type(m.type, depth=depth+1, budget=budget)) for m in raw.members)
    if len({name for name,_,_ in members}) != len(members):
        raise ExpressionError('expression_member_duplicate')
    return Type(raw.width, raw.signed, raw.two_state, tuple(raw.packed), tuple(raw.unpacked), members)


@dataclass
class BoundExpression:
    key: str
    spec: WaveformExpression
    typed: Typed
    declarations: dict
    type_sources: dict

    @property
    def paths(self):
        """All possible backing declarations, including inactive branches."""
        return tuple(self.declarations)

    def validate(self, parser):
        for path, original in self.declarations.items():
            check_cancelled()
            if parser.get_signal_declaration(path) != original:
                raise ExpressionError('expression_declaration_changed',
                    message='dump declaration changed during expression evaluation')

    def evaluate(self, values):
        """values(path) returns one actual dump sample or an explicit missing value."""
        def sample(node):
            declaration = self.declarations[node.signal]
            projected = Projection(node.signal, BitRange(**declaration['declared_range']),
                                  # Kept local to avoid synthesizing a dump declaration.
                                  _selection(node.signal, node.bits))
            raw = values(node.signal)
            fact = raw if isinstance(raw, dict) and 'value' in raw else {'value': raw}
            return {**fact, 'value': projected.value(fact.get('value'))}
        return evaluate(self.typed.expr, sample)

    def receipt(self):
        typ = self.typed.type
        return dict(key=self.key, expr=self.spec.expr, typing=self.spec.typing,
                    kind='derived', width=typ.width, signed=typ.signed,
                    two_state=typ.two_state, type_sources=self.type_sources,
                    dependencies=list(self.paths))


def _selection(path, bits):
    from .connectivity_ir import SignalSelection
    return SignalSelection(symbol=path, bits=bits)


def bind_expression(parser, raw):
    try:
        return _bind_expression(parser, raw)
    except ValidationError as exc:
        issues = input_validation_issues(exc, raw)
        raise ExpressionError('expression_input_invalid',
            parameter=issues[0]['parameter'] if issues else None,
            message='expression_input_invalid: invalid input fields', issues=issues) from exc


def _bind_expression(parser, raw):
    spec = raw if isinstance(raw, WaveformExpression) else WaveformExpression.model_validate(raw)
    types = {}
    for name, typ in spec.types.items():
        try:
            types[name] = integral_type(typ)
        except ExpressionError as exc:
            exc.operand = name
            raise
    declarations, sources, resolved = {}, {}, {}

    def validate_array_mapping(binding, typ):
        if typ is None or not typ.unpacked:
            raise ExpressionError('expression_array_type_unresolved')
        if binding.path_template is not None:
            if len(typ.unpacked) != 1:
                raise ExpressionError('expression_array_mapping_invalid',
                    message='path_template needs exactly one declared unpacked dimension')
            left, right = typ.unpacked[0]
            if abs(right-left)+1 > 128:
                raise ExpressionError('expression_dependency_limit',
                    message='path_template exceeds 128 elements; use sparse elements bindings')
            step = 1 if right >= left else -1
            binding = ExpressionArray(elements=[ExpressionElement(indices=[i],
                signal=binding.path_template.replace('{index}', str(i)))
                for i in range(left, right+step, step)])
        seen = set()
        for entry in binding.elements:
            check_cancelled()
            indices = tuple(entry.indices)
            if (len(indices) != len(typ.unpacked) or indices in seen or
                    any(not min(r) <= i <= max(r) for i,r in zip(indices,typ.unpacked))):
                raise ExpressionError('expression_array_mapping_invalid')
            seen.add(indices)
        return binding
    # Bindings are exact names. The scope is only a caller-supplied prefix.
    def path_for(name):
        return f'{spec.scope}.{name}' if spec.scope else name

    def signal(binding, typ, name):
        check_cancelled()
        if isinstance(binding, str):
            declaration = parser.get_signal_declaration(binding)
            declared = BitRange(**declaration['declared_range'])
            if declared.width > 4096:
                raise ExpressionError('expression_width_limit')
            bits = declared.indices
        else:
            adapter = SelectionParser(parser)
            try:
                key = adapter.bind(binding.model_dump(exclude_none=True))
            except ValueError as exc:
                raise ExpressionError('expression_binding_invalid', operand=name, message=str(exc)) from exc
            projection = adapter.projections[key]
            declaration = adapter._declarations[projection.path]
            bits = projection.selection.bits
            declared = projection.declared
        if len(bits) > 4096 or declared.width > 65536:
            raise ExpressionError('expression_width_limit')
        if typ is None:
            if spec.typing != 'wave_bits':
                # A provider can attach current semantic facts without forcing a
                # commercial frontend for portable waveform-only installations.
                getter = getattr(parser, 'get_expression_type', None)
                typ = getter(name) if getter else None
                if not isinstance(typ, Type):
                    raise ExpressionError('expression_type_unresolved', operand=name,
                        message='expression_type_unresolved: ' + name)
                sources[name] = 'semantic_provider'
            else:
                packed = ((declared.left, declared.right),) if isinstance(binding, str) else ((len(bits)-1,0),)
                typ = Type(len(bits), packed=packed)
                sources[name] = 'wave_bits'
        else:
            sources[name] = 'explicit'
        if typ.width != len(bits) or typ.unpacked:
            raise ExpressionError('expression_dump_type_shape_mismatch')
        path = declaration['path']
        declarations[path] = declaration
        if len(declarations) > 128 or sum(d['width'] for d in declarations.values()) > 65536:
            raise ExpressionError('expression_dependency_limit')
        return Typed(Expr('signal',typ.width,signal=path,bits=bits,declared_bits=declared.indices,
                          signed=typ.signed,two_state=typ.two_state),typ)

    def array(binding, typ, name):
        binding = validate_array_mapping(binding,typ)
        leaf = replace(typ, unpacked=())
        elements = {}
        for entry in binding.elements:
            check_cancelled()
            indices = tuple(entry.indices)
            # A mapped but absent element is missing evidence, not an X value.
            try:
                elements[indices] = signal(entry.signal, leaf, name).expr
            except KeyError:
                elements[indices] = Expr('unsupported',leaf.width,reason='array_element_not_dumped')
        def build(entries, depth):
            check_cancelled()
            if depth == len(typ.unpacked):
                return next(iter(entries.values()))
            groups = {}
            for indices,node in entries.items():
                groups.setdefault(indices[depth],{})[indices] = node
            keys = sorted(groups)
            return Expr('array',typ.width,tuple(build(groups[i],depth+1) for i in keys),
                        bits=tuple(keys),bounds=typ.unpacked[depth],signed=typ.signed,two_state=typ.two_state)
        sources[name] = 'explicit'
        return Typed(build(elements,0),typ)

    def resolve(name):
        if name in resolved:
            return resolved[name]
        if name in spec.constants:
            def no_names(name):
                raise KeyError(name)
            try:
                result = Compiler(no_names,types).compile(spec.constants[name])
            except ExpressionError as exc:
                if exc.code == 'expression_signal_unresolved':
                    raise ExpressionError('expression_constant_required', operand=name,
                        message=str(exc)) from exc
                raise
            # Unknown literals are valid constants too; only dependencies are
            # forbidden. The parser above has no signal resolver.
        else:
            binding = spec.bindings.get(name,path_for(name))
            typ = types.get(name)
            try:
                result = array(binding,typ,name) if isinstance(binding,ExpressionArray) else signal(binding,typ,name)
            except ExpressionError as exc:
                if exc.operand is None:
                    exc.operand = name
                if exc.reason == 'expression_array_type_unresolved':
                    exc.parameter = 'types.' + name
                raise
        resolved[name] = result
        return result

    compiler = Compiler(resolve,types)
    typed = compiler.compile(spec.expr)
    for name in compiler.type_uses:
        sources.setdefault(name,'explicit')
        if isinstance(spec.bindings.get(name),ExpressionArray):
            validate_array_mapping(spec.bindings[name],types[name])
    # Unused bindings never force waveform reads or enter the dependency list.
    used = set()
    pending = [typed.expr]
    while pending:
        node = pending.pop()
        if node.op == 'signal':
            used.add(node.signal)
        pending.extend(node.args)
    declarations = {p:d for p,d in declarations.items() if p in used}
    digest = hashlib.sha256(json.dumps(spec.model_dump(),sort_keys=True,separators=(',',':')).encode()).hexdigest()[:20]
    return BoundExpression('expr@'+digest,spec,typed,declarations,sources)
