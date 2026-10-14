"""Freeze-comparator and independent fixture-oracle guards (no SDK required)."""
import copy

from scripts.benchmark_fsdb_optimization import (
    canonical, classify_metrics, expected_bits, protocol_oracle, real_workloads, summarize_pairs,
)


def test_normalization_has_exact_path_and_preserves_cursor_anchor():
    response={"elapsed_ms":1,"nested":{"elapsed_ms":2},
        "cursor":{"name":"hs_abcd","time_ps":10,"metadata":{"valid":"top.valid"}},
        "coverage_status":"truncated","transition_count_is_lower_bound":True}
    result=canonical(response)
    assert "elapsed_ms" not in result
    assert result["nested"]=={"elapsed_ms":2}
    assert result["cursor"]==response["cursor"]
    assert result["coverage_status"]=="truncated" and result["transition_count_is_lower_bound"]
    assert response["elapsed_ms"]==1


def test_original_real_workloads_keep_47_response_shape_per_case():
    case=dict(wave={"realpath":"/wave.fsdb"},signals=dict(clock="clk",data="data",data2="data2",sparse="sparse"),
        window_ps=[3,2003],scattered_times_ps=[9003,1003,5003])
    scenarios=real_workloads(case)
    assert len(scenarios)==13
    assert sum(len(row["steps"])*3+len(row.get("prelude",[])) for row in scenarios.values())==47
    assert scenarios["cycles_global"]["steps"][0]["arguments"]["cycle_index_origin"]=="global"
    assert scenarios["sparse_fresh"].get("prelude") is None


def test_protocol_formula_has_nonzero_cross_boundary_evidence():
    rows=protocol_oracle(256)
    assert len(rows)==10
    clean=rows["top.fast.vr0"]
    assert clean["sample_count"]==256 and clean["stall_count"]==96
    assert clean["ready_without_valid_cycles"]==16
    assert clean["max_stall_cycles"]==24 and clean["max_stall_begin_ps"]==1130
    assert clean["ended_in_stall"] and clean["final_stall_cycles"]==8
    assert clean["payload_hold_violations"]==0
    assert rows["top.fast.vr1"]["payload_hold_violations"]==3
    assert rows["top.fast.vr2"]["valid_deassert_violations"]==4
    assert rows["top.fast.vr4"]["unknown_sample_cycles"]==8
    assert rows["top.fast.m_if1"]["x_while_valid_violations"]==4
    assert rows["top.fast.m_if1"]["payload_hold_violations"]==3
    assert rows["top.fast.m_if1"]["write_data_hold_violations"]==3
    assert rows["top.fast.m_if0"]["write_data_hold_violations"]==0
    assert rows["top.slow.vr6"]["sample_count"]==128


def test_window_oracle_respects_absolute_edges_and_scope():
    result=protocol_oracle(256,20*64,20*192,"top.fast.vr1")
    assert set(result)=={"top.fast.vr1"}
    row=result["top.fast.vr1"]
    assert row["sample_count"]==128 and row["payload_hold_violations"]==1
    assert row["max_stall_begin_ps"]==20*120+10


def test_protocol_oracle_separates_full_prefix_and_complete_long_trace():
    # The unchanged 64 MiB control clock budget retains 130055 one-bit
    # records; safe before-edge sampling yields exactly 65027 rising edges.
    prefix=(64*1024**2//(512+4)-1)//2
    assert prefix==65027
    rows=protocol_oracle(131072,clock_prefix_samples=prefix)
    assert len(rows)==10 and all(row["sample_count"]==prefix for row in rows.values())
    assert all(row["transition_data_truncated"] for row in rows.values())
    ahb=rows["top.fast.m_if1"]
    assert ahb["payload_hold_violations"]==1016
    assert ahb["write_data_hold_violations"]==1016
    assert ahb["final_stall_cycles"]==11
    complete=protocol_oracle(32768,clock_prefix_samples=prefix)
    assert not any(row["transition_data_truncated"] for row in complete.values())
    assert complete["top.fast.vr1"]["payload_hold_violations"]==511
    assert complete["top.slow.vr6"]["sample_count"]==16384
    window=protocol_oracle(131072,1280,3840,clock_prefix_samples=prefix)
    assert not any(row["transition_data_truncated"] for row in window.values())


def test_wide_bus_guard_checks_full_value_and_exact_count():
    import pytest
    from scripts.benchmark_fsdb_optimization import validate_result
    bits=format(0x5A00000000000000,"064b")*16
    oracle=dict(kind="wide_bus",events=[[0,"0"*1024],[30000,bits],[60000,bits[:-1]+"1"]])
    item=dict(id="wide_1024",oracle=oracle)
    point=dict(tool="get_signal_at_time",arguments=dict(time_ps=30000))
    assert validate_result(item,point,dict(value=dict(bin=bits)))
    with pytest.raises(AssertionError):validate_result(item,point,dict(value=dict(bin=bits[1:])))
    step=dict(tool="get_signal_transitions",arguments=dict(start_time_ps=0,end_time_ps=-1,max_transitions=1))
    response=dict(transitions=[dict(time_ps=0,value=dict(bin="0"*1024))],transition_count=3,truncated=True)
    assert validate_result(item,step,response)
    with pytest.raises(AssertionError):validate_result(item,step,dict(response,transition_count=1))


def test_pair_statistics_refuse_missing_arm_or_semantic_change():
    import pytest
    metrics={"query":dict(first_ms=1,repeat_median_ms=2,maximum_ms=3,process_wall_ms=4,
        peak_rss_kib=100,after_close_rss_kib=80)}
    row=dict(id="w",round=0,status="completed",semantics=["same"],prelude_semantics=[],metrics=metrics)
    a=dict(row,arm="A");b=copy.deepcopy(dict(row,arm="B"));b["metrics"]["query"]["first_ms"]=2
    plan={"workloads":[dict(id="w",pairs=1,capacity_only=False)]}
    report=summarize_pairs({"records":[a,b]},plan)
    assert report["w"]["metrics"]["query/first_ms"]["deltas"]==[1]
    with pytest.raises(AssertionError):summarize_pairs({"records":[a]},plan)
    b["semantics"]=["changed"]
    with pytest.raises(AssertionError):summarize_pairs({"records":[a,b]},plan)


def test_small_same_direction_confirmation_is_not_a_regression_allowance():
    frozen={"aa":{"w":{"metrics":{"query/first_ms":{"noise_radius":10}}}}}
    group={"metrics":{"query/first_ms":{"median_delta":1,"deltas":[1,1,2,1,3]}}}
    # Consistent positive evidence already stops the first candidate suite,
    # even when smaller than the A/A noise radius.
    assert classify_metrics("w",group,frozen)[0]
    assert classify_metrics("w",group,frozen,confirmation=True)[0]


def test_capacity_comparator_rejects_lost_completion_in_either_order():
    from scripts.benchmark_fsdb_optimization import capacity_outcome
    import pytest
    a=dict(arm="A",status="completed",semantics=["exact"],prelude_semantics=[])
    b=dict(a,arm="B",status="capacity_not_completed")
    assert capacity_outcome([a,b])==capacity_outcome([b,a])=="completion_regression"
    assert capacity_outcome([dict(a,status="capacity_not_completed"),dict(b,status="completed")])=="improved_completion"
    assert capacity_outcome([dict(a,status="capacity_not_completed"),b])=="both_limited"
    assert capacity_outcome([a,dict(b,status="completed")])=="preserved_completion"
    with pytest.raises(AssertionError):capacity_outcome([a,dict(b,status="completed",semantics=["wrong"])])


def test_aa_calibration_rejects_instability_without_using_max_as_allowance():
    from scripts.benchmark_fsdb_optimization import AA_RULE,calibrate_aa
    def groups(deltas):
        return {"w":dict(capacity_only=False,metrics={"count/first_ms":dict(
            a_values=[100]*5,b_values=[100+d for d in deltas],deltas=deltas,
            median_delta=sorted(deltas)[2],envelope=max(abs(d) for d in deltas))})}
    original=groups([-.1,.1,0,.1,4])
    report=calibrate_aa(original,AA_RULE)
    assert report["status"]=="stable"
    assert original["w"]["metrics"]["count/first_ms"]["noise_radius"]<.2
    candidate={"metrics":{"count/first_ms":dict(median_delta=.2,deltas=[.2,.2,-.1,.2,.2])}}
    assert classify_metrics("w",candidate,{"aa":original})[0]
    assert calibrate_aa(groups([-50,-20,0,20,50]),AA_RULE)["status"]=="unstable"
    assert calibrate_aa(groups([20]*5),AA_RULE)["status"]=="unstable"


def test_only_one_confirmation_is_reserved_with_exact_lineage(tmp_path):
    from scripts.benchmark_fsdb_optimization import reserve_confirmation,write
    import pytest
    suite=dict(plan_sha256="p",harness_sha256="h",source_identities=["a","b"],
        wrappers=["wa","wb"],probes=["pa","pb"],selected_workloads=["w"])
    prior=dict(suite,status="suspected_regression",suspicions=[dict(workload="w")])
    path=tmp_path/"summary.json";write(path,prior)
    with pytest.raises(AssertionError):reserve_confirmation(path,dict(suite,probes=["changed"]),tmp_path/"bad")
    assert not (tmp_path/"CONFIRMATION_RESERVED.json").exists()
    reserve_confirmation(path,suite,tmp_path/"confirm")
    with pytest.raises(FileExistsError):reserve_confirmation(path,suite,tmp_path/"try_again")


def test_wide_oracle_preserves_all_lanes_and_unknowns():
    oracle={"width":256,"unknown_records":{"5":"x","6":"z"}}
    assert expected_bits(oracle,5,"top.data[255:0]")=="x"*256
    assert expected_bits(oracle,6,"top.data[255:0]")=="z"*256
    expected=format(2654435761,"064b")*4
    assert expected_bits(oracle,1,"top.data[255:0]")==expected
    for width,steps in ((64,600000),(256,600000),(1024,200000)):
        for index in (1,262144,steps):
            actual=expected_bits(dict(oracle,width=width),index,f"top.data[{width-1}:0]")
            assert len(actual)==width
            assert actual==format(index*2654435761&0xffffffff,"064b")*(width//64)
        assert steps//min(262144,(64*1024**2+width+7)//(width+8))>=2


def test_guard_interruption_still_writes_receipt_and_kills_group(tmp_path,monkeypatch):
    import json
    import threading
    import pytest
    from scripts import benchmark_fsdb_optimization as bench
    terminated=threading.Event()
    class Child:
        pid=123456
        returncode=None
        def poll(self):return self.returncode
        def wait(self,timeout=None):
            assert terminated.wait(timeout)
            self.returncode=-15
            return self.returncode
    child=Child();signals=[];calls=0
    def sample(pid):
        nonlocal calls
        calls+=1
        if calls==1:raise KeyboardInterrupt()
        return {},[]
    monkeypatch.setattr(bench.subprocess,"Popen",lambda *a,**kw:child)
    monkeypatch.setattr(bench,"group_memory",sample)
    monkeypatch.setattr(bench,"system_memory",lambda:dict(available_kib=10**9))
    def kill(pid,sig):
        signals.append((pid,sig));terminated.set()
    monkeypatch.setattr(bench.os,"killpg",kill)
    with pytest.raises(KeyboardInterrupt):
        bench.guarded(["worker"],tmp_path/"guard",bench.ORDINARY)
    receipt=json.loads((tmp_path/"guard/guard.json").read_text())
    assert receipt["guard_reason"]=="parent_interrupted" and receipt["returncode"]==-15
    assert signals==[(child.pid,bench.signal.SIGTERM)]


def test_guard_observes_short_worker_exit_without_polling_quantization(tmp_path):
    import json
    import sys
    import time
    from scripts import benchmark_fsdb_optimization as bench
    for delay in (.02,.18):
        output=tmp_path/str(delay)
        program=("import json,os,time; time.sleep("+str(delay)+
            "); print(json.dumps({'finished':time.monotonic()}),flush=True); os._exit(0)")
        receipt=bench.guarded([sys.executable,"-c",program],output,bench.ORDINARY)
        returned=time.monotonic()
        finished=json.loads((output/"worker.log").read_text())["finished"]
        assert receipt["returncode"]==0 and receipt["guard_reason"] is None
        # The old 200 ms sleep adds ~170 ms after a 20 ms child has exited.
        assert 0<=returned-finished<.1
        assert receipt["guard_wall_ms"]>=receipt["process_wall_ms"]>=delay*1000


def test_guard_still_terminates_timed_out_process_group(tmp_path):
    import sys
    from scripts import benchmark_fsdb_optimization as bench
    receipt=bench.guarded([sys.executable,"-c","import time; time.sleep(10)"],
        tmp_path/"timeout",dict(bench.ORDINARY,timeout_sec=.05))
    assert receipt["guard_reason"]=="timeout" and receipt["returncode"]<0
    assert .05*1000<=receipt["process_wall_ms"]<5000
    assert receipt["guard_wall_ms"]>=receipt["process_wall_ms"]


def test_guard_monitor_start_failure_still_reaps_worker(tmp_path,monkeypatch):
    import json
    import sys
    import pytest
    from scripts import benchmark_fsdb_optimization as bench
    def fail_start(self):raise RuntimeError("monitor unavailable")
    monkeypatch.setattr(bench.threading.Thread,"start",fail_start)
    with pytest.raises(RuntimeError,match="monitor unavailable"):
        bench.guarded([sys.executable,"-c","import time; time.sleep(10)"],
            tmp_path/"failure",bench.ORDINARY)
    receipt=json.loads((tmp_path/"failure/guard.json").read_text())
    assert receipt["guard_reason"]=="parent_interrupted" and receipt["returncode"]<0


def test_suite_affinity_is_inherited_by_worker_and_restored_on_failure(tmp_path):
    import json
    import os
    import sys
    import pytest
    from scripts import benchmark_fsdb_optimization as bench
    original=set(os.sched_getaffinity(0))
    if len(original)<2:pytest.skip("requires two permitted logical CPUs")
    guard_cpu,worker_cpu=sorted(original)[:2]
    plan=dict(conditions=dict(cpu_affinity=dict(guard_cpu=guard_cpu,worker_cpu=worker_cpu)))
    with pytest.raises(RuntimeError,match="leave suite"):
        with bench.suite_affinity(plan):
            assert os.sched_getaffinity(0)=={guard_cpu}
            receipt=bench.guarded([sys.executable,"-c",
                "import os,json; print(json.dumps(sorted(os.sched_getaffinity(0))))"],
                tmp_path/"affinity",bench.ORDINARY,worker_affinity=[worker_cpu])
            assert receipt["returncode"]==0 and receipt["guard_reason"] is None
            assert receipt["worker_cpu_affinity"]==[worker_cpu]
            assert receipt["parent_cpu_affinity"]==[guard_cpu]
            assert json.loads((tmp_path/"affinity/worker.log").read_text())==[worker_cpu]
            raise RuntimeError("leave suite")
    assert os.sched_getaffinity(0)==original


def test_memory_diagnostic_keeps_raw_mapping_and_refuses_overwrite(tmp_path):
    import json
    import pytest
    from scripts import benchmark_fsdb_optimization as bench
    relative=bench.memory_diagnostic_snapshot(tmp_path,"after-import")
    raw=(tmp_path/relative).read_text()
    assert "Rss:" in raw and "AnonHugePages:" in raw and "VmFlags:" in raw
    receipt=json.loads((tmp_path/"memory-diagnostics/after-import.json").read_text())
    assert receipt["diagnostic_only"] and receipt["smaps_file"]=="after-import.smaps"
    assert receipt["before_memory_kib"]["VmRSS"]>0
    assert receipt["peak_after_kib"]>=receipt["peak_before_kib"]
    with pytest.raises(FileExistsError):
        bench.memory_diagnostic_snapshot(tmp_path,"after-import")
    assert (tmp_path/relative).read_text()==raw


def test_guard_disables_thp_in_exec_child_without_changing_parent(tmp_path):
    import os
    import sys
    import pytest
    from scripts import benchmark_fsdb_optimization as bench
    parent_thp=bench.proc_thp_enabled(os.getpid())
    if parent_thp is None:pytest.skip("requires Linux THP status reporting")
    command=[sys.executable,"-c",
        "from pathlib import Path; import time; "
        "print(next(line for line in Path('/proc/self/status').read_text().splitlines() "
        "if line.startswith('THP_enabled:')),flush=True); time.sleep(.05)"]
    receipt=bench.guarded(command,tmp_path/"thp-child",bench.ORDINARY,disable_worker_thp=True)
    assert receipt["returncode"]==0 and receipt["guard_reason"] is None
    assert receipt["worker_thp_enabled"]==0
    assert receipt["parent_thp_enabled"]==parent_thp==bench.proc_thp_enabled(os.getpid())
    assert (tmp_path/"thp-child/worker.log").read_text().strip().split()==["THP_enabled:","0"]


def test_single_dense_session_retains_terminal_zero_span_metadata():
    from scripts.benchmark_fsdb_optimization import validate_single_dense_session
    import pytest
    def row(a,b):return dict(start_tick=a,close_tick=b,start_ps=a,close_ps=b)
    metadata=dict(sessions=2,scale_fs=1000,duration_ps=2000,session_layout=[row(0,2000),row(2000,2000)])
    result=validate_single_dense_session(metadata,1001,1100)
    assert result["positive_span_sessions"]==1 and result["terminal_zero_span_entries"]==1
    initial=dict(metadata,session_layout=[row(0,0),row(0,2000)])
    assert validate_single_dense_session(initial,1001,1100)["initial_zero_span_entries"]==1
    for invalid in ([row(0,1000),row(1000,2000)], [row(0,2000),row(1100,1100)],
                    [row(0,2000),row(1999,1999)], [row(2000,2000),row(0,2000)],
                    [row(0,2000),row(0,0)]):
        with pytest.raises(AssertionError):
            validate_single_dense_session(dict(metadata,session_layout=invalid),1001,1100)
    with pytest.raises(AssertionError):validate_single_dense_session(metadata,1001,2000)
