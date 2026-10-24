#!/usr/bin/env python3
"""One isolated Slang request. Never executes HDL, Tcl, EDA or user functions."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.property_records import explanation_record, blocked, gap
from src.property_semantics import Projector, ProjectionLimit, kind, spelling


def _diagnostics(compilation, trees, manager):
    import pyslang
    from pyslang import analysis
    engine = pyslang.DiagnosticEngine(manager)
    parse = [d for tree in trees for d in tree.diagnostics]
    all_diags = list(compilation.getAllDiagnostics())
    # Compilation binding alone does not validate assertion clock flow,
    # multiclock operator legality or all local-variable flow restrictions.
    # This native analysis is still inside the killable, RSS-limited worker.
    options=analysis.AnalysisOptions()
    options.maxLoopAnalysisSteps=1024
    options.maxCaseAnalysisSteps=1024
    analyzer=analysis.AnalysisManager(options)
    analyzer.analyze(compilation)
    all_diags.extend(analyzer.getDiagnostics())
    # Preserve error evidence before trimming warning details.
    all_diags.sort(key=lambda d:not d.isError())
    rows = []
    parse_keys = {(str(d.code), int(d.location.offset), str(d.location.buffer)) for d in parse}
    for d in all_diags[:128]:
        loc = manager.getFullyOriginalLoc(d.location)
        rows.append(dict(code=str(d.code).removeprefix('DiagCode(').removesuffix(')'),
                         severity='error' if d.isError() else 'warning', message=engine.formatMessage(d),
                         file=str(manager.getFileName(loc)), line=int(manager.getLineNumber(loc)),
                         column=int(manager.getColumnNumber(loc)),
                         syntax=(str(d.code),int(d.location.offset),str(d.location.buffer)) in parse_keys))
    return rows, any(d.isError() for d in parse), len(all_diags)>128


def _text_source(item, unit):
    from pyslang import syntax
    text = item['text']
    clean = re.sub(r'/\*.*?\*/|//[^\n]*', '', text, flags=re.S).strip()
    if re.match(r'(?:module|interface|package|program)\b', clean):
        return text, 'source_text', None
    prefix = 'module __tw_property;\n' + '`line 1 "shared-unit" 0\n' + (unit or {}).get('declarations','') + '\n'
    if (unit or {}).get('default_clock'):
        prefix += 'default clocking @(' + unit['default_clock'] + '); endclocking\n'
    if (unit or {}).get('default_disable'):
        prefix += 'default disable iff (' + unit['default_disable'] + ');\n'
    prefix += '`line 1 "input" 0\n'
    if re.match(r'(?:property|sequence)\b', clean):
        parsed = syntax.SyntaxTree.fromText(text)
        decl = parsed.root
        if kind(decl) in {'PropertyDeclaration','SequenceDeclaration'}:
            name = str(decl.name.valueText)
            invocation = name + ('(' + ','.join(item.get('arguments',[])) + ')' if item.get('arguments') else '')
            return prefix + text + '\n`line 1 "binding-probe" 0\n__tw_target: assert property ('+invocation+');\nendmodule', 'definition', '__tw_target'
        return prefix+text+'\nendmodule', 'definition', None
    if re.match(r'(?:(?:[A-Za-z_$][\w$]*|\\\S+)\s*:\s*)?(?:assert|assume|cover|expect)\b',clean):
        # expect and immediate assertions are procedural constructs.
        procedural = bool(re.match(r'(?:\w+\s*:\s*)?(?:expect\b|(?:assert|assume|cover)\s*(?:#0|final|\())',clean))
        return prefix + ('initial begin\n' if procedural else '') + text + ('\nend' if procedural else '') + '\nendmodule', 'directive', None
    return prefix + '__tw_target: assert property (\n`line 1 "input" 0\n'+text+');\nendmodule', 'expression', '__tw_target'


def _collect(root, projector, *, collect_controls=True):
    from pyslang import ast
    candidates = []
    def visit(n):
        projector.charge()
        if isinstance(n,ast.GenerateBlockSymbol) and n.isUninstantiated:
            return ast.VisitAction.Skip
        if isinstance(n,(ast.PropertySymbol,ast.SequenceSymbol)):
            candidates.append(dict(kind='definition', path=str(n.hierarchicalPath), name=n.name,
                                   source=projector.source(n), obj=n, scope=n.parentScope))
            return ast.VisitAction.Skip
        if isinstance(n,ast.ProceduralBlockSymbol):
            def statements(s):
                projector.charge()
                if isinstance(s,(ast.ConcurrentAssertionStatement,ast.ImmediateAssertionStatement)):
                    label = getattr(s.syntax,'label',None)
                    name = str(label.name.valueText) if label is not None else ''
                    path = str(n.hierarchicalPath)
                    # Nested named blocks are part of the selected activation path.
                    parents=[]; syn=s.syntax.parent
                    while syn and kind(syn) not in {'ModuleDeclaration','GenerateBlock','InitialBlock','AlwaysBlock','ConcurrentAssertionMember'}:
                        bn=getattr(syn,'blockName',None)
                        if bn: parents.append(str(bn.name.valueText))
                        syn=syn.parent
                    path='.'.join([path,*reversed(parents),name] if name else [path,*reversed(parents)])
                    candidates.append(dict(kind='directive',path=path,name=name,source=projector.source(s),obj=s,scope=n.parentScope,procedure=n))
                    return ast.VisitAction.Skip
                if collect_controls and isinstance(s,ast.CallExpression) and str(s.subroutineName).startswith('$assert'):
                    projector.effects(s,'procedural_assertion_control',
                        'when_enclosing_procedure_reaches_call; execution_and_enabled_state_not_observed',None)
                # Assertions are statements. Ordinary expression trees cannot
                # contain another assertion site; bound property/value trees
                # are projected only after the selected site is resolved.
                if isinstance(s,ast.Expression):return ast.VisitAction.Skip
            n.body.visit(statements)
            return ast.VisitAction.Skip
        # Procedure bodies were handled above. Do not walk unrelated task/
        # function statement trees or variable initializer expressions while
        # discovering declaration and directive symbols across the hierarchy.
        if isinstance(n,(ast.Statement,ast.Expression)):
            return ast.VisitAction.Skip
    root.visit(visit)
    return candidates


def _defaults(candidate, projector):
    from pyslang import ast
    scope = candidate['scope']
    clock = None; disable = None
    while scope is not None:
        try:
            members=list(scope)
        except TypeError:
            break
        for member in members:
            projector.charge()
            if isinstance(member,ast.ClockingBlockSymbol) and str(member.syntax.globalOrDefault).strip()=='default':
                clock=clock or projector.clock(member.event, 'scope_default')
        parent=getattr(scope,'parentScope',None)
        if parent is scope: break
        scope=parent
    syntax=candidate['obj'].syntax
    while syntax is not None:
        for member in getattr(syntax,'members',[]):
            if kind(member)=='DefaultDisableDeclaration' and disable is None:
                disable={'text':str(member.expr).strip(), 'source':projector.source(member),
                         '_syntax':member.expr,
                         'origin':'scope_default', 'binding':'frontend_checked',
                         'semantics':'asynchronous_unsampled; abort_neither_pass_nor_fail'}
        syntax=syntax.parent
    if clock is None and candidate.get('procedure'):
        target_range=candidate['obj'].sourceRange
        enclosing=[]
        def visit(n):
            projector.charge()
            if isinstance(n,ast.TimedStatement) and kind(n.timing)=='SignalEvent':
                sr=n.sourceRange
                if (sr.start.buffer==target_range.start.buffer
                        and int(sr.start.offset)<=int(target_range.start.offset)
                        and int(sr.end.offset)>=int(target_range.end.offset)):
                    enclosing.append(n)
            if isinstance(n,ast.ConcurrentAssertionStatement):return ast.VisitAction.Skip
        candidate['procedure'].body.visit(visit)
        if enclosing:
            nearest=min(enclosing,key=lambda n:int(n.sourceRange.end.offset)-int(n.sourceRange.start.offset))
            clock=projector.clock(nearest.timing,'procedural_event_inference')
    return clock,disable


def _qualified_condition(expr,scope,projector):
    """Qualify resolved lexical names for a synthetic default-disable probe."""
    sr=expr.sourceRange; start=int(sr.start.offset)
    text=projector.manager.getSourceText(sr.start.buffer).encode()[start:int(sr.end.offset)]
    edits=[]
    def visit(n):
        projector.charge()
        if kind(n)=='IdentifierName':
            name=str(n.identifier.valueText)
            if name.startswith('$'):return
            symbol=scope.lookupName(name)
            if symbol is None:raise ValueError('default_disable_binding_unresolved')
            r=n.sourceRange
            if r.start.buffer!=sr.start.buffer:raise ValueError('default_disable_macro_context')
            edits.append((int(r.start.offset)-start,int(r.end.offset)-start,str(symbol.hierarchicalPath).encode()))
    expr.visit(visit)
    for a,b,value in sorted(set(edits),reverse=True):text=text[:a]+value+text[b:]
    return text.decode()


_RUNTIME_REPETITION = frozenset({'ForLoopStatement','LoopStatement','WhileLoopStatement','RepeatLoopStatement',
    'DoWhileStatement','ForeachLoopStatement','ForeverStatement','RandSequenceStatement'})


def _activation_schedule(row):
    """Source-site encounter bounds, never observed runtime attempt counts."""
    schedule=dict(scope='one_elaborated_assertion_site', actual_attempt_count='not_observed',
                  first_start_time='not_observed')
    k=row['kind']
    if k=='none_definition_or_fragment':
        schedule.update(policy='caller_activation_required',max_site_encounters_per_process_start=None,
                        fresh_attempt_requires='caller_supplied_activation',basis='definition_or_fragment_only')
    elif k=='concurrent_declaration':
        schedule.update(policy='each_leading_clock_event',max_site_encounters_per_process_start=None,
                        fresh_attempt_requires='leading_clock_event_subject_to_controls',basis='concurrent_declaration')
    elif k=='procedural':
        repeated=any(c['kind'] in _RUNTIME_REPETITION for c in row['controls'])
        once=row['procedure']=='InitialBlock' and not repeated
        schedule.update(policy='at_most_one_initial_encounter' if once else 'per_procedural_encounter',
                        max_site_encounters_per_process_start=1 if once else None,
                        fresh_attempt_requires='another_procedural_encounter',
                        basis='initial_without_enclosing_runtime_repetition' if once else
                              ('enclosing_runtime_repetition' if repeated else 'repeating_or_other_procedure'),
                        zero_encounters_possible=True,
                        clock_progression='clock_ticks_advance_an_existing_attempt; do_not_invent_new_procedural_encounters')
    else:
        schedule.update(policy='unresolved',max_site_encounters_per_process_start=None,
                        fresh_attempt_requires='unknown',basis='source_activation_unresolved')
    row['schedule']=schedule
    return row


def _activation(statement):
    syntax=statement.syntax.parent
    ancestors=[]
    while syntax is not None:
        k=kind(syntax)
        if k=='LoopStatement':
            keyword=str(syntax.repeatOrWhile.valueText)
            k={'while':'WhileLoopStatement','repeat':'RepeatLoopStatement'}.get(keyword,k)
        if k in _RUNTIME_REPETITION or k in {'ConditionalStatement','CaseStatement','TimingControlStatement',
                                             'WaitStatement','WaitOrderStatement','RandCaseStatement'}:
            ancestors.append({'kind':k,'source':str(syntax)[:2048]})
        if k=='ConcurrentAssertionMember':
            return _activation_schedule(dict(kind='concurrent_declaration', trigger='each_leading_clock_event', controls=ancestors))
        if k in {'InitialBlock','AlwaysBlock','FinalBlock'}:
            return _activation_schedule(dict(kind='procedural', procedure=k, trigger='when_control_reaches_statement; concurrent_assertion_activation_uses_procedural_rules', controls=ancestors))
        syntax=syntax.parent
    return _activation_schedule(dict(kind='unresolved', controls=ancestors))


def _syntax_fallback(tree, projector):
    from pyslang import syntax
    temporal={'PropertySpec','SimpleSequenceExpr','BinarySequenceExpr','BinaryPropertyExpr','SequenceConcatExpr',
              'DelayedSequenceExpr','RepeatedSequenceExpr','FirstMatchSequenceExpr','ClockingPropertyExpr',
              'ConditionalPropertyExpr','UnaryPropertyExpr','StrongWeakPropertyExpr','ParenthesizedSequenceExpr'}
    def visit(n):
        projector.charge()
        if isinstance(n,syntax.SyntaxNode) and kind(n) in temporal:
            out=projector.add(n,kind(n),'unresolved',None,{'binding':'syntax_only'},
                              'Syntax retained; unresolved declarations or diagnostics prevent a confirmed semantic interpretation.')
            if projector.result['root'] is None: projector.result['root']=out['id']
    tree.root.visit(visit)


def explain(payload):
    from pyslang import ast,syntax,driver
    import pyslang
    item=payload['item']; started=time.monotonic()
    result=explanation_record(item['id'])
    result['identity']=payload.get('identity',{})
    projector=None
    try:
        input_kind='source_file'; synthetic=None
        if item.get('text') is not None:
            source,input_kind,synthetic=_text_source(item,payload.get('unit'))
            tree=syntax.SyntaxTree.fromText(source, name='property-input')
            trees=[tree]; manager=tree.sourceManager
            compilation=ast.Compilation(); compilation.addSyntaxTree(tree)
        else:
            from src.source_graph_worker import _configure_driver
            frontend=_configure_driver(driver,payload['frontend_args'])
            if not frontend.parseAllSources():
                # Driver parser can still expose trees and useful syntax errors.
                pass
            compilation=frontend.createCompilation(); manager=frontend.sourceManager
            trees=list(frontend.syntaxTrees)
        root=compilation.getRoot()
        projector=Projector(manager,result,payload['limits'],deadline=started+payload['timeout_sec'])
        projector.execution_requested=bool(payload.get('execution_plan'))
        diagnostics,syntax_bad,diag_limited=_diagnostics(compilation,trees,manager)
        result['diagnostics']=diagnostics
        result['syntax_status']='invalid' if syntax_bad else 'valid'
        if diag_limited:
            result['diagnostics_truncated']=True
            result['output_truncated']=True
        candidates=_collect(root,projector)
        selected=[]
        for c in candidates:
            if synthetic and c['name']!=synthetic: continue
            if not synthetic and item.get('target') and item['target'] not in {c['name'],c['path']}: continue
            if item.get('instance_path') and not (c['path']==item['instance_path'] or c['path'].startswith(item['instance_path']+'.')): continue
            if item.get('source_file') and Path(c['source'].get('file','')).resolve()!=Path(item['source_file']).resolve(): continue
            if item.get('line'):
                first=c['source'].get('line',0); last=first+spelling(c['obj']).count('\n')
                if not first<=item['line']<=last: continue
            selected.append(c)
        if len(selected)!=1:
            result['candidates']=[{k:c[k] for k in ('kind','path','name','source')} for c in selected[:64]]
            projector.missing('ambiguous_target' if selected else 'target_not_found','context',
                'Select exactly one declaration or directive by target, instance_path and/or source line.',None,
                'Use an exact candidate path or source line; declarations and directives are separate targets.')
            if not selected and trees: _syntax_fallback(trees[-1],projector)
        else:
            target=selected[0]
            result['target']={k:target[k] for k in ('kind','path','name','source')}
            if synthetic and input_kind=='definition':
                definitions=[c for c in candidates if c['kind']=='definition' and c['source'].get('file')=='input']
                if len(definitions)==1:
                    result['target']={k:definitions[0][k] for k in ('kind','path','name','source')}
            elif input_kind=='expression':
                result['target']['kind']='expression'
            default_clock,default_disable=_defaults(target,projector)
            disable_syntax=default_disable.pop('_syntax',None) if default_disable else None
            obj=target['obj']
            if target['kind']=='definition':
                # A separate synthetic caller binds the existing declaration; it is
                # never reported as an activated assertion in the original design.
                invocation=target['path']+('('+','.join(item.get('arguments',[]))+')' if item.get('arguments') else '')
                event='@('+projector.qualified_event(default_clock)+') ' if default_clock else ''
                scales=[str(t.body.timeScale) for t in root.topInstances if t.body.timeScale is not None]
                scale='timeunit '+scales[0].replace(' / ','; timeprecision ')+'; ' if scales else ''
                probe=syntax.SyntaxTree.fromText('module __tw_binding_probe; '+scale+'__tw_target: assert property ('+event+invocation+'); endmodule',
                                                manager,name='binding-probe')
                import pyslang
                opts=compilation.options
                probe_tops=[*(opts.topModules or [t.name for t in root.topInstances]),'__tw_binding_probe']
                opts.topModules=set(probe_tops)
                recomp=ast.Compilation(pyslang.Bag([opts]))
                for tree in trees: recomp.addSyntaxTree(tree)
                recomp.addSyntaxTree(probe)
                reroot=recomp.getRoot()
                fresh=_collect(reroot,projector,collect_controls=False)
                matches=[c for c in fresh if c['path']=='__tw_binding_probe.__tw_target']
                if not matches:
                    raise ValueError('definition_binding_failed')
                obj=matches[0]['obj']; input_kind='definition'
                diagnostics,syntax_bad,diag_limited=_diagnostics(recomp,[*trees,probe],manager)
                result['diagnostics']=diagnostics
                result['diagnostics_truncated']=diag_limited
                result['output_truncated']|=diag_limited
            directive='definition' if input_kind=='definition' else ('expression' if input_kind=='expression' else obj.assertionKind.name)
            result['directive']=dict(kind=directive, activation=(_activation_schedule({'kind':'none_definition_or_fragment'})
                                      if directive in {'definition','expression'} else _activation(obj)),
                                      runtime_enabled='not_observed', default_disable=default_disable)
            if isinstance(obj,ast.ImmediateAssertionStatement):
                result['directive']['default_disable']=None
                result['directive']['form']='immediate'
                from src.property_completion import completion_policy
                result['directive']['completion_policy']=completion_policy(result['directive'])
                result['directive']['phase']='postponed' if obj.isFinal else ('observed_deferred' if obj.isDeferred else 'procedural_statement')
                n=projector.add(obj,'immediate','value',None,{'expression':projector.value(obj.cond)},
                                'Check this value when procedural control reaches the assertion; this is not a concurrent temporal property.')
            else:
                result['directive']['form']='expect' if obj.assertionKind.name=='Expect' else 'concurrent'
                from src.property_completion import completion_policy
                result['directive']['completion_policy']=completion_policy(result['directive'])
                if obj.assertionKind.name=='Expect':
                    result['directive']['activation']['blocking']='procedure_waits_for_pass_or_fail; no_implicit_repeated_attempts'
                n=projector.project(obj.propertySpec,'sequence' if obj.assertionKind.name=='CoverSequence' else 'property',default_clock)
                if default_disable:
                    default_disable['applied']=not any(x['op']=='disable_iff' for x in result['nodes'])
                    if default_disable['applied']:
                        # Slang's public assertion AST omits inherited default
                        # disables. Bind just that expression in a synthetic
                        # caller under the exact elaboration options, preserving
                        # the original source as its provenance.
                        import pyslang
                        condition=_qualified_condition(disable_syntax,target['scope'],projector)
                        ck=default_clock or (result['clocks'][0]['id'] if result['clocks'] else None)
                        event='@('+projector.qualified_event(ck)+') ' if ck else ''
                        scales=[str(t.body.timeScale) for t in root.topInstances if t.body.timeScale is not None]
                        scale='timeunit '+scales[0].replace(' / ','; timeprecision ')+'; ' if scales else ''
                        disable_probe=syntax.SyntaxTree.fromText('module __tw_disable_probe; '+scale+
                            '__tw_disable: assert property ('+event+'disable iff('+condition+') 1); endmodule',manager,name='default-disable-probe')
                        disable_opts=compilation.options
                        disable_tops=[*(disable_opts.topModules or [t.name for t in root.topInstances]),'__tw_disable_probe']
                        disable_opts.topModules=set(disable_tops)
                        disable_comp=ast.Compilation(pyslang.Bag([disable_opts]))
                        for tree in trees:disable_comp.addSyntaxTree(tree)
                        disable_comp.addSyntaxTree(disable_probe)
                        disabled_root=disable_comp.getRoot()
                        found=_collect(disabled_root,projector,collect_controls=False)
                        stmt=next(c['obj'] for c in found if c['path']=='__tw_disable_probe.__tw_disable')
                        bound=stmt.propertySpec
                        while kind(bound)=='Clocking':bound=bound.expr
                        if kind(bound)=='DisableIff':
                            default_disable['condition']=projector.value(bound.condition,ck)
                            default_disable['sampling']='asynchronous_unsampled; sampled_functions_keep_their_own_sampling'
                        else:
                            projector.missing('default_disable_binding_unresolved','context','Inherited disable expression could not be bound.',disable_syntax)
                if default_clock is None and not result['clocks']:
                    projector.missing('clock_unresolved','context','No explicit or scope-default leading clock was recovered.',obj,
                                      'Supply a default clock in the shared unit or inspect the procedural activation event.')
            result['root']=n['id']
            projector.check_local_flow(n['id'])
            if directive not in {'definition','expression'}:
                phase='procedural_action' if isinstance(obj,ast.ImmediateAssertionStatement) else 'reactive_action'
                projector.effects(obj.ifTrue,phase,'on_success',default_clock)
                projector.effects(obj.ifFalse,phase,'on_failure',default_clock)
            errors=[d for d in result['diagnostics'] if d['severity']=='error']
            for d in errors[:32]:
                missing_context=d['code'] in {'UndeclaredIdentifier','UnknownClassOrPackage','UnknownPackage','UnknownModule','UnknownMember','UnknownMacro','CouldNotOpenIncludeFile','NoGlobalClocking','AssertionNoClock','NotAValue','UnknownSystemName'}
                result['gaps'].append(gap(d['code'],'syntax' if d['syntax'] else ('context' if missing_context else 'syntax'),
                    d['message'],'Supply missing declarations/definitions or correct the reported language error.',d))
            result['syntax_status']='invalid' if syntax_bad else 'valid'
            result['context_status']='incomplete' if errors or any(g['category']=='context' for g in result['gaps']) else 'bound'
        result['variables']=list(projector.variables.values())
        if result['syntax_status']=='invalid' and not any(g['category']=='syntax' for g in result['gaps']):
            for d in result['diagnostics']:
                if d['syntax'] and d['severity']=='error':
                    result['gaps'].append(gap(d['code'],'syntax',d['message'],'Correct the reported syntax before treating it as a legal unsupported construct.',d))
    except ProjectionLimit as exc:
        result['analysis_limited']=True
        result['gaps'].append(gap(str(exc),'limit','Property projection stopped at a resource limit.',
                                 'Select a smaller target or raise the bounded node/depth/time allowance.'))
    result['node_count']=len(result['nodes'])
    result['semantic_status']='partial' if result['gaps'] else 'complete'
    if not result['nodes']: result['semantic_status']='not_run'
    result['summary']=projector.summary()
    if result['gaps']:
        result['summary'].append('Gaps remain: '+', '.join(dict.fromkeys(g['code'] for g in result['gaps'])))
    if result['diagnostics_truncated']:
        result['summary'].append('Frontend diagnostic display is truncated; errors precede warnings.')
    return result


def main():
    payload=json.load(sys.stdin)
    try:
        import pyslang
        if pyslang.__version__ != payload['frontend_version']:
            result=blocked(payload['item'],'frontend_version_mismatch','frontend','Configured and installed frontend versions differ.',
                           'Use the configured optional Source Graph interpreter/version.')
        else:
            result=explain(payload)
    except ImportError:
        result=blocked(payload['item'],'frontend_unavailable','frontend','Optional pyslang frontend is unavailable.',
                       'Use an existing interpreter with the configured pyslang version; no dependency was installed. Continue source reasoning as labelled inference.')
    except Exception as exc:
        result=blocked(payload['item'],'frontend_failed','frontend','Frontend could not complete: '+type(exc).__name__,
                       'Inspect the supplied source/context and retry a smaller self-contained item.')
    print(json.dumps(result,separators=(',',':')))


if __name__=='__main__':
    main()
