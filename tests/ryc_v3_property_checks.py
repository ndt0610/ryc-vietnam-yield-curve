"""Deterministic property checks for the RYC V3 accounting contract.

Unlike an example-based regression test, each check varies dates, settlement
lags, coupon amounts, curve levels and scenario probability scales.  These
are mathematical invariants of the declared MODEL_ONLY engine, never evidence
of investment performance or Vietnamese-market executability.
"""
from __future__ import annotations

import json
import math
import random
from copy import deepcopy
from datetime import date, timedelta

from ryc_v3_engine import analyse_payload


YEAR = 365.25
RNG = random.Random(20260912)


def settlement(day: date, lag: int) -> date:
    cursor, counted = day, 0
    if cursor.weekday() >= 5:
        raise AssertionError("Fixture decision must be a weekday.")
    while counted < lag:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            counted += 1
    return cursor


def pv_at(flows: list[tuple[date, float]], valuation: date, rate: float) -> float:
    return sum(amount * math.exp(-rate * (payment - valuation).days / YEAR) for payment, amount in flows)


def flat_holder_fixture(case: int) -> dict:
    decision = date(2025, 1, 2) + timedelta(days=7 * case)
    # All decision dates deliberately remain Thursdays, away from coupon dates.
    lag = case % 4
    value_date = settlement(decision, lag)
    rate = RNG.uniform(-0.01, 0.12)
    coupon_a, coupon_b, final_coupon = (round(RNG.uniform(0.1, 5.0), 5) for _ in range(3))
    flows = [
        (decision + timedelta(days=90), coupon_a),
        (decision + timedelta(days=270), coupon_b),
        (decision + timedelta(days=720), 100 + final_coupon),
    ]
    terminal = decision + timedelta(days=510)
    bid = pv_at(flows, value_date, rate)
    return {
        "mode": "holder",
        "decision_date": decision.isoformat(),
        "terminal_date": terminal.isoformat(),
        "position_face_value_vnd": 100_000_000,
        "epsilon_bps": 0,
        "minimum_robust_support": 1,
        "candidate_frequency": "business_day" if case % 2 else "monthly",
        "curve_value_date": value_date.isoformat(),
        "quote": {"bid_dirty_per_100": bid, "ask_dirty_per_100": bid, "value_date": value_date.isoformat()},
        "bond": {
            "identifier": f"PROPERTY-H-{case}",
            "issue_date": (decision - timedelta(days=365)).isoformat(),
            "cashflows": [{"payment_date": d.isoformat(), "amount_per_100": a} for d, a in flows],
        },
        "execution": {"buy_fee_bps": 0, "sell_fee_bps": 0, "settlement_lag_business_days": lag},
        "curve_nodes": [{"tenor_years": x, "zero_rate_decimal": rate} for x in (0.01, 1, 3, 5, 10)],
        "scenarios": [{"name": "flat", "probability": 1, "terminal_level_shift_bps": 0,
                       "terminal_slope_shift_bps": 0, "cash_rate_decimal": rate, "sell_haircut_bps": 0}],
    }


def flat_buyer_fixture(case: int) -> dict:
    decision = date(2025, 1, 2) + timedelta(days=7 * case)
    lag = case % 4
    value_date = settlement(decision, lag)
    rate = RNG.uniform(-0.01, 0.12)
    maturity, terminal = decision + timedelta(days=720), decision + timedelta(days=300)
    flows = [(maturity, 100.0)]
    bid = pv_at(flows, value_date, rate)
    return {
        "mode": "buyer",
        "decision_date": decision.isoformat(),
        "terminal_date": terminal.isoformat(),
        "position_face_value_vnd": 100_000_000,
        "epsilon_bps": 0,
        "minimum_robust_support": 1,
        "candidate_frequency": "business_day" if case % 2 else "monthly",
        "curve_value_date": value_date.isoformat(),
        "quote": {"bid_dirty_per_100": bid, "ask_dirty_per_100": bid, "value_date": value_date.isoformat()},
        "bond": {"identifier": f"PROPERTY-B-{case}", "issue_date": (decision - timedelta(days=365)).isoformat(),
                 "cashflows": [{"payment_date": maturity.isoformat(), "amount_per_100": 100}]},
        "execution": {"buy_fee_bps": case % 70, "sell_fee_bps": (case * 3) % 70,
                      "settlement_lag_business_days": lag},
        "curve_nodes": [{"tenor_years": x, "zero_rate_decimal": rate} for x in (0.01, 1, 3, 5, 10)],
        "scenarios": [{"name": "flat", "probability": 1, "terminal_level_shift_bps": 0,
                       "terminal_slope_shift_bps": 0, "cash_rate_decimal": rate, "sell_haircut_bps": 0}],
    }


def close(actual: float, expected: float, label: str, tolerance: float = 2e-7) -> None:
    if abs(actual - expected) > tolerance:
        raise AssertionError(f"{label}: {actual} vs {expected}")


def run() -> dict[str, object]:
    result: dict[str, object] = {}

    # A frictionless bond and cash account at the same flat continuously
    # compounded rate conserve terminal cash exactly. This holds across sale
    # dates, coupon dates and settlement lags; any deviation is phantom alpha.
    holder_cases = 32
    for case in range(holder_cases):
        payload = flat_holder_fixture(case)
        report = analyse_payload(payload)
        baseline = report["baseline_terminal_wealth"]["per_100_face"]
        if report["expected_maximiser"]["date"] != payload["decision_date"]:
            raise AssertionError("Flat-curve holder tie did not resolve to the earliest cash date.")
        payment_dates = {f["payment_date"] for f in payload["bond"]["cashflows"]}
        for candidate in report["candidates"]:
            close(candidate["expected_terminal_wealth"]["per_100_face"], baseline, f"holder flat conservation {case}")
            if candidate["settlement_date"] > payload["terminal_date"]:
                raise AssertionError("Candidate settles after H.")
            if candidate["execution_date"] in payment_dates:
                raise AssertionError("Candidate executes on a coupon payment date.")
            execution, settle = candidate["execution_date"], candidate["settlement_date"]
            if any(execution < payment <= settle for payment in payment_dates):
                raise AssertionError("Candidate settlement crosses an unmodelled coupon entitlement window.")
            ledger_dates = [x["cash_date"] for x in candidate["cash_ledger"]["flat"] if x["kind"] == "coupon"]
            if any(not (payload["decision_date"] < item < execution) for item in ledger_dates):
                raise AssertionError("Holder coupon ledger violates declared ownership rule.")
    result["holder_flat_curve_cash_conservation"] = {"pass": holder_cases, "seed": 20260912}

    # On the same flat curve an entry with non-negative costs must never beat
    # equal starting cash. Exact fee-free indifference resolves to no-buy.
    buyer_cases = 32
    for case in range(buyer_cases):
        report = analyse_payload(flat_buyer_fixture(case))
        baseline = report["baseline_terminal_wealth"]["per_100_face"]
        if report["expected_maximiser"]["kind"] != "no_buy_cash":
            raise AssertionError("No-buy did not resolve a flat-curve buyer tie/cost loss.")
        for candidate in report["candidates"]:
            if candidate["expected_terminal_wealth"]["per_100_face"] > baseline + 2e-7:
                raise AssertionError("Flat-curve buyer gained artificial edge over cash.")
    result["buyer_flat_curve_no_artificial_edge"] = {"pass": buyer_cases, "seed": 20260912}

    # Scenario weights are only relative weights. Rescaling every positive
    # weight, or appending a zero-weight extreme scenario, cannot alter any
    # displayed economic comparison.
    payload = flat_holder_fixture(3)
    payload["scenarios"] = [
        {"name": "up", "probability": .2, "terminal_level_shift_bps": 50, "terminal_slope_shift_bps": 10, "cash_rate_decimal": .03, "sell_haircut_bps": 10},
        {"name": "down", "probability": .8, "terminal_level_shift_bps": -25, "terminal_slope_shift_bps": -5, "cash_rate_decimal": .02, "sell_haircut_bps": 20},
    ]
    original = analyse_payload(payload)
    scaled = deepcopy(payload)
    for scenario in scaled["scenarios"]:
        scenario["probability"] *= 17
    with_zero = deepcopy(payload)
    with_zero["scenarios"].append({"name": "zero-weight-extreme", "probability": 0,
        "terminal_level_shift_bps": 9000, "terminal_slope_shift_bps": -9000,
        "cash_rate_decimal": -.5, "sell_haircut_bps": 9000})
    for comparison in (analyse_payload(scaled), analyse_payload(with_zero)):
        if comparison["expected_maximiser"] != original["expected_maximiser"]:
            raise AssertionError("Scenario probability representation changed the maximiser.")
        for left, right in zip(original["candidates"], comparison["candidates"]):
            close(left["expected_terminal_wealth"]["per_100_face"], right["expected_terminal_wealth"]["per_100_face"], "probability-scale invariance")
            close(left["maximum_regret"]["per_100_face"], right["maximum_regret"]["per_100_face"], "zero-weight regret invariance")
    result["scenario_weight_representation_invariance"] = "pass"

    return result


if __name__ == "__main__":
    print(json.dumps({"property_checks": run(), "scope": "deterministic synthetic accounting invariants; not empirical validation"}, ensure_ascii=False, indent=2))
