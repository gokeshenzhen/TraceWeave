"""Same event table written to FST and VCD; expected facts live in tests."""
from fst_fixture import write_fst


def write_pair(directory, declarations, rows, *, name='analysis', scale=-12, start=0, end=40, activity=()):
    fst = write_fst(directory / (name + '.fst'), rows, declarations=declarations,
                    scale=scale, start=start, end=end, activity=activity)
    vcd = directory / (name + '.vcd')
    symbols = {}
    lines = [f'$timescale {10 ** (scale + 15)}fs $end', '$scope module top $end']
    for i, (signal, width, kind, direction, alias) in enumerate(declarations):
        symbol = symbols[alias] if alias else f's{i}'
        symbols[signal] = symbol
        lines.append(f'$var wire {width} {symbol} {signal} $end')
    lines.extend(['$upscope $end', '$enddefinitions $end', f'#{start}'])
    for at, signal, bits in sorted(rows, key=lambda r: r[0]):
        lines.extend([f'#{at}', f'b{bits} {symbols[signal]}'])
    lines.append(f'#{end}')
    vcd.write_text('\n'.join(lines) + '\n')
    return fst, vcd
