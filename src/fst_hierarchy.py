"""Bounded embedded FST hierarchy decoding inside the isolated worker.

Pinned libfst's IterateHier copies attribute strings into a 512-byte scope
buffer. Real enum tables exceed that size. Decode metadata before native open,
and leave digital values to libfst's separately checked traversal. No external
hierarchy files or whole-waveform conversion are supported here.
"""
import ctypes
import struct
import zlib

MAX_HIERARCHY_BYTES = 32 * 1024 * 1024
MAX_ATTRIBUTE_BYTES = 65536  # below libfst ProcessHier's 69632-byte buffer


def varint(data, pos):
    value = 0
    for shift in range(0, 70, 7):
        if pos >= len(data):
            break
        byte = data[pos]
        pos += 1
        if shift == 63 and byte > 1:
            break
        value |= (byte & 127) << shift
        if byte < 128:
            return value, pos
    raise ValueError("fst_corrupt: invalid hierarchy varint")


def hierarchy_bytes(path, native_path):
    """Bound every compressed/expanded allocation before safe decompression."""
    decoder = None

    def lz4(data, size):
        nonlocal decoder
        if not 0 < size <= MAX_HIERARCHY_BYTES or len(data) > MAX_HIERARCHY_BYTES:
            raise ValueError("fst_metadata_memory_limit")
        if decoder is None:
            decoder = ctypes.CDLL(str(native_path)).LZ4_decompress_safe
            decoder.argtypes = (ctypes.c_char_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int)
            decoder.restype = ctypes.c_int
        destination = ctypes.create_string_buffer(size)
        if decoder(data, destination, len(data), size) != size:
            raise ValueError("fst_corrupt: invalid compressed hierarchy")
        return destination.raw

    with open(path, "rb") as stream:
        while True:
            raw = stream.read(9)
            if not raw:
                break
            if len(raw) != 9:
                raise ValueError("fst_corrupt: incomplete hierarchy block")
            kind, length = raw[0], struct.unpack(">Q", raw[1:])[0]
            if length < 8:
                raise ValueError("fst_corrupt: invalid hierarchy block")
            if kind not in (4, 6, 7):
                stream.seek(length - 8, 1)
                continue
            if length < 16 or length - 16 > MAX_HIERARCHY_BYTES:
                raise ValueError("fst_metadata_memory_limit")
            size = struct.unpack(">Q", stream.read(8))[0]
            if not 0 < size <= MAX_HIERARCHY_BYTES:
                raise ValueError("fst_metadata_memory_limit")
            packed = stream.read(length - 16)
            if len(packed) != length - 16:
                raise ValueError("fst_corrupt: incomplete hierarchy block")
            if kind == 4:
                expand = zlib.decompressobj(16 + zlib.MAX_WBITS)
                try:
                    data = expand.decompress(packed, size + 1)
                except zlib.error as exc:
                    raise ValueError("fst_corrupt: invalid compressed hierarchy") from exc
                if len(data) != size or not expand.eof or expand.unused_data:
                    raise ValueError("fst_corrupt: invalid compressed hierarchy")
                return data
            if kind == 7:
                intermediate_size, offset = varint(packed, 0)
                packed = lz4(packed[offset:], intermediate_size)
            return lz4(packed, size)
    raise ValueError("fst_corrupt: embedded hierarchy missing")


def records(data):
    """Yield scope/variable facts and skip validated nonsemantic attributes."""
    pos = handle = 0

    def byte():
        nonlocal pos
        if pos >= len(data):
            raise ValueError("fst_corrupt: incomplete hierarchy record")
        result = data[pos]
        pos += 1
        return result

    def string(limit, reason):
        nonlocal pos
        end = data.find(b"\0", pos, min(len(data), pos + limit + 1))
        if end < 0:
            raise ValueError(reason)
        result = data[pos:end]
        pos = end + 1
        return result

    while pos < len(data):
        tag = byte()
        if tag == 254:
            byte()  # scope type; public contract does not expose it
            name = string(4096, "fst_scope_limit").decode("utf-8", "strict")
            string(4096, "fst_scope_limit")  # definition component
            yield ("scope", name)
        elif tag == 255:
            yield ("upscope",)
        elif tag == 252:
            byte(); byte()  # attribute type/subtype
            string(MAX_ATTRIBUTE_BYTES, "fst_metadata_attribute_limit")
            _, pos = varint(data, pos)
        elif tag == 253:
            continue
        elif 0 <= tag <= 29:
            direction = byte()
            name = string(4096, "fst_signal_width_or_name_limit").decode("utf-8", "strict")
            width, pos = varint(data, pos)
            alias, pos = varint(data, pos)
            if width > 2**32 - 1:
                raise ValueError("fst_corrupt: invalid hierarchy width")
            if alias > handle:
                raise ValueError("fst_corrupt: invalid hierarchy alias")
            if not alias:
                handle += 1
            if tag == 18:
                width = (width - 2) // 3
            yield ("var", name, width, direction, alias or handle, bool(alias), tag)
        else:
            raise ValueError("fst_corrupt: unknown hierarchy record")


def validate(data):
    # Validate attribute/name sizes before libfst Open/ProcessHier can touch
    # them. Complete records are decoded again lazily for index construction.
    for _ in records(data):
        pass
