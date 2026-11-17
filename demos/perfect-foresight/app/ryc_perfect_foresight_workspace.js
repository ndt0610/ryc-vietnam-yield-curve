/* Perfect Foresight presentation only. All pricing, ledger and choices come from Python. */
(function (root) {
  'use strict';
  const el = id => document.getElementById(id);
  const fmt = new Intl.NumberFormat('vi-VN', { maximumFractionDigits: 4 });
  const fmtVnd = new Intl.NumberFormat('vi-VN', { maximumFractionDigits: 0 });
  const text = value => value == null ? 'Chưa có' : String(value);
  const number = value => typeof value === 'number' && Number.isFinite(value) ? fmt.format(value) : 'Chưa có';
  const vnd = value => typeof value === 'number' && Number.isFinite(value) ? fmtVnd.format(value) + ' VND' : 'Chưa có';
  const money = value => value ? `${number(value.per_100_face)} /100 · ${vnd(value.for_position_vnd)}` : 'Không áp dụng';
  const labels = {
    hold_to_h: 'Giữ đến ngày mục tiêu', single_sale: 'Bán 1 đợt', two_tranche: 'Bán 2 đợt',
    sale_gross: 'Tiền bán gộp', sell_fee: 'Phí bán', liquidity_haircut: 'Chiết khấu thanh khoản',
    market_impact: 'Tác động thị trường', fixed_ticket_fee: 'Phí cố định mỗi lệnh',
    coupon: 'Coupon', redemption: 'Đáo hạn (theo CF)', model_mark_proxy: 'Giá trị mô hình phần chưa bán'
  };
  const stepChoices = [5, 10, 20, 25, 50, 100];
  const chartPalette = { axis: '#516579', grid: '#e1e8ed', blue: '#1e648d', amber: '#9b5b14', teal: '#167568', red: '#b53e48' };
  const presetNames = { flat: 'Đường cong phẳng', steep: 'Đường cong dốc lên', inverted: 'Đường cong đảo ngược',
    shock: 'Sốc lãi suất', cash_rise: 'Lãi tiền mặt tăng', high_cost: 'Phí bán cao' };
  const state = { result: null, dirty: true, version: 0, chartComparison: null, chartCurve: null, chartPrice: null, chartHeat: null, chartLedger: null,
    chartRegret: null, playTimer: null, regretResult: null, regretLoadVersion: 0, selectedEpisode: 0, preset: 'flat', activeRun: null };
  const isFileMode = () => root.location?.protocol === 'file:';
  const dateLabel = iso => /^\d{4}-\d{2}-\d{2}$/.test(iso || '') ? iso.slice(8, 10) + '/' + iso.slice(5, 7) + '/' + iso.slice(0, 4) : text(iso);
  function updateHeaderFacts(payload) {
    el('currentBond').textContent = `Mã trái phiếu ${payload.bond?.identifier || 'chưa khai báo'}`;
    el('currentDecision').textContent = `Ngày quyết định ${dateLabel(payload.decision_date)}`;
    el('currentScenario').textContent = presetNames[state.preset] || 'Giả định đã chỉnh';
    el('currentTerminal').textContent = `Ngày mục tiêu ${dateLabel(payload.terminal_date)}`;
    el('currentPosition').textContent = `Mệnh giá ${vnd(payload.position_face_value_vnd)}`;
    el('presetDescription').textContent = state.preset === 'custom'
      ? 'Bạn đã thay đổi đầu vào. Bấm Tính phương án để cập nhật kết luận.'
      : 'Giả định tổng hợp. Bấm Tính phương án để xem kết quả của tình huống đã chọn.';
    const nodes = payload.curve_path?.[0]?.curve_nodes || [];
    const edgeNodes = nodes.length > 1 ? [nodes[0], nodes[nodes.length - 1]] : nodes;
    const zeroRates = edgeNodes.length
      ? edgeNodes.map(node => `${number(node.tenor_years)} năm: ${number(node.zero_rate_decimal * 100)}%`).join(' · ')
      : 'chưa có snapshot';
    const firstCashRate = payload.reinvestment_path?.[0]?.rate_cc_decimal;
    const cashRate = typeof firstCashRate === 'number' ? `${number(firstCashRate * 100)}%/năm (ghép liên tục)` : 'chưa khai báo';
    el('assumptionSummary').textContent = `Zero rate ngày đầu ${zeroRates}. Lãi tiền mặt kỳ đầu ${cashRate}. Phí bán ${number(payload.execution?.sell_fee_bps)} bps; phí mỗi lệnh ${vnd(payload.execution?.fixed_ticket_fee_vnd)}.`;
  }

  function controlValue(payload, name) {
    if (name === 'position_face_value_vnd') return payload.position_face_value_vnd;
    if (['sale_step_pct', 'min_sale_pct', 'max_tranches'].includes(name)) return payload.liquidation?.[name];
    return payload.execution?.[name];
  }
  function syncControls(payload) {
    for (const input of document.querySelectorAll('[data-control]')) {
      const name = input.dataset.control;
      const value = controlValue(payload, name);
      if (value == null) continue;
      if (input.dataset.rangeIndex === 'true') input.value = String(Math.max(0, stepChoices.indexOf(value)));
      else input.value = String(value);
      if (input.type === 'range') {
        if (name === 'min_sale_pct') { input.min = String(payload.liquidation.sale_step_pct); input.step = String(payload.liquidation.sale_step_pct); }
        const min = Number(input.min), max = Number(input.max);
        input.style?.setProperty?.('--range-fill', `${100 * (Number(input.value) - min) / (max - min || 1)}%`);
      }
      if (name === 'min_sale_pct' && input.type === 'number') {
        input.min = String(payload.liquidation.sale_step_pct);
        input.step = String(payload.liquidation.sale_step_pct);
      }
    }
  }
  function updateControl(name, raw, rangeIndex) {
    let payload;
    try { payload = parsePayload(); } catch (error) { setError(`Sửa JSON trước khi dùng slider: ${error.message}`); return; }
    if (!payload.execution || typeof payload.execution !== 'object' || !payload.liquidation || typeof payload.liquidation !== 'object'
      || !Number.isFinite(payload.liquidation.sale_step_pct)) {
      setError('Payload thiếu execution hoặc liquidation hợp lệ; sửa trong Advanced JSON Inspector trước khi dùng slider.');
      return;
    }
    let value = Number(raw);
    if (!Number.isFinite(value)) { setError('Tham số phải là số hữu hạn.'); return; }
    const limits = {
      position_face_value_vnd: [1_000_000, 1_000_000_000], sell_fee_bps: [0, 300],
      fixed_ticket_fee_vnd: [0, 1_000_000], market_impact_bps_full_sale: [0, 500],
      market_impact_exponent: [1, 4], min_sale_pct: [5, 100], max_tranches: [1, 2]
    };
    if (name === 'sale_step_pct') {
      if (rangeIndex) value = stepChoices[Math.max(0, Math.min(stepChoices.length - 1, Math.round(value)))];
      else value = stepChoices.reduce((best, option) => Math.abs(option - value) < Math.abs(best - value) ? option : best, stepChoices[0]);
    } else {
      const [min, max] = limits[name] || [0, 0];
      value = Math.min(max, Math.max(min, value));
      if (name === 'min_sale_pct') {
        const step = payload.liquidation.sale_step_pct;
        value = Math.min(100, Math.max(step, Math.round(value / step) * step));
      } else if (name === 'max_tranches' || name === 'position_face_value_vnd' || name === 'fixed_ticket_fee_vnd') value = Math.round(value);
      else if (name !== 'market_impact_exponent') value = Math.round(value);
      else value = Math.round(value * 10) / 10;
    }
    if (name === 'position_face_value_vnd') payload[name] = value;
    else if (['sale_step_pct', 'min_sale_pct', 'max_tranches'].includes(name)) {
      payload.liquidation[name] = value;
      if (name === 'sale_step_pct') payload.liquidation.min_sale_pct = Math.min(100, Math.max(value, Math.ceil(payload.liquidation.min_sale_pct / value) * value));
    } else payload.execution[name] = value;
    state.preset = 'custom';
    setPayload(payload);
    document.querySelectorAll('[data-preset]').forEach(button => button.classList.remove('active'));
  }

  function setError(message) {
    const box = el('errorBox');
    box.textContent = message || '';
    box.hidden = !message;
  }
  function stale() {
    stopPlayback();
    state.version += 1;
    state.dirty = true;
    el('results').classList.toggle('stale', !!state.result);
    el('staleNotice').hidden = !state.result;
    el('runButton').disabled = isFileMode();
  }
  function parsePayload() {
    const payload = JSON.parse(el('jsonEditor').value);
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) throw new Error('Payload phải là JSON object.');
    if (payload.model_variant !== 'perfect_foresight_v1') throw new Error('model_variant phải là perfect_foresight_v1.');
    return payload;
  }
  function applyPreset(base, kind) {
    const payload = JSON.parse(JSON.stringify(base));
    delete payload.scenarios;
    delete payload.quote;
    payload.execution ??= {};
    payload.reinvestment_path ??= [];
    const curve = payload.curve_path;
    if (!Array.isArray(curve) || !curve.length) throw new Error('Preset cần example có curve_path.');
    for (let i = 0; i < curve.length; i++) {
      for (const node of curve[i].curve_nodes) {
        let rate = 0.04;
        if (kind === 'steep') rate = node.tenor_years < 1 ? 0.025 : 0.055;
        if (kind === 'inverted') rate = node.tenor_years < 1 ? 0.065 : 0.035;
        if (kind === 'shock' && i > 0) rate = 0.04 + 0.015;
        node.zero_rate_decimal = rate;
      }
    }
    for (let i = 0; i < payload.reinvestment_path.length; i++) {
      payload.reinvestment_path[i].rate_cc_decimal = kind === 'cash_rise' ? (i ? 0.095 : 0.02) : 0.04;
    }
    payload.execution.sell_fee_bps = kind === 'high_cost' ? 60 : 0;
    payload.execution.fixed_ticket_fee_vnd = kind === 'high_cost' ? 250000 : 0;
    payload.execution.market_impact_bps_full_sale = kind === 'high_cost' ? 100 : 0;
    payload.execution.market_impact_exponent = 2;
    return payload;
  }
  function setPayload(payload) {
    el('jsonEditor').value = JSON.stringify(payload, null, 2);
    syncControls(payload);
    updateHeaderFacts(payload);
    stale();
    setError('');
  }
  function chart(id, key) {
    if (!root.echarts) {
      if (id === 'regretChart') el('regretMeta').textContent = 'Không tải được ECharts cục bộ; bảng đối chiếu vẫn hiển thị.';
      else el('chartWarning').hidden = false;
      return null;
    }
    if (!state[key]) state[key] = root.echarts.init(el(id));
    return state[key];
  }
  function renderComparisonChart(result) {
    const hold = result.hold_to_h?.expected_terminal_wealth?.for_position_vnd;
    const choices = [
      ['Bán 1 đợt', result.best_single_sale],
      ['Bán 2 đợt', result.best_two_tranche]
    ].filter(([, plan]) => typeof plan?.expected_terminal_wealth?.for_position_vnd === 'number');
    if (typeof hold !== 'number' || !choices.length) {
      el('comparisonChart').hidden = true;
      el('comparisonInsight').textContent = 'Không có đủ phương án bán để vẽ chênh lệch.';
      return;
    }
    el('comparisonChart').hidden = false;
    const deltas = choices.map(([, plan]) => plan.expected_terminal_wealth.for_position_vnd - hold);
    el('comparisonInsight').textContent = choices.map(([name], index) =>
      `${name} ${deltas[index] === 0 ? 'bằng giữ' : `${deltas[index] > 0 ? 'hơn' : 'kém'} giữ ${vnd(Math.abs(deltas[index]))}`}`
    ).join(' · ') + '.';
    const instance = chart('comparisonChart', 'chartComparison');
    if (!instance) return;
    const narrow = el('comparisonChart').clientWidth < 480;
    const largest = Math.max(...deltas.map(value => Math.abs(value)), 1);
    const tick = 10 ** Math.floor(Math.log10(largest)) / 2;
    const bound = Math.ceil(largest / tick) * tick;
    const axisUnit = bound >= 1_000_000 ? 1_000_000 : 1_000;
    const axisSuffix = bound >= 1_000_000 ? 'm' : 'k';
    instance.setOption({
      animationDuration: 250,
      grid: { left: narrow ? 86 : 112, right: narrow ? 38 : 75, top: 12, bottom: 30 },
      tooltip: { trigger: 'item', formatter: point => `${point.name}: ${point.value >= 0 ? '+' : '−'}${vnd(Math.abs(point.value))} so với giữ` },
      xAxis: { type: 'value', min: -bound, max: bound, axisLabel: { color: chartPalette.axis, formatter: value => `${number(value / axisUnit)}${axisSuffix}` },
        axisLine: { show: true, lineStyle: { color: chartPalette.axis } }, splitLine: { lineStyle: { color: chartPalette.grid } } },
      yAxis: { type: 'category', inverse: true, data: choices.map(([name]) => name), axisLabel: { color: chartPalette.axis, fontWeight: 700 } },
      series: [{ type: 'bar', barMaxWidth: 34, markLine: { silent: true, symbol: 'none', label: { show: false }, data: [{ xAxis: 0 }], lineStyle: { color: '#5a7180', width: 1.5 } }, data: deltas.map((value, index) => ({ value,
        itemStyle: { color: choices[index][1]?.kind === result.expected_maximiser?.kind ? chartPalette.teal : value < 0 ? chartPalette.amber : chartPalette.blue } })) }]
    }, true);
    instance.resize();
  }
  function renderCurve(result) {
    const selected = el('curveScenario').value;
    const path = result.curve_path_view?.[selected] || [];
    const slider = el('curveSlider');
    slider.max = Math.max(path.length - 1, 0);
    slider.value = Math.max(0, Math.min(Number(slider.value) || 0, path.length - 1));
    slider.style?.setProperty?.('--range-fill', `${100 * Number(slider.value) / (Number(slider.max) || 1)}%`);
    const snapshot = path[Number(slider.value)];
    el('curveDate').textContent = snapshot ? snapshot.value_date : 'Không có snapshot';
    el('playbackStatus').textContent = snapshot
      ? `Snapshot ${Number(slider.value) + 1}/${path.length} · ${snapshot.value_date} · đường cong, giá và bề mặt bán cùng mốc ngày.`
      : 'Chưa có snapshot để phát timeline.';
    if (!snapshot) return;
    const instance = chart('curveChart', 'chartCurve');
    if (!instance) return;
    const rates = snapshot.curve_nodes.map(node => node.zero_rate_decimal * 100);
    const lowRate = Math.min(...rates), highRate = Math.max(...rates);
    const ratePadding = Math.max((highRate - lowRate) * 0.2, 0.25);
    instance.setOption({
      animationDuration: 250,
      grid: { left: 54, right: 24, top: 24, bottom: 45 },
      tooltip: { trigger: 'axis', valueFormatter: value => `${number(value)}%` },
      xAxis: { type: 'value', name: 'Kỳ hạn (năm)', axisLabel: { color: chartPalette.axis }, nameTextStyle: { color: chartPalette.axis }, splitLine: { lineStyle: { color: chartPalette.grid } } },
      yAxis: { type: 'value', min: lowRate - ratePadding, max: highRate + ratePadding,
        name: 'Zero rate (%)', axisLabel: { color: chartPalette.axis }, nameTextStyle: { color: chartPalette.axis }, splitLine: { lineStyle: { color: chartPalette.grid } } },
      series: [{ type: 'line', showSymbol: true, symbolSize: 9, smooth: false,
        lineStyle: { width: 3, color: chartPalette.blue }, itemStyle: { color: chartPalette.blue },
        areaStyle: { color: 'rgba(30,100,141,.08)' },
        data: snapshot.curve_nodes.map(node => [node.tenor_years, node.zero_rate_decimal * 100]) },
        { type: 'scatter', name: 'Kỳ hạn còn lại', symbolSize: 15, itemStyle: { color: chartPalette.amber },
          data: snapshot.maturity_zero_rate_decimal == null ? [] : [[snapshot.maturity_tenor_years, snapshot.maturity_zero_rate_decimal * 100]] }]
    }, true);
    renderPricePath(path, Number(slider.value));
  }
  function renderPricePath(path, selectedIndex) {
    const prices = path.map(row => typeof row.dirty_model_price_per_100 === 'number' ? row.dirty_model_price_per_100 : null);
    const priced = prices.map((price, index) => ({ price, index })).filter(row => row.price !== null);
    el('pricePathInsight').textContent = priced.length
      ? `${priced.length} ngày định giá được Python trả về. Từ ${dateLabel(path[priced[0].index].value_date)}: ${number(priced[0].price)} đến ${dateLabel(path[priced[priced.length - 1].index].value_date)}: ${number(priced[priced.length - 1].price)} trên 100 mệnh giá. Giá này chưa cộng tiền coupon đã nhận hoặc lãi tái đầu tư.`
      : 'Không có ngày định giá mô hình trong đường cong đã chọn.';
    const instance = chart('pricePathChart', 'chartPrice');
    if (!instance) return;
    const narrow = el('pricePathChart').clientWidth < 480;
    instance.setOption({
      animationDuration: 250,
      grid: { left: narrow ? 48 : 68, right: 25, top: 25, bottom: narrow ? 65 : 52 },
      tooltip: { trigger: 'axis', valueFormatter: value => `${number(value)} /100 mệnh giá` },
      xAxis: { type: 'category', data: path.map(row => dateLabel(row.value_date)),
        axisLabel: { color: chartPalette.axis, interval: 0, rotate: narrow ? 30 : 0 }, axisLine: { lineStyle: { color: chartPalette.axis } } },
      yAxis: { type: 'value', scale: true, name: narrow ? '' : 'Dirty /100', nameTextStyle: { color: chartPalette.axis },
        axisLabel: { color: chartPalette.axis, formatter: value => number(value) }, splitLine: { lineStyle: { color: chartPalette.grid } } },
      series: [
        { name: 'Giá dirty mô hình', type: 'line', connectNulls: false, smooth: false, symbolSize: 8,
          lineStyle: { color: chartPalette.blue, width: 3 }, itemStyle: { color: chartPalette.blue }, data: prices },
        { name: 'Ngày đang chọn', type: 'scatter', symbolSize: 15, itemStyle: { color: chartPalette.amber },
          data: prices[selectedIndex] == null ? [] : [[selectedIndex, prices[selectedIndex]]] }
      ]
    }, true);
    instance.resize();
  }
  function renderHeatmap(result) {
    const rows = result.single_sale_surface || [];
    const body = el('singleSaleRows'); body.replaceChildren();
    const holdValue = result.hold_to_h?.expected_terminal_wealth?.per_100_face;
    for (const row of rows) {
      const tr = document.createElement('tr');
      const difference = typeof holdValue === 'number' ? row.expected_terminal_wealth.per_100_face - holdValue : null;
      const values = [dateLabel(row.execution_date), `${number(row.sale_pct_initial_face)}%`,
        number(row.expected_terminal_wealth.per_100_face), difference == null ? '—' : `${difference >= 0 ? '+' : ''}${number(difference)}`];
      for (const value of values) { const td = document.createElement('td'); td.textContent = value; tr.appendChild(td); }
      body.appendChild(tr);
    }
    const dates = [...new Set(rows.map(row => row.execution_date))];
    const lots = [...new Set(rows.map(row => row.sale_pct_initial_face))].sort((a, b) => a - b);
    const near = new Set((result.near_optimal_single_sales || []).map(row => `${row.execution_date}|${row.sale_pct_initial_face}`));
    const scenarioPath = result.curve_path_view?.[el('curveScenario').value] || [];
    const activeDate = scenarioPath[Number(el('curveSlider').value)]?.value_date;
    const data = rows.map(row => ({
      value: [dates.indexOf(row.execution_date), lots.indexOf(row.sale_pct_initial_face), row.expected_terminal_wealth.per_100_face],
      itemStyle: row.execution_date === activeDate
        ? { borderColor: chartPalette.blue, borderWidth: 3 }
        : near.has(`${row.execution_date}|${row.sale_pct_initial_face}`) ? { borderColor: chartPalette.amber, borderWidth: 3 } : undefined
    }));
    el('heatSummary').textContent = `${rows.length} phương án · ${near.size} gần tối ưu · timeline ${activeDate || '—'}`;
    const instance = chart('heatChart', 'chartHeat');
    if (!instance || !data.length) return;
    const values = data.map(row => row.value[2]);
    instance.setOption({
      grid: { left: 58, right: 85, top: 20, bottom: 62 },
      tooltip: { formatter: p => `${dates[p.value[0]]}<br>${lots[p.value[1]]}% bán<br>${number(p.value[2])} /100` },
      xAxis: { type: 'category', data: dates, axisLabel: { color: chartPalette.axis }, splitArea: { show: true } },
      yAxis: { type: 'category', data: lots.map(v => `${v}%`), axisLabel: { color: chartPalette.axis }, splitArea: { show: true } },
      visualMap: { min: Math.min(...values), max: Math.max(...values), calculable: true, orient: 'vertical', right: 0, top: 'middle', textStyle: { color: chartPalette.axis }, inRange: { color: ['#e9f1f5', '#9cc9d0', '#2e8277'] } },
      series: [{ type: 'heatmap', data, label: { show: true, formatter: p => number(p.value[2]), color: '#173448', fontSize: 11 } }]
    }, true);
  }
  function stopPlayback() {
    if (state.playTimer) { root.clearInterval(state.playTimer); state.playTimer = null; }
    const button = el('playButton');
    button.textContent = 'Phát';
    button.setAttribute?.('aria-pressed', 'false');
  }
  function stepTimeline() {
    if (!state.result || state.dirty) return;
    const path = state.result.curve_path_view?.[el('curveScenario').value] || [];
    if (!path.length) return;
    const slider = el('curveSlider');
    slider.value = String((Number(slider.value) + 1) % path.length);
    renderCurve(state.result);
    renderHeatmap(state.result);
  }
  function togglePlayback() {
    if (state.playTimer) { stopPlayback(); return; }
    if (!state.result || state.dirty) return;
    el('playButton').textContent = 'Tạm dừng';
    el('playButton').setAttribute?.('aria-pressed', 'true');
    state.playTimer = root.setInterval(stepTimeline, 1300);
  }
  function planForChoice(result) {
    const key = el('planChoice').value;
    return result[key] || result.expected_maximiser;
  }
  function renderLedger(result) {
    const plan = planForChoice(result);
    const scenarioName = el('ledgerScenario').value;
    const rows = plan?.cash_ledger?.[scenarioName] || [];
    const body = el('ledgerRows');
    body.replaceChildren();
    for (const row of rows) {
      const tr = document.createElement('tr');
      const cells = [labels[row.kind] || row.kind, dateLabel(row.cash_date), row.is_cash ? 'Tiền đã nhận hoặc chi' : 'Định giá mô hình · chưa thu tiền',
        vnd(row.cash_amount?.for_position_vnd), vnd(row.terminal_contribution?.for_position_vnd)];
      for (const value of cells) { const td = document.createElement('td'); td.textContent = text(value); tr.appendChild(td); }
      if (!row.is_cash) tr.classList.add('noncash');
      body.appendChild(tr);
    }
    el('ledgerBasis').textContent = plan?.terminal_value_basis === 'MODEL_MARK_PROXY'
      ? 'Phần chưa bán là giá trị mô hình tại ngày mục tiêu, chưa phải tiền đã nhận (MODEL_MARK_PROXY, is_cash=false).'
      : 'Giá trị tại ngày mục tiêu đến từ dòng tiền hợp đồng hoặc lệnh bán (CONTRACTUAL_CASH_OR_SALE).';
    el('ledgerChart').hidden = rows.length <= 1;
    if (rows.length <= 1) return;
    const instance = chart('ledgerChart', 'chartLedger');
    if (!instance) return;
    instance.setOption({
      grid: { left: 145, right: 26, top: 12, bottom: 35 },
      tooltip: { trigger: 'axis', valueFormatter: value => vnd(value) },
      xAxis: { type: 'value', axisLabel: { color: chartPalette.axis, formatter: v => `${number(v / 1_000_000)}m` }, splitLine: { lineStyle: { color: chartPalette.grid } } },
      yAxis: { type: 'category', inverse: true, data: rows.map(row => labels[row.kind] || row.kind), axisLabel: { color: chartPalette.axis, width: 130, overflow: 'truncate' } },
      series: [{ type: 'bar', data: rows.map(row => ({ value: row.terminal_contribution?.for_position_vnd || 0,
        itemStyle: { color: row.is_cash ? (row.terminal_contribution?.for_position_vnd < 0 ? chartPalette.red : chartPalette.teal) : chartPalette.amber } })) }]
    }, true);
    instance.resize();
  }
  function renderResult(result) {
    if (result.model_variant !== 'perfect_foresight_v1' || result.status !== 'MODEL_ONLY') throw new Error('API trả hợp đồng khác Perfect Foresight V1 / MODEL_ONLY.');
    state.result = result;
    state.dirty = false;
    el('pfEmpty').hidden = true;
    el('results').hidden = false;
    el('results').classList.remove('stale');
    el('staleNotice').hidden = true;
    el('scope').textContent = `${result.analysis_version} · ${result.engine_revision}`;
    const options = [
      ['hold_to_h', 'Giữ đến ngày mục tiêu', result.hold_to_h],
      ['best_single_sale', 'Bán một đợt', result.best_single_sale],
      ['best_two_tranche', 'Bán hai đợt', result.best_two_tranche]
    ];
    const winner = result.expected_maximiser;
    const winnerName = options.find(([, , plan]) => plan?.kind === winner?.kind)?.[1] || labels[winner?.kind] || 'Phương án tốt nhất';
    el('decisionTitle').textContent = winnerName;
    el('decisionWealth').textContent = vnd(winner?.expected_terminal_wealth?.for_position_vnd);
    const margin = result.decision_margin || {};
    const gap = margin.expected_margin?.for_position_vnd;
    const tolerance = margin.economic_tolerance?.for_position_vnd;
    const runnerName = labels[margin.runner_up_kind] || margin.runner_up_kind || 'phương án kế tiếp';
    el('decisionAdvantage').textContent = typeof gap === 'number' ? vnd(gap) : 'Không có phương án đối chiếu';
    const callout = el('marginCallout');
    callout.classList.remove('is-caution', 'is-clear', 'is-neutral');
    if (margin.margin_exceeds_economic_tolerance === false && typeof tolerance === 'number') {
      callout.classList.add('is-caution');
      callout.textContent = `Chênh lệch chưa vượt ngưỡng kinh tế (${vnd(tolerance)}). Đây chỉ là phương án đứng đầu về số học trong giả định này.`;
    } else if (margin.margin_exceeds_economic_tolerance === true && typeof tolerance === 'number') {
      callout.classList.add('is-clear');
      callout.textContent = `Chênh lệch vượt ngưỡng kinh tế của mô hình (${vnd(tolerance)}). Đây vẫn là kết quả từ dữ liệu giả định.`;
    } else {
      callout.classList.add('is-neutral');
      callout.textContent = 'Chưa có đủ phương án để đánh giá độ lớn chênh lệch.';
    }
    const soldPct = (winner?.sales || []).reduce((total, sale) => total + sale.sale_pct_initial_face, 0);
    el('remainingPosition').textContent = `${number(Math.max(0, 100 - soldPct))}% mệnh giá ban đầu`;
    el('decisionExplanation').textContent = `${typeof gap === 'number' ? `Phương án kế tiếp trong toàn bộ lưới: ${runnerName.toLowerCase()}. ` : ''}${winner?.sales?.length ? 'Lịch bán: ' + winner.sales.map(sale => `${dateLabel(sale.execution_date)}, bán ${number(sale.sale_pct_initial_face)}%`).join('; ') + '.' : 'Không bán trước ngày mục tiêu.'}`;
    el('decisionBasis').textContent = winner?.terminal_value_basis === 'MODEL_MARK_PROXY'
      ? 'Bao gồm giá trị ước tính của trái phiếu còn nắm giữ tại ngày mục tiêu; khoản đó chưa phải tiền đã thu.'
      : 'Giá trị tại ngày mục tiêu được tính từ dòng tiền hợp đồng và các lệnh bán trong mô hình.';
    el('inputBasis').textContent = result.current_price?.basis === 'observed_input'
      ? 'Giá đầu vào do người dùng nhập; mô hình chưa xác minh độc lập báo giá này.'
      : 'Giá đầu vào được tạo từ đường cong tổng hợp; chưa có báo giá thị trường được xác minh.';
    const comparisonBody = el('comparisonRows'); comparisonBody.replaceChildren();
    const holdVnd = result.hold_to_h?.expected_terminal_wealth?.for_position_vnd;
    for (const [, name, plan] of options) {
      const tr = document.createElement('tr');
      if (plan?.kind === winner?.kind) tr.classList.add('selected-plan');
      const value = plan?.expected_terminal_wealth?.for_position_vnd;
      const difference = typeof value === 'number' && typeof holdVnd === 'number' ? value - holdVnd : null;
      const values = [name, plan ? (plan.sales.length ? plan.sales.map(sale => `${dateLabel(sale.execution_date)} · ${number(sale.sale_pct_initial_face)}%`).join(' / ') : 'Không bán') : 'Không có',
        typeof value === 'number' ? vnd(value) : 'Không áp dụng', difference == null ? '—' : difference === 0 ? 'Bằng giữ' : `${difference > 0 ? '+' : '−'}${vnd(Math.abs(difference))}`];
      for (const item of values) { const td = document.createElement('td'); td.textContent = item; tr.appendChild(td); }
      comparisonBody.appendChild(tr);
    }
    renderComparisonChart(result);
    const evaluated = result.alternatives_evaluated || {};
    el('comparisonScope').textContent = `Python đã xét ${number(evaluated.hold)} phương án giữ, ${number(evaluated.single_sale)} phương án bán một đợt và ${number(evaluated.two_tranche)} phương án bán hai đợt. Bảng hiển thị phương án tốt nhất của mỗi nhóm; chênh lệch kết luận được lấy từ toàn bộ lưới.`;
    const basis = result.current_price?.basis;
    el('priceBasis').textContent = basis === 'observed_input'
      ? 'observed_input · giá nhập do người dùng cung cấp, chưa được xác minh độc lập'
      : 'synthetic_curve_model · giá do mô hình tạo';
    el('riskStatus').textContent = `Rủi ro: ${result.risk_diagnostic?.status || 'UNKNOWN'} · R, λ và J không tham gia lựa chọn`;
    const cards = [
      ['hold_to_h', result.hold_to_h], ['best_single_sale', result.best_single_sale],
      ['best_two_tranche', result.best_two_tranche], ['expected_maximiser', result.expected_maximiser]
    ];
    for (const [id, plan] of cards) {
      el(id + 'Kind').textContent = plan ? labels[plan.kind] || plan.kind : 'Không áp dụng';
      el(id + 'Value').textContent = plan ? money(plan.expected_terminal_wealth) : 'Không áp dụng';
      el(id + 'Sales').textContent = plan ? (plan.sales.length ? plan.sales.map(sale => `${sale.execution_date} · ${number(sale.sale_pct_initial_face)}%`).join(' / ') : 'Không bán trước H') : 'Không có phương án';
    }
    el('twoValue').textContent = money(result.two_tranche_option_value_vs_single_sale);
    el('decisionMargin').textContent = `${money(margin?.expected_margin)} · ngưỡng ${money(margin?.economic_tolerance)} · ${margin?.margin_exceeds_economic_tolerance ? 'Vượt ngưỡng' : 'Chưa vượt ngưỡng'}`;
    const price = result.current_price || {};
    el('currentPrice').textContent = `Dirty Bid ${number(price.dirty_bid_per_100)} · Clean Bid ${number(price.clean_bid_per_100)} · AI ${number(price.accrued_interest_per_100)} · Parallel zero duration ${number(price.parallel_zero_duration_years)} năm`;
    el('markNotice').hidden = ![result.hold_to_h, result.best_single_sale, result.best_two_tranche, result.expected_maximiser].some(p => p?.terminal_value_basis === 'MODEL_MARK_PROXY');
    const names = Object.keys(result.scenario_weights || {});
    for (const id of ['ledgerScenario', 'curveScenario']) {
      const select = el(id); const previous = select.value;
      select.replaceChildren();
      for (const name of names) { const option = document.createElement('option'); option.value = name; option.textContent = name; select.appendChild(option); }
      select.value = names.includes(previous) ? previous : names[0] || '';
    }
    renderCurve(result); renderHeatmap(result); renderLedger(result);
  }
  function validateRegretResult(result) {
    const finite = value => typeof value === 'number' && Number.isFinite(value);
    if (!result || result.schema_version !== 'RYC-P3B-SIM-V1' || result.evidence_status !== 'CURVE_LEVEL_SIMULATION'
      || result.regret_label !== 'CURVE_MODEL_REGRET' || result.point_in_time_claim !== false
      || !['MODEL_ONLY', 'EX_POST_EXPLORATORY'].includes(result.research_tier)
      || !['PRIMARY_ONE_SALE', 'SECONDARY_TWO_SALES'].includes(result.protocol)
      || !Array.isArray(result.episodes) || !result.episodes.length || result.episode_count !== result.episodes.length
      || !finite(result.total_curve_model_regret_per_100_face) || !finite(result.mean_curve_model_regret_per_100_face)) {
      throw new Error('JSON không đúng hợp đồng P3-B CURVE_MODEL_REGRET / CURVE_LEVEL_SIMULATION.');
    }
    for (const row of result.episodes) {
      if (!row || typeof row.episode_id !== 'string' || !row.episode_id || !row.selected_plan || !row.strategy_fixed_plan
        || !row.hindsight_oracle || !['hold_to_h', 'single_sale', 'two_tranche'].includes(row.selected_plan.kind)
        || !['hold_to_h', 'single_sale', 'two_tranche'].includes(row.strategy_fixed_plan.kind)
        || !['hold_to_h', 'single_sale', 'two_tranche'].includes(row.hindsight_oracle.kind)
        || !Array.isArray(row.selected_plan.sales) || !Array.isArray(row.strategy_fixed_plan.sales)
        || !Array.isArray(row.hindsight_oracle.sales)
        || !finite(row.selected_plan.expected_terminal_wealth?.per_100_face)
        || !finite(row.strategy_wealth_per_100_face) || !finite(row.oracle_wealth_per_100_face)
        || !finite(row.curve_model_regret_per_100_face) || row.curve_model_regret_per_100_face < -1e-7
        || row.regret_theorem_scope !== 'SINGLE_SCENARIO_SAME_FEASIBLE_SET') {
        throw new Error(`Episode ${row?.episode_id || 'không tên'} thiếu số liệu hoặc nhãn hợp lệ.`);
      }
    }
    return result;
  }
  function saleLabel(plan) {
    return plan?.sales?.length ? plan.sales.map(sale => `${dateLabel(sale.execution_date)} · ${number(sale.sale_pct_initial_face)}%`).join(' / ') : 'Không bán trước ngày mục tiêu';
  }
  function detailArticle(className, title, wealth, plan) {
    const article = document.createElement('article'); article.classList.add(className);
    const heading = document.createElement('h3'); heading.textContent = title; article.appendChild(heading);
    const strong = document.createElement('strong'); strong.textContent = `${number(wealth)} /100`; article.appendChild(strong);
    const description = document.createElement('p'); description.textContent = `${labels[plan.kind] || plan.kind} · ${saleLabel(plan)}`; article.appendChild(description);
    return article;
  }
  function renderRegretDetail(index) {
    const result = state.regretResult;
    if (!result) return;
    const row = result.episodes[index];
    if (!row) return;
    state.selectedEpisode = index;
    const box = el('regretDetail'); box.replaceChildren();
    box.appendChild(detailArticle('forecast', 'Dự kiến ban đầu', row.selected_plan.expected_terminal_wealth.per_100_face, row.selected_plan));
    box.appendChild(detailArticle('strategy', 'Kế hoạch đã chọn trên diễn biến đánh giá', row.strategy_wealth_per_100_face, row.strategy_fixed_plan));
    box.appendChild(detailArticle('oracle', 'Tốt nhất khi nhìn lại', row.oracle_wealth_per_100_face, row.hindsight_oracle));
    const hasModelMark = [row.terminal_value_basis?.strategy, row.terminal_value_basis?.oracle].includes('MODEL_MARK_PROXY');
    el('regretBasis').textContent = `Kỳ ${index + 1} · ${dateLabel(row.decision_date)} → ${dateLabel(row.terminal_date)} · ${hasModelMark ? 'Phần trái phiếu chưa bán được định giá mô hình, chưa phải tiền đã thu.' : 'Giá trị đến từ dòng tiền và lệnh bán trong mô hình.'} Mã kỳ: ${row.episode_id}.`;
    for (const [position, tr] of [...el('regretRows').children].entries()) tr.classList.toggle('active', position === index);
  }
  function renderRegret(result) {
    validateRegretResult(result);
    state.regretResult = result;
    el('regretLoading').hidden = true;
    el('regretEmpty').hidden = true;
    el('regretResults').hidden = false;
    el('regretError').hidden = true;
    el('regretTier').textContent = result.research_tier;
    el('regretTotal').textContent = `${number(result.total_curve_model_regret_per_100_face)} /100`;
    el('regretMean').textContent = `${number(result.mean_curve_model_regret_per_100_face)} /100`;
    el('regretEpisodes').textContent = text(result.episode_count);
    el('regretProtocol').textContent = result.protocol;
    el('regretProtocolPlain').textContent = result.protocol === 'PRIMARY_ONE_SALE'
      ? 'Phạm vi kỳ này: chỉ so sánh giữ hoặc bán tối đa một đợt; phương án nhìn lại dùng cùng tập lựa chọn và ràng buộc.'
      : 'Phạm vi kỳ này: giữ hoặc bán tối đa hai đợt; phương án nhìn lại dùng cùng tập lựa chọn và ràng buộc.';
    el('regretScope').textContent = `${result.research_tier} · point_in_time_claim=false · mô hình không có fill.`;
    el('regretSourceInfo').textContent = `${result.harness_revision || 'unknown revision'} · ${result.episode_count} cửa sổ · input SHA-256 ${result.input_sha256 || 'không có'}. Hash trong JSON là thông tin do tệp khai báo, chưa xác thực nguồn.`;
    el('regretMeta').textContent = `Forecast: ${result.episodes[0]?.forecast_method || 'UNKNOWN'} · ${result.scope?.day_count || 'MODEL_DAY_COUNT_UNKNOWN'} · ${result.scope?.cash_benchmark || 'MODEL_CASH_RATE'} · public runner ${result.scope?.public_empirical_runner || 'EMPIRICAL_EXECUTION_LOCKED'}.`;
    const body = el('regretRows'); body.replaceChildren();
    for (const [index, row] of result.episodes.entries()) {
      const tr = document.createElement('tr');
      tr.tabIndex = 0;
      tr.setAttribute?.('role', 'button');
      tr.setAttribute?.('aria-label', `Xem chi tiết kỳ ${row.episode_id}`);
      tr.addEventListener('click', () => renderRegretDetail(index));
      tr.addEventListener('keydown', event => {
        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); renderRegretDetail(index); }
      });
      const values = [`Kỳ ${index + 1}`, `${dateLabel(row.decision_date)} → ${dateLabel(row.terminal_date)}`,
        labels[row.selected_plan.kind] || row.selected_plan.kind,
        labels[row.hindsight_oracle.kind] || row.hindsight_oracle.kind,
        number(row.selected_plan.expected_terminal_wealth.per_100_face), number(row.strategy_wealth_per_100_face),
        number(row.oracle_wealth_per_100_face), number(row.curve_model_regret_per_100_face)];
      for (const value of values) { const td = document.createElement('td'); td.textContent = text(value); tr.appendChild(td); }
      body.appendChild(tr);
    }
    renderRegretDetail(0);
    renderRegretChart(result);
  }
  function renderRegretChart(result) {
    const instance = chart('regretChart', 'chartRegret');
    const narrow = el('regretChart').clientWidth < 520;
    if (instance) instance.setOption({
      color: [chartPalette.blue, chartPalette.amber, chartPalette.teal],
      grid: { left: narrow ? 37 : 52, right: narrow ? 7 : 22, top: narrow ? 78 : 55, bottom: narrow ? 68 : 55 },
      legend: { data: ['Selected · forecast', 'Fixed · realised', 'Oracle · realised'],
        orient: narrow ? 'vertical' : 'horizontal', right: narrow ? 3 : 'center', top: 4,
        itemWidth: narrow ? 10 : 25, itemHeight: 10, textStyle: { color: chartPalette.axis, fontSize: narrow ? 10 : 12 } },
      tooltip: { trigger: 'axis', valueFormatter: value => `${number(value)} /100` },
      xAxis: { type: 'category', data: result.episodes.map((row, index) => `Kỳ ${index + 1}`), axisLabel: { color: chartPalette.axis, interval: 0, rotate: narrow ? 24 : 0, fontSize: narrow ? 10 : 12 }, axisLine: { lineStyle: { color: chartPalette.axis } } },
      yAxis: { type: 'value', name: narrow ? '' : 'W(H) /100', nameTextStyle: { color: chartPalette.axis }, axisLabel: { color: chartPalette.axis }, splitLine: { lineStyle: { color: chartPalette.grid } } },
      series: [
        { name: 'Selected · forecast', type: 'bar', barMaxWidth: 28, data: result.episodes.map(row => row.selected_plan.expected_terminal_wealth.per_100_face) },
        { name: 'Fixed · realised', type: 'bar', barMaxWidth: 28, data: result.episodes.map(row => row.strategy_wealth_per_100_face) },
        { name: 'Oracle · realised', type: 'bar', barMaxWidth: 28, data: result.episodes.map(row => row.oracle_wealth_per_100_face) }
      ]
    }, true);
  }
  async function loadRegret(loader) {
    const version = ++state.regretLoadVersion;
    el('regretLoading').hidden = false;
    el('regretEmpty').hidden = true;
    el('regretResults').hidden = true;
    el('regretError').hidden = true;
    try {
      const result = await loader();
      if (version !== state.regretLoadVersion) return;
      renderRegret(result);
    } catch (error) {
      if (version !== state.regretLoadVersion) return;
      state.regretResult = null;
      el('regretLoading').hidden = true;
      el('regretEmpty').hidden = false;
      el('regretError').textContent = `Không thể đọc kết quả P3-B: ${error.message}`;
      el('regretError').hidden = false;
    }
  }
  async function loadRegretExample() {
    if (isFileMode()) return;
    await loadRegret(async () => {
      const response = await fetch('/RYC_P3B_SYNTHETIC_RESULT_2026-11-16.json', { cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return response.json();
    });
  }
  function switchTab(which) {
    const regret = which === 'regret';
    el('pfIntro').hidden = regret;
    el('regretIntro').hidden = !regret;
    el('pfWorkspace').hidden = regret;
    el('pfSidebar').hidden = regret;
    el('regretWorkspace').hidden = !regret;
    el('regretSidebar').hidden = !regret;
    el('tabPF').setAttribute?.('aria-selected', String(!regret));
    el('tabRegret').setAttribute?.('aria-selected', String(regret));
    stopPlayback();
    root.setTimeout?.(() => [state.chartComparison, state.chartCurve, state.chartPrice, state.chartHeat, state.chartLedger, state.chartRegret].forEach(item => item?.resize()), 20);
  }
  async function run() {
    if (isFileMode()) {
      setError('Hãy chạy python demos/perfect-foresight/app/ryc_v3_local_server.py --host 127.0.0.1 --port 8772 rồi mở http://127.0.0.1:8772/ryc_perfect_foresight_workspace.html');
      return;
    }
    let payload;
    try { payload = parsePayload(); } catch (error) { setError(`JSON không hợp lệ: ${error.message}`); return; }
    const version = state.version;
    state.activeRun = version;
    el('runButton').disabled = true;
    el('runButton').textContent = 'Đang tính…';
    el('rerunButton').disabled = true;
    setError('');
    try {
      const response = await fetch('/api/v3/analyse', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
      const body = await response.json();
      if (version !== state.version) return;
      if (!response.ok) throw new Error(`HTTP ${response.status}: ${body.error || 'Không rõ lỗi đầu vào.'}`);
      renderResult(body);
    } catch (error) {
      if (version === state.version) setError(`Không thể phân tích: ${error.message}`);
    } finally {
      if (state.activeRun === version) {
        el('rerunButton').disabled = false;
        el('runButton').disabled = false;
        el('runButton').textContent = 'Tính phương án';
        state.activeRun = null;
      }
    }
  }
  async function loadExample() {
    try {
      const response = await fetch('/ryc_perfect_foresight_example.json', { cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const base = await response.json();
      state.example = base;
      state.preset = 'flat';
      setPayload(base);
      document.querySelector('[data-preset="flat"]')?.classList.add('active');
      await run();
    } catch (error) { setError(`Không tải được example JSON: ${error.message}`); }
  }
  async function health() {
    try {
      const response = await fetch('/api/health', { cache: 'no-store' });
      const data = await response.json();
      if (!response.ok || data.status !== 'ok') throw new Error(data.error || `HTTP ${response.status}`);
      el('apiStatus').textContent = 'Mô hình đã kết nối';
      el('apiStatus').classList.add('online');
    } catch (error) { el('apiStatus').textContent = `Chưa kết nối mô hình · ${error.message}`; }
  }
  function init() {
    el('jsonEditor').addEventListener('input', () => {
      stale();
      state.preset = 'custom';
      try { const payload = parsePayload(); syncControls(payload); updateHeaderFacts(payload); setError(''); } catch (_) { /* Advanced JSON is edited freely until Run. */ }
    });
    el('runButton').addEventListener('click', run);
    el('rerunButton').addEventListener('click', run);
    for (const input of document.querySelectorAll('[data-control]')) {
      input.addEventListener(input.type === 'range' ? 'input' : 'change', () => updateControl(input.dataset.control, input.value, input.dataset.rangeIndex === 'true'));
    }
    el('upload').addEventListener('change', async event => {
      const file = event.target.files?.[0]; if (!file) return;
      try { state.preset = 'custom'; setPayload(JSON.parse(await file.text())); } catch (error) { setError(`Tệp JSON không hợp lệ: ${error.message}`); }
      event.target.value = '';
    });
    document.querySelectorAll('[data-preset]').forEach(button => button.addEventListener('click', () => {
      try {
        state.preset = button.dataset.preset;
        setPayload(applyPreset(state.example || parsePayload(), button.dataset.preset));
        document.querySelectorAll('[data-preset]').forEach(item => item.classList.toggle('active', item === button));
      }
      catch (error) { setError(error.message); }
    }));
    el('curveSlider').addEventListener('input', () => { stopPlayback(); if (state.result) { renderCurve(state.result); renderHeatmap(state.result); } });
    el('curveScenario').addEventListener('change', () => { stopPlayback(); el('curveSlider').value = 0; if (state.result) { renderCurve(state.result); renderHeatmap(state.result); } });
    el('playButton').addEventListener('click', togglePlayback);
    el('stepButton').addEventListener('click', stepTimeline);
    el('planChoice').addEventListener('change', () => state.result && renderLedger(state.result));
    el('ledgerScenario').addEventListener('change', () => state.result && renderLedger(state.result));
    el('tabPF').addEventListener('click', () => switchTab('pf'));
    el('tabRegret').addEventListener('click', () => switchTab('regret'));
    el('advancedInspector').addEventListener('toggle', () => {
      if (el('advancedInspector').open) el('jsonEditor').focus();
    });
    document.querySelectorAll('.curve-detail, .chart-detail, .regret-chart-detail').forEach(details => details.addEventListener('toggle', () => {
      if (details.open) root.setTimeout?.(() => [state.chartCurve, state.chartPrice, state.chartHeat, state.chartRegret].forEach(instance => instance?.resize()), 30);
    }));
    el('loadRegretExample').addEventListener('click', loadRegretExample);
    el('regretUpload').addEventListener('change', async event => {
      const file = event.target.files?.[0]; if (!file) return;
      await loadRegret(async () => JSON.parse(await file.text()));
      event.target.value = '';
    });
    root.addEventListener('resize', () => {
      [state.chartComparison, state.chartCurve, state.chartPrice, state.chartHeat, state.chartLedger, state.chartRegret].forEach(item => item?.resize());
      if (state.regretResult && !el('regretWorkspace').hidden) renderRegretChart(state.regretResult);
    });
    if (isFileMode()) {
      el('apiStatus').textContent = 'Đang mở bằng file:// · cần HTTP';
      el('apiStatus').classList.add('warn');
      el('runButton').disabled = true;
      el('loadRegretExample').disabled = true;
      el('pfEmpty').textContent = 'Chạy Python V3 và mở trang qua http://127.0.0.1:8772/ryc_perfect_foresight_workspace.html để phân tích.';
      return;
    }
    health(); loadExample(); loadRegretExample();
  }
  const api = { applyPreset, renderResult, renderLedger, renderRegret, validateRegretResult, loadRegretExample,
    updateControl, stepTimeline, togglePlayback, switchTab, run, stale, init, state };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (typeof document !== 'undefined') document.addEventListener('DOMContentLoaded', init);
})(typeof window !== 'undefined' ? window : globalThis);
