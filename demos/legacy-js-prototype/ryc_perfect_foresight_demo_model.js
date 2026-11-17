(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.RycPerfectForesightDemo = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const DAYS_PER_YEAR = 365.25;
  const MS_PER_DAY = 24 * 60 * 60 * 1000;

  const DEFAULT_CONFIG = Object.freeze({
    scenario: "base",
    start_date: "2026-01-05",
    analysis_months: 18,
    face_value_vnd: 100000000,
    current_price_per_100: 99.2,
    economic_growth_pct_annual: 4.0,
    asset_trend_pct_annual: 1.2,
    coupon_carry_pct_annual: 3.0,
    cash_rate_pct_annual: 4.0,
    holding_cost_bps_annual: 35,
    sell_fee_bps: 8,
    tax_bps: 0,
    market_impact_bps_full_sale: 18,
    volatility_pct_annual: 6.0,
    risk_penalty_bps_per_vol_year: 12,
    shock_month: 10,
    shock_size_pct: 0,
    min_sale_pct: 0,
    max_sale_pct: 100,
    sale_step_pct: 10,
    earliest_sale_month: 0,
    latest_sale_month: 18,
    near_optimal_bps_of_face: 6
  });

  const PRESETS = Object.freeze({
    base: {
      label: "Co so",
      overrides: {}
    },
    low_growth: {
      label: "Tang truong thap",
      overrides: {
        economic_growth_pct_annual: 1.2,
        asset_trend_pct_annual: 0.1,
        holding_cost_bps_annual: 55,
        volatility_pct_annual: 7.0,
        risk_penalty_bps_per_vol_year: 16
      }
    },
    high_growth: {
      label: "Tang truong cao",
      overrides: {
        economic_growth_pct_annual: 7.0,
        asset_trend_pct_annual: 3.0,
        holding_cost_bps_annual: 20,
        volatility_pct_annual: 5.0,
        risk_penalty_bps_per_vol_year: 8
      }
    },
    adverse_shock: {
      label: "Cu soc bat loi",
      overrides: {
        economic_growth_pct_annual: 3.0,
        asset_trend_pct_annual: 0.7,
        holding_cost_bps_annual: 45,
        volatility_pct_annual: 9.0,
        risk_penalty_bps_per_vol_year: 18,
        shock_month: 7,
        shock_size_pct: 6.5
      }
    }
  });

  function cloneDefault() {
    return Object.assign({}, DEFAULT_CONFIG);
  }

  function applyPreset(config) {
    const scenario = config.scenario || "base";
    const preset = PRESETS[scenario] || PRESETS.base;
    return Object.assign(cloneDefault(), preset.overrides, config, { scenario });
  }

  function parseDate(value, field) {
    if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) {
      throw new Error(`${field} phai dung YYYY-MM-DD.`);
    }
    const date = new Date(`${value}T00:00:00Z`);
    if (!Number.isFinite(date.getTime()) || date.toISOString().slice(0, 10) !== value) {
      throw new Error(`${field} khong phai ngay hop le.`);
    }
    return date;
  }

  function addMonths(date, months) {
    const out = new Date(date.getTime());
    const sourceDay = out.getUTCDate();
    out.setUTCDate(1);
    out.setUTCMonth(out.getUTCMonth() + months);
    const lastDay = new Date(Date.UTC(out.getUTCFullYear(), out.getUTCMonth() + 1, 0)).getUTCDate();
    out.setUTCDate(Math.min(sourceDay, lastDay));
    return out;
  }

  function iso(date) {
    return date.toISOString().slice(0, 10);
  }

  function daysBetween(start, end) {
    return Math.round((end.getTime() - start.getTime()) / MS_PER_DAY);
  }

  function yearFraction(start, end) {
    const days = daysBetween(start, end);
    if (days < 0) throw new Error("Khoang thoi gian khong duoc di nguoc.");
    return days / DAYS_PER_YEAR;
  }

  function cashFactor(fromDate, terminalDate, annualRateDecimal) {
    return Math.exp(annualRateDecimal * yearFraction(fromDate, terminalDate));
  }

  function finiteNumber(value, field) {
    const number = Number(value);
    if (!Number.isFinite(number)) throw new Error(`${field} phai la so huu han.`);
    return number;
  }

  function boundedNumber(value, field, min, max) {
    const number = finiteNumber(value, field);
    if (number < min || number > max) throw new Error(`${field} nam ngoai pham vi ${min}..${max}.`);
    return number;
  }

  function wholeNumber(value, field, min, max) {
    const number = boundedNumber(value, field, min, max);
    if (!Number.isInteger(number)) throw new Error(`${field} phai la so nguyen.`);
    return number;
  }

  function normalizeConfig(input) {
    const cfg = applyPreset(input || {});
    const out = {
      scenario: cfg.scenario,
      start_date: cfg.start_date,
      analysis_months: wholeNumber(cfg.analysis_months, "analysis_months", 1, 60),
      face_value_vnd: boundedNumber(cfg.face_value_vnd, "face_value_vnd", 100, 1000000000000),
      current_price_per_100: boundedNumber(cfg.current_price_per_100, "current_price_per_100", 1, 300),
      economic_growth_pct_annual: boundedNumber(cfg.economic_growth_pct_annual, "economic_growth_pct_annual", -20, 25),
      asset_trend_pct_annual: boundedNumber(cfg.asset_trend_pct_annual, "asset_trend_pct_annual", -30, 40),
      coupon_carry_pct_annual: boundedNumber(cfg.coupon_carry_pct_annual, "coupon_carry_pct_annual", 0, 20),
      cash_rate_pct_annual: boundedNumber(cfg.cash_rate_pct_annual, "cash_rate_pct_annual", -5, 25),
      holding_cost_bps_annual: boundedNumber(cfg.holding_cost_bps_annual, "holding_cost_bps_annual", 0, 5000),
      sell_fee_bps: boundedNumber(cfg.sell_fee_bps, "sell_fee_bps", 0, 10000),
      tax_bps: boundedNumber(cfg.tax_bps, "tax_bps", 0, 10000),
      market_impact_bps_full_sale: boundedNumber(cfg.market_impact_bps_full_sale, "market_impact_bps_full_sale", 0, 5000),
      volatility_pct_annual: boundedNumber(cfg.volatility_pct_annual, "volatility_pct_annual", 0, 80),
      risk_penalty_bps_per_vol_year: boundedNumber(cfg.risk_penalty_bps_per_vol_year, "risk_penalty_bps_per_vol_year", 0, 1000),
      shock_month: wholeNumber(cfg.shock_month, "shock_month", 0, 60),
      shock_size_pct: boundedNumber(cfg.shock_size_pct, "shock_size_pct", -30, 60),
      min_sale_pct: boundedNumber(cfg.min_sale_pct, "min_sale_pct", 0, 100),
      max_sale_pct: boundedNumber(cfg.max_sale_pct, "max_sale_pct", 0, 100),
      sale_step_pct: boundedNumber(cfg.sale_step_pct, "sale_step_pct", 1, 100),
      earliest_sale_month: wholeNumber(cfg.earliest_sale_month, "earliest_sale_month", 0, 60),
      latest_sale_month: wholeNumber(cfg.latest_sale_month, "latest_sale_month", 0, 60),
      near_optimal_bps_of_face: boundedNumber(cfg.near_optimal_bps_of_face, "near_optimal_bps_of_face", 0, 500)
    };
    parseDate(out.start_date, "start_date");
    if (out.min_sale_pct > out.max_sale_pct) throw new Error("min_sale_pct khong duoc lon hon max_sale_pct.");
    if (out.earliest_sale_month > out.latest_sale_month) throw new Error("earliest_sale_month khong duoc lon hon latest_sale_month.");
    if (out.earliest_sale_month > out.analysis_months) throw new Error("Khong co phuong an kha thi: earliest_sale_month sau H.");
    out.latest_sale_month = Math.min(out.latest_sale_month, out.analysis_months);
    return out;
  }

  function saleGrid(minPct, maxPct, stepPct) {
    const grid = [];
    for (let q = minPct; q <= maxPct + 1e-9; q += stepPct) {
      grid.push(Math.min(maxPct, Math.round(q * 1000000) / 1000000) / 100);
      if (grid.length > 200) throw new Error("Luoi ty le ban qua day cho demo.");
    }
    if (grid[grid.length - 1] < maxPct / 100 - 1e-9) grid.push(maxPct / 100);
    // q=0 is a hold-to-H reference, not a selling decision. If it remains in
    // the optimisation grid, every month ties and the tie-breaker picks month 0.
    return Array.from(new Set(grid.map((q) => Math.round(q * 1000000) / 1000000)))
      .filter((q) => q > 1e-12)
      .sort((a, b) => a - b);
  }

  function buildYieldCurve(cfg) {
    const maturities = [0.5, 1, 2, 3, 5, 7, 10, 15, 20, 30];
    const baseRate = Math.max(0.5, cfg.cash_rate_pct_annual - 0.8);
    const slope = Math.max(0.1, (cfg.asset_trend_pct_annual + 0.35 * cfg.economic_growth_pct_annual) * 0.8 + 1.2);
    return maturities.map(tau => {
      const yieldPct = baseRate + slope * (tau / (tau + 2.5));
      return { maturity_years: tau, spot_yield_pct: yieldPct };
    });
  }

  function buildTimeline(cfg) {
    const start = parseDate(cfg.start_date, "start_date");
    const terminal = addMonths(start, cfg.analysis_months);
    const growth = (cfg.asset_trend_pct_annual + 0.35 * cfg.economic_growth_pct_annual) / 100;
    const initialMaturity = 5.0; // Initial 5-year benchmark bond
    const timeline = [];

    for (let month = 0; month <= cfg.analysis_months; month += 1) {
      const date = addMonths(start, month);
      const years = yearFraction(start, date);
      const smooth = cfg.current_price_per_100 * Math.exp(growth * years);
      let shock = 0;
      if (cfg.shock_size_pct !== 0 && month >= cfg.shock_month) {
        const shockProgress = 1 - Math.exp(-(month - cfg.shock_month + 1) / 3);
        shock = cfg.current_price_per_100 * (cfg.shock_size_pct / 100) * shockProgress;
      }
      const price = Math.max(1, smooth - shock);
      const remainingMaturity = Math.max(0.1, initialMaturity - month / 12);
      const spotYield = (cfg.cash_rate_pct_annual - 0.8) + (growth * 100 * 0.8 + 1.2) * (remainingMaturity / (remainingMaturity + 2.5));

      timeline.push({
        month,
        date: iso(date),
        price_per_100: price,
        remaining_maturity_years: Number(remainingMaturity.toFixed(2)),
        spot_yield_pct: Number(spotYield.toFixed(2))
      });
    }
    return { start, terminal, timeline, yield_curve: buildYieldCurve(cfg) };
  }

  function periodicCarryToH(cfg, saleMonth, q, terminalDate) {
    const start = parseDate(cfg.start_date, "start_date");
    const couponRate = cfg.coupon_carry_pct_annual / 100;
    const holdingCostRate = cfg.holding_cost_bps_annual / 10000;
    const vol = cfg.volatility_pct_annual / 100;
    const riskScale = cfg.risk_penalty_bps_per_vol_year / 10000;
    let coupon = 0;
    let holdingCost = 0;
    let riskPenalty = 0;
    for (let month = 1; month <= cfg.analysis_months; month += 1) {
      const cashDate = addMonths(start, month);
      const held = month <= saleMonth ? 1 : 1 - q;
      const factor = cashFactor(cashDate, terminalDate, cfg.cash_rate_pct_annual / 100);
      coupon += held * 100 * couponRate / 12 * factor;
      holdingCost += held * 100 * holdingCostRate / 12 * factor;
      riskPenalty += held * 100 * riskScale * vol / 12 * factor;
    }
    return { coupon, holdingCost, riskPenalty };
  }

  function evaluateDecision(cfg, timelinePack, month, q) {
    const row = timelinePack.timeline[month];
    const terminalRow = timelinePack.timeline[timelinePack.timeline.length - 1];
    const saleDate = parseDate(row.date, "sale_date");
    const terminalDate = timelinePack.terminal;
    const reinvest = cashFactor(saleDate, terminalDate, cfg.cash_rate_pct_annual / 100);
    const feeRate = (cfg.sell_fee_bps + cfg.tax_bps) / 10000;
    const impactRate = (cfg.market_impact_bps_full_sale / 10000) * q;
    const saleNet = q * row.price_per_100 * Math.max(0, 1 - feeRate - impactRate);
    const saleAtH = saleNet * reinvest;
    const remainingAtH = (1 - q) * terminalRow.price_per_100;
    const carry = periodicCarryToH(cfg, month, q, terminalDate);
    const objective = saleAtH + remainingAtH + carry.coupon - carry.holdingCost - carry.riskPenalty;
    return {
      sale_month: month,
      sale_date: row.date,
      sale_pct: q * 100,
      gross_sale_per_100: q * row.price_per_100,
      sale_net_at_h_per_100: saleAtH,
      remaining_value_at_h_per_100: remainingAtH,
      coupon_carry_at_h_per_100: carry.coupon,
      holding_cost_at_h_per_100: carry.holdingCost,
      risk_penalty_at_h_per_100: carry.riskPenalty,
      objective_per_100: objective,
      objective_vnd: objective * cfg.face_value_vnd / 100
    };
  }

  function comparisonRows(surface, optimum, cfg, holdToH) {
    const byMonth = new Map();
    for (const row of surface) {
      const current = byMonth.get(row.sale_month);
      if (!current || row.objective_per_100 > current.objective_per_100) byMonth.set(row.sale_month, row);
    }
    const months = Array.from(byMonth.keys()).sort((a, b) => a - b);
    const early = byMonth.get(months[0]);
    const late = byMonth.get(months[months.length - 1]);
    const sellNow = surface.filter((r) => r.sale_month === 0).sort((a, b) => b.objective_per_100 - a.objective_per_100)[0] || early;
    const midpoint = byMonth.get(months[Math.floor(months.length / 2)]);
    const rows = [
      ["ban_som", "Ban som", early],
      ["toi_uu", "Diem toi uu", optimum],
      ["ban_muon", "Ban muon", late],
      ["ban_ngay", "Ban ngay", sellNow],
      ["lich_co_dinh", "Lich co dinh", midpoint]
    ];
    if (holdToH) rows.push(["giu_den_H", "Giu den H", holdToH]);
    return rows.map(([key, label, row]) => ({
      key,
      label,
      sale_date: row.sale_date,
      sale_month: row.sale_month,
      sale_pct: row.sale_pct,
      objective_per_100: row.objective_per_100,
      objective_vnd: row.objective_vnd,
      gap_to_optimum_per_100: optimum.objective_per_100 - row.objective_per_100,
      gap_to_optimum_vnd: (optimum.objective_per_100 - row.objective_per_100) * cfg.face_value_vnd / 100
    }));
  }

  function makeSensitivity(inputConfig, optimum) {
    const holding = [];
    const trend = [];
    const baseHolding = inputConfig.holding_cost_bps_annual;
    const baseTrend = inputConfig.asset_trend_pct_annual;
    for (const delta of [-80, -40, 0, 40, 80, 160]) {
      const cfg = normalizeConfig(Object.assign({}, inputConfig, { holding_cost_bps_annual: Math.max(0, baseHolding + delta) }));
      const result = evaluatePerfectForesight(cfg, { skipSensitivity: true });
      holding.push({ holding_cost_bps_annual: cfg.holding_cost_bps_annual, optimum_month: result.optimum.sale_month, optimum_sale_pct: result.optimum.sale_pct, objective_per_100: result.optimum.objective_per_100 });
    }
    for (const delta of [-3, -1.5, 0, 1.5, 3, 5]) {
      const cfg = normalizeConfig(Object.assign({}, inputConfig, { asset_trend_pct_annual: baseTrend + delta }));
      const result = evaluatePerfectForesight(cfg, { skipSensitivity: true });
      trend.push({ asset_trend_pct_annual: cfg.asset_trend_pct_annual, optimum_month: result.optimum.sale_month, optimum_sale_pct: result.optimum.sale_pct, objective_per_100: result.optimum.objective_per_100 });
    }
    return { holding_cost: holding, asset_trend: trend, reference_optimum_month: optimum.sale_month };
  }

  function evaluatePerfectForesight(input, options) {
    const cfg = normalizeConfig(input);
    const timelinePack = buildTimeline(cfg);
    const qGrid = saleGrid(cfg.min_sale_pct, cfg.max_sale_pct, cfg.sale_step_pct);
    const surface = [];
    for (let month = cfg.earliest_sale_month; month <= cfg.latest_sale_month; month += 1) {
      for (const q of qGrid) surface.push(evaluateDecision(cfg, timelinePack, month, q));
    }
    if (!surface.length) throw new Error("Khong co phuong an ban kha thi: mien q chi co 0% hoac rong.");
    surface.sort((a, b) => {
      if (Math.abs(b.objective_per_100 - a.objective_per_100) > 1e-10) return b.objective_per_100 - a.objective_per_100;
      if (a.sale_month !== b.sale_month) return a.sale_month - b.sale_month;
      return b.sale_pct - a.sale_pct;
    });
    const optimum = surface[0];
    const holdToH = evaluateDecision(cfg, timelinePack, cfg.analysis_months, 0);
    const threshold = cfg.near_optimal_bps_of_face / 10000 * 100;
    const nearOptimal = surface.filter((row) => optimum.objective_per_100 - row.objective_per_100 <= threshold + 1e-12);
    const comparisons = comparisonRows(surface, optimum, cfg, holdToH);
    const sellNow = comparisons.find((row) => row.key === "ban_ngay");
    const hold = comparisons.find((row) => row.key === "giu_den_H");
    const result = {
      analysis_version: "RYC-PERFECT-FORESIGHT-DEMO-1",
      scope: "DEMO_SYNTHETIC_PERFECT_FORESIGHT_ONLY",
      warning: "Day la ban demo thu nghiem trong dieu kien gia dinh du lieu tuong lai da duoc biet truoc. Ket qua nham minh hoa co che tim diem ban toi uu, khong phai du bao thuc te va khong dai dien cho ket luan cuoi cung cua nghien cuu.",
      config: cfg,
      terminal_date: iso(timelinePack.terminal),
      timeline: timelinePack.timeline,
      yield_curve: timelinePack.yield_curve,
      surface: surface.slice().sort((a, b) => a.sale_month - b.sale_month || a.sale_pct - b.sale_pct),
      hold_to_h: holdToH,
      optimum,
      near_optimal: nearOptimal,
      comparisons,
      lift_vs_sell_now_per_100: sellNow ? optimum.objective_per_100 - sellNow.objective_per_100 : null,
      lift_vs_hold_to_h_per_100: hold ? optimum.objective_per_100 - hold.objective_per_100 : null,
      assumptions: [
        "Du lieu gia/duong gia tri trong tuong lai la kich ban mo phong va duoc gia su da biet truoc.",
        "Ban q% tai ngay s; q=0% duoc tinh rieng nhu phuong an giu den H, khong tham gia chon t* vi khong phai hanh dong ban.",
        "Phan con lai sau khi ban duoc giu den H va quy thanh tien theo gia tri mo phong tai H.",
        "Coupon/carry, chi phi nam giu va phat rui ro duoc tinh gan dung hang thang va quy ve H bang lai suat tien mat.",
        "Khong co lich ngay nghi Viet Nam, quote/fill that, thue ca nhan, ex-coupon/record-date hay rang buoc thanh khoan that."
      ],
      formula: "W_H(s,q)=q*P_s*(1-fee-tax-impact*q)*A(s,H)+(1-q)*P_H+carry_H-cost_H-risk_H; optimise over q>0, q=0 is hold-to-H reference",
      units: {
        objective_per_100: "VND tren moi VND100 menh gia",
        objective_vnd: "VND cho face_value_vnd",
        prices: "dirty/synthetic value per VND100 face",
        rates: "phan tram nam hoac basis point nhu ten truong"
      }
    };
    if (!options || !options.skipSensitivity) result.sensitivity = makeSensitivity(cfg, optimum);
    return result;
  }

  return {
    DAYS_PER_YEAR,
    DEFAULT_CONFIG,
    PRESETS,
    normalizeConfig,
    buildTimeline,
    evaluateDecision,
    evaluatePerfectForesight
  };
});
