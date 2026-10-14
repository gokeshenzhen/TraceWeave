#!/usr/bin/env python3
"""Serial, guarded control/candidate FSDB benchmarks with immutable evidence.

This program never installs a wrapper, modifies a source tree, or simulates a
real design. Run build -> generate -> plan -> smoke -> run -> freeze. Every
output directory is new. Performance work is protected by a machine-local
exclusive lock, including input generation and full input hashing.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import copy
import fcntl
import gc
import hashlib
import itertools
import json
import math
import os
from pathlib import Path
import platform
import resource
import signal
import statistics
import subprocess
import sys
import time

SCRIPT = Path(__file__).resolve()
ROOT = SCRIPT.parents[1]
UINT64_MAX = 2**64 - 1
OFFSET_PS = 10**12
# Exact JSON paths, never a recursive key-name filter. Cursor objects (including
# name, time, note, signal association and metadata) are compared in full.
NORMALIZED_RESPONSE_PATHS = (("elapsed_ms",),)
METRICS = ("first_ms", "repeat_median_ms", "maximum_ms", "process_wall_ms",
           "peak_rss_kib", "after_close_rss_kib")
ORDINARY = dict(timeout_sec=900, rss_mib=4096, as_mib=8192, available_min_mib=2048)
LONG = dict(timeout_sec=1800, rss_mib=4096, as_mib=8192, available_min_mib=2048)
CAPACITY = dict(timeout_sec=900, rss_mib=2048, as_mib=4096, available_min_mib=2048)


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024**2), b""):
            digest.update(block)
    return digest.hexdigest()


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False)
        stream.write("\n")


def read(path):
    return json.loads(Path(path).read_text())


def file_identity(path):
    path = Path(path)
    info = path.stat()
    return dict(path=str(path), realpath=str(path.resolve()), size_bytes=info.st_size,
                mtime_ns=info.st_mtime_ns, sha256=sha(path))


def source_identity(root):
    root = Path(root).resolve()
    names = subprocess.check_output(["git", "-C", str(root), "ls-files", "server.py",
        "config.py", "src", "native/fsdb", "scripts/build_wrapper.sh"], text=True).splitlines()
    files = {name: sha(root / name) for name in names if (root / name).is_file()}
    return dict(root=str(root), head=subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip(),
        tracked_status=subprocess.check_output(["git", "-C", str(root), "status", "--porcelain",
        "--untracked-files=no"], text=True), files=files,
        content_sha256=hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest())


@contextmanager
def serial_lock():
    with open("/tmp/traceweave-fsdb-optimization-benchmark.lock", "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another FSDB performance/generation/hash job owns the serial lock") from exc
        yield


def proc_memory(pid):
    try:
        return {line.split()[0][:-1]: int(line.split()[1])
            for line in Path(f"/proc/{pid}/status").read_text().splitlines()
            if line.startswith(("VmRSS:", "VmHWM:", "VmSwap:", "VmSize:"))}
    except (FileNotFoundError, ProcessLookupError):
        return {}


def system_memory():
    fields = {line.split()[0][:-1]: int(line.split()[1])
        for line in Path("/proc/meminfo").read_text().splitlines()}
    return dict(available_kib=fields["MemAvailable"], swap_used_kib=fields["SwapTotal"]-fields["SwapFree"],
                load_average=list(os.getloadavg()))


def group_memory(pgid):
    # The actual worker is launched as the group leader. Also count subprocesses
    # so an accidental helper cannot escape the RSS guard or cleanup boundary.
    sums = dict(VmRSS=0, VmHWM=0, VmSwap=0, VmSize=0)
    members = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdecimal():
            continue
        try:
            pid = int(proc.name)
            if os.getpgid(pid) != pgid:
                continue
        except (ProcessLookupError, PermissionError):
            continue
        members.append(pid)
        for key, value in proc_memory(pid).items():
            sums[key] += value
    return sums, members


def guarded(command, output, limits, *, env=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    before = system_memory()
    if before["available_kib"] < limits["available_min_mib"] * 1024:
        raise RuntimeError("host memory reserve unavailable before launch")
    def set_limits():
        resource.setrlimit(resource.RLIMIT_AS, (limits["as_mib"] * 1024**2,) * 2)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    start = time.monotonic()
    peak = dict(VmRSS=0, VmHWM=0, VmSwap=0, VmSize=0)
    reason = None
    samples = []
    with (output / "worker.log").open("x") as stream:
        child = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT,
            start_new_session=True, preexec_fn=set_limits, env=env)
        try:
            while child.poll() is None:
                memory, members = group_memory(child.pid)
                for key, value in memory.items():
                    peak[key] = max(peak[key], value)
                elapsed = time.monotonic() - start
                host = system_memory()
                if not samples or elapsed - samples[-1]["seconds"] >= 5:
                    samples.append(dict(seconds=elapsed, memory_kib=memory, host=host, process_count=len(members)))
                if memory["VmRSS"] > limits["rss_mib"] * 1024:
                    reason = "rss_limit"
                elif elapsed > limits["timeout_sec"]:
                    reason = "timeout"
                elif host["available_kib"] < limits["available_min_mib"] * 1024:
                    reason = "host_memory_reserve"
                if reason:
                    break
                time.sleep(.2)
        except BaseException:
            reason = "parent_interrupted"
            raise
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=5)
            # Kill any surviving group member even if the leader exited first.
            _, leftovers = group_memory(child.pid)
            if leftovers:
                os.killpg(child.pid, signal.SIGKILL)
                reason = reason or "orphan_processes"
    result = dict(command=command, returncode=child.returncode, guard_reason=reason,
        process_wall_ms=(time.monotonic()-start)*1000, observed_peak_kib=peak,
        limits=limits, host_before=before, host_after=system_memory(), resource_samples=samples)
    write(output / "guard.json", result)
    return result


def canonical(result):
    value = copy.deepcopy(result)
    for path in NORMALIZED_RESPONSE_PATHS:
        node = value
        for part in path[:-1]:
            node = node.get(part, {}) if isinstance(node, dict) else {}
        if isinstance(node, dict):
            node.pop(path[-1], None)
    return value


def digest_json(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def real_workloads(case):
    """Keep the original 13 workloads / 47 responses per real case."""
    wave, sig = case["wave"]["realpath"], case["signals"]
    start, end = case["window_ps"]
    center = (start + end) // 2
    def step(label, tool, **args):
        return dict(label=label, tool=tool, arguments=dict(wave_path=wave, **args))
    def trans(label, role, a=start, b=end, cap=1000):
        return step(label, "get_signal_transitions", signal_path=sig[role],
                    start_time_ps=a, end_time_ps=b, max_transitions=cap)
    bundle = [sig[k] for k in ("clock", "data", "data2", "sparse")]
    cycle = dict(clock_path=sig["clock"], signal_paths=bundle[1:], start_time_ps=start,
        end_time_ps=end, num_cycles=20, edge="posedge", sample_phase="after", sample_offset_ps=1)
    return {
        "point": dict(steps=[step("point", "get_signal_at_time", signal_path=sig["data"], time_ps=center)]),
        "point_scattered": dict(steps=[step(label,"get_signal_at_time",signal_path=sig["data"],time_ps=t)
            for label,t in zip(("late","early","middle"),case["scattered_times_ps"])]),
        "clock_window": dict(steps=[trans("query","clock")]),
        "data_window": dict(steps=[trans("query","data")]),
        "sparse_fresh": dict(steps=[trans("query","sparse")]),
        "sparse_after_dense": dict(prelude=[trans("load_clock","clock",cap=1),trans("load_data","data",cap=1)],steps=[trans("query","sparse")]),
        "clock_full_cap1000": dict(steps=[trans("query","clock",0,-1)]),
        "data_full_cap1000": dict(steps=[trans("query","data",0,-1)]),
        "around_full": dict(steps=[step("query","get_signals_around_time",signal_paths=bundle,
            center_time_ps=center,window_ps=(end-start)//2,extra_transitions=5,return_mode="full")]),
        "around_values_only": dict(steps=[step("query","get_signals_around_time",signal_paths=bundle,
            center_time_ps=center,window_ps=0,extra_transitions=0,return_mode="values_only")]),
        "cycles_global": dict(steps=[step("query","get_signals_by_cycle",**cycle,cycle_index_origin="global")]),
        "cycles_window": dict(steps=[step("query","get_signals_by_cycle",**cycle,cycle_index_origin="window")]),
        "cycles_window_before": dict(steps=[step("query","get_signals_by_cycle",
            **{**cycle,"sample_phase":"before","sample_offset_ps":0},cycle_index_origin="window")]),
    }


def protocol_oracle(cycles, start=0, end=-1, scope=None):
    """Independent sampled-stimulus model: count predicates and contiguous runs.

    No TraceWeave discovery, sampler or protocol implementation is imported.
    Stalls deliberately cross cycle 4096 and writer flush boundaries; the full
    trace ends in a stall. Unknown valid and AHB address episodes are separate.
    """
    expected = {}
    specs = [(f"top.fast.vr{i}",i,False,20,10,cycles) for i in range(6)]
    specs += [(f"top.slow.vr{i}",i,False,40,20,cycles//2) for i in range(6,8)]
    specs += [(f"top.fast.m_if{i}",i,True,20,10,cycles) for i in range(2)]
    for name,index,ahb,period,phase,count in specs:
        if scope and not (name == scope or name.startswith(scope+".")):
            continue
        beats=[]
        for cycle in range(count):
            timestamp=period*cycle+phase
            if timestamp < start or (end>=0 and timestamp>end):
                continue
            p=(cycle+32)%64
            valid=True if ahb else not((index%3==2 and p==40) or (index==0 and p<4))
            if not ahb and index==4 and p in (4,5):
                valid=None
            ready=p<24 or p>=48
            payload=((cycle+32)//64)*(8 if ahb else 2)
            payload += (4 if ahb else 1) if ((index==1 if ahb else index%3==1) and p>=32) else 0
            if ahb and index==1 and p==8:
                payload=None
            beats.append((timestamp,valid,ready,payload))
        runs=[]
        for stalled,group in itertools.groupby(beats,lambda row:row[1] is True and not row[2]):
            segment=list(group)
            if stalled:
                runs.append(segment)
        longest=max(runs,key=len) if runs else []
        payload_changes=sum(a[3] is not None and b[3] is not None and a[3]!=b[3]
            for run in runs for a,b in zip(run,run[1:]))
        unknown_episodes=sum(row[3] is None and (j==0 or beats[j-1][3] is not None)
            for j,row in enumerate(beats)) if ahb else 0
        expected[name]=dict(sample_count=len(beats),
            transfer_count=sum(v is True and r for _,v,r,_ in beats),
            stall_count=sum(v is True and not r for _,v,r,_ in beats),
            max_stall_cycles=len(longest),max_stall_begin_ps=longest[0][0] if longest else None,
            ended_in_stall=bool(beats and beats[-1][1] is True and not beats[-1][2]),
            final_stall_cycles=len(runs[-1]) if runs and runs[-1][-1]==beats[-1] else 0,
            payload_hold_violations=payload_changes,
            valid_deassert_violations=sum(a[1] is True and not a[2] and b[1] is False
                for a,b in zip(beats,beats[1:])),
            x_while_valid_violations=unknown_episodes,write_data_hold_violations=0,
            ready_without_valid_cycles=sum(v is False and r for _,v,r,_ in beats),
            unknown_sample_cycles=sum(v is None for _,v,_,_ in beats),
            transition_data_truncated=False)
    return expected


def validate_result(item, step, result):
    assert not result.get("error") and not result.get("error_code") and result.get("ok") is not False, result
    tool, args = step["tool"], step["arguments"]
    oracle=item.get("oracle",{})
    if tool=="get_signal_transitions":
        events=result["transitions"]
        assert len(events)<=args["max_transitions"]
        assert [r["time_ps"] for r in events]==sorted(r["time_ps"] for r in events)
        assert all(r["time_ps"]>=args["start_time_ps"] and (args["end_time_ps"]<0 or r["time_ps"]<=args["end_time_ps"]) for r in events)
        pred=result.get("predecessor")
        assert pred is None or pred["time_ps"]<args["start_time_ps"]
        assert not result.get("transition_count_is_lower_bound",False)
        if oracle.get("kind")=="counter":
            n,offset,stride=oracle["steps"],oracle["offset_ps"],oracle["stride_ps"]
            begin,end=args["start_time_ps"],args["end_time_ps"]
            lower=max(1,math.ceil((begin-offset)/stride))
            upper=min(n,(end-offset)//stride) if end>=0 else n
            count=max(0,upper-lower+1)+int(begin==0)
            assert result["transition_count"]==count,(item["id"],result["transition_count"],count)
            assert bool(result["truncated"])==(count>args["max_transitions"])
            for event in events + ([pred] if pred else []):
                t=event["time_ps"]
                i=0 if t==0 else (t-offset)//stride
                assert t==0 or (1<=i<=n and t==offset+i*stride)
                word=(i&1) if "clk" in args["signal_path"] else ((i*2654435761)&0xffffffff)
                assert int(event["value"]["bin"],2)==word
            if begin>0:
                pi=min(n,max(0,(begin-1-offset)//stride))
                assert pred is not None and pred["time_ps"]==(offset+pi*stride if pi else 0)
    elif tool=="get_signal_at_time" and oracle.get("kind")=="counter":
        i=min(oracle["steps"],max(0,(args["time_ps"]-oracle["offset_ps"])//oracle["stride_ps"]))
        word=(i&1) if "clk" in args["signal_path"] else ((i*2654435761)&0xffffffff)
        assert int(result["value"]["bin"],2)==word
    elif tool=="get_signals_by_cycle":
        assert result["num_cycles_returned"]==20 and not result["signal_errors"]
        assert not result.get("transition_data_truncated",False)
    elif tool=="sweep_handshakes" and oracle.get("kind")=="protocol":
        expected=protocol_oracle(oracle["cycles"],args.get("start_time_ps",0),args.get("end_time_ps",-1),args.get("scope"))
        assert result["coverage_status"]=="complete",result
        assert result["interface_count"]==len(expected)>0
        assert result["discovered_count"]==len(expected) and not result["skipped"]
        actual={row["scope"]:row for row in result["interfaces"]}
        assert set(actual)==set(expected),(actual.keys(),expected.keys())
        for name,facts in expected.items():
            row=actual[name]
            for key,value in facts.items():
                assert row[key]==value,(item["id"],name,key,row[key],value)
            coverage=row["coverage"]
            assert coverage["clock_sampled"] and coverage["stall_checked"]
            assert coverage["payload_hold_checked"] and coverage["valid_hold_checked"]
            if ".m_if" in name:
                assert coverage["x_while_valid_checked"] and coverage["write_data_hold_checked"]
        if result.get("cursor"):
            top=result["interfaces"][0]
            assert result["cursor"]["time_ps"]==top["max_stall_begin_ps"]
            assert result["cursor"]["metadata"]["valid"]==top["valid"]
    return True


def loaded_libraries():
    paths=sorted({line.split()[-1] for line in Path("/proc/self/maps").read_text().splitlines()
        if any(name in line for name in ("libfsdb_wrapper", "libnffr", "libnsys", "libz.so"))})
    return [dict(path=p,sha256=sha(p)) for p in paths]


async def worker(args):
    source=Path(args.source_root).resolve()
    sys.path.insert(0,str(source))
    os.environ["TRACEWEAVE_TELEMETRY"]="0"
    os.environ["TRACEWEAVE_FSDB_GROUP_MAX_SIGNALS"]="16"
    os.environ["TRACEWEAVE_CACHE_DIR"]=str(args.output / "runtime_cache")
    from src import fsdb_parser
    fsdb_parser._WRAPPER_SO=str(args.wrapper.resolve())
    import server
    from src.clock_edge_cache import cache
    item=next(row for row in read(args.plan)["workloads"] if row["id"]==args.workload)
    wave=item["wave"]
    initial=proc_memory(os.getpid())
    captured={}
    original=server.usage_telemetry.record_call
    def capture(*a,**kw):
        captured.clear();captured.update(kw.get("diagnostics") or {})
        return original(*a,**kw)
    server.usage_telemetry.record_call=capture
    def prepare():
        started=time.perf_counter_ns(); parser=server._get_parser(wave);parser._open()
        open_ms=(time.perf_counter_ns()-started)/1e6
        opened=proc_memory(os.getpid())
        started=time.perf_counter_ns();parser._get_buf()
        return parser,open_ms,opened,(time.perf_counter_ns()-started)/1e6
    parser,open_ms,opened,buffer_ms=await server._run_in_wave_thread(wave,prepare)
    libraries=loaded_libraries()
    rows=[];prelude=[]
    before=Path(wave).stat()
    async def invoke(step,repeat,is_prelude=False):
        gc.collect();mem_before=proc_memory(os.getpid());started=time.perf_counter_ns()
        blocks=await server.call_tool(step["tool"],step["arguments"])
        elapsed=(time.perf_counter_ns()-started)/1e6
        memory=proc_memory(os.getpid())
        encoded="".join(block.text for block in blocks if block.type=="text")
        result=json.loads(encoded)
        validate_result(item,step,result)
        name=f"responses/{'prelude' if is_prelude else repeat}-{step['label']}.json"
        target=args.output/name;target.parent.mkdir(exist_ok=True)
        with target.open("x") as stream:stream.write(encoded)
        return dict(step=step["label"],repeat=repeat,elapsed_ms=elapsed,
            rss_before_kib=mem_before.get("VmRSS",0),rss_after_kib=memory.get("VmRSS",0),
            peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            response=name,response_sha256=sha(target),semantic_sha256=digest_json(canonical(result)),
            diagnostics=dict(captured),clock_cache=cache.snapshot())
    try:
        for step in item["scenario"].get("prelude",[]):
            prelude.append(await invoke(step,-1,True))
        for repeat in range(item["calls_per_worker"]):
            for step in item["scenario"]["steps"]:
                rows.append(await invoke(step,repeat))
    finally:
        started=time.perf_counter_ns()
        await server._run_in_wave_thread(wave,parser.close)
        close_ms=(time.perf_counter_ns()-started)/1e6
    after=Path(wave).stat()
    assert (before.st_ino,before.st_size,before.st_mtime_ns)==(after.st_ino,after.st_size,after.st_mtime_ns)
    write(args.output/"result.json",dict(id=item["id"],scenario=item["scenario"],rows=rows,prelude=prelude,
        open_ms=open_ms,buffer_ms=buffer_ms,close_ms=close_ms,initial_memory_kib=initial,
        after_open_memory_kib=opened,after_close_memory_kib=proc_memory(os.getpid()),
        peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        wrapper=file_identity(args.wrapper),loaded_libraries=libraries,source_root=str(source)))


def build(args):
    out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    source=args.source_root.resolve();sdk=args.batch.resolve()/"control/sdk"
    commands=[]
    generator=out/"fixture"
    commands.append(["g++","-O2","-std=c++11","-I"+str(sdk/"FsdbWriter"),
        str(SCRIPT.with_name("fsdb_optimization_fixture.cpp")),"-L"+str(sdk/"FsdbWriter/linux64"),
        "-lnffw","-ldl","-Wl,-rpath,"+str(sdk/"FsdbWriter/linux64"),"-o",str(generator)])
    text=(source/"native/fsdb/fsdb_wrapper.cpp").read_text()
    marker="    if (!FsdbReclaimEventCursors(ctx->obj, sig->idcode, siblings, ctx->cleanup_failed))\n        return -6;"
    assert text.count(marker)==1,"reclaim profiling insertion point changed; review instrumentation"
    text="static unsigned long long bench_reclaims=0, bench_reclaim_ns=0;\n"+text.replace(marker,
        "    auto bench_start = _ProfileClock::now();\n    ++bench_reclaims;\n"
        "    bool bench_ok = FsdbReclaimEventCursors(ctx->obj, sig->idcode, siblings, ctx->cleanup_failed);\n"
        "    bench_reclaim_ns += _ElapsedNs(bench_start, _ProfileClock::now());\n    if (!bench_ok) return -6;")
    wrapper=out/"instrumented_wrapper.cpp";wrapper.write_text(text)
    commands.append(["g++","-O0","-std=c++11","-I"+str(sdk/"FsdbReader"),
        "-I"+str(source/"native/fsdb"),'-DTRACEWEAVE_BENCH_WRAPPER="'+str(wrapper)+'"',
        str(SCRIPT.with_name("fsdb_optimization_probe.cpp")),"-L"+str(sdk/"FsdbReader/linux64"),
        "-lnffr","-lnsys","/usr/lib64/libz.so.1","-ldl","-Wl,-rpath,"+str(sdk/"FsdbReader/linux64"),"-o",str(out/"probe")])
    for command in commands:
        subprocess.run(command,check=True)
    write(out/"BUILD.json",dict(source=source_identity(source),commands=commands,
        artifacts={str(p):sha(p) for p in (generator,out/"probe",wrapper)},
        generator_source=sha(SCRIPT.with_name("fsdb_optimization_fixture.cpp")),
        probe_source=sha(SCRIPT.with_name("fsdb_optimization_probe.cpp"))))


def generate(args):
    directory=args.output.resolve();directory.mkdir(parents=True,exist_ok=False)
    records=[]
    for name,mode,steps,sync,memory in (("dense_10m","dense",10_000_000,0,128),
        ("dense_100m","dense",100_000_000,0,128),("single_session_10m","dense",10_000_000,0,1024),
        ("protocol_small","protocol",256,32,128),("protocol_large","protocol",131072,4096,128)):
        path=directory/(name+".fsdb")
        receipt=guarded([str(args.build/"fixture"),str(path),mode,str(steps),str(sync),str(memory)],
            directory/(name+"-generation"),dict(timeout_sec=900,rss_mib=4096,as_mib=8192,available_min_mib=2048))
        assert receipt["returncode"]==0 and not receipt["guard_reason"],receipt
        records.append(dict(id=name,mode=mode,steps=steps,sync_every=sync,writer_memory_mib=memory,wave=file_identity(path)))
    write(directory/"FIXTURES.json",dict(fixtures=records,generator=file_identity(args.build/"fixture")))


def make_plan(args):
    workloads=[];inputs=[]
    def add(identifier,wave,scenario=None,*,oracle=None,pairs=5,capacity=False,native=None):
        workloads.append(dict(id=identifier,wave=wave,kind="native" if native else "public",
            scenario=scenario,native=native,oracle=oracle or {},pairs=1 if capacity else pairs,
            calls_per_worker=1 if capacity else 3,capacity_only=capacity,required=True,
            limits=CAPACITY if capacity else (LONG if pairs==3 else ORDINARY)))
    real=read(args.real_manifest)
    for case in real["cases"]:
        inputs.append(case["wave"])
        for name,scenario in real_workloads(case).items():
            add(case["id"]+"__"+name,case["wave"]["realpath"],scenario)
        add(case["id"]+"__sweep_window",case["wave"]["realpath"],dict(steps=[dict(label="sweep",tool="sweep_handshakes",
            arguments=dict(wave_path=case["wave"]["realpath"],start_time_ps=case["window_ps"][0],
                end_time_ps=case["window_ps"][1],max_interfaces=64,max_wait_cycles=16))]))
    assets=read(args.batch/"control/IDENTITY.json")
    existing=args.batch.parents[1]/"fsdb-billion-20261013"
    data=[("activity_10m",existing/"activity_10000000.fsdb",10_000_000,0,10,5),
          ("activity_10m_small_sessions",existing/"activity_10m_small_sessions.fsdb",10_000_000,0,10,5),
          ("activity_100m",existing/"activity_100000000.fsdb",100_000_000,0,10,3)]
    fixtures=read(args.fixtures/"FIXTURES.json")
    for fixture in fixtures["fixtures"]:
        inputs.append(fixture["wave"])
        if fixture["mode"]=="dense":
            if fixture["id"]=="single_session_10m":
                add("single_session_10m__capacity",fixture["wave"]["realpath"],native=dict(
                    signal="top.data[31:0]",start_ps=OFFSET_PS+1,end_ps=OFFSET_PS+fixture["steps"],
                    expected_records=fixture["steps"]+1),capacity=True)
            else:
                data.append((fixture["id"],Path(fixture["wave"]["realpath"]),fixture["steps"],OFFSET_PS,1,3 if fixture["steps"]==100_000_000 else 5))
        else:
            wave=fixture["wave"]["realpath"]
            for suffix,scope,start,end in (("full",None,0,-1),("window",None,20*64,20*192),
                ("single", "top.fast.vr0",0,-1),("ahb","top.fast.m_if1",0,-1)):
                arguments=dict(wave_path=wave,max_interfaces=64,max_wait_cycles=8)
                if start or end!=-1:
                    arguments.update(start_time_ps=start,end_time_ps=end)
                if scope:arguments["scope"]=scope
                add(fixture["id"]+"__sweep_"+suffix,wave,dict(steps=[dict(label="sweep",tool="sweep_handshakes",arguments=arguments)]),
                    oracle=dict(kind="protocol",cycles=fixture["steps"]))
    for name,path,n,offset,stride,pairs in data:
        wave=str(path.resolve())
        if not any(row["realpath"]==wave for row in inputs):inputs.append(file_identity(path))
        oracle=dict(kind="counter",steps=n,offset_ps=offset,stride_ps=stride)
        start=offset+1 if offset else 0;end=offset+n*stride if offset else -1
        add(name+"__native_count",wave,native=dict(signal="top.data[31:0]",start_ps=start,end_ps=end,expected_records=n+1),oracle=oracle,pairs=pairs)
        for cap in (1,1000):
            add(name+f"__cap{cap}",wave,dict(steps=[dict(label="transitions",tool="get_signal_transitions",arguments=dict(
                wave_path=wave,signal_path="top.data[31:0]",start_time_ps=start,end_time_ps=end,max_transitions=cap))]),oracle=oracle,pairs=pairs)
    giant=existing/"activity_1700000000.fsdb";wave=str(giant.resolve());inputs.append(file_identity(giant))
    oracle=dict(kind="counter",steps=1_700_000_000,offset_ps=0,stride_ps=10)
    for role,path in (("data","top.data[31:0]"),("clock","top.clk")):
        for fraction in (1,5,9):
            when=1_700_000_000*fraction+3
            add(f"giant__{role}_point_{fraction}",wave,dict(steps=[dict(label="point",tool="get_signal_at_time",
                arguments=dict(wave_path=wave,signal_path=path,time_ps=when))]),oracle=oracle)
        add(f"giant__{role}_window",wave,dict(steps=[dict(label="window",tool="get_signal_transitions",
            arguments=dict(wave_path=wave,signal_path=path,start_time_ps=15_300_000_003,end_time_ps=15_300_000_203,max_transitions=1000))]),oracle=oracle)
        add(f"giant__{role}_native_capacity",wave,native=dict(signal=path,start_ps=0,end_ps=-1,expected_records=1_700_000_001),oracle=oracle,capacity=True)
    plan=dict(schema_version=1,created_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        control_identity_sha256=sha(args.batch/"control/IDENTITY.json"),control_head=assets["head"],
        inputs=inputs,workloads=workloads,normalization_paths=NORMALIZED_RESPONSE_PATHS,
        metrics=METRICS,group_cap=16,performance_order="per-workload pairs; AB then BA alternating; fresh process per arm; 3 calls per worker",
        aa_rule=dict(method="paired absolute differences; envelope=max(abs(diff)); median and MAD reported separately",
            timing_floor_ms=.001,rss_floor_kib=4,confirmation_rounds=1,
            acceptance="A/A envelope separates suspicion from noise, never authorizes regression. A confirmation with all paired differences positive and positive median is a confirmed regression even below the envelope. Unresolved candidates are not accepted."),
        conditions=dict(cache="no OS cache flush; hash all inputs before each suite equally",telemetry=False,
            timing="server.call_tool includes worker/lock/schema/JSON; excludes import/open/buffer, oracle and file I/O; whole process separately",
            native_timing="seek + exact scan + in-scan reclamation + group cleanup; final close and process wall separately",
            environment="same CPU/SDK/compiler/default -O0; generation/hash/benchmark serialized"),
        tests_required=["tests/test_fsdb_cursor_reclaim.py","tests/test_fsdb_event_cursor_native.py","tests/test_event_pages.py",
            "tests/test_fsdb_capped_transitions.py","tests/test_fsdb_point_window.py","tests/test_fsdb_timescale.py",
            "tests/test_fsdb_widebus.py","tests/test_fsdb_runtime.py","tests/test_cycle_query.py","tests/test_fsdb_compact_clock.py",
            "tests/test_clock_edge_cache.py","tests/test_handshake_sweep.py","tests/test_server_concurrency.py"])
    write(args.output,plan)


def metrics(record, guard):
    rows=record["rows"]
    labels=list(dict.fromkeys(row["step"] for row in rows))
    result={}
    for label in labels:
        chosen=[row for row in rows if row["step"]==label]
        result[label]=dict(first_ms=chosen[0]["elapsed_ms"],
            repeat_median_ms=statistics.median(row["elapsed_ms"] for row in chosen[1:]) if len(chosen)>1 else None,
            maximum_ms=max(row["elapsed_ms"] for row in chosen),process_wall_ms=guard["process_wall_ms"],
            peak_rss_kib=max(record["peak_rss_kib"],guard["observed_peak_kib"]["VmHWM"]),
            after_close_rss_kib=record["after_close_memory_kib"].get("VmRSS",0))
    return result


def native_record(directory, item):
    rows=[]
    for line in (directory/"worker.log").read_text().splitlines():
        if line.startswith("{"):
            try:rows.append(json.loads(line))
            except json.JSONDecodeError:pass
    counts=[row for row in rows if row.get("phase")=="count"]
    assert len(counts)==item["calls_per_worker"]
    # Native /proc end-residency is sampled by the probe before exit (added to
    # the final receipt), distinct from HWM. Do not substitute peak for ending.
    close=next(row for row in rows if row.get("phase")=="close")
    record=dict(id=item["id"],rows=[dict(row,step="count",semantic_sha256=digest_json(dict(records=row["records"]))) for row in counts],
        phases=rows,peak_rss_kib=max(row["peak_rss_kib"] for row in counts),
        loaded_libraries=[dict(path=row["path"],sha256=sha(row["path"])) for row in rows if row.get("phase")=="library"],
        after_close_memory_kib=dict(VmRSS=close["rss_kib"]))
    write(directory/"result.json",record)
    return record


def run_arm(args,item,arm,round_index,root,wrapper,probe):
    directory=args.output/item["id"]/f"r{round_index:02d}-{arm}"
    if item["kind"]=="public":
        command=[sys.executable,str(SCRIPT),"worker","--plan",str(args.plan.resolve()),"--workload",item["id"],
            "--source-root",str(root),"--wrapper",str(wrapper),"--output",str(directory)]
    else:
        req=item["native"]
        command=[str(probe),item["wave"],req["signal"],str(req["start_ps"]),
            str(UINT64_MAX if req["end_ps"]<0 else req["end_ps"]),str(req["expected_records"]),str(item["calls_per_worker"]),"count"]
    guard=guarded(command,directory,item["limits"])
    result=dict(id=item["id"],arm=arm,round=round_index,directory=str(directory),guard=guard)
    if guard["returncode"] or guard["guard_reason"]:
        assert item["capacity_only"],f"required workload failed: {directory}"
        result["status"]="capacity_not_completed"
    else:
        record=read(directory/"result.json") if item["kind"]=="public" else native_record(directory,item)
        result.update(status="completed",metrics=metrics(record,guard),
            semantics=[row["semantic_sha256"] for row in record["rows"]],
            prelude_semantics=[row["semantic_sha256"] for row in record.get("prelude",[])])
    write(directory/"receipt.json",result)
    return result


def run_suite(args):
    args.output=args.output.resolve();args.output.mkdir(parents=True,exist_ok=False)
    plan=read(args.plan);selected=[row for row in plan["workloads"] if not args.workload or row["id"] in args.workload]
    assert selected and (not args.workload or len(selected)==len(set(args.workload))),"unknown workload"
    roots=[args.control_root.resolve(),(args.candidate_root or args.control_root).resolve()]
    wrappers=[args.control_wrapper.resolve(),(args.candidate_wrapper or args.control_wrapper).resolve()]
    probes=[args.control_probe.resolve(),(args.candidate_probe or args.control_probe).resolve()]
    identities=[source_identity(root) for root in roots]
    for input_ in plan["inputs"]:
        if input_["realpath"] in {row["wave"] for row in selected}:
            assert Path(input_["realpath"]).stat().st_size==input_["size_bytes"] and sha(input_["realpath"])==input_["sha256"]
    suite=dict(plan_sha256=sha(args.plan),harness_sha256=sha(SCRIPT),mode=args.command,
        source_identities=identities,wrappers=[file_identity(p) for p in wrappers],probes=[file_identity(p) for p in probes],
        python=sys.version,platform=platform.platform(),host=system_memory(),records=[])
    write(args.output/"RUN_START.json",{k:v for k,v in suite.items() if k!="records"})
    for item in selected:
        rounds=1 if args.command=="smoke" else item["pairs"]
        for round_index in range(rounds):
            for index in ((0,1) if round_index%2==0 else (1,0)):
                record=run_arm(args,item,"AB"[index],round_index,roots[index],wrappers[index],probes[index])
                suite["records"].append(record)
                print(json.dumps({"id":item["id"],"arm":"AB"[index],"round":round_index,"status":record["status"]}),flush=True)
            a,b=suite["records"][-2:]
            if a["status"]==b["status"]=="completed":
                assert a["semantics"]==b["semantics"] and a["prelude_semantics"]==b["prelude_semantics"],f"semantic mismatch: {item['id']}"
    assert [source_identity(root) for root in roots]==identities,"source changed during run"
    write(args.output/"summary.json",suite)


def summarize_pairs(summary,plan):
    groups={}
    for item in plan["workloads"]:
        rows=[row for row in summary["records"] if row["id"]==item["id"]]
        assert len(rows)==item["pairs"]*2,(item["id"],len(rows))
        if item["capacity_only"]:
            groups[item["id"]]=dict(capacity_only=True,receipts=rows)
            continue
        assert all(row["status"]=="completed" for row in rows)
        pairs=[(next(r for r in rows if r["round"]==i and r["arm"]=="A"),
                next(r for r in rows if r["round"]==i and r["arm"]=="B")) for i in range(item["pairs"])]
        assert all(a["semantics"]==b["semantics"] and a["prelude_semantics"]==b["prelude_semantics"] for a,b in pairs)
        reports={}
        for label in pairs[0][0]["metrics"]:
            for metric in METRICS:
                deltas=[b["metrics"][label][metric]-a["metrics"][label][metric] for a,b in pairs]
                center=statistics.median(deltas)
                reports[label+"/"+metric]=dict(deltas=deltas,median_delta=center,
                    mad=statistics.median(abs(d-center) for d in deltas),envelope=max(abs(d) for d in deltas),
                    a_values=[a["metrics"][label][metric] for a,b in pairs],b_values=[b["metrics"][label][metric] for a,b in pairs])
        groups[item["id"]]=dict(capacity_only=False,metrics=reports)
    return groups


def freeze(args):
    plan=read(args.plan);summary=read(args.summary)
    assert summary["plan_sha256"]==sha(args.plan) and summary["harness_sha256"]==sha(SCRIPT)
    assert summary["source_identities"][0]["content_sha256"]==summary["source_identities"][1]["content_sha256"]
    assert summary["wrappers"][0]["sha256"]==summary["wrappers"][1]["sha256"]
    aa=summarize_pairs(summary,plan)
    # Presence of independent validation and MCP observations is an explicit
    # gate, not a boolean silently manufactured from A/A response equality.
    validation=read(args.validation)
    assert validation["status"]=="passed" and validation["mcp_fixture_observations"]
    assert validation["required_tests_complete"] and validation["metadata_complete"]
    write(args.output,dict(schema_version=1,status="frozen",plan=plan,plan_sha256=sha(args.plan),
        harness_sha256=sha(SCRIPT),aa_summary_sha256=sha(args.summary),aa=aa,
        validation_receipt=validation,validation_sha256=sha(args.validation),
        control=summary["source_identities"][0],wrapper=summary["wrappers"][0],probe=summary["probes"][0]))


def compare(args):
    freeze_=read(args.freeze);summary=read(args.summary);plan=freeze_["plan"]
    assert summary["plan_sha256"]==freeze_["plan_sha256"]
    groups=summarize_pairs(summary,plan);suspicions=[];improvements=[]
    for workload,group in groups.items():
        if group["capacity_only"]:continue
        for key,values in group["metrics"].items():
            before=freeze_["aa"][workload]["metrics"][key]
            floor=4 if key.endswith("kib") else .001
            envelope=max(before["envelope"],floor)
            entry=dict(workload=workload,metric=key,envelope=envelope,**values)
            if values["median_delta"]>envelope or (args.confirmation and all(d>0 for d in values["deltas"])):
                suspicions.append(entry)
            if values["median_delta"] < -envelope:
                improvements.append(entry)
    report=dict(status="regression" if args.confirmation and suspicions else "suspicious" if suspicions else "no_detected_regression",
        confirmation=bool(args.confirmation),suspicions=suspicions,improvements=improvements,groups=groups,
        note="No detected regression is not acceptance: focused correctness, original-control and previous-accepted comparisons are still required.")
    write(args.output,report)
    print(json.dumps(dict(status=report["status"],suspicions=len(suspicions),improvements=len(improvements))))


def main():
    cli=argparse.ArgumentParser(description=__doc__)
    commands=cli.add_subparsers(dest="command",required=True)
    command=commands.add_parser("build")
    for arg in ("batch","source-root","output"):command.add_argument("--"+arg,type=Path,required=True)
    command=commands.add_parser("generate")
    for arg in ("build","output"):command.add_argument("--"+arg,type=Path,required=True)
    command=commands.add_parser("plan")
    for arg in ("batch","fixtures","real-manifest","output"):command.add_argument("--"+arg,type=Path,required=True)
    for name in ("run","smoke"):
        command=commands.add_parser(name)
        for arg in ("plan","output","control-root","control-wrapper","control-probe"):
            command.add_argument("--"+arg,type=Path,required=True)
        for arg in ("candidate-root","candidate-wrapper","candidate-probe"):
            command.add_argument("--"+arg,type=Path)
        command.add_argument("--workload",action="append")
    command=commands.add_parser("worker")
    for arg in ("plan","output","source-root","wrapper"):command.add_argument("--"+arg,type=Path,required=True)
    command.add_argument("--workload",required=True)
    command=commands.add_parser("freeze")
    for arg in ("plan","summary","validation","output"):command.add_argument("--"+arg,type=Path,required=True)
    command=commands.add_parser("compare")
    for arg in ("freeze","summary","output"):command.add_argument("--"+arg,type=Path,required=True)
    command.add_argument("--confirmation",action="store_true")
    args=cli.parse_args()
    if args.command=="worker":
        asyncio.run(worker(args));return
    with serial_lock():
        {"build":build,"generate":generate,"plan":make_plan,"run":run_suite,"smoke":run_suite,
         "freeze":freeze,"compare":compare}[args.command](args)


if __name__=="__main__":
    main()
