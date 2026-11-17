const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');

const appDir = path.resolve(__dirname, '../app');
const html = fs.readFileSync(path.join(appDir, 'ryc_perfect_foresight_workspace.html'), 'utf8');
const js = fs.readFileSync(path.join(appDir, 'ryc_perfect_foresight_workspace.js'), 'utf8');
assert.match(html, /ryc_perfect_foresight_workspace\.js/);
assert.match(html, /echarts-6\.1\.0\.min\.js/);
assert.match(html, /ryc_perfect_foresight_workspace\.css/);
assert.match(html, /href="\.\/ryc_perfect_foresight_workspace\.css"/);
assert.match(html, /href="\.\/ryc_perfect_foresight_workspace_light\.css"/);
assert.match(html, /src="\.\/ryc_perfect_foresight_workspace\.js"/);
assert.match(html, /src="\.\/echarts-6\.1\.0\.min\.js"/);
assert.match(html, /id="fileProtocolWarning" role="alert" hidden/);
assert.match(html, /window\.location\.protocol === 'file:'/);
const css = fs.readFileSync(path.join(appDir, 'ryc_perfect_foresight_workspace.css'), 'utf8');
assert.match(css, /url\('fonts\/InterVariable\.woff2'\)/);
assert.match(html, /Giữ hay bán trái phiếu trước ngày mục tiêu/);
assert.match(html, /Chỉnh dữ liệu JSON/);
assert.match(html, /Diễn biến lãi suất giả định/);
assert.match(html, /Khi lãi suất khác giả định/);
assert.match(html, /id="comparisonRows"/);
assert.match(html, /id="comparisonChart"/);
assert.match(html, /id="pricePathChart"/);
assert.match(html, /id="marginCallout"/);
assert.match(html, /id="staleNotice"/);
assert.match(html, /data-control="position_face_value_vnd"/);
assert.doesNotMatch(js, /Math\.(exp|pow)|function\s+dirty_model_price|discount_factor|parallel_zero_duration_years\s*=/);

const nodes = new Map();
function node() {
  const classes = new Set();
  return {
    textContent: '', value: '', hidden: false, disabled: false, children: [], dataset: {},
    classList: { add: x => classes.add(x), remove: x => classes.delete(x), toggle: (x, on) => on ? classes.add(x) : classes.delete(x), contains: x => classes.has(x) },
    appendChild(child) { this.children.push(child); }, replaceChildren() { this.children = []; },
    addEventListener() {}, scrollIntoView() {}
  };
}
global.document = {
  getElementById(id) { if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id); },
  createElement: node, addEventListener() {}, querySelectorAll() { return []; }
};
global.echarts = { init() { return { setOption(option) { this.option = option; }, resize() {} }; } };
global.addEventListener = () => {};
const $ = id => document.getElementById(id);
const ui = require(path.join(appDir, 'ryc_perfect_foresight_workspace.js'));
const example = JSON.parse(fs.readFileSync(path.join(appDir, 'ryc_perfect_foresight_example.json'), 'utf8'));
const flat = ui.applyPreset(example, 'flat');
assert.equal(flat.curve_path.length, example.curve_path.length);
assert.deepEqual(flat.curve_path.map(x => x.value_date), example.curve_path.map(x => x.value_date));
assert.equal(flat.curve_path[0].curve_nodes[0].zero_rate_decimal, 0.04);
assert.equal(ui.applyPreset(example, 'cash_rise').reinvestment_path[1].rate_cc_decimal, 0.095);
assert.equal(ui.applyPreset(example, 'high_cost').execution.fixed_ticket_fee_vnd, 250000);
if (process.env.RYC_PYTHON_TEST_RUNTIME) {
  for (const preset of ['flat', 'steep', 'inverted', 'shock', 'cash_rise', 'high_cost']) {
    const check = spawnSync(process.env.RYC_PYTHON_TEST_RUNTIME, ['-c',
      'import json,sys; from ryc_v3_engine import analyse_payload; result=analyse_payload(json.load(sys.stdin)); assert result["status"] == "MODEL_ONLY"'],
    { input: JSON.stringify(ui.applyPreset(example, preset)), cwd: appDir, encoding: 'utf8' });
    assert.equal(check.status, 0, `${preset} must be valid Python input: ${check.stderr}`);
  }
}

const m = x => ({ per_100_face: x, for_position_vnd: x * 1_000_000 });
const plan = (kind, amount, sales = [], mark = true) => ({
  kind, sales, expected_terminal_wealth: m(amount),
  terminal_value_basis: mark ? 'MODEL_MARK_PROXY' : 'CONTRACTUAL_CASH_OR_SALE',
  cash_ledger: { Base: [
    { kind: 'sale_gross', cash_date: '2026-02-05', is_cash: true, cash_amount: m(50), terminal_contribution: m(51) },
    { kind: 'fixed_ticket_fee', cash_date: '2026-02-05', is_cash: true, cash_amount: m(-1), terminal_contribution: m(-1) },
    { kind: 'model_mark_proxy', cash_date: '2026-03-05', is_cash: false, cash_amount: m(50), terminal_contribution: m(50) }
  ] }
});
const result = {
  model_variant: 'perfect_foresight_v1', status: 'MODEL_ONLY', analysis_version: 'RYC-V3-perfect-foresight-v1', engine_revision: 'test',
  scenario_weights: { Base: 1 }, current_price: { dirty_bid_per_100: 95, clean_bid_per_100: null, accrued_interest_per_100: null, parallel_zero_duration_years: 1.2, basis: 'synthetic_curve_model' },
  risk_diagnostic: { status: 'NOT_ESTIMATED' },
  hold_to_h: plan('hold_to_h', 100), best_single_sale: plan('single_sale', 101, [{ execution_date: '2026-01-05', sale_pct_initial_face: 50 }]),
  best_two_tranche: plan('two_tranche', 102, [{ execution_date: '2026-01-05', sale_pct_initial_face: 50 }, { execution_date: '2026-02-05', sale_pct_initial_face: 50 }], false),
  expected_maximiser: plan('two_tranche', 102, [{ execution_date: '2026-01-05', sale_pct_initial_face: 50 }, { execution_date: '2026-02-05', sale_pct_initial_face: 50 }], false),
  two_tranche_option_value_vs_single_sale: m(1),
  decision_margin: { runner_up_kind: 'two_tranche', runner_up_expected_terminal_wealth: m(101.8),
    expected_margin: m(0.2), economic_tolerance: m(0.3), margin_exceeds_economic_tolerance: false },
  alternatives_evaluated: { hold: 1, single_sale: 4, two_tranche: 3 },
  single_sale_surface: [
    { execution_date: '2026-01-05', sale_pct_initial_face: 50, expected_terminal_wealth: m(101) },
    { execution_date: '2026-02-05', sale_pct_initial_face: 50, expected_terminal_wealth: m(100) }
  ],
  near_optimal_single_sales: [{ execution_date: '2026-01-05', sale_pct_initial_face: 50 }]
};
result.curve_path_view = { Base: flat.curve_path.map((item, index) => ({ ...item, maturity_tenor_years: 1,
  maturity_zero_rate_decimal: 0.04, dirty_model_price_per_100: 96 + index * 0.3 })) };
$('planChoice').value = 'hold_to_h';
ui.renderResult(result);
assert.match($('expected_maximiserValue').textContent, /102/);
assert.match($('twoValue').textContent, /1 \/100/);
assert.match($('priceBasis').textContent, /synthetic_curve_model/);
assert.match($('riskStatus').textContent, /NOT_ESTIMATED/);
assert.match($('decisionTitle').textContent, /Bán hai đợt/);
assert.match($('decisionAdvantage').textContent, /200\.000/);
assert.match($('marginCallout').textContent, /chưa vượt ngưỡng kinh tế/i);
assert.match($('decisionExplanation').textContent, /bán 2 đợt/i);
assert.match($('remainingPosition').textContent, /0%/);
assert.match($('inputBasis').textContent, /đường cong tổng hợp/);
assert.equal($('comparisonRows').children.length, 3);
assert.deepEqual(ui.state.chartComparison.option.series[0].data.map(row => row.value), [1_000_000, 2_000_000]);
assert.match($('comparisonScope').textContent, /3 phương án bán hai đợt/);
assert.match($('decisionBasis').textContent, /dòng tiền hợp đồng/);
assert.equal($('ledgerRows').children.length, 3);
assert.match($('ledgerRows').children[2].children[2].textContent, /Định giá mô hình/);
assert.equal(ui.state.chartHeat.option.series[0].data[0].itemStyle.borderWidth, 3);
assert.equal(ui.state.chartCurve.option.series[1].data[0][0], 1);
assert.deepEqual(ui.state.chartPrice.option.series[0].data, [96, 96.3, 96.6]);
assert.match($('pricePathInsight').textContent, /được Python trả về/);
assert.match($('ledgerBasis').textContent, /is_cash=false/);
ui.stepTimeline();
assert.equal(String($('curveSlider').value), '1');
assert.deepEqual(ui.state.chartPrice.option.series[1].data, [[1, 96.3]]);
assert.equal(ui.state.chartHeat.option.series[0].data[1].itemStyle.borderColor, '#1e648d');
ui.stale();
assert.equal($('results').classList.contains('stale'), true);
assert.equal($('staleNotice').hidden, false);

$('jsonEditor').value = JSON.stringify(flat);
ui.updateControl('sale_step_pct', '25', false);
ui.updateControl('min_sale_pct', '37', false);
ui.updateControl('market_impact_exponent', '2.5', false);
ui.updateControl('max_tranches', '1', false);
const adjusted = JSON.parse($('jsonEditor').value);
assert.equal(adjusted.liquidation.sale_step_pct, 25);
assert.equal(adjusted.liquidation.min_sale_pct, 25);
assert.equal(adjusted.execution.market_impact_exponent, 2.5);
assert.equal(adjusted.liquidation.max_tranches, 1);
if (process.env.RYC_PYTHON_TEST_RUNTIME) {
  const check = spawnSync(process.env.RYC_PYTHON_TEST_RUNTIME, ['-c',
    'import json,sys; from ryc_v3_engine import analyse_payload; assert analyse_payload(json.load(sys.stdin))["status"] == "MODEL_ONLY"'],
  { input: JSON.stringify(adjusted), cwd: appDir, encoding: 'utf8' });
  assert.equal(check.status, 0, `slider payload must be valid Python input: ${check.stderr}`);
}

const regret = JSON.parse(fs.readFileSync(path.join(appDir, 'RYC_P3B_SYNTHETIC_RESULT_2026-11-16.json'), 'utf8'));
ui.renderRegret(regret);
assert.equal($('regretResults').hidden, false);
assert.equal($('regretRows').children.length, 2);
assert.match($('regretTotal').textContent, /8,2017/);
assert.equal(ui.state.chartRegret.option.series.length, 3);
assert.match($('regretBasis').textContent, /định giá mô hình, chưa phải tiền đã thu/);
assert.match($('regretProtocolPlain').textContent, /tối đa một đợt/);
ui.switchTab('regret');
assert.equal($('pfWorkspace').hidden, true);
assert.equal($('regretSidebar').hidden, false);
const invalidRegret = { ...regret, point_in_time_claim: true };
assert.throws(() => ui.renderRegret(invalidRegret), /hợp đồng P3-B/);

(async () => {
  $('jsonEditor').value = JSON.stringify(flat);
  for (const status of [400, 422]) {
    global.fetch = async () => ({ ok: false, status, json: async () => ({ error: 'curve_path is missing exact value-date snapshots' }) });
    await ui.run();
    assert.match($('errorBox').textContent, new RegExp(`HTTP ${status}`));
    assert.match($('errorBox').textContent, /curve_path/);
    assert.equal($('results').classList.contains('stale'), true);
  }
  let finishResponse;
  global.fetch = () => new Promise(resolve => { finishResponse = resolve; });
  const pending = ui.run();
  ui.stale();
  finishResponse({ ok: true, status: 200, json: async () => result });
  await pending;
  assert.equal($('runButton').textContent, 'Tính phương án');
  assert.equal($('runButton').disabled, false);
  assert.equal($('staleNotice').hidden, false);
  global.fetch = async () => ({ ok: true, status: 200, json: async () => regret });
  await ui.loadRegretExample();
  assert.equal($('regretLoading').hidden, true);
  assert.equal($('regretError').hidden, true);
  global.fetch = async () => ({ ok: true, status: 200, json: async () => invalidRegret });
  await ui.loadRegretExample();
  assert.equal($('regretResults').hidden, true);
  assert.equal($('regretError').hidden, false);
  assert.match($('regretError').textContent, /hợp đồng P3-B/);
  global.location = { protocol: 'file:' };
  global.fetch = async () => { throw new Error('file mode must not call fetch'); };
  ui.init();
  assert.match($('apiStatus').textContent, /file:\/\//);
  assert.equal($('runButton').disabled, true);
  assert.equal($('loadRegretExample').disabled, true);
  ui.stale();
  assert.equal($('runButton').disabled, true);
  await ui.run();
  assert.match($('errorBox').textContent, /http:\/\/127\.0\.0\.1:8772/);
  console.log('Perfect Foresight UI: presets, sliders, timeline, regret audit, charts, ledger flags, stale state and HTTP errors PASS');
})().catch(error => { console.error(error); process.exitCode = 1; });
