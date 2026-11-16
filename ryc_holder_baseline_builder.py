"""Build a declared synthetic SELL_NOW versus HOLD baseline DecisionRun.

The builder translates explicit holder facts into P2 event-ledger cases.  It
does not derive a sale price from a curve, infer coupon entitlement, or claim
that the input is qualified market evidence.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ryc_policy_vertical_slice import (MANIFEST_VERSION, create_decision_run_export,
                                       replay_decision_run_export)


BUILDER_VERSION = 'RYC-HOLDER-BASELINE-BUILDER-1'
BUILDER_SCOPE = 'SYNTHETIC_HOLDER_BASELINE'
HOLDER_EXPORT_VERSION = 'RYC-HOLDER-BASELINE-EXPORT-1'
HOLDER_EXPORT_SCOPE = 'DECLARED_SYNTHETIC_HOLDER_BASELINE_NOT_MARKET_VALIDATION'
HOLDER_EXPORT_FIELDS = {
    'export_version', 'holder_run_id', 'builder_version', 'scope',
    'request_sha256', 'builder_sha256', 'decision_run_id', 'warnings',
    'request', 'decision_run',
}
REQUEST_FIELDS = {
    'builder_version', 'scope', 'currency', 'decision_at', 'terminal_at',
    'mandate', 'instrument', 'initial_cash_vnd', 'cash_rate_decimal',
    'source_refs', 'sell_now', 'coupon_cashflows', 'redemption',
}
SELL_FIELDS = {
    'quote_id', 'source_ref_id', 'quote_observed_at', 'quote_expires_at',
    'order_at', 'fill_at', 'settlement_at', 'release_at', 'quantity',
    'gross_proceeds_vnd', 'fees_vnd', 'net_proceeds_vnd',
}
REDEMPTION_FIELDS = {
    'claim_id', 'redemption_at', 'receive_at', 'release_at', 'quantity',
    'amount_vnd',
}
COUPON_FIELDS = {
    'claim_id', 'entitlement_at', 'receive_at', 'release_at', 'amount_vnd',
    'holder_entitled_if_sell_now',
}


class HolderBaselineBuilderError(ValueError):
    """The declared holder baseline cannot be materialized safely."""


def _require(condition, reason):
    if not condition:
        raise HolderBaselineBuilderError(reason)


def _text(value, reason):
    _require(isinstance(value, str) and bool(value.strip()), reason)
    return value


def _number(value, name, *, positive=False, signed=False):
    _require(type(value) is int or isinstance(value, str),
             'EXACT_DECIMAL_REQUIRED:' + name)
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise HolderBaselineBuilderError('INVALID_NUMBER:' + name) from error
    valid_sign = signed or (parsed > 0 if positive else parsed >= 0)
    _require(parsed.is_finite() and valid_sign, 'INVALID_NUMBER:' + name)
    _require(abs(parsed) <= Decimal('1e18') and parsed.as_tuple().exponent >= -12,
             'UNSUPPORTED_NUMERIC_PRECISION:' + name)
    return parsed


def _instant(value, reason='EXPLICIT_INSTANT_REQUIRED'):
    try:
        parsed = datetime.fromisoformat(value)
        _require(parsed.tzinfo is not None, 'TIMEZONE_REQUIRED')
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError) as error:
        raise HolderBaselineBuilderError(reason) from error


def _canonical_json_bytes(value):
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False,
                             sort_keys=True, separators=(',', ':')).encode('utf-8')
    except (TypeError, ValueError) as error:
        raise HolderBaselineBuilderError('CANONICAL_JSON_REQUIRED') from error
    return encoded


def _input_sha256(request):
    return hashlib.sha256(_canonical_json_bytes(request)).hexdigest()


def _builder_sha256():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _same_digest(left, right):
    return (isinstance(left, str) and isinstance(right, str)
            and hmac.compare_digest(left, right))


def _holder_run_id(request_sha256, builder_sha256, decision_run_id):
    identity = {
        'export_version': HOLDER_EXPORT_VERSION,
        'builder_version': BUILDER_VERSION,
        'request_sha256': request_sha256,
        'builder_sha256': builder_sha256,
        'decision_run_id': decision_run_id,
    }
    return 'RYC-HOLDER-' + hashlib.sha256(_canonical_json_bytes(identity)).hexdigest()


def _event(event_id, kind, at, **fields):
    return dict(event_id=event_id, kind=kind, at=at, **fields)


def _ordered_events(events):
    priority = {
        'ORDER_SELL': 0, 'FILL_SELL': 1, 'SETTLE_SELL': 2,
        'COUPON_ENTITLEMENT': 3, 'REDEEM_PRINCIPAL': 3,
        'RECEIVE_COUPON': 4, 'RECEIVE_PRINCIPAL': 4, 'RELEASE_CASH': 5,
    }
    return sorted(events, key=lambda row: (_instant(row['at']), priority[row['kind']],
                                            row['event_id']))


def _case(request, events):
    instrument = request['instrument']
    return {
        'scope': 'SYNTHETIC_ONLY',
        'currency': 'VND',
        'decision_at': request['decision_at'],
        'terminal_at': request['terminal_at'],
        'initial_cash_vnd': request['initial_cash_vnd'],
        'initial_positions': {
            instrument['instrument_id']: instrument['quantity'],
        },
        'cash_rate_decimal': request['cash_rate_decimal'],
        'events': events,
    }


def _sale_events(request):
    sale = request['sell_now']
    instrument = request['instrument']['instrument_id']
    events = [
        _event('sell:order', 'ORDER_SELL', sale['order_at'], instrument=instrument,
               order_id='sell-now', quantity=sale['quantity']),
        _event('sell:fill', 'FILL_SELL', sale['fill_at'], order_id='sell-now',
               trade_id='sell-now', quantity=sale['quantity'],
               net_proceeds_vnd=sale['net_proceeds_vnd']),
        _event('sell:settle', 'SETTLE_SELL', sale['settlement_at'],
               trade_id='sell-now'),
        _event('sell:release', 'RELEASE_CASH', sale['release_at'],
               lot_id='sale:sell-now'),
    ]
    events.extend(_coupon_events(request, sell_now=True))
    return _ordered_events(events)


def _coupon_events(request, *, sell_now):
    instrument = request['instrument']['instrument_id']
    events = []
    for coupon in request['coupon_cashflows']:
        if sell_now and not coupon['holder_entitled_if_sell_now']:
            continue
        claim_id = coupon['claim_id']
        prefix = ('sell' if sell_now else 'hold') + ':coupon:' + claim_id
        events.extend([
            _event(prefix + ':entitle', 'COUPON_ENTITLEMENT',
                   coupon['entitlement_at'], instrument=instrument,
                   claim_id=claim_id, amount_vnd=coupon['amount_vnd']),
            _event(prefix + ':receive', 'RECEIVE_COUPON', coupon['receive_at'],
                   claim_id=claim_id),
            _event(prefix + ':release', 'RELEASE_CASH', coupon['release_at'],
                   lot_id='coupon:' + claim_id),
        ])
    return events


def _hold_events(request):
    redemption = request['redemption']
    instrument = request['instrument']['instrument_id']
    claim_id = redemption['claim_id']
    events = _coupon_events(request, sell_now=False)
    events.extend([
        _event('hold:principal:redeem', 'REDEEM_PRINCIPAL',
               redemption['redemption_at'], instrument=instrument,
               claim_id=claim_id, quantity=redemption['quantity'],
               amount_vnd=redemption['amount_vnd']),
        _event('hold:principal:receive', 'RECEIVE_PRINCIPAL',
               redemption['receive_at'], claim_id=claim_id),
        _event('hold:principal:release', 'RELEASE_CASH',
               redemption['release_at'], lot_id='principal:' + claim_id),
    ])
    return _ordered_events(events)


def _validate_request(request):
    _require(isinstance(request, dict) and set(request) == REQUEST_FIELDS,
             'BUILDER_REQUEST_FIELDS')
    _require(request['builder_version'] == BUILDER_VERSION,
             'UNSUPPORTED_BUILDER_VERSION')
    _require(request['scope'] == BUILDER_SCOPE, 'UNSUPPORTED_BUILDER_SCOPE')
    _require(request['currency'] == 'VND', 'VND_ONLY')
    decision = _instant(request['decision_at'])
    terminal = _instant(request['terminal_at'])
    _require(decision < terminal, 'TERMINAL_AFTER_DECISION_REQUIRED')
    _require(isinstance(request['mandate'], dict), 'MANDATE_REQUIRED')
    _number(request['initial_cash_vnd'], 'initial_cash_vnd')
    _number(request['cash_rate_decimal'], 'cash_rate_decimal', signed=True)
    _require(isinstance(request['source_refs'], list), 'SOURCE_REFS_REQUIRED')
    source_ids = []
    for source_ref in request['source_refs']:
        _require(isinstance(source_ref, dict) and 'source_id' in source_ref,
                 'SOURCE_REF_FIELDS')
        source_ids.append(_text(source_ref['source_id'], 'SOURCE_ID_REQUIRED'))
    _require(len(source_ids) == len(set(source_ids)), 'DUPLICATE_SOURCE_ID')
    _require(isinstance(request['coupon_cashflows'], list),
             'COUPON_CASHFLOWS_REQUIRED')

    instrument = request['instrument']
    _require(isinstance(instrument, dict)
             and set(instrument) == {'instrument_id', 'quantity'},
             'INSTRUMENT_FIELDS')
    _text(instrument['instrument_id'], 'INSTRUMENT_ID_REQUIRED')
    quantity = _number(instrument['quantity'], 'instrument.quantity', positive=True)

    sale = request['sell_now']
    sell_exclusion = None
    if sale is None:
        sell_exclusion = {
            'action_id': 'SELL_NOW',
            'status': 'DATA_INSUFFICIENT',
            'reason_code': 'SELL_EXECUTION_REQUIRED',
            'description': ('No explicit executable sale quote and cash timeline '
                            'were declared at the decision instant.'),
        }
    else:
        _require(isinstance(sale, dict) and set(sale) == SELL_FIELDS,
                 'SELL_NOW_FIELDS')
        _text(sale['quote_id'], 'QUOTE_ID_REQUIRED')
        _require(sale['source_ref_id'] is None
                 or isinstance(sale['source_ref_id'], str), 'SOURCE_REF_ID')
        _require(sale['source_ref_id'] is None
                 or sale['source_ref_id'] in source_ids,
                 'UNKNOWN_SELL_SOURCE_REF')
        observed = _instant(sale['quote_observed_at'])
        expires = _instant(sale['quote_expires_at'])
        order = _instant(sale['order_at'])
        fill = _instant(sale['fill_at'])
        settlement = _instant(sale['settlement_at'])
        release = _instant(sale['release_at'])
        _require(observed <= decision and observed <= expires,
                 'QUOTE_OBSERVATION_ORDER')
        _require(order == decision, 'SELL_NOW_ORDER_AT_DECISION_REQUIRED')
        _require(order <= fill <= settlement <= release, 'SELL_TIMELINE_ORDER')
        _require(_number(sale['quantity'], 'sell.quantity', positive=True) == quantity,
                 'FULL_HOLDING_SALE_REQUIRED')
        gross = _number(sale['gross_proceeds_vnd'], 'gross_proceeds_vnd', positive=True)
        fees = _number(sale['fees_vnd'], 'fees_vnd')
        net = _number(sale['net_proceeds_vnd'], 'net_proceeds_vnd', positive=True)
        _require(gross - fees == net, 'SALE_NET_IDENTITY_MISMATCH')
        if expires < decision:
            sell_exclusion = {
                'action_id': 'SELL_NOW',
                'status': 'INFEASIBLE',
                'reason_code': 'QUOTE_EXPIRED_AT_DECISION',
                'description': ('The declared sale quote expired before the decision '
                                'instant and cannot be ranked.'),
            }

    redemption = request['redemption']
    _require(isinstance(redemption, dict) and set(redemption) == REDEMPTION_FIELDS,
             'REDEMPTION_FIELDS')
    _text(redemption['claim_id'], 'PRINCIPAL_CLAIM_ID_REQUIRED')
    redeem_at = _instant(redemption['redemption_at'])
    receive_at = _instant(redemption['receive_at'])
    release_at = _instant(redemption['release_at'])
    _require(decision <= redeem_at <= receive_at <= release_at,
             'REDEMPTION_TIMELINE_ORDER')
    _require(_number(redemption['quantity'], 'redemption.quantity', positive=True)
             == quantity, 'FULL_REDEMPTION_REQUIRED')
    _number(redemption['amount_vnd'], 'redemption.amount_vnd', positive=True)

    coupon_claims = []
    for coupon in request['coupon_cashflows']:
        _require(isinstance(coupon, dict) and set(coupon) == COUPON_FIELDS,
                 'COUPON_FIELDS')
        claim_id = _text(coupon['claim_id'], 'COUPON_CLAIM_ID_REQUIRED')
        coupon_claims.append(claim_id)
        entitlement_at = _instant(coupon['entitlement_at'])
        receive_at = _instant(coupon['receive_at'])
        release_at = _instant(coupon['release_at'])
        _require(decision <= entitlement_at <= receive_at <= release_at,
                 'COUPON_TIMELINE_ORDER')
        _number(coupon['amount_vnd'], 'coupon.amount_vnd', positive=True)
        _require(type(coupon['holder_entitled_if_sell_now']) is bool,
                 'EXPLICIT_SELL_COUPON_ENTITLEMENT_REQUIRED')
    _require(len(coupon_claims) == len(set(coupon_claims)),
             'DUPLICATE_COUPON_CLAIM')
    return sell_exclusion


def build_holder_baseline_decision(request):
    """Return one deterministic P10 DecisionRun from explicit synthetic facts."""
    request = copy.deepcopy(request)
    sell_exclusion = _validate_request(request)
    actions = {'HOLD_TO_REDEMPTION': _case(request, _hold_events(request))}
    policies = [
        {'policy_id': 'HOLD_TO_REDEMPTION_POLICY',
         'description': 'Hold the declared instrument through explicit principal cash release.',
         'action_by_label': {'decision_now': 'HOLD_TO_REDEMPTION'}},
    ]
    exclusions = []
    if sell_exclusion is None:
        actions['SELL_NOW'] = _case(request, _sale_events(request))
        policies.insert(0, {
            'policy_id': 'SELL_NOW_POLICY',
            'description': 'Sell the full declared holding using the explicit net execution.',
            'action_by_label': {'decision_now': 'SELL_NOW'},
        })
    else:
        exclusions.append(sell_exclusion)
    manifest = {
        'manifest_version': MANIFEST_VERSION,
        'scope': 'SYNTHETIC_POLICY_VERTICAL_SLICE',
        'currency': 'VND',
        'decision_at': request['decision_at'],
        'terminal_at': request['terminal_at'],
        'mandate': copy.deepcopy(request['mandate']),
        'provenance': {
            'producer': 'ryc_holder_baseline_builder',
            'producer_version': BUILDER_VERSION,
            'evidence_scope': 'DECLARED_SYNTHETIC_ONLY',
            'input_sha256': _input_sha256(request),
            'source_refs': copy.deepcopy(request['source_refs']),
        },
        'forecast_run': {
            'scope': 'NO_FORECAST_CURRENT_ACTION_BASELINE',
            'information_cutoff': request['decision_at'],
            'labels': ['decision_now'],
        },
        'policies': policies,
        'exclusions': exclusions,
        'worlds': [{
            'world_id': 'declared_current_baseline',
            'probability': 1,
            'observation_label': 'decision_now',
            'action_cases': actions,
        }],
    }
    return create_decision_run_export(manifest)


def create_holder_baseline_export(request):
    """Retain exact builder input and bind it to the resulting DecisionRun."""
    request_copy = copy.deepcopy(request)
    decision_run = build_holder_baseline_decision(request_copy)
    request_sha256 = _input_sha256(request_copy)
    builder_sha256 = _builder_sha256()
    decision_run_id = decision_run['decision_run_id']
    return {
        'export_version': HOLDER_EXPORT_VERSION,
        'holder_run_id': _holder_run_id(request_sha256, builder_sha256,
                                        decision_run_id),
        'builder_version': BUILDER_VERSION,
        'scope': HOLDER_EXPORT_SCOPE,
        'request_sha256': request_sha256,
        'builder_sha256': builder_sha256,
        'decision_run_id': decision_run_id,
        'warnings': ['DECLARED_SYNTHETIC_INPUT_ONLY',
                     'EXACT_BUILDER_REQUEST_RETAINED',
                     'NOT_MARKET_OR_FORECAST_VALIDATION'],
        'request': request_copy,
        'decision_run': decision_run,
    }


def replay_holder_baseline_export(exported):
    """Verify request, builder and inner DecisionRun, then rebuild exactly."""
    _require(isinstance(exported, dict) and set(exported) == HOLDER_EXPORT_FIELDS,
             'HOLDER_EXPORT_FIELDS')
    _require(exported['export_version'] == HOLDER_EXPORT_VERSION,
             'HOLDER_EXPORT_VERSION_MISMATCH')
    _require(exported['builder_version'] == BUILDER_VERSION,
             'BUILDER_VERSION_MISMATCH')
    _require(exported['scope'] == HOLDER_EXPORT_SCOPE, 'HOLDER_EXPORT_SCOPE_MISMATCH')
    request_sha256 = _input_sha256(exported['request'])
    _require(_same_digest(exported['request_sha256'], request_sha256),
             'REQUEST_HASH_MISMATCH')
    builder_sha256 = _builder_sha256()
    _require(_same_digest(exported['builder_sha256'], builder_sha256),
             'BUILDER_HASH_MISMATCH')
    replayed_decision = replay_decision_run_export(exported['decision_run'])
    decision_run_id = replayed_decision['decision_run_id']
    _require(_same_digest(exported['decision_run_id'], decision_run_id),
             'INNER_DECISION_RUN_ID_MISMATCH')
    expected_holder_id = _holder_run_id(request_sha256, builder_sha256,
                                        decision_run_id)
    _require(_same_digest(exported['holder_run_id'], expected_holder_id),
             'HOLDER_RUN_ID_MISMATCH')
    rebuilt = create_holder_baseline_export(copy.deepcopy(exported['request']))
    _require(_canonical_json_bytes(rebuilt) == _canonical_json_bytes(exported),
             'HOLDER_CACHED_RESULT_MISMATCH')
    return {
        'status': 'HOLDER_BASELINE_REPLAY_VERIFIED',
        'holder_run_id': exported['holder_run_id'],
        'request_sha256': request_sha256,
        'builder_sha256': builder_sha256,
        'decision_run_id': decision_run_id,
        'decision_result': replayed_decision['result'],
    }
