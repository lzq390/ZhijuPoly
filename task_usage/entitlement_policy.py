"""Versioned entitlement decisions; pure data in/data out, no IO or authority changes.

Specifications and facts must come from trusted server adapters. This module does
not authenticate users, validate scientific truth, reserve budgets, or infer
completion from an HTTP status. Policy installation/pinning belongs to the repo.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re


class EntitlementPolicyError(ValueError):
    pass


class InvalidSpecification(EntitlementPolicyError):
    pass


class InvalidQualificationFacts(EntitlementPolicyError):
    pass


POOLS = ('chemistry_small', 'generation', 'assistant', 'summary', 'md', 'dft', 'batch', 'online')
POOL_LABELS = {'chemistry_small': '小型化学服务', 'generation': '候选生成', 'assistant': '助手',
               'summary': '浏览总结', 'md': '分子动力学', 'dft': 'DFT', 'batch': '批量聚合', 'online': '在线抽取'}
PERIODS = ('day', 'week', 'month')
POLICY_SCHEMA_VERSION = 1
EXAMPLE_POLICY_VERSION = 'entitlements-example-v1'


def _integer(low, high):
    return {'type': 'integer', 'min': low, 'max': high}


def _number(low, high, *, exclusive_min=False):
    return {'type': 'number', 'min': low, 'max': high, 'exclusive_min': exclusive_min}


def _enum(*values):
    return {'type': 'enum', 'values': list(values)}


def _variant(pool, fields=None, *, mode=None):
    return {'mode': mode or ('count' if pool else 'rate_only'), 'pool_id': pool, 'fields': fields or {}}


def _module(pool, fields=None, *, selector=None, variants=None, mode=None):
    return {'pool_id': pool, 'selector': selector,
            'variants': variants or {'default': _variant(pool, fields, mode=mode)}}


_ASSISTANT_FIELDS = {
    'message_count': _integer(1, 200), 'input_characters': _integer(0, 200_000),
    'image_count': _integer(0, 3), 'image_bytes': _integer(0, 30 * 1024**2),
    'max_output_tokens': _integer(1, 8192), 'provider_attempt_limit': _integer(1, 8),
}
_MODULE_CONTRACTS = {
    'predict': _module('chemistry_small', {'property_count': _integer(1, 9)}),
    'ocsr': _module('chemistry_small', {'image_bytes': _integer(1, 10 * 1024**2), 'image_pixels': _integer(1, 20_000_000)}),
    'retrosynthesis': _module('chemistry_small', {'num_beams': _integer(1, 20), 'num_return_sequences': _integer(1, 10), 'max_new_tokens': _integer(16, 256)}),
    'polymerization': _module('chemistry_small', {'max_results': _integer(1, 20), 'monomer_count': _integer(1, 2)}),
    'structure': _module('chemistry_small', selector='operation', variants={
        '3d': _variant('chemistry_small', {'operation': _enum('3d'), 'atom_count': _integer(1, 500)}),
        '2d': _variant(None, {'operation': _enum('2d')}),
        'standardize': _variant(None, {'operation': _enum('standardize')}),
    }),
    'conditional_generation': _module('generation', {
        'candidate_count': _integer(1, 50), 'top_k': _integer(1, 20),
        'temperature': _number(0.1, 2.0), 'delta_tg': _number(-1_000_000.0, 1_000_000.0),
    }),
    'polytao': _module('generation', selector='operation', variants={
        'generate': _variant('generation', {'operation': _enum('generate'), 'candidate_count': _integer(1, 50),
            'temperature': _number(0.1, 2.0), 'top_k': _integer(1, 500),
            'top_p': _number(0.0, 1.0, exclusive_min=True), 'max_length': _integer(16, 512)}),
        'descriptors': _variant(None, {'operation': _enum('descriptors')}),
    }),
    'assistant': _module('assistant', _ASSISTANT_FIELDS),
    'tg_assistant': _module('assistant', _ASSISTANT_FIELDS),
    'recording_summary': _module('summary', {'evidence_characters': _integer(0, 80_000),
        'max_output_tokens': _integer(1, 8192), 'provider_attempt_limit': _integer(1, 8)}),
    'monomer_md': _module('md', selector='run_mode', variants={
        'formal': _variant('md', {'run_mode': _enum('formal'),
            'protocol': _enum('Density', 'Transport', 'HVap', 'Dielectric', 'Compressibility'),
            'requested_steps': _integer(1, 1_000_000_000), 'requested_atom_count': _integer(1, 1_000_000)}),
        'demo': _variant('md', {'run_mode': _enum('demo'), 'protocol': _enum('DensityDemo'),
            'requested_steps': _integer(1, 1_000_000_000)}),
    }),
    'monomer_dft': _module('dft', {
        'calculation_type': _enum('single_point', 'optimization'),
        'model': _enum('aimnet2', 'aimnet2-2025', 'aimnet2-b973c', 'aimnet2-nse', 'aimnet2-pd', 'aimnet2-rxn'),
        'total_atoms': _integer(1, 300),
        'requested_properties': {'type': 'enum_list', 'values': ['energy', 'charges', 'forces', 'hessian', 'frequencies'], 'min_items': 1, 'max_items': 5},
        'max_steps': _integer(0, 50), 'conformer_max_iterations': _integer(1, 5000),
    }),
    'polymerization_batch': _module('batch', {'raw_pairs': _integer(1, 50_000),
        'valid_pairs': _integer(1, 50_000), 'rows_a': _integer(1, 5000), 'rows_b': _integer(1, 5000)}),
    'online_knowledge': _module('online', {'max_papers': _integer(1, 2000), 'mode': _enum('synthesis', 'property')}),
    'reverse_design': _module(None, {'candidate_size': _integer(1, 200)}),
    'query': _module('chemistry_small', selector='match_mode', variants={
        'property': _variant('chemistry_small', {'match_mode': _enum('property'),
            'property_count': _integer(1, 1), 'top_k': _integer(1, 100),
            'similarity_threshold': _number(0.0, 1.0)}),
        'structure': _variant(None, {'match_mode': _enum('structure'),
            'top_k': _integer(1, 100), 'similarity_threshold': _number(0.0, 1.0)}),
    }),
    **{name: _module(None) for name in ('database_browser', 'dft_history', 'knowledge', 'batch_preflight', 'md_demo')},
    'openscience': _module(None, mode='external'),
}
_RULES = {
    'generation_partial_counts': True,
    'chemistry_no_match_counts': True,
    'batch_partial_counts': False,
    'online_partial_counts': False,
    'dft_non_converged_counts': True,
    'md_demo_counts': False,
}
_TOP_KEYS = {'schema_version', 'version', 'example', 'enforcement_default', 'default_tier',
             'free_assignment', 'period', 'result_max_bytes', 'operation_max_rows',
             'operation_max_bytes', 'request_rate_per_minute', 'active_operations_per_owner', 'tiers', 'rules', 'modules'}


def _require(condition, label, error=EntitlementPolicyError):
    if not condition:
        raise error(label)


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise EntitlementPolicyError('policy_not_canonical_json') from None


def example_policy():
    """Illustrative small test budgets only; this function enables nothing."""
    return {'schema_version': 1, 'version': EXAMPLE_POLICY_VERSION, 'example': True,
        'enforcement_default': False, 'default_tier': 'member', 'free_assignment': 'explicit',
        'period': 'month', 'result_max_bytes': 1024**2, 'operation_max_rows': 200,
        'operation_max_bytes': 16 * 1024**2, 'request_rate_per_minute': 120,
        'active_operations_per_owner': 4,
        'tiers': {tier: {'pools': {pool: limit for pool in POOLS}}
                  for tier, limit in [('free', 2), ('member', 10)]},
        'rules': deepcopy(_RULES), 'modules': deepcopy(_MODULE_CONTRACTS)}


def _validate_field_definition(actual, expected):
    _require(type(actual) is dict and set(actual) == set(expected), 'policy_field_shape')
    _require(actual['type'] == expected['type'], 'policy_field_type')
    kind = expected['type']
    if kind in {'integer', 'number'}:
        for key in ('min', 'max'):
            value = actual[key]
            _require(type(value) is int if kind == 'integer' else type(value) in (int, float) and expected['min'] <= value <= expected['max'], 'policy_field_bound')
        _require(expected['min'] <= actual['min'] <= actual['max'] <= expected['max'], 'policy_field_bound')
        if kind == 'number':
            _require(type(actual['exclusive_min']) is bool and actual['exclusive_min'] == expected['exclusive_min'], 'policy_field_bound')
    else:
        values = actual['values']
        _require(type(values) is list and values and all(type(x) is str for x in values)
                 and len(values) == len(set(values)) and set(values) <= set(expected['values']), 'policy_field_values')
        if kind == 'enum_list':
            _require(type(actual['min_items']) is int and type(actual['max_items']) is int
                     and 1 <= actual['min_items'] <= actual['max_items'] <= expected['max_items'], 'policy_field_items')


def load_policy(document):
    """Validate an immutable-policy candidate. Persistence enforces version uniqueness.

    Unknown keys are errors. Specs can tighten these adapter-contract bounds but
    cannot redefine modules, selectors, pools, or introduce secret/text fields.
    """
    _require(type(document) is dict and set(document) == _TOP_KEYS, 'policy_shape')
    _require(type(document['schema_version']) is int and document['schema_version'] == 1, 'policy_schema_version')
    _require(type(document['version']) is str and re.fullmatch(r'[a-z][a-z0-9_.-]{0,63}', document['version']) is not None, 'policy_version')
    _require(type(document['example']) is bool and document['enforcement_default'] is False, 'policy_default_off')
    _require('example' not in document['version'] or document['example'] is True, 'policy_example_marker')
    _require(document['default_tier'] == 'member' and document['free_assignment'] == 'explicit', 'policy_tier_mapping')
    _require(type(document['period']) is str and document['period'] in PERIODS, 'policy_period')
    for key, high in [('result_max_bytes', 64 * 1024**2), ('operation_max_rows', 100_000), ('operation_max_bytes', 1024**3)]:
        _require(type(document[key]) is int and 1 <= document[key] <= high, 'policy_storage_cap')
    _require(document['operation_max_bytes'] >= document['result_max_bytes'], 'policy_storage_cap')
    for key, maximum in [('request_rate_per_minute', 100_000), ('active_operations_per_owner', 1000)]:
        _require(type(document[key]) is int and 1 <= document[key] <= maximum, 'policy_operational_limit')
    tiers = document['tiers']
    _require(type(tiers) is dict and set(tiers) == {'free', 'member'}, 'policy_tiers')
    for tier in tiers.values():
        _require(type(tier) is dict and set(tier) == {'pools'}, 'policy_tier_shape')
        pools = tier['pools']
        _require(type(pools) is dict and set(pools) == set(POOLS), 'policy_pools')
        _require(all(v is None or type(v) is int and 0 <= v <= 1_000_000 for v in pools.values()), 'policy_budget')
    rules = document['rules']
    _require(type(rules) is dict and set(rules) == set(_RULES) and all(type(v) is bool for v in rules.values()), 'policy_rules')
    modules = document['modules']
    _require(type(modules) is dict and set(modules) == set(_MODULE_CONTRACTS), 'policy_modules')
    for name, expected in _MODULE_CONTRACTS.items():
        item = modules[name]
        _require(type(item) is dict and set(item) == {'pool_id', 'selector', 'variants'}, 'policy_module_shape')
        _require(item['pool_id'] == expected['pool_id'] and item['selector'] == expected['selector'], 'policy_pool_mapping')
        variants = item['variants']
        _require(type(variants) is dict and set(variants) == set(expected['variants']), 'policy_variants')
        for variant, shape in expected['variants'].items():
            candidate = variants[variant]
            _require(type(candidate) is dict and set(candidate) == {'mode', 'pool_id', 'fields'}, 'policy_variant_shape')
            _require(candidate['mode'] == shape['mode'] and candidate['pool_id'] == shape['pool_id'], 'policy_pool_mapping')
            fields = candidate['fields']
            _require(type(fields) is dict and set(fields) == set(shape['fields']), 'policy_fields')
            for field, definition in shape['fields'].items():
                _validate_field_definition(fields[field], definition)
    _json(document)
    return deepcopy(document)


validate_policy = load_policy


def policy_sha256(policy):
    return hashlib.sha256(_json(load_policy(policy)).encode('utf-8')).hexdigest()


def _field_value(value, shape):
    kind = shape['type']
    if kind == 'integer':
        _require(type(value) is int and shape['min'] <= value <= shape['max'], 'spec_integer_bound', InvalidSpecification)
        return value
    if kind == 'number':
        _require(type(value) in (int, float) and shape['min'] <= value <= shape['max'], 'spec_finite_number', InvalidSpecification)
        _require(shape['min'] <= value <= shape['max'] and (not shape['exclusive_min'] or value > shape['min']), 'spec_number_bound', InvalidSpecification)
        return float(value) if value != 0 else 0.0
    if kind == 'enum':
        _require(type(value) is str and value in shape['values'], 'spec_enum', InvalidSpecification)
        return value
    _require(type(value) is list and shape['min_items'] <= len(value) <= shape['max_items']
             and all(type(x) is str and x in shape['values'] for x in value)
             and len(value) == len(set(value)), 'spec_enum_list', InvalidSpecification)
    return sorted(value)


def normalize_spec(policy, module, spec):
    policy = load_policy(policy)
    _require(type(module) is str and module in policy['modules'], 'spec_module', InvalidSpecification)
    _require(type(spec) is dict, 'spec_shape', InvalidSpecification)
    contract = policy['modules'][module]
    selector = contract['selector']
    variant = spec.get(selector) if selector else 'default'
    _require(type(variant) is str and variant in contract['variants'], 'spec_variant', InvalidSpecification)
    definition = contract['variants'][variant]
    fields = definition['fields']
    _require(set(spec) == set(fields), 'spec_fields', InvalidSpecification)
    canonical = {name: _field_value(spec[name], fields[name]) for name in sorted(fields)}
    if module == 'retrosynthesis':
        _require(canonical['num_return_sequences'] <= canonical['num_beams'], 'spec_generation_shape', InvalidSpecification)
    if module in {'assistant', 'tg_assistant'}:
        _require((canonical['image_count'] == 0) == (canonical['image_bytes'] == 0), 'spec_image_shape', InvalidSpecification)
    if module == 'polymerization_batch':
        _require(canonical['valid_pairs'] <= canonical['raw_pairs'], 'spec_batch_pairs', InvalidSpecification)
    if module == 'monomer_dft':
        _require(canonical['max_steps'] == 0 if canonical['calculation_type'] == 'single_point'
                 else 10 <= canonical['max_steps'] <= 50, 'spec_dft_steps', InvalidSpecification)
        if {'hessian', 'frequencies'} & set(canonical['requested_properties']):
            _require(canonical['total_atoms'] <= 100, 'spec_dft_hessian_atoms', InvalidSpecification)
    digest = hashlib.sha256(_json({'module': module, 'spec': canonical}).encode('utf-8')).hexdigest()
    return {'module': module, 'mode': definition['mode'], 'pool_id': definition['pool_id'],
        'canonical_spec': canonical, 'spec_sha256': digest, 'spec_id': 'v1:' + digest,
        'policy_version': policy['version'], 'policy_sha256': policy_sha256(policy)}


_COMMON = {'validated': 'bool', 'completion_proven': 'bool'}
_FACTS = {
    'predict': {'returned_properties': 'count'},
    'query': {'predicted_properties': 'count', 'match_count': 'count', 'no_match': 'bool'},
    'ocsr': {'output_present': 'bool'}, 'structure': {'output_present': 'bool'},
    'retrosynthesis': {'valid_count': 'count', 'no_match': 'bool'},
    'polymerization': {'valid_count': 'count', 'no_match': 'bool'},
    'conditional_generation': {'valid_count': 'count', 'returned_count': 'count'},
    'polytao': {'valid_count': 'count', 'returned_count': 'count'},
    'assistant': {'output_present': 'bool', 'outcome': 'assistant_outcome'},
    'tg_assistant': {'output_present': 'bool', 'outcome': 'assistant_outcome'},
    'recording_summary': {'output_present': 'bool', 'outcome': 'assistant_outcome'},
    'monomer_md': {'report_present': 'bool', 'protocol_completed': 'bool', 'cleanup_confirmed': 'bool'},
    'monomer_dft': {'report_present': 'bool', 'requested_properties_complete': 'bool', 'converged': 'nullable_bool'},
    'polymerization_batch': {'report_present': 'bool', 'complete': 'bool', 'pair_errors': 'count'},
    'online_knowledge': {'report_present': 'bool', 'example_used': 'bool', 'successful_extractions': 'count', 'failed_extractions': 'count'},
}
_ASSISTANT_OUTCOMES = {'service_completed', 'partial', 'clarification', 'refusal', 'cache_hit', 'no_events'}
_FAILED = {'failed', 'cancelled', 'timed_out', 'interrupted', 'rejected'}
_PENDING = {'pending', 'submitted', 'queued', 'running', 'cancel_requested', 'cancelling', 'unknown'}
_COMPLETED = {'completed', 'completed_with_errors', 'found_enough', 'exhausted'}


def _facts(module, facts):
    definition = {**_COMMON, **_FACTS[module]}
    _require(type(facts) is dict and set(facts) == set(definition), 'qualification_fields', InvalidQualificationFacts)
    for name, kind in definition.items():
        value = facts[name]
        valid = (type(value) is bool if kind == 'bool' else
                 value is None or type(value) is bool if kind == 'nullable_bool' else
                 type(value) is int and 0 <= value <= 2**63-1 if kind == 'count' else
                 type(value) is str and value in _ASSISTANT_OUTCOMES)
        _require(valid, 'qualification_field_type', InvalidQualificationFacts)
    return facts


def qualify(policy, module, spec, typedresultfacts, status):
    """Decide one bounded root service delivery, not scientific correctness.

    Missing evidence remains unknown/terminal=False; a repository must preserve
    its reservation. Adapters, not clients, supply validated completion facts.
    """
    policy = load_policy(policy)
    selection = normalize_spec(policy, module, spec)
    canonical = selection['canonical_spec']
    rules = policy['rules']
    _require(type(status) is str and status in _FAILED | _PENDING | _COMPLETED, 'qualification_status', InvalidQualificationFacts)

    def decision(outcome, qualified=False, *, terminal=True, reason=None):
        return {**selection, 'qualified': qualified, 'outcome': outcome,
                'units': int(qualified), 'terminal': terminal, 'reason': reason or outcome}

    if status in _PENDING:
        return decision('unknown', terminal=False, reason='terminal_not_proven')
    if status in _FAILED:
        return decision(status)
    if selection['mode'] != 'count':
        return decision(selection['mode'])
    _require(status == 'completed' or (module == 'polymerization_batch' and status == 'completed_with_errors'), 'qualification_module_status', InvalidQualificationFacts)
    if typedresultfacts is None:
        return decision('unknown', terminal=False, reason='result_evidence_missing')
    facts = _facts(module, typedresultfacts)
    if not facts['completion_proven']:
        return decision('unknown', terminal=False, reason='completion_not_proven')
    if not facts['validated']:
        return decision('invalid_result', reason='business_validation_failed')
    if module == 'query':
        _require(facts['predicted_properties'] <= canonical['property_count']
                 and facts['match_count'] <= canonical['top_k']
                 and facts['no_match'] == (facts['match_count'] == 0),
                 'qualification_query_counts', InvalidQualificationFacts)
        if facts['predicted_properties'] != canonical['property_count']:
            return decision('partial')
        return decision('no_match', rules['chemistry_no_match_counts']) if facts['no_match'] else decision('service_completed', True)
    if module == 'predict':
        _require(facts['returned_properties'] <= canonical['property_count'], 'qualification_count_bound', InvalidQualificationFacts)
        return decision('service_completed', True) if facts['returned_properties'] == canonical['property_count'] else decision('partial')
    if module in {'ocsr', 'structure'}:
        return decision('service_completed', True) if facts['output_present'] else decision('invalid_result')
    if module in {'retrosynthesis', 'polymerization'}:
        limit = canonical['num_return_sequences'] if module == 'retrosynthesis' else canonical['max_results']
        _require(facts['valid_count'] <= limit and not (facts['no_match'] and facts['valid_count']), 'qualification_count_bound', InvalidQualificationFacts)
        if facts['valid_count']:
            return decision('service_completed', True)
        return decision('no_match', rules['chemistry_no_match_counts']) if facts['no_match'] else decision('no_candidates')
    if module in {'conditional_generation', 'polytao'}:
        target = canonical['candidate_count']  # top_k is sampling, not result slicing.
        _require(facts['valid_count'] <= facts['returned_count'] <= target, 'qualification_count_bound', InvalidQualificationFacts)
        if not facts['valid_count']:
            return decision('no_candidates')
        if facts['valid_count'] < target:
            return decision('partial', rules['generation_partial_counts'])
        return decision('service_completed', True)
    if module in {'assistant', 'tg_assistant', 'recording_summary'}:
        outcome = facts['outcome']
        if outcome != 'service_completed':
            return decision(outcome)
        return decision('service_completed', True) if facts['output_present'] else decision('invalid_result')
    if module == 'monomer_md':
        if not facts['cleanup_confirmed']:
            return decision('unknown', terminal=False, reason='cleanup_not_proven')
        if not facts['report_present'] or not facts['protocol_completed']:
            return decision('partial')
        return decision('demo', rules['md_demo_counts']) if canonical['run_mode'] == 'demo' else decision('service_completed', True)
    if module == 'monomer_dft':
        if not facts['report_present'] or not facts['requested_properties_complete']:
            return decision('partial')
        if canonical['calculation_type'] == 'optimization':
            if facts['converged'] is None:
                return decision('unknown', terminal=False, reason='convergence_report_missing')
            if facts['converged'] is False:
                return decision('non_converged', rules['dft_non_converged_counts'])
        else:
            _require(facts['converged'] is None, 'qualification_single_point_convergence', InvalidQualificationFacts)
        return decision('service_completed', True)
    if module == 'polymerization_batch':
        if not facts['report_present']:
            return decision('invalid_result')
        partial = status == 'completed_with_errors' or not facts['complete'] or facts['pair_errors'] > 0
        return decision('partial', rules['batch_partial_counts']) if partial else decision('service_completed', True)
    if facts['example_used']:
        return decision('demo')
    if not facts['report_present']:
        return decision('invalid_result')
    if facts['failed_extractions']:
        return decision('partial', rules['online_partial_counts'] and facts['successful_extractions'] > 0)
    return decision('service_completed', True) if facts['successful_extractions'] else decision('no_match')


def public_policy(policy):
    """Only policy configuration, never owner assignments or request/result data."""
    policy = load_policy(policy)
    return {'version': policy['version'], 'sha256': policy_sha256(policy),
        'example': policy['example'], 'period': policy['period'],
        'request_rate_per_minute': policy['request_rate_per_minute'],
        'active_operations_per_owner': policy['active_operations_per_owner'],
        'tiers': deepcopy(policy['tiers']), 'rules': deepcopy(policy['rules']),
        'pool_labels': dict(POOL_LABELS), 'modules': deepcopy(policy['modules'])}
