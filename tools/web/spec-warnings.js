/* Checks always use the saved schematic, independent of editable row values. */
function specificationSummary(check) {
  const labels = {value: '标称值', footprint: '封装', dielectric: '温度系数 / 介质'};
  const lines = (check?.issues || []).map(issue => `${(issue.refs || []).join('、')} ${labels[issue.field] || issue.field}：原理图 ${issue.expected}；商品 ${issue.actual}`);
  for (const detail of check?.unknown_details || []) {
    lines.push(`${(detail.refs || []).join('、')} ${detail.message}`.trim());
  }
  const explained = new Set((check?.unknown_details || []).map(detail => detail.field));
  for (const field of check?.unknown_fields || []) {
    if (!explained.has(field)) lines.push(`${labels[field] || field}信息缺失或未识别，暂不能完成此项核对`);
  }
  if (check?.error) lines.push(check.error);
  return lines.join('\n');
}

if (typeof module !== 'undefined') module.exports = {specificationSummary};

if (typeof document !== 'undefined') (() => {
  if (candidateOnly) return;
  let timer, drawFrame, requestSequence = 0;
  const dialog = document.createElement('dialog');
  dialog.id = 'specWarningDialog';
  dialog.setAttribute('aria-labelledby', 'specWarningTitle');
  dialog.innerHTML = '<header><h2 id="specWarningTitle">选料参数核对</h2><button type="button" aria-label="关闭参数核对" title="关闭">&times;</button></header><div data-spec-details></div>';
  dialog.querySelector('button').addEventListener('click', () => dialog.close());
  document.body.appendChild(dialog);

  const draw = () => {
    const rowsByRef = new Map(currentRows.map(row => [row.ref, row]));
    for (const tr of rowsEl.querySelectorAll('tr[data-ref]')) {
      const marker = tr.querySelector('[data-spec-warning]'), row = rowsByRef.get(tr.dataset.ref);
      if (!marker || !row) continue;
      const check = row.spec_check, code = stockCode(row);
      if (!code || !check || check.c_code !== code || check.status === 'not_applicable' || rowIsNc(row)) {
        marker.replaceChildren();
        continue;
      }
      const mismatch = check.status === 'mismatch';
      const unknown = !mismatch && check.status === 'unknown';
      if (!mismatch && !unknown) { marker.replaceChildren(); continue; }
      const detail = specificationSummary(check) || '尚未取得可核对的商品或原理图信息';
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'spec-warning-icon' + (unknown ? ' spec-unknown' : '');
      button.textContent = mismatch ? '\u26a0' : '?';
      button.title = (mismatch ? '选料规格不符\n' : '规格待核对\n') + detail;
      button.setAttribute('aria-label', mismatch ? '查看选料规格警告' : '查看待核对规格');
      button.addEventListener('click', () => {
        dialog.querySelector('[data-spec-details]').textContent = code + '\n' + button.title;
        dialog.showModal();
      });
      marker.replaceChildren(button);
    }
  };

  window.refreshSpecificationWarnings = (recheck = false) => {
    if (!drawFrame) drawFrame = requestAnimationFrame(() => { drawFrame = null; draw(); });
    const sequence = ++requestSequence;
    clearTimeout(timer);
    if (!recheck) return;
    timer = setTimeout(async () => {
      const requested = currentRows.filter(row => stockCode(row) && !rowIsNc(row))
        .map(row => ({refs: row.refs || row.ref, c_code: stockCode(row)}));
      if (!requested.length) return;
      try {
        const response = await fetch('/api/spec_check', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({rows: requested})});
        const data = await response.json();
        if (sequence !== requestSequence) return;
        if (!response.ok || !data.ok) throw new Error(data.error || '规格核对失败');
        const checks = new Map(data.checks.map(item => [item.refs, item]));
        for (const row of currentRows) {
          const result = checks.get(row.refs || row.ref);
          if (result && result.c_code === stockCode(row)) {
            row.spec_check = result.check;
            delete row.spec_check_needs_refresh;
          }
        }
      } catch (error) {
        if (sequence !== requestSequence) return;
        for (const row of currentRows) if (stockCode(row)) row.spec_check = {
          c_code: stockCode(row), status: 'unknown', issues: [], error: error.message};
      }
      draw();
    }, 180);
  };
  window.refreshSpecificationWarnings();
})();
