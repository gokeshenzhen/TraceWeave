"""Small four-state abstract vectors. '?' denotes dynamic, not an RTL X."""


def vector(size, regions):
    bits = ['?'] * size
    for row in regions:
        end = size - row['lsb_offset']
        bits[end-row['width']:end] = row['bits']
    return ''.join(bits)


def value(bits):
    regions, end = [], len(bits)
    while end:
        if bits[end-1] == '?':
            end -= 1
            continue
        start = end - 1
        while start and bits[start-1] != '?':
            start -= 1
        regions.append(dict(lsb_offset=len(bits)-end, width=end-start, bits=bits[start:end]))
        end = start
    return len(bits), regions, '?' not in bits
