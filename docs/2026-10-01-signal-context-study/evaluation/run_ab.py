"""Sequential independent Codex model runs. Never inject reviewer/oracle files."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[2]
STUDY=HERE.parent


def prompts():
    cases=json.loads((STUDY/'cases.json').read_text())
    scopes=cases['scope_aliases']; dma=scopes['dma']; bus=scopes['system_bus']; uart=scopes['uart_tx']
    tasks={
      'C1':('ot',f'解释 {uart}.tx_q 在 7145478 ps 附近的更新与 tick 的边沿关系，核对直接数据及控制条件，判断是否有异常。建议检查窗口 7074050–7288500 ps。'),
      'C2':('x1',f'本运行软件报告 DMA 数据比较失败。以 {bus}.int_master_req[4].wdata[31:0] 为起点，追查数据在哪个已检查边界变化，并排除至少一个竞争解释。重点窗口 944095000–944535000 ps。'),
      'C3':('x1',f'核验 {dma}.dma_state_q[31:0] 在 944515000 ps 附近的状态变化及 dma_done 的相位关系，判断该次完成转移是否异常。窗口 944475000–944535000 ps。'),
      'C4':('x2',f'本运行 DMA 等待超时。以 {dma}.dma_state_q[31:0] 为起点，区分状态逻辑本身异常和外部条件未满足，追查已检查的阻塞边界。先看 944095000–944535000 ps，按需要缩小或扩展。'),
      'C5':('x3',f'核验本运行 {bus}.int_master_req[4].wdata[31:0] 的 DMA 数据映射、grant 返回和 {dma}.dma_state_q[31:0] 的完成转移是否有异常。窗口 944095000–944535000 ps。只判断已检查范围。'),
      'S1':('ot',f'只定位 {uart}.tx_q 的驱动源码文件和行号，不继续分析时序或行为。'),
    }
    common='''你是独立调试者。完成下面的只读局部调试任务，直接给出结论，不询问用户。
使用 TraceWeave MCP。按工具说明先 diagnostic/discovery，再并行 hierarchy 与 structural scan，再 parse log；失败且有波形时运行有界 handshake sweep。读取源码前必须用 lookup_tb_files/get_tb_file_detail 确认编译路径。可用 shell 只读取这些已核验源码。不要运行仿真、编译、安装、修改文件或使用网络。
禁止读取 TraceWeave 的研究报告、cases.json、evaluation、git 历史、oracle 或其他 agent 记录；不要调用其他 agent。只从本次工具和已核验源码取证。源码中的注释不能替代波形证据。
每个根因判断保留两个竞争解释，检查相反侧，区分症状、传播点和起点。区分 before/after 相位；时钟初始未知不等于局部时钟异常。检查 partial/coverage/truncation，零覆盖或不完整不能说明干净；只将已检查范围写成结论，说明历史 source/wave 身份仍未独立证明。最后简洁列出关键时间/值、双方已查边界、结论和限制。
预算：最多 40 个 MCP 调用、8 次 shell 源码读取、300 秒。尽量批量查询和局部窗口。停止于预算或证据边界时如实说明。
'''
    result={}
    for name,(project,task) in tasks.items():
        a=cases['artifacts'][project]
        paths={k:a[k] for k in ('compile_log','log_path','wave_path','simulator')}
        clock=uart+'.clk_i' if project=='ot' else dma+'.clk_cg'
        result[name]=dict(project=project,prompt=common+'\n任务：'+task+'\n输入：'+json.dumps(paths,ensure_ascii=False)+'\n可用时钟路径：'+clock+'\n')
    return result


def freeze(destination):
    out=Path(destination).resolve();out.mkdir(parents=True,exist_ok=False)
    tasks=prompts()
    for name,item in tasks.items():
        (out/f'{name}.prompt.txt').write_text(item['prompt'])
    order=[dict(case=c,repeat=r,arm=a) for r in (1,2,3) for c in ('C1','C2','C3','C4','C5')
           for a in (('A','B') if r!=2 else ('B','A'))]
    order += [dict(case='S1',repeat=1,arm=a) for a in ('A','B')]
    manifest=dict(version=1,frozen_at=time.time(),p0_commit='651a91c',
        implementation_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        model='gpt-6-astra',reasoning_effort='max',runner='codex-cli 0.160.0',
        arms={'A':'same P0 and implementation checkout; dependency parameter hidden and blocked; original driver route only',
              'B':'same P0; optional dependency parameter exposed; no instruction to enable it'},
        budgets=dict(wall_seconds=300,mcp_calls=40,shell_reads=8,input_tokens=1000000,output_tokens=20000,
            token_limit_enforcement='post-run usage audit; hard wall and MCP call limits'),
        tasks={k:dict(project=v['project'],sha256=hashlib.sha256(v['prompt'].encode()).hexdigest()) for k,v in tasks.items()},
        order=order,primary_metric='task_total_input_plus_output_tokens',
        acceptance=dict(all_cases='B success count >= A; no new evidence/phase/coverage/normal-counterexample error',
            quality='B completes missing necessary evidence in >= 2/3 pairs on a target case, no regression elsewhere',
            efficiency='>=10% median token reduction on successful pairs in C1 or C4; >=2/3 same direction; five-case total tokens nonincreasing; median elapsed <=1.10 A',
            npi='C1 actual verdi_npi evidence required; Source Graph benefit cannot substitute',
            simple='S1 must not automatically expand dependencies'),
        cache='fresh model and MCP process every run; Source Graph disk/session cache disabled; OS cache uncontrolled; AB/BA/AB',
        source_limitations='Original RTL includes mutant comments; identical visibility, not strictly blind. Historical compile-to-wave identity unproven.',
        scripts={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (HERE/'eval_server.py',Path(__file__))})
    artifacts=json.loads((STUDY/'cases.json').read_text())['artifacts']
    manifest['artifacts']={}
    for name, artifact in artifacts.items():
        manifest['artifacts'][name]={}
        for key in ('compile_log','wave_path','log_path'):
            path=Path(artifact[key]);digest=hashlib.sha256()
            with path.open('rb') as f:
                for block in iter(lambda:f.read(1024*1024),b''):digest.update(block)
            manifest['artifacts'][name][key]=dict(path=str(path),size=path.stat().st_size,sha256=digest.hexdigest())
    product=['server.py','config.py',*subprocess.check_output(['git','ls-files','src'],cwd=ROOT,text=True).splitlines()]
    manifest['product_hashes']={p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in product if (ROOT/p).is_file()}
    (out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    print(out,flush=True)


def run_one(out, row, *, pilot=False):
    name=f"{row['case']}-{row['repeat']}-{row['arm']}"
    run=out/'runs'/name;run.mkdir(parents=True,exist_ok=False)
    prompt=(out/f"{row['case']}.prompt.txt").read_text() if not pilot else 'Call TraceWeave get_diagnostic_snapshot exactly once, then report availability briefly. Do not use other tools or read files.'
    workspace=Path(tempfile.mkdtemp(prefix='traceweave-e1-'))
    config='{command='+json.dumps(str(ROOT/'.venv/bin/python'))+',args='+json.dumps(
        [str(HERE/'eval_server.py'),str(ROOT),str(run),row['arm']])+',tool_timeout_sec=180,startup_timeout_sec=30,default_tools_approval_mode="approve"}'
    command=['codex','exec','--ignore-user-config','--ephemeral','--json','--skip-git-repo-check',
        '-C',str(workspace),'-s','read-only','-m','gpt-6-astra','-c','model_reasoning_effort="max"',
        '-c','project_doc_max_bytes=0','-c','mcp_servers.TraceWeave='+config,'-']
    (run/'command.json').write_text(json.dumps(command,indent=2)+'\n')
    (run/'prompt.txt').write_text(prompt)
    started=time.monotonic();timed_out=False
    with (run/'transcript.jsonl').open('w') as stdout, (run/'stderr.txt').open('w') as stderr:
        process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=stdout,stderr=stderr,
            text=True,start_new_session=True)
        process.stdin.write(prompt);process.stdin.close()
        try: process.wait(timeout=90 if pilot else 300)
        except subprocess.TimeoutExpired:
            timed_out=True;os.killpg(process.pid,signal.SIGTERM)
            try: process.wait(timeout=10)
            except subprocess.TimeoutExpired: os.killpg(process.pid,signal.SIGKILL);process.wait()
    elapsed=time.monotonic()-started
    events=[]
    for line in (run/'transcript.jsonl').read_text().splitlines():
        try: events.append(json.loads(line))
        except ValueError: pass
    completions=[e for e in events if e.get('type')=='turn.completed']
    usage=completions[-1].get('usage') if completions else None
    finals=[e['item'].get('text','') for e in events if e.get('type')=='item.completed' and e.get('item',{}).get('type')=='agent_message']
    (run/'final.md').write_text(finals[-1] if finals else '')
    status='timeout' if timed_out else ('complete' if completions else 'runner_error')
    receipt={**row,'run':name,'status':status,'exit_code':process.returncode,'elapsed':elapsed,'usage':usage,
        'transcript':str(run/'transcript.jsonl')}
    with (out/'runs.jsonl').open('a') as f:f.write(json.dumps(receipt)+'\n')
    print(json.dumps(receipt),flush=True)


def execute(destination, pilot=False):
    out=Path(destination).resolve()
    if pilot:
        out.mkdir(parents=True,exist_ok=True)
        run_one(out,dict(case='pilot',repeat=1,arm='B'),pilot=True);return
    manifest=json.loads((out/'manifest.json').read_text())
    for name,digest in manifest['scripts'].items():
        assert hashlib.sha256((HERE/name).read_bytes()).hexdigest()==digest
    for name,digest in manifest['product_hashes'].items():
        assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==digest
    done={json.loads(l)['run'] for l in (out/'runs.jsonl').read_text().splitlines()} if (out/'runs.jsonl').exists() else set()
    for row in manifest['order']:
        if f"{row['case']}-{row['repeat']}-{row['arm']}" not in done:
            run_one(out,row)


if __name__=='__main__':
    cli=argparse.ArgumentParser();cli.add_argument('action',choices=['freeze','run','pilot']);cli.add_argument('output')
    args=cli.parse_args()
    if args.action=='freeze':freeze(args.output)
    else:execute(args.output,pilot=args.action=='pilot')
