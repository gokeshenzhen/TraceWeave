"""Shared public driver fixtures and a type/presence/order-sensitive oracle."""
import json

from src.schemas import ExplainDriverResult
from src.source_graph_backend import SourceGraphConnectivityBackend
from tests.test_dynamic_evidence import project
from tests.test_source_graph_backend import _entry
from tests.test_source_graph_driver_processes import bank_backend


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def guards_source(count=300):
    body = 'q=0;\n' + '\n'.join(
        f"if (sel == 9'd{i}) q=data ^ 9'd{i};" for i in range(count))
    return ('module t(input logic [8:0] sel,data,output logic [8:0] q); '
            f'always_comb begin {body} end endmodule')


def driver_fixture(kind):
    if kind == 'guards301':
        backend = SourceGraphConnectivityBackend(_entry(project(guards_source())._entry.ir))
        target = 't.q'
    else:
        backend, target = bank_backend(kind == 'fixed'), 'bank.S'
    raw = backend.find_driver(target, '', '', recursive=True)
    return ExplainDriverResult.model_validate({k: v for k, v in raw.items() if not k.startswith('_')}), backend


def public(model):
    return json.loads(model.model_dump_json(indent=2, exclude_none=True))
