const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const model = require("./ryc_perfect_foresight_demo_model.js");

function result(config) {
  return model.evaluatePerfectForesight(config);
}

function maxSurfaceRow(r) {
  return r.surface.reduce((best, row) => row.objective_per_100 > best.objective_per_100 ? row : best, r.surface[0]);
}

function hasZeroSaleRows(r) {
  return r.surface.some((row) => Math.abs(row.sale_pct) < 1e-9);
}

function sameDecision(a, b) {
  return a.sale_month === b.sale_month && Math.abs(a.sale_pct - b.sale_pct) < 1e-9;
}

function mustThrow(config, text) {
  assert.throws(() => result(config), (error) => String(error.message).includes(text));
}

const steadyLowCost = result({
  asset_trend_pct_annual: 4,
  economic_growth_pct_annual: 5,
  holding_cost_bps_annual: 0,
  min_sale_pct: 100,
  max_sale_pct: 100,
  shock_size_pct: 0
});
assert.equal(steadyLowCost.optimum.sale_month, steadyLowCost.config.analysis_months, "Low-cost rising path should wait until the end when full sale is required.");

const highCarryCost = result({
  asset_trend_pct_annual: 4,
  economic_growth_pct_annual: 5,
  holding_cost_bps_annual: 1400,
  min_sale_pct: 100,
  max_sale_pct: 100,
  shock_size_pct: 0
});
assert.ok(highCarryCost.optimum.sale_month <= 1, "High holding cost should pull the full-sale optimum toward the start.");

const lowGrowth = result({ scenario: "low_growth", min_sale_pct: 100, max_sale_pct: 100 });
const highGrowth = result({ scenario: "high_growth", min_sale_pct: 100, max_sale_pct: 100 });
assert.ok(highGrowth.optimum.sale_month > lowGrowth.optimum.sale_month, "Higher growth should move the full-sale optimum later.");

const adverse = result({ scenario: "adverse_shock", min_sale_pct: 100, max_sale_pct: 100 });
assert.ok(adverse.optimum.sale_month <= adverse.config.shock_month, "Adverse shock should make the optimum avoid the post-shock region.");

const boundary = result({
  analysis_months: 1,
  min_sale_pct: 0,
  max_sale_pct: 100,
  sale_step_pct: 100,
  earliest_sale_month: 0,
  latest_sale_month: 1,
  shock_month: 1
});
assert.equal(boundary.surface.length, 2, "Boundary grid should include only real sale quantities in the optimisation surface.");
assert.equal(boundary.hold_to_h.sale_pct, 0, "Hold-to-H should still be available as a separate reference.");

mustThrow({ earliest_sale_month: 8, latest_sale_month: 4 }, "earliest_sale_month");
mustThrow({ min_sale_pct: 80, max_sale_pct: 20 }, "min_sale_pct");
mustThrow({ min_sale_pct: 0, max_sale_pct: 0 }, "q chi co 0%");

const ties = result({
  analysis_months: 6,
  current_price_per_100: 100,
  economic_growth_pct_annual: 0,
  asset_trend_pct_annual: 0,
  coupon_carry_pct_annual: 0,
  cash_rate_pct_annual: 0,
  holding_cost_bps_annual: 0,
  sell_fee_bps: 0,
  tax_bps: 0,
  market_impact_bps_full_sale: 0,
  volatility_pct_annual: 0,
  risk_penalty_bps_per_vol_year: 0,
  shock_size_pct: 0,
  min_sale_pct: 100,
  max_sale_pct: 100,
  near_optimal_bps_of_face: 1
});
assert.ok(ties.near_optimal.length > 1, "Flat zero-cost case should be reported as a near-optimal region.");

const base = result({ scenario: "base" });
assert.ok(base.optimum.sale_pct > 0, "Default optimum must be a real sale, not the q=0 hold reference.");
assert.ok(!hasZeroSaleRows(base), "Optimisation surface must exclude q=0 rows that make all months tie at the start.");
assert.ok(sameDecision(base.optimum, maxSurfaceRow(base)), "Published optimum must be the maximum row of the decision surface.");
const comparisonOptimum = base.comparisons.find((row) => row.key === "toi_uu");
assert.equal(comparisonOptimum.objective_per_100, base.optimum.objective_per_100, "Comparison table must use the same optimum as the surface.");
assert.ok(base.lift_vs_sell_now_per_100 !== null, "Lift versus sell-now must be computed.");
assert.ok(base.lift_vs_hold_to_h_per_100 !== null, "Lift versus hold-to-H must be computed.");

const secondRun = result({ scenario: "base" });
assert.deepEqual(base.timeline, secondRun.timeline, "Demo path must be deterministic for the same inputs.");
assert.deepEqual(base.optimum, secondRun.optimum, "Optimum must be reproducible for the same inputs.");

const html = fs.readFileSync(path.join(__dirname, "ryc_perfect_foresight_demo.html"), "utf8");
assert.ok(html.includes("Đây là bản demo thử nghiệm với đường giá tương lai được cho trước theo giả định.") && html.includes("đây không phải dự báo"), "UI must carry the required demo warning.");
assert.ok(html.includes("ryc_perfect_foresight_demo_model.js"), "UI must load the tested model module.");
assert.ok(!/mô hình dự báo chính xác|khuyến nghị đầu tư chắc chắn|kết quả đã được kiểm chứng/i.test(html), "UI must not use forbidden claim language.");

const script = html.match(/<script>\s*\(function\(\)\{([\s\S]*)\}\)\(\);\s*<\/script>/);
assert.ok(script, "Inline UI script should be present and parseable.");
vm.runInNewContext(`(function(){${script[1]}})`, { window: {}, document: { getElementById: () => ({ addEventListener() {}, value: "", selectedOptions: [{ textContent: "" }], classList: { add() {}, remove() {} }, style: {} }) }, Intl, setTimeout() {}, URL, Blob });

console.log(JSON.stringify({
  perfect_foresight_demo: "pass",
  checks: [
    "rising low-cost path waits",
    "high holding cost sells early",
    "high growth moves optimum later",
    "adverse shock changes timing",
    "boundary grid excludes q=0 from optimisation",
    "q=0 kept only as hold-to-H reference",
    "no feasible/invalid inputs fail closed",
    "near-optimal region",
    "surface/KPI consistency",
    "deterministic repeated run",
    "UI warning and claim hygiene"
  ]
}, null, 2));
