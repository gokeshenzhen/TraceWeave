"""Evidence-bound naming between one current compile context and one dump.

This only maps roots. It never interprets an index, invents a declaration or
proves that current source bytes produced a historical waveform.
"""
from dataclasses import dataclass
import re

from .cancellation import check_cancelled
from .scope_metadata import file_identity


@dataclass(frozen=True)
class RootBinding:
    wave_root: str
    design_root: str
    wave_identity: tuple

    def to_design(self, path):
        return self.design_root + path[len(self.wave_root):] if path.startswith(self.wave_root + '.') else path

    def to_wave(self, path):
        return self.wave_root + path[len(self.design_root):] if path.startswith(self.design_root + '.') else path

    def receipt(self, path):
        return dict(status='bound', wave_signal=path, source_signal=self.to_design(path),
                    wave_root=self.wave_root, design_root=self.design_root,
                    evidence=['verilator_compile_top', 'exact_wave_declaration'],
                    historical_source_identity='not_proven')


def bind_root(parser, path, compile_result, *, top_hint=None):
    """Recognize Verilator's wrapper only with a unique compiled top and dump.

    A real TOP top always wins. Escaped identifiers are left for the semantic
    resolver; no split or suffix lookup is used to reinterpret their spelling.
    """
    check_cancelled()
    tops = tuple(dict.fromkeys(compile_result.get('top_modules') or ()))
    if (compile_result.get('simulator') != 'verilator' or len(tops) != 1
            or tops[0] == 'TOP' or not path.startswith('TOP.' + tops[0] + '.')
            or top_hint not in (None, tops[0]) or '\\' in tops[0]):
        return None
    identity = file_identity(parser.file_path)
    try:
        declaration = parser.get_signal_declaration(path)
    except (KeyError, ValueError):
        return None
    # Bare vector aliases are accepted only when the exact base is declared.
    # No basename/suffix match, including a coincidentally identical child, wins.
    declared = declaration.get('path', '')
    vector = re.fullmatch(r'(.*)\[(-?\d+):(-?\d+)\]', declared)
    exact_base = (vector.group(1) if vector and
                  abs(int(vector.group(2))-int(vector.group(3)))+1 == declaration['width'] else None)
    if path not in (declared, exact_base):
        return None
    if identity != file_identity(parser.file_path):
        raise ValueError('wave_design_binding_changed')
    return RootBinding('TOP.' + tops[0], tops[0], identity)


def bind_source_expression(parser, expr, binding=None, *, engine=None):
    """Preserve source coordinates; translate only the independently bound root."""
    from .dynamic_binding import signal_expression
    path = binding.to_wave(expr.signal) if binding else expr.signal
    try:
        bound = signal_expression(parser, path, expr.bits, expr.declared_bits,
                                  array_indices=expr.array_indices)
        if expr.declared_bits and tuple(expr.declared_bits) != bound.declared_bits:
            raise ValueError('source_wave_shape_mismatch')
        return bound
    except (KeyError, ValueError):
        if engine is None:
            raise
    # A dump may expose just a struct field. Map through this artifact's typed
    # aliases, never through a suffix search or a numeric-index guess.
    parts = expr.signal.split('.')
    instance = next((p for i in range(len(parts)-1, 0, -1)
                     if (p := '.'.join(parts[:i])) in engine.instance_index), None)
    if instance is None:
        raise KeyError(path)
    definition = engine.definition_index[engine.instance_index[instance].definition_id]
    symbol = expr.signal[len(instance) + 1:]
    matches = {}
    for member in definition.packed_members:
        check_cancelled()
        if member.aggregate != symbol or not set(expr.bits).issubset(member.aggregate_bits):
            continue
        bit_map = dict(zip(member.aggregate_bits, member.packed_range.indices))
        local_bits = tuple(bit_map[b] for b in expr.bits)
        alias = instance + '.' + member.name
        alias = binding.to_wave(alias) if binding else alias
        try:
            # A typed element alias ending [i] is a storage coordinate.
            indices = tuple(int(i) for i in re.findall(r'\[(-?\d+)\]', member.name)) if member.name.endswith(']') else ()
            bound = signal_expression(parser, alias, local_bits, member.packed_range.indices,
                                      array_indices=indices)
            if bound.declared_bits != member.packed_range.indices:
                continue
            declaration = parser.get_signal_declaration(bound.signal)
        except (KeyError, ValueError):
            continue
        key = (declaration.get('storage_key', bound.signal), bound.bits, bound.declared_bits)
        matches[key] = bound
    if len(matches) > 1:
        raise ValueError('packed_member_mapping_ambiguous')
    if not matches:
        raise KeyError(path)
    return next(iter(matches.values()))
