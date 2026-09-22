"""Exact waveform declaration binding for typed dynamic source references."""
from .connectivity_ir import BitRange
from .divergence_mapping import selected_path, split_selection
from .dynamic_evidence import Expr


def signal_expression(parser, path, bits=(), declared_bits=()):
    """Resolve one exact declaration, preserving generate indices and alias path."""
    base, suffix = split_selection(path)
    try:
        declaration = parser.get_signal_declaration(base)
    except (KeyError, ValueError):
        try:
            declaration = parser.get_signal_declaration(path)
        except (KeyError, ValueError):
            if not declared_bits:
                raise
            declaration = parser.get_signal_declaration(selected_path(base, declared_bits))
    if not 1 <= declaration['width'] <= 65536:
        raise ValueError('dynamic_bit_mapping_unavailable')
    bounds = BitRange(**declaration['declared_range'])
    if bounds.width != declaration['width']:
        raise ValueError('dynamic_bit_mapping_unavailable')
    declared = bounds.indices
    selected = tuple(bits or suffix or declared)
    if not set(selected).issubset(declared) or len(selected) > 4096 or len(set(selected)) != len(selected):
        raise ValueError('dynamic_bit_mapping_unavailable')
    return Expr('signal', len(selected), signal=declaration['path'], bits=selected,
                declared_bits=declared)
