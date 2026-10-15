"""Private bounded cumulative counts at complete public-picosecond boundaries.

An anchor (t, n) means n records in the scanned window have public time < t.
No value, native handle, or unclosed same-time group is retained. The final
end + 1 anchor is implicit (a Python integer, so uint64 end cannot wrap).
"""
from array import array
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
import sys


MAX_ENTRIES = 16
MAX_BYTES = 4 * 1024 * 1024
MAX_POINTS = 32768
_UINT64_MAX = (1 << 64) - 1


@dataclass(frozen=True)
class CountIndex:
    start: int
    end: int
    total: int
    full_tail: bool
    times: array
    counts: array

    def plan(self, start, end):
        """Return [left, right) interior and its count, or an exact-scan miss."""
        if end == -1:
            if not self.full_tail:
                return None
            end = self.end
        if start < self.start or end > self.end or end < start:
            return None
        if not self.total:
            return start, end + 1, 0
        left_i = bisect_left(self.times, start)
        if left_i == len(self.times):
            return None
        left, before_left = self.times[left_i], self.counts[left_i]
        if end == self.end:
            right, before_right = end + 1, self.total
        else:
            right_i = bisect_right(self.times, end + 1) - 1
            if right_i < 0:
                return None
            right, before_right = self.times[right_i], self.counts[right_i]
        if right <= left:
            return None
        return left, right, before_right - before_left

    def accounted_bytes(self, key):
        # Include compact arrays' actual allocated capacity and conservative
        # per-entry dict/LRU/object bookkeeping, not only the 16-byte payload.
        return (sys.getsizeof(self.times) + sys.getsizeof(self.counts) +
                sys.getsizeof(self) + sys.getsizeof(self.__dict__) +
                sum(sys.getsizeof(x) for x in (self.start, self.end, self.total, self.full_tail)) +
                sys.getsizeof(key) + sum(map(sys.getsizeof, key)) + 256)


class CountIndexBuilder:
    def __init__(self, start, max_points=None):
        self.capacity = MAX_POINTS if max_points is None else max_points
        self.enabled = self.capacity >= 2 and 0 <= start <= _UINT64_MAX
        self.start = start
        self.times = array('Q', [start]) if self.enabled else array('Q')
        self.counts = array('Q', [0]) if self.enabled else array('Q')
        self.offers, self.stride, self.coarsenings = 0, 1, 0

    def offer(self, time_ps, count):
        if not self.enabled:
            return
        if not (self.times[-1] < time_ps <= _UINT64_MAX and
                self.counts[-1] <= count <= _UINT64_MAX):
            # Duplicate start anchors add no information. Other malformed
            # receipts discard the optimization, never manufacture a count.
            if time_ps == self.start and count == 0:
                return
            self.enabled = False
            return
        self.offers += 1
        if self.offers % self.stride:
            return
        if len(self.times) >= self.capacity:
            self.times, self.counts = self.times[::2], self.counts[::2]
            self.stride *= 2
            self.coarsenings += 1
            if self.offers % self.stride:
                return
        self.times.append(time_ps)
        self.counts.append(count)

    def finish(self, end, total, full_tail=False):
        if (not self.enabled or end is None or end < self.start or
                self.times[-1] > end + 1 or total < self.counts[-1] or
                not 0 <= total <= _UINT64_MAX):
            return None
        # A peek can coincide with end+1. Keep that anchor implicit instead.
        if self.times[-1] == end + 1:
            if self.counts[-1] != total:
                return None
            self.times.pop()
            self.counts.pop()
        return CountIndex(self.start, end, total, full_tail, self.times, self.counts)
