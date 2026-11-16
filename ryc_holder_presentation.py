"""Read-only presentation model for a verified synthetic holder export."""
from __future__ import annotations

import copy

from ryc_holder_baseline_builder import replay_holder_baseline_export


PRESENTATION_VERSION = 'RYC-HOLDER-PRESENTATION-1'
PRESENTATION_SCOPE = 'DECLARED_SYNTHETIC_PRESENTATION_NOT_MARKET_ADVICE'


class HolderPresentationError(ValueError):
    """The verified export is outside the supported presentation contract."""


def _require(condition, reason):
    if not condition:
        raise HolderPresentationError(reason)


def _policy_action_map(manifest):
    mapping = {}
    for policy in manifest['policies']:
        actions = policy['action_by_label']
        _require(set(actions) == {'decision_now'}, 'UNSUPPORTED_PRESENTATION_LABELS')
        mapping[policy['policy_id']] = actions['decision_now']
    return mapping


def _present_actions(result, manifest):
    policy_actions = _policy_action_map(manifest)
    rows = []
    for policy in result['policy_results']:
        _require(len(policy['outcomes']) == 1, 'ONE_WORLD_BASELINE_REQUIRED')
        outcome = policy['outcomes'][0]
        action_id = policy_actions[policy['policy_id']]
        _require(outcome['action_id'] == action_id, 'POLICY_ACTION_MISMATCH')
        rows.append({
            'action_id': action_id,
            'policy_id': policy['policy_id'],
            'status': outcome['status'],
            'objective_cash_vnd': outcome['objective_cash_vnd'],
            'shortfall_vnd': outcome['shortfall_vnd'],
            'reasons': copy.deepcopy(outcome['reasons']),
            'terminal_components': copy.deepcopy(outcome['terminal_components']),
        })
    for exclusion in result['exclusions']:
        rows.append({
            'action_id': exclusion['action_id'],
            'policy_id': None,
            'status': 'EXCLUDED_' + exclusion['status'],
            'objective_cash_vnd': None,
            'shortfall_vnd': None,
            'reasons': [exclusion['reason_code']],
            'terminal_components': None,
        })
    return rows


def present_holder_baseline(exported):
    """Verify first, then expose only traceable values without recalculation."""
    replay_holder_baseline_export(exported)
    request = exported['request']
    decision_run = exported['decision_run']
    manifest = decision_run['manifest']
    result = decision_run['result']
    selected = result['selected_policy']
    warnings = list(dict.fromkeys(exported['warnings'] + result['warnings']))
    return {
        'presentation_version': PRESENTATION_VERSION,
        'scope': PRESENTATION_SCOPE,
        'holder_run_id': exported['holder_run_id'],
        'decision_run_id': exported['decision_run_id'],
        'evidence': {
            'evidence_scope': result['provenance']['evidence_scope'],
            'source_refs': copy.deepcopy(result['provenance']['source_refs']),
            'warnings': warnings,
        },
        'horizon': {
            'decision_at': request['decision_at'],
            'terminal_at': request['terminal_at'],
            'mandate': copy.deepcopy(request['mandate']),
        },
        'decision': {
            'status': selected['status'],
            'policy_id': selected.get('policy_id'),
            'policy_ids': copy.deepcopy(selected.get('policy_ids', [])),
            'expected_objective_cash_vnd': selected['expected_objective_cash_vnd'],
        },
        'sale': copy.deepcopy(request['sell_now']),
        'coupon_cashflows': copy.deepcopy(request['coupon_cashflows']),
        'redemption': copy.deepcopy(request['redemption']),
        'actions': _present_actions(result, manifest),
        'exclusions': copy.deepcopy(result['exclusions']),
        'integrity': {
            'request_sha256': exported['request_sha256'],
            'builder_sha256': exported['builder_sha256'],
            'manifest_sha256': decision_run['manifest_sha256'],
            'evaluator_sha256': decision_run['evaluator_sha256'],
            'result_sha256': decision_run['result_sha256'],
        },
    }
