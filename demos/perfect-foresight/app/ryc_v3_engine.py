"""RYC Vietnam V3 — cash-flow-consistent decision engine.

This module is the single mathematical source of truth for the public
``Decision Studio``.  It deliberately solves a narrower, auditable problem
before attempting a historical performance claim:

    Given a VND fixed-rate bond, a position state today, a common terminal
    date H, declared yield-curve scenarios and a cash alternative, which
    *eligible* sale dates preserve the most terminal wealth?

It distinguishes three ledgers:

* a holder already owns the bond, so coupons before a sale belong to them;
* a prospective buyer keeps cash until the entry purchase settles, then can
  only sell after entry settlement; and
* redemption is a contractual cash event, not a sale.  V3's buy-to-sale
  scope deliberately rejects horizons containing a coupon until record/
  ex-coupon entitlement data are supplied per ISIN.

The engine is a research simulator.  It never labels a result a live trading
signal or an empirical backtest.  The companion data contract is required
before actual Vietnamese observations can be evaluated.

No third-party package is required.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence


DAYS_PER_YEAR = 365.25
EPS = 1.0e-9
PER_100_FACE = 100.0
ENGINE_REVISION = "2026-11-16-p3-fixed-plan-v1"
RATE_CUSHION_SEARCH_BOUND_BPS = 5_000.0


class ModelInputError(ValueError):
    """An input violates an economic or accounting invariant."""


def parse_date(value: str, field: str = "date") -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError) as error:
        raise ModelInputError(f"{field} must use YYYY-MM-DD.") from error


def year_fraction(start: date, end: date) -> float:
    if end < start:
        raise ModelInputError("A time interval cannot run backward.")
    return (end - start).days / DAYS_PER_YEAR


def add_months(value: date, months: int) -> date:
    """Add calendar months, preserving end-of-month intent."""
    index = value.month - 1 + months
    year, month = value.year + index // 12, index % 12 + 1
    next_month = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    last_day = (next_month - timedelta(days=1)).day
    source_next = date(
        value.year + (value.month == 12),
        1 if value.month == 12 else value.month + 1,
        1,
    )
    source_eom = value.day == (source_next - timedelta(days=1)).day
    return date(year, month, last_day if source_eom else min(value.day, last_day))


def _finite(value: Any, name: str) -> float:
    try:
        output = float(value)
    except (TypeError, ValueError) as error:
        raise ModelInputError(f"{name} must be numeric.") from error
    if not math.isfinite(output):
        raise ModelInputError(f"{name} must be finite.")
    return output


def _whole_nonnegative_int(value: Any, name: str) -> int:
    """Parse a public numeric field without silently truncating it."""
    parsed = _finite(value, name)
    if parsed < 0 or not parsed.is_integer():
        raise ModelInputError(f"{name} must be an exact non-negative whole number.")
    return int(parsed)


@dataclass(frozen=True)
class CashFlow:
    """Contractual cash flow per VND100 face value."""

    payment_date: date
    amount_per_100: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.amount_per_100) or self.amount_per_100 <= 0:
            raise ModelInputError("Every contractual cash flow must be positive and finite.")


@dataclass(frozen=True)
class FixedRateBullet:
    """A fixed-rate bullet bond normalised to VND100 face value."""

    identifier: str
    issue_date: date
    cashflows: tuple[CashFlow, ...]

    def __post_init__(self) -> None:
        if not self.identifier.strip():
            raise ModelInputError("Bond identifier is required.")
        if not self.cashflows:
            raise ModelInputError("A bond requires its contractual cash-flow schedule.")
        dates = [flow.payment_date for flow in self.cashflows]
        if dates != sorted(dates) or len(set(dates)) != len(dates):
            raise ModelInputError("Cash-flow dates must be strictly increasing and unique.")
        if any(day <= self.issue_date for day in dates):
            raise ModelInputError("Every cash flow must follow the issue date.")
        # A bullet bond must repay its normalised principal at maturity.  The
        # final amount may also include the last coupon, hence >= 100.
        if self.cashflows[-1].amount_per_100 + EPS < PER_100_FACE:
            raise ModelInputError("The final cash flow must include VND100 principal per VND100 face.")

    @property
    def maturity_date(self) -> date:
        return self.cashflows[-1].payment_date

    @property
    def payment_dates(self) -> frozenset[date]:
        return frozenset(flow.payment_date for flow in self.cashflows)

    def assert_live(self, on_date: date) -> None:
        if on_date < self.issue_date or on_date >= self.maturity_date:
            raise ModelInputError("The bond must be live and pre-maturity on the decision date.")

    def flows_after(self, on_date: date) -> tuple[CashFlow, ...]:
        return tuple(flow for flow in self.cashflows if flow.payment_date > on_date)


@dataclass(frozen=True)
class ZeroCurve:
    """Continuously compounded spot curve, with linear interpolation."""

    nodes: tuple[tuple[float, float], ...]

    def __post_init__(self) -> None:
        if len(self.nodes) < 2:
            raise ModelInputError("At least two zero-curve nodes are required.")
        if any(term <= 0 or not math.isfinite(rate) for term, rate in self.nodes):
            raise ModelInputError("Curve nodes need positive tenors and finite zero rates.")
        if any(right[0] <= left[0] for left, right in zip(self.nodes, self.nodes[1:])):
            raise ModelInputError("Curve tenors must be strictly increasing.")

    @property
    def max_tenor(self) -> float:
        return self.nodes[-1][0]

    def spot(self, tenor: float) -> float:
        if tenor < -EPS:
            raise ModelInputError("A curve cannot be queried at a negative tenor.")
        if tenor > self.max_tenor + EPS:
            raise ModelInputError(
                f"The curve ends at {self.max_tenor:.2f} years but pricing needs {tenor:.2f} years."
            )
        if tenor <= self.nodes[0][0]:
            return self.nodes[0][1]
        for (left_t, left_r), (right_t, right_r) in zip(self.nodes, self.nodes[1:]):
            if tenor <= right_t + EPS:
                weight = (tenor - left_t) / (right_t - left_t)
                return left_r + weight * (right_r - left_r)
        return self.nodes[-1][1]

    def shifted(self, level_bps: float, slope_bps: float) -> "ZeroCurve":
        """Apply transparent level/slope scenario shifts in basis points.

        The slope component is centered at half of the available curve range:
        a positive number raises long rates and lowers short rates by a
        corresponding amount.  It is a declared scenario mechanism, never a
        fitted yield forecast.
        """
        level, slope, maximum = level_bps / 10_000.0, slope_bps / 10_000.0, self.max_tenor
        return ZeroCurve(tuple(
            (term, rate + level + slope * (term / maximum - 0.5))
            for term, rate in self.nodes
        ))


def _price_with_constant_oas(
    bond: FixedRateBullet,
    curve: ZeroCurve,
    valuation_date: date,
    oas_decimal: float,
) -> float:
    """PV per VND100 face after a constant static Z-spread shift (option-free cash flows)."""
    bond.assert_live(valuation_date)
    return sum(
        flow.amount_per_100
        * math.exp(
            -(curve.spot(year_fraction(valuation_date, flow.payment_date)) + oas_decimal)
            * year_fraction(valuation_date, flow.payment_date)
        )
        for flow in bond.flows_after(valuation_date)
    )


def dirty_model_price_per_100(bond: FixedRateBullet, curve: ZeroCurve, valuation_date: date) -> float:
    """Uncalibrated PV of future contractual cash flows per VND100 face."""
    return _price_with_constant_oas(bond, curve, valuation_date, 0.0)


def parallel_zero_duration_years(
    bond: FixedRateBullet, curve: ZeroCurve, valuation_date: date, z_spread_decimal: float = 0.0,
) -> float:
    """PV-weighted sensitivity to a parallel continuously compounded zero shift."""
    bond.assert_live(valuation_date)
    components = [
        (year_fraction(valuation_date, flow.payment_date), flow.amount_per_100)
        for flow in bond.flows_after(valuation_date)
    ]
    discounted = [
        (tenor, amount * math.exp(-(curve.spot(tenor) + z_spread_decimal) * tenor))
        for tenor, amount in components
    ]
    price = sum(value for _, value in discounted)
    return sum(tenor * value for tenor, value in discounted) / price


@dataclass(frozen=True)
class PriceCalibration:
    """Constant-Z-spread bridge from a model curve to today's observed bid.

    Future curve PVs must not be compared directly with an observed current
    bid if the two have an unexplained level gap.  V3 calibrates a fixed Z-spread
    on the decision date and carries it into each scenario PV.  It is an
    explicit *constant-spread assumption*, not evidence that the spread will
    stay constant in reality; scenario haircuts remain a separate liquidity
    stress.
    """

    reference_side: str
    reference_dirty_per_100: float
    uncalibrated_dirty_per_100: float
    constant_oas_bps: float
    repriced_dirty_per_100: float
    value_date: date


def calibrate_constant_oas(
    bond: FixedRateBullet,
    curve: ZeroCurve,
    valuation_date: date,
    observed_dirty_per_100: float,
    *,
    reference_side: str = "bid",
) -> PriceCalibration:
    """Find a constant Z-spread that reprices model PV to an observed dirty price."""
    observed = _finite(observed_dirty_per_100, "observed dirty price")
    if observed <= 0:
        raise ModelInputError("Observed dirty price used for curve calibration must be positive.")
    if reference_side not in {"bid", "mid", "indicative_bid"}:
        raise ModelInputError("Curve calibration must use a declared bid-side or mid reference.")
    uncalibrated = _price_with_constant_oas(bond, curve, valuation_date, 0.0)
    # PV is monotone decreasing in the Z-spread.  Expand a generous bracket rather
    # than using a fragile closed-form approximation for irregular cash flows.
    low, high = -1.0, 1.0
    for _ in range(12):
        if _price_with_constant_oas(bond, curve, valuation_date, low) >= observed:
            break
        low *= 2.0
    else:
        raise ModelInputError("Could not bracket a negative calibration Z-spread.")
    for _ in range(12):
        if _price_with_constant_oas(bond, curve, valuation_date, high) <= observed:
            break
        high *= 2.0
    else:
        raise ModelInputError("Could not bracket a positive calibration Z-spread.")
    for _ in range(100):
        middle = (low + high) / 2.0
        value = _price_with_constant_oas(bond, curve, valuation_date, middle)
        if value > observed:
            low = middle
        else:
            high = middle
    oas = (low + high) / 2.0
    repriced = _price_with_constant_oas(bond, curve, valuation_date, oas)
    if abs(repriced - observed) > 1.0e-7:
        raise ModelInputError("Curve calibration did not reprice the observed dirty price.")
    return PriceCalibration(reference_side, observed, uncalibrated, oas * 10_000.0, repriced, valuation_date)


@dataclass(frozen=True)
class SettlementCalendar:
    """A declared settlement rule.

    A weekend-only calendar is admissible only for the educational Model Mode.
    An empirical implementation must pass a complete official calendar of
    explicitly classified dates; it must never infer Vietnamese holidays.
    """

    lag_business_days: int
    business_days: frozenset[date] | None = None
    label: str = "weekend-only-method-assumption"

    def __post_init__(self) -> None:
        if isinstance(self.lag_business_days, bool) or not isinstance(self.lag_business_days, int):
            raise ModelInputError("Settlement lag must be an exact non-negative whole number of business days.")
        if self.lag_business_days < 0:
            raise ModelInputError("Settlement lag cannot be negative.")
        if self.business_days is not None and not self.business_days:
            raise ModelInputError("An explicit settlement calendar cannot be empty.")

    @property
    def is_method_only(self) -> bool:
        return self.business_days is None

    def is_business_day(self, day: date) -> bool:
        return day in self.business_days if self.business_days is not None else day.weekday() < 5

    def following_business_day(self, day: date) -> date:
        cursor = day
        # A finite guard catches an incomplete explicit calendar rather than
        # silently treating unlisted dates as holidays forever.
        for _ in range(370):
            if self.is_business_day(cursor):
                return cursor
            cursor += timedelta(days=1)
        raise ModelInputError("Settlement calendar has no available following business day.")

    def settlement_date(self, execution_date: date) -> date:
        if not self.is_business_day(execution_date):
            raise ModelInputError("Execution date must be a business day under the declared settlement calendar.")
        cursor, counted = execution_date, 0
        while counted < self.lag_business_days:
            cursor += timedelta(days=1)
            if self.is_business_day(cursor):
                counted += 1
            if (cursor - execution_date).days > 370:
                raise ModelInputError("Settlement calendar cannot establish the stated settlement date.")
        return cursor


@dataclass(frozen=True)
class Scenario:
    """A predeclared conditional path used for sensitivity, not a forecast claim."""

    name: str
    probability: float
    terminal_level_shift_bps: float
    terminal_slope_shift_bps: float
    cash_rate_decimal: float
    sell_haircut_bps: float = 0.0

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ModelInputError("Every scenario needs a name.")
        if not math.isfinite(self.probability) or self.probability < 0:
            raise ModelInputError("Scenario probability must be finite and non-negative.")
        if not math.isfinite(self.cash_rate_decimal):
            raise ModelInputError("Scenario cash rate must be finite.")
        if not math.isfinite(self.terminal_level_shift_bps) or not math.isfinite(self.terminal_slope_shift_bps):
            raise ModelInputError("Scenario curve shifts must be finite.")
        if not math.isfinite(self.sell_haircut_bps) or not 0 <= self.sell_haircut_bps < 10_000:
            raise ModelInputError("Scenario haircut must be finite and lie in [0, 10,000) bps so sale proceeds remain non-negative.")

    def cash_factor(self, start: date, terminal: date) -> float:
        return math.exp(self.cash_rate_decimal * year_fraction(start, terminal))

    def curve_on(self, base_curve: ZeroCurve, curve_value_date: date, sale_value_date: date, terminal_date: date) -> ZeroCurve:
        """Linear stress path on value dates: zero at q0, full shock at H.

        The input curve and calibrated bid are both anchored at q0, the
        current settlement. Starting this clock at execution t instead would
        add a shock to the very curve being used to fit today's price.
        """
        full_horizon = year_fraction(curve_value_date, terminal_date)
        elapsed = year_fraction(curve_value_date, sale_value_date)
        progress = 0.0 if full_horizon <= EPS else min(1.0, elapsed / full_horizon)
        return base_curve.shifted(
            self.terminal_level_shift_bps * progress,
            self.terminal_slope_shift_bps * progress,
        )


@dataclass(frozen=True)
class ExecutionAssumptions:
    buy_fee_bps: float
    sell_fee_bps: float
    settlement_calendar: SettlementCalendar

    def __post_init__(self) -> None:
        if not 0 <= self.buy_fee_bps < 10_000 or not 0 <= self.sell_fee_bps < 10_000:
            raise ModelInputError("Execution fees must lie in [0, 10,000) bps so trade cash flows remain economically valid.")


@dataclass(frozen=True)
class CandidateValue:
    execution_date: date
    settlement_date: date
    month_index: int
    scenario_terminal_wealth_per_100: Mapping[str, float]
    expected_terminal_wealth_per_100: float
    worst_terminal_wealth_per_100: float
    maximum_regret_per_100: float
    robust_support: float
    near_optimal_expected: bool
    in_robust_zone: bool
    rate_cushion_by_scenario: Mapping[str, Mapping[str, float | str | None]] | None = None
    worst_case_rate_cushion_bps: float | None = None


@dataclass(frozen=True)
class CostBasis:
    purchase_date: date
    purchase_dirty_per_100: float
    current_bid_dirty_per_100: float
    unrealised_dirty_pnl_per_100: float


@dataclass(frozen=True)
class HoldToMaturityValue:
    """Contractual redemption alternative for an existing holder only.

    This value is present only when H is on/after maturity.  In Model Mode,
    contractual coupon and principal payments are assumed investible on their
    stated payment dates.  An empirical implementation must replace that
    timing assumption with documented payment-availability evidence.
    """

    maturity_date: date
    scenario_terminal_wealth_per_100: Mapping[str, float]
    expected_terminal_wealth_per_100: float
    worst_terminal_wealth_per_100: float
    maximum_regret_per_100: float
    robust_support: float
    in_robust_zone: bool


@dataclass(frozen=True)
class CashMovement:
    """One signed cash movement and its value at the comparison horizon."""

    kind: str
    cash_date: date
    amount_per_100: float
    terminal_value_per_100: float


def _cash_movement(kind: str, cash_date: date, amount: float, scenario: Scenario, terminal: date) -> CashMovement:
    return CashMovement(kind, cash_date, amount, amount * scenario.cash_factor(cash_date, terminal))


@dataclass(frozen=True)
class DecisionAnalysis:
    """A model-only decision surface and its declared guard state."""

    mode: str
    status: str
    decision_date: date
    terminal_date: date
    baseline_label: str
    baseline_expected_terminal_wealth_per_100: float
    baseline_by_scenario_per_100: Mapping[str, float]
    epsilon_terminal_per_100: float
    candidates: tuple[CandidateValue, ...]
    robust_zone_dates: tuple[date, ...]
    representative_exit_date: date | None
    expected_best_exit_date: date | None
    expected_edge_vs_baseline_per_100: float | None
    worst_edge_vs_baseline_per_100: float | None
    action: str
    warnings: tuple[str, ...]
    price_calibration: PriceCalibration
    hold_to_maturity: HoldToMaturityValue | None = None
    cost_basis: CostBasis | None = None
    cash_ledgers: Mapping[str, Mapping[str, tuple[CashMovement, ...]]] | None = None
    baseline_cash_ledger: Mapping[str, tuple[CashMovement, ...]] | None = None
    candidate_frequency: str = "monthly"
    expected_maximiser_kind: str = "sale"
    expected_maximiser_date: date | None = None
    expected_maximiser_terminal_wealth_per_100: float | None = None
    expected_maximiser_values_per_100: Mapping[str, float] | None = None
    expected_runner_up_kind: str | None = None
    expected_runner_up_date: date | None = None
    expected_runner_up_terminal_wealth_per_100: float | None = None
    expected_decision_margin_per_100: float | None = None
    expected_margin_exceeds_epsilon: bool | None = None
    scenario_weights: Mapping[str, float] | None = None


def _weighted_mean(values: Iterable[tuple[float, float]]) -> float:
    numerator, denominator = 0.0, 0.0
    for value, weight in values:
        numerator += value * weight
        denominator += weight
    if denominator <= EPS:
        raise ModelInputError("Scenario probabilities must have a positive total.")
    return numerator / denominator


def _expected_decision_margin(
    options: Sequence[tuple[str, date | None, float, Mapping[str, float]]],
    chosen_kind: str,
    chosen_date: date | None,
    chosen_expected: float,
    epsilon_terminal_per_100: float,
) -> tuple[str | None, date | None, float | None, float | None, bool | None]:
    """Return the runner-up and whether the expected winner is material.

    This diagnostic does not alter the objective. It stops a numerical maximum
    from being presented as an economically distinguishable choice when it is
    within the predeclared terminal-wealth tolerance of the runner-up.
    """
    remainder = [item for item in options if not (item[0] == chosen_kind and item[1] == chosen_date)]
    if not remainder:
        return None, None, None, None, None
    runner = max(remainder, key=lambda item: (item[2], -item[1].toordinal() if item[1] else 0))
    margin = max(0.0, chosen_expected - runner[2])
    return runner[0], runner[1], runner[2], margin, margin > epsilon_terminal_per_100 + EPS


def _choose_expected_option(
    options: Sequence[tuple[str, date | None, float, Mapping[str, float]]],
    *,
    prefer_no_buy_cash_on_numeric_tie: bool = False,
) -> tuple[str, date | None, float, Mapping[str, float]]:
    """Maximise expectation while resolving only machine-precision ties.

    `epsilon_terminal` is an economic diagnostic and must not alter the
    objective. `EPS` is different: it prevents round-off in equal cash-flow
    identities from selecting an arbitrary later day.
    """
    best_value = max(item[2] for item in options)
    tied = [item for item in options if item[2] >= best_value - EPS]
    if prefer_no_buy_cash_on_numeric_tie:
        cash = next((item for item in tied if item[0] == "no_buy_cash"), None)
        if cash is not None:
            return cash
    return min(tied, key=lambda item: item[1].toordinal() if item[1] else date.max.toordinal())


def _normalise_scenarios(scenarios: Sequence[Scenario]) -> tuple[Scenario, ...]:
    if not scenarios:
        raise ModelInputError("At least one scenario is required.")
    names = [item.name for item in scenarios]
    if len(names) != len(set(names)):
        raise ModelInputError("Scenario names must be unique.")
    total = sum(item.probability for item in scenarios)
    if total <= EPS:
        raise ModelInputError("Scenario probabilities must sum to a positive number.")
    return tuple(
        Scenario(
            item.name,
            item.probability / total,
            item.terminal_level_shift_bps,
            item.terminal_slope_shift_bps,
            item.cash_rate_decimal,
            item.sell_haircut_bps,
        )
        for item in scenarios
        if item.probability > 0
    )


def _eligible_monthly_dates(
    *,
    anchor: date,
    first_month: int,
    terminal_date: date,
    bond: FixedRateBullet,
    calendar: SettlementCalendar,
    earliest_execution_date: date,
    frequency: str = "monthly",
) -> tuple[tuple[int, date, date], ...]:
    """Generate month-labelled business-day sale candidates with settled cash by H."""
    if frequency not in {"monthly", "business_day"}:
        raise ModelInputError("candidate_frequency must be monthly or business_day.")
    if (terminal_date - anchor).days > 366 * 20:
        raise ModelInputError("Model horizon is limited to 20 years; it will not silently truncate a search.")
    candidates: list[tuple[int, date, date]] = []
    seen: set[date] = set()
    latest_execution = min(terminal_date, bond.maturity_date - timedelta(days=1))
    def append_candidate(execution: date, month: int) -> bool:
        if execution < earliest_execution_date or execution > latest_execution or not calendar.is_business_day(execution):
            return False
        # A settlement beyond H cannot be admissible. This bounded check
        # also avoids asking an explicit calendar for dates after H.
        remaining = (terminal_date - execution).days
        if calendar.lag_business_days and sum(calendar.is_business_day(execution + timedelta(days=i)) for i in range(1, min(remaining, 370) + 1)) < calendar.lag_business_days:
            return False
        settlement = calendar.settlement_date(execution)
        if (
            execution >= earliest_execution_date
            and settlement <= terminal_date
            and execution not in bond.payment_dates
            # Without an ISIN-level entitlement engine, an execution whose
            # settlement window crosses a payment date is just as ambiguous
            # as an execution on the payment date itself.  Do not assign that
            # coupon to either side by convenience.
            and not any(execution < payment_date <= settlement for payment_date in bond.payment_dates)
            and execution not in seen
        ):
            candidates.append((month, execution, settlement))
            seen.add(execution)
            return True
        return False

    if frequency == "business_day":
        cursor = max(anchor, earliest_execution_date)
        while cursor <= latest_execution:
            month = (cursor.year - anchor.year) * 12 + cursor.month - anchor.month
            append_candidate(cursor, month)
            cursor += timedelta(days=1)
    else:
        month = first_month
        while add_months(anchor, month) <= latest_execution:
            raw = add_months(anchor, month)
            day = calendar.following_business_day(raw)
            append_candidate(day, month)
            month += 1
        # The last feasible sale is an essential boundary alternative even
        # when H does not fall on a monthly anniversary.
        cursor = latest_execution
        while cursor >= max(anchor, earliest_execution_date):
            if cursor in seen or append_candidate(cursor, (cursor.year - anchor.year) * 12 + cursor.month - anchor.month):
                break
            cursor -= timedelta(days=1)
    if not candidates:
        raise ModelInputError("No eligible monthly exit exists after maturity, coupon, business-day and settlement guards.")
    return tuple(sorted(candidates, key=lambda item: item[1]))


def _net_model_sale_price(
    bond: FixedRateBullet,
    base_curve: ZeroCurve,
    scenario: Scenario,
    decision_date: date,
    execution_date: date,
    terminal_date: date,
    sell_fee_bps: float,
    constant_oas_decimal: float,
    settlement_date: date,
    curve_value_date: date,
) -> float:
    curve = scenario.curve_on(base_curve, curve_value_date, settlement_date, terminal_date)
    # The curve is first bridged to the observable decision-date bid through
    # a constant Z-spread.  Without this bridge, comparing today's bid with a raw
    # curve PV on a future date can manufacture a discontinuity and a false
    # riding edge before any curve movement has occurred.
    # The scenario is information at execution; its spot tenors and the
    # full/dirty invoice price are anchored at that trade's settlement.
    # Discounting to execution but paying at settlement creates phantom
    # carry when weekends change the length of the settlement gap.
    gross = _price_with_constant_oas(bond, curve, settlement_date, constant_oas_decimal)
    return gross * (1.0 - scenario.sell_haircut_bps / 10_000.0) * (1.0 - sell_fee_bps / 10_000.0)


def _holder_rate_cushions(
    *,
    bond: FixedRateBullet,
    base_curve: ZeroCurve,
    scenarios: Sequence[Scenario],
    decision_date: date,
    terminal_date: date,
    execution_date: date,
    settlement_date: date,
    execution: ExecutionAssumptions,
    constant_oas_decimal: float,
    baseline_by_scenario: Mapping[str, float],
) -> tuple[Mapping[str, Mapping[str, float | str | None]], float | None]:
    """Quantify each future exit's rate-rise cushion against sell-now.

    For one scenario, solve the additional *parallel* zero-curve shift ``δ``
    at the future sale settlement such that the holder's terminal wealth from
    waiting equals that scenario's settled sell-now wealth.  The scenario's
    declared level/slope move, cash rate and haircut remain fixed; ``δ`` is an
    extra adverse rate shock.  This is a sensitivity measure, not a forecast.

    A bounded search is intentional.  A missing root reports why rather than
    presenting an arbitrary large number as a risk cushion.
    """
    if execution_date == decision_date:
        return ({
            scenario.name: {
                "additional_parallel_shift_bps": None,
                "status": "not_applicable_sell_now_baseline",
            }
            for scenario in scenarios
        }, None)

    coupon_terminal_by_scenario = {
        scenario.name: sum(
            flow.amount_per_100 * scenario.cash_factor(flow.payment_date, terminal_date)
            for flow in bond.cashflows
            if decision_date < flow.payment_date < execution_date
        )
        for scenario in scenarios
    }
    output: dict[str, dict[str, float | str | None]] = {}
    roots: list[float] = []
    all_roots = True
    for scenario in scenarios:
        curve_at_settlement = scenario.curve_on(
            base_curve, execution.settlement_calendar.settlement_date(decision_date), settlement_date, terminal_date
        )
        sale_cash_factor = scenario.cash_factor(settlement_date, terminal_date)
        coupon_terminal = coupon_terminal_by_scenario[scenario.name]
        baseline = baseline_by_scenario[scenario.name]

        def difference(extra_parallel_shift_bps: float) -> float:
            stressed_curve = curve_at_settlement.shifted(extra_parallel_shift_bps, 0.0)
            gross = _price_with_constant_oas(
                bond, stressed_curve, settlement_date, constant_oas_decimal
            )
            net = gross * (1.0 - scenario.sell_haircut_bps / 10_000.0) * (
                1.0 - execution.sell_fee_bps / 10_000.0
            )
            return coupon_terminal + net * sale_cash_factor - baseline

        lower, upper = -RATE_CUSHION_SEARCH_BOUND_BPS, RATE_CUSHION_SEARCH_BOUND_BPS
        lower_value, upper_value = difference(lower), difference(upper)
        if lower_value < -EPS:
            output[scenario.name] = {
                "additional_parallel_shift_bps": None,
                "status": "below_minus_5000bp_search_bound",
            }
            all_roots = False
            continue
        if upper_value > EPS:
            output[scenario.name] = {
                "additional_parallel_shift_bps": None,
                "status": "above_plus_5000bp_search_bound",
            }
            all_roots = False
            continue
        for _ in range(80):
            midpoint = (lower + upper) / 2.0
            if difference(midpoint) > 0:
                lower = midpoint
            else:
                upper = midpoint
        root = (lower + upper) / 2.0
        roots.append(root)
        output[scenario.name] = {
            "additional_parallel_shift_bps": root,
            "status": "root_found_within_plus_minus_5000bp",
        }
    return output, min(roots) if all_roots and roots else None


def _score_candidates(
    raw: Sequence[tuple[int, date, date, Mapping[str, float]]],
    scenarios: Sequence[Scenario],
    epsilon_terminal_per_100: float,
    minimum_robust_support: float,
    comparison_values: Sequence[Mapping[str, float]] = (),
) -> tuple[tuple[CandidateValue, ...], date, tuple[date, ...], date | None, Mapping[str, float]]:
    """Score a conditional exit surface and return earliest robust-zone date.

    The representative exit date is deliberately **None** if the declared
    robust-zone test fails.  A minimum-regret point is still useful as a
    diagnostic but must not be promoted into a tradable instruction.
    """
    if not 0.0 < minimum_robust_support <= 1.0:
        raise ModelInputError("minimum_robust_support must lie in (0, 1]; zero would make every candidate falsely robust.")
    expected = [
        _weighted_mean((item[3][scenario.name], scenario.probability) for scenario in scenarios)
        for item in raw
    ]
    maximum_expected = max(expected)
    # First eligible candidate resolves only a numerical equality, never the
    # user-declared economic epsilon. This makes flat-curve cash identities
    # deterministic and prevents a floating-point later-date preference.
    expected_best_index = next(index for index, value in enumerate(expected) if value >= maximum_expected - EPS)
    all_value_maps = [item[3] for item in raw] + list(comparison_values)
    best_expected_all = max(
        _weighted_mean((values[s.name], s.probability) for s in scenarios)
        for values in all_value_maps
    )
    scenario_best = {
        scenario.name: max(values[scenario.name] for values in all_value_maps)
        for scenario in scenarios
    }
    output: list[CandidateValue] = []
    for index, (month, execution, settlement, values) in enumerate(raw):
        maximum_regret = max(scenario_best[name] - values[name] for name in scenario_best)
        support = _weighted_mean(
            (
                1.0 if scenario_best[scenario.name] - values[scenario.name] <= epsilon_terminal_per_100 + EPS else 0.0,
                scenario.probability,
            )
            for scenario in scenarios
        )
        output.append(CandidateValue(
            execution,
            settlement,
            month,
            dict(values),
            expected[index],
            min(values.values()),
            maximum_regret,
            support,
            best_expected_all - expected[index] <= epsilon_terminal_per_100 + EPS,
            support + EPS >= minimum_robust_support,
        ))
    robust_dates = tuple(item.execution_date for item in output if item.in_robust_zone)
    # Predeclared tie-break: act on the earliest date in the robust zone,
    # avoiding a hidden hindsight preference for later returns.
    representative = robust_dates[0] if robust_dates else None
    return tuple(output), raw[expected_best_index][1], robust_dates, representative, scenario_best


def _score_hold_to_maturity(
    values: Mapping[str, float],
    maturity_date: date,
    scenarios: Sequence[Scenario],
    epsilon_terminal_per_100: float,
    minimum_robust_support: float,
    scenario_best: Mapping[str, float],
) -> HoldToMaturityValue:
    """Score contractual redemption against all eligible sale alternatives."""
    expected = _weighted_mean((values[item.name], item.probability) for item in scenarios)
    support = _weighted_mean(
        (
            1.0 if scenario_best[item.name] - values[item.name] <= epsilon_terminal_per_100 + EPS else 0.0,
            item.probability,
        )
        for item in scenarios
    )
    return HoldToMaturityValue(
        maturity_date,
        dict(values),
        expected,
        min(values.values()),
        max(scenario_best[name] - values[name] for name in scenario_best),
        support,
        support + EPS >= minimum_robust_support,
    )


def analyse_holder(
    *,
    bond: FixedRateBullet,
    decision_date: date,
    terminal_date: date,
    current_bid_dirty_per_100: float,
    base_curve: ZeroCurve,
    scenarios: Sequence[Scenario],
    execution: ExecutionAssumptions,
    epsilon_bps: float,
    minimum_robust_support: float,
    purchase_date: date | None = None,
    purchase_dirty_per_100: float | None = None,
    candidate_frequency: str = "monthly",
) -> DecisionAnalysis:
    """Evaluate sale timing for an investor who already owns the bond."""
    bond.assert_live(decision_date)
    if not decision_date < terminal_date:
        raise ModelInputError("Holder analysis requires a terminal cash date strictly after the decision date.")
    if decision_date in bond.payment_dates:
        raise ModelInputError("A decision on a payment date needs record/ex-coupon data and is outside V3 scope.")
    bid = _finite(current_bid_dirty_per_100, "current bid")
    if bid <= 0:
        raise ModelInputError("Current bid must be positive.")
    if epsilon_bps < 0:
        raise ModelInputError("epsilon_bps cannot be negative.")
    scenarios = _normalise_scenarios(scenarios)
    candidates = _eligible_monthly_dates(
        anchor=decision_date,
        first_month=0,
        terminal_date=terminal_date,
        bond=bond,
        calendar=execution.settlement_calendar,
        earliest_execution_date=decision_date,
        frequency=candidate_frequency,
    )
    # A filtered/rolled first candidate is not necessarily a sale today.
    # Without a valid today leg we cannot label any comparison "sell_now".
    # Do not invent coupon ownership or shift the benchmark to next month.
    if not any(sale_date == decision_date for _, sale_date, _ in candidates):
        raise ModelInputError(
            "SELL_NOW_BASELINE_UNAVAILABLE: Không thể lập mốc bán ngay tại ngày quyết định "
            "với lịch giao dịch, ngày nhận tiền và quyền coupon hiện có. "
            "Mô hình dừng so sánh; không tự thay mốc bằng ngày bán trong tương lai."
        )
    current_settlement = execution.settlement_calendar.settlement_date(decision_date)
    calibration = calibrate_constant_oas(
        bond, base_curve, current_settlement, bid, reference_side="bid"
    )
    constant_oas_decimal = calibration.constant_oas_bps / 10_000.0
    raw: list[tuple[int, date, date, Mapping[str, float]]] = []
    ledgers: dict[str, dict[str, tuple[CashMovement, ...]]] = {}
    for month, sale_date, settlement in candidates:
        ledgers[sale_date.isoformat()] = {}
        scenario_values: dict[str, float] = {}
        for scenario in scenarios:
            net_sale = (
                bid * (1.0 - execution.sell_fee_bps / 10_000.0)
                if sale_date == decision_date
                else _net_model_sale_price(
                    bond, base_curve, scenario, decision_date, sale_date,
                    terminal_date, execution.sell_fee_bps, constant_oas_decimal,
                    settlement, current_settlement,
                )
            )
            movements = tuple(
                _cash_movement("coupon", flow.payment_date, flow.amount_per_100, scenario, terminal_date)
                for flow in bond.cashflows
                if decision_date < flow.payment_date < sale_date
            ) + (_cash_movement("sale_net", settlement, net_sale, scenario, terminal_date),)
            ledgers[sale_date.isoformat()][scenario.name] = movements
            scenario_values[scenario.name] = sum(item.terminal_value_per_100 for item in movements)
        raw.append((month, sale_date, settlement, scenario_values))

    # Hold-to-maturity becomes a commensurable cash alternative only once H
    # is on/after redemption.  It is deliberately absent for H < maturity:
    # calling an unsold bond's model mark at H “cash” would mix a security
    # valuation with settled sale proceeds.
    hold_values: dict[str, float] | None = None
    if terminal_date >= bond.maturity_date:
        ledgers["hold_to_maturity"] = {
            scenario.name: tuple(
                _cash_movement(
                    "redemption" if flow.payment_date == bond.maturity_date else "coupon",
                    flow.payment_date, flow.amount_per_100, scenario, terminal_date,
                ) for flow in bond.cashflows
                if decision_date < flow.payment_date <= bond.maturity_date
            ) for scenario in scenarios
        }
        hold_values = {
            scenario.name: sum(
                flow.terminal_value_per_100
                for flow in ledgers["hold_to_maturity"][scenario.name]
            )
            for scenario in scenarios
        }
    baseline_by_scenario = next(values for _, sale_date, _, values in raw if sale_date == decision_date)
    baseline_expected = _weighted_mean((baseline_by_scenario[item.name], item.probability) for item in scenarios)
    epsilon_terminal = baseline_expected * epsilon_bps / 10_000.0
    scored, expected_best, robust_zone, representative, scenario_best = _score_candidates(
        raw, scenarios, epsilon_terminal, minimum_robust_support,
        [hold_values] if hold_values is not None else (),
    )
    # Alongside the scenario surface, expose a direct holder-facing
    # sensitivity: how much *additional parallel yield rise* each future
    # exit can absorb before its terminal cash equals settled sell-now.
    # This never enters the objective; it explains the fragility of waiting.
    cushioned: list[CandidateValue] = []
    for item in scored:
        rate_cushions, worst_rate_cushion = _holder_rate_cushions(
            bond=bond,
            base_curve=base_curve,
            scenarios=scenarios,
            decision_date=decision_date,
            terminal_date=terminal_date,
            execution_date=item.execution_date,
            settlement_date=item.settlement_date,
            execution=execution,
            constant_oas_decimal=constant_oas_decimal,
            baseline_by_scenario=baseline_by_scenario,
        )
        cushioned.append(replace(
            item,
            rate_cushion_by_scenario=rate_cushions,
            worst_case_rate_cushion_bps=worst_rate_cushion,
        ))
    scored = tuple(cushioned)
    robust_representative = next((item for item in scored if item.execution_date == representative), None)
    expected_best_candidate = next(item for item in scored if item.execution_date == expected_best)
    hold_to_maturity = (
        _score_hold_to_maturity(
            hold_values, bond.maturity_date, scenarios, epsilon_terminal,
            minimum_robust_support, scenario_best,
        )
        if hold_values is not None
        else None
    )

    # The economic objective and the robustness diagnostic must never be
    # conflated.  The main answer is the maximum scenario-weighted terminal
    # wealth among every stated alternative.  The robust zone is reported as
    # a sensitivity diagnostic only; it does not silently change the target.
    options: list[tuple[str, date | None, float, Mapping[str, float]]] = [
        ("sale", item.execution_date, item.expected_terminal_wealth_per_100,
         item.scenario_terminal_wealth_per_100)
        for item in scored
    ]
    if hold_to_maturity is not None:
        options.append(("hold_to_maturity", bond.maturity_date,
                        hold_to_maturity.expected_terminal_wealth_per_100,
                        hold_to_maturity.scenario_terminal_wealth_per_100))
    # Earliest cash resolves an exact expected-value tie. It is declared here,
    # not inferred from whether a scenario-support filter happens to pass.
    kind, choice_date, chosen_expected, chosen_values = _choose_expected_option(options)
    runner_kind, runner_date, runner_expected, decision_margin, margin_exceeds_epsilon = _expected_decision_margin(
        options, kind, choice_date, chosen_expected, epsilon_terminal,
    )
    if kind == "hold_to_maturity":
        action = "MODEL_MAX_EXPECTED_HOLD_TO_MATURITY"
        status = "EXPECTED_MAX_HOLD_TO_MATURITY"
    else:
        action = "MODEL_MAX_EXPECTED_EXIT"
        status = "EXPECTED_MAX_EXIT"
    edge = chosen_expected - baseline_expected
    worst_edge = (
        min(chosen_values[item.name] - baseline_by_scenario[item.name] for item in scenarios)
        if chosen_values is not None
        else None
    )
    warnings = [
        "MODEL_ONLY: forecast curves, cash rates, liquidity haircuts and weekend-only settlement are declared assumptions, not observed Vietnamese trading data.",
        "Future curve PVs carry a constant Z-spread calibrated to today's observed bid. This is a declared spread-stability assumption, not evidence that credit or liquidity spread will stay constant.",
        "The chosen alternative maximises scenario-weighted terminal wealth over stated alternatives. Robust-zone support is reported separately; it is not a probability of success and does not override the objective.",
        "Decision margin compares the expected winner with the runner-up. A margin at or below epsilon means the stated tolerance cannot economically distinguish the two; it does not change which alternative maximises expectation.",
    ]
    if hold_to_maturity is not None:
        warnings.append(
            "Hold-to-maturity treats contractual coupon/redemption cash as investible on its stated payment date. This is a Model Mode payment-availability assumption; empirical use needs payment evidence."
        )
    if execution.settlement_calendar.is_method_only:
        warnings.append("Settlement uses a weekend-only calendar. An empirical study must replace it with a documented Vietnam market calendar.")
    cost_basis = None
    if purchase_date is not None or purchase_dirty_per_100 is not None:
        if purchase_date is None or purchase_dirty_per_100 is None:
            raise ModelInputError("Provide both purchase date and purchase dirty price, or neither.")
        if purchase_date > decision_date or purchase_dirty_per_100 <= 0:
            raise ModelInputError("Purchase date/price is inconsistent with the current holder state.")
        cost_basis = CostBasis(purchase_date, purchase_dirty_per_100, bid, bid - purchase_dirty_per_100)
        warnings.append("Cost basis is shown for mark-to-market context only. It does not alter a forward-looking sell/hold decision unless tax or constraints are explicitly modelled.")
    return DecisionAnalysis(
        "holder",
        status,
        decision_date,
        terminal_date,
        "sell_now",
        baseline_expected,
        dict(baseline_by_scenario),
        epsilon_terminal,
        scored,
        robust_zone,
        representative,
        expected_best,
        edge,
        worst_edge,
        action,
        tuple(warnings),
        calibration,
        hold_to_maturity,
        cost_basis,
        ledgers,
        ledgers[decision_date.isoformat()],
        candidate_frequency,
        kind,
        choice_date,
        chosen_expected,
        chosen_values,
        runner_kind,
        runner_date,
        runner_expected,
        decision_margin,
        margin_exceeds_epsilon,
        scenario_weights={s.name: s.probability for s in scenarios},
    )


def analyse_buyer(
    *,
    bond: FixedRateBullet,
    decision_date: date,
    terminal_date: date,
    current_bid_dirty_per_100: float,
    current_ask_dirty_per_100: float,
    base_curve: ZeroCurve,
    scenarios: Sequence[Scenario],
    execution: ExecutionAssumptions,
    epsilon_bps: float,
    minimum_robust_support: float,
    candidate_frequency: str = "monthly",
) -> DecisionAnalysis:
    """Compare purchasing today with retaining equivalent cash to terminal H.

    V3 intentionally uses a coupon-safe buy-to-sale scope.  It does not guess
    whether a buyer receives a coupon around record, ex-coupon and settlement
    dates.  That feature opens only with documented per-ISIN entitlement data.
    """
    bond.assert_live(decision_date)
    if not decision_date < terminal_date < bond.maturity_date:
        raise ModelInputError("Buyer analysis requires decision < terminal < maturity; redemption is separate.")
    if any(decision_date <= flow.payment_date <= terminal_date for flow in bond.cashflows):
        raise ModelInputError(
            "BUYER_COUPON_ENTITLEMENT_REQUIRED: V3 will not guess coupon ownership. Choose a horizon before the next payment or add documented ex-coupon/record-date rules."
        )
    ask = _finite(current_ask_dirty_per_100, "current ask")
    if ask <= 0:
        raise ModelInputError("Current ask must be positive.")
    bid = _finite(current_bid_dirty_per_100, "current bid")
    if bid <= 0:
        raise ModelInputError("Current bid must be positive; it calibrates the future sale-side curve PV.")
    if ask < bid:
        raise ModelInputError("Current ask cannot be below current bid in this bid/ask execution model.")
    if epsilon_bps < 0:
        raise ModelInputError("epsilon_bps cannot be negative.")
    scenarios = _normalise_scenarios(scenarios)
    entry_settlement = execution.settlement_calendar.settlement_date(decision_date)
    if entry_settlement >= terminal_date:
        raise ModelInputError("Entry settlement must precede the comparison horizon.")
    calibration = calibrate_constant_oas(
        bond, base_curve, entry_settlement, bid, reference_side="bid"
    )
    constant_oas_decimal = calibration.constant_oas_bps / 10_000.0
    entry_outlay = ask * (1.0 + execution.buy_fee_bps / 10_000.0)
    candidates = _eligible_monthly_dates(
        anchor=decision_date,
        first_month=1,
        terminal_date=terminal_date,
        bond=bond,
        calendar=execution.settlement_calendar,
        earliest_execution_date=entry_settlement + timedelta(days=1),
        frequency=candidate_frequency,
    )
    # Per-100 starting capital is defined to equal the full entry outlay.  It
    # remains in cash until settlement, which is why the debit is compounded
    # from entry settlement rather than incorrectly from the decision date.
    raw: list[tuple[int, date, date, Mapping[str, float]]] = []
    baseline_by_scenario: dict[str, float] = {}
    baseline_ledger: dict[str, tuple[CashMovement, ...]] = {}
    ledgers: dict[str, dict[str, tuple[CashMovement, ...]]] = {}
    for scenario in scenarios:
        baseline_ledger[scenario.name] = (_cash_movement("initial_cash", decision_date, entry_outlay, scenario, terminal_date),)
        baseline_by_scenario[scenario.name] = sum(item.terminal_value_per_100 for item in baseline_ledger[scenario.name])
    for month, sale_date, settlement in candidates:
        values: dict[str, float] = {}
        ledgers[sale_date.isoformat()] = {}
        for scenario in scenarios:
            net_sale = _net_model_sale_price(
                bond, base_curve, scenario, decision_date, sale_date,
                terminal_date, execution.sell_fee_bps, constant_oas_decimal,
                settlement, entry_settlement,
            )
            movements = baseline_ledger[scenario.name] + (
                _cash_movement("purchase_outlay", entry_settlement, -entry_outlay, scenario, terminal_date),
                _cash_movement("sale_net", settlement, net_sale, scenario, terminal_date),
            )
            ledgers[sale_date.isoformat()][scenario.name] = movements
            values[scenario.name] = sum(item.terminal_value_per_100 for item in movements)
        raw.append((month, sale_date, settlement, values))
    baseline_expected = _weighted_mean((baseline_by_scenario[item.name], item.probability) for item in scenarios)
    epsilon_terminal = baseline_expected * epsilon_bps / 10_000.0
    scored, expected_best, robust_zone, representative, _scenario_best = _score_candidates(
        raw, scenarios, epsilon_terminal, minimum_robust_support, [baseline_by_scenario]
    )
    expected_best_candidate = next(item for item in scored if item.execution_date == expected_best)
    # A prospective buyer has an explicit no-buy cash alternative. It is part
    # of the mathematical maximisation, not merely a later warning label.
    if baseline_expected >= expected_best_candidate.expected_terminal_wealth_per_100 - EPS:
        kind, choice_date, chosen_expected, chosen_values = "no_buy_cash", None, baseline_expected, baseline_by_scenario
        action, status = "MODEL_MAX_EXPECTED_NO_BUY_CASH", "EXPECTED_MAX_NO_BUY_CASH"
    else:
        kind, choice_date = "sale", expected_best_candidate.execution_date
        chosen_expected, chosen_values = expected_best_candidate.expected_terminal_wealth_per_100, expected_best_candidate.scenario_terminal_wealth_per_100
        action, status = "MODEL_MAX_EXPECTED_ENTRY_AND_EXIT", "EXPECTED_MAX_ENTRY_AND_EXIT"
    options: list[tuple[str, date | None, float, Mapping[str, float]]] = [
        ("no_buy_cash", None, baseline_expected, baseline_by_scenario),
        *(("sale", item.execution_date, item.expected_terminal_wealth_per_100,
           item.scenario_terminal_wealth_per_100) for item in scored),
    ]
    runner_kind, runner_date, runner_expected, decision_margin, margin_exceeds_epsilon = _expected_decision_margin(
        options, kind, choice_date, chosen_expected, epsilon_terminal,
    )
    edge = chosen_expected - baseline_expected
    worst_edge = min(chosen_values[item.name] - baseline_by_scenario[item.name] for item in scenarios)
    warnings = [
        "MODEL_ONLY: this is a cash-flow-consistent scenario comparison, not an executable BUY recommendation or empirical evidence.",
        "Future sale-side curve PVs carry a constant Z-spread calibrated to today's bid; entry uses today's ask. The bid-ask gap and fees are intentionally not erased by the calibration.",
        "Buyer scope is coupon-safe. No coupon can occur from decision through terminal until ex-coupon, record-date and settlement entitlement rules are attached to the ISIN.",
        "Initial cash equals the entry outlay per VND100 face; cash earns interest until entry settlement and sale proceeds earn interest only after sale settlement.",
        "Decision margin compares the expected winner with the runner-up. A margin at or below epsilon is economically indistinguishable under the declared tolerance; it does not change the objective.",
    ]
    if execution.settlement_calendar.is_method_only:
        warnings.append("Settlement uses a weekend-only calendar. An empirical study must use a documented Vietnam market calendar.")
    return DecisionAnalysis(
        "buyer",
        status,
        decision_date,
        terminal_date,
        "no_buy_cash",
        baseline_expected,
        baseline_by_scenario,
        epsilon_terminal,
        scored,
        robust_zone,
        representative,
        expected_best,
        edge,
        worst_edge,
        action,
        tuple(warnings),
        calibration,
        cash_ledgers=ledgers,
        baseline_cash_ledger=baseline_ledger,
        candidate_frequency=candidate_frequency,
        expected_maximiser_kind=kind,
        expected_maximiser_date=choice_date,
        expected_maximiser_terminal_wealth_per_100=chosen_expected,
        expected_maximiser_values_per_100=chosen_values,
        expected_runner_up_kind=runner_kind,
        expected_runner_up_date=runner_date,
        expected_runner_up_terminal_wealth_per_100=runner_expected,
        expected_decision_margin_per_100=decision_margin,
        expected_margin_exceeds_epsilon=margin_exceeds_epsilon,
        scenario_weights={s.name: s.probability for s in scenarios},
    )


def _payload_curve(raw_nodes: Any) -> ZeroCurve:
    if not isinstance(raw_nodes, list):
        raise ModelInputError("curve_nodes must be a list.")
    if any(not isinstance(item, Mapping) for item in raw_nodes):
        raise ModelInputError("Every curve node must be an object; malformed rows cannot be dropped.")
    return ZeroCurve(tuple(
        (_finite(item.get("tenor_years"), "curve tenor"), _finite(item.get("zero_rate_decimal"), "curve zero rate"))
        for item in raw_nodes
        if isinstance(item, Mapping)
    ))


def _payload_bond(raw: Mapping[str, Any]) -> FixedRateBullet:
    if not isinstance(raw, Mapping):
        raise ModelInputError("bond must be an object.")
    flows = raw.get("cashflows")
    if not isinstance(flows, list):
        raise ModelInputError("bond.cashflows must be a list.")
    if any(not isinstance(item, Mapping) for item in flows):
        raise ModelInputError("Every cash flow must be an object; a malformed payment cannot be dropped.")
    return FixedRateBullet(
        str(raw.get("identifier", "VND-bond")),
        parse_date(str(raw.get("issue_date", "")), "bond.issue_date"),
        tuple(
            CashFlow(
                parse_date(str(item.get("payment_date", "")), "cash-flow payment_date"),
                _finite(item.get("amount_per_100"), "cash-flow amount_per_100"),
            )
            for item in flows
            if isinstance(item, Mapping)
        ),
    )


def _payload_scenarios(raw: Any) -> tuple[Scenario, ...]:
    if not isinstance(raw, list):
        raise ModelInputError("scenarios must be a list.")
    if any(not isinstance(item, Mapping) for item in raw):
        raise ModelInputError("Every scenario must be an object; malformed scenarios cannot be dropped.")
    return tuple(Scenario(
        str(item.get("name", "")),
        _finite(item.get("probability"), "scenario probability"),
        _finite(item.get("terminal_level_shift_bps", 0), "scenario level shift"),
        _finite(item.get("terminal_slope_shift_bps", 0), "scenario slope shift"),
        _finite(item.get("cash_rate_decimal"), "scenario cash rate"),
        _finite(item.get("sell_haircut_bps", 0), "scenario haircut"),
    ) for item in raw if isinstance(item, Mapping))


def analysis_to_dict(analysis: DecisionAnalysis, position_face_value_vnd: float) -> dict[str, Any]:
    """Serialize an analysis; retain per-100 and full-position values explicitly."""
    position_face = _finite(position_face_value_vnd, "position_face_value_vnd")
    if position_face <= 0:
        raise ModelInputError("position_face_value_vnd must be positive.")
    scale = position_face / PER_100_FACE

    def money(per_100: float | None) -> dict[str, float] | None:
        if per_100 is None:
            return None
        return {"per_100_face": per_100, "for_position_vnd": per_100 * scale}

    def ledger(values: Mapping[str, tuple[CashMovement, ...]]) -> dict[str, Any]:
        return {name: [{
            "kind": flow.kind,
            "cash_date": flow.cash_date.isoformat(),
            "cash_amount": money(flow.amount_per_100),
            "reinvestment_to_terminal": money(flow.terminal_value_per_100 - flow.amount_per_100),
            "terminal_contribution": money(flow.terminal_value_per_100),
        } for flow in flows] for name, flows in values.items()}

    def breakdown(values: Mapping[str, tuple[CashMovement, ...]]) -> dict[str, Any]:
        """Presentation totals from the same signed ledger used in the objective.

        Cash amounts plus accumulation equals terminal wealth. Aggregating
        scenario-weighted cash does not imply one realised cash-flow path.
        """
        weights = analysis.scenario_weights or {}
        cash = _weighted_mean((sum(f.amount_per_100 for f in flows), weights[name])
                              for name, flows in values.items())
        accumulation = _weighted_mean((sum(f.terminal_value_per_100 - f.amount_per_100 for f in flows), weights[name])
                                      for name, flows in values.items())
        return {"net_cash_received": money(cash),
                "accumulation_to_terminal": money(accumulation),
                "terminal_wealth": money(cash + accumulation)}

    return {
        "analysis_version": "RYC-V3-model-only",
        "engine_revision": ENGINE_REVISION,
        "status": analysis.status,
        "mode": analysis.mode,
        "scenario_weights": dict(analysis.scenario_weights or {}),
        "action": analysis.action,
        "objective_audit": {
            "objective": "maximise scenario-weighted terminal wealth over included alternatives",
            "maximising_alternative": analysis.expected_maximiser_kind,
            "maximising_date": analysis.expected_maximiser_date.isoformat() if analysis.expected_maximiser_date else None,
            "maximum_expected_terminal_wealth": money(analysis.expected_maximiser_terminal_wealth_per_100),
            "selection_rule": "earliest date only resolves an exact expected-wealth tie; the robustness diagnostic does not override the objective",
            "robustness_diagnostic": "an exit date is epsilon-close to the best stated alternative within a scenario; weighted support is not a probability of success",
        },
        "expected_maximiser": {
            "kind": analysis.expected_maximiser_kind,
            "date": analysis.expected_maximiser_date.isoformat() if analysis.expected_maximiser_date else None,
            "expected_terminal_wealth": money(analysis.expected_maximiser_terminal_wealth_per_100),
            "scenario_terminal_wealth_per_100": dict(analysis.expected_maximiser_values_per_100 or {}),
        },
        "decision_margin": {
            "runner_up_kind": analysis.expected_runner_up_kind,
            "runner_up_date": analysis.expected_runner_up_date.isoformat() if analysis.expected_runner_up_date else None,
            "runner_up_expected_terminal_wealth": money(analysis.expected_runner_up_terminal_wealth_per_100),
            "expected_margin": money(analysis.expected_decision_margin_per_100),
            "economic_tolerance": money(analysis.epsilon_terminal_per_100),
            "margin_exceeds_economic_tolerance": analysis.expected_margin_exceeds_epsilon,
            "interpretation": "The objective still selects maximum expected terminal wealth. False means the winner and runner-up are not economically distinguishable at the declared epsilon.",
        },
        "decision_date": analysis.decision_date.isoformat(),
        "terminal_date": analysis.terminal_date.isoformat(),
        "baseline_label": analysis.baseline_label,
        "baseline_terminal_wealth": money(analysis.baseline_expected_terminal_wealth_per_100),
        "baseline_by_scenario_per_100": dict(analysis.baseline_by_scenario_per_100),
        "baseline_cash_ledger": ledger(analysis.baseline_cash_ledger or {}),
        "baseline_cash_breakdown": breakdown(analysis.baseline_cash_ledger or {}),
        "epsilon_terminal": money(analysis.epsilon_terminal_per_100),
        "representative_exit_date": analysis.representative_exit_date.isoformat() if analysis.representative_exit_date else None,
        "expected_best_exit_date": analysis.expected_best_exit_date.isoformat() if analysis.expected_best_exit_date else None,
        "expected_best_terminal_wealth": money(next(
            item.expected_terminal_wealth_per_100
            for item in analysis.candidates
            if item.execution_date == analysis.expected_best_exit_date
        )),
        "robust_zone_dates": [item.isoformat() for item in analysis.robust_zone_dates],
        "expected_edge_vs_baseline": money(analysis.expected_edge_vs_baseline_per_100),
        "worst_edge_vs_baseline": money(analysis.worst_edge_vs_baseline_per_100),
        "cost_basis": None if analysis.cost_basis is None else {
            "purchase_date": analysis.cost_basis.purchase_date.isoformat(),
            "purchase_dirty_per_100": analysis.cost_basis.purchase_dirty_per_100,
            "current_bid_dirty_per_100": analysis.cost_basis.current_bid_dirty_per_100,
            "unrealised_dirty_pnl": money(analysis.cost_basis.unrealised_dirty_pnl_per_100),
        },
        "price_calibration": {
            "reference_side": analysis.price_calibration.reference_side,
            "value_date": analysis.price_calibration.value_date.isoformat(),
            "spread_type": "constant_z_spread_option_free",
            "reference_dirty_per_100": analysis.price_calibration.reference_dirty_per_100,
            "uncalibrated_dirty_per_100": analysis.price_calibration.uncalibrated_dirty_per_100,
            "constant_oas_bps": analysis.price_calibration.constant_oas_bps,
            "repriced_dirty_per_100": analysis.price_calibration.repriced_dirty_per_100,
        },
        "hold_to_maturity": None if analysis.hold_to_maturity is None else {
            "maturity_date": analysis.hold_to_maturity.maturity_date.isoformat(),
            "cash_ledger": ledger((analysis.cash_ledgers or {})["hold_to_maturity"]),
            "cash_breakdown": breakdown((analysis.cash_ledgers or {})["hold_to_maturity"]),
            "expected_terminal_wealth": money(analysis.hold_to_maturity.expected_terminal_wealth_per_100),
            "worst_terminal_wealth": money(analysis.hold_to_maturity.worst_terminal_wealth_per_100),
            "maximum_regret": money(analysis.hold_to_maturity.maximum_regret_per_100),
            "robust_support": analysis.hold_to_maturity.robust_support,
            "in_robust_zone": analysis.hold_to_maturity.in_robust_zone,
            "scenario_terminal_wealth_per_100": dict(analysis.hold_to_maturity.scenario_terminal_wealth_per_100),
            "payment_availability_assumption": "contractual payment date equals cash-available date in MODEL_ONLY mode",
        },
        "candidates": [
            {
                "execution_date": item.execution_date.isoformat(),
                "settlement_date": item.settlement_date.isoformat(),
                "price_value_date": item.settlement_date.isoformat(),
                "month_index": item.month_index,
                "expected_terminal_wealth": money(item.expected_terminal_wealth_per_100),
                "expected_edge_vs_baseline": money(item.expected_terminal_wealth_per_100 - analysis.baseline_expected_terminal_wealth_per_100),
                "worst_edge_vs_baseline": money(min(
                    value - analysis.baseline_by_scenario_per_100[name]
                    for name, value in item.scenario_terminal_wealth_per_100.items()
                )),
                "scenario_edge_vs_baseline_per_100": {
                    name: value - analysis.baseline_by_scenario_per_100[name]
                    for name, value in item.scenario_terminal_wealth_per_100.items()
                },
                "cash_ledger": ledger((analysis.cash_ledgers or {}).get(item.execution_date.isoformat(), {})),
                "cash_breakdown": breakdown((analysis.cash_ledgers or {})[item.execution_date.isoformat()]),
                "worst_terminal_wealth": money(item.worst_terminal_wealth_per_100),
                "maximum_regret": money(item.maximum_regret_per_100),
                "robust_support": item.robust_support,
                "near_optimal_expected": item.near_optimal_expected,
                "in_robust_zone": item.in_robust_zone,
                "rate_cushion": None if item.rate_cushion_by_scenario is None else {
                    "definition": "Additional parallel zero-curve shift at sale settlement that makes this future holder exit equal the same-scenario settled sell-now wealth at H. It is a sensitivity, not a forecast.",
                    "search_bound_bps": RATE_CUSHION_SEARCH_BOUND_BPS,
                    "by_scenario": dict(item.rate_cushion_by_scenario),
                    "worst_case_additional_parallel_shift_bps": item.worst_case_rate_cushion_bps,
                },
                "scenario_terminal_wealth_per_100": dict(item.scenario_terminal_wealth_per_100),
            }
            for item in analysis.candidates
        ],
        "warnings": list(analysis.warnings),
        "scope": {
            "classification": "MODEL_ONLY",
            "candidate_grid": analysis.candidate_frequency,
            "optimality_scope": "maximum over evaluated eligible dates and declared scenarios, not a forecast of the realised best sale",
            "zero_weight_scenarios": "excluded from expectation, support, regret and worst-case comparisons",
            "price_basis": "dirty_price_per_100_face",
            "curve_anchor": "spot tenors start at settlement; the declared linear stress path is zero at current settlement q0 and reaches its full shock at H",
            "scenario_path": {"initial_value_date": analysis.price_calibration.value_date.isoformat(), "terminal_value_date": analysis.terminal_date.isoformat(), "progress_rule": "(sale_settlement - initial_value_date) / (H - initial_value_date)", "interpretation": "declared value-date scenario, not an estimated forecast"},
            "cash_compounding": "continuous",
            "day_count": "actual_days_divided_by_365.25_model_assumption",
            "coupon_entitlement_status": "payment-date simplification; ex-coupon and record-date entitlement not modelled",
            "holder_coupon_rule": "holder receives contractual payments strictly after decision and strictly before sale execution; payment-date execution is excluded",
            "buyer_coupon_rule": "coupon-safe only; any payment from decision through terminal blocks buyer analysis",
            "settlement_rule": "weekend-only assumption is educational only; empirical use requires an explicit documented market calendar",
        },
    }


@dataclass(frozen=True)
class ReinvestmentPath:
    """Piecewise continuous cash alternative, with no missing time interval."""

    segments: tuple[tuple[date, date, float], ...]

    def factor(self, start: date, terminal: date) -> float:
        if start > terminal:
            raise ModelInputError("Cash cannot be accumulated backward from its availability date.")
        if start == terminal:
            return 1.0
        if not self.segments or start < self.segments[0][0] or terminal > self.segments[-1][1]:
            raise ModelInputError("Reinvestment path does not cover a cash-flow date through H.")
        exponent = sum(
            rate * year_fraction(max(start, left), min(terminal, right))
            for left, right, rate in self.segments
            if max(start, left) < min(terminal, right)
        )
        return math.exp(exponent)


@dataclass(frozen=True)
class PerfectForesightScenario:
    name: str
    weight: float
    curves: Mapping[date, ZeroCurve]
    reinvestment: ReinvestmentPath
    sell_haircut_bps: float


def _pf_reinvestment_path(raw: Any, start: date, terminal: date, flat_rate: Any) -> ReinvestmentPath:
    if raw is None:
        if flat_rate is None:
            raise ModelInputError("Provide reinvestment_path or cash_rate_decimal for every scenario.")
        return ReinvestmentPath(((start, terminal, _finite(flat_rate, "cash_rate_decimal")),))
    if not isinstance(raw, list) or not raw:
        raise ModelInputError("reinvestment_path must be a non-empty list of dated rate segments.")
    segments: list[tuple[date, date, float]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ModelInputError("Every reinvestment segment must be an object.")
        left = parse_date(item.get("start_date"), "reinvestment start_date")
        right = parse_date(item.get("end_date"), "reinvestment end_date")
        rate = _finite(item.get("rate_cc_decimal"), "reinvestment rate_cc_decimal")
        if left >= right:
            raise ModelInputError("Every reinvestment segment needs start_date < end_date.")
        if segments and left != segments[-1][1]:
            raise ModelInputError("Reinvestment path has a gap or overlap.")
        segments.append((left, right, rate))
    if segments[0][0] != start or segments[-1][1] != terminal:
        raise ModelInputError("Reinvestment path must cover exactly decision_date through H.")
    return ReinvestmentPath(tuple(segments))


def _pf_curve_path(raw: Any, first_value_date: date, terminal: date) -> Mapping[date, ZeroCurve]:
    if not isinstance(raw, list) or not raw:
        raise ModelInputError("curve_path must be a non-empty list of dated zero-curve snapshots.")
    curves: dict[date, ZeroCurve] = {}
    previous: date | None = None
    for item in raw:
        if not isinstance(item, Mapping):
            raise ModelInputError("Every curve_path snapshot must be an object.")
        value_date = parse_date(item.get("value_date"), "curve_path.value_date")
        if value_date < first_value_date or value_date > terminal or (previous is not None and value_date <= previous):
            raise ModelInputError("Curve snapshots must be strictly ordered from current settlement through H.")
        curves[value_date] = _payload_curve(item.get("curve_nodes"))
        previous = value_date
    if first_value_date not in curves:
        raise ModelInputError("curve_path must contain the current settlement value date.")
    return curves


def _pf_scenarios(payload: Mapping[str, Any], decision: date, terminal: date, current_settlement: date) -> tuple[PerfectForesightScenario, ...]:
    raw_scenarios = payload.get("scenarios")
    if raw_scenarios is None:
        raw_scenarios = [{"name": "Perfect Foresight", "probability": 1}]
    if not isinstance(raw_scenarios, list) or not raw_scenarios:
        raise ModelInputError("Perfect Foresight scenarios must be a non-empty list.")
    if any(not isinstance(item, Mapping) for item in raw_scenarios):
        raise ModelInputError("Every Perfect Foresight scenario must be an object.")
    scenarios: list[PerfectForesightScenario] = []
    names: set[str] = set()
    for raw in raw_scenarios:
        name = str(raw.get("name", "")).strip()
        probability = _finite(raw.get("probability", 1), "scenario probability")
        haircut = _finite(raw.get("sell_haircut_bps", 0), "sell_haircut_bps")
        if not name or name in names or probability < 0 or not 0 <= haircut < 10_000:
            raise ModelInputError("Scenario names must be unique and non-empty; probability and haircut must be valid.")
        names.add(name)
        curves = _pf_curve_path(raw.get("curve_path", payload.get("curve_path")), current_settlement, terminal)
        reinvestment = _pf_reinvestment_path(
            raw.get("reinvestment_path", payload.get("reinvestment_path")),
            decision, terminal, raw.get("cash_rate_decimal", payload.get("cash_rate_decimal")),
        )
        scenarios.append(PerfectForesightScenario(name, probability, curves, reinvestment, haircut))
    total_weight = sum(item.weight for item in scenarios)
    if total_weight <= 0:
        raise ModelInputError("Perfect Foresight scenario weights must have positive total.")
    scenarios = [replace(item, weight=item.weight / total_weight) for item in scenarios if item.weight > 0]
    base_nodes = scenarios[0].curves[current_settlement].nodes
    if any(item.curves[current_settlement].nodes != base_nodes for item in scenarios[1:]):
        raise ModelInputError("All scenarios must share the same current settlement curve before future paths diverge.")
    return tuple(scenarios)


def _pf_accrued_interest(raw_bond: Mapping[str, Any], on_date: date, bond: FixedRateBullet) -> float | None:
    """Optional contractual accrual schedule; never infer a coupon from an annual rate."""
    raw_periods = raw_bond.get("accrual_periods")
    if raw_periods is None:
        return None
    if not isinstance(raw_periods, list) or any(not isinstance(item, Mapping) for item in raw_periods):
        raise ModelInputError("bond.accrual_periods must be a list of objects.")
    previous_end: date | None = None
    accrued: float | None = None
    for item in raw_periods:
        left = parse_date(item.get("period_start"), "accrual period_start")
        right = parse_date(item.get("period_end"), "accrual period_end")
        coupon = _finite(item.get("coupon_per_100"), "accrual coupon_per_100")
        if left >= right or coupon < 0 or (previous_end is not None and left < previous_end):
            raise ModelInputError("Accrual periods must be ordered, non-overlapping and have non-negative coupons.")
        matching_flow = next((flow for flow in bond.cashflows if flow.payment_date == right), None)
        if matching_flow is None or coupon > matching_flow.amount_per_100 + EPS:
            raise ModelInputError("Each accrual period must end on a contractual coupon date and fit its cash flow.")
        if left <= on_date < right:
            accrued = coupon * (on_date - left).days / (right - left).days
        previous_end = right
    if accrued is not None:
        return accrued
    return 0.0 if any(on_date == flow.payment_date for flow in bond.cashflows) else None


def _pf_candidates(
    raw: Any, decision: date, terminal: date, bond: FixedRateBullet, calendar: SettlementCalendar,
) -> tuple[tuple[date, date], ...]:
    if raw is None:
        generated = _eligible_monthly_dates(
            anchor=decision, first_month=0, terminal_date=terminal, bond=bond,
            calendar=calendar, earliest_execution_date=decision,
        )
        candidates = tuple(
            (execution, settlement) for _, execution, settlement in generated
            if settlement < bond.maturity_date
        )
    else:
        if not isinstance(raw, list) or not raw:
            raise ModelInputError("liquidation.candidate_dates must be a non-empty date list.")
        dates = [parse_date(item, "candidate execution date") for item in raw]
        if dates != sorted(set(dates)):
            raise ModelInputError("Candidate dates must be strictly increasing and unique.")
        candidates_list: list[tuple[date, date]] = []
        for execution in dates:
            if execution < decision or execution > terminal or execution >= bond.maturity_date or not calendar.is_business_day(execution):
                raise ModelInputError("A candidate execution date is outside the eligible bond/business-day horizon.")
            settlement = calendar.settlement_date(execution)
            if settlement > terminal or settlement >= bond.maturity_date:
                raise ModelInputError("A candidate must settle by H and before bond maturity.")
            if execution in bond.payment_dates or any(execution < day <= settlement for day in bond.payment_dates):
                raise ModelInputError("Coupon payment on execution or within settlement window needs entitlement evidence.")
            candidates_list.append((execution, settlement))
        candidates = tuple(candidates_list)
    if not candidates or candidates[0][0] != decision:
        raise ModelInputError("Perfect Foresight requires an eligible sell-now candidate at decision_date.")
    if len(candidates) > 24:
        raise ModelInputError("Phase 1 Perfect Foresight is limited to 24 candidate dates.")
    return candidates


def analyse_perfect_foresight_payload(
    payload: Mapping[str, Any], fixed_sales: Any = None,
) -> dict[str, Any]:
    """Deterministic, model-only oracle over hold, one sale and two sale tranches."""
    if payload.get("mode") != "holder":
        raise ModelInputError("perfect_foresight_v1 supports an existing holder only.")
    unsupported = set(payload).intersection({
        "coupon_carry_pct_annual", "economic_growth_pct_annual", "asset_trend_pct_annual",
        "shock_month", "shock_size_pct", "tax_bps", "holding_cost_bps_annual",
        "volatility_pct_annual", "risk_penalty_bps_per_vol_year",
    })
    if unsupported:
        raise ModelInputError("Legacy demo inputs are not pricing inputs in perfect_foresight_v1: " + ", ".join(sorted(unsupported)))
    raw_bond = payload.get("bond")
    bond = _payload_bond(raw_bond)
    if "day_count" in raw_bond:
        raise ModelInputError("A contractual day_count requires a dedicated convention adapter; current discounting uses ACT/365.25 model time.")
    decision = parse_date(payload.get("decision_date"), "decision_date")
    terminal = parse_date(payload.get("terminal_date"), "terminal_date")
    if not decision < terminal:
        raise ModelInputError("Perfect Foresight requires decision_date < H.")
    bond.assert_live(decision)
    if decision in bond.payment_dates:
        raise ModelInputError("A decision on a coupon payment date needs entitlement evidence.")
    if any(decision < flow.payment_date <= terminal for flow in bond.cashflows):
        if payload.get("coupon_entitlement_policy") != "payment_date_model_assumption":
            raise ModelInputError("Declare payment_date_model_assumption or provide an entitlement engine before crossing a coupon.")
    face = _finite(payload.get("position_face_value_vnd", 100), "position_face_value_vnd")
    if face <= 0:
        raise ModelInputError("position_face_value_vnd must be positive.")
    raw_execution = payload.get("execution")
    if not isinstance(raw_execution, Mapping):
        raise ModelInputError("execution object is required.")
    if "tax_bps" in raw_execution:
        raise ModelInputError("tax_bps is not modelled in perfect_foresight_v1; omit it or add an explicit tax ledger rule.")
    raw_days = raw_execution.get("business_days")
    if raw_days is not None and not isinstance(raw_days, list):
        raise ModelInputError("execution.business_days must be a date list.")
    calendar = SettlementCalendar(
        _whole_nonnegative_int(raw_execution.get("settlement_lag_business_days", 1), "settlement lag"),
        frozenset(parse_date(item, "business calendar date") for item in raw_days) if raw_days is not None else None,
        str(raw_execution.get("settlement_calendar_label", "weekend-only-method-assumption")),
    )
    sell_fee_bps = _finite(raw_execution.get("sell_fee_bps", 0), "sell_fee_bps")
    impact_bps = _finite(raw_execution.get("market_impact_bps_full_sale", 0), "market_impact_bps_full_sale")
    impact_exponent = _finite(raw_execution.get("market_impact_exponent", 2), "market_impact_exponent")
    fixed_fee_vnd = _finite(raw_execution.get("fixed_ticket_fee_vnd", 0), "fixed_ticket_fee_vnd")
    if not 0 <= sell_fee_bps < 10_000 or not 0 <= impact_bps < 10_000 or not 1 <= impact_exponent <= 4 or fixed_fee_vnd < 0:
        raise ModelInputError("Sale fee, impact, impact exponent or fixed ticket fee is outside Phase 1 bounds.")
    raw_liquidation = payload.get("liquidation")
    if not isinstance(raw_liquidation, Mapping):
        raise ModelInputError("liquidation object is required.")
    sale_step = _finite(raw_liquidation.get("sale_step_pct", 10), "sale_step_pct")
    lot_count_float = 100 / sale_step if sale_step > 0 else math.inf
    if not 5 <= sale_step <= 100 or not math.isfinite(lot_count_float) or abs(lot_count_float - round(lot_count_float)) > EPS:
        raise ModelInputError("sale_step_pct must divide 100 exactly and create at most 20 lots.")
    lot_count = round(lot_count_float)
    min_sale_pct = _finite(raw_liquidation.get("min_sale_pct", sale_step), "min_sale_pct")
    min_units_float = min_sale_pct / sale_step
    if not sale_step <= min_sale_pct <= 100 or abs(min_units_float - round(min_units_float)) > EPS:
        raise ModelInputError("min_sale_pct must be a positive multiple of sale_step_pct.")
    min_units = round(min_units_float)
    max_tranches = _whole_nonnegative_int(raw_liquidation.get("max_tranches", 2), "max_tranches")
    if max_tranches not in (1, 2):
        raise ModelInputError("Phase 1 supports one or two sale tranches.")
    candidates = _pf_candidates(raw_liquidation.get("candidate_dates"), decision, terminal, bond, calendar)
    current_settlement = candidates[0][1]
    if payload.get("curve_value_date") is not None and parse_date(payload.get("curve_value_date"), "curve_value_date") != current_settlement:
        raise ModelInputError("curve_value_date must match current settlement in perfect_foresight_v1.")
    scenarios = _pf_scenarios(payload, decision, terminal, current_settlement)
    required_curve_dates = {settlement for _, settlement in candidates}
    if terminal < bond.maturity_date:
        required_curve_dates.add(terminal)
    for scenario in scenarios:
        missing = required_curve_dates.difference(scenario.curves)
        if missing:
            raise ModelInputError(
                "curve_path is missing exact value-date snapshots: "
                + ", ".join(day.isoformat() for day in sorted(missing))
            )
    raw_quote = payload.get("quote")
    if raw_quote is not None and not isinstance(raw_quote, Mapping):
        raise ModelInputError("quote must be an object when supplied.")
    if isinstance(raw_quote, Mapping) and "clean_bid_per_100" in raw_quote:
        raise ModelInputError("A clean quote cannot be used as a dirty bid without an accrued-interest conversion.")
    if isinstance(raw_quote, Mapping) and raw_quote.get("bid_dirty_per_100") is None:
        raise ModelInputError("A supplied quote must contain bid_dirty_per_100; omit quote for a synthetic model price.")
    observed_bid = None
    if isinstance(raw_quote, Mapping) and raw_quote.get("bid_dirty_per_100") is not None:
        observed_bid = _finite(raw_quote.get("bid_dirty_per_100"), "bid_dirty_per_100")
        if observed_bid <= 0 or parse_date(raw_quote.get("value_date"), "quote.value_date") != current_settlement:
            raise ModelInputError("Observed dirty bid must be positive and dated at current settlement.")
    base_curve = scenarios[0].curves[current_settlement]
    calibration = (
        calibrate_constant_oas(bond, base_curve, current_settlement, observed_bid)
        if observed_bid is not None else None
    )
    z_spread = calibration.constant_oas_bps / 10_000 if calibration else 0.0
    current_bid = observed_bid if observed_bid is not None else dirty_model_price_per_100(bond, base_curve, current_settlement)
    raw_epsilon = _finite(payload.get("epsilon_bps", 10), "epsilon_bps")
    if raw_epsilon < 0:
        raise ModelInputError("epsilon_bps must be non-negative.")
    fixed_fee_per_100 = fixed_fee_vnd / (face / PER_100_FACE)
    scale = face / PER_100_FACE

    def money(amount: float) -> dict[str, float]:
        return {"per_100_face": amount, "for_position_vnd": amount * scale}

    # A single pricing pass serves every lot combination. No curve is
    # interpolated between value dates, and each quoted PV is settlement-anchored.
    prices: dict[str, dict[date, float]] = {}
    factors: dict[str, dict[date, float]] = {}
    cash_dates = {settlement for _, settlement in candidates}
    cash_dates.update(flow.payment_date for flow in bond.cashflows if decision < flow.payment_date <= terminal)
    cash_dates.add(terminal)
    for scenario in scenarios:
        prices[scenario.name] = {
            value_date: _price_with_constant_oas(bond, scenario.curves[value_date], value_date, z_spread)
            for value_date in required_curve_dates
        }
        factors[scenario.name] = {
            cash_date: scenario.reinvestment.factor(cash_date, terminal)
            for cash_date in cash_dates
        }

    def sale_components(scenario: PerfectForesightScenario, execution: date, settlement: date, units: int) -> tuple[float, float, float, float, float]:
        fraction = units / lot_count
        price = current_bid if execution == decision else prices[scenario.name][settlement]
        haircut_bps = 0.0 if execution == decision else scenario.sell_haircut_bps
        gross = fraction * price
        fee = -gross * sell_fee_bps / 10_000
        haircut = -gross * haircut_bps / 10_000
        impact = -price * impact_bps / 10_000 * fraction ** impact_exponent
        fixed = -fixed_fee_per_100
        return gross, fee, haircut, impact, fixed

    sale_value: dict[str, dict[tuple[date, int], float]] = {}
    for scenario in scenarios:
        sale_value[scenario.name] = {}
        for execution, settlement in candidates:
            factor = factors[scenario.name][settlement]
            for units in range(min_units, lot_count + 1):
                sale_value[scenario.name][(execution, units)] = sum(
                    sale_components(scenario, execution, settlement, units)
                ) * factor

    def score(sales: tuple[tuple[date, date, int], ...]) -> tuple[dict[str, float], float]:
        remaining_units = lot_count - sum(units for _, _, units in sales)
        if remaining_units < 0:
            raise ModelInputError("Sale fractions exceed the initial position.")
        values: dict[str, float] = {}
        for scenario in scenarios:
            total = sum(sale_value[scenario.name][(execution, units)] for execution, _, units in sales)
            for flow in bond.cashflows:
                if decision < flow.payment_date <= terminal:
                    held_units = lot_count - sum(units for execution, _, units in sales if execution < flow.payment_date)
                    total += (held_units / lot_count) * flow.amount_per_100 * factors[scenario.name][flow.payment_date]
            if terminal < bond.maturity_date:
                mark = prices[scenario.name][terminal] * (1 - scenario.sell_haircut_bps / 10_000)
                total += (remaining_units / lot_count) * mark
            values[scenario.name] = total
        expected = sum(values[scenario.name] * scenario.weight for scenario in scenarios)
        return values, expected

    def plan(kind: str, sales: tuple[tuple[date, date, int], ...]) -> dict[str, Any]:
        values, expected = score(sales)
        return {"kind": kind, "sales": sales, "scenario_values": values, "expected": expected}

    hold = plan("hold_to_h", ())
    best = hold
    runner: dict[str, Any] | None = None
    best_single: dict[str, Any] | None = None
    best_two: dict[str, Any] | None = None
    single_surface: list[dict[str, Any]] = []
    evaluated = {"hold": 1, "single_sale": 0, "two_tranche": 0}

    def consider(candidate: dict[str, Any]) -> None:
        nonlocal best, runner
        if candidate["expected"] > best["expected"] + EPS:
            runner, best = best, candidate
        elif runner is None or candidate["expected"] > runner["expected"] + EPS:
            runner = candidate

    if fixed_sales is None:
        for execution, settlement in candidates:
            for units in range(min_units, lot_count + 1):
                candidate = plan("single_sale", ((execution, settlement, units),))
                evaluated["single_sale"] += 1
                if best_single is None or candidate["expected"] > best_single["expected"] + EPS:
                    best_single = candidate
                consider(candidate)
                single_surface.append({
                    "execution_date": execution.isoformat(), "settlement_date": settlement.isoformat(),
                    "sale_pct_initial_face": 100 * units / lot_count,
                    "expected_terminal_wealth": money(candidate["expected"]),
                    "scenario_terminal_wealth_per_100": candidate["scenario_values"],
                })
        if max_tranches == 2:
            for first_index, (first_day, first_settlement) in enumerate(candidates):
                for second_day, second_settlement in candidates[first_index + 1:]:
                    for first_units in range(min_units, lot_count + 1):
                        for second_units in range(min_units, lot_count - first_units + 1):
                            candidate = plan("two_tranche", (
                                (first_day, first_settlement, first_units),
                                (second_day, second_settlement, second_units),
                            ))
                            evaluated["two_tranche"] += 1
                            if best_two is None or candidate["expected"] > best_two["expected"] + EPS:
                                best_two = candidate
                            consider(candidate)

    def serialize(chosen: dict[str, Any] | None) -> dict[str, Any] | None:
        if chosen is None:
            return None
        sales = chosen["sales"]
        remaining = 1 - sum(units / lot_count for _, _, units in sales)
        ledgers: dict[str, list[dict[str, Any]]] = {}
        for scenario in scenarios:
            entries: list[dict[str, Any]] = []

            def add(kind: str, when: date, amount: float, is_cash: bool = True) -> None:
                entries.append({
                    "kind": kind, "cash_date": when.isoformat(), "is_cash": is_cash,
                    "cash_amount": money(amount),
                    "terminal_contribution": money(amount * factors[scenario.name][when]),
                })

            for execution, settlement, units in sales:
                for kind, amount in zip(
                    ("sale_gross", "sell_fee", "liquidity_haircut", "market_impact", "fixed_ticket_fee"),
                    sale_components(scenario, execution, settlement, units),
                ):
                    add(kind, settlement, amount)
            for flow in bond.cashflows:
                if decision < flow.payment_date <= terminal:
                    held = 1 - sum(units / lot_count for execution, _, units in sales if execution < flow.payment_date)
                    add("redemption" if flow.payment_date == bond.maturity_date else "coupon", flow.payment_date, held * flow.amount_per_100)
            if terminal < bond.maturity_date and remaining > EPS:
                mark = prices[scenario.name][terminal] * (1 - scenario.sell_haircut_bps / 10_000)
                add("model_mark_proxy", terminal, remaining * mark, False)
            if abs(sum(row["terminal_contribution"]["per_100_face"] for row in entries) - chosen["scenario_values"][scenario.name]) > 1e-7:
                raise AssertionError("Perfect Foresight ledger does not reconcile with the scored objective.")
            ledgers[scenario.name] = entries
        return {
            "kind": chosen["kind"],
            "sales": [{
                "execution_date": execution.isoformat(),
                "settlement_date": sale_settlement.isoformat(),
                "sale_pct_initial_face": 100 * units / lot_count,
                "model_dirty_bid_proxy_per_100": current_bid if execution == decision else {
                    scenario.name: prices[scenario.name][sale_settlement] for scenario in scenarios
                },
            } for execution, sale_settlement, units in sales],
            "total_sold_pct_initial_face": 100 * (1 - remaining),
            "remaining_pct_initial_face": 100 * remaining,
            "expected_terminal_wealth": money(chosen["expected"]),
            "scenario_terminal_wealth_per_100": chosen["scenario_values"],
            "cash_ledger": ledgers,
            "terminal_value_basis": "MODEL_MARK_PROXY" if terminal < bond.maturity_date and remaining > EPS else "CONTRACTUAL_CASH_OR_SALE",
        }

    if fixed_sales is not None:
        if not isinstance(fixed_sales, Mapping) or not isinstance(fixed_sales.get("sales"), list):
            raise ModelInputError("A fixed plan must be an object containing sales[].")
        raw_sales = fixed_sales["sales"]
        if len(raw_sales) > max_tranches or len(raw_sales) > 2:
            raise ModelInputError("Fixed plan exceeds the declared tranche limit.")
        candidate_map = dict(candidates)
        chosen_sales: list[tuple[date, date, int]] = []
        previous_execution: date | None = None
        for item in raw_sales:
            if not isinstance(item, Mapping):
                raise ModelInputError("Each fixed sale must be an object.")
            execution = parse_date(item.get("execution_date"), "fixed sale execution_date")
            if execution not in candidate_map or (previous_execution is not None and execution <= previous_execution):
                raise ModelInputError("Fixed sales must use strictly increasing candidate execution dates.")
            settlement = candidate_map[execution]
            if item.get("settlement_date") is not None and parse_date(item["settlement_date"], "fixed sale settlement_date") != settlement:
                raise ModelInputError("Fixed sale settlement differs from the declared calendar.")
            percent = _finite(item.get("sale_pct_initial_face"), "fixed sale_pct_initial_face")
            unit_float = percent / sale_step
            if not math.isfinite(unit_float) or abs(unit_float - round(unit_float)) > EPS or not min_units <= round(unit_float) <= lot_count:
                raise ModelInputError("Fixed sale must be a valid positive lot multiple.")
            chosen_sales.append((execution, settlement, round(unit_float)))
            previous_execution = execution
        if sum(units for _, _, units in chosen_sales) > lot_count:
            raise ModelInputError("Fixed sales exceed the initial position.")
        kind = ("hold_to_h", "single_sale", "two_tranche")[len(chosen_sales)]
        if fixed_sales.get("kind") not in (None, kind):
            raise ModelInputError("Fixed plan kind does not match the sale count.")
        chosen = serialize(plan(kind, tuple(chosen_sales)))
        return {
            "analysis_version": "RYC-V3-fixed-plan-v1", "engine_revision": ENGINE_REVISION,
            "model_variant": "perfect_foresight_v1", "status": "MODEL_ONLY",
            "evaluation": "fixed_plan_no_optimization", "decision_date": decision.isoformat(),
            "terminal_date": terminal.isoformat(), "scenario_weights": {s.name: s.weight for s in scenarios},
            "position_face_value_vnd": face, "fixed_plan": chosen,
            "scope": {"evidence_tier": "MODEL_ONLY", "terminal_mark": chosen["terminal_value_basis"],
                      "day_count": "actual_days_divided_by_365.25_model_assumption",
                      "settlement_calendar": calendar.label},
        }

    sell_now = plan("single_sale", ((candidates[0][0], candidates[0][1], lot_count),))
    tolerance = abs(hold["expected"]) * raw_epsilon / 10_000
    margin = best["expected"] - runner["expected"] if runner is not None else None
    current_ai = _pf_accrued_interest(raw_bond, current_settlement, bond)
    current_clean = current_bid - current_ai if current_ai is not None else None
    return {
        "analysis_version": "RYC-V3-perfect-foresight-v1",
        "engine_revision": ENGINE_REVISION,
        "model_variant": "perfect_foresight_v1",
        "status": "MODEL_ONLY",
        "mode": "holder",
        "objective_audit": {
            "objective": "maximise scenario-weighted terminal wealth W_cash_H over hold, one sale and up to two tranches",
            "selection_rule": "hold and fewer trades resolve numeric ties; risk diagnostic does not change the maximum",
            "mark_interpretation": "unmatured residual is a dirty bid model proxy at H, not settled cash or an observed fill",
        },
        "scope": {
            "evidence_tier": "MODEL_ONLY",
            "oracle": "declared future curve and reinvestment paths are known by assumption",
            "curve_time_interpolation": "none; exact value-date snapshots required",
            "curve_tenor_interpolation": "linear spot interpolation as in V3",
            "price_value_date": "sale settlement",
            "terminal_mark": "MODEL_MARK_PROXY, not an observed fill" if terminal < bond.maturity_date else "contractual redemption",
            "coupon_entitlement": payload.get("coupon_entitlement_policy", "no_coupon_inside_horizon"),
            "day_count": "actual_days_divided_by_365.25_model_assumption",
            "accrued_interest": "actual days in declared coupon period, model-only" if current_ai is not None else "unavailable",
            "settlement_calendar": calendar.label,
            "risk_penalty": "diagnostic only; not deducted from terminal wealth",
        },
        "decision_date": decision.isoformat(),
        "terminal_date": terminal.isoformat(),
        "scenario_weights": {scenario.name: scenario.weight for scenario in scenarios},
        "position_face_value_vnd": face,
        "price_calibration": {
            **asdict(calibration), "value_date": calibration.value_date.isoformat(),
        } if calibration else {
            "reference_side": "synthetic_curve_model", "reference_dirty_per_100": current_bid,
            "constant_oas_bps": 0.0, "value_date": current_settlement.isoformat(),
        },
        "current_price": {
            "value_date": current_settlement.isoformat(),
            "dirty_bid_per_100": current_bid,
            "accrued_interest_per_100": current_ai,
            "clean_bid_per_100": current_clean,
            "parallel_zero_duration_years": parallel_zero_duration_years(bond, base_curve, current_settlement, z_spread),
            "basis": "observed_input" if observed_bid is not None else "synthetic_curve_model",
        },
        "curve_path_view": {
            scenario.name: [{
                "value_date": value_date.isoformat(),
                "curve_nodes": [
                    {"tenor_years": tenor, "zero_rate_decimal": rate}
                    for tenor, rate in curve.nodes
                ],
                "maturity_tenor_years": (bond.maturity_date - value_date).days / 365.25 if value_date < bond.maturity_date else None,
                "maturity_zero_rate_decimal": curve.spot((bond.maturity_date - value_date).days / 365.25)
                if value_date < bond.maturity_date and (bond.maturity_date - value_date).days / 365.25 <= curve.max_tenor else None,
                "dirty_model_price_per_100": prices[scenario.name].get(value_date),
            } for value_date, curve in sorted(scenario.curves.items())]
            for scenario in scenarios
        },
        "hold_to_h": serialize(hold),
        "best_single_sale": serialize(best_single),
        "best_two_tranche": serialize(best_two),
        "two_tranche_option_value_vs_single_sale": money(best_two["expected"] - best_single["expected"]) if best_two is not None else None,
        "expected_maximiser": serialize(best),
        "sell_now_full_position": serialize(sell_now),
        "single_sale_surface": single_surface,
        "near_optimal_single_sales": [row for row in single_surface if best["expected"] - row["expected_terminal_wealth"]["per_100_face"] <= tolerance + EPS],
        "decision_margin": {
            "runner_up_kind": runner["kind"] if runner else None,
            "runner_up_expected_terminal_wealth": money(runner["expected"]) if runner else None,
            "expected_margin": money(margin) if margin is not None else None,
            "economic_tolerance": money(tolerance),
            "margin_exceeds_economic_tolerance": margin > tolerance + EPS if margin is not None else None,
        },
        "alternatives_evaluated": evaluated,
        "risk_diagnostic": {"R": None, "lambda": None, "J": None, "status": "NOT_ESTIMATED"},
    }


def evaluate_fixed_plan(payload: Mapping[str, Any], selected_plan: Mapping[str, Any]) -> dict[str, Any]:
    """Score a precommitted hold/one/two-sale plan on a declared path without searching.

    The caller must retain the ex-ante selected plan and supply a separate
    realised-path payload. This function never labels model marks as fills.
    """
    if not isinstance(payload, Mapping):
        raise ModelInputError("Fixed-plan payload must be a JSON object.")
    if not isinstance(selected_plan, Mapping):
        raise ModelInputError("A fixed plan must be a JSON object containing sales[].")
    if payload.get("model_variant") != "perfect_foresight_v1":
        raise ModelInputError("Fixed-plan evaluation requires perfect_foresight_v1.")
    return analyse_perfect_foresight_payload(payload, fixed_sales=selected_plan)


def analyse_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Public local-API adapter used by the V3 Decision Studio."""
    if not isinstance(payload, Mapping):
        raise ModelInputError("Request body must be a JSON object.")
    variant = payload.get("model_variant")
    if variant == "perfect_foresight_v1":
        return analyse_perfect_foresight_payload(payload)
    if variant not in (None, "v3_scenario"):
        raise ModelInputError("Unknown model_variant.")
    bond = _payload_bond(payload.get("bond", {}))
    decision_date = parse_date(str(payload.get("decision_date", "")), "decision_date")
    terminal_date = parse_date(str(payload.get("terminal_date", "")), "terminal_date")
    quote = payload.get("quote")
    if not isinstance(quote, Mapping):
        raise ModelInputError("quote object is required.")
    execution_raw = payload.get("execution")
    if not isinstance(execution_raw, Mapping):
        raise ModelInputError("execution object is required.")
    explicit_days = execution_raw.get("business_days")
    business_days = None
    if explicit_days is not None:
        if not isinstance(explicit_days, list):
            raise ModelInputError("execution.business_days must be a list when supplied.")
        business_days = frozenset(parse_date(str(item), "business calendar date") for item in explicit_days)
    execution = ExecutionAssumptions(
        _finite(execution_raw.get("buy_fee_bps", 0), "buy fee"),
        _finite(execution_raw.get("sell_fee_bps", 0), "sell fee"),
        SettlementCalendar(
            _whole_nonnegative_int(execution_raw.get("settlement_lag_business_days", 1), "settlement lag"),
            business_days,
            str(execution_raw.get("settlement_calendar_label", "weekend-only-method-assumption")),
        ),
    )
    # Public inputs may declare value dates. Reject a mismatched curve/quote
    # instead of treating rates from different anchors as interchangeable.
    declared_dates = {"curve_value_date": payload.get("curve_value_date"), "quote.value_date": quote.get("value_date")}
    for field, raw_date in declared_dates.items():
        if raw_date is not None:
            declared = parse_date(str(raw_date), field)
            expected = execution.settlement_calendar.settlement_date(decision_date)
            if declared != expected:
                raise ModelInputError(f"VALUE_DATE_MISMATCH: {field} phải là {expected.isoformat()} theo quy ước spot/giá thanh toán của mô hình.")
    common = {
        "bond": bond,
        "decision_date": decision_date,
        "terminal_date": terminal_date,
        "base_curve": _payload_curve(payload.get("curve_nodes")),
        "scenarios": _payload_scenarios(payload.get("scenarios")),
        "execution": execution,
        "epsilon_bps": _finite(payload.get("epsilon_bps", 10), "epsilon_bps"),
        "minimum_robust_support": _finite(payload.get("minimum_robust_support", 1), "minimum_robust_support"),
        "candidate_frequency": str(payload.get("candidate_frequency", "monthly")),
    }
    mode = str(payload.get("mode", ""))
    if mode == "holder":
        held = payload.get("held_position") or {}
        if not isinstance(held, Mapping):
            raise ModelInputError("held_position must be an object.")
        purchase_date = parse_date(str(held["purchase_date"]), "purchase date") if held.get("purchase_date") else None
        purchase_price = _finite(held["purchase_dirty_per_100"], "purchase dirty price") if held.get("purchase_dirty_per_100") is not None else None
        analysis = analyse_holder(
            **common,
            current_bid_dirty_per_100=_finite(quote.get("bid_dirty_per_100"), "current bid"),
            purchase_date=purchase_date,
            purchase_dirty_per_100=purchase_price,
        )
    elif mode == "buyer":
        analysis = analyse_buyer(
            **common,
            current_bid_dirty_per_100=_finite(quote.get("bid_dirty_per_100"), "current bid"),
            current_ask_dirty_per_100=_finite(quote.get("ask_dirty_per_100"), "current ask"),
        )
    else:
        raise ModelInputError("mode must be 'holder' or 'buyer'.")
    result = analysis_to_dict(analysis, _finite(payload.get("position_face_value_vnd", 100), "position face value"))
    result["scope"]["value_date_source"] = "explicit" if all(value is not None for value in declared_dates.values()) else "assumed_from_model_settlement_calendar"
    return result


def _test_bond(*, coupon_date: date | None = None) -> FixedRateBullet:
    flows = []
    if coupon_date is not None:
        flows.append(CashFlow(coupon_date, 3.0))
    flows.append(CashFlow(date(2028, 1, 2), 103.0))
    return FixedRateBullet("TEST", date(2024, 1, 2), tuple(flows))


def self_test() -> dict[str, str]:
    """Executable invariants.  These are accounting tests, not performance tests."""
    result: dict[str, str] = {}
    curve = ZeroCurve(((0.25, 0.02), (5.0, 0.025)))
    cal = SettlementCalendar(1)
    execution = ExecutionAssumptions(5, 5, cal)
    scenarios = (Scenario("base", 1.0, 0, 0, 0.02, 0),)
    bond = _test_bond()

    # An observed bid and a raw curve PV need a transparent bridge.  The
    # calibrator must reproduce the chosen bid exactly before that spread is
    # carried into future scenario prices.
    calibration = calibrate_constant_oas(bond, curve, date(2025, 1, 2), 100.0)
    if abs(calibration.repriced_dirty_per_100 - 100.0) > 1.0e-7:
        raise AssertionError("Constant-Z-spread calibration did not reprice the reference bid.")
    result["curve_bid_calibration_reprices_reference"] = "pass"

    # Settlement lag has a real cost in the engine's displayed W(H), not just
    # in a standalone compounding helper.
    d, h = date(2025, 1, 2), date(2025, 12, 31)
    holder_fast = analyse_holder(
        bond=bond, decision_date=d, terminal_date=h, current_bid_dirty_per_100=100,
        base_curve=curve, scenarios=scenarios,
        execution=ExecutionAssumptions(0, 0, SettlementCalendar(1)),
        epsilon_bps=10, minimum_robust_support=1,
    )
    holder_slow = analyse_holder(
        bond=bond, decision_date=d, terminal_date=h, current_bid_dirty_per_100=100,
        base_curve=curve, scenarios=scenarios,
        execution=ExecutionAssumptions(0, 0, SettlementCalendar(3)),
        epsilon_bps=10, minimum_robust_support=1,
    )
    fast_by_execution = {item.execution_date: item for item in holder_fast.candidates}
    slow_by_execution = {item.execution_date: item for item in holder_slow.candidates}
    common_execution = next(day for day in fast_by_execution if day in slow_by_execution and day > d)
    if not (
        fast_by_execution[common_execution].expected_terminal_wealth_per_100
        > slow_by_execution[common_execution].expected_terminal_wealth_per_100
    ):
        raise AssertionError("Engine candidate wealth did not fall when settlement was delayed.")
    result["settlement_changes_terminal_wealth"] = "pass"

    # Buyer cash remains invested until entry settlement.  Test the actual
    # engine output against the ledger identity that V2 previously violated.
    zero_coupon = FixedRateBullet("ZERO", date(2024, 1, 2), (CashFlow(date(2026, 1, 2), 100.0),))
    zero_curve = ZeroCurve(((0.25, 0.02), (5.0, 0.02)))
    zero_bid = dirty_model_price_per_100(zero_coupon, zero_curve, cal.settlement_date(d))
    buyer = analyse_buyer(
        bond=zero_coupon, decision_date=d, terminal_date=h,
        current_bid_dirty_per_100=zero_bid, current_ask_dirty_per_100=zero_bid,
        base_curve=zero_curve, scenarios=scenarios,
        execution=ExecutionAssumptions(0, 0, cal), epsilon_bps=0, minimum_robust_support=1,
    )
    buyer_first = buyer.candidates[0]
    entry = cal.settlement_date(d)
    sale_settle = buyer_first.settlement_date
    sale_price = dirty_model_price_per_100(zero_coupon, zero_curve, buyer_first.settlement_date)
    expected_baseline = zero_bid * scenarios[0].cash_factor(d, h)
    expected_candidate = (
        zero_bid * scenarios[0].cash_factor(d, h)
        - zero_bid * scenarios[0].cash_factor(entry, h)
        + sale_price * scenarios[0].cash_factor(sale_settle, h)
    )
    if abs(buyer.baseline_expected_terminal_wealth_per_100 - expected_baseline) > 1.0e-8:
        raise AssertionError("Buyer engine baseline does not use equal initial cash.")
    if abs(buyer_first.expected_terminal_wealth_per_100 - expected_candidate) > 1.0e-8:
        raise AssertionError("Buyer engine did not debit entry outlay at entry settlement.")
    result["buyer_ledger_debits_at_entry_settlement"] = "pass"

    # Flat carry with a curve-matched bid and no frictions creates no
    # artificial roll-down alpha.  This protects the curve/Z-spread bridge.
    flat_holder = analyse_holder(
        bond=zero_coupon, decision_date=d, terminal_date=h,
        current_bid_dirty_per_100=zero_bid, base_curve=zero_curve,
        scenarios=scenarios, execution=ExecutionAssumptions(0, 0, cal),
        epsilon_bps=0, minimum_robust_support=1,
    )
    # With settlement-anchored prices, flat matching discount/cash rates
    # give identical wealth across ALL dates, including Friday-to-Monday.
    flat_values = [
        item.expected_terminal_wealth_per_100
        for item in flat_holder.candidates
    ]
    if max(flat_values) - min(flat_values) > 1.0e-8:
        raise AssertionError("Flat matched carry manufactured an artificial riding edge.")
    result["flat_matched_carry_has_no_artificial_rolldown_alpha"] = "pass"

    # The rate cushion answers a holder's actual fragility question: under
    # matched flat carry, a future exit has no extra parallel-rate rise to
    # absorb before it merely ties sell-now.  It must therefore solve near
    # zero, rather than manufacture a positive cushion from the grid.
    flat_future = next(item for item in flat_holder.candidates if item.execution_date > d)
    flat_cushion = flat_future.rate_cushion_by_scenario or {}
    flat_root = flat_cushion.get("base", {}).get("additional_parallel_shift_bps")
    if flat_root is None or abs(float(flat_root)) > 1.0e-5:
        raise AssertionError("Flat matched carry did not produce a zero rate cushion.")
    result["flat_matched_carry_has_zero_rate_cushion"] = "pass"

    # A buyer may not be granted a coupon without ownership rules.
    coupon_bond = _test_bond(coupon_date=date(2025, 7, 2))
    try:
        analyse_buyer(
            bond=coupon_bond, decision_date=d, terminal_date=h,
            current_bid_dirty_per_100=99.5, current_ask_dirty_per_100=100,
            base_curve=curve, scenarios=scenarios,
            execution=execution, epsilon_bps=10, minimum_robust_support=1,
        )
        raise AssertionError("Buyer coupon guard was bypassed.")
    except ModelInputError as error:
        if "BUYER_COUPON_ENTITLEMENT_REQUIRED" not in str(error):
            raise
    result["buyer_coupon_entitlement_guard"] = "pass"

    # Holder coupon date execution is omitted from the candidate surface.
    holder = analyse_holder(
        bond=coupon_bond, decision_date=d, terminal_date=h,
        current_bid_dirty_per_100=100, base_curve=curve, scenarios=scenarios,
        execution=execution, epsilon_bps=10, minimum_robust_support=1,
    )
    if date(2025, 7, 2) in {item.execution_date for item in holder.candidates}:
        raise AssertionError("Coupon-date sale candidate was not excluded.")
    result["holder_coupon_date_excluded"] = "pass"

    # Crossing a coupon in the settlement window is also excluded.  Merely
    # excluding the trade date would still guess which party owns the coupon.
    blackout_calendar = SettlementCalendar(3)
    try:
        analyse_holder(
            bond=coupon_bond, decision_date=date(2025, 6, 30), terminal_date=date(2025, 12, 31),
            current_bid_dirty_per_100=100, base_curve=curve, scenarios=scenarios,
            execution=ExecutionAssumptions(5, 5, blackout_calendar), epsilon_bps=10, minimum_robust_support=1,
        )
        raise AssertionError("An unavailable sell-now leg was replaced by a later sale.")
    except ModelInputError as error:
        if "SELL_NOW_BASELINE_UNAVAILABLE" not in str(error):
            raise
    result["settlement_window_coupon_baseline_unavailable"] = "pass"

    # A cost basis changes presentation but not forward candidate wealth.
    a = analyse_holder(
        bond=bond, decision_date=d, terminal_date=h, current_bid_dirty_per_100=100,
        base_curve=curve, scenarios=scenarios, execution=execution,
        epsilon_bps=10, minimum_robust_support=1,
    )
    b = analyse_holder(
        bond=bond, decision_date=d, terminal_date=h, current_bid_dirty_per_100=100,
        base_curve=curve, scenarios=scenarios, execution=execution,
        epsilon_bps=10, minimum_robust_support=1,
        purchase_date=date(2024, 2, 1), purchase_dirty_per_100=97,
    )
    if [x.expected_terminal_wealth_per_100 for x in a.candidates] != [x.expected_terminal_wealth_per_100 for x in b.candidates]:
        raise AssertionError("Sunk cost changed a forward exit surface.")
    result["cost_basis_separate_from_forward_decision"] = "pass"

    # A strict robust threshold must not manufacture an exact exit point when
    # no candidate passes it.
    unstable = analyse_holder(
        bond=bond, decision_date=d, terminal_date=h, current_bid_dirty_per_100=94,
        base_curve=curve,
        scenarios=(
            Scenario("up", 0.5, 1_000, 0, 0.01, 0),
            Scenario("down", 0.5, -1_000, 0, 0.05, 0),
        ),
        execution=execution, epsilon_bps=0, minimum_robust_support=1,
    )
    if unstable.representative_exit_date is not None or unstable.expected_maximiser_kind != "sale":
        raise AssertionError("A failed robustness diagnostic was presented as a robust exit date.")
    result["no_false_robust_exit_when_unstable"] = "pass"

    # The full holder question includes contractual redemption when H reaches
    # maturity.  It must be scored as cash, not as a fabricated sale quote.
    redemption_bond = FixedRateBullet("REDEEM", date(2024, 1, 2), (CashFlow(date(2026, 1, 2), 100.0),))
    redemption_curve = ZeroCurve(((0.25, 0.05), (5.0, 0.05)))
    redemption = analyse_holder(
        bond=redemption_bond, decision_date=d, terminal_date=date(2026, 1, 2),
        current_bid_dirty_per_100=dirty_model_price_per_100(redemption_bond, redemption_curve, d),
        base_curve=redemption_curve,
        scenarios=(Scenario("base", 1.0, 0, 0, 0.0, 0),),
        execution=ExecutionAssumptions(0, 0, cal), epsilon_bps=0, minimum_robust_support=1,
    )
    if (
        redemption.hold_to_maturity is None
        or not redemption.hold_to_maturity.in_robust_zone
        or redemption.action != "MODEL_MAX_EXPECTED_HOLD_TO_MATURITY"
    ):
        raise AssertionError("Holder redemption alternative was not modelled as a cash decision.")
    result["holder_redemption_is_a_scored_cash_alternative"] = "pass"

    # Zero support would mechanically classify every candidate as robust,
    # defeating the point of an uncertainty-aware exit zone.
    try:
        analyse_holder(
            bond=bond, decision_date=d, terminal_date=h, current_bid_dirty_per_100=100,
            base_curve=curve, scenarios=scenarios, execution=execution,
            epsilon_bps=10, minimum_robust_support=0,
        )
        raise AssertionError("Zero robust support was accepted.")
    except ModelInputError as error:
        if "minimum_robust_support" not in str(error):
            raise
    result["zero_robust_support_rejected"] = "pass"

    # Sale candidates must settle by the common terminal date.
    if any(item.settlement_date > h for item in a.candidates):
        raise AssertionError("A candidate settles after terminal H.")
    result["candidate_settlement_before_terminal"] = "pass"
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="RYC V3 cash-flow-consistent decision engine")
    parser.add_argument("payload", nargs="?", help="Path to a V3 model payload JSON file.")
    parser.add_argument("--self-test", action="store_true", help="Run accounting invariants.")
    args = parser.parse_args()
    if not args.payload and not args.self_test:
        parser.error("Provide a V3 payload path or --self-test.")
    output: dict[str, Any] = {}
    if args.self_test:
        output["self_test"] = self_test()
    if args.payload:
        with open(args.payload, "r", encoding="utf-8") as handle:
            output["analysis"] = analyse_payload(json.load(handle))
    # Windows PowerShell sessions may still expose a legacy ANSI stdout codec.
    # The research payload deliberately contains Vietnamese labels, so make the
    # command-line artifact reproducible instead of depending on terminal codepage.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
