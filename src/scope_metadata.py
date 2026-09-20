"""Private bounded metadata paging contract; cursors never leave a request.

Page order is case-sensitive path order, independent of public keyword search.
The parser's index generation and file stat identity must both stay unchanged.
"""
from dataclasses import dataclass
import os

PAGE_ITEMS = 1024
PAGE_BYTES = 1024 * 1024
PAGE_SCAN = 4096


def normalize_scope(scope):
    # An escaped identifier may itself end with a dot. Such paths are exact;
    # the optional trailing hierarchy separator applies only to plain names.
    value = scope or ""
    return value if "\\" in value else value.rstrip(".")


class ScopeIdentityChanged(RuntimeError):
    """The current request cannot combine metadata from different indexes."""


def file_identity(path):
    stat = os.stat(path)
    return (os.path.realpath(path), stat.st_dev, stat.st_ino, stat.st_size,
            stat.st_mtime_ns, stat.st_ctime_ns)


@dataclass(frozen=True)
class ScopeCursor:
    epoch: object
    identity: tuple
    scope: str
    direct: bool
    next_path: str

    def validate(self, epoch, identity, scope, direct):
        if self.epoch is not epoch or self.identity != identity:
            raise ScopeIdentityChanged("waveform index changed during scope enumeration")
        if self.scope != scope or self.direct != direct:
            raise ValueError("scope cursor does not match query")


def page_limits(max_items, max_bytes, max_visited):
    values = (max_items, max_bytes, max_visited)
    if any(isinstance(n, bool) or not isinstance(n, int) or n <= 0 for n in values):
        raise ValueError("scope page limits must be positive integers")
    return min(max_items, PAGE_ITEMS), min(max_bytes, PAGE_BYTES), min(max_visited, PAGE_SCAN)
