"""Test-only SV renderer; expected values never come from the implementation."""
import json
from pathlib import Path


FIXTURE = Path(__file__).parent / 'fixtures/expressions/operators.json'


def cases():
    data = json.loads(FIXTURE.read_text())
    return [{**case, 'inputs': {**data['inputs'], **case['inputs']}}
            for case in data['cases']]


def render():
    lines = ['module expression_oracle;', 'initial begin']
    for case in cases():
        lines.append('begin : ' + case['id'])
        for name, value in case['inputs'].items():
            signed = ' signed' if value.get('signed') else ''
            lines.append(f"logic{signed} [{value['width']-1}:0] {name};")
        for name, value in case['inputs'].items():
            lines.append(f"{name} = {value['width']}'b{value['value']};")
        expr = case['expression']
        lines.append(f'$display("EXPR {case["id"]} %0d %b", $bits({expr}), ({expr}));')
        lines.append('end')
    lines.extend(['$finish;', 'end', 'endmodule', ''])
    return '\n'.join(lines)


def check_output(output):
    observed = {}
    for line in output.splitlines():
        if line.startswith('EXPR '):
            _, name, width, bits = line.split()
            observed[name] = {'width': int(width), 'bin': bits.lower()}
    expected = {case['id']: case['expected'] for case in cases()}
    errors = {name: {'expected': wanted, 'observed': observed.get(name)}
              for name, wanted in expected.items() if observed.get(name) != wanted}
    assert not errors, errors
    assert set(observed) == set(expected)
