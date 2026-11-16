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
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence


DAYS_PER_YEAR = 365.25
EPS = 1.0e-9
PER_100_FACE = 100.0
ENGINE_REVISION = "2026-09-28-shared-cash-factor"
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


def cash_accumulation_factor(start: date | datetime, terminal: date | datetime, rate: float) -> float:
    """Shared MODEL_ONLY continuous factor; aware instants preserve intraday time.

    Date inputs retain V3's day/365.25 convention. This is not a bank deposit
    or Vietnamese instrument convention and does not infer availability.
    """
    if isinstance(start, datetime) or isinstance(terminal, datetime):
        if not (isinstance(start, datetime) and isinstance(terminal, datetime)
                and start.utcoffset() is not None and terminal.utcoffset() is not None):
            raise ModelInputError("Cash instants require two timezone-aware timestamps.")
        elapsed = (terminal.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds()
        if elapsed < 0:
            raise ModelInputError("A cash interval cannot run backward.")
        fraction = elapsed / (86400 * DAYS_PER_YEAR)
    else:
        fraction = year_fraction(start, terminal)
    if not math.isfinite(rate):
        raise ModelInputError("Cash rate must be finite.")
    try:
        factor = math.exp(rate * fraction)
    except OverflowError as error:
        raise ModelInputError("Cash factor outside numeric range.") from error
    if not math.isfinite(factor) or factor <= 0:
        raise ModelInputError("Cash factor outside numeric range.")
    return factor


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
        return cash_accumulation_factor(start, terminal, self.cash_rate_decimal)

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


def analyse_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Public local-API adapter used by the V3 Decision Studio."""
    if not isinstance(payload, Mapping):
        raise ModelInputError("Request body must be a JSON object.")
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
