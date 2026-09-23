"""Request-local expression observations over existing waveform readers.

No lock or native handle is owned here. Event ordering uses complete physical
time groups, and every resident cursor closes before its FSDB group unloads.
"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
from dataclasses import replace

from .cancellation import check_cancelled
from .event_pages import Event, EventPage, GroupCursor, IncompleteGroup, limits
from .expression_binding import bind_expression
from .expression_values import Value
from .waveform_selection import SelectionParser, MAX_PROJECTED_CELLS, MAX_PROJECTED_BITS

MAX_EVENTS = 262144
MAX_BYTES = 64 * 1024 * 1024
MAX_OBSERVATIONS = 128


class ReadBudget:
    def __init__(self):
        self.events = self.nbytes = 0

    def consume(self, page):
        check_cancelled()
        self.events += len(page.events) + bool(page.predecessor)
        self.nbytes += page.output_bytes
        if self.events > MAX_EVENTS or self.nbytes > MAX_BYTES:
            raise IncompleteGroup('expression_observation_limit')


class ExpressionEventReader:
    mode = 'expression_time_groups'
    native = False

    def __init__(self, bound, readers, start, end_fs, max_events, max_bytes, observe, budget=None):
        limits(max_events,max_bytes)
        self.bound, self.width, self.end_fs = bound,bound.typed.type.width,end_fs
        self.max_events, self.max_bytes = max_events,max_bytes
        self.observe, self.budget = observe,budget or ReadBudget()
        self.paths = bound.paths
        self.cursors, self.states = [], {}
        self.previous = None
        self.predecessor = None
        self.first, self.failed = True, False
        self.failure = None
        self.pending_constant = not self.paths and start == 0
        try:
            self.cursors = [GroupCursor(r,consume=self.budget.consume) for r in readers]
            self.states = {p:c.predecessor.value if c.predecessor else None
                           for p,c in zip(self.paths,self.cursors)}
            initial = self.bound.evaluate(self.states.get)
            self.previous = initial.value
            anchors = [c.predecessor.time_fs for c in self.cursors if c.predecessor]
            if start > 0 and (anchors or not self.paths):
                at = max(anchors,default=0)
                self.predecessor = Event(at,(at+999)//1000,initial.value)
                self.observe(initial,at,'dependency_anchor')
        except IncompleteGroup as exc:
            self.failed, self.failure = True,str(exc)

    def next_time(self):
        return min((c.peek() for c in self.cursors if c.peek() is not None),default=None)

    def read_page(self):
        check_cancelled()
        predecessor = self.predecessor if self.first else None
        self.first = False
        rows = []
        nbytes = 32 + self.width if predecessor else 0
        if nbytes > self.max_bytes:
            self.failed, self.failure = True,'expression_page_limit'
            predecessor = None
        while not self.failed and len(rows) + bool(predecessor) < self.max_events:
            check_cancelled()
            at = 0 if self.pending_constant else self.next_time()
            if at is None:
                if any(c.page.truncated or not c.page.complete for c in self.cursors):
                    self.failed, self.failure = True,'transition_data_truncated'
                break
            if 32 + self.width > self.max_bytes - nbytes:
                if not rows and predecessor is None:
                    self.failed, self.failure = True,'expression_page_limit'
                break
            try:
                for path,cursor in zip(self.paths,self.cursors):
                    if cursor.peek() == at:
                        self.states[path] = cursor.take_group(at)
                        if cursor.peek() is None and cursor.page.truncated:
                            raise IncompleteGroup('transition_data_truncated')
                result = self.bound.evaluate(self.states.get)
                self.observe(result,at,'after')
                if result.value != self.previous or self.pending_constant or (at == 0 and not rows):
                    rows.append(Event(at,(at+999)//1000,result.value))
                    nbytes += 32 + self.width
                    self.previous = result.value
                self.pending_constant = False
            except IncompleteGroup as exc:
                self.failed, self.failure = True,str(exc)
        upcoming = 0 if self.pending_constant else self.next_time()
        complete = not self.failed and upcoming is None and all(c.page.complete for c in self.cursors)
        return EventPage(tuple(rows),predecessor,upcoming,complete,self.failed,nbytes)


class StoredReader:
    mode = 'bounded_expression_dependency'
    native = False

    def __init__(self, pages, width, end_fs):
        self.pages, self.width, self.end_fs = iter(pages),width,end_fs

    def read_page(self):
        return next(self.pages,EventPage((),complete=True))


class ExpressionParser(SelectionParser):
    def __init__(self, parser):
        super().__init__(parser)
        self.expressions = {}
        self.observations = {}
        # Type/source identity belongs to this request; do not reuse a derived
        # clock across bindings or semantic snapshots via the backing token.
        self._clock_cache_token = None

    def bind(self, spec):
        if not isinstance(spec, dict) or 'expr' not in spec:
            return super().bind(spec)
        bound = bind_expression(self.parser,spec)
        if bound.key not in self.expressions:
            if len(self.expressions) >= 128:
                raise ValueError('expression_count_limit')
            self.expressions[bound.key] = bound
            self.observations[bound.key] = dict(observations=[],observation_count=0,
                observations_truncated=False,gaps=[],coverage_status='not_observed',observed_bits=0)
        return bound.key

    def _record(self, key, result, time_fs, phase):
        state = self.observations[key]
        state['observation_count'] += 1
        state['gaps'] = list(dict.fromkeys(state['gaps'] + result.gaps))
        missing = result.value is None or any(g in result.gaps for g in (
            'signal_not_dumped','array_element_not_dumped','expression_observation_limit','transition_data_truncated'))
        if missing:
            state['coverage_status'] = 'partial'
        elif state['coverage_status'] == 'not_observed':
            state['coverage_status'] = 'complete'
        cost = sum(d['width'] + len(d['bits']) for d in result.dependencies) + len(result.value or '')
        if len(state['observations']) < MAX_OBSERVATIONS and state['observed_bits'] + cost <= 65536:
            state['observed_bits'] += cost
            state['observations'].append(dict(time_fs=time_fs,time_ps=(time_fs+999)//1000,
                phase=phase,value=result.value,gaps=result.gaps,
                dependencies=[{k:d[k] for k in ('signal','bits','width','role','value')} for d in result.dependencies]))
        else:
            state['observations_truncated'] = True

    def expression_receipts(self):
        return [{**b.receipt(),**{k:v for k,v in self.observations[key].items() if k != 'observed_bits'}}
                for key,b in self.expressions.items()]

    def _limited(self,key,gap='transition_data_truncated'):
        state = self.observations[key]
        state['coverage_status'] = 'partial'
        if gap not in state['gaps']:
            state['gaps'].append(gap)

    def _event_source_paths(self, path):
        b = self.expressions.get(path)
        return list(b.paths) if b else [super()._event_source_path(path)]

    @contextmanager
    def transition_group(self, paths):
        expanded = list(dict.fromkeys(p for path in paths for p in self._event_source_paths(path)))
        group = getattr(self.parser,'transition_group',None)
        if group:
            with group(expanded) as active:
                yield active
        else:
            yield False

    def get_signal_width(self,path):
        b = self.expressions.get(path)
        return b.typed.type.width if b else super().get_signal_width(path)

    def _public_value(self,key,bits):
        return Value(bits,self.expressions[key].typed.type.signed).public() if bits is not None else None

    def get_value_at_time(self,path,time_ps):
        if path not in self.expressions:
            return super().get_value_at_time(path,time_ps)
        bound = self.expressions[path]
        bound.validate(self.parser)
        cache = {}
        def value(p):
            if p not in cache:
                cache[p] = self.parser.get_value_at_time(p,time_ps)
            return cache[p]
        result = bound.evaluate(value)
        self._record(path,result,time_ps*1000,'after')
        return dict(signal=path,time_ps=time_ps,time_ns=time_ps/1000,value=self._public_value(path,result.value))

    def _reader(self,path,readers,start,max_events,max_bytes,budget=None):
        end_fs = min((r.end_fs for r in readers if r.end_fs is not None),default=None)
        if end_fs is None:
            end_fs = self.parser.get_header()['simulation_duration_ps']*1000
        return ExpressionEventReader(self.expressions[path],readers,start,end_fs,max_events,max_bytes,
            lambda r,t,p:self._record(path,r,t,p),budget)

    @contextmanager
    def _event_pages(self,path,start=0,end=-1,*,max_events=1024,max_bytes=262144):
        if path not in self.expressions:
            with super()._event_pages(path,start,end,max_events=max_events,max_bytes=max_bytes) as reader:
                yield reader
            return
        bound = self.expressions[path]
        bound.validate(self.parser)
        with ExitStack() as stack:
            readers = [stack.enter_context(self.parser._event_pages(p,start,end,
                max_events=max_events,max_bytes=max_bytes)) for p in bound.paths]
            reader = self._reader(path,readers,start,max_events,max_bytes)
            yield reader
            if reader.failed:
                self._limited(path,reader.failure)

    @contextmanager
    def _standalone_reader(self,path,start,end):
        from .waveform_batch import event_readers,EventPagingUnavailable
        try:
            with event_readers([(self,path)],start,end) as readers:
                yield readers[0]
                return
        except EventPagingUnavailable:
            pass
        # Oversized resident groups are read one declaration at a time into a
        # bounded request buffer. Never nest loads inside an active FSDB group.
        if getattr(self.parser,'_transition_group_active',False):
            raise ValueError('expression_dependency_group_unavailable')
        budget, stored = ReadBudget(),[]
        header = self.parser.get_header()
        for p in self.expressions[path].paths:
            check_cancelled()
            pages = []
            try:
                with event_readers([(self.parser,p)],start,end) as readers:
                    while True:
                        page = readers[0].read_page()
                        budget.consume(page)
                        pages.append(page)
                        if page.complete or page.truncated:
                            break
                    stored.append(StoredReader(pages,readers[0].width,readers[0].end_fs))
            except EventPagingUnavailable:
                if header.get('scale_fs_per_tick',0) < 1000:
                    raise ValueError('legacy_sub_ps_order_unavailable')
                raw = self.parser.get_transitions(p,start_ps=start,end_ps=end)
                from .divergence_compare import bit_value
                width = self.parser.get_signal_width(p)
                def event(row):
                    return Event(row['time_ps']*1000,row['time_ps'],bit_value(row.get('value'),width))
                page = EventPage(tuple(event(r) for r in raw['transitions']),
                    event(raw['predecessor']) if raw.get('predecessor') else None,
                    complete=not raw.get('truncated'),truncated=bool(raw.get('truncated')),
                    output_bytes=sum(width+32 for _ in raw['transitions']))
                budget.consume(page)
                stored.append(StoredReader([page],width,header['simulation_duration_ps']*1000))
        # Replaying these pages is accounted by the derived reader; the earlier
        # budget bounds resident materialization before any result is emitted.
        reader = self._reader(path,stored,start,1024,262144)
        yield reader
        if reader.failed:
            self._limited(path,reader.failure)

    def get_transitions(self,path,start_ps=0,end_ps=-1):
        if path not in self.expressions:
            return super().get_transitions(path,start_ps,end_ps)
        if start_ps < 0 or end_ps < -1 or 0 <= end_ps < start_ps:
            raise ValueError('expression_window_invalid')
        self.expressions[path].validate(self.parser)
        header = self.parser.get_header()
        rows, previous, truncated = [],None,False
        effective_end = header['simulation_duration_ps'] if end_ps < 0 else end_ps
        def row(event):
            return dict(time_ps=event.time_ps,time_fs=event.time_fs,time_ns=event.time_ps/1000,
                        value=self._public_value(path,event.value))
        try:
            with self._standalone_reader(path,start_ps,end_ps) as reader:
                while True:
                    check_cancelled()
                    page = reader.read_page()
                    if page.predecessor:
                        previous = row(page.predecessor)
                    rows.extend(row(e) for e in page.events)
                    if len(rows)*self.get_signal_width(path) > MAX_PROJECTED_BITS:
                        truncated = True
                        break
                    if page.complete or page.truncated:
                        truncated = page.truncated
                        break
        except IncompleteGroup as exc:
            truncated = True
            self._limited(path,str(exc))
        if truncated:
            self._limited(path)
        return dict(signal=path,start_ps=start_ps,end_ps=effective_end,transitions=rows,
                    transition_count=len(rows),predecessor=previous,predecessor_kind='dependency_anchor',
                    truncated=truncated,transition_count_is_lower_bound=truncated)

    def _sampling_transitions(self,path,start,end):
        if path in self.expressions:
            return self.get_transitions(path,start,end)
        from .cycle_query import _read_before_transitions
        # Preserve ordered static projections without recursing through this
        # class's sampling hook.
        adapter = SelectionParser(self.parser)
        adapter.projections,adapter._declarations = self.projections,self._declarations
        return _read_before_transitions(adapter,path,start,end)

    def sample_columns(self,paths,edges,offset,sample_times,session,*,sample_phase='after'):
        from .cycle_query import _sample_signal_columns_at_edges
        if len(edges)*len(paths) > MAX_PROJECTED_CELLS or len(edges)*sum(self.get_signal_width(p) for p in paths) > MAX_PROJECTED_BITS:
            raise ValueError('expression_sample_limit')
        sample_times = sample_times if sample_times is not None else [e+offset for e in edges]
        cache, errors, limited, columns = {},{},set(),{}
        def column(p):
            if p not in cache:
                raw,failed,truncated = _sample_signal_columns_at_edges(self.parser,[p],edges,offset,
                    sample_times=sample_times,sampling_session=session,safe_prefix_only=True,sample_phase=sample_phase)
                cache[p] = raw.get(p,[None]*len(edges))
                errors.update(failed)
                limited.update(truncated)
            return cache[p]
        output_errors,output_limited = {},[]
        for path in dict.fromkeys(paths):
            check_cancelled()
            bound = self.expressions.get(path)
            projection = self.projections.get(path)
            if bound:
                bound.validate(self.parser)
                output = []
                for i,at in enumerate(sample_times):
                    result = bound.evaluate(lambda p: {'value':column(p)[i],
                        'gaps':['transition_data_truncated'] if p in limited and column(p)[i] is None else []})
                    self._record(path,result,at if sample_phase == 'before' else at*1000,sample_phase)
                    output.append(self._public_value(path,result.value))
                columns[path] = output
                if limited.intersection(bound.paths):
                    output_limited.append(path)
                    self._limited(path)
            elif projection:
                self._validate(projection)
                columns[path] = [projection.value(v) for v in column(projection.path)]
                if projection.path in errors:
                    output_errors[path] = errors[projection.path]
                if projection.path in limited:
                    output_limited.append(path)
            else:
                columns[path] = column(path)
                if path in errors:
                    output_errors[path] = errors[path]
                if path in limited:
                    output_limited.append(path)
        return columns,output_errors,output_limited

    def get_signals_around_time(self,paths,center_ps,window_ps=500,extra_transitions=5):
        ordinary = [p for p in paths if p not in self.expressions]
        result = super().get_signals_around_time(ordinary,center_ps,window_ps,extra_transitions)
        start,end = max(0,center_ps-window_ps),center_ps+window_ps
        for path in paths:
            if path not in self.expressions:
                continue
            # Historical rows require actual derived changes, not dependency
            # anchors masquerading as output transitions.
            stream = self.get_transitions(path,0 if extra_transitions else start,end)
            rows = stream['transitions']
            before = [r for r in rows if r['time_fs'] < start*1000]
            result['signals'][path] = dict(value_at_center=self.get_value_at_time(path,center_ps)['value'],
                transitions_in_window=[r for r in rows if start*1000 <= r['time_fs'] <= end*1000],
                pre_window_transitions=before[-extra_transitions:] if extra_transitions else [],
                truncated=stream['truncated'])
            result['truncated'] |= stream['truncated']
        return result
