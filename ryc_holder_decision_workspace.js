(function () {
  'use strict';

  const API = 'http://127.0.0.1:8772/api/v3';
  const state = { exported: null, presentation: null, stale: false };
  const $ = id => document.getElementById(id);
  const value = id => $(id).value.trim();
  const exactNumber = id => {
    const raw = value(id);
    if (raw === '') throw new Error(`Thiếu giá trị bắt buộc: ${id}`);
    const parsed = Number(raw);
    if (!Number.isFinite(parsed)) throw new Error(`Giá trị số không hợp lệ: ${id}`);
    return /^-?\d+$/.test(raw) ? parsed : raw;
  };
  const esc = input => String(input).replace(/[&<>"']/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[char]);
  const display = input => input === null || input === undefined ? 'Chưa xác định' : String(input);
  const money = input => input === null || input === undefined
    ? 'Chưa xác định'
    : `${new Intl.NumberFormat('vi-VN', { maximumFractionDigits: 6 }).format(Number(input))} VND`;

  function couponRows() {
    return [...document.querySelectorAll('[data-coupon-row]')].map(row => ({
      claim_id: row.querySelector('[data-field="claim_id"]').value.trim(),
      entitlement_at: row.querySelector('[data-field="entitlement_at"]').value.trim(),
      receive_at: row.querySelector('[data-field="receive_at"]').value.trim(),
      release_at: row.querySelector('[data-field="release_at"]').value.trim(),
      amount_vnd: /^-?\d+$/.test(row.querySelector('[data-field="amount_vnd"]').value)
        ? Number(row.querySelector('[data-field="amount_vnd"]').value)
        : row.querySelector('[data-field="amount_vnd"]').value,
      holder_entitled_if_sell_now: row.querySelector('[data-field="holder_entitled_if_sell_now"]').checked,
    }));
  }

  function sourceRows() {
    return [...document.querySelectorAll('[data-source-row]')].map(row => ({
      source_id: row.querySelector('[data-field="source_id"]').value.trim(),
      locator: row.querySelector('[data-field="locator"]').value.trim(),
      sha256: row.querySelector('[data-field="sha256"]').value.trim() || null,
      status: row.querySelector('[data-field="status"]').value,
    }));
  }

  function buildRequest() {
    const sell = $('sellEnabled').checked ? {
      quote_id: value('quoteId'), source_ref_id: value('quoteSourceRef') || null,
      quote_observed_at: value('quoteObservedAt'), quote_expires_at: value('quoteExpiresAt'),
      order_at: value('orderAt'), fill_at: value('fillAt'),
      settlement_at: value('settlementAt'), release_at: value('releaseAt'),
      quantity: exactNumber('sellQuantity'), gross_proceeds_vnd: exactNumber('grossProceeds'),
      fees_vnd: exactNumber('fees'), net_proceeds_vnd: exactNumber('netProceeds'),
    } : null;
    return {
      builder_version: 'RYC-HOLDER-BASELINE-BUILDER-1',
      scope: 'SYNTHETIC_HOLDER_BASELINE', currency: 'VND',
      decision_at: value('decisionAt'), terminal_at: value('terminalAt'),
      mandate: { mode: 'FULL_CASH_EXIT', minimum_cash_vnd: exactNumber('minimumCash') },
      instrument: { instrument_id: value('instrumentId'), quantity: exactNumber('quantity') },
      initial_cash_vnd: exactNumber('initialCash'), cash_rate_decimal: value('cashRate'),
      source_refs: sourceRows(), sell_now: sell, coupon_cashflows: couponRows(),
      redemption: {
        claim_id: value('redemptionClaimId'), redemption_at: value('redemptionAt'),
        receive_at: value('redemptionReceiveAt'), release_at: value('redemptionReleaseAt'),
        quantity: exactNumber('redemptionQuantity'), amount_vnd: exactNumber('redemptionAmount'),
      },
    };
  }

  function actionName(id) {
    return id === 'SELL_NOW' ? 'Bán ngay' : id === 'HOLD_TO_REDEMPTION' ? 'Giữ đến hoàn gốc' : id;
  }

  function decisionCopy(decision) {
    if (decision.status === 'POLICY_TIE') return {
      title: 'HAI PHƯƠNG ÁN BẰNG NHAU TẠI H',
      explanation: 'Dữ liệu đã khai báo không tạo ra một lựa chọn duy nhất. Không có khuyến nghị bán hoặc giữ.',
    };
    if (decision.status === 'NO_COMPLETE_POLICY') return {
      title: 'CHƯA CÓ PHƯƠNG ÁN HOÀN TẤT MỤC TIÊU TIỀN MẶT TẠI H',
      explanation: 'Xem trạng thái dòng tiền bị khóa, khoản phải thu và lý do chưa hoàn tất ở từng phương án.',
    };
    const chosen = decision.policy_id === 'SELL_NOW_POLICY' ? 'BÁN NGAY' : 'GIỮ ĐẾN HOÀN GỐC';
    return {
      title: `${chosen} TẠO NHIỀU TIỀN KHẢ DỤNG HƠN TẠI H`,
      explanation: 'Kết luận chỉ đúng với execution, quyền coupon, hoàn gốc và thời điểm giải phóng tiền đã khai báo.',
    };
  }

  function componentText(components) {
    if (!components) return 'Chưa xác định';
    return [
      `khả dụng ${money(components.cash_available_vnd)}`,
      `bị khóa ${money(components.cash_locked_vnd)}`,
      `phải thu bán ${money(components.pending_sale_receivables_vnd)}`,
      `phải thu coupon ${money(components.pending_coupon_receivables_vnd)}`,
      `phải thu gốc ${money(components.pending_principal_receivables_vnd)}`,
    ].join(' · ');
  }

  function renderPresentation(presentation) {
    const copy = decisionCopy(presentation.decision);
    $('decisionTitle').textContent = copy.title;
    $('decisionExplanation').textContent = copy.explanation;
    $('warningList').innerHTML = presentation.evidence.warnings
      .map(item => `<li>${esc(item)}</li>`).join('');
    $('actionRows').innerHTML = presentation.actions.map(action => `<tr>
      <th scope="row">${esc(actionName(action.action_id))}</th>
      <td>${esc(action.status)}</td>
      <td>${esc(display(action.objective_cash_vnd))}</td>
      <td>${esc(display(action.shortfall_vnd))}</td>
      <td>${esc(componentText(action.terminal_components))}</td>
      <td>${action.reasons.length ? action.reasons.map(esc).join('<br>') : '—'}</td>
    </tr>`).join('');
    $('saleAudit').textContent = presentation.sale
      ? `Tiền bán gộp ${money(presentation.sale.gross_proceeds_vnd)} − phí ${money(presentation.sale.fees_vnd)} = tiền bán ròng ${money(presentation.sale.net_proceeds_vnd)}.`
      : 'Không có execution bán đủ điều kiện; phương án bán phải được hiển thị là loại trừ.';
    $('integrity').textContent = `Holder run ${presentation.holder_run_id} · request ${presentation.integrity.request_sha256} · result ${presentation.integrity.result_sha256}`;
    $('decisionPanel').hidden = false;
    $('resultRegion').dataset.state = presentation.decision.status;
    $('runStatus').textContent = 'Đã kiểm lại export và hiển thị kết quả từ presentation contract.';
  }

  function clearResult(reason) {
    state.exported = null;
    state.presentation = null;
    state.stale = false;
    $('decisionPanel').hidden = true;
    $('actionRows').innerHTML = '';
    $('exportButton').disabled = true;
    $('runStatus').textContent = reason || '';
  }

  function markStale() {
    if (!state.presentation || state.stale) return;
    state.stale = true;
    $('decisionPanel').hidden = true;
    $('exportButton').disabled = true;
    $('resultRegion').dataset.state = 'STALE';
    $('runStatus').textContent = 'Input đã thay đổi. Kết quả cũ bị ẩn; hãy chạy lại trước khi sử dụng hoặc xuất.';
  }

  function showError(message) {
    clearResult('Lần tính mới thất bại; kết quả cũ đã bị xóa.');
    $('errorBox').textContent = message;
    $('errorBox').hidden = false;
    $('resultRegion').dataset.state = 'ERROR';
  }

  async function post(path, body) {
    const response = await fetch(`${API}${path}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `API lỗi HTTP ${response.status}`);
    return payload;
  }

  async function runDecision() {
    clearResult('Đang tạo và kiểm lại hồ sơ quyết định…');
    $('errorBox').hidden = true;
    $('runButton').disabled = true;
    try {
      const request = buildRequest();
      const exported = await post('/holder-baseline', { request });
      const presentation = await post('/holder-presentation', { export: exported });
      state.exported = exported;
      state.presentation = presentation;
      state.stale = false;
      renderPresentation(presentation);
      $('exportButton').disabled = false;
    } catch (error) {
      showError(error.message || String(error));
    } finally {
      $('runButton').disabled = false;
    }
  }

  function downloadExport() {
    if (!state.exported || state.stale) return;
    const anchor = document.createElement('a');
    anchor.href = URL.createObjectURL(new Blob([JSON.stringify(state.exported, null, 2)], { type: 'application/json' }));
    anchor.download = `${state.exported.holder_run_id || 'ryc-holder-export'}.json`;
    document.body.append(anchor); anchor.click(); anchor.remove();
    setTimeout(() => URL.revokeObjectURL(anchor.href), 500);
  }

  function toggleSellFields() {
    $('sellFields').hidden = !$('sellEnabled').checked;
    markStale();
  }

  function addRow(templateId, containerId) {
    const template = $(templateId);
    const fragment = template.content.cloneNode(true);
    fragment.querySelector('[data-remove-row]').addEventListener('click', event => {
      event.currentTarget.closest('[data-coupon-row],[data-source-row]').remove();
      markStale();
    });
    $(containerId).append(fragment);
    markStale();
  }

  function init() {
    $('runButton').addEventListener('click', runDecision);
    $('exportButton').addEventListener('click', downloadExport);
    $('sellEnabled').addEventListener('change', toggleSellFields);
    $('addCoupon').addEventListener('click', () => addRow('couponTemplate', 'couponRows'));
    $('addSource').addEventListener('click', () => addRow('sourceTemplate', 'sourceRows'));
    document.querySelectorAll('#holderForm input,#holderForm select').forEach(control => control.addEventListener('input', markStale));
    toggleSellFields();
  }

  window.RYCHolderUI = { buildRequest, renderPresentation, runDecision, markStale, showError, state };
  document.addEventListener('DOMContentLoaded', init);
})();
