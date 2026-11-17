"""Independent Phase 1 fixtures for the model-only Perfect Foresight variant."""

from __future__ import annotations

import copy
import json
import math
import sys
import unittest
from datetime import date
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from ryc_v3_engine import ModelInputError, analyse_payload, evaluate_fixed_plan, dirty_model_price_per_100, parallel_zero_duration_years, _pf_candidates, CashFlow, FixedRateBullet, SettlementCalendar, ZeroCurve


EXAMPLE = APP_DIR / "ryc_perfect_foresight_example.json"


def fixture() -> dict:
    with EXAMPLE.open(encoding="utf-8") as handle:
        result = json.load(handle)
    result["execution"].update({
        "sell_fee_bps": 0, "fixed_ticket_fee_vnd": 0,
        "market_impact_bps_full_sale": 0,
    })
    return result


def number(result: dict, key: str = "expected_maximiser") -> float:
    return result[key]["expected_terminal_wealth"]["per_100_face"]


def ledger_sum(plan: dict, scenario: str = "Perfect Foresight") -> float:
    return sum(row["terminal_contribution"]["per_100_face"] for row in plan["cash_ledger"][scenario])


class PerfectForesightTests(unittest.TestCase):
    def test_fixed_plan_matches_oracle_on_same_path_for_hold_one_and_two_sales(self) -> None:
        data = fixture()
        oracle = analyse_payload(data)
        for key in ("hold_to_h", "best_single_sale", "best_two_tranche", "expected_maximiser"):
            selected = oracle[key]
            fixed = evaluate_fixed_plan(data, selected)
            self.assertEqual(fixed["evaluation"], "fixed_plan_no_optimization")
            self.assertEqual(fixed["fixed_plan"]["kind"], selected["kind"])
            self.assertAlmostEqual(fixed["fixed_plan"]["expected_terminal_wealth"]["per_100_face"],
                                   selected["expected_terminal_wealth"]["per_100_face"])
            self.assertEqual(fixed["fixed_plan"]["cash_ledger"], selected["cash_ledger"])

    def test_realised_fixed_plan_does_not_reoptimise_and_regret_is_nonnegative(self) -> None:
        forecast = fixture()
        forecast["curve_path"][1]["curve_nodes"][0]["zero_rate_decimal"] = 0.01
        forecast["curve_path"][1]["curve_nodes"][1]["zero_rate_decimal"] = 0.01
        chosen = analyse_payload(forecast)["expected_maximiser"]
        realised = fixture()
        for node in realised["curve_path"][1]["curve_nodes"]:
            node["zero_rate_decimal"] = 0.09
        oracle = analyse_payload(realised)["expected_maximiser"]
        scored = evaluate_fixed_plan(realised, chosen)["fixed_plan"]
        self.assertEqual(
            [(sale["execution_date"], sale["sale_pct_initial_face"]) for sale in scored["sales"]],
            [(sale["execution_date"], sale["sale_pct_initial_face"]) for sale in chosen["sales"]],
        )
        regret = oracle["expected_terminal_wealth"]["per_100_face"] - scored["expected_terminal_wealth"]["per_100_face"]
        self.assertGreaterEqual(regret, -1e-9)

    def test_fixed_plan_rejects_invalid_lots_dates_settlement_and_inventory(self) -> None:
        data = fixture()
        with self.assertRaises(ModelInputError):
            evaluate_fixed_plan(data, None)
        for sales in (
            [{"execution_date": "2026-02-06", "sale_pct_initial_face": 50}],
            [{"execution_date": "2026-02-05", "settlement_date": "2026-02-06", "sale_pct_initial_face": 50}],
            [{"execution_date": "2026-01-05", "sale_pct_initial_face": 25}],
            [{"execution_date": "2026-01-05", "sale_pct_initial_face": 100},
             {"execution_date": "2026-02-05", "sale_pct_initial_face": 50}],
        ):
            with self.subTest(sales=sales), self.assertRaises(ModelInputError):
                evaluate_fixed_plan(data, {"sales": sales})

    def test_two_tranche_option_value_is_owned_by_engine(self) -> None:
        result = analyse_payload(fixture())
        expected = number(result, "best_two_tranche") - number(result, "best_single_sale")
        self.assertAlmostEqual(result["two_tranche_option_value_vs_single_sale"]["per_100_face"], expected)
        self.assertAlmostEqual(
            result["two_tranche_option_value_vs_single_sale"]["for_position_vnd"],
            expected * fixture()["position_face_value_vnd"] / 100,
        )

    def test_curve_view_roll_down_marker_is_from_python(self) -> None:
        result = analyse_payload(fixture())
        snapshots = result["curve_path_view"]["Perfect Foresight"]
        self.assertEqual([item["value_date"] for item in snapshots], ["2026-01-05", "2026-02-05", "2026-03-05"])
        self.assertAlmostEqual(snapshots[0]["maturity_tenor_years"], 365 / 365.25)
        self.assertGreater(snapshots[0]["maturity_tenor_years"], snapshots[1]["maturity_tenor_years"])
        self.assertAlmostEqual(snapshots[0]["maturity_zero_rate_decimal"], 0.04)
        for snapshot in snapshots:
            remaining_years = (date(2027, 1, 5) - date.fromisoformat(snapshot["value_date"])).days / 365.25
            self.assertAlmostEqual(
                snapshot["dirty_model_price_per_100"],
                100 * math.exp(-0.04 * remaining_years),
                places=10,
            )
        self.assertAlmostEqual(snapshots[0]["dirty_model_price_per_100"], result["current_price"]["dirty_bid_per_100"])
        with_extra_snapshot = fixture()
        with_extra_snapshot["curve_path"].insert(1, {
            "value_date": "2026-01-20",
            "curve_nodes": with_extra_snapshot["curve_path"][0]["curve_nodes"],
        })
        extra = analyse_payload(with_extra_snapshot)["curve_path_view"]["Perfect Foresight"]
        self.assertIsNone(extra[1]["dirty_model_price_per_100"])

    def test_zero_coupon_price_and_parallel_shift(self) -> None:
        bond = FixedRateBullet("ZERO", date(2025, 1, 1), (CashFlow(date(2027, 1, 5), 100),))
        curve = ZeroCurve(((0.25, 0.05), (2.0, 0.05)))
        value_date = date(2026, 1, 5)
        expected = 100 * math.exp(-0.05 * (date(2027, 1, 5) - value_date).days / 365.25)
        self.assertAlmostEqual(dirty_model_price_per_100(bond, curve, value_date), expected, places=10)
        self.assertLess(dirty_model_price_per_100(bond, curve.shifted(100, 0), value_date), expected)
        bump = 1.0e-5
        up = dirty_model_price_per_100(bond, curve.shifted(bump * 10_000, 0), value_date)
        down = dirty_model_price_per_100(bond, curve.shifted(-bump * 10_000, 0), value_date)
        finite_difference = -(up - down) / (2 * bump * expected)
        self.assertAlmostEqual(parallel_zero_duration_years(bond, curve, value_date), finite_difference, places=8)

    def test_coupon_bond_prices_each_cash_flow(self) -> None:
        bond = FixedRateBullet("COUPON", date(2025, 1, 1), (
            CashFlow(date(2026, 7, 5), 3), CashFlow(date(2027, 1, 5), 103),
        ))
        value_date = date(2026, 1, 5)
        curve = ZeroCurve(((0.25, 0.04), (2.0, 0.04)))
        expected = sum(flow.amount_per_100 * math.exp(-0.04 * (flow.payment_date - value_date).days / 365.25)
                       for flow in bond.cashflows)
        self.assertAlmostEqual(dirty_model_price_per_100(bond, curve, value_date), expected, places=10)

    def test_clean_plus_declared_accrual_equals_dirty(self) -> None:
        data = fixture()
        data["bond"]["cashflows"] = [
            {"payment_date": "2026-02-05", "amount_per_100": 5},
            {"payment_date": "2027-01-05", "amount_per_100": 105},
        ]
        data["bond"]["accrual_periods"] = [{
            "period_start": "2025-08-05", "period_end": "2026-02-05", "coupon_per_100": 5,
        }]
        data["coupon_entitlement_policy"] = "payment_date_model_assumption"
        data["liquidation"]["candidate_dates"][1] = "2026-02-06"
        data["curve_path"][1]["value_date"] = "2026-02-06"
        result = analyse_payload(data)
        price = result["current_price"]
        expected_ai = 5 * (date(2026, 1, 5) - date(2025, 8, 5)).days / (date(2026, 2, 5) - date(2025, 8, 5)).days
        self.assertAlmostEqual(price["accrued_interest_per_100"], expected_ai)
        self.assertAlmostEqual(price["clean_bid_per_100"] + expected_ai, price["dirty_bid_per_100"])

    def test_flat_reinvestment_matches_legacy_exponential_factor(self) -> None:
        data = fixture()
        data["reinvestment_path"] = None
        data["cash_rate_decimal"] = 0.04
        result = analyse_payload(data)
        bid = result["current_price"]["dirty_bid_per_100"]
        days = (date(2026, 3, 5) - date(2026, 1, 5)).days
        self.assertAlmostEqual(number(result, "sell_now_full_position"), bid * math.exp(0.04 * days / 365.25), places=9)

    def test_two_rate_segments_start_at_cash_availability(self) -> None:
        data = fixture()
        data["reinvestment_path"][0]["rate_cc_decimal"] = 0.01
        data["reinvestment_path"][1]["rate_cc_decimal"] = 0.08
        result = analyse_payload(data)
        bid = result["current_price"]["dirty_bid_per_100"]
        expected = bid * math.exp(0.01 * 31 / 365.25 + 0.08 * 28 / 365.25)
        self.assertAlmostEqual(number(result, "sell_now_full_position"), expected, places=9)
        late = next(row for row in result["single_sale_surface"] if row["execution_date"] == "2026-02-05" and row["sale_pct_initial_face"] == 100)
        late_price = result["best_two_tranche"]["sales"][1]["model_dirty_bid_proxy_per_100"]["Perfect Foresight"]
        self.assertAlmostEqual(late["expected_terminal_wealth"]["per_100_face"], late_price * math.exp(0.08 * 28 / 365.25), places=9)

    def test_sale_cash_starts_at_settlement_not_execution(self) -> None:
        data = fixture()
        data["execution"]["settlement_lag_business_days"] = 1
        for snapshot, value_date in zip(data["curve_path"], ("2026-01-06", "2026-02-06", "2026-03-05")):
            snapshot["value_date"] = value_date
        result = analyse_payload(data)
        bid = result["current_price"]["dirty_bid_per_100"]
        self.assertEqual(result["sell_now_full_position"]["sales"][0]["settlement_date"], "2026-01-06")
        expected = bid * math.exp(0.04 * (date(2026, 3, 5) - date(2026, 1, 6)).days / 365.25)
        self.assertAlmostEqual(number(result, "sell_now_full_position"), expected, places=9)

    def test_coupon_inside_settlement_window_is_rejected(self) -> None:
        data = fixture()
        data["execution"]["settlement_lag_business_days"] = 1
        data["bond"]["cashflows"] = [
            {"payment_date": "2026-02-06", "amount_per_100": 5},
            {"payment_date": "2027-01-05", "amount_per_100": 105},
        ]
        data["coupon_entitlement_policy"] = "payment_date_model_assumption"
        with self.assertRaisesRegex(ModelInputError, "settlement window"):
            analyse_payload(data)

    def test_missing_curve_snapshot_and_cash_gap_fail_closed(self) -> None:
        data = fixture()
        del data["curve_path"][1]
        with self.assertRaisesRegex(ModelInputError, "missing exact value-date"):
            analyse_payload(data)
        data = fixture()
        data["reinvestment_path"][1]["start_date"] = "2026-02-06"
        with self.assertRaisesRegex(ModelInputError, "gap or overlap"):
            analyse_payload(data)

    def test_coupon_entitlement_and_settlement_blackout(self) -> None:
        data = fixture()
        data["bond"]["cashflows"] = [
            {"payment_date": "2026-02-05", "amount_per_100": 5},
            {"payment_date": "2027-01-05", "amount_per_100": 105},
        ]
        with self.assertRaisesRegex(ModelInputError, "Declare payment_date_model_assumption"):
            analyse_payload(data)
        data["coupon_entitlement_policy"] = "payment_date_model_assumption"
        with self.assertRaisesRegex(ModelInputError, "Coupon payment on execution"):
            analyse_payload(data)
        data["liquidation"]["candidate_dates"][1] = "2026-02-06"
        data["curve_path"][1]["value_date"] = "2026-02-06"
        result = analyse_payload(data)
        coupon = next(row for row in result["best_two_tranche"]["cash_ledger"]["Perfect Foresight"] if row["kind"] == "coupon")
        self.assertAlmostEqual(coupon["cash_amount"]["per_100_face"], 2.5)
        self.assertEqual(result["best_two_tranche"]["sales"][0]["settlement_date"], "2026-01-05")

    def test_maturity_redemption_is_once_and_has_no_mark(self) -> None:
        data = fixture()
        data["bond"]["cashflows"] = [{"payment_date": "2026-03-05", "amount_per_100": 105}]
        data["terminal_date"] = "2026-04-05"
        data["coupon_entitlement_policy"] = "payment_date_model_assumption"
        data["curve_path"] = data["curve_path"][:2]
        data["reinvestment_path"] = [{"start_date": "2026-01-05", "end_date": "2026-04-05", "rate_cc_decimal": 0.0}]
        result = analyse_payload(data)
        hold = result["hold_to_h"]
        self.assertEqual(number(result, "hold_to_h"), 105)
        self.assertEqual([row["kind"] for row in hold["cash_ledger"]["Perfect Foresight"]], ["redemption"])
        self.assertEqual(hold["terminal_value_basis"], "CONTRACTUAL_CASH_OR_SALE")

    def test_generated_sale_grid_never_prices_on_maturity_settlement(self) -> None:
        bond = FixedRateBullet("EARLY", date(2025, 1, 1), (CashFlow(date(2026, 3, 5), 100),))
        candidates = _pf_candidates(
            None, date(2026, 1, 5), date(2026, 4, 5), bond, SettlementCalendar(1),
        )
        self.assertTrue(candidates)
        self.assertTrue(all(settlement < bond.maturity_date for _, settlement in candidates))

    def test_hold_competes_with_conditional_best_sale(self) -> None:
        data = fixture()
        data["execution"].update({"sell_fee_bps": 50, "fixed_ticket_fee_vnd": 10000})
        result = analyse_payload(data)
        self.assertEqual(result["expected_maximiser"]["kind"], "hold_to_h")
        self.assertEqual(result["best_single_sale"]["kind"], "single_sale")
        self.assertGreater(number(result, "hold_to_h"), number(result, "best_single_sale"))

    def test_flat_curve_and_matched_cash_do_not_create_timing_edge(self) -> None:
        result = analyse_payload(fixture())
        self.assertAlmostEqual(number(result, "hold_to_h"), number(result, "sell_now_full_position"), places=9)
        self.assertEqual(result["expected_maximiser"]["kind"], "hold_to_h")

    def test_two_tranche_domain_and_single_degeneracy(self) -> None:
        data = fixture()
        both = analyse_payload(data)
        self.assertEqual(both["alternatives_evaluated"], {"hold": 1, "single_sale": 4, "two_tranche": 1})
        self.assertEqual([row["sale_pct_initial_face"] for row in both["best_two_tranche"]["sales"]], [50, 50])
        self.assertLessEqual(both["best_two_tranche"]["total_sold_pct_initial_face"], 100)
        data["liquidation"]["max_tranches"] = 1
        single_only = analyse_payload(data)
        self.assertIsNone(single_only["best_two_tranche"])
        self.assertAlmostEqual(number(single_only, "best_single_sale"), number(both, "best_single_sale"))

    def test_grid_maximum_matches_independent_zero_coupon_oracle(self) -> None:
        data = fixture()
        data["curve_path"][1]["curve_nodes"][0]["zero_rate_decimal"] = 0.02
        data["curve_path"][1]["curve_nodes"][1]["zero_rate_decimal"] = 0.02
        data["curve_path"][2]["curve_nodes"][0]["zero_rate_decimal"] = 0.05
        data["curve_path"][2]["curve_nodes"][1]["zero_rate_decimal"] = 0.05
        data["execution"].update({
            "sell_fee_bps": 10, "market_impact_bps_full_sale": 40,
            "market_impact_exponent": 2, "fixed_ticket_fee_vnd": 10000,
        })
        result = analyse_payload(data)
        maturity = date(2027, 1, 5)
        terminal = date(2026, 3, 5)
        dates = (date(2026, 1, 5), date(2026, 2, 5))
        rates = (0.04, 0.02)
        prices = [100 * math.exp(-rate * (maturity - day).days / 365.25)
                  for day, rate in zip(dates, rates)]
        mark = 100 * math.exp(-0.05 * (maturity - terminal).days / 365.25)

        def wealth(sales: tuple[tuple[int, float], ...]) -> float:
            remaining = 1 - sum(q for _, q in sales)
            return remaining * mark + sum(
                (q * prices[i] * (1 - 0.001) - prices[i] * 0.004 * q * q - 0.01)
                * math.exp(0.04 * (terminal - dates[i]).days / 365.25)
                for i, q in sales
            )

        oracle = [wealth(())]
        oracle += [wealth(((i, q),)) for i in (0, 1) for q in (0.5, 1.0)]
        oracle += [wealth(((0, 0.5), (1, 0.5)))]
        self.assertAlmostEqual(number(result), max(oracle), places=9)
        self.assertAlmostEqual(number(result, "best_two_tranche"), oracle[-1], places=9)

    def test_fixed_ticket_and_convex_impact_are_per_order(self) -> None:
        data = fixture()
        data["execution"].update({"fixed_ticket_fee_vnd": 10000, "market_impact_bps_full_sale": 100, "market_impact_exponent": 2})
        result = analyse_payload(data)
        two = result["best_two_tranche"]["cash_ledger"]["Perfect Foresight"]
        full = result["sell_now_full_position"]["cash_ledger"]["Perfect Foresight"]
        self.assertEqual(len([row for row in two if row["kind"] == "fixed_ticket_fee"]), 2)
        self.assertEqual(len([row for row in full if row["kind"] == "fixed_ticket_fee"]), 1)
        self.assertAlmostEqual(sum(row["cash_amount"]["per_100_face"] for row in two if row["kind"] == "fixed_ticket_fee"), -0.02)
        self.assertAlmostEqual(next(row["cash_amount"]["per_100_face"] for row in full if row["kind"] == "market_impact"), -result["current_price"]["dirty_bid_per_100"] * 0.01)

    def test_ledger_units_and_risk_diagnostic(self) -> None:
        result = analyse_payload(fixture())
        for key in ("hold_to_h", "best_single_sale", "best_two_tranche", "expected_maximiser"):
            plan = result[key]
            self.assertAlmostEqual(ledger_sum(plan), plan["scenario_terminal_wealth_per_100"]["Perfect Foresight"])
            self.assertAlmostEqual(plan["expected_terminal_wealth"]["for_position_vnd"], plan["expected_terminal_wealth"]["per_100_face"] * 1_000_000)
        self.assertEqual(result["risk_diagnostic"]["status"], "NOT_ESTIMATED")
        self.assertIsNone(result["risk_diagnostic"]["J"])

    def test_public_claims_identify_synthetic_price_and_non_cash_mark(self) -> None:
        result = analyse_payload(fixture())
        self.assertEqual(result["status"], "MODEL_ONLY")
        self.assertEqual(result["current_price"]["basis"], "synthetic_curve_model")
        self.assertIsNone(result["current_price"]["clean_bid_per_100"])
        self.assertGreater(result["current_price"]["parallel_zero_duration_years"], 0)
        self.assertEqual(result["hold_to_h"]["terminal_value_basis"], "MODEL_MARK_PROXY")
        self.assertFalse(result["hold_to_h"]["cash_ledger"]["Perfect Foresight"][0]["is_cash"])
        self.assertEqual(result["scope"]["curve_time_interpolation"], "none; exact value-date snapshots required")

    def test_higher_fee_cannot_improve_sell_now(self) -> None:
        base = fixture()
        no_fee = analyse_payload(base)
        base["execution"]["sell_fee_bps"] = 100
        with_fee = analyse_payload(base)
        self.assertLess(number(with_fee, "sell_now_full_position"), number(no_fee, "sell_now_full_position"))

    def test_observed_bid_calibration_and_cost_basis_independence(self) -> None:
        base = fixture()
        base["quote"] = {"bid_dirty_per_100": 97, "value_date": "2026-01-05"}
        first = analyse_payload(base)
        json.dumps(first)
        self.assertAlmostEqual(first["price_calibration"]["repriced_dirty_per_100"], 97)
        self.assertAlmostEqual(first["curve_path_view"]["Perfect Foresight"][0]["dirty_model_price_per_100"], 97)
        self.assertEqual(first["price_calibration"]["value_date"], "2026-01-05")
        base["held_position"] = {"purchase_date": "2025-06-01", "purchase_dirty_per_100": 80}
        second = analyse_payload(base)
        self.assertEqual(first["expected_maximiser"]["kind"], second["expected_maximiser"]["kind"])
        self.assertAlmostEqual(number(first), number(second))

    def test_scenario_weighted_objective_and_zero_weight_exclusion(self) -> None:
        data = fixture()
        data.pop("reinvestment_path")
        data["scenarios"] = [
            {"name": "slow cash", "probability": 1, "cash_rate_decimal": 0.01},
            {"name": "fast cash", "probability": 3, "cash_rate_decimal": 0.10},
            {"name": "ignored", "probability": 0, "cash_rate_decimal": 0.20},
        ]
        result = analyse_payload(data)
        self.assertEqual(result["scenario_weights"], {"slow cash": 0.25, "fast cash": 0.75})
        sale = result["sell_now_full_position"]
        expected = 0.25 * sale["scenario_terminal_wealth_per_100"]["slow cash"] + 0.75 * sale["scenario_terminal_wealth_per_100"]["fast cash"]
        self.assertAlmostEqual(sale["expected_terminal_wealth"]["per_100_face"], expected)
        self.assertNotIn("ignored", sale["cash_ledger"])

    def test_variant_is_opt_in_and_unknown_variant_rejected(self) -> None:
        base = fixture()
        base["model_variant"] = "unknown"
        with self.assertRaisesRegex(ModelInputError, "Unknown model_variant"):
            analyse_payload(base)

    def test_legacy_price_drivers_and_clean_quote_are_not_silently_used(self) -> None:
        data = fixture()
        data["asset_trend_pct_annual"] = 5
        with self.assertRaisesRegex(ModelInputError, "Legacy demo inputs"):
            analyse_payload(data)
        data.pop("asset_trend_pct_annual")
        data["quote"] = {"clean_bid_per_100": 98}
        with self.assertRaisesRegex(ModelInputError, "clean quote"):
            analyse_payload(data)

    def test_inconsistent_current_scenario_curve_rejected(self) -> None:
        data = fixture()
        data["scenarios"] = [
            {"name": "a", "probability": 1, "curve_path": copy.deepcopy(data["curve_path"]), "reinvestment_path": data["reinvestment_path"]},
            {"name": "b", "probability": 1, "curve_path": copy.deepcopy(data["curve_path"]), "reinvestment_path": data["reinvestment_path"]},
        ]
        data["scenarios"][1]["curve_path"][0]["curve_nodes"][0]["zero_rate_decimal"] = 0.05
        with self.assertRaisesRegex(ModelInputError, "share the same current settlement curve"):
            analyse_payload(data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
