"""Reproducible synthetic cases explaining the RYC decision mechanism.

The cases are deliberately mathematical, not Vietnam market data. They show
why curve slope alone cannot be a buy/sell rule once the cash alternative and
curve movement are part of the terminal-wealth ledger.
"""
from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path

from ryc_v3_engine import analyse_payload


OUT = Path(__file__).with_name("RYC_V3_SYNTHETIC_STUDY_RESULTS.json")
DECISION, MATURITY, TERMINAL = date(2025, 1, 2), date(2030, 1, 2), date(2028, 1, 4)


def payload(name: str, *, level: float, slope: float, cash: float, level_shift_bps: float = 0, epsilon_bps: float = 0) -> dict:
    tenor = (MATURITY - DECISION).days / 365.25
    bid = 100 * math.exp(-(level + slope * tenor) * tenor)
    return {
        "mode": "holder", "decision_date": DECISION.isoformat(), "terminal_date": TERMINAL.isoformat(),
        "position_face_value_vnd": 100_000_000, "candidate_frequency": "business_day",
        "epsilon_bps": epsilon_bps, "minimum_robust_support": 1,
        "curve_value_date": DECISION.isoformat(),
        "quote": {"bid_dirty_per_100": bid, "ask_dirty_per_100": bid, "value_date": DECISION.isoformat()},
        "bond": {"identifier": name, "issue_date": "2024-01-02",
                 "cashflows": [{"payment_date": MATURITY.isoformat(), "amount_per_100": 100}]},
        "execution": {"buy_fee_bps": 0, "sell_fee_bps": 0, "settlement_lag_business_days": 0},
        "curve_nodes": [{"tenor_years": x, "zero_rate_decimal": level + slope * x} for x in (0.01, 1, 3, 5, 10)],
        "scenarios": [{"name": "declared case", "probability": 1, "terminal_level_shift_bps": level_shift_bps,
                       "terminal_slope_shift_bps": 0, "cash_rate_decimal": cash, "sell_haircut_bps": 0}],
    }


def compact(label: str, report: dict) -> dict:
    margin = report["decision_margin"]
    return {
        "case": label,
        "objective": report["objective_audit"]["objective"],
        "action": report["action"],
        "selected_date": report["expected_maximiser"]["date"],
        "selected_terminal_wealth_per_100": report["expected_maximiser"]["expected_terminal_wealth"]["per_100_face"],
        "runner_up_date": margin["runner_up_date"],
        "decision_margin_per_100": margin["expected_margin"]["per_100_face"],
        "margin_exceeds_epsilon": margin["margin_exceeds_economic_tolerance"],
    }


def run() -> dict:
    cases = [
        ("upward_static_curve_interior_exit", payload("UPWARD", level=.02, slope=.004, cash=.05, epsilon_bps=1)),
        ("upward_curve_high_cash_rate_exit_now", payload("HIGH-CASH", level=.02, slope=.004, cash=.07)),
        ("upward_curve_rises_exit_now", payload("RISING-LEVEL", level=.02, slope=.004, cash=.04, level_shift_bps=200)),
        ("flat_curve_matched_cash_no_timing_edge", payload("FLAT", level=.04, slope=0, cash=.04)),
    ]
    report = {"scope": "synthetic mathematical scenarios; not Vietnam market observations", "cases": [compact(label, analyse_payload(item)) for label, item in cases]}
    if report["cases"][0]["selected_date"] == DECISION.isoformat():
        raise AssertionError("Stable upward fixture should demonstrate an interior exit.")
    if report["cases"][1]["selected_date"] != DECISION.isoformat() or report["cases"][2]["selected_date"] != DECISION.isoformat():
        raise AssertionError("High cash/rising-level fixtures should make immediate exit optimal.")
    if report["cases"][3]["margin_exceeds_epsilon"]:
        raise AssertionError("Flat matched-carry fixture should have no material timing edge.")
    return report


if __name__ == "__main__":
    output = run()
    OUT.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False, indent=2))
