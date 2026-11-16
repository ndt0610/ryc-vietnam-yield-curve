"""Synthetic policy-to-outcome vertical slice for RYC.

This module is deliberately small and core-only. It does not fetch market data,
forecast a curve, or render UI. It evaluates declared synthetic policy actions
through the P2 event ledger and keeps incomplete paths in the result.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import re
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path

import ryc_investor_ledger as ledger_module
import ryc_v3_engine as engine_module
from ryc_investor_ledger import LedgerError, assess_terminal_cash, decimal_text


MANIFEST_VERSION = 'RYC-POLICY-MANIFEST-2'
ANALYSIS_VERSION = 'RYC-POLICY-VERTICAL-SLICE-3'
EXPORT_VERSION = 'RYC-DECISION-RUN-EXPORT-3'
EXPORT_SCOPE = 'SYNTHETIC_ONLY_NOT_MARKET_VALIDATION'
EXACT_TOLERANCE = dict(mode='EXACT_CANONICAL_JSON', money_vnd_absolute='0',
                       probability_absolute='0')
EXPORT_FIELDS = {'export_version', 'decision_run_id', 'analysis_version', 'scope',
                 'numeric_tolerance', 'manifest_sha256', 'evaluator_sha256',
                 'result_sha256', 'warnings', 'manifest', 'result'}
SHA256_PATTERN = re.compile(r'^[0-9a-f]{64}$')
EVIDENCE_SCOPE = 'DECLARED_SYNTHETIC_ONLY'
SOURCE_STATUSES = {'DECLARED_SYNTHETIC_ONLY', 'SOURCE_REVIEW_ONLY', 'UNQUALIFIED'}
EXCLUSION_STATUSES = {'UNSUPPORTED', 'INFEASIBLE', 'UNKNOWN_ELIGIBILITY',
                      'INVALID_INPUT', 'DATA_INSUFFICIENT'}


class PolicySliceError(ValueError):
    """The synthetic policy manifest is invalid."""


def _canonical_json_bytes(value):
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                             separators=(',', ':'))
    except (TypeError, ValueError) as error:
        raise PolicySliceError('CANONICAL_JSON_REQUIRED') from error
    return encoded.encode('utf-8')


def _sha256_json(value):
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _evaluator_sha256():
    """Fingerprint policy plus its ledger and accumulation-engine dependency."""
    component_hashes = {
        'policy_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'ledger_sha256': hashlib.sha256(Path(ledger_module.__file__).read_bytes()).hexdigest(),
        'engine_sha256': hashlib.sha256(Path(engine_module.__file__).read_bytes()).hexdigest(),
    }
    return _sha256_json(component_hashes)


def _decision_run_id(manifest_sha256, evaluator_sha256):
    identity = dict(export_version=EXPORT_VERSION, analysis_version=ANALYSIS_VERSION,
                    manifest_sha256=manifest_sha256, evaluator_sha256=evaluator_sha256)
    return 'RYC-POLICY-' + _sha256_json(identity)


def _same_digest(left, right):
    return isinstance(left, str) and isinstance(right, str) and hmac.compare_digest(left, right)


def _require(condition, reason):
    if not condition:
        raise PolicySliceError(reason)


def _text(value, reason):
    _require(isinstance(value, str) and bool(value.strip()), reason)
    return value


def _decimal(value, name, *, positive=False):
    _require(type(value) is int or isinstance(value, str), 'EXACT_DECIMAL_REQUIRED:' + name)
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise PolicySliceError('INVALID_DECIMAL:' + name) from error
    _require(parsed.is_finite(), 'INVALID_DECIMAL:' + name)
    _require(parsed > 0 if positive else parsed >= 0, 'INVALID_DECIMAL:' + name)
    return parsed


def _case_matches_manifest(case, manifest):
    return (case.get('scope') == 'SYNTHETIC_ONLY' and case.get('currency') == manifest['currency']
            and case.get('decision_at') == manifest['decision_at']
            and case.get('terminal_at') == manifest['terminal_at'])


def _validate_provenance(provenance):
    required = {'producer', 'producer_version', 'evidence_scope', 'input_sha256',
                'source_refs'}
    _require(isinstance(provenance, dict) and set(provenance) == required,
             'PROVENANCE_FIELDS')
    _text(provenance['producer'], 'PROVENANCE_PRODUCER_REQUIRED')
    _text(provenance['producer_version'], 'PROVENANCE_PRODUCER_VERSION_REQUIRED')
    _require(provenance['evidence_scope'] == EVIDENCE_SCOPE,
             'UNSUPPORTED_EVIDENCE_SCOPE')
    input_sha256 = provenance['input_sha256']
    _require(input_sha256 is None or (isinstance(input_sha256, str)
                                      and SHA256_PATTERN.fullmatch(input_sha256)),
             'INVALID_PROVENANCE_INPUT_SHA256')
    refs = provenance['source_refs']
    _require(isinstance(refs, list), 'SOURCE_REFS_REQUIRED')
    source_ids = []
    for ref in refs:
        _require(isinstance(ref, dict)
                 and set(ref) == {'source_id', 'locator', 'sha256', 'status'},
                 'SOURCE_REF_FIELDS')
        source_ids.append(_text(ref['source_id'], 'SOURCE_ID_REQUIRED'))
        _text(ref['locator'], 'SOURCE_LOCATOR_REQUIRED')
        _require(ref['sha256'] is None or (isinstance(ref['sha256'], str)
                                           and SHA256_PATTERN.fullmatch(ref['sha256'])),
                 'INVALID_SOURCE_SHA256')
        _require(ref['status'] in SOURCE_STATUSES, 'UNSUPPORTED_SOURCE_STATUS')
    _require(len(source_ids) == len(set(source_ids)), 'DUPLICATE_SOURCE_ID')


def _validate_exclusions(exclusions):
    _require(isinstance(exclusions, list), 'EXCLUSIONS_REQUIRED')
    action_ids = []
    for exclusion in exclusions:
        _require(isinstance(exclusion, dict)
                 and set(exclusion) == {'action_id', 'status', 'reason_code', 'description'},
                 'EXCLUSION_FIELDS')
        action_ids.append(_text(exclusion['action_id'], 'EXCLUSION_ACTION_ID_REQUIRED'))
        _require(exclusion['status'] in EXCLUSION_STATUSES, 'UNSUPPORTED_EXCLUSION_STATUS')
        _text(exclusion['reason_code'], 'EXCLUSION_REASON_REQUIRED')
        _text(exclusion['description'], 'EXCLUSION_DESCRIPTION_REQUIRED')
    _require(len(action_ids) == len(set(action_ids)), 'DUPLICATE_EXCLUSION_ACTION')
    return set(action_ids)


def _validate_policy(policy, labels):
    _require(set(policy) == {'policy_id', 'description', 'action_by_label'}, 'POLICY_FIELDS')
    policy_id = _text(policy['policy_id'], 'POLICY_ID_REQUIRED')
    _text(policy['description'], 'POLICY_DESCRIPTION_REQUIRED')
    mapping = policy['action_by_label']
    _require(isinstance(mapping, dict) and set(mapping) == labels, 'NONANTICIPATIVE_LABEL_MAP_REQUIRED')
    for label, action_id in mapping.items():
        _text(label, 'LABEL_REQUIRED')
        _text(action_id, 'ACTION_ID_REQUIRED')
    return policy_id


def _validate_world(world, labels, manifest):
    _require(set(world) == {'world_id', 'probability', 'observation_label', 'action_cases'}, 'WORLD_FIELDS')
    world_id = _text(world['world_id'], 'WORLD_ID_REQUIRED')
    probability = _decimal(world['probability'], 'probability')
    label = _text(world['observation_label'], 'OBSERVATION_LABEL_REQUIRED')
    _require(label in labels, 'UNKNOWN_OBSERVATION_LABEL')
    action_cases = world['action_cases']
    _require(isinstance(action_cases, dict) and action_cases, 'ACTION_CASES_REQUIRED')
    for action_id, case in action_cases.items():
        _text(action_id, 'ACTION_ID_REQUIRED')
        _require(isinstance(case, dict) and _case_matches_manifest(case, manifest), 'CASE_MANIFEST_MISMATCH')
    return world_id, probability


def _terminal_components(assessment):
    state = assessment['state']
    return dict(cash_available_vnd=state['cash_available_vnd'],
                cash_locked_vnd=state['cash_locked_vnd'],
                pending_sale_receivables_vnd=state['pending_sale_receivables_vnd'],
                pending_coupon_receivables_vnd=state['pending_coupon_receivables_vnd'],
                pending_principal_receivables_vnd=state['pending_principal_receivables_vnd'],
                positions=state['positions'])


def _invalid_outcome(error):
    return dict(status='INVALID_ACTION_PATH', reasons=[str(error)], objective_cash_vnd=None,
                shortfall_vnd=None, terminal_components=None)


def _assess_case(case, mandate):
    try:
        assessment = assess_terminal_cash(case, mandate)
    except LedgerError as error:
        return _invalid_outcome(error)
    return dict(status=assessment['status'], reasons=list(assessment['reasons']),
                objective_cash_vnd=assessment['objective_cash_vnd'],
                shortfall_vnd=assessment['shortfall_vnd'],
                terminal_components=_terminal_components(assessment))


def _aggregate_outcomes(outcomes):
    with localcontext() as ctx:
        ctx.prec = 40
        expected_available = Decimal(0)
        expected_shortfall = Decimal(0)
        expected_objective = Decimal(0)
        survivor_objective = Decimal(0)
        complete_weight = Decimal(0)
        invalid_weight = Decimal(0)
        for outcome in outcomes:
            probability = Decimal(outcome['probability'])
            components = outcome['terminal_components']
            if components is not None:
                expected_available += probability * Decimal(components['cash_available_vnd'])
            if outcome['shortfall_vnd'] is not None:
                expected_shortfall += probability * Decimal(outcome['shortfall_vnd'])
            if outcome['objective_cash_vnd'] is not None:
                cash = Decimal(outcome['objective_cash_vnd'])
                expected_objective += probability * cash
                survivor_objective += probability * cash
                complete_weight += probability
            if outcome['status'] == 'INVALID_ACTION_PATH':
                invalid_weight += probability
        survivor_mean = None
        if complete_weight > 0:
            survivor_mean = decimal_text(survivor_objective / complete_weight)
        return dict(complete_weight=decimal_text(complete_weight),
                    invalid_weight=decimal_text(invalid_weight),
                    expected_available_cash_vnd=decimal_text(expected_available),
                    expected_shortfall_vnd=decimal_text(expected_shortfall),
                    expected_objective_cash_vnd=(decimal_text(expected_objective)
                                                 if complete_weight == Decimal(1) else None),
                    survivor_only_mean_cash_vnd=survivor_mean)


def _choose_policy(policy_results):
    eligible = [p for p in policy_results if p['summary']['expected_objective_cash_vnd'] is not None
                and p['summary']['invalid_weight'] == '0']
    if not eligible:
        return dict(status='NO_COMPLETE_POLICY', policy_id=None, expected_objective_cash_vnd=None)
    best_value = max(Decimal(p['summary']['expected_objective_cash_vnd'])
                     for p in eligible)
    best = [p for p in eligible
            if Decimal(p['summary']['expected_objective_cash_vnd']) == best_value]
    if len(best) > 1:
        return dict(status='POLICY_TIE', policy_id=None,
                    policy_ids=sorted(p['policy_id'] for p in best),
                    expected_objective_cash_vnd=decimal_text(best_value))
    selected = best[0]
    return dict(status='POLICY_SELECTED', policy_id=selected['policy_id'],
                expected_objective_cash_vnd=selected['summary']['expected_objective_cash_vnd'])


def _clairvoyant_upper_bound(manifest):
    with localcontext() as ctx:
        ctx.prec = 40
        total = Decimal(0)
        complete_weight = Decimal(0)
        choices = []
        for world in manifest['worlds']:
            best = None
            best_action = None
            for action_id, case in world['action_cases'].items():
                outcome = _assess_case(case, manifest['mandate'])
                if outcome['objective_cash_vnd'] is None:
                    continue
                value = Decimal(outcome['objective_cash_vnd'])
                if best is None or value > best:
                    best, best_action = value, action_id
            if best is not None:
                probability = Decimal(world['probability'])
                total += probability * best
                complete_weight += probability
            choices.append(dict(world_id=world['world_id'], action_id=best_action,
                                objective_cash_vnd=None if best is None else decimal_text(best)))
        return dict(expected_objective_cash_vnd=(decimal_text(total) if complete_weight == Decimal(1) else None),
                    complete_weight=decimal_text(complete_weight),
                    choices=choices,
                    warning='DIAGNOSTIC_UPPER_BOUND_NOT_ADMISSIBLE_POLICY')


def evaluate_policy_manifest(manifest):
    """Evaluate nonanticipative policy candidates on declared synthetic worlds."""
    _require(isinstance(manifest, dict), 'MANIFEST_REQUIRED')
    _require('manifest_version' in manifest, 'MANIFEST_VERSION_REQUIRED')
    _require(manifest['manifest_version'] == MANIFEST_VERSION,
             'UNSUPPORTED_MANIFEST_VERSION')
    required = {'manifest_version', 'scope', 'currency', 'decision_at', 'terminal_at',
                'mandate', 'provenance', 'forecast_run', 'policies', 'exclusions',
                'worlds'}
    _require(set(manifest) == required, 'MANIFEST_FIELDS')
    _require(manifest['scope'] == 'SYNTHETIC_POLICY_VERTICAL_SLICE', 'UNSUPPORTED_SCOPE')
    _require(manifest['currency'] == 'VND', 'VND_ONLY')
    _validate_provenance(manifest['provenance'])
    excluded_action_ids = _validate_exclusions(manifest['exclusions'])
    _require(isinstance(manifest['forecast_run'], dict), 'FORECAST_RUN_REQUIRED')
    labels = set(manifest['forecast_run'].get('labels', []))
    _require(labels, 'FORECAST_LABELS_REQUIRED')
    _require(isinstance(manifest['policies'], list) and manifest['policies'], 'POLICIES_REQUIRED')
    _require(isinstance(manifest['worlds'], list) and manifest['worlds'], 'WORLDS_REQUIRED')

    policy_ids = [_validate_policy(policy, labels) for policy in manifest['policies']]
    _require(len(set(policy_ids)) == len(policy_ids), 'DUPLICATE_POLICY_ID')
    policy_action_ids = {action_id for policy in manifest['policies']
                         for action_id in policy['action_by_label'].values()}
    _require(not excluded_action_ids.intersection(policy_action_ids),
             'EXCLUDED_ACTION_REFERENCED')

    with localcontext() as ctx:
        ctx.prec = 40
        probability_sum = Decimal(0)
        world_action_ids = set()
        for world in manifest['worlds']:
            _, probability = _validate_world(world, labels, manifest)
            probability_sum += probability
            world_action_ids.update(world['action_cases'])
        _require(probability_sum == Decimal(1), 'WORLD_PROBABILITY_SUM_NOT_ONE')
        _require(not excluded_action_ids.intersection(world_action_ids),
                 'EXCLUDED_ACTION_HAS_CASE')

    results = []
    for policy in manifest['policies']:
        outcomes = []
        for world in manifest['worlds']:
            label = world['observation_label']
            action_id = policy['action_by_label'][label]
            if action_id not in world['action_cases']:
                outcome = _invalid_outcome('ACTION_NOT_AVAILABLE:' + action_id)
            else:
                outcome = _assess_case(world['action_cases'][action_id], manifest['mandate'])
            outcomes.append(dict(world_id=world['world_id'], observation_label=label,
                                 probability=decimal_text(Decimal(world['probability'])),
                                 action_id=action_id, **outcome))
        results.append(dict(policy_id=policy['policy_id'], description=policy['description'],
                            outcomes=outcomes, summary=_aggregate_outcomes(outcomes)))

    warnings = ['SYNTHETIC_DECLARED_PATHS_ONLY',
                'NO_MARKET_DATA_OR_FORECAST_FIT',
                'INCOMPLETE_PATHS_RETAINED_NOT_DROPPED']
    if manifest['exclusions']:
        warnings.append('EXCLUDED_ACTIONS_RETAINED_NOT_RANKED')
    return dict(manifest_version=MANIFEST_VERSION,
                analysis_version=ANALYSIS_VERSION,
                scope=EXPORT_SCOPE,
                decision_at=manifest['decision_at'],
                terminal_at=manifest['terminal_at'],
                provenance=copy.deepcopy(manifest['provenance']),
                exclusions=copy.deepcopy(manifest['exclusions']),
                policy_results=results,
                selected_policy=_choose_policy(results),
                clairvoyant_upper_bound=_clairvoyant_upper_bound(manifest),
                warnings=warnings)


def create_decision_run_export(manifest):
    """Create a self-checking, deterministic export for one synthetic DecisionRun.

    The identifier binds the canonical input to the exact evaluator bundle. No
    timestamp is invented. Hashes detect accidental or unacknowledged changes;
    they do not prove that the declared input is true market data.
    """
    manifest_copy = copy.deepcopy(manifest)
    result = evaluate_policy_manifest(manifest_copy)
    manifest_sha256 = _sha256_json(manifest_copy)
    evaluator_sha256 = _evaluator_sha256()
    result_sha256 = _sha256_json(result)
    return dict(export_version=EXPORT_VERSION,
                decision_run_id=_decision_run_id(manifest_sha256, evaluator_sha256),
                analysis_version=ANALYSIS_VERSION,
                scope=EXPORT_SCOPE,
                numeric_tolerance=copy.deepcopy(EXACT_TOLERANCE),
                manifest_sha256=manifest_sha256,
                evaluator_sha256=evaluator_sha256,
                result_sha256=result_sha256,
                warnings=list(result['warnings']),
                manifest=manifest_copy,
                result=copy.deepcopy(result))


def replay_decision_run_export(exported):
    """Verify export integrity and recompute its result with the current core."""
    _require(isinstance(exported, dict) and set(exported) == EXPORT_FIELDS, 'EXPORT_FIELDS')
    _require(exported['export_version'] == EXPORT_VERSION, 'EXPORT_VERSION_MISMATCH')
    _require(exported['analysis_version'] == ANALYSIS_VERSION, 'ANALYSIS_VERSION_MISMATCH')
    _require(exported['scope'] == EXPORT_SCOPE, 'EXPORT_SCOPE_MISMATCH')
    _require(exported['numeric_tolerance'] == EXACT_TOLERANCE, 'NUMERIC_TOLERANCE_MISMATCH')

    manifest_sha256 = _sha256_json(exported['manifest'])
    _require(_same_digest(exported['manifest_sha256'], manifest_sha256),
             'MANIFEST_HASH_MISMATCH')
    evaluator_sha256 = _evaluator_sha256()
    _require(_same_digest(exported['evaluator_sha256'], evaluator_sha256),
             'EVALUATOR_HASH_MISMATCH')
    expected_id = _decision_run_id(manifest_sha256, evaluator_sha256)
    _require(_same_digest(exported['decision_run_id'], expected_id),
             'DECISION_RUN_ID_MISMATCH')

    stored_result_sha256 = _sha256_json(exported['result'])
    _require(_same_digest(exported['result_sha256'], stored_result_sha256),
             'RESULT_HASH_MISMATCH')
    recomputed = evaluate_policy_manifest(copy.deepcopy(exported['manifest']))
    _require(_canonical_json_bytes(recomputed) == _canonical_json_bytes(exported['result']),
             'CACHED_RESULT_MISMATCH')
    _require(exported['warnings'] == recomputed['warnings'], 'EXPORT_WARNINGS_MISMATCH')
    return dict(status='REPLAY_VERIFIED', decision_run_id=expected_id,
                manifest_sha256=manifest_sha256, evaluator_sha256=evaluator_sha256,
                result_sha256=_sha256_json(recomputed), result=recomputed)
