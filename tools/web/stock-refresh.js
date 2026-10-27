/* The durable queue lives on the backend, not in this window's request list. */
(() => {
  if (candidateOnly || toolOnly) return;
  const start = document.getElementById('queryMissingStock');
  const pause = document.getElementById('pauseStockQuery');
  const all = document.getElementById('queryAllStock');
  const freshness = document.getElementById('stockFreshness');
  let snapshot = null, cursor = 0, jobId = '', timer = null, polling = false, changing = false, generation = 0;
  let readSequence = 0, appliedRead = 0;
  const available = () => !window.desktopExitPreparing && !rowsLoading && loadedRowsBom !== null && loadedRowsBom === bomSelect.value;
  const codes = () => [...new Set(currentRows.flatMap(row => [stockCode(row),
    ...(row.stock?.entries || []).map(item => item.c_code),
    ...alternateCodes(row.alternate_parts)]).map(normalizeCCode).filter(Boolean))];

  function controls(data) {
    const busy = ['running','pausing'].includes(data.state);
    start.disabled = !available() || data.state === 'pausing' || changing;
    all.disabled = !available() || busy || changing;
    pause.disabled = !busy || data.state === 'pausing' || changing;
    freshness.disabled = busy || changing;
    const resume = data.state === 'paused' && data.pending > 0;
    setToolbarLabel(start, busy ? (data.state === 'pausing' ? '正在暂停' : '暂停查询') : resume ? '继续查询' : '开始查询');
    start.setAttribute('aria-label', busy ? '暂停库存查询' : resume ? '继续库存查询' : '开始库存查询');
    start.querySelector('use')?.setAttribute('href', `#toolbar-icon-${busy ? 'pause' : 'package-search'}`);
    start.dataset.tooltip = busy ? '暂停并保存进度，下次打开可继续。' : '优先查询未查、失败和过期库存；暂停后可继续。';
  }
  window.stockRefreshCanPause = () => ['running','pausing'].includes(snapshot?.state);
  window.syncStockRefreshControls = () => { if (snapshot) controls(snapshot); };

  function paint(data) {
    snapshot = data;
    if ((data.id || '') !== jobId) { jobId = data.id || ''; cursor = 0; }
    controls(data);
    const busy = ['running','pausing'].includes(data.state);
    if (['running','pausing','paused','completed'].includes(data.state)) {
      const state = {running:'查询中',pausing:'正在暂停',paused:'已暂停',completed:'已完成'}[data.state];
      stockProgress.classList.add('open');
      stockProgressFill.style.width = `${data.total ? Math.min(100, data.done/data.total*100) : 0}%`;
      stockProgressText.textContent = `库存${state} ${data.done}/${data.total} · 成功 ${data.succeeded} · 失败 ${data.failed}`
        + (data.skipped_fresh ? ` · 跳过近期 ${data.skipped_fresh}` : '')
        + (data.active_codes?.length ? ` · ${data.active_codes.join(' / ')}` : '')
        + (data.updated_at ? ` · ${data.updated_at.replace('T',' ')}` : '');
    }
    if (data.results) {
      window.stockRefreshApplying = true;
      try {
        for (const [code, stock] of Object.entries(data.results)) updateRowsForStock(code, stock);
      } finally { window.stockRefreshApplying = false; }
      if (Object.keys(data.results).length) applyActiveFilter();
      cursor = data.revision;
    }
    if (data.items) {
      for (const tr of document.querySelectorAll('#rows tr[data-stock-code]')) {
        const badge = tr.querySelector('[data-stock-freshness]');
        if (!badge) continue;
        const item = data.items[tr.dataset.stockCode];
        const labels = {running:'正在查询',pending:busy?'等待查询':'待继续',failed:'上次更新失败'};
        const age = stockQueryAge(rowDataForElement(tr)?.stock || {});
        badge.textContent = [labels[item?.state],age].filter(Boolean).join(' · ');
        badge.title = item?.error || '';
      }
    }
    if (data.error) setState(data.error, 'error');
  }

  function schedule() {
    clearTimeout(timer);
    if (['running','pausing'].includes(snapshot?.state) && !window.desktopExitPreparing) timer = setTimeout(poll, 600);
  }

  async function readState() {
    const captured = generation;
    const request = ++readSequence;
    const response = await fetch(`/api/stock_refresh?since=${cursor}&job=${encodeURIComponent(jobId)}`);
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || '读取库存进度失败');
    if (captured === generation && request > appliedRead &&
        (!snapshot || data.id !== snapshot.id || data.revision >= snapshot.revision)) {
      appliedRead = request;
      paint(data);
    }
    return data;
  }

  async function poll() {
    if (polling || window.desktopExitPreparing) return;
    polling = true;
    try { await readState(); }
    catch (error) {
      setState(`库存进度暂不可用：${error.message}；重新读取可恢复，不要重复开始`, 'error');
    } finally { polling = false; schedule(); }
  }

  async function change(payload) {
    if (changing) throw new Error('库存操作正在保存，请稍候');
    changing = true;
    generation += 1;
    if (snapshot) controls(snapshot);
    try {
      const response = await fetch('/api/stock_refresh', {method:'POST',
        headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.error || '库存操作失败');
      paint(data);
      return data;
    } finally { changing = false; if (snapshot) controls(snapshot); schedule(); }
  }

  window.startDurableStockRefresh = async mode => {
    if (!available()) throw new Error('请先加载当前数据');
    await readState();
    const resume = mode !== 'all' && snapshot?.state === 'paused' && snapshot.pending > 0;
    if (mode === 'all' && snapshot?.state === 'paused' && snapshot.pending > 0 &&
        !confirm('重新刷新全部库存将替换已暂停的查询进度，是否继续？')) return;
    const data = await change(resume ? {action:'resume', codes:codes()} :
      {action:'start', codes:codes(), mode, max_age_hours:Number(freshness.value)});
    await readState();
    setState(resume ? '已继续库存查询' : `库存查询已开始；跳过近期 ${data.skipped_fresh || 0} 项`, 'status');
  };
  window.pauseDurableStockRefresh = async () => {
    clearTimeout(timer);
    const data = await change({action:'pause'});
    setState(data.state === 'pausing' ? '正在暂停库存查询，进度自动保存' : '库存查询进度已保存', 'status');
    return data;
  };
  window.restoreStockRefresh = async () => {
    if (candidateOnly || toolOnly) return;
    try { await readState(); schedule(); }
    catch (error) { setState(`恢复库存进度失败：${error.message}`, 'error'); }
  };
  pause.addEventListener('click', () => window.pauseDurableStockRefresh().catch(error => setState(error.message,'error')));
  freshness.addEventListener('change', () => {
    updateUiSettings({stockMaxAgeHours:Number(freshness.value)});
    for (const tr of document.querySelectorAll('#rows tr[data-stock-code]')) {
      const badge = tr.querySelector('[data-stock-freshness]');
      if (badge) badge.textContent = stockQueryAge(rowDataForElement(tr)?.stock || {});
    }
  });
  const saved = Number(loadUiSettings().stockMaxAgeHours);
  if ([1,6,24].includes(saved)) freshness.value = String(saved);
})();
