"""Acceptance tests for the public RYC V3 Decision Workspace.

These tests intentionally exercise the user-facing result contract rather than
only private helper functions.  They protect the claims that matter to the
research project: equal starting capital, settlement-aware cash accounting,
unit scaling, coupon safety and robust-zone honesty.
"""

from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from datetime import date
from pathlib import Path

from ryc_v3_engine import ENGINE_REVISION, ModelInputError, analyse_payload
from ryc_v3_local_server import validate_bind_host


HERE = Path(__file__).resolve().parent


def fixture() -> dict:
    return json.loads((HERE / "ryc_v3_example_model.json").read_text(encoding="utf-8"))


def buyer_fixture() -> dict:
    payload = fixture()
    payload.update({
        "mode": "buyer",
        "epsilon_bps": 100,
        "quote": {"bid_dirty_per_100": 94.5, "ask_dirty_per_100": 95.0},
        "scenarios": [
            {"name": "Base", "probability": 0.5, "terminal_level_shift_bps": 0, "terminal_slope_shift_bps": 0, "cash_rate_decimal": 0.01, "sell_haircut_bps": 0},
            {"name": "Yield giảm", "probability": 0.25, "terminal_level_shift_bps": -15, "terminal_slope_shift_bps": 0, "cash_rate_decimal": 0.008, "sell_haircut_bps": 5},
            {"name": "Yield tăng", "probability": 0.25, "terminal_level_shift_bps": 15, "terminal_slope_shift_bps": 0, "cash_rate_decimal": 0.012, "sell_haircut_bps": 10},
        ],
    })
    return payload


def run() -> dict[str, str]:
    results: dict[str, str] = {}

    # The browser must call the same engine revision that the local API
    # advertises.  A stale literal here makes an otherwise healthy server look
    # broken in the workspace, so keep this integration contract executable.
    workspace = (HERE / "ryc_v3_decision_workspace.html").read_text(encoding="utf-8")
    expected_revision = re.search(r"health\.revision!==['\"]([^'\"]+)", workspace)
    if not expected_revision or expected_revision.group(1) != ENGINE_REVISION:
        raise AssertionError("Workspace revision guard does not match the engine revision.")
    if validate_bind_host("127.0.0.1") != "127.0.0.1":
        raise AssertionError("Local server rejected its declared loopback host.")
    try:
        validate_bind_host("0.0.0.0")
        raise AssertionError("Local-only server accepted a public bind address.")
    except ValueError:
        pass
    results["workspace_engine_revision_guard"] = "pass"

    # Registry behavior is tested by executing its JavaScript in
    # ryc_registry_behavior_tests.cjs, rather than matching source strings.

    holder = analyse_payload(fixture())
    if holder["analysis_version"] != "RYC-V3-model-only" or not holder["candidates"]:
        raise AssertionError("The public result contract is incomplete.")
    if holder["scope"]["price_basis"] != "dirty_price_per_100_face":
        raise AssertionError("Price basis is not carried into the result contract.")
    expected_holder = holder.get("expected_maximiser")
    if (
        not expected_holder
        or expected_holder["kind"] != "sale"
        or expected_holder["date"] != holder["expected_best_exit_date"]
        or expected_holder["expected_terminal_wealth"] != holder["expected_best_terminal_wealth"]
    ):
        raise AssertionError("Holder public result does not expose the actual expected-wealth maximiser.")
    margin = holder.get("decision_margin")
    if (
        not margin
        or margin["runner_up_kind"] is None
        or margin["expected_margin"] is None
        or margin["expected_margin"]["per_100_face"] < 0
        or margin["margin_exceeds_economic_tolerance"] is not True
    ):
        raise AssertionError("Public result does not expose a coherent expected decision margin.")
    # The search grid is part of the mathematical contract, not merely a UI
    # preference.  A denser declared grid must reach the Python engine and an
    # undeclared frequency must fail closed rather than silently falling back.
    business_day = fixture()
    business_day["candidate_frequency"] = "business_day"
    dense = analyse_payload(business_day)
    if (
        dense["scope"].get("candidate_grid") != "business_day"
        or len(dense["candidates"]) <= len(holder["candidates"])
    ):
        raise AssertionError("Declared business-day grid did not reach the engine or was not denser than monthly grid.")
    invalid_grid = fixture()
    invalid_grid["candidate_frequency"] = "weekly"
    try:
        analyse_payload(invalid_grid)
        raise AssertionError("Unsupported candidate frequency was accepted by the public API.")
    except ModelInputError as error:
        if "candidate_frequency" not in str(error):
            raise
    results["versioned_public_contract"] = "pass"

    # The explanation UI must be able to display a ledger that is exactly the
    # cash ledger scored by the engine, plus an honest scenario-weighted
    # summary. These are presentation fields, but they must never create a
    # second financial calculation in the browser.
    if not math.isclose(sum(holder["scenario_weights"].values()), 1.0, abs_tol=1e-12):
        raise AssertionError("Public scenario weights are not normalised.")
    first_candidate = holder["candidates"][0]
    for name, flows in first_candidate["cash_ledger"].items():
        ledger_terminal = sum(flow["terminal_contribution"]["per_100_face"] for flow in flows)
        if not math.isclose(ledger_terminal, first_candidate["scenario_terminal_wealth_per_100"][name], abs_tol=1e-9):
            raise AssertionError("Public cash ledger no longer reconciles to scored wealth.")
    summary = first_candidate["cash_breakdown"]
    if not math.isclose(
        summary["net_cash_received"]["per_100_face"] + summary["accumulation_to_terminal"]["per_100_face"],
        summary["terminal_wealth"]["per_100_face"], abs_tol=1e-9,
    ):
        raise AssertionError("Public cash breakdown does not reconcile.")
    results["public_ledger_explanation_reconciles"] = "pass"

    future_cushion = holder["candidates"][1].get("rate_cushion")
    if (
        not future_cushion
        or future_cushion["search_bound_bps"] != 5_000.0
        or not all("status" in item for item in future_cushion["by_scenario"].values())
    ):
        raise AssertionError("Holder result does not expose auditable rate-cushion sensitivity.")
    if any(item.get("rate_cushion") is not None for item in analyse_payload(buyer_fixture())["candidates"]):
        raise AssertionError("Buyer result incorrectly exposes the holder-only rate cushion.")
    results["holder_rate_cushion_is_public_and_scope_limited"] = "pass"

    # Independently value the sell-now leg from the declared bid, fee and
    # cash availability date.  This must not be the first surviving future
    # monthly candidate after a coupon blackout or a weekend roll.
    simple = fixture()
    simple["decision_date"] = "2025-06-30"
    simple["terminal_date"] = "2025-12-31"
    simple.pop("held_position", None)
    simple["execution"]["settlement_lag_business_days"] = 3
    simple["scenarios"] = [{"name": "Base", "probability": 1,
        "terminal_level_shift_bps": 0, "terminal_slope_shift_bps": 0,
        "cash_rate_decimal": 0.02, "sell_haircut_bps": 0}]
    simple_result = analyse_payload(simple)
    tau = (date(2025, 12, 31) - date(2025, 7, 3)).days / 365.25
    expected_cash = simple["quote"]["bid_dirty_per_100"] * (1 - simple["execution"]["sell_fee_bps"] / 10000) * math.exp(0.02 * tau)
    if abs(simple_result["baseline_terminal_wealth"]["per_100_face"] - expected_cash) > 1e-9:
        raise AssertionError("Sell-now baseline differs from its independent cash ledger.")
    results["sell_now_baseline_independent_cash_ledger"] = "pass"

    coupon_blackout = deepcopy(simple)
    coupon_blackout["bond"]["cashflows"].insert(0, {"payment_date": "2025-07-02", "amount_per_100": 3})
    weekend = deepcopy(simple)
    weekend["decision_date"] = "2025-06-29"
    for case in (coupon_blackout, weekend):
        try:
            analyse_payload(case)
            raise AssertionError("A future sale was silently substituted for today's baseline.")
        except ModelInputError as error:
            if "SELL_NOW_BASELINE_UNAVAILABLE" not in str(error):
                raise
    results["missing_sell_now_never_replaced_by_future_sale"] = "pass"

    # Raw curve PV and the observable bid can legitimately differ because of
    # spread, liquidity and curve-construction basis.  The public contract
    # must show the bridge and must reprice the declared bid before using it
    # for future sale-side PVs.
    calibration = holder.get("price_calibration")
    observed_bid = fixture()["quote"]["bid_dirty_per_100"]
    if (
        not calibration
        or calibration["reference_side"] != "bid"
        or abs(calibration["reference_dirty_per_100"] - observed_bid) > 1e-9
        or abs(calibration["repriced_dirty_per_100"] - observed_bid) > 1e-7
    ):
        raise AssertionError("Future curve PVs are not transparently calibrated to the observed bid.")
    results["bid_curve_calibration_public_and_exact"] = "pass"

    # Historical cost basis is useful context, but it must never bias the
    # marginal sell-vs-hold choice from today onward.
    changed_cost = fixture()
    changed_cost["held_position"]["purchase_dirty_per_100"] = 80.0
    different_cost = analyse_payload(changed_cost)
    original = [x["expected_terminal_wealth"]["per_100_face"] for x in holder["candidates"]]
    revised = [x["expected_terminal_wealth"]["per_100_face"] for x in different_cost["candidates"]]
    if original != revised:
        raise AssertionError("Cost basis leaked into the forward decision surface.")
    results["sunk_cost_cannot_bias_forward_exit"] = "pass"

    # Position scaling is explicit: double face doubles VND wealth but never
    # changes the per-100 mathematical result or selected date.
    doubled = fixture()
    doubled["position_face_value_vnd"] *= 2
    doubled_result = analyse_payload(doubled)
    a = holder["baseline_terminal_wealth"]
    b = doubled_result["baseline_terminal_wealth"]
    if abs(a["per_100_face"] - b["per_100_face"]) > 1e-10 or abs(2 * a["for_position_vnd"] - b["for_position_vnd"]) > 1e-5:
        raise AssertionError("Per-100 and full-position units were mixed.")
    results["position_unit_scaling"] = "pass"

    buyer = analyse_payload(buyer_fixture())
    if buyer["mode"] != "buyer" or buyer["baseline_label"] != "no_buy_cash":
        raise AssertionError("Buyer did not use the no-buy cash benchmark.")
    if buyer["action"] not in {"MODEL_MAX_EXPECTED_ENTRY_AND_EXIT", "MODEL_MAX_EXPECTED_NO_BUY_CASH"}:
        raise AssertionError("Buyer returned an undeclared action state.")
    results["buyer_uses_equal_cash_benchmark"] = "pass"

    # A higher buy fee cannot improve the purchase edge.  This detects the
    # exact omission that existed in the old JavaScript buyer branch.
    expensive = buyer_fixture()
    expensive["execution"]["buy_fee_bps"] = 100
    expensive_result = analyse_payload(expensive)
    edge = buyer["expected_edge_vs_baseline"]
    expensive_edge = expensive_result["expected_edge_vs_baseline"]
    if edge is not None and expensive_edge is not None and expensive_edge["per_100_face"] > edge["per_100_face"] + 1e-9:
        raise AssertionError("Increasing buy fee improved buyer edge.")
    results["higher_buy_fee_never_improves_edge"] = "pass"

    # V3 refuses to guess whether a new buyer owns a coupon.  The same holder
    # path remains permitted because ownership before the decision is known.
    coupon_buyer = buyer_fixture()
    coupon_buyer["bond"]["cashflows"] = [
        {"payment_date": "2026-07-02", "amount_per_100": 2},
        {"payment_date": "2028-01-02", "amount_per_100": 102},
    ]
    try:
        analyse_payload(coupon_buyer)
        raise AssertionError("Buyer was assigned an unproven coupon entitlement.")
    except ModelInputError as error:
        if "BUYER_COUPON_ENTITLEMENT_REQUIRED" not in str(error):
            raise
    results["buyer_coupon_safety_gate"] = "pass"

    # The result cannot promote a minimax diagnostic into a sell date when
    # robust support fails.  Generate intentionally conflicting scenarios.
    unstable = fixture()
    unstable["epsilon_bps"] = 0
    unstable["quote"]["bid_dirty_per_100"] = 94
    unstable["scenarios"] = [
        {"name": "Sharp rise", "probability": 0.5, "terminal_level_shift_bps": 1_000, "terminal_slope_shift_bps": 0, "cash_rate_decimal": 0.01, "sell_haircut_bps": 0},
        {"name": "Sharp fall", "probability": 0.5, "terminal_level_shift_bps": -1_000, "terminal_slope_shift_bps": 0, "cash_rate_decimal": 0.05, "sell_haircut_bps": 0},
    ]
    unstable_result = analyse_payload(unstable)
    if unstable_result["representative_exit_date"] is not None or unstable_result["objective_audit"]["maximising_alternative"] != "sale":
        raise AssertionError("An unstable robustness diagnostic was presented as a robust exit date.")
    results["no_false_robust_exit"] = "pass"

    # Support equal to zero would make every exit date appear robust even in
    # mutually conflicting scenarios.  It must be rejected at the API edge.
    zero_support = fixture()
    zero_support["minimum_robust_support"] = 0
    try:
        analyse_payload(zero_support)
        raise AssertionError("Zero robust support was accepted by the public API.")
    except ModelInputError as error:
        if "minimum_robust_support" not in str(error):
            raise
    results["zero_robust_support_rejected"] = "pass"

    # Economic input guards must fail closed: a negative sale factor or a
    # silently truncated settlement lag would create artificial results.
    excessive_fee = fixture()
    excessive_fee["execution"]["sell_fee_bps"] = 10_000
    try:
        analyse_payload(excessive_fee)
        raise AssertionError("A fee that makes sale proceeds non-positive was accepted.")
    except ModelInputError as error:
        if "Execution fees" not in str(error):
            raise
    fractional_lag = fixture()
    fractional_lag["execution"]["settlement_lag_business_days"] = 1.5
    try:
        analyse_payload(fractional_lag)
        raise AssertionError("Fractional settlement lag was silently truncated.")
    except ModelInputError as error:
        if "settlement lag" not in str(error):
            raise
    results["economic_input_guards_fail_closed"] = "pass"

    # A holder may compare redemption only once terminal H is on/after
    # maturity; it is a contractual cash alternative, not a sale quote.
    redeem = fixture()
    redeem["terminal_date"] = "2028-01-02"
    redeem["bond"]["cashflows"] = [{"payment_date": "2028-01-02", "amount_per_100": 102}]
    redemption = analyse_payload(redeem)
    if not redemption["hold_to_maturity"] or redemption["hold_to_maturity"]["maturity_date"] != "2028-01-02":
        raise AssertionError("Maturity cash alternative is missing from the public contract.")
    results["redemption_cash_alternative_public"] = "pass"

    # Every displayed candidate is settled by H; otherwise comparisons would
    # silently include cash that is unavailable at the terminal date.
    if any(item["settlement_date"] > holder["terminal_date"] for item in holder["candidates"]):
        raise AssertionError("A visible candidate settles after terminal H.")
    results["terminal_cash_date_guard"] = "pass"
    return results


if __name__ == "__main__":
    print(json.dumps({"acceptance_tests": run()}, ensure_ascii=False, indent=2))
