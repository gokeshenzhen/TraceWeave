"""Exact waveform declaration binding for typed dynamic source references."""
from .connectivity_ir import BitRange
from .divergence_mapping import selected_path, split_selection
from .dynamic_evidence import Expr


def _array_declaration(parser, path, declared_bits):
    # VCD may spell the same elaborated memory word as an escaped identifier.
    # These are exact semantic spellings, not wildcard/suffix searches. If
    # distinct storage declarations match, refuse the ambiguous binding.
    names = [path]
    if '\\' not in path and '.' in path:
        scope,leaf = path.rsplit('.',1)
        names.append(scope + '.\\' + leaf)
    candidates = [n for name in names for n in (name,selected_path(name,declared_bits))]
    matches = {}
    for name in dict.fromkeys(candidates):
        try:
            d = parser.get_signal_declaration(name)
        except (KeyError,ValueError):
            continue
        if d['width'] == 1 and len(declared_bits) == 1 and d['path'] in names:
            # A bare scalar memory name ends with its unpacked index. The
            # dump-name parser cannot distinguish that from a packed bit;
            # the semantic array shape can, without inventing any storage.
            d = {**d,'declared_range':dict(left=declared_bits[0],right=declared_bits[0])}
        bounds = BitRange(**d['declared_range'])
        if d['width'] != len(declared_bits) or bounds.indices != tuple(declared_bits):
            continue
        if d['path'] not in candidates:
            continue
        matches[(d.get('storage_key',d['path']),d['width'],bounds)] = d
    if len(matches)>1:
        raise ValueError('array_element_mapping_ambiguous')
    if not matches:
        raise KeyError(path)
    return next(iter(matches.values()))


def signal_expression(parser, path, bits=(), declared_bits=(), *, array_indices=()):
    """Resolve one exact declaration, preserving generate indices and alias path."""
    # A semantic memory element's final [i] is an unpacked coordinate, never
    # a packed bit suffix. Only exact element declarations are admissible.
    base, suffix = (path, ()) if array_indices else split_selection(path)
    try:
        if array_indices:
            declaration = _array_declaration(parser,path,declared_bits)
        else:
            declaration = parser.get_signal_declaration(base)
    except (KeyError, ValueError):
        if array_indices:
            raise
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
                declared_bits=declared, array_indices=array_indices)
