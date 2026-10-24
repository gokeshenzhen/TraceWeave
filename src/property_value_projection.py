"""Optional typed value lowering for property plans, inside the Slang worker.

Reuse the elaborated value projector. Sampled functions become explicit history
inputs, never strings to be reparsed or stateless waveform expressions.
"""
from dataclasses import asdict
from types import SimpleNamespace

from .connectivity_ir import SignalSelection
from .dynamic_evidence import Expr
from .property_history import sampled_default
from .slang_dynamic import expression

HISTORY_FUNCTIONS = frozenset({'$past','$rose','$fell','$stable','$changed','$sampled'})


class ValueAdapter:
    def _template_symbol_path(self, node, record, aliases):
        return str(node.symbol.hierarchicalPath)

    def _template_selection(self, node, record, aliases):
        if node.kind.name not in {'NamedValue','HierarchicalValue'}:
            return None
        # Parameters and enum constants must be folded rather than dumped.
        if node.symbol.kind.name not in {'Variable','Net','FormalArgument','LocalAssertionVar'}:
            return None
        typ=node.type.canonicalType
        width=int(typ.bitWidth)
        bounds=typ.fixedRange if typ.hasFixedRange and int(typ.fixedRange.width)==width else None
        left,right=(int(bounds.left),int(bounds.right)) if bounds is not None else (width-1,0)
        step=-1 if left>=right else 1
        return SignalSelection(symbol=str(node.symbol.hierarchicalPath),bits=tuple(range(left,right+step,step)))

    def _template_expression_selections(self, node, record, aliases):
        return ()


def project_value(projector, node, clock):
    anchor=None
    def find(n):
        nonlocal anchor
        projector.charge()
        if n.kind.name in {'NamedValue','HierarchicalValue'}:
            anchor=n.symbol
    node.visit(find)
    timing=projector.native_clocks.get(clock)
    if anchor is None and timing is not None and timing.kind.name=='SignalEvent':
        if timing.expr.kind.name in {'NamedValue','HierarchicalValue'}:
            anchor=timing.expr.symbol
    histories={}

    def special(n, visit, width, signed, two_state):
        projector.charge()
        if n.kind.name!='Call' or str(n.subroutineName) not in HISTORY_FUNCTIONS:
            return None
        name=str(n.subroutineName); args=list(n.arguments)
        key=f'@history:{projector.history_sequence}'
        projector.history_sequence+=1
        row=dict(function=name,clock=clock,expression=asdict(visit(args[0])),
                 default=sampled_default(projector,args[0],clock),ticks=1,gate=None)
        if any(a.kind.name=='ClockingEvent' for a in args):
            row['gap']='sampled_explicit_clock_unavailable'
        if name=='$past':
            from .slang_connectivity_projector import _constant_int
            ticks=_constant_int(args[1],anchor) if len(args)>1 and args[1].kind.name!='EmptyArgument' else 1
            if ticks is None or not 1<=ticks<=4096:
                row['gap']='sampled_history_depth_unavailable'
            else:
                row['ticks']=ticks
            if len(args)>2 and args[2].kind.name not in {'EmptyArgument','ClockingEvent'}:
                row['gate']=asdict(visit(args[2]))
        histories[key]=row
        return Expr('signal',width,signal=key,bits=tuple(range(width-1,-1,-1)),signed=signed,two_state=two_state)

    ir=expression(ValueAdapter(),node,SimpleNamespace(symbol=anchor),{},special=special)
    return dict(expression=asdict(ir),histories=histories)
