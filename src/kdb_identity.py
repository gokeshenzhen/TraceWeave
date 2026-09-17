"""Bounded KDB identity that ignores transient NPI lock-file directory mtimes."""

import os
from .divergence_compare import file_identity

# Elaboration maps name the compiled database. Do not enumerate every library
# object (a full-design scan), or use directory mtime: NPI creates/removes locks.
_ELABORATION_MAPS = ("_libFileMap", "_nstnElabDB", "_smElabDBMap", "_stnElabDB")


def kdb_identity(path):
    identity = file_identity(path)
    if identity is None or not os.path.isdir(path):
        return identity
    return (
        identity[0],
        identity[1],
        identity[2],
        tuple(
            (name, file_identity(os.path.join(path, name)))
            for name in _ELABORATION_MAPS
        ),
    )
