/* Local stock is an independent pool, shared by every row with the same main code. */
function localInventoryComparison(record, rowQuantity, totalQuantity, sets, options = {}) {
  if (!Number.isInteger(sets) || sets < 1 || sets > 1000000) throw new Error('套数必须为 1 到 1,000,000 的正整数');
  const required = totalQuantity * sets;
  const result = {rowRequired: rowQuantity * sets, required, quantity: null, difference: null,
    shared: totalQuantity !== rowQuantity, status: 'unmatched', issue: options.issue || ''};
  if (options.mixed || options.issue) return {...result, status: 'unresolved'};
  if (options.nc && (!record || (!record.status || record.status === 'ready') && Number.isInteger(record.quantity) && record.quantity >= 0)) {
    return {...result, quantity:record?.quantity ?? null, status:'nc'};
  }
  if (!record) return result;
  if (record.status && record.status !== 'ready' || !Number.isInteger(record.quantity) || record.quantity < 0) {
    return {...result, status: 'unresolved', issue: record.issue || ''};
  }
  return {...result, quantity: record.quantity, difference: record.quantity - required,
    status: record.quantity >= required ? 'sufficient' : 'insufficient'};
}

function localInventoryPresentation(result, market = {}, hasCode = true, selectedCode = '') {
  if (result.status === 'unresolved') return {label: '本地库存 待核实', local: '本地库存 待核实', tone: 'unresolved'};
  const local = !hasCode ? '本地库存 —' : `本地库存 ${result.quantity ?? 0}`;
  return {label: local, local, tone: 'neutral'};
}

if (typeof module !== 'undefined') module.exports = {localInventoryComparison, localInventoryPresentation};

if (typeof document !== 'undefined') (() => {
  if (candidateOnly) return;
  let records = {}, pendingRecords = {}, readIssue = '', initialized = false, busy = false, previewId = '', sequence = 0, pendingMatches = {};
  let dirty = false;
  window.hasUnsavedInventory = () => dirty;
  const count = document.getElementById('localInventorySetCount');
  const savedCount = loadUiSettings().localInventorySetCount;
  count.value = Number.isInteger(savedCount) && savedCount >= 1 && savedCount <= 1000000 ? savedCount : 3;
  let sets = Number(count.value);

  window.refreshLocalInventory = () => {
    const totals = new Map();
    const rowsByRef = new Map(currentRows.map(row => [row.ref, row]));
    for (const row of currentRows) {
      const code = stockCode(row);
      if (code && !rowIsNc(row)) totals.set(code, (totals.get(code) || 0) + Number(row.quantity || 0));
    }
    for (const tr of rowsEl.querySelectorAll('tr[data-ref]')) {
      const row = rowsByRef.get(tr.dataset.ref), cell = tr.querySelector('[data-local-inventory]');
      if (!row || !cell) continue;
      const code = stockCode(row), quantity = Number(row.quantity || 0);
      if (!initialized) { cell.textContent = '本地库存 读取中…'; continue; }
      const result = localInventoryComparison(records[code], quantity, totals.get(code) || quantity, sets,
        {nc: rowIsNc(row), mixed: row.selection_codes_mixed, issue: readIssue});
      const display = localInventoryPresentation(result, row.stock || {}, Boolean(code), code);
      cell.textContent = display.local;
      cell.className = `local-inventory-${display.tone}`;
      cell.title = result.issue || '';
    }
  };
  let refreshFrame;
  window.queueLocalInventoryRefresh = () => {
    if (refreshFrame) return;
    refreshFrame = requestAnimationFrame(() => { refreshFrame = null; window.refreshLocalInventory(); });
  };

  async function request(url, payload) {
    const response = await fetch(url, payload === undefined ? {} : {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
    const result = await response.json();
    if (!response.ok || result.ok === false) throw new Error(result.error || '本地库存请求失败');
    return result;
  }

  async function load() {
    try {
      const result = await request('/api/local_inventory');
      records = result.items || {};
      pendingRecords = result.unmatched || {};
      readIssue = '';
    } catch (error) { records = {}; pendingRecords = {}; readIssue = `库存读取失败：${error.message}`; }
    initialized = true;
    window.refreshLocalInventory();
  }
  window.reloadLocalInventory = load;

  count.addEventListener('input', () => {
    const next = count.value === '' ? NaN : Number(count.value);
    if (!Number.isInteger(next) || next < 1 || next > 1000000) {
      count.setAttribute('aria-invalid', 'true');
      count.setCustomValidity('套数必须为 1 到 1,000,000 的正整数');
      return;
    }
    count.removeAttribute('aria-invalid');
    count.setCustomValidity('');
    try {
      updateUiSettings({localInventorySetCount: next});
      sets = next;
      window.refreshLocalInventory();
    } catch (error) { setState(`套数保存失败：${error.message}`, 'error'); }
  });
  count.addEventListener('blur', () => {
    count.value = sets;
    count.removeAttribute('aria-invalid');
    count.setCustomValidity('');
  });

  const dialog = document.createElement('dialog');
  dialog.id = 'localInventoryDialog';
  dialog.setAttribute('aria-labelledby', 'localInventoryTitle');
  dialog.innerHTML = `<header><h2 id="localInventoryTitle">导入本地库存</h2><button type="button" data-inventory-close aria-label="关闭库存导入" title="关闭">&times;</button></header>
    <div class="inventory-body"><label class="inventory-file">Excel 文件<input id="inventorySource" type="text" spellcheck="false" aria-label="库存 Excel 路径"><button id="inventoryBrowse" type="button" aria-label="选择库存 Excel" title="选择 Excel 文件"><svg class="toolbar-icon" aria-hidden="true"><use href="#toolbar-icon-folder-open"></use></svg></button></label>
    <div class="inventory-sheet"><label>工作表 <select id="inventorySheet" aria-label="库存工作表"><option value="">自动识别</option></select></label><button id="inventoryPreview" type="button">预览</button></div>
    <p id="inventoryStatus" role="status" aria-live="polite">数量字段：同型号已购合计（当前现存量）</p>
    <div class="inventory-table-wrap"><table><thead><tr><th>C 编码</th><th>型号 / 品牌 / 封装</th><th>原库存</th><th>导入量</th><th>处理</th></tr></thead><tbody id="inventoryPreviewRows"></tbody></table></div>
    <details id="inventoryUnmatched" hidden open><summary>待匹配库存</summary><div class="inventory-table-wrap"><table><thead><tr><th>采购编号 / 型号</th><th>现存量</th><th>对应主料 C 编码</th></tr></thead><tbody></tbody></table></div></details>
    <details id="inventoryIssues" hidden><summary>待核实明细</summary><ul></ul></details></div>
    <footer><button type="button" data-inventory-close>取消</button><button id="inventoryConfirm" type="button" disabled>确认导入</button></footer>`;
  document.body.appendChild(dialog);
  const source = dialog.querySelector('#inventorySource'), sheet = dialog.querySelector('#inventorySheet');
  const status = dialog.querySelector('#inventoryStatus'), confirm = dialog.querySelector('#inventoryConfirm');
  const previewRows = dialog.querySelector('#inventoryPreviewRows');
  const controls = () => dialog.querySelectorAll('input, select, button');
  const setBusy = value => {
    busy = value;
    window.localInventoryBusy = value;
    controls().forEach(control => { control.disabled = value; });
    confirm.disabled = value || !previewId;
  };
  const invalidate = () => {
    sequence++;
    previewId = '';
    confirm.disabled = true;
    previewRows.innerHTML = '';
    dialog.querySelector('#inventoryIssues').hidden = true;
  };

  async function preview() {
    if (busy) return;
    invalidate();
    const expectedSequence = sequence;
    setBusy(true);
    status.textContent = '正在读取库存…';
    try {
      const result = await request('/api/local_inventory/preview', {path: source.value.trim(), sheet: sheet.value, matches: pendingMatches});
      if (expectedSequence !== sequence) return;
      pendingMatches = result.matches || {};
      sheet.innerHTML = result.sheets.map(name => `<option value="${esc(name)}">${esc(name)}</option>`).join('');
      sheet.value = result.sheet;
      const s = result.summary;
      status.textContent = `新增 ${s.added} · 更新 ${s.updated} · 重复 ${s.repeated} · 待核实 ${s.unresolved} · 无效行 ${s.invalid} · 待匹配 ${s.unmatched}`;
      previewRows.innerHTML = Object.entries(result.items).map(([code, item]) => `<tr><td>${esc(code)}</td><td>${esc(item.model || '')}<br>${esc([item.brand, item.package].filter(Boolean).join(' · '))}</td><td>${esc(item.previous_quantity ?? '—')}</td><td>${esc(item.quantity ?? '待核实')}</td><td>${esc(item.action)}${item.issue ? `<div>${esc(item.issue)}</div>` : ''}</td></tr>`).join('');
      const issues = dialog.querySelector('#inventoryIssues');
      issues.hidden = !result.issues.length;
      issues.querySelector('ul').innerHTML = result.issues.map(issue => `<li>${esc(issue)}</li>`).join('');
      const pending = dialog.querySelector('#inventoryUnmatched');
      const candidates = Object.entries(result.match_candidates || {});
      pending.hidden = !candidates.length;
      pending.querySelector('tbody').innerHTML = candidates.map(([key, item]) => `<tr><td>${esc(item.source_product_codes.join('、') || '无采购编号')}<br>${esc(item.model)}<br>${esc([item.brand, item.package].filter(Boolean).join(' · '))}</td><td>${esc(item.quantity ?? '待核实')}</td><td><input type="text" data-inventory-match="${esc(key)}" value="${esc(pendingMatches[key] || '')}" aria-label="${esc(item.model)} 对应主料 C 编码" placeholder="未匹配" spellcheck="false"></td></tr>`).join('');
      previewId = Object.keys(result.items).length || Object.keys(result.unmatched || {}).length ? result.preview_id : '';
    } catch (error) { status.textContent = error.message; }
    finally { setBusy(false); }
  }

  document.getElementById('importLocalInventory').addEventListener('click', () => {
    closeToolbarMenus();
    if (!dialog.open) dialog.showModal();
  });
  dialog.querySelectorAll('[data-inventory-close]').forEach(button => button.addEventListener('click', () => { if (!busy) {dirty=false;dialog.close();} }));
  dialog.addEventListener('cancel', event => { if (busy) event.preventDefault(); else dirty=false; });
  source.addEventListener('input', () => { dirty = true; invalidate(); pendingMatches = {}; dialog.querySelector('#inventoryUnmatched').hidden = true; sheet.innerHTML = '<option value="">自动识别</option>'; status.textContent = '等待预览'; });
  source.addEventListener('keydown', event => { if (event.key === 'Enter' && !event.isComposing) { event.preventDefault(); preview(); } });
  sheet.addEventListener('change', () => { dirty=true; pendingMatches = {}; preview(); });
  dialog.querySelector('#inventoryUnmatched').addEventListener('input', event => {
    const input = event.target.closest('[data-inventory-match]');
    if (!input) return;
    dirty = true;
    previewId = '';
    confirm.disabled = true;
    const value = input.value.trim().toUpperCase();
    if (value) pendingMatches[input.dataset.inventoryMatch] = value;
    else delete pendingMatches[input.dataset.inventoryMatch];
  });
  dialog.querySelector('#inventoryUnmatched').addEventListener('change', preview);
  dialog.querySelector('#inventoryPreview').addEventListener('click', preview);
  dialog.querySelector('#inventoryBrowse').addEventListener('click', async () => {
    if (busy) return;
    setBusy(true);
    try {
      const result = await request('/api/path_picker', {field: 'source_bom', value: source.value});
      if (!result.path) return;
      invalidate();
      pendingMatches = {};
      source.value = result.path;
      dirty = true;
      sheet.innerHTML = '<option value="">自动识别</option>';
    } catch (error) { status.textContent = error.message; }
    finally { setBusy(false); }
    if (source.value) await preview();
  });
  confirm.addEventListener('click', async () => {
    if (busy || !previewId) return;
    setBusy(true);
    try {
      const result = await request('/api/local_inventory/import', {confirmed: true, preview_id: previewId});
      invalidate();
      await load();
      dirty = false;
      if (toolOnly) await window.notifyNativeToolChanged();
      const s = result.summary;
      const message = `库存已导入：新增 ${s.added}，更新 ${s.updated}，重复 ${s.repeated}，待核实 ${s.unresolved}，待匹配 ${s.unmatched}；未出现的编码已保留`;
      status.textContent = message;
      setState(message, 'saved');
    } catch (error) { status.textContent = error.message; }
    finally { setBusy(false); }
  });
  load();
})();
