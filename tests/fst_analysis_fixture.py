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


def cycle_pair(directory, widths, cycles, *, name='cycles', activity=(), scale=-12):
    """Controls settle at 10*i; the independently known accepting edge is +5."""
    widths = {'clk': 1, **widths}
    declarations = [(n if w == 1 else f'{n} [{w-1}:0]', w, 'wire',
                     'output' if 'ready' in n else 'input', None) for n,w in widths.items()]
    names = dict(zip(widths, (d[0] for d in declarations)))
    rows = []
    for i, cycle in enumerate(cycles):
        for n,w in widths.items():
            value = 0 if n == 'clk' else cycle.get(n, 1 if n in {'rst','reset'} else 0)
            bits = value*w if isinstance(value, str) else f'{value:0{w}b}'
            rows.append((10*i, names[n], bits))
        rows.append((10*i+5, 'clk', '1'))
    rows.append((10*len(cycles), 'clk', '0'))
    return write_pair(directory, declarations, rows, name=name, end=10*len(cycles), activity=activity, scale=scale)
