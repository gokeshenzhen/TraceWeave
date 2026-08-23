import hashlib
import threading
import time

from src import compile_source_index
from src.compile_session_snapshot import CompileSessionSnapshotBuilder
from src.compile_source_index import CompileSourceIndex
from src.tb_hierarchy_builder import build_hierarchy


def test_preload_deduplicates_content_and_reuses_exact_byte_snapshot(tmp_path):
    source = tmp_path / "top.sv"
    raw = b"module top;\r\nendmodule\r\n"
    source.write_bytes(raw)
    index = CompileSourceIndex(max_bytes=1024, max_files=4)

    index.preload([str(source), str(source)])
    first = index.read(str(source))
    second = index.read(str(source))
    metrics = index.metrics_snapshot()

    assert first is second
    assert first.text == "module top;\nendmodule\n"
    assert first.snapshot is not None
    assert first.snapshot.sha256 == hashlib.sha256(raw).hexdigest()
    assert metrics["compile_source_index_status"] == "ready"
    assert metrics["compile_source_index_planned_file_count"] == 1
    assert metrics["compile_source_index_physical_read_count"] == 1
    assert metrics["compile_source_index_cache_hit_count"] == 2
    assert metrics["compile_source_index_entry_count"] == 1


def test_capacity_bypass_never_turns_into_partial_text_cache(tmp_path):
    first = tmp_path / "first.sv"
    second = tmp_path / "second.sv"
    first.write_text("module first; endmodule\n")
    second.write_text("module second; endmodule\n")
    index = CompileSourceIndex(max_bytes=1, max_files=4)

    index.preload([str(first), str(second)])
    index.read(str(first))
    index.read(str(first))
    metrics = index.metrics_snapshot()

    assert metrics["compile_source_index_status"] == "bypass_capacity"
    assert metrics["compile_source_index_entry_count"] == 0
    assert metrics["compile_source_index_physical_read_count"] == 2
    assert metrics["compile_source_index_cache_hit_count"] == 0


def test_decoded_resident_limit_rolls_back_the_entire_preload(tmp_path):
    first = tmp_path / "first.sv"
    second = tmp_path / "second.sv"
    first.write_text("module first; endmodule\n")
    second.write_text("module second; endmodule\n")
    index = CompileSourceIndex(
        # The raw source set fits, but two retained Python entries do not.
        max_bytes=1000,
        max_files=4,
    )

    index.preload([str(first), str(second)])
    metrics = index.metrics_snapshot()

    assert metrics["compile_source_index_planned_bytes"] < 1000
    assert metrics["compile_source_index_status"] == "bypass_capacity"
    assert metrics["compile_source_index_entry_count"] == 0
    assert metrics["compile_source_index_cache_bytes"] == 0


def test_lazy_same_path_miss_is_single_flight(tmp_path, monkeypatch):
    source = tmp_path / "late.svh"
    source.write_text("leaf u_leaf();\n")
    real_read = compile_source_index.read_source_content
    read_started = threading.Event()
    release = threading.Event()
    read_count = 0

    def delayed_read(path):
        nonlocal read_count
        read_count += 1
        read_started.set()
        release.wait(timeout=5)
        return real_read(path)

    monkeypatch.setattr(
        compile_source_index,
        "read_source_content",
        delayed_read,
    )
    index = CompileSourceIndex(max_bytes=1024, max_files=4)
    index.preload([])
    results = []

    def read():
        results.append(index.read_text(str(source)))

    threads = [threading.Thread(target=read) for _ in range(2)]
    for thread in threads:
        thread.start()
    assert read_started.wait(timeout=5)
    time.sleep(0.02)
    release.set()
    for thread in threads:
        thread.join(timeout=5)

    assert results == ["leaf u_leaf();\n", "leaf u_leaf();\n"]
    assert read_count == 1
    metrics = index.metrics_snapshot()
    assert metrics["compile_source_index_cache_miss_count"] == 1
    assert metrics["compile_source_index_cache_hit_count"] == 1
    assert metrics["compile_source_index_physical_read_count"] == 1


def test_snapshot_builder_accepts_indexed_content_without_rehashing(tmp_path):
    source = tmp_path / "top.sv"
    source.write_text("module top; endmodule\n")
    index = CompileSourceIndex(max_bytes=1024, max_files=4)
    index.preload([str(source)])
    builder = CompileSessionSnapshotBuilder(indexed_reader=index.read)

    assert builder.read_text(str(source)) == "module top; endmodule\n"
    snapshot = builder.finish()

    assert snapshot.complete is True
    assert snapshot.file_count == 1
    assert index.metrics_snapshot()["compile_source_index_physical_read_count"] == 1


def test_close_drops_source_text_and_future_reads_bypass_cache(tmp_path):
    source = tmp_path / "top.sv"
    source.write_text("module top; endmodule\n")
    index = CompileSourceIndex(max_bytes=1024, max_files=4)
    index.preload([str(source)])

    index.close()
    closed = index.metrics_snapshot()
    assert closed["compile_source_index_entry_count"] == 0
    assert closed["compile_source_index_cache_bytes"] == 0

    assert index.read_text(str(source)) == "module top; endmodule\n"
    after = index.metrics_snapshot()
    assert after["compile_source_index_entry_count"] == 0
    assert after["compile_source_index_physical_read_count"] == 2


def test_hierarchy_reuses_index_snapshot_and_reports_numeric_metrics(tmp_path):
    source = tmp_path / "top.sv"
    source.write_text("module top; endmodule\n")
    compile_result = {
        "simulator": "vcs",
        "primary_top": "top",
        "top_modules": ["top"],
        "files": {
            "user": [
                {
                    "path": str(source),
                    "type": "module",
                    "category": "rtl",
                }
            ]
        },
    }
    index = CompileSourceIndex(max_bytes=1024, max_files=4)
    index.preload([str(source)])

    result = build_hierarchy(
        compile_result,
        apply_source_overlay=False,
        source_index=index,
    )

    assert result["_scan_results"][0]["modules"] == ["top"]
    assert result["_compile_session_snapshot"].complete is True
    metrics = result["build_metrics"]
    assert metrics["compile_source_index_status"] == "ready"
    assert metrics["compile_source_index_physical_read_count"] == 1
    assert metrics["compile_source_index_cache_hit_count"] == 1


def test_source_change_after_preload_marks_hierarchy_snapshot_incomplete(tmp_path):
    source = tmp_path / "top.sv"
    source.write_text("module top; endmodule\n")
    compile_result = {
        "simulator": "vcs",
        "primary_top": "top",
        "top_modules": ["top"],
        "files": {
            "user": [
                {
                    "path": str(source),
                    "type": "module",
                    "category": "rtl",
                }
            ]
        },
    }
    index = CompileSourceIndex(max_bytes=4096, max_files=4)
    index.preload([str(source)])
    source.write_text("module top; logic changed; endmodule\n")

    result = build_hierarchy(
        compile_result,
        apply_source_overlay=False,
        source_index=index,
    )

    snapshot = result["_compile_session_snapshot"]
    assert snapshot.complete is False
    assert "compile_content_changed_during_scan" in snapshot.issue_codes
