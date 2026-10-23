/* A native tool window shares the backend, not the workbench's editable DOM. */
(() => {
  let signature = '', nativeOpen = false, nativeOpening = false;
  let candidateActionBusy = false, candidateExiting = false, blankPress = null;
  const bridge = () => window.pywebview?.api?.candidate_window;
  async function nativeAction(action, value) {
    if (window.pywebview?.api?.tool_window) {
      if (candidateOnly) {
        const result = await window.pywebview.api.tool_window(action);
        if (!result?.ok) throw new Error(result?.error || '更换型号窗口操作失败');
        return result;
      }
      if (action === 'open') return window.openNativeTool('candidate', value);
      const result = await window.pywebview.api.tool_window(action, {kind:'candidate'});
      if (!result?.ok) throw new Error(result?.error || '更换型号窗口操作失败');
      return result;
    }
    if (!bridge()) throw new Error('更换型号窗口尚未就绪，请稍后重试');
    const result = await bridge()(action, value ?? null);
    if (!result?.ok) throw new Error(result?.error || '更换型号窗口操作失败');
    return result;
  }
  window.nativeCandidateVisibility = value => {
    nativeOpen = Boolean(value?.open);
    candidateModal.classList.toggle('open', nativeOpen);
    candidateModal.classList.toggle('minimized', nativeOpen);
  };
  window.openNativeCandidates = async tr => {
    if (nativeOpening) return;
    nativeOpening = true;
    try {
      await flushRowEdits();
      const payload = {ref: tr.dataset.ref || '', refs: candidateRowKey(tr), bom: bomSelect.value || '__schematic__',
        comment: fieldValue(tr, 'comment'), value: fieldValue(tr, 'value'), footprint: fieldValue(tr, 'footprint'), code: currentLcscCode(tr)};
      await nativeAction('open', payload);
      candidateMinimizedTitle.textContent = '更换型号：' + compactRefsText(payload.refs);
      window.nativeCandidateVisibility({open: true});
    } finally { nativeOpening = false; }
  };
  window.minimizeNativeCandidates = async () => {
    if (!nativeOpen) return;
    try { await nativeAction('minimize'); }
    catch (error) { setState(error.message, 'error'); }
  };
  window.restoreNativeCandidates = async () => {
    try { await nativeAction('restore'); }
    catch (error) { setState(error.message, 'error'); }
  };
  window.nativeCandidateSaved = async payload => {
    try {
      await flushRowEdits();
      const tr = [...rowsEl.querySelectorAll('tr[data-ref]')].find(row => candidateRowKey(row) === payload?.refs);
      await loadRows(tr ? rowAnchor(tr) : null);
      setState('已更新选料，请按需同步原理图', 'saved');
    } catch (error) { setState('选料已保存，列表刷新失败：' + error.message, 'error'); }
  };
  window.notifyNativeCandidateSaved = () => nativeAction('changed');
  window.nativeCandidateCanExit = () => !candidateExiting && !candidateActionBusy && candidateTargetRow?.dataset.candidateSaving !== '1';
  window.prepareNativeCandidateExit = () => {
    if (!window.nativeCandidateCanExit()) return false;
    candidateExiting = true;
    document.body.inert = true;
    return true;
  };
  window.cancelNativeCandidateExit = () => { candidateExiting = false; document.body.inert = false; };
  window.receiveNativeCandidateRequest = payload => {
    if (!candidateOnly) return {ok: false, error: '此页面不是更换型号子窗口'};
    if (candidateExiting) return {ok: false, error: '工作台正在退出，请稍候'};
    const key = JSON.stringify(payload);
    if (key === signature && candidateModal.classList.contains('open')) {
      candidateModal.classList.remove('minimized');
      return {ok: true};
    }
    if (!window.nativeCandidateCanExit()) return {ok: false, error: '正在保存或转换，请稍候再更换其他器件'};
    signature = key;
    const tr = document.createElement('tr');
    tr.dataset.ref = payload.ref;
    tr.dataset.refs = payload.refs;
    tr.dataset.originalCode = payload.code || '';
    for (const field of ['comment', 'value', 'footprint']) {
      const td = document.createElement('td'), input = document.createElement('textarea');
      input.dataset.field = field; input.value = payload[field] || ''; td.append(input); tr.append(td);
    }
    rowsEl.replaceChildren(tr);
    currentRows = [{...payload}];
    bomSelect.replaceChildren(new Option(payload.bom, payload.bom));
    void openCandidates(tr).catch(error => { candidateSearchProgress.textContent = error.message; });
    return {ok: true};
  };
  if (candidateOnly) {
    document.body.classList.add('candidate-only');
    candidateRows.addEventListener('click', event => {
      if (!event.target.closest('button[data-action]')) return;
      if (candidateActionBusy || candidateExiting) { event.stopImmediatePropagation(); return; }
      candidateActionBusy = true;
      // The existing action listener owns the async operation and releases this flag.
    }, true);
    window.finishNativeCandidateAction = () => { candidateActionBusy = false; };
    window.closeNativeCandidates = () => {
      signature = '';
      void nativeAction('close').catch(error => { candidateSearchProgress.textContent = error.message; });
    };
    return;
  }
  const interactive = 'button,a,input,textarea,select,label,dialog,.fp-overlay,[role="button"],.modal:not(.candidate-minimized-bar)';
  document.addEventListener('pointerdown', event => {
    blankPress = null;
    if (event.button !== 0 || event.target.closest(interactive)) return;
    const surface = event.target === candidateModal || event.target.matches('html,body,main,header.workbench-chrome,.bom-search,.filter-tabs,.workbench-statusbar');
    if (!surface && String(event.target.textContent || '').trim()) return;
    blankPress = {target: event.target, x: event.clientX, y: event.clientY};
  });
  document.addEventListener('pointerup', event => {
    const press = blankPress; blankPress = null;
    if (!press || event.target !== press.target || Math.hypot(event.clientX - press.x, event.clientY - press.y) > 4) return;
    if (nativeOpen) void window.minimizeNativeCandidates();
    else if (candidateModal.classList.contains('open') && !candidateModal.classList.contains('minimized')) minimizeCandidates();
  });
  document.addEventListener('pointercancel', () => { blankPress = null; });
})();
