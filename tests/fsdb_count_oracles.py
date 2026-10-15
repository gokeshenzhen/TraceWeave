"""Small independent raw-record oracle, including same-tick ordering."""


def public_records(raw_records, scale_fs, start_ps=0, end_ps=-1, cap=1000):
    public = [
        dict(time_ps=(tick * scale_fs + 999) // 1000, value=value)
        for tick, value in raw_records
    ]
    earlier = [row for row in public if row["time_ps"] < start_ps]
    rows = [
        row
        for row in public
        if row["time_ps"] >= start_ps and (end_ps < 0 or row["time_ps"] <= end_ps)
    ]
    return dict(
        count=len(rows),
        predecessor=earlier[-1] if earlier else None,
        rows=rows[:cap],
        truncated=len(rows) > cap,
    )


def counter_window(steps, start_ps, end_ps, stride_ps=10):
    first = max(1, (start_ps + stride_ps - 1) // stride_ps)
    last = steps if end_ps < 0 else min(steps, end_ps // stride_ps)
    return max(0, last - first + 1) + int(start_ps == 0)


SAME_TICK_100FS = [
    (0, "0"),
    (1, "1"),
    (2, "x"),
    (9, "z"),
    (10, "0"),
    (10, "1"),
    (10, "x"),
    (10, "0"),
    (11, "1"),
    (20, "z"),
    (29, "0"),
    (30, "1"),
]


def assert_native_boundary(
    consumed_ticks, scale_fs, boundary_ps, events_before_boundary, valid, peek_tick=None
):
    """Check the ABI contract without choosing the implementation's boundary."""
    observed = [(tick * scale_fs + 999) // 1000 for tick in consumed_ticks]
    if peek_tick is not None:
        observed.append((peek_tick * scale_fs + 999) // 1000)
    if not valid:
        return
    assert boundary_ps in observed
    assert any(time < boundary_ps for time in observed), (
        "boundary needs two distinct observed public times"
    )
    before = sum(
        (tick * scale_fs + 999) // 1000 < boundary_ps for tick in consumed_ticks
    )
    assert events_before_boundary == before
    assert 0 < before <= len(consumed_ticks)
