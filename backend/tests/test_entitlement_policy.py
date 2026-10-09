"""Pure policy decisions: synthetic facts only, no IO/services during decisions."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from task_usage.entitlement_policy import (
    POOLS, EntitlementPolicyError, InvalidSpecification, InvalidQualificationFacts,
    example_policy, load_policy, normalize_spec, policy_sha256, public_policy, qualify,
)


@pytest.fixture
def policy():
    return load_policy(example_policy())


def facts(**kwargs):
    return {'validated': True, 'completion_proven': True, **kwargs}


SPECS = {
    'predict': {'property_count': 2},
    'ocsr': {'image_bytes': 100, 'image_pixels': 400},
    'retrosynthesis': {'num_beams': 5, 'num_return_sequences': 5, 'max_new_tokens': 128},
    'polymerization': {'max_results': 10, 'monomer_count': 2},
    'structure': {'operation': '3d', 'atom_count': 10},
    'conditional_generation': {'candidate_count': 10, 'top_k': 5, 'temperature': 1.0, 'delta_tg': 10.0},
    'polytao': {'operation': 'generate', 'candidate_count': 10, 'temperature': 1.0, 'top_k': 100, 'top_p': .999, 'max_length': 300},
    'assistant': {'message_count': 2, 'input_characters': 100, 'image_count': 0, 'image_bytes': 0, 'max_output_tokens': 1400, 'provider_attempt_limit': 4},
    'tg_assistant': {'message_count': 2, 'input_characters': 100, 'image_count': 1, 'image_bytes': 50, 'max_output_tokens': 2400, 'provider_attempt_limit': 4},
    'recording_summary': {'evidence_characters': 400, 'max_output_tokens': 4096, 'provider_attempt_limit': 1},
    'monomer_md': {'run_mode': 'formal', 'protocol': 'Density', 'requested_steps': 1500000, 'requested_atom_count': 10000},
    'monomer_dft': {'calculation_type': 'single_point', 'model': 'aimnet2', 'total_atoms': 3, 'requested_properties': ['energy'], 'max_steps': 0, 'conformer_max_iterations': 500},
    'polymerization_batch': {'raw_pairs': 8, 'valid_pairs': 6, 'rows_a': 2, 'rows_b': 4},
    'online_knowledge': {'max_papers': 4, 'mode': 'synthesis'},
    'reverse_design': {'candidate_size': 200},
    'query': {'match_mode': 'property', 'property_count': 1, 'top_k': 10, 'similarity_threshold': .7},
    **{m: {} for m in ('database_browser','dft_history','knowledge','batch_preflight','md_demo','openscience')},
}


def test_versioned_example_exact_and_no_mutable_global_aliases(policy):
    path = Path(__file__).resolve().parents[2] / 'config/examples/entitlements-example-v1.json'
    assert json.loads(path.read_text()) == policy
    assert policy['example'] is True and policy['enforcement_default'] is False
    assert policy['default_tier'] == 'member' and policy['free_assignment'] == 'explicit'
    assert set(policy['tiers']) == {'member','free'}
    assert set(policy['tiers']['free']['pools']) == set(POOLS)
    policy['tiers']['free']['pools']['md'] = 999
    assert example_policy()['tiers']['free']['pools']['md'] == 2


@pytest.mark.parametrize('module', SPECS)
def test_complete_module_spec_inventory(policy,module):
    original=deepcopy(SPECS[module])
    selected=normalize_spec(policy,module,original)
    assert original==SPECS[module]
    assert selected['module']==module and selected['policy_version']==policy['version']
    assert len(selected['spec_sha256'])==64 and selected['spec_id']=='v1:'+selected['spec_sha256']
    assert selected['policy_sha256']==policy_sha256(policy)
    if selected['mode']=='count':assert selected['pool_id'] in POOLS
    else:assert selected['pool_id'] is None


@pytest.mark.parametrize('module,spec',[
    ('structure',{'operation':'2d'}),('structure',{'operation':'standardize'}),
    ('polytao',{'operation':'descriptors'}),('md_demo',{}),
])
def test_rate_only_variants_never_consume_counts(policy,module,spec):
    decision=qualify(policy,module,spec,None,'completed')
    assert decision['mode']=='rate_only' and decision['units']==0 and not decision['qualified']


@pytest.mark.parametrize('change',[
    lambda d:d.update(unknown=True), lambda d:d.update(schema_version=2),
    lambda d:d.update(version=''), lambda d:d.update(example=False),
    lambda d:d.update(enforcement_default=True), lambda d:d.update(period='year'),
    lambda d:d.update(default_tier='free'), lambda d:d.update(free_assignment='automatic'),
    lambda d:d.update(result_max_bytes=0), lambda d:d.update(operation_max_rows=True),
    lambda d:d['tiers'].update(admin={'pools':{}}),
    lambda d:d['tiers']['free']['pools'].update(md=-1),
    lambda d:d['tiers']['member']['pools'].update(dft=True),
    lambda d:d['modules']['predict'].update(pool_id='assistant'),
    lambda d:d['modules']['predict']['variants']['default']['fields']['property_count'].update(max=99),
])
def test_policy_unknown_shape_unsafe_bounds_and_example_marker_rejected(policy,change):
    change(policy)
    with pytest.raises(EntitlementPolicyError):load_policy(policy)


@pytest.mark.parametrize('spec',[
    {}, {'property_count':True}, {'property_count':0}, {'property_count':10},
    {'property_count':'2'}, {'property_count':2,'owner_user_id':'private'},
    {'property_count':2,'pool_id':'other'}, {'property_count':2,'api_key':'never-read'},
])
def test_strict_specs_reject_missing_unknown_and_bad_types(policy,spec):
    with pytest.raises(InvalidSpecification):normalize_spec(policy,'predict',spec)


@pytest.mark.parametrize('bad',[float('nan'),float('inf'),-float('inf'),10**1000,True])
def test_spec_numeric_nonfinite_or_unbounded_rejected_with_safe_error(policy,bad):
    spec={**SPECS['conditional_generation'],'delta_tg':bad}
    with pytest.raises(InvalidSpecification):normalize_spec(policy,'conditional_generation',spec)


def test_policy_snapshot_changes_hash_and_spec_hash_is_canonical(policy):
    a=normalize_spec(policy,'monomer_dft',{**SPECS['monomer_dft'],'requested_properties':['forces','energy']})
    b=normalize_spec(policy,'monomer_dft',{**SPECS['monomer_dft'],'requested_properties':['energy','forces']})
    assert a==b
    policy['version']='test-new-version'
    policy['rules']['generation_partial_counts']=False
    assert policy_sha256(policy)!=a['policy_sha256']


def test_tg_top_k_is_sampling_not_return_limit_and_partial_is_separate(policy):
    full=qualify(policy,'conditional_generation',SPECS['conditional_generation'],facts(valid_count=10,returned_count=10),'completed')
    partial=qualify(policy,'conditional_generation',SPECS['conditional_generation'],facts(valid_count=5,returned_count=5),'completed')
    assert full['outcome']=='service_completed' and full['units']==1
    assert partial['outcome']=='partial' and partial['units']==1
    policy['rules']['generation_partial_counts']=False
    assert qualify(policy,'conditional_generation',SPECS['conditional_generation'],facts(valid_count=5,returned_count=5),'completed')['units']==0


@pytest.mark.parametrize('module',['conditional_generation','polytao'])
def test_generation_zero_never_success_and_counts_need_consistency(policy,module):
    empty=qualify(policy,module,SPECS[module],facts(valid_count=0,returned_count=0),'completed')
    assert empty['outcome']=='no_candidates' and empty['units']==0
    with pytest.raises(InvalidQualificationFacts):
        qualify(policy,module,SPECS[module],facts(valid_count=2,returned_count=1),'completed')


@pytest.mark.parametrize('module',['retrosynthesis','polymerization'])
def test_honest_no_match_is_configurable_service_delivery(policy,module):
    value=facts(valid_count=0,no_match=True)
    result=qualify(policy,module,SPECS[module],value,'completed')
    assert result['outcome']=='no_match' and result['units']==1
    policy['rules']['chemistry_no_match_counts']=False
    assert qualify(policy,module,SPECS[module],value,'completed')['units']==0


@pytest.mark.parametrize('module',['assistant','tg_assistant','recording_summary'])
@pytest.mark.parametrize('outcome',['partial','clarification','refusal','cache_hit','no_events'])
def test_assistant_non_delivery_outcomes_do_not_consume(policy,module,outcome):
    value=facts(output_present=True,outcome=outcome)
    result=qualify(policy,module,SPECS[module],value,'completed')
    assert result['outcome']==outcome and result['units']==0 and result['terminal']


@pytest.mark.parametrize('module',['assistant','tg_assistant','recording_summary'])
def test_assistant_requires_proven_final_business_output(policy,module):
    value=facts(output_present=True,outcome='service_completed')
    assert qualify(policy,module,SPECS[module],value,'completed')['units']==1
    value['completion_proven']=False
    result=qualify(policy,module,SPECS[module],value,'completed')
    assert result['outcome']=='unknown' and not result['terminal'] and result['units']==0


def test_dft_nonconverged_is_not_scientific_success_and_missing_property_is_partial(policy):
    spec={**SPECS['monomer_dft'],'calculation_type':'optimization','max_steps':50}
    value=facts(report_present=True,requested_properties_complete=True,converged=False)
    result=qualify(policy,'monomer_dft',spec,value,'completed')
    assert result['outcome']=='non_converged' and result['units']==1
    policy['rules']['dft_non_converged_counts']=False
    assert qualify(policy,'monomer_dft',spec,value,'completed')['units']==0
    value['requested_properties_complete']=False
    assert qualify(policy,'monomer_dft',spec,value,'completed')['outcome']=='partial'


def test_md_cleanup_unconfirmed_remains_reserved_demo_is_explicit_zero(policy):
    value=facts(report_present=True,protocol_completed=True,cleanup_confirmed=True)
    assert qualify(policy,'monomer_md',SPECS['monomer_md'],value,'completed')['units']==1
    value['cleanup_confirmed']=False
    assert qualify(policy,'monomer_md',SPECS['monomer_md'],value,'completed')['terminal'] is False
    value['cleanup_confirmed']=True
    demo={'run_mode':'demo','protocol':'DensityDemo','requested_steps':300}
    result=qualify(policy,'monomer_md',demo,value,'completed')
    assert result['outcome']=='demo' and result['units']==0 and result['pool_id']=='md'
    policy['rules']['md_demo_counts']=True
    assert qualify(policy,'monomer_md',demo,value,'completed')['units']==1


def test_batch_partial_default_zero_bounded_batch_not_chunks(policy):
    value=facts(report_present=True,complete=True,pair_errors=0)
    assert qualify(policy,'polymerization_batch',SPECS['polymerization_batch'],value,'completed')['units']==1
    result=qualify(policy,'polymerization_batch',SPECS['polymerization_batch'],value,'completed_with_errors')
    assert result['outcome']=='partial' and result['units']==0


def test_online_example_and_partial_are_distinct_from_service_delivery(policy):
    value=facts(report_present=True,example_used=False,successful_extractions=2,failed_extractions=0)
    assert qualify(policy,'online_knowledge',SPECS['online_knowledge'],value,'completed')['units']==1
    value['failed_extractions']=1
    assert qualify(policy,'online_knowledge',SPECS['online_knowledge'],value,'completed')['outcome']=='partial'
    assert qualify(policy,'online_knowledge',SPECS['online_knowledge'],value,'completed')['units']==0
    value['example_used']=True
    assert qualify(policy,'online_knowledge',SPECS['online_knowledge'],value,'completed')['outcome']=='demo'


@pytest.mark.parametrize('status',['failed','cancelled','timed_out','interrupted','rejected'])
def test_proven_failure_never_consumes_success_count(policy,status):
    result=qualify(policy,'predict',SPECS['predict'],None,status)
    assert result['outcome']==status and result['terminal'] and result['units']==0


def test_missing_facts_remains_unknown_not_free_completion(policy):
    for status in ['running','cancel_requested','completed']:
        result=qualify(policy,'predict',SPECS['predict'],None,status)
        assert not result['terminal'] and result['outcome']=='unknown'


def test_invalid_facts_never_silently_dropped(policy):
    for value in [facts(returned_properties=True), facts(returned_properties=2,raw_text='private'), {}]:
        with pytest.raises(InvalidQualificationFacts):
            qualify(policy,'predict',SPECS['predict'],value,'completed')
    with pytest.raises(InvalidQualificationFacts):
        qualify(policy,'predict',SPECS['predict'],facts(returned_properties=2),'completed_with_errors')


def test_decisions_have_no_side_effects_or_user_identity(policy,monkeypatch):
    import builtins, os
    def blocked(*args,**kwargs):raise AssertionError('unexpected IO/environment access')
    monkeypatch.setattr(builtins,'open',blocked)
    monkeypatch.setattr(os,'getenv',blocked)
    original=deepcopy(policy)
    decision=qualify(policy,'predict',SPECS['predict'],facts(returned_properties=2),'completed')
    assert decision['qualified'] and decision['units']==1 and policy==original
    public=public_policy(policy)
    assert 'owner_user_id' not in json.dumps(public) and 'default_tier' not in public


def test_policy_number_bounds_do_not_leak_overflow(policy):
    definition=policy['modules']['conditional_generation']['variants']['default']['fields']['delta_tg']
    definition['max']=10**1000
    with pytest.raises(EntitlementPolicyError,match='policy_field_bound'):load_policy(policy)


def test_proven_refusal_is_zero_but_unproven_eof_stays_unknown(policy):
    value=facts(output_present=False,outcome='refusal')
    result=qualify(policy,'assistant',SPECS['assistant'],value,'completed')
    assert result['terminal'] and result['outcome']=='refusal' and result['units']==0
    value['completion_proven']=False
    result=qualify(policy,'assistant',SPECS['assistant'],value,'completed')
    assert not result['terminal'] and result['outcome']=='unknown'


@pytest.mark.parametrize('field',['request_rate_per_minute','active_operations_per_owner'])
@pytest.mark.parametrize('invalid',[0,-1,True,None,100001])
def test_operational_limits_are_strict_positive_bounded_integers(policy,field,invalid):
    policy[field]=invalid
    with pytest.raises(EntitlementPolicyError,match='policy_operational_limit'):load_policy(policy)


def test_operational_guards_are_separate_from_monthly_count_budgets(policy):
    assert policy['request_rate_per_minute']==120
    assert policy['active_operations_per_owner']==4
    assert policy['tiers']['free']['pools']['md']==2
    assert public_policy(policy)['active_operations_per_owner']==4


@pytest.mark.parametrize('period',['day','week','month'])
def test_supported_utc_periods_are_pinned_in_policy_hash(policy,period):
    original_hash=policy_sha256(policy)
    policy['period']=period
    checked=load_policy(policy)
    assert checked['period']==period and public_policy(checked)['period']==period
    assert (policy_sha256(checked)==original_hash)==(period=='month')
    assert example_policy()['period']=='month'


@pytest.mark.parametrize('period',['hour','year','Day','UTC-month','',None,True,1,[],{}])
def test_unknown_periods_and_wrong_types_rejected(policy,period):
    policy['period']=period
    with pytest.raises(EntitlementPolicyError,match='policy_period'):load_policy(policy)


def test_property_query_uses_shared_small_pool_and_strict_server_spec(policy):
    selected=normalize_spec(policy,'query',SPECS['query'])
    assert selected['mode']=='count' and selected['pool_id']=='chemistry_small'
    for spec in [{}, {'match_mode':'property'},
                 {**SPECS['query'],'property_count':0},
                 {**SPECS['query'],'property_count':2},
                 {**SPECS['query'],'property_count':True},
                 {**SPECS['query'],'top_k':101},
                 {**SPECS['query'],'top_k':0},
                 {**SPECS['query'],'similarity_threshold':float('nan')},
                 {**SPECS['query'],'property_name':'private-input'},
                 {**SPECS['query'],'pool_id':None},
                 {**SPECS['query'],'match_mode':'similarity'}]:
        with pytest.raises(InvalidSpecification):normalize_spec(policy,'query',spec)


def test_query_pool_and_selector_cannot_be_redefined_in_policy(policy):
    policy['modules']['query']['variants']['property']['pool_id']=None
    policy['modules']['query']['variants']['property']['mode']='rate_only'
    with pytest.raises(EntitlementPolicyError,match='policy_pool_mapping'):load_policy(policy)


def test_structural_query_is_rate_only_after_request_model_normalization(policy):
    spec={'match_mode':'structure','top_k':10,'similarity_threshold':.7}
    result=qualify(policy,'query',spec,None,'completed')
    assert result['mode']=='rate_only' and result['pool_id'] is None and result['units']==0
    for bad in [{**spec,'match_mode':'exact'},{**spec,'property_count':1}]:
        with pytest.raises(InvalidSpecification):normalize_spec(policy,'query',bad)


def test_property_query_proven_prediction_and_search_are_one_delivery(policy):
    result=qualify(policy,'query',SPECS['query'],facts(predicted_properties=1,match_count=2,no_match=False),'completed')
    assert result['qualified'] and result['units']==1 and result['outcome']=='service_completed'
    empty=facts(predicted_properties=1,match_count=0,no_match=True)
    result=qualify(policy,'query',SPECS['query'],empty,'completed')
    assert result['outcome']=='no_match' and result['units']==1
    policy['rules']['chemistry_no_match_counts']=False
    assert qualify(policy,'query',SPECS['query'],empty,'completed')['units']==0


@pytest.mark.parametrize('value',[
    facts(predicted_properties=2,match_count=0,no_match=True),
    facts(predicted_properties=True,match_count=0,no_match=True),
    facts(predicted_properties=1,match_count=11,no_match=False),
    facts(predicted_properties=1,match_count=1,no_match=True),
    facts(predicted_properties=1,match_count=0,no_match=False),
    facts(predicted_properties=1,match_count=1,no_match=False,raw_prediction=100),
])
def test_property_query_rejects_inconsistent_or_untrusted_fact_shapes(policy,value):
    with pytest.raises(InvalidQualificationFacts):qualify(policy,'query',SPECS['query'],value,'completed')


def test_property_query_missing_prediction_or_terminal_does_not_consume(policy):
    value=facts(predicted_properties=0,match_count=0,no_match=True)
    result=qualify(policy,'query',SPECS['query'],value,'completed')
    assert result['outcome']=='partial' and result['units']==0
    value=facts(predicted_properties=1,match_count=1,no_match=False)
    value['completion_proven']=False
    result=qualify(policy,'query',SPECS['query'],value,'completed')
    assert result['outcome']=='unknown' and not result['terminal']
    value['completion_proven']=True;value['validated']=False
    result=qualify(policy,'query',SPECS['query'],value,'completed')
    assert result['outcome']=='invalid_result' and result['units']==0
