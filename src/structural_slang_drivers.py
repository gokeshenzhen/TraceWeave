"""Bounded static writer inventory over an already elaborated Slang design.

No waveform reads, frontend construction, query IR or MCP subcalls. Net-port
collapse is modeled per bit; procedural branches share their enclosing writer.
Runtime force/DPI effects are outside the declared static evidence domain.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os

from .cancellation import check_cancelled
from .slang_connectivity_projector import _selected_bits
from .structural_driver_facts import DRIVER_CATEGORIES, driver_fact


class _Limit(Exception):
    pass


@dataclass
class _Signal:
    symbol: object
    bits: tuple[int, ...]
    source: dict | None
    net_kind: str
    excluded: bool = False


class _Inventory:
    def __init__(self, session, scope, categories, limits):
        from pyslang.ast import VisitAction
        self.skip = VisitAction.Skip
        self.session, self.scope, self.categories, self.limits = session, scope, categories, limits
        self.signals, self.parent, self.writers, self.reads, self.blocked = {}, {}, {}, set(), set()
        self.gaps, self.driver_rows, self.facts = set(), {}, []
        self.driver_refs = {}
        self.source_paths = {}
        self.nodes = self.instances = self.bits = self.work = 0
        self.complete = False
        self.unbounded_write = False

    def tick(self):
        check_cancelled()
        self.nodes += 1
        if self.nodes > self.limits.max_ast_nodes:
            raise _Limit('driver_ast_limit')

    def step(self):
        check_cancelled()
        self.work += 1
        if self.work > self.limits.max_propagation_steps:
            raise _Limit('driver_work_limit')

    def location(self, node):
        loc = getattr(getattr(node, 'sourceRange', None), 'start', getattr(node, 'location', None))
        if loc is None:
            return None
        sm = self.session.driver.sourceManager
        filename = str(sm.getFileName(loc))
        if filename not in self.source_paths:
            self.source_paths[filename] = os.path.realpath(filename)
        return dict(file=self.source_paths[filename], line=int(sm.getLineNumber(loc)))

    def signal(self, symbol):
        if type(symbol).__name__ not in {'NetSymbol', 'VariableSymbol'}:
            return None
        if type(symbol).__name__ == 'VariableSymbol' and symbol.lifetime.name == 'Automatic':
            return None
        name = str(symbol.hierarchicalPath)
        if name in self.signals:
            return name
        typ = symbol.type.canonicalType
        if not typ.isIntegral:
            if typ.isUnpackedArray:
                self.gaps.add('driver_unpacked_signal_unsupported')
            return None
        width = int(typ.bitWidth)
        if not 0 < width <= self.limits.max_width:
            self.gaps.add('driver_width_unsupported')
            return None
        rng = typ.getBitVectorRange()
        if abs(rng.left-rng.right)+1 != width:
            self.gaps.add('driver_packed_shape_unsupported')
            return None
        self.bits += width
        if self.bits > self.limits.max_propagation_bits:
            raise _Limit('driver_bit_limit')
        bits = tuple(range(rng.left, rng.right + (-1 if rng.left > rng.right else 1),
                           -1 if rng.left > rng.right else 1))
        kind = symbol.netType.netKind.name if type(symbol).__name__ == 'NetSymbol' else 'variable'
        self.signals[name] = _Signal(symbol, bits, self.location(symbol), kind)
        if type(symbol).__name__ == 'VariableSymbol' and symbol.initializer is not None:
            self.blocked.update((name, b) for b in bits)
        if kind not in {'Wire', 'Tri', 'UWire', 'variable'}:
            self.blocked.update((name, b) for b in bits)
            self.gaps.add('driver_net_resolution_boundary')
        return name

    def targets(self, node, instance, depth=0):
        self.tick()
        if depth > 64:
            raise _Limit('driver_expression_depth_limit')
        typ = type(node).__name__
        if typ in {'NamedValueExpression', 'HierarchicalValueExpression'}:
            name = self.signal(node.symbol)
            return tuple((name, b) for b in self.signals[name].bits) if name else ()
        if typ in {'ElementSelectExpression', 'RangeSelectExpression'}:
            base = self.targets(node.value, instance, depth+1)
            if not base:
                return ()
            selected = _selected_bits(node, tuple(range(len(base)-1, -1, -1)), instance)
            # The helper uses the declared expression range to index flattened
            # MSB-first positions, including ascending/signed/indexed selects.
            return tuple(base[len(base)-1-b] for b in selected)
        if typ == 'ConcatenationExpression':
            parts = [self.targets(n, instance, depth+1) for n in node.operands]
            return tuple(x for p in parts for x in p) if all(parts) else ()
        if typ == 'ConversionExpression':
            return self.targets(node.operand, instance, depth+1)
        return ()

    def root(self, key):
        self.step()
        current = key
        while current in self.parent:
            current = self.parent[current]
        while key in self.parent:
            following = self.parent[key]
            self.parent[key] = current
            key = following
        return current

    def union(self, left, right):
        left, right = self.root(left), self.root(right)
        if left == right:
            return
        self.parent[right] = left
        incoming = self.writers.pop(right, None)
        if incoming is None:
            return
        current = self.writers.get(left)
        if current is None:
            self.writers[left] = incoming
        elif isinstance(current, set):
            current.update(incoming if isinstance(incoming, set) else (incoming,))
        elif isinstance(incoming, set):
            incoming.add(current)
            self.writers[left] = incoming
        elif current != incoming:
            self.writers[left] = {current, incoming}

    def claim(self, targets, writer):
        continuous = self.driver_rows[writer]['kind'] in {'continuous_assignment', 'net_initializer'}
        for position, bit in enumerate(targets):
            # Preserve a writer's original logical bit before net collapse.
            # Two bits of one vector may be wired onto the same physical net;
            # repeated assignments to the SAME bit in one process remain one.
            # Continuous concatenations also retain repeated LHS positions.
            suffix = ':' + str(position) if continuous else ''
            identity = hashlib.sha256((writer + ':' + repr(bit) + suffix).encode()).hexdigest()
            # Keep shared source metadata and a compact bit reference until a
            # fact actually needs it. Most bits have one writer, so allocate a
            # set only when an independent source joins that physical net.
            self.driver_refs[identity] = (writer, bit, len(targets)-1-position if continuous else None)
            root = self.root(bit)
            current = self.writers.get(root)
            if current is None:
                self.writers[root] = identity
            elif isinstance(current, set):
                current.add(identity)
            elif current != identity:
                self.writers[root] = {current, identity}

    def driver_row(self, identity):
        writer, bit, offset = self.driver_refs[identity]
        row = {**self.driver_rows[writer], 'driver_id':identity,
               'target_signal':bit[0], 'target_bits':[bit[1]]}
        if offset is not None:
            row['assignment_lsb_offset'] = offset
        return row

    def block(self, node, instance, reason):
        names = set()
        automatic, other = False, False
        def visit(n):
            nonlocal automatic, other
            self.tick()
            if type(n).__name__ in {'NamedValueExpression', 'HierarchicalValueExpression'}:
                symbol = n.symbol
                if type(symbol).__name__ == 'VariableSymbol' and symbol.lifetime.name == 'Automatic':
                    automatic = True
                    return
                other = True
                name = self.signal(n.symbol)
                if name:
                    names.add(name)
        node.visit(visit)
        # Automatic subroutine state is outside the persistent signal inventory.
        # Its local writes cannot invalidate absence proofs for module signals.
        if automatic and not other:
            return
        self.gaps.add(reason)
        for name in names:
            self.blocked.update((name, b) for b in self.signals[name].bits)
        if not names:
            self.unbounded_write = True

    def writer(self, node, path, kind, suffix=''):
        source = self.location(node)
        identity = path + ':' + kind + ':' + str(source) + ':' + suffix
        key = hashlib.sha256(identity.encode()).hexdigest()
        self.driver_rows[key] = dict(driver_id=key, instance=path, kind=kind, source=source)
        return key

    def walk(self, node, instance, writer=None, initial=False):
        def visit(n):
            self.tick()
            typ = type(n).__name__
            if typ == 'AssignmentExpression':
                targets = self.targets(n.left, instance)
                if targets and writer is not None and not initial:
                    self.claim(targets, writer)
                else:
                    self.block(n.left, instance, 'driver_initial_state_boundary' if initial else 'driver_write_target_unmodeled')
                self.walk(n.right, instance, writer, initial)
                if getattr(n, 'isCompound', False):
                    self.walk(n.left, instance)
                return self.skip
            if typ in {'NamedValueExpression', 'HierarchicalValueExpression',
                       'ElementSelectExpression', 'RangeSelectExpression'}:
                targets = self.targets(n, instance)
                if targets:
                    self.reads.update(targets)
                    return self.skip
            if typ in {'ProceduralAssignStatement', 'ProceduralDeassignStatement',
                       'ForceStatement', 'ReleaseStatement'}:
                self.block(n, instance, 'driver_force_release_boundary')
                return self.skip
            if typ == 'UnaryExpression' and any(word in n.op.name.lower() for word in ('increment', 'decrement')):
                targets = self.targets(n.operand, instance)
                if targets and writer is not None and not initial:
                    self.claim(targets, writer)
                else:
                    self.block(n.operand, instance, 'driver_write_target_unmodeled')
                self.walk(n.operand, instance)
                return self.skip
            if typ == 'CallExpression':
                routine = getattr(n, 'subroutine', None)
                formal = list(getattr(routine, 'arguments', ()))
                actual = list(n.arguments)
                for index, arg in enumerate(actual):
                    if index < len(formal) and getattr(formal[index].direction, 'name', '') != 'In':
                        self.block(arg, instance, 'driver_call_write_boundary')
                    self.walk(arg, instance)
                return self.skip
        node.visit(visit)

    def ports(self, instance):
        path = str(instance.hierarchicalPath)
        top = '.' not in path
        for connection in instance.portConnections:
            self.tick()
            port, actual = connection.port, connection.expression
            if type(port).__name__ != 'PortSymbol':
                self.gaps.add('driver_interface_boundary')
                continue
            name = self.signal(port.internalSymbol)
            if name is None:
                self.gaps.add('driver_port_type_unsupported')
                continue
            signal = self.signals[name]
            internal = tuple((name, b) for b in signal.bits)
            direction = port.direction.name
            if direction in {'InOut', 'Ref'}:
                signal.excluded = True
                self.blocked.update(internal)
                if actual is not None:
                    self.block(actual, instance, 'driver_bidirectional_boundary')
                continue
            if direction == 'In':
                signal.excluded = True
                if actual is None:
                    self.blocked.update(internal)
                    continue
                self.walk(actual, instance)
            if actual is None:
                if top and direction == 'Out':
                    self.reads.update(internal)
                continue
            expression = actual.left if direction == 'Out' and type(actual).__name__ == 'AssignmentExpression' else actual
            external = self.targets(expression, instance)
            collapsed = (signal.net_kind != 'variable' and len(external) == len(internal)
                         and all(self.signals[n].net_kind != 'variable' for n, _ in external))
            if collapsed:
                for a, b in zip(internal, external):
                    self.union(a, b)
            elif direction == 'In':
                self.claim(internal, self.writer(port, path, 'input_binding', str(port.name)))
            elif direction == 'Out':
                self.reads.update(internal)
                if external:
                    if len(external) == len(internal):
                        for target, (_, bit) in zip(external, internal):
                            writer = self.writer(port, path, 'output_binding', f'{port.name}[{bit}]')
                            self.driver_rows[writer].update(signal_path=name,source_bits=[bit])
                            self.claim((target,),writer)
                    else:
                        self.claim(external, self.writer(port, path, 'output_binding', str(port.name)))
                        self.blocked.update(external)
                        self.gaps.add('driver_output_width_conversion_boundary')
                    if signal.net_kind != 'variable':
                        self.blocked.update(internal)
                        self.gaps.add('driver_output_net_conversion_boundary')
                else:
                    self.block(expression, instance, 'driver_output_binding_unmodeled')

    def collect(self):
        from .structural_semantics import _members
        pending = list(reversed(tuple(self.session.root.topInstances)))
        while pending:
            check_cancelled()
            instance = pending.pop()
            self.instances += 1
            if self.instances > self.limits.max_instances:
                raise _Limit('driver_instance_limit')
            path = str(instance.hierarchicalPath)
            interface = instance.definition.definitionKind.name == 'Interface'
            for index, member in enumerate(_members(instance.body)):
                self.tick()
                typ = type(member).__name__
                if typ == 'InstanceSymbol':
                    pending.append(member)
                elif typ == 'PortSymbol' and '.' not in path:
                    name = self.signal(member.internalSymbol)
                    if name:
                        targets = tuple((name, b) for b in self.signals[name].bits)
                        direction = member.direction.name
                        if direction == 'In':
                            self.signals[name].excluded = True
                            self.claim(targets, self.writer(member, path, 'primary_input', str(member.name)))
                        elif direction == 'Out':
                            self.reads.update(targets)
                        else:
                            self.signals[name].excluded = True
                            self.blocked.update(targets)
                elif typ in {'NetSymbol', 'VariableSymbol'}:
                    name = self.signal(member)
                    if name and interface:
                        self.blocked.update((name, b) for b in self.signals[name].bits)
                        self.gaps.add('driver_interface_runtime_boundary')
                    if name and member.initializer is not None:
                        targets = tuple((name, b) for b in self.signals[name].bits)
                        self.walk(member.initializer, instance)
                        if typ == 'NetSymbol':
                            self.claim(targets, self.writer(member, path, 'net_initializer'))
                        else:
                            self.blocked.update(targets)
                elif typ in {'ContinuousAssignSymbol', 'ProceduralBlockSymbol'}:
                    node = member.assignment if typ == 'ContinuousAssignSymbol' else member.body
                    kind = 'continuous_assignment' if typ == 'ContinuousAssignSymbol' else 'procedural_process'
                    initial = typ == 'ProceduralBlockSymbol' and member.procedureKind.name == 'Initial'
                    self.walk(node, instance, self.writer(member, path, kind, str(index)), initial)
                elif typ == 'SubroutineSymbol':
                    # A subroutine may write module state directly. Call-site
                    # process ownership is not yet modeled, so retain a write
                    # boundary rather than proving its targets undriven.
                    self.walk(member.body, instance)
                elif typ in {'NetAliasSymbol', 'PrimitiveInstanceSymbol', 'CheckerInstanceSymbol'}:
                    self.gaps.add('driver_static_construct_unmodeled')
                    self.unbounded_write = True
            self.ports(instance)
        self.complete = not self.unbounded_write and not self.session.diagnostic_payload.get('blocking_error_count')
        if not self.complete:
            self.gaps.add('driver_inventory_incomplete')

    def run(self):
        try:
            self.collect()
        except _Limit as exc:
            self.gaps.add(str(exc))
        # Normalize after every net-port union; the same physical net has one
        # driver set even when its declaration appears on both sides of a port.
        try:
            reads = {self.root(b) for b in self.reads}
            blocked = {self.root(b) for b in self.blocked}
            for name, signal in self.signals.items():
                if self.scope and not name.startswith(self.scope + '.'):
                    continue
                groups = {}
                for bit in signal.bits:
                    root = self.root((name, bit))
                    known = self.writers.get(root, ())
                    writers = (known,) if isinstance(known, str) else known
                    complete = self.complete and root not in blocked
                    if root in blocked and root in reads and not signal.excluded:
                        self.gaps.add('driver_bits_unresolved')
                    kind = ('multiple_driver' if len(writers) >= 2 else
                            'undriven_signal' if not writers and complete and root in reads
                            and not signal.excluded else None)
                    if kind not in self.categories:
                        continue
                    key = (kind, tuple(sorted(writers)), complete)
                    groups.setdefault(key, []).append(bit)
                for (kind, writers, complete), bits in groups.items():
                    if len(self.facts) >= self.limits.max_facts:
                        raise _Limit('driver_fact_limit')
                    self.facts.append(driver_fact(kind, name, bits,
                        [self.driver_row(w) for w in writers], complete=complete,
                        source=signal.source, net_kind=signal.net_kind))
        except _Limit as exc:
            self.gaps.add(str(exc))
        return dict(facts=self.facts, gaps=sorted(self.gaps),
                    inventory_complete=self.complete, instances=self.instances,
                    signal_count=len(self.signals), signal_bits=min(self.bits, self.limits.max_propagation_bits),
                    ast_nodes=self.nodes, work_steps=self.work)


def scan_slang_drivers(session, *, scope, categories, limits):
    return _Inventory(session, scope, set(categories) & DRIVER_CATEGORIES, limits).run()
