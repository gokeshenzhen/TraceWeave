"""Bounded input-port observations from an exclusive NPI Language owner.

Verified against Verdi V-2023.12-SP2: Port/HighConn/LowConn, PortInstFile/
LineNo, InternalScope and BinStrVal. A missing HighConn alone is NOT open.
Default-port semantics and contextual width conversion remain explicit gaps.
No source reading, recursive fan-in, waveform access or design building occurs.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from contextlib import nullcontext

from .hierarchy_source_ir import HierarchyBudgetExceeded, HierarchyQueryBudget
from .npi_hierarchy_source import _LanguageIterator, NpiHierarchyUnavailable
from .structural_semantic_router import NPI_PORT_CATEGORIES, NPI_CATEGORIES
from .structural_driver_facts import DRIVER_CATEGORIES, driver_fact
from .structural_gap_diagnostics import GapDiagnostics
from .structural_scan_continuation import initial_state, validate_state, new_job, job_position, MAX_FRONTIER_BYTES, MAX_PENDING, MAX_CUMULATIVE
from .structural_native_values import BITWISE, REDUCTIONS, operate, vector, value as abstract_value

CAPABILITY_VERSION = "npi_structural_v8"
_DYNAMIC = {"npiNet", "npiReg", "npiLogicVar", "npiIntVar", "npiIntegerVar", "npiBitVar", "npiStructVar", "npiStructNet", "npiPackedArrayVar", "npiPackedArrayNet", "npiEnumVar", "npiEnumNet"}
_CONSTANTS = {'npiConstant', 'npiParameter', 'npiSpecParam', 'npiEnumConst', 'npiPackedArrayParameter'}
_SELECTS = {'npiBitSelect', 'npiRegBit', 'npiNetBit', 'npiParameterBit', 'npiPartSelect'}
_SCOPES = {"npiModule", "npiInterface", "npiProgram", "npiGenScope"}
# InternalScope also exposes procedural statement scopes. They cannot contain
# elaborated module input bindings; generate scopes have their own npiGenScope
# kind and must still be traversed.
_IGNORE_SCOPES = {"npiTask", "npiFunction", "npiNamedBegin", "npiNamedFork", "npiClassDefn",
                  "npiFor", "npiBegin", "npiFork"}
# Copy a bounded group of sibling paths before descending. Wide generated
# hierarchies otherwise reopen and replay the same parent prefix every 32
# children. Frontier byte/frame limits can flush this batch earlier.
_CHILD_BATCH_SIZE = 64


class _UnsupportedPort(Exception):
    def __init__(self, reason, **details):
        self.reason = reason
        self.details = details
        super().__init__(reason)


def scan_budget(options, *, deadline=None):
    limits = options['limits']
    budget = HierarchyQueryBudget(timeout_sec=options['timeout_sec'],
                                  max_scopes=limits['max_instances'], max_calls=100000,
                                  max_symbols=100000, max_bytes=8 * 1024 * 1024, max_depth=128)
    if deadline is not None:
        budget.deadline = min(budget.deadline, deadline)
    return budget


def scan_native_ports(provider, options, *, state=None, driver_reader=None):
    p, budget = provider, provider.budget
    categories = set(options['categories']) & NPI_CATEGORIES
    driver_categories = categories & DRIVER_CATEGORIES
    root = options.get('scope') or p.top
    work = initial_state(root, categories) if state is None else validate_state(state, top=p.top, root=root, categories=categories)
    gaps = {c: set(work['category_gaps'][c]) for c in categories}
    facts, seen = [], set()
    started = time.monotonic()
    ports = driver_bits = 0
    diagnostics = GapDiagnostics()
    diagnostics.merge(work['gap_summary'])
    gap_sink, diag_sink = gaps, diagnostics

    def gap(reason, affected=None, **details):
        selected = categories if affected is None else categories & set(affected)
        if not selected:
            return
        for c in selected:
            gap_sink[c].add(reason)
        diag_sink.add(reason, details)

    def width(handle):
        value = p.scalar(handle, 'size')
        if type(value) is not int or value < 1:
            raise _UnsupportedPort('npi_width_unavailable', expression_kind=p.scalar(handle, 'type'), actual_width=value)
        if value > options['limits']['max_width']:
            raise _UnsupportedPort('native_width_limit')
        return value

    def binary(handle, size):
        p.check(); budget.call()
        value = p.low.get_value(handle.cps_obj, p.low.BinStrVal, False)
        budget.checkpoint()
        if value is None:
            return None
        # get_str_val copies a scalar from the value capsule while still owned.
        budget.call(); bits = value.get_str_val(); budget.checkpoint()
        if not isinstance(bits, str) or len(bits) != size or set(bits.lower()) - set('01xz'):
            return None
        return bits.lower()

    def index_value(handle, depth):
        if handle is None:
            raise _UnsupportedPort('npi_selection_range_unavailable')
        size, rows, complete = parts(handle, depth + 1)
        if not complete or len(rows) != 1 or set(rows[0]['bits']) - set('01'):
            raise _UnsupportedPort('npi_selection_range_unavailable')
        bits = rows[0]['bits']
        value = int(bits, 2)
        # A leading zero denotes the same nonnegative index with either
        # signedness. Enum constants may lack this otherwise irrelevant SDK
        # metadata; high-bit values still require an unambiguous interpretation.
        if bits[0] == '0':
            return value
        return value - (1 << size) if signedness(handle, depth) == 1 else value

    def packed_range(handle, size, depth):
        ts = p.one(handle, 'typespec_handle')
        if ts is None:
            raise _UnsupportedPort('npi_selection_range_unavailable')
        kind = p.scalar(ts, 'type')
        for _ in range(64):
            if kind != 'npiEnumTypespec':
                break
            ts = p.one(ts, 'base_typespec_handle')
            if ts is None:
                raise _UnsupportedPort('npi_selection_range_unavailable')
            kind = p.scalar(ts, 'type')
        else:
            raise HierarchyBudgetExceeded('native_expression_depth_limit')
        # The installed SDK exposes packed dimensions under the typespec, not
        # LeftRange/RightRange directly on a Reg/Parameter. Reject aggregate and
        # multidimensional types rather than guessing their flattened order.
        if kind in {'npiIntTypespec', 'npiIntegerTypespec'} and size == 32:
            return 31, 0, 1
        if kind == 'npiStructTypespec' and p.scalar(ts, 'packed') == 1:
            return size - 1, 0, 1
        if kind not in {'npiLogicTypespec', 'npiBitTypespec', 'npiPackedArrayTypespec'}:
            raise _UnsupportedPort('npi_selection_range_unavailable')
        it = _LanguageIterator(p, p.low.Range, ts)
        try:
            r = it.read()
            if r is None or it.read() is not None:
                raise _UnsupportedPort('npi_selection_range_unavailable')
            left = index_value(p.one(r, 'left_range_handle'), depth)
            right = index_value(p.one(r, 'right_range_handle'), depth)
        finally:
            p.close_iterator(it)
        count = abs(left - right) + 1
        if size % count or kind != 'npiPackedArrayTypespec' and count != size:
            raise _UnsupportedPort('npi_selection_range_unavailable')
        return left, right, size // count

    def selection(handle, kind, depth):
        parent = p.one(handle, 'parent_handle')
        parent_kind = p.scalar(parent, 'type') if parent is not None else None
        if parent_kind not in _DYNAMIC | _CONSTANTS:
            raise _UnsupportedPort('npi_selection_parent_unmodeled', expression_kind=kind, parent_kind=parent_kind)
        parent_size, rows, complete = parts(parent, depth + 1)
        decl_left, decl_right, stride = packed_range(parent, parent_size, depth)
        if kind == 'npiPartSelect':
            left = index_value(p.one(handle, 'left_range_handle'), depth)
            right = index_value(p.one(handle, 'right_range_handle'), depth)
            size = (abs(left - right) + 1) * stride
            if size > options['limits']['max_width']:
                raise _UnsupportedPort('native_width_limit')
            observed = p.scalar(handle, 'size')
            if ((type(observed) is int and observed > 0 and observed != size)
                    or left != right and (left > right) != (decl_left > decl_right)):
                raise _UnsupportedPort('npi_selection_range_unavailable')
        else:
            size = stride
            if kind != 'npiBitSelect' and width(handle) != size:
                raise _UnsupportedPort('npi_width_unavailable')
            index = p.one(handle, 'index_handle')
            if index is not None:
                left = right = index_value(index, depth)
            else:
                # Fixed bit objects in V-2023.12-SP2 have no Index relationship.
                # Accept only a canonical decimal suffix of the EXACT parent's
                # native identity, never decompile/parse an expression or name
                # a new object by a guessed path. Variable indexes remain gaps.
                path, parent_path = p.scalar(handle, 'full_name'), p.scalar(parent, 'full_name')
                suffix = path[len(parent_path):] if (isinstance(path, str)
                         and isinstance(parent_path, str) and parent_path and path.startswith(parent_path)) else ''
                if (len(suffix) <= 32 and re.fullmatch(r'\[-?(?:0|[1-9][0-9]*)\]', suffix)
                        and (kind != 'npiBitSelect' or p.scalar(handle, 'constant_select') == 1)):
                    left = right = int(suffix[1:-1])
                else:
                    # Elaborated package/genvar selections retain symbolic
                    # spellings in this SDK. Native identity, not string
                    # evaluation, proves the selected declared bit.
                    matches = []
                    if (stride == 1 and parent_size <= 256
                            and (parent_kind in _DYNAMIC or p.scalar(handle, 'constant_select') == 1)
                            and callable(getattr(p.low, 'handle_by_index', None))
                            and callable(getattr(p.low, 'compare_objects', None))):
                        for index in range(min(decl_left, decl_right), max(decl_left, decl_right) + 1):
                            p.check(); budget.call()
                            bit = p.wrap(p.low.handle_by_index(parent.cps_obj, index))
                            budget.checkpoint()
                            if bit is not None:
                                budget.call()
                                same = p.low.compare_objects(handle.cps_obj, bit.cps_obj)
                                budget.checkpoint()
                                if type(same) is not int or same not in (0, 1):
                                    raise _UnsupportedPort('npi_selection_identity_unavailable')
                                if same == 1:
                                    matches.append(index)
                    if len(matches) != 1:
                        raise _UnsupportedPort('npi_selection_index_unavailable', expression_kind=kind, parent_kind=parent_kind)
                    left = right = matches[0]
        if not (min(decl_left, decl_right) <= min(left, right)
                <= max(left, right) <= max(decl_left, decl_right)):
            raise _UnsupportedPort('npi_selection_out_of_range')
        offset = abs(right - decl_right) * stride
        selected = []
        for row in rows:
            lo = max(offset, row['lsb_offset'])
            hi = min(offset + size, row['lsb_offset'] + row['width'])
            if lo < hi:
                end = row['width'] - (lo - row['lsb_offset'])
                selected.append({'lsb_offset': lo - offset, 'width': hi - lo,
                                 'bits': row['bits'][end - (hi - lo):end]})
        return size, selected, complete

    def signedness(handle, depth=0):
        if depth > 64:
            raise HierarchyBudgetExceeded('native_expression_depth_limit')
        kind = p.scalar(handle, 'type')
        if kind in {'npiRefObj', 'npiExtRef'}:
            actual = p.one(handle, 'actual_handle')
            if actual is None:
                raise _UnsupportedPort('npi_reference_unresolved')
            return signedness(actual, depth + 1)
        op = p.scalar(handle, 'op_type') if kind == 'npiOperation' else None
        if op in {'npiConcatOp', 'npiMultiConcatOp', 'npiNotOp', 'npiLogAndOp', 'npiLogOrOp'} | REDUCTIONS.keys():
            return 0
        if op in {'npiExprCastOp', 'npiConditionOp', 'npiBitNegOp'} | BITWISE.keys():
            it = _LanguageIterator(p, p.low.Operand, handle)
            try:
                args = []
                for _ in range(4):
                    item = it.read()
                    if item is None:
                        break
                    args.append(item)
                expected = 3 if op == 'npiConditionOp' else 1 if op == 'npiBitNegOp' else 2
                if len(args) != expected:
                    raise _UnsupportedPort('npi_signedness_unavailable', expression_kind=kind)
                # A sized cast retains the data operand's signedness; SDK
                # metadata can instead describe its width-expression operand.
                data = args[1:] if op in {'npiExprCastOp', 'npiConditionOp'} else args
                return common_signedness(data, depth + 1)
            finally:
                p.close_iterator(it)
        signed = p.scalar(handle, 'signed')
        if signed in (0, 1):
            return signed
        ts = p.one(handle, 'typespec_handle')
        signed = p.scalar(ts, 'signed') if ts is not None else None
        if signed not in (0, 1):
            raise _UnsupportedPort('npi_signedness_unavailable', expression_kind=kind)
        return signed

    def common_signedness(handles, depth=0):
        failure = None
        for h in handles:
            try:
                if signedness(h, depth) == 0:
                    return 0
            except _UnsupportedPort as exc:
                failure = exc
        if failure is not None:
            raise failure
        return 1

    def check_parameter_selection(handle, depth=0):
        if depth > 64:
            raise HierarchyBudgetExceeded('native_expression_depth_limit')
        parent = p.one(handle, 'parent_handle')
        if parent is None:
            return
        kind = p.scalar(parent, 'type')
        if kind in {'npiArrayParameter', 'npiPackedArrayParameter'}:
            # The SDK can return the first element's value for a dynamic
            # parameter-array selection. Type=Parameter is not constant proof
            # until the exact selection is verified independently of value().
            ts = p.one(parent, 'typespec_handle')
            it = _LanguageIterator(p, p.low.Range, ts) if ts is not None else None
            try:
                r = it.read() if it is not None else None
                if r is None or it.read() is not None:
                    raise _UnsupportedPort('npi_constant_selection_unverified')
                left = index_value(p.one(r, 'left_range_handle'), depth)
                right = index_value(p.one(r, 'right_range_handle'), depth)
            finally:
                if it is not None:
                    p.close_iterator(it)
            index = p.one(handle, 'index_handle')
            if index is not None:
                try:
                    selected = index_value(index, depth)
                except _UnsupportedPort:
                    raise _UnsupportedPort('npi_constant_selection_unverified') from None
            else:
                path, parent_path = p.scalar(handle, 'full_name'), p.scalar(parent, 'full_name')
                suffix = path[len(parent_path):] if (isinstance(path, str)
                    and isinstance(parent_path, str) and parent_path and path.startswith(parent_path)) else ''
                if len(suffix) > 32 or not re.fullmatch(r'\[-?(?:0|[1-9][0-9]*)\]', suffix):
                    raise _UnsupportedPort('npi_constant_selection_unverified')
                selected = int(suffix[1:-1])
            if not min(left, right) <= selected <= max(left, right):
                raise _UnsupportedPort('npi_constant_selection_unverified')
        if kind in {'npiParameter', 'npiPackedArrayParameter'}:
            check_parameter_selection(parent, depth + 1)

    def resize(handle, data, target, common_signed=None):
        size, rows, _ = data
        bits = vector(size, rows)
        if target == size:
            return data
        if target < size:
            return abstract_value(bits[-target:])
        resolved = handle
        for _ in range(65):
            kind = p.scalar(resolved, 'type')
            if kind not in {'npiRefObj', 'npiExtRef'}:
                break
            resolved = p.one(resolved, 'actual_handle')
            if resolved is None:
                raise _UnsupportedPort('npi_reference_unresolved')
        else:
            raise HierarchyBudgetExceeded('native_expression_depth_limit')
        signed = signedness(resolved) if common_signed is None else common_signed
        # This SDK does not distinguish an unbased unsized '1/'x/'z from a
        # one-bit unsigned literal. Their context extension differs. Keep that
        # case unresolved; '0 and 1'b0 have identical extension semantics.
        if (size == 1 and signed == 0 and kind == 'npiConstant'
                and bits != '0' and p.scalar(resolved, 'signed') != 1):
            raise _UnsupportedPort('npi_contextual_width_conversion_unverified', actual_width=size, port_width=target)
        return abstract_value((bits[0] if signed else '0') * (target-size) + bits)

    def operation(handle, depth, context=None):
        op = p.scalar(handle, 'op_type')
        it = _LanguageIterator(p, p.low.Operand, handle)
        handles = []
        try:
            for _ in range(257):
                item = it.read()
                if item is None:
                    break
                if len(handles) == 256:
                    raise HierarchyBudgetExceeded('native_operand_limit')
                handles.append(item)
        finally:
            p.close_iterator(it)
        operands = [parts(item, depth + 1) for item in handles]
        if op == 'npiAssignmentPatternOp' and len(handles) == 1 and context is not None:
            formal = p.one(context, 'low_conn_handle')
            ts = p.one(formal, 'typespec_handle') if formal is not None else None
            if ts is not None and p.scalar(ts, 'type') in {'npiPackedArrayTypespec', 'npiArrayTypespec'}:
                it = _LanguageIterator(p, p.low.Range, ts)
                try:
                    r = it.read()
                    single = (r is not None and it.read() is None
                        and index_value(p.one(r, 'left_range_handle'), depth)
                        == index_value(p.one(r, 'right_range_handle'), depth))
                finally:
                    p.close_iterator(it)
                elem = p.one(ts, 'elem_typespec_handle')
                if (single and elem is not None and p.scalar(elem, 'type') == 'npiStructTypespec'
                        and p.scalar(elem, 'packed') == 1 and operands[0][0] == width(context)):
                    bits = vector(*operands[0][:2])
                    if set(bits) & set('xz'):
                        actual_type = p.one(handles[0], 'typespec_handle')
                        if actual_type is None:
                            raise _UnsupportedPort('npi_pattern_four_state_conversion_unverified')
                        budget.call()
                        same = p.low.compare_objects(actual_type.cps_obj, elem.cps_obj)
                        budget.checkpoint()
                        if same != 1:
                            raise _UnsupportedPort('npi_pattern_four_state_conversion_unverified')
                    return operands[0]
        if op == 'npiExprCastOp' and len(handles) == 2:
            size = index_value(handles[0], depth)
            if not 1 <= size <= options['limits']['max_width']:
                raise _UnsupportedPort('native_width_limit')
            return resize(handles[1], operands[1], size)
        if op == 'npiMultiConcatOp' and len(handles) == 2:
            count = index_value(handles[0], depth)
            size = count * operands[1][0]
            if count < 1 or size > options['limits']['max_width']:
                raise _UnsupportedPort('native_width_limit')
            return abstract_value(vector(*operands[1][:2]) * count)
        size = p.scalar(handle, 'size')
        # Concat operands are self-determined. The SDK can return an incorrect
        # positive parent Size as well as -1 for a nested sized cast.
        if op == 'npiConcatOp' and operands:
            size = sum(w for w, _, _ in operands)
        elif op in BITWISE:
            size = max((w for w, _, _ in operands), default=0)
        elif op == 'npiBitNegOp' and len(operands) == 1:
            size = operands[0][0]
        elif op == 'npiConditionOp' and len(operands) == 3:
            size = max(operands[1][0], operands[2][0])
        elif op in {'npiNotOp', 'npiLogAndOp', 'npiLogOrOp'} | REDUCTIONS.keys():
            size = 1
        if type(size) is not int or size < 1:
            raise _UnsupportedPort('npi_width_unavailable', expression_kind='npiOperation', operator=op, actual_width=size)
        if size > options['limits']['max_width']:
            raise _UnsupportedPort('native_width_limit')
        if not operands:
            gap('npi_expression_unmodeled', ['constant_connection'], expression_kind='npiOperation', operator=op)
            return size, [], False
        if op == 'npiConcatOp' and sum(w for w, _, _ in operands) == size:
            return abstract_value(''.join(vector(w, rows) for w, rows, _ in operands))
        aligned = list(operands)
        if op in BITWISE or op == 'npiConditionOp':
            positions = range(1, 3) if op == 'npiConditionOp' and len(handles) == 3 else range(len(handles))
            if any(operands[i][0] != size for i in positions):
                common = common_signedness([handles[i] for i in positions])
                for i in positions:
                    aligned[i] = resize(handles[i], operands[i], size, common_signed=common)
        bits = operate(op, [vector(w, rows) for w, rows, _ in aligned])
        if bits is not None:
            return abstract_value(bits)
        if all(complete for _, _, complete in operands):
            bits = binary(handle, size)
            if bits is not None:
                return size, [{'lsb_offset': 0, 'width': size, 'bits': bits}], True
            gap('npi_constant_value_unavailable', ['constant_connection'])
        gap('npi_expression_unmodeled', ['constant_connection'], expression_kind='npiOperation', operator=op)
        return size, [], False

    def parts(handle, depth=0, context=None):
        if depth > 64:
            raise HierarchyBudgetExceeded('native_expression_depth_limit')
        kind = p.scalar(handle, 'type')
        if kind in {'npiRefObj', 'npiExtRef'}:
            # These are references, not packed values: size is -1 in the SDK.
            # Resolve the documented relationship before inspecting type/width;
            # in particular, never evaluate an initial value of referenced state.
            actual = p.one(handle, 'actual_handle')
            if actual is None:
                raise _UnsupportedPort('npi_reference_unresolved')
            return parts(actual, depth + 1, context=context)
        if kind in _SELECTS:
            return selection(handle, kind, depth)
        if kind == 'npiOperation':
            return operation(handle, depth, context=context)
        size = width(handle)
        if kind in _CONSTANTS:
            if kind != 'npiConstant':
                check_parameter_selection(handle, depth)
            bits = binary(handle, size)
            if bits is None:
                gap('npi_constant_value_unavailable', ['constant_connection'])
            return size, ([] if bits is None else [{'lsb_offset': 0, 'width': size, 'bits': bits}]), bits is not None
        if kind in _DYNAMIC:
            if kind in {'npiStructVar', 'npiStructNet'}:
                ts = p.one(handle, 'typespec_handle')
                if ts is None or p.scalar(ts, 'packed') != 1:
                    raise _UnsupportedPort('npi_packed_type_unavailable', expression_kind=kind)
            return size, [], False
        gap('npi_expression_unmodeled', ['constant_connection'], expression_kind=kind)
        return size, [], False

    def location(handle, inst=False):
        file = p.scalar(handle, 'port_inst_file' if inst else 'file')
        line = p.scalar(handle, 'port_inst_line_no' if inst else 'line_no')
        return {'file': file, 'line': line} if isinstance(file, str) and file and type(line) is int and line > 0 else None

    def save(kind, **data):
        if kind not in categories:
            return
        if len(facts) >= options['limits']['max_facts']:
            raise HierarchyBudgetExceeded('fact_limit')
        fact = dict(kind=kind, **data)
        size = len(json.dumps(fact, ensure_ascii=True)) * 4 + 256
        if budget.fact_bytes + size > budget.max_bytes:
            raise HierarchyBudgetExceeded('fact_byte_limit')
        budget.fact_bytes += size
        if sum(work['counts'].values()) >= MAX_CUMULATIVE:
            raise HierarchyBudgetExceeded('native_total_fact_limit')
        facts.append(fact)
        work['counts'][kind] = work['counts'].get(kind, 0) + 1

    def position_fingerprint(previous, identity):
        return hashlib.sha256((previous + '\0' + json.dumps(identity, ensure_ascii=True)).encode()).hexdigest()

    def inspect_port(port, path, name):
        direction = p.scalar(port, 'direction')
        if direction != 'npiInput':
            if direction not in {'npiOutput', 'npiInout', 'npiRef'}:
                gap('npi_port_direction_unavailable')
            return
        if not isinstance(name, str) or not name:
            gap('npi_port_identity_unavailable'); return
        diag_sink.begin_port(path, name)
        actual = p.one(port, 'high_conn_handle')
        loc = location(port, True)
        base = dict(instance=path, port=name, source=loc,
                    definition_source=location(port), source_association='unverified', origin='input_binding')
        if actual is None:
            low = p.one(port, 'low_conn_handle')
            if (loc is not None and low is not None
                    and p.scalar(low, 'full_name') == path + '.' + name):
                # Positive port-instance syntax + exact low binding,
                # not a missing driver or an empty relationship alone.
                save('open_input', **base, connection_status='explicit_empty',
                     interpretation='Explicit empty parent actual; default-port behavior unverified. No implicit 0 or Z conclusion.')
                gap('npi_empty_actual_default_semantics_unverified')
            else:
                gap('npi_missing_actual_or_default_unresolved')
            return
        if 'constant_connection' not in categories:
            return
        try:
            port_width = width(port)
            size, regions, _ = parts(actual, context=port)
        except _UnsupportedPort as exc:
            gap(exc.reason, ['constant_connection'], **exc.details)
            return
        except HierarchyBudgetExceeded as exc:
            if exc.reason not in {'native_expression_depth_limit', 'native_operand_limit'}:
                raise
            # A bounded, unmodeled expression is a gap on this port, not an
            # invitation to retry it forever or abandon unrelated instances.
            gap(exc.reason, ['constant_connection'])
            return
        if size != port_width:
            try:
                size, regions, _ = resize(actual, (size, regions, False), port_width)
            except _UnsupportedPort as exc:
                gap(exc.reason, ['constant_connection'], **exc.details)
                return
        if regions:
            save('constant_connection', **base, width=port_width, constant_regions=regions)

    def inspect_ports(handle, path, job):
        nonlocal ports, gap_sink, diag_sink
        if path == p.top:
            job['phase'] = 'signals' if driver_categories else 'children'
            return
        it = _LanguageIterator(p, p.low.Port, handle)
        index = 0
        fingerprint = ''
        try:
            while True:
                port = it.read()
                if port is None:
                    if index < job['port_index']:
                        raise NpiHierarchyUnavailable('npi_scan_position_changed')
                    job['phase'] = 'signals' if driver_categories else 'children'
                    break
                name = p.scalar(port, 'name')
                next_fingerprint = position_fingerprint(fingerprint, name)
                if index < job['port_index']:
                    fingerprint = next_fingerprint
                    index += 1
                    if index == job['port_index'] and fingerprint != job['port_fingerprint']:
                        raise NpiHierarchyUnavailable('npi_scan_position_changed')
                    continue
                if ports >= budget.max_symbols:
                    raise HierarchyBudgetExceeded('native_port_limit')
                ports += 1
                local_gaps, local_diag = {c: set() for c in categories}, GapDiagnostics()
                gap_sink, diag_sink = local_gaps, local_diag
                try:
                    inspect_port(port, path, name)
                finally:
                    gap_sink, diag_sink = gaps, diagnostics
                # Commit only a fully inspected port. On a stop its cursor and
                # diagnostics are replayed; earlier positive ports survive.
                for c in categories:
                    gaps[c].update(local_gaps[c])
                diagnostics.merge(local_diag.result())
                index += 1
                job['port_index'] = index
                fingerprint = next_fingerprint
                job['port_fingerprint'] = fingerprint
        finally:
            p.close_iterator(it)

    def inspect_signals(handle, path, job):
        nonlocal driver_bits
        if driver_reader is None:
            gap('npi_driver_reader_unavailable', DRIVER_CATEGORIES)
            job['phase'] = 'children'
            return
        index, fingerprint = 0, ''
        scope_kind = p.scalar(handle, 'type')
        owner, owner_kind = handle, scope_kind
        for _ in range(budget.max_depth + 1):
            if owner_kind != 'npiGenScope':
                break
            owner = p.one(owner, 'scope_handle')
            if owner is None:
                break
            owner_kind = p.scalar(owner, 'type')
        blackbox = p.scalar(owner, 'is_black_box') if owner is not None and owner_kind == 'npiModule' else None
        eligible_scope = owner_kind == 'npiModule' and blackbox == 0 and not p.degraded
        if not eligible_scope and 'undriven_signal' in driver_categories:
            gap('npi_driver_scope_unverified', {'undriven_signal'})
        boundaries = set()
        iterator = _LanguageIterator(p, p.low.Port, handle)
        try:
            while (port := iterator.read()) is not None:
                direction = p.scalar(port, 'direction')
                if direction in {'npiInput','npiInout','npiRef'}:
                    low = p.one(port, 'low_conn_handle')
                    name = p.scalar(low, 'full_name') if low is not None else None
                    if isinstance(name, str) and name.startswith(path + '.'):
                        boundaries.add(name)
                    else:
                        eligible_scope = False
                        gap('npi_driver_port_boundary_unmodeled', {'undriven_signal'})
        finally:
            p.close_iterator(iterator)
        for relation in ('Net', 'Reg'):
            constant = getattr(p.low, relation, None)
            if constant is None:
                gap('npi_driver_declarations_unavailable', DRIVER_CATEGORIES)
                continue
            it = _LanguageIterator(p, constant, handle)
            try:
                while (signal := it.read()) is not None:
                    name, full = p.scalar(signal, 'name'), p.scalar(signal, 'full_name')
                    kind, size = p.scalar(signal, 'type'), p.scalar(signal, 'size')
                    identity = [relation, name, full, kind, size]
                    following = position_fingerprint(fingerprint, identity)
                    if index < job['signal_index']:
                        index += 1; fingerprint = following
                        if index == job['signal_index'] and fingerprint != job['signal_fingerprint']:
                            raise NpiHierarchyUnavailable('npi_scan_position_changed')
                        continue
                    try:
                        if not isinstance(name, str) or full != path + '.' + name:
                            raise _UnsupportedPort('npi_driver_signal_identity_unavailable')
                        if kind not in {'npiNet','npiReg','npiLogicVar','npiBitVar','npiIntVar','npiIntegerVar'}:
                            raise _UnsupportedPort('npi_driver_signal_type_unsupported')
                        size = width(signal)
                        if size == 1:
                            # A scalar has no Range. Preserve an explicitly
                            # declared one-bit range when the SDK supplies it.
                            try:
                                left, right, stride = packed_range(signal, size, 0)
                            except _UnsupportedPort:
                                ts = p.one(signal, 'typespec_handle')
                                if ts is None or p.scalar(ts, 'type') not in {'npiLogicTypespec','npiBitTypespec'}:
                                    raise
                                iterator = _LanguageIterator(p, p.low.Range, ts)
                                try:
                                    if iterator.read() is not None:
                                        raise _UnsupportedPort('npi_driver_range_unavailable')
                                finally:
                                    p.close_iterator(iterator)
                                left = right = 0; stride = 1
                        else:
                            left, right, stride = packed_range(signal, size, 0)
                        if stride != 1:
                            raise _UnsupportedPort('npi_driver_packed_shape_unsupported')
                        bits = tuple(range(left, right + (-1 if left > right else 1), -1 if left > right else 1))
                        net_kind = p.scalar(signal, 'net_type') if kind == 'npiNet' else 'variable'
                        current = position_fingerprint(following, [left, right, net_kind])
                        if job['bit_index'] and current != job['signal_current_fingerprint']:
                            raise NpiHierarchyUnavailable('npi_scan_position_changed')
                        source = location(signal)
                    except _UnsupportedPort as exc:
                        if job['bit_index']:
                            raise NpiHierarchyUnavailable('npi_scan_position_changed') from exc
                        gap(exc.reason, DRIVER_CATEGORIES)
                        bits = ()
                    except HierarchyBudgetExceeded as exc:
                        if exc.reason not in {'native_expression_depth_limit', 'native_operand_limit'}:
                            raise
                        gap(exc.reason, DRIVER_CATEGORIES)
                        bits = ()
                    span = getattr(driver_reader, 'signal', None)
                    with (span(full, left, right) if bits and callable(span) else nullcontext()):
                        for offset in range(job['bit_index'], len(bits)):
                            if ports + driver_bits >= budget.max_symbols:
                                raise HierarchyBudgetExceeded('native_driver_bit_limit')
                            bit = bits[offset]
                            query_path = full if size == 1 and bit == 0 else f'{full}[{bit}]'
                            drivers, complete, used, reasons = driver_reader.read(query_path, categories=driver_categories)
                            driver_bits += 1
                            selected_kind = ('multiple_driver' if len(drivers) >= 2 else
                                'undriven_signal' if not drivers and complete and used
                                and full not in boundaries and eligible_scope and kind == 'npiNet'
                                and net_kind in {'npiWire','npiTri','npiUwire'} else None)
                            if not drivers and complete and used and full not in boundaries and selected_kind is None:
                                reasons.add('npi_driver_negative_boundary')
                            if selected_kind in driver_categories:
                                fact = driver_fact(selected_kind, full, [bit], drivers,
                                    complete=complete, source=source, net_kind=net_kind)
                                fact['source_association'] = 'unverified'
                                save(fact.pop('kind'), **fact)
                            for reason in reasons:
                                gap(reason, DRIVER_CATEGORIES)
                            job['bit_index'] = offset + 1
                            job['signal_current_fingerprint'] = current
                    index += 1; fingerprint = following
                    job.update(signal_index=index, bit_index=0, signal_fingerprint=fingerprint,
                               signal_current_fingerprint='')
            finally:
                p.close_iterator(it)
        if index < job['signal_index']:
            raise NpiHierarchyUnavailable('npi_scan_position_changed')
        job['phase'] = 'children'

    def job_bytes(job):
        return 512 + 6 * (len(job['path']) + len(job['parent'] or ''))

    pending = work['pending']
    frontier_bytes = sum(job_bytes(j) + sum(job_bytes(c) for c in j['children']) for j in pending)
    initial_position = (work['completed_instances'], sum(work['counts'].values()),
                        [job_position(j) for j in pending])
    stop_reason = None

    def visit_job(job):
        nonlocal frontier_bytes
        path, depth = job['path'], job['depth']
        p.check()
        if depth > budget.max_depth:
            raise HierarchyBudgetExceeded('scope_depth_limit')
        path_key = hashlib.sha256(path.encode()).digest()
        if path_key not in seen:
            if budget.scope_count >= budget.max_scopes:
                raise HierarchyBudgetExceeded('scope_limit')
            seen.add(path_key); budget.scope_count += 1
        handle = p.lookup(path)
        if handle is None:
            gap('npi_scope_not_found')
            return
        if p.scalar(handle, 'full_name') != path:
            raise NpiHierarchyUnavailable('npi_scope_changed_during_query')
        if job['parent'] is None:
            if not p._under_top(handle):
                raise NpiHierarchyUnavailable('npi_scope_changed_during_query')
        else:
            parent = p.one(handle, 'scope_handle')
            if parent is None or p.scalar(parent, 'full_name') != job['parent']:
                raise NpiHierarchyUnavailable('npi_scope_parent_mismatch')
        kind = p.scalar(handle, 'type')
        if kind not in _SCOPES:
            gap('npi_scope_kind_unmodeled')
            return
        if kind in {'npiModule', 'npiInterface', 'npiProgram'} and p.scalar(handle, 'is_black_box') == 1:
            gap('npi_blackbox_boundary')
        if job['phase'] == 'ports':
            if categories & NPI_PORT_CATEGORIES:
                inspect_ports(handle, path, job)
            else:
                job['phase'] = 'signals' if driver_categories else 'children'
        if job['phase'] == 'signals':
            inspect_signals(handle, path, job)
        it = _LanguageIterator(p, p.low.InternalScope, handle)
        index = 0
        fingerprint = ''
        child_paths = {c['path'] for c in job['children']}
        # Keep the larger batch for shallow/wide parents, then fall back to
        # legacy breadth as queued paths consume the frontier. This preserves
        # room for descending through deeply nested generated subtrees.
        child_batch_size = min(_CHILD_BATCH_SIZE,
                               32 if frontier_bytes >= MAX_FRONTIER_BYTES // 4 else _CHILD_BATCH_SIZE)
        try:
            while True:
                child = it.read()
                if child is None:
                    if index < job['child_index']:
                        raise NpiHierarchyUnavailable('npi_scan_position_changed')
                    break
                kind, child_path = p.scalar(child, 'type'), p.scalar(child, 'full_name')
                next_fingerprint = position_fingerprint(fingerprint, [kind, child_path])
                if index < job['child_index']:
                    fingerprint = next_fingerprint
                    index += 1
                    if index == job['child_index'] and fingerprint != job['child_fingerprint']:
                        raise NpiHierarchyUnavailable('npi_scan_position_changed')
                    continue
                if kind not in _IGNORE_SCOPES:
                    parent = p.one(child, 'scope_handle')
                    if (not isinstance(child_path, str) or not child_path.startswith(path + '.')
                            or parent is None or p.scalar(parent, 'full_name') != path):
                        raise NpiHierarchyUnavailable('npi_scope_parent_mismatch')
                    if child_path in child_paths:
                        gap('npi_scope_alias_or_cycle')
                    else:
                        child_job = new_job(child_path, path, depth + 1)
                        if (len(pending) + len(job['children']) >= MAX_PENDING
                                or frontier_bytes + job_bytes(child_job) > MAX_FRONTIER_BYTES - 65536):
                            if job['children']:
                                return False
                            raise HierarchyBudgetExceeded('native_frontier_limit')
                        job['children'].append(child_job)
                        child_paths.add(child_path)
                        frontier_bytes += job_bytes(child_job)
                index += 1
                job['child_index'] = index
                fingerprint = next_fingerprint
                job['child_fingerprint'] = fingerprint
                if len(job['children']) >= child_batch_size:
                    # Copy a small sibling batch, then visit it before
                    # reopening this parent's child iterator at its cursor.
                    # No iterator or native object survives that safe point.
                    return False
        finally:
            p.close_iterator(it)

    try:
        if not (root == p.top or root.startswith(p.top + '.')):
            gap('npi_scope_outside_selected_top')
            pending.clear()
        else:
            p._require_top()
            while pending:
                budget.checkpoint()
                if work['completed_instances'] >= MAX_CUMULATIVE:
                    raise HierarchyBudgetExceeded('native_total_scope_limit')
                recycle = getattr(p, 'recycle_scan_handles', None)
                live = getattr(p, 'handle_count', 0) - getattr(p, '_released_handle_count', 0)
                if live >= 4096 and recycle is not None:
                    recycle()
                    live = 0
                job = pending[-1]
                before = job_position(job)
                try:
                    complete = visit_job(job) is not False
                except HierarchyBudgetExceeded as exc:
                    after = job_position(job)
                    if exc.reason == 'native_handle_limit' and recycle is not None and (live > 0 or after != before):
                        # Scope/port frames are copied and every native iterator
                        # has closed. Retry only after a real release/advance.
                        recycle()
                        continue
                    raise
                children, job['children'] = job['children'], []
                if complete:
                    pending.pop()
                    frontier_bytes -= job_bytes(job)
                    work['completed_instances'] += 1
                pending.extend(reversed(children))
    except HierarchyBudgetExceeded as exc:
        stop_reason = exc.reason
    if p.degraded:
        gap('npi_degraded_design')
    # Persist semantic observations, not transient page-work stops. A later
    # successful page can finish the same inventory without stale limit gaps.
    work['category_gaps'] = {c: sorted(gaps[c]) for c in categories}
    work['gap_summary'] = diagnostics.result()
    if stop_reason:
        gap(stop_reason)
    final_position = (work['completed_instances'], sum(work['counts'].values()),
                      [job_position(j) for j in pending])
    resumable = bool(pending and initial_position != final_position and stop_reason not in {
        'timeout_sec', 'scope_depth_limit', 'native_frontier_limit', 'native_total_fact_limit', 'native_total_scope_limit'})
    results = {c: {'status': 'partial' if gaps[c] else 'complete', 'gaps': sorted(gaps[c])} for c in categories}
    for c in set(options['categories']) - categories:
        results[c] = {'status': 'unsupported', 'gaps': ['npi_category_unsupported']}
    all_gaps = sorted({g for row in results.values() for g in row['gaps']})
    return dict(status='partial' if all_gaps else 'complete', scope=options.get('scope'),
                artifact_identity=p.artifact_identity, capability_version=CAPABILITY_VERSION,
                categories_checked=sorted(categories), category_results=results, gaps=all_gaps,
                gap_summary=diagnostics.result(),
                facts=facts, total_facts=sum(work['counts'].values()), counts=dict(work['counts']),
                _scan_state=work, scan_progress=dict(inventory='cumulative_counts_current_page_facts',
                    completed_instances=work['completed_instances'], pending_scopes=len(pending),
                    cumulative_counts=dict(work['counts']), page_facts=len(facts),
                    status='available' if resumable else 'blocked' if pending else 'complete',
                    next_scopes=[j['path'] for j in pending[-3:]]),
                instances_visited=budget.scope_count, query_metrics={}, analysis_limits=[], coverage_status='partial' if all_gaps else 'complete',
                limitations=['kdb_source_association_unverified', 'native_positions_not_current_source_validation',
                             *(['static_netlist_drivers_only','runtime_force_dpi_class_writes_not_checked']
                               if driver_categories else ['input_ports_only']), 'local_native_rss_not_hard_isolated'],
                metrics={'native_port_count': ports,
                         **({'native_driver_bit_count':driver_bits} if driver_categories else {}),
                         'native_handle_peak': getattr(p, 'handle_peak', 0),
                         'native_handle_batches': getattr(p, 'handle_batches', 1),
                         'native_traversal_ms': (time.monotonic()-started)*1000})


def query_native_scan(backend, options, *, top, budget, state=None):
    def query(p):
        reader = None
        if set(options['categories']) & DRIVER_CATEGORIES:
            from .npi_structural_drivers import NpiBitDrivers
            reader = NpiBitDrivers(p, backend)
        return scan_native_ports(p, options, state=state, driver_reader=reader)
    result = backend._query_language(query, top=top, budget=budget)
    if result.get('analysis_limits'):
        result['status'] = 'partial'
        for row in result['category_results'].values():
            if row['status'] == 'complete':
                row['status'] = 'partial'
                row['gaps'] = sorted(set(row['gaps']) | set(result['analysis_limits']))
    result['metrics'].update(result.pop('query_metrics'))
    result['metrics']['native_instance_count'] = budget.scope_count
    result['metrics']['native_call_count'] = budget.call_count
    result['kdb_binding'] = dict(backend._bound_kdb_anchor.receipt(), load_status='loaded', load_quality=backend.kdb_load_quality)
    return result
