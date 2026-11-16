"""Independent economic checks of RYC; no price helper is used as an oracle.

Expected values come from closed-form discounting and signed cash identities.
These are mathematical checks of the declared model, not market backtests.
"""
from __future__ import annotations

import json
import math
from copy import deepcopy
from datetime import date, timedelta

from ryc_v3_engine import ModelInputError, analyse_payload


def fixture(a=0.02, b=0.004, cash=0.05, lag=0):
    decision, maturity = date(2025, 1, 2), date(2030, 1, 2)
    q0 = decision
    for _ in range(lag):
        q0 += timedelta(days=1)
        while q0.weekday() >= 5:
            q0 += timedelta(days=1)
    tenor = (maturity - q0).days / 365.25
    bid = 100 * math.exp(-(a + b * tenor) * tenor)
    return {
        "mode": "holder", "decision_date": decision.isoformat(),
        "terminal_date": "2028-01-04", "position_face_value_vnd": 100_000_000,
        "epsilon_bps": 0, "minimum_robust_support": 1,
        "candidate_frequency": "business_day", "curve_value_date": q0.isoformat(),
        "quote": {"bid_dirty_per_100": bid, "ask_dirty_per_100": bid, "value_date": q0.isoformat()},
        "bond": {"identifier": "SYNTHETIC-MATH-CHECK", "issue_date": "2024-01-02",
                 "cashflows": [{"payment_date": maturity.isoformat(), "amount_per_100": 100}]},
        "execution": {"buy_fee_bps": 0, "sell_fee_bps": 0, "settlement_lag_business_days": lag},
        "curve_nodes": [{"tenor_years": t, "zero_rate_decimal": a + b * t} for t in (0.01, 1, 3, 5, 10)],
        "scenarios": [{"name": "Stable", "probability": 1, "cash_rate_decimal": cash,
                       "terminal_level_shift_bps": 0, "terminal_slope_shift_bps": 0, "sell_haircut_bps": 0}],
    }


def near(actual, expected, label, tolerance=1e-8):
    if abs(actual - expected) > tolerance:
        raise AssertionError(f"{label}: {actual} differs from {expected}")


def run():
    results = {}
    # A curve observed at q0 must receive zero scenario shock at q0 and
    # the full declared shock at H. Check actual sale cash against a closed
    # form for both position states, including a weekend settlement gap.
    for mode in ('holder', 'buyer'):
        moving = fixture(a=0.03, b=0, cash=0.02, lag=3)
        moving['mode'] = mode
        moving['terminal_date'] = '2025-04-02'
        moving['candidate_frequency'] = 'monthly'
        moving['scenarios'][0]['terminal_level_shift_bps'] = 200
        q0 = date.fromisoformat(moving['curve_value_date'])
        h = date.fromisoformat(moving['terminal_date'])
        t = date.fromisoformat(moving['decision_date'])
        m = date(2030, 1, 2)
        checked = analyse_payload(moving)
        for candidate in checked['candidates']:
            q = date.fromisoformat(candidate['settlement_date'])
            progress = (q-q0).days / (h-q0).days
            price = 100 * math.exp(-(0.03 + 0.02*progress) * (m-q).days/365.25)
            value = price * math.exp(0.02*(h-q).days/365.25)
            if mode == 'buyer':
                outlay = moving['quote']['ask_dirty_per_100']
                value += outlay*(math.exp(0.02*(h-t).days/365.25)-math.exp(0.02*(h-q0).days/365.25))
            near(candidate['expected_terminal_wealth']['per_100_face'], value, 'settlement-anchored moving curve')
            if mode == 'holder' and q > q0:
                baseline = moving['quote']['bid_dirty_per_100']*math.exp(0.02*(h-q0).days/365.25)
                root = math.log(value/baseline)/((m-q).days/365.25)*10000
                near(candidate['rate_cushion']['by_scenario']['Stable']['additional_parallel_shift_bps'], root, 'moving curve cushion', 1e-6)
    results['moving_curve_price_and_cushion_share_value_date_anchor'] = 'pass'
    # Under z(tau)=a+b*tau and a stable curve, log W(q) has derivative
    # a+2*b*(T-q)-r and second derivative -2*b (time measured in years).
    # For b>0 the interior maximum has tau*=(r-a)/(2*b).
    p = fixture()
    result = analyse_payload(p)
    maturity = date(2030, 1, 2)
    terminal = date.fromisoformat(p["terminal_date"])
    exact = maturity - timedelta(days=(0.05 - 0.02) / (2 * 0.004) * 365.25)
    # Keep the unrounded datetime for a precise continuous-to-grid distance.
    optimal_ordinal = maturity.toordinal() - (0.05 - 0.02) / (2 * 0.004) * 365.25
    machine = date.fromisoformat(result["expected_best_exit_date"])
    if abs(machine.toordinal() - optimal_ordinal) > 3:
        raise AssertionError("Daily optimiser does not recover the analytic interior maximum.")
    for item in result["candidates"]:
        q = date.fromisoformat(item["settlement_date"])
        tau, remaining = (maturity - q).days / 365.25, (terminal - q).days / 365.25
        expected = 100 * math.exp(-(0.02 + 0.004 * tau) * tau + 0.05 * remaining)
        near(item["expected_terminal_wealth"]["per_100_face"], expected, "linear-curve closed form")
    results["analytic_interior_optimum"] = {"machine_date": machine.isoformat(), "distance_to_continuous_optimum_days": round(abs(machine.toordinal() - optimal_ordinal), 6)}

    # The rate cushion has an analytical root in this zero-coupon fixture.
    # It is the extra parallel shift at future settlement that makes future
    # W(H) equal the independently priced sell-now W(H).
    cushion_candidate = result["candidates"][1]
    cushion_q = date.fromisoformat(cushion_candidate["settlement_date"])
    tauq = (maturity - cushion_q).days / 365.25
    log_baseline = math.log(p["quote"]["bid_dirty_per_100"]) + 0.05 * (terminal - date.fromisoformat(p["curve_value_date"])).days / 365.25
    log_unshifted = math.log(100) - (0.02 + 0.004 * tauq) * tauq + 0.05 * (terminal - cushion_q).days / 365.25
    exact_cushion_bps = (log_unshifted - log_baseline) / tauq * 10_000
    displayed_cushion = cushion_candidate["rate_cushion"]["by_scenario"]["Stable"]["additional_parallel_shift_bps"]
    near(float(displayed_cushion), exact_cushion_bps, "rate cushion closed form", tolerance=1e-6)
    results["rate_cushion_matches_closed_form_breakeven"] = "pass"

    # When the cash alternative is weak, W grows throughout the horizon:
    # an optimiser must check the final admissible day, not stop at month 35.
    boundary = fixture(cash=0)
    boundary["candidate_frequency"] = "monthly"
    boundary_result = analyse_payload(boundary)
    if boundary_result["expected_best_exit_date"] != boundary["terminal_date"]:
        raise AssertionError("Monthly search omitted the maximum at the horizon boundary.")
    results["monthly_grid_includes_terminal_boundary"] = "pass"

    # b<0 makes log W convex; any stationary point is a MINIMUM. Comparing
    # only an interior derivative root would give the wrong sell decision.
    inverted = analyse_payload(fixture(a=0.06, b=-0.004, cash=0.03))
    endpoints = {inverted["candidates"][0]["execution_date"], inverted["candidates"][-1]["execution_date"]}
    if inverted["expected_best_exit_date"] not in endpoints:
        raise AssertionError("Inverted linear curve should maximise at an endpoint in this fixture.")
    results["inverted_curve_checks_maximum_not_stationary_root"] = "pass"

    # A flat, frictionless curve reinvested at the same rate produces the
    # SAME terminal wealth before/after coupons and across weekend lags.
    for lag in (0, 1, 3):
        flat = fixture(a=0.04, b=0, cash=0.04, lag=lag)
        flat["bond"]["cashflows"] = [
            {"payment_date": "2025-07-02", "amount_per_100": 2},
            {"payment_date": "2026-07-02", "amount_per_100": 2},
            {"payment_date": "2030-01-02", "amount_per_100": 102},
        ]
        q0 = date.fromisoformat(flat["quote"]["value_date"])
        bid = sum(f["amount_per_100"] * math.exp(-0.04 * (date.fromisoformat(f["payment_date"]) - q0).days / 365.25) for f in flat["bond"]["cashflows"])
        flat["quote"].update(bid_dirty_per_100=bid, ask_dirty_per_100=bid)
        flat_result = analyse_payload(flat)
        expected = bid * math.exp(0.04 * (terminal - q0).days / 365.25)
        if lag and not any((date.fromisoformat(x["settlement_date"]) - date.fromisoformat(x["execution_date"])).days > lag for x in flat_result["candidates"]):
            raise AssertionError("Weekend settlement was not exercised.")
        for item in flat_result["candidates"]:
            near(item["expected_terminal_wealth"]["per_100_face"], expected, "flat cash conservation")
            near(item["expected_edge_vs_baseline"]["per_100_face"], 0, "no artificial riding alpha")
    results["flat_coupon_curve_no_phantom_profit_all_lags"] = "pass"

    # A flat-curve tie should be economically indistinguishable at epsilon=0,
    # even though a deterministic date remains visible as the maximiser.
    flat_margin = flat_result["decision_margin"]
    if flat_margin["margin_exceeds_economic_tolerance"] is not False or flat_margin["expected_margin"]["per_100_face"] > 2e-7:
        raise AssertionError("Flat-curve numerical tie was presented as a material decision margin.")
    if flat_result["expected_maximiser"]["date"] != flat["decision_date"]:
        raise AssertionError("Flat-curve numerical tie did not resolve to the earliest cash date.")
    results["decision_margin_does_not_invent_distinction"] = "pass"

    # The displayed breakdown must be the actual ledger used for scoring.
    buyer = fixture(a=0.04, b=0, cash=0.04, lag=3)
    buyer["mode"] = "buyer"
    buyer["quote"]["ask_dirty_per_100"] += 0.4
    buyer["execution"]["buy_fee_bps"] = 5
    for report in (result, flat_result, analyse_payload(buyer)):
        for item in report["candidates"]:
            for scenario, flows in item["cash_ledger"].items():
                total = sum(f["terminal_contribution"]["per_100_face"] for f in flows)
                near(total, item["scenario_terminal_wealth_per_100"][scenario], "ledger sum")
                for flow in flows:
                    near(flow["cash_amount"]["per_100_face"] + flow["reinvestment_to_terminal"]["per_100_face"], flow["terminal_contribution"]["per_100_face"], "cash plus accumulation")
                near(total - report["baseline_by_scenario_per_100"][scenario], item["scenario_edge_vs_baseline_per_100"][scenario], "paired scenario edge")
    results["displayed_ledger_equals_scored_cash"] = "pass"

    # Costs must not turn an otherwise indifferent flat-curve buyer into a
    # purchase preference. No-buy is included in the support comparison.
    expensive = analyse_payload(buyer)
    if expensive["action"] == "MODEL_ENTER_IF_CONSTRAINTS_PASS":
        raise AssertionError("A frictional flat-curve purchase was preferred to cash.")
    if any(x["expected_edge_vs_baseline"]["per_100_face"] >= 0 for x in expensive["candidates"]):
        raise AssertionError("Buy spread/fees disappeared from cash comparison.")
    results["frictional_buyer_does_not_invent_purchase_edge"] = "pass"

    mismatched = deepcopy(p)
    mismatched["curve_value_date"] = "2025-01-03"
    try:
        analyse_payload(mismatched)
        raise AssertionError("Mismatched curve/quote value dates were accepted.")
    except ModelInputError as error:
        if "VALUE_DATE_MISMATCH" not in str(error):
            raise
    results["curve_quote_value_dates_must_match"] = "pass"

    # The stated objective is not allowed to be overridden by a failed
    # robustness diagnostic. Conversely, a buyer's cash alternative must win
    # when it has the greater expected terminal value.
    conflicting = fixture()
    conflicting["minimum_robust_support"] = 1
    conflicting["scenarios"] = [
        {"name": "rise", "probability": .5, "cash_rate_decimal": .01,
         "terminal_level_shift_bps": 1000, "terminal_slope_shift_bps": 0, "sell_haircut_bps": 0},
        {"name": "fall", "probability": .5, "cash_rate_decimal": .05,
         "terminal_level_shift_bps": -1000, "terminal_slope_shift_bps": 0, "sell_haircut_bps": 0},
    ]
    conflict_result = analyse_payload(conflicting)
    if conflict_result["representative_exit_date"] is not None or conflict_result["objective_audit"]["maximising_alternative"] != "sale":
        raise AssertionError("Robustness diagnostic overrode the declared expected-wealth objective.")
    results["objective_is_not_overridden_by_robustness_diagnostic"] = "pass"

    cash_wins = deepcopy(buyer)
    cash_wins["execution"].update(buy_fee_bps=100, sell_fee_bps=100)
    cash_result = analyse_payload(cash_wins)
    if cash_result["expected_maximiser"]["kind"] != "no_buy_cash" or cash_result["action"] != "MODEL_MAX_EXPECTED_NO_BUY_CASH":
        raise AssertionError("No-buy cash was not included in the buyer objective.")
    results["buyer_objective_includes_no_buy_cash"] = "pass"

    malformed = deepcopy(p)
    malformed["curve_nodes"].append("not-a-node")
    try:
        analyse_payload(malformed)
        raise AssertionError("Malformed curve row was silently dropped.")
    except ModelInputError:
        pass
    results["malformed_input_fails_closed"] = "pass"
    return results


if __name__ == "__main__":
    print(json.dumps({"mathematical_checks": run(), "scope": "synthetic analytical fixtures, not empirical validation"}, ensure_ascii=False, indent=2))
