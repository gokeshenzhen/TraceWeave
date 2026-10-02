"""Read-only extraction from frozen model runs; correctness is graded separately.

Writes derived metrics/audit only. A completed model turn and a completed runner
process are distinct facts. Usage from an actual turn.completed is retained even
when process cleanup reaches the wall limit; it never turns that run into a pass.
Dependency reuse below means a later explicit waveform request touched the same
storage path, not that the dependency caused or improved the model's reasoning.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re


def records(path):
    if not path.exists():
        return []
    result = []
    for line in path.read_text().splitlines():
        try:
            result.append(json.loads(line))
        except ValueError:
            pass  # A concurrently written last line is not a completed event.
    return result


def payloads(call):
    result = []
    for block in call.get('result', []):
        if block.get('type') == 'text':
            try:
                result.append(json.loads(block['text']))
            except ValueError:
                pass
    return result


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def storage(path):
    # Drop only a trailing leaf selection, preserving instance/array indices.
    return re.sub(r'\[-?\d+(?::-?\d+)?\]$', '', path)


def requested_paths(call):
    if call['tool'] not in {'get_signal_at_time', 'get_signal_transitions',
                           'get_signals_around_time', 'get_signals_by_cycle',
                           'inspect_handshake', 'verify_window'}:
        return set()
    args = call['arguments']
    result = set(args.get('signal_paths', []))
    for key in ('signal_path', 'clock_path', 'valid', 'ready', 'valid_htrans',
                'hwrite', 'write_data'):
        if isinstance(args.get(key), str):
            result.add(args[key])
    return {storage(p) for p in result}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def collect(out):
    manifest = json.loads((out / 'manifest.json').read_text())
    rows, audits = [], []
    for receipt in records(out / 'runs.jsonl'):
        run = out / 'runs' / receipt['run']
        calls = sorted(records(run / 'calls.jsonl'), key=lambda c: c['index'])
        events = records(run / 'transcript.jsonl')
        completed = [e for e in events if e.get('type') == 'turn.completed']
        commands = [e['item'] for e in events if e.get('type') == 'item.completed'
                    and e.get('item', {}).get('type') == 'command_execution']
        skill_reads = [c for c in commands if '/.codex/skills/' in c.get('command', '')]
        source_commands = [c for c in commands if c not in skill_reads]
        errors, dependency_calls, receipts = [], [], []
        dep_bytes = 0
        dep_rows = []
        metrics = []
        for call in calls:
            for data in payloads(call):
                if isinstance(data, dict) and data.get('error'):
                    errors.append(dict(index=call['index'], tool=call['tool'], error=data['error']))
                for obj in walk(data):
                    if 'actual_backend' in obj:
                        receipts.append(dict(index=call['index'], tool=call['tool'],
                                             actual_backend=obj['actual_backend']))
                    if isinstance(obj.get('operation_metrics'), dict):
                        metrics.append(dict(index=call['index'], tool=call['tool'],
                                            metrics=obj['operation_metrics']))
                if not isinstance(data, dict) or not isinstance(data.get('dependency_context'), dict):
                    continue
                context = data['dependency_context']
                dependency_calls.append(call['index'])
                dep_bytes += len(json.dumps(context).encode())
                before = set().union(*(requested_paths(c) for c in calls if c['index'] < call['index']))
                after = set().union(*(requested_paths(c) for c in calls if c['index'] > call['index']))
                for dep in context.get('dependencies', []):
                    wave = dep.get('wave', {})
                    path = wave.get('path')
                    base = storage(path) if path else None
                    dep_rows.append(dict(index=call['index'], roles=dep.get('roles'),
                        binding=dep.get('binding'), wave=wave, source=dep.get('source'),
                        storage_requested_before=bool(base and base in before),
                        storage_requested_after=bool(base and base in after)))
        usage = completed[-1].get('usage') if completed else None
        tokens = usage.get('input_tokens', 0) + usage.get('output_tokens', 0) if usage else None
        count = Counter(c['tool'] for c in calls)
        final = (run / 'final.md').read_text()
        row = dict(receipt, model_turn_completed=bool(completed),
            final_present=bool(final), total_tokens=tokens,
            mcp_calls=len(calls), tool_counts=dict(count), mcp_errors=errors,
            shell_commands=len(commands), skill_reads=len(skill_reads),
            source_read_commands=len(source_commands),
            shell_errors=[dict(command=c['command'], exit_code=c.get('exit_code'))
                          for c in commands if c.get('exit_code') not in (None, 0)],
            tool_response_bytes=sum(c.get('response_bytes', 0) for c in calls),
            tool_elapsed_sum=sum(c['elapsed'] for c in calls),
            tool_elapsed_by_name={name:sum(c['elapsed'] for c in calls if c['tool'] == name)
                                  for name in count},
            dependency_requests=sum(bool(c['arguments'].get('include_dependencies')) for c in calls),
            dependency_calls=dependency_calls, dependency_response_bytes=dep_bytes,
            dependency_rows=dep_rows, backend_receipts=receipts,
            operation_metrics=metrics,
            input_budget_exceeded=bool(usage and usage.get('input_tokens', 0) > manifest['budgets']['input_tokens']),
            output_budget_exceeded=bool(usage and usage.get('output_tokens', 0) > manifest['budgets']['output_tokens']),
            source_read_budget_exceeded=len(source_commands) > manifest['budgets']['shell_reads'])
        rows.append(row)
        loaded = json.loads((run / 'loaded.json').read_text())
        mismatches = []
        root = Path(__file__).resolve().parents[3]
        for module in loaded['modules'].values():
            try:
                relative = str(Path(module['path']).relative_to(root))
            except ValueError:
                continue
            expected = manifest['product_hashes'].get(relative)
            if expected and expected != module['sha256']:
                mismatches.append(relative)
        schemas = json.loads((run / 'tools.json').read_text())
        native_hash = digest(schemas)
        for tool in schemas:
            if tool['name'] == 'explain_signal_driver':
                tool['inputSchema']['properties'].pop('include_dependencies', None)
        audits.append(dict(run=receipt['run'], pid=loaded['pid'], head=loaded['head'],
            product_mismatches=mismatches,
            prompt_matches=hashlib.sha256((run/'prompt.txt').read_bytes()).hexdigest()
                           == manifest['tasks'][receipt['case']]['sha256'],
            tool_schema_sha256=native_hash, common_tool_schema_sha256=digest(schemas),
            arm_A_dependency_requests=bool(receipt['arm'] == 'A' and row['dependency_requests'])))
    (out / 'metrics.jsonl').write_text(''.join(json.dumps(r, ensure_ascii=False)+'\n' for r in rows))
    (out / 'audit.json').write_text(json.dumps(dict(runs=audits,
        common_tool_schemas_equal=len({a['common_tool_schema_sha256'] for a in audits}) <= 1,
        independent_mcp_pids=len({a['pid'] for a in audits}) == len(audits),
        product_hashes_match=all(not a['product_mismatches'] for a in audits),
        prompts_match=all(a['prompt_matches'] for a in audits)), indent=2)+'\n')
    for r in rows:
        print(r['run'], r['status'], r['model_turn_completed'], r['total_tokens'],
              r['mcp_calls'], r['dependency_requests'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('experiment', type=Path)
    collect(parser.parse_args().experiment.resolve())
