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


def bind_source_expression(parser, expr, binding=None):
    """Preserve source coordinates; translate only the independently bound root."""
    from .dynamic_binding import signal_expression
    path = binding.to_wave(expr.signal) if binding else expr.signal
    return signal_expression(parser, path, expr.bits, expr.declared_bits,
                             array_indices=expr.array_indices)
