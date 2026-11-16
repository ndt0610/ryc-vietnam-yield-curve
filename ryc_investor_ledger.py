"""P2 conditional event ledger. SYNTHETIC_ONLY, no HTTP/UI/market adapter yet."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from ryc_v3_engine import cash_accumulation_factor, ModelInputError


class LedgerError(ValueError):
    """The path is invalid/infeasible; no partial result may be ranked."""


def require(condition, reason):
    if not condition:
        raise LedgerError(reason)


def number(value, name, *, positive=False, signed=False):
    require(type(value) is int or isinstance(value, str), 'EXACT_DECIMAL_REQUIRED:'+name)
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise LedgerError('INVALID_NUMBER:'+name) from error
    require(result.is_finite() and (signed or (result > 0 if positive else result >= 0)), 'INVALID_NUMBER:'+name)
    require(abs(result) <= Decimal('1e18') and result.as_tuple().exponent >= -12,
            'UNSUPPORTED_NUMERIC_PRECISION:'+name)
    return result


def instant(value):
    try:
        parsed = datetime.fromisoformat(value)
        require(parsed.tzinfo is not None, 'TIMEZONE_REQUIRED')
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError) as error:
        raise LedgerError('EXPLICIT_INSTANT_REQUIRED') from error


def text(value):
    require(isinstance(value, str) and bool(value.strip()), 'IDENTIFIER_REQUIRED')
    return value


def decimal_text(value):
    """Canonical plain decimal for stable API/export equality."""
    rendered = format(value, 'f')
    if '.' in rendered:
        rendered = rendered.rstrip('0').rstrip('.')
    return '0' if rendered in ('', '-0') else rendered


FIELDS = {
    'ORDER_SELL': {'instrument', 'order_id', 'quantity'},
    'FILL_SELL': {'order_id', 'trade_id', 'quantity', 'net_proceeds_vnd'},
    'SETTLE_SELL': {'trade_id'},
    'CANCEL_SELL': {'order_id'},
    'FAIL_SETTLEMENT': {'trade_id'},
    'RELEASE_CASH': {'lot_id'},
    'BUY': {'instrument', 'quantity', 'cost_vnd'},
    'COUPON_ENTITLEMENT': {'claim_id', 'instrument', 'amount_vnd'},
    'RECEIVE_COUPON': {'claim_id'},
    'REDEEM_PRINCIPAL': {'instrument', 'claim_id', 'quantity', 'amount_vnd'},
    'RECEIVE_PRINCIPAL': {'claim_id'},
    'FEE': {'amount_vnd'},
}


class _Book:
    def __init__(self, case):
        self.cash = number(case['initial_cash_vnd'], 'initial_cash_vnd')
        require(isinstance(case['initial_positions'], dict), 'POSITIONS_OBJECT_REQUIRED')
        self.owned = {text(k): number(v, 'quantity') for k, v in case['initial_positions'].items()}
        self.reserved = {k: Decimal(0) for k in self.owned}
        self.orders, self.trades, self.lots = {}, {}, {}
        self.claims = {}
        self.principal_claims = {}
        self.redeemed = set()
        self.trace = []

    def apply(self, e):
        kind = e['kind']
        if kind == 'ORDER_SELL':
            name, oid = text(e['instrument']), text(e['order_id'])
            qty = number(e['quantity'], 'quantity', positive=True)
            require(oid not in self.orders, 'DUPLICATE_ORDER')
            require(name in self.owned and self.owned[name]-self.reserved[name] >= qty,
                    'INSUFFICIENT_UNRESERVED_POSITION')
            self.reserved[name] += qty
            self.orders[oid] = dict(instrument=name, unfilled=qty)
        elif kind == 'FILL_SELL':
            oid, tid = text(e['order_id']), text(e['trade_id'])
            require(oid in self.orders and tid not in self.trades, 'INVALID_TRADE_LINK')
            order = self.orders[oid]
            qty = number(e['quantity'], 'quantity', positive=True)
            proceeds = number(e['net_proceeds_vnd'], 'net_proceeds_vnd', positive=True)
            require(qty <= order['unfilled'], 'FILL_EXCEEDS_ORDER')
            order['unfilled'] -= qty
            self.trades[tid] = dict(instrument=order['instrument'], quantity=qty,
                                    net=proceeds, status='PENDING')
        elif kind == 'CANCEL_SELL':
            oid = text(e['order_id'])
            require(oid in self.orders, 'UNKNOWN_ORDER')
            order = self.orders[oid]
            require(order['unfilled'] > 0, 'NO_UNFILLED_QUANTITY')
            self.reserved[order['instrument']] -= order['unfilled']
            order['unfilled'] = Decimal(0)
        elif kind in ('SETTLE_SELL', 'FAIL_SETTLEMENT'):
            tid = text(e['trade_id'])
            require(tid in self.trades, 'UNKNOWN_TRADE')
            trade = self.trades[tid]
            require(trade['status'] in ('PENDING', 'FAILED_PENDING'), 'TRADE_NOT_PENDING')
            if kind == 'FAIL_SETTLEMENT':
                trade['status'] = 'FAILED_PENDING'
                return
            name, qty = trade['instrument'], trade['quantity']
            self.owned[name] -= qty
            self.reserved[name] -= qty
            self.lots['sale:'+tid] = dict(amount=trade['net'], status='LOCKED')
            trade['status'] = 'SETTLED'
        elif kind == 'COUPON_ENTITLEMENT':
            cid, name = text(e['claim_id']), text(e['instrument'])
            require(cid not in self.claims, 'DUPLICATE_COUPON_CLAIM')
            amount = number(e['amount_vnd'], 'amount_vnd', positive=True)
            self.claims[cid] = dict(instrument=name, amount=amount, status='PENDING')
        elif kind == 'RECEIVE_COUPON':
            cid = text(e['claim_id'])
            require(cid in self.claims and self.claims[cid]['status'] == 'PENDING', 'COUPON_NOT_PENDING')
            self.lots['coupon:'+cid] = dict(amount=self.claims[cid]['amount'], status='LOCKED')
            self.claims[cid]['status'] = 'RECEIVED'
        elif kind == 'REDEEM_PRINCIPAL':
            name, cid = text(e['instrument']), text(e['claim_id'])
            qty = number(e['quantity'], 'quantity', positive=True)
            amount = number(e['amount_vnd'], 'amount_vnd', positive=True)
            require(name not in self.redeemed, 'INSTRUMENT_REDEEMED')
            require(cid not in self.principal_claims, 'DUPLICATE_PRINCIPAL_CLAIM')
            require(name in self.owned and self.reserved[name] == 0,
                    'REDEMPTION_REQUIRES_UNCOMMITTED_POSITION')
            require(qty == self.owned[name], 'FULL_REDEMPTION_REQUIRED')
            # Declared extinguishment, not an inferred maturity/entitlement rule.
            # Principal only: the final coupon must use its separate claim.
            self.owned[name] -= qty
            self.redeemed.add(name)
            self.principal_claims[cid] = dict(instrument=name, amount=amount, status='PENDING')
        elif kind == 'RECEIVE_PRINCIPAL':
            cid = text(e['claim_id'])
            require(cid in self.principal_claims and self.principal_claims[cid]['status'] == 'PENDING',
                    'PRINCIPAL_NOT_PENDING')
            self.lots['principal:'+cid] = dict(amount=self.principal_claims[cid]['amount'], status='LOCKED')
            self.principal_claims[cid]['status'] = 'RECEIVED'
        elif kind == 'RELEASE_CASH':
            lid = text(e['lot_id'])
            require(lid in self.lots and self.lots[lid]['status'] == 'LOCKED', 'LOT_NOT_LOCKED')
            lot = self.lots[lid]
            self.cash += lot['amount']
            lot['status'] = 'AVAILABLE'
        elif kind == 'FEE':
            fee = number(e['amount_vnd'], 'amount_vnd', positive=True)
            require(self.cash >= fee, 'INSUFFICIENT_AVAILABLE_CASH')
            self.cash -= fee
        elif kind == 'BUY':
            name = text(e['instrument'])
            require(name not in self.redeemed, 'INSTRUMENT_REDEEMED')
            qty = number(e['quantity'], 'quantity', positive=True)
            cost = number(e['cost_vnd'], 'cost_vnd', positive=True)
            require(self.cash >= cost, 'INSUFFICIENT_AVAILABLE_CASH')
            self.cash -= cost
            self.owned[name] = self.owned.get(name, Decimal(0)) + qty
            self.reserved.setdefault(name, Decimal(0))

    def snapshot(self):
        return dict(cash_available_vnd=decimal_text(self.cash),
                    cash_locked_vnd=decimal_text(sum((v['amount'] for v in self.lots.values()
                                                      if v['status'] == 'LOCKED'), Decimal(0))),
                    pending_sale_receivables_vnd=decimal_text(sum((v['net'] for v in self.trades.values()
                                                                   if v['status'] != 'SETTLED'), Decimal(0))),
                    pending_coupon_receivables_vnd=decimal_text(sum((v['amount'] for v in self.claims.values()
                                                                     if v['status'] == 'PENDING'), Decimal(0))),
                    pending_principal_receivables_vnd=decimal_text(sum((v['amount'] for v in self.principal_claims.values()
                                                                        if v['status'] == 'PENDING'), Decimal(0))),
                    positions={k: dict(owned=decimal_text(v), committed_or_reserved=decimal_text(self.reserved[k]),
                                       freely_available=decimal_text(v-self.reserved[k]))
                               for k, v in self.owned.items()})

    def obligations(self):
        return dict(trades={k: dict(instrument=v['instrument'], status=v['status'],
                                    delivery_quantity=decimal_text(v['quantity']) if v['status'] != 'SETTLED' else '0',
                                    net_proceeds_vnd=decimal_text(v['net'])) for k, v in self.trades.items()},
                    unfilled_orders={k: decimal_text(v['unfilled']) for k, v in self.orders.items()})

    def accrue(self, start, end, rate):
        if start == end:
            return
        try:
            factor = cash_accumulation_factor(start, end, rate)
        except ModelInputError as error:
            raise LedgerError(str(error)) from error
        interest = self.cash * (Decimal(str(factor))-1)
        self.cash += interest
        self.trace.append(dict(kind='MODEL_INTEREST', start=start.isoformat(), end=end.isoformat(),
                               interest_vnd=decimal_text(interest), **self.snapshot()))


def replay_investor_ledger(case):
    """Replay a declared synthetic path; unavailable cash never finances a buy."""
    required = {'scope', 'currency', 'decision_at', 'terminal_at', 'initial_cash_vnd',
                'initial_positions', 'cash_rate_decimal', 'events'}
    require(isinstance(case, dict) and set(case) == required, 'CASE_FIELDS')
    require(case['scope'] == 'SYNTHETIC_ONLY' and case['currency'] == 'VND', 'SYNTHETIC_VND_ONLY')
    start, terminal = instant(case['decision_at']), instant(case['terminal_at'])
    require(start < terminal, 'TERMINAL_AFTER_DECISION_REQUIRED')
    rate = float(number(case['cash_rate_decimal'], 'cash_rate_decimal', signed=True))
    require(isinstance(case['events'], list), 'EVENT_LIST_REQUIRED')
    with localcontext() as ctx:
        ctx.prec = 40
        book = _Book(case)
        previous = last_applied = start
        seen = {}
        for e in case['events']:
            require(isinstance(e, dict) and isinstance(e.get('kind'), str)
                    and e['kind'] in FIELDS, 'EVENT_TYPE')
            require(set(e) == FIELDS[e['kind']] | {'event_id', 'kind', 'at'}, 'EVENT_FIELDS')
            for key in FIELDS[e['kind']]:
                if key in ('quantity', 'net_proceeds_vnd', 'cost_vnd', 'amount_vnd'):
                    number(e[key], key, positive=True)
                else:
                    text(e[key])
            eid, at = text(e['event_id']), instant(e['at'])
            if eid in seen:
                require(seen[eid] == e, 'CONFLICTING_EVENT_ID')
                continue
            require(previous <= at, 'EVENT_ORDER')
            seen[eid] = dict(e)
            previous = at
            if at > terminal:
                continue
            book.accrue(last_applied, at, rate)
            book.apply(e)
            last_applied = at
            book.trace.append(dict(event_id=eid, kind=e['kind'], at=e['at'], **book.snapshot()))
        book.accrue(last_applied, terminal, rate)
        return dict(version='RYC-P2-EVENT-LEDGER-1', scope='SYNTHETIC_ONLY',
                    eligible_for_executable=False, eligible_for_curve_pilot=False,
                    **book.snapshot(), **book.obligations(), trace=book.trace)


def assess_terminal_cash(case, mandate):
    """Assess one declared full-cash-exit mandate, retaining all failed states.

    Not a ranker: a downstream risk/policy evaluator must not discard failures.
    Residual assets are reported, never assigned zero economic value.
    """
    state = replay_investor_ledger(case)
    result = dict(state=state, status='MANDATE_REQUIRED', reasons=[],
                  shortfall_vnd=None, objective_cash_vnd=None)
    if mandate is None:
        result['reasons'] = ['MANDATE_REQUIRED']
        return result
    require(isinstance(mandate, dict) and set(mandate) == {'mode', 'minimum_cash_vnd'}, 'MANDATE_FIELDS')
    require(mandate['mode'] == 'FULL_CASH_EXIT', 'UNSUPPORTED_TERMINAL_MANDATE')
    minimum = number(mandate['minimum_cash_vnd'], 'minimum_cash_vnd')
    cash = Decimal(state['cash_available_vnd'])
    reasons = []
    if any(Decimal(p['owned']) != 0 for p in state['positions'].values()):
        reasons.append('RESIDUAL_POSITION')
    for field in ('cash_locked_vnd', 'pending_sale_receivables_vnd', 'pending_coupon_receivables_vnd',
                  'pending_principal_receivables_vnd'):
        if Decimal(state[field]) != 0:
            reasons.append(field.upper())
    if any(instant(e['at']) > instant(case['terminal_at']) for e in case['events']):
        reasons.append('POST_H_EVENTS_UNRESOLVED')
    with localcontext() as ctx:
        ctx.prec = 40
        shortfall = max(Decimal(0), minimum-cash)
    if shortfall > 0:
        reasons.append('CASH_SHORTFALL')
    result.update(reasons=reasons, shortfall_vnd=decimal_text(shortfall),
                  status=('CASH_EXIT_SATISFIED' if not reasons else
                          'SHORTFALL' if reasons == ['CASH_SHORTFALL'] else 'INCOMPLETE_CASH_EXIT'))
    if not reasons:
        result['objective_cash_vnd'] = decimal_text(cash)
    return result
