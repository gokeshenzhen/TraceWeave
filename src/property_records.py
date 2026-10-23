"""Primitive worker receipts; deliberately independent of Pydantic / MCP."""


def explanation_record(item_id):
    return dict(id=item_id,identity={},target={},mode={},directive={},syntax_status='not_checked',
                semantic_status='not_run',context_status='unverified',root=None,nodes=[],clocks=[],
                variables=[],effects=[],references=[],diagnostics=[],diagnostics_truncated=False,candidates=[],gaps=[],gap_codes=[],
                gaps_truncated=False,summary=[],analysis_limited=False,output_truncated=False,node_count=0)


def gap(code,category,message,recovery,source=None):
    return dict(code=code,category=category,message=message,recovery=recovery,source=source or {})


def blocked(item,code,category,message,recovery):
    row=explanation_record(item['id'])
    source={'file':item.get('source_file'),'line':item.get('line'),'text':(item.get('text') or '')[:1024]}
    row['target']={k:item[k] for k in ('source_file','line','instance_path','target') if item.get(k) is not None}
    row['gaps']=[gap(code,category,message,recovery,source)]
    row['gap_codes']=[code]
    row['analysis_limited']=category=='limit'
    if category=='identity':
        row['context_status']='changed' if 'changed' in code else 'incomplete'
    return row
