from src.structural_semantic_router import combine_semantics, missing_categories
from src.schemas import SemanticScanResult

OPTIONS = {'scope': 'top', 'categories': ['constant_connection', 'open_input', 'constant_comparison']}


def native(status='complete'):
    return dict(status=status, categories_checked=['constant_connection', 'open_input'],
                category_results={c: {'status': status, 'gaps': [] if status == 'complete' else ['native_call_limit']}
                                  for c in ['constant_connection', 'open_input']},
                artifact_identity='kdb', facts=[{'kind': 'constant_connection', 'port': 'a'}], total_facts=1)


def test_native_two_categories_do_not_complete_default_request():
    r = SemanticScanResult.model_validate(combine_semantics(OPTIONS, native()))
    assert r.status == 'partial'
    assert r.category_coverage[-1].status == 'not_run'
    assert missing_categories(OPTIONS['categories'], native()) == ['constant_comparison']
    r = combine_semantics({**OPTIONS, 'categories': OPTIONS['categories'][:2]}, native())
    assert r['status'] == 'complete'


def test_mixed_provenance_and_complete_source_replaces_partial_without_double_counting():
    n = native('partial')
    s = dict(status='complete', categories_checked=OPTIONS['categories'], artifact_identity='source',
             facts=[{'kind': 'constant_connection', 'port': 'a'}, {'kind': 'constant_comparison'}])
    r = combine_semantics(OPTIONS, n, s)
    assert r['status'] == 'complete' and r['total_facts'] == 2
    assert all(f['backend'] == 'slang' for f in r['facts'])
    assert r['result_groups'][0]['selected_categories'] == []
    r = combine_semantics(OPTIONS, native(), s)
    assert r['backend'] == 'mixed' and r['total_facts'] == 2
    assert r['facts'][0]['result_group'] == 'verdi_npi'
    assert {g['artifact_identity'] for g in r['result_groups']} == {'kdb', 'source'}


def test_cache_hit_is_not_completion_and_old_payloads_remain_readable():
    source = dict(status='partial', categories_checked=['constant_connection'], facts=[],
                  cache_disposition='hit_query_artifact', gaps=['query_artifact_port_bindings_only'])
    r = SemanticScanResult.model_validate(combine_semantics(OPTIONS, source=source))
    assert r.status == 'partial' and r.total_facts == 0
    assert r.category_coverage[1].status == 'not_run'
    assert SemanticScanResult.model_validate({'status': 'not_run'}).category_coverage == []


def test_unknown_native_form_preserves_positive_evidence_with_partial_source():
    r = combine_semantics(OPTIONS, native('partial'), dict(status='partial',
                          categories_checked=OPTIONS['categories'], facts=[], gaps=['source_gap']))
    assert r['status'] == 'partial' and r['facts'][0]['backend'] == 'verdi_npi'
    assert 'native_call_limit' in r['gaps']


def test_requested_domain_and_checked_top_keep_kdb_binding_separate_from_source():
    n = native()
    n.update(selected_top='top', checked_scope='top',
             kdb_binding={'identity_sha256': 'kdb', 'load_status':'loaded', 'source_association':'unverified'})
    r = SemanticScanResult.model_validate(combine_semantics({**OPTIONS, 'scope':None}, n))
    assert r.category_coverage[0].scope is None
    assert r.category_coverage[0].checked_scope == 'top'
    assert r.result_groups[0].selected_top == 'top'
    assert r.result_groups[0].kdb_binding['source_association'] == 'unverified'


def test_frontend_resource_stop_remains_partial_without_inventing_checked_categories():
    r = combine_semantics(OPTIONS, source={'status':'partial','gaps':['worker_rss_limit']})
    assert r['status'] == 'partial' and not r['categories_checked']
    assert all(c['status'] == 'partial' for c in r['category_coverage'])
