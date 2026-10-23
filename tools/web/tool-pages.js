/* Thin native-page adapters reuse the existing settings and selection transactions. */
(() => {
  const query = new URLSearchParams(location.search), child = query.get('view') === 'tool';
  const kind = query.get('tool');
  const titles = {footprint:'封装管理',settings:'工程设置',inventory:'本地库存',
    'value-parameters':'补充 Value',undo:'撤回修改','list-view':'列表视图',
    'field-mapping':'字段映射','plugin-update':'插件更新'};
  let shell, loading = false, writing = false;
  function status(message, error = false) {
    if (!shell) return;
    const line = shell.querySelector('[data-tool-status]');
    line.textContent = message; line.dataset.error = String(error);
  }
  function showShell(name) {
    const panel=shell?.querySelector('#columnPanel');
    if(panel) document.body.append(panel);
    shell?.remove();
    shell = document.createElement('section'); shell.className = 'native-tool-shell';
    const header = document.createElement('header'), title = document.createElement('h2'), close = document.createElement('button');
    title.textContent = titles[name]; close.type='button'; close.title='关闭'; close.setAttribute('aria-label','关闭工具窗口');
    close.innerHTML='<svg class="toolbar-icon" aria-hidden="true"><use href="#toolbar-icon-x"></use></svg>';
    close.onclick=()=>window.closeNativeTool().catch(error=>status(error.message,true));
    header.append(title,close); shell.append(header);
    const line=document.createElement('p'); line.dataset.toolStatus=''; line.role='status';
    line.textContent='正在读取…'; shell.append(line); document.body.append(shell);
  }
  window.addEventListener('workbench-tool-error', event => status(event.detail, true));
  async function rowFor(payload) {
    await loadBomOptions();
    const bom = payload.bom || '__schematic__';
    if (![...bomSelect.options].some(option=>option.value===bom)) throw new Error('数据来源已改变，请从当前行重新打开');
    bomSelect.value=bom; refsInput.value=payload.refs || payload.ref;
    await loadRows();
    const refs=String(payload.refs || payload.ref).split(',').map(ref=>ref.trim()).filter(Boolean).sort().join(',');
    const tr=[...rowsEl.querySelectorAll('tr[data-refs]')].find(row=>row.dataset.refs.split(',').map(ref=>ref.trim()).sort().join(',')===refs);
    if (!tr || loadedRowsBom!==bom) throw new Error('该行已拆分或合并，请从当前分组重新打开');
    return tr;
  }
  function canExit(cancelPreview = false) {
    const cancelableFootprintPreview = kind === 'footprint' && window.rowFootprintBusy &&
      !window.rowFootprintSaving && typeof window.cancelRowFootprintPreview === 'function';
    const allowed = (cancelableFootprintPreview || !loading) && !writing && !window.pathSettingsBusy && !window.localInventoryBusy &&
      !window.rowFootprintSaving && !window.pluginUpdateBusy &&
      !window.hasUnsavedPathSettings?.() && !window.hasUnsavedInventory?.() &&
      !window.hasUnsavedValueParameters?.() && !document.querySelector('[data-composing="1"]');
    if (allowed && cancelPreview && cancelableFootprintPreview) return window.cancelRowFootprintPreview();
    return allowed;
  }
  async function post(url,payload) {
    const response=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const result=await response.json();
    if(!response.ok || !result.ok) throw new Error(result.error || '操作未完成');
    return result;
  }
  async function undo(payload) {
    const refs=payload.refs || payload.ref, preview=await post('/api/undo_workbench',{refs});
    if(!preview.available) {status('该行没有可撤回的未同步修改');return;}
    status(preview.mode==='snapshot'?'恢复本轮工作台修改前记录，不修改原理图、PCB 或封装库。':
      '修改前快照缺失或已跨越同步，将采用当前已保存原理图字段；不会撤回已同步历史或猜测历史确认状态。');
    const table=document.createElement('table');
    for(const item of preview.changes) {
      const tr=document.createElement('tr');
      for(const text of [item.ref || refs,item.field,['description','alternate_parts','procurement_choice'].includes(item.field)?'恢复原记录':`${item.old || '—'} → ${item.new || '—'}`]) {
        const td=document.createElement('td');td.textContent=text;tr.append(td);
      }
      table.append(tr);
    }
    const apply=document.createElement('button');apply.type='button';apply.textContent='确认撤回本行修改';
    apply.onclick=async()=>{
      if(writing)return;
      writing=true;apply.disabled=true;
      try {
        await post('/api/undo_workbench',{refs,confirm:true,revision:preview.revision});
        await window.notifyNativeToolChanged();table.remove();apply.remove();status('已撤回工作台修改；原理图和 PCB 未改动');
      } catch(error) {status(error.message,true);}
      finally {writing=false;apply.disabled=false;}
    };
    shell.append(table,apply);
  }
  async function mount(name,payload) {
    showShell(name); loading=true;
    let footprintFinished=false;
    try {
      if(name==='settings' || name==='field-mapping') {
        await loadBomOptions(); await window.openPathSettings();
        if(name==='field-mapping') {
          document.documentElement.classList.add('tool-field-mapping');
          document.querySelector('#pathFieldMapping').open=true;
          document.querySelector('#pathSettingsTitle').textContent='字段映射';
        }
      } else if(name==='inventory') document.querySelector('#importLocalInventory').click();
      else if(name==='plugin-update') document.querySelector('#openPluginUpdates').click();
      else if(name==='list-view') {
        const panel=document.querySelector('#columnPanel');
        shell.append(panel);renderColumnPanel();panel.classList.add('open');
        panel.onchange=()=>void window.notifyNativeToolChanged().catch(error=>status(error.message,true));
      } else if(name==='undo') await undo(payload);
      else {
        const tr=await rowFor(payload);
        if(name==='footprint') {
          // Preview cancellation is safe; actual publication keeps its separate guard.
          const result = await window.manageRowFootprint(tr);
          footprintFinished=result?.finished === true;
        } else await window.openValueParameters(tr.dataset.refs);
      }
      if(name!=='undo') status('');
    } catch(error) {status(error.message,true);}
    finally {
      loading=false;
      if(child && footprintFinished && window.nativeToolMounted) {
        void window.closeNativeTool().catch(error=>status(error.message,true));
      }
    }
  }
  for(const name of Object.keys(titles)) window.registerWorkbenchTool?.(name,{
    mount:payload=>mount(name,payload),canReplace:()=>!loading && canExit(),prepareExit:()=>canExit(true),cancelExit:()=>{},
    unmount:()=>document.querySelectorAll('dialog[open]').forEach(dialog=>dialog.close())
  });
  if(child && titles[kind]) {
    document.documentElement.classList.add('native-utility-page');
    document.querySelectorAll('#pathSettingsDialog,#localInventoryDialog,#valueParametersDialog,#pluginUpdatesDialog').forEach(dialog=>{
      dialog.addEventListener('close',()=>{
        if(window.nativeToolMounted) setTimeout(()=>window.closeNativeTool().catch(error=>status(error.message,true)),0);
      });
    });
    document.querySelector('#autoSizeColumns').addEventListener('click',event=>{
      event.preventDefault();event.stopImmediatePropagation();
      void window.proposeNativeToolAction('autosize').catch(error=>status(error.message,true));
    },true);
  }
  if(!child && !candidateOnly) {
    let editEpoch=0, refreshVersion=0;
    rowsEl.addEventListener('input',()=>editEpoch++);
    rowsEl.addEventListener('change',()=>editEpoch++);
    async function refreshUtilityRows(kind) {
      const request=++refreshVersion;
      await flushRowEdits();
      if(['settings','field-mapping'].includes(kind)) await loadBomOptions(true);
      const bom=bomSelect.value, version=rowsLoadVersion, epoch=editEpoch;
      if(loadedRowsBom!==bom) {await loadRows();return;}
      const response=await fetch('/api/rows?refs='+encodeURIComponent(refsInput.value.trim())+
        '&bom='+encodeURIComponent(bom)+'&stock=0');
      const data=await response.json();
      if(!response.ok) throw new Error(data.error || '修改已保存，列表状态读取失败');
      if(request!==refreshVersion || version!==rowsLoadVersion || bom!==bomSelect.value) return;
      if(epoch!==editEpoch || rowEditQueue.pending) {
        setTimeout(()=>refreshUtilityRows(kind).catch(error=>setState(error.message,'error')),300);
        return;
      }
      const active=document.activeElement, tr=active?.closest('tr[data-refs]') ||
        [...rowsEl.querySelectorAll('tr[data-refs]')].find(row=>row.getBoundingClientRect().bottom>120);
      const anchor=rowAnchor(tr), field=active?.dataset.field;
      const selection=field && active.setSelectionRange ? [active.selectionStart,active.selectionEnd] : null;
      renderRows(data.rows,{patch:true});
      restoreRowAnchor(anchor);
      if(field && tr && document.activeElement===document.body) {
        const next=[...rowsEl.querySelectorAll('tr[data-refs]')].find(row=>row.dataset.refs===tr.dataset.refs);
        const input=[...next?.querySelectorAll('[data-field]') || []].find(input=>input.dataset.field===field);
        input?.focus({preventScroll:true});
        if(selection?.every(Number.isInteger) && input?.setSelectionRange) input.setSelectionRange(...selection);
      }
    }
    const menus={openPathConfig:'settings',openFieldMapping:'field-mapping',importLocalInventory:'inventory',
      toggleColumns:'list-view',openPluginUpdates:'plugin-update'};
    const rows={'manage-footprint':'footprint','value-parameters':'value-parameters','undo-workbench':'undo'};
    document.addEventListener('click',event=>{
      if(!window.pywebview?.api?.tool_window)return;
      const button=event.target.closest('button');
      const name=menus[button?.id] || rows[button?.dataset.action];
      if(!name)return;
      event.preventDefault();event.stopImmediatePropagation();closeToolbarMenus();
      const tr=button.closest('tr[data-refs]');
      const payload=tr?{refs:tr.dataset.refs,bom:loadedRowsBom || '__schematic__'}:{};
      void (async()=>{await flushRowEdits();await window.openNativeTool(name,payload);})()
        .catch(error=>setState(error.message,'error'));
    },true);
    window.addEventListener('storage',event=>{
      if(event.key===UI_STORAGE_KEY) {applyColumnOrder();applyColumnVisibility();renderColumnPanel();}
    });
    window.addEventListener('workbench-tool-changed',event=>{
      if(['footprint','value-parameters','undo','settings','field-mapping'].includes(event.detail?.kind)) {
        void refreshUtilityRows(event.detail.kind).catch(error=>setState(error.message,'error'));
      }
      if(event.detail?.kind==='inventory') void window.reloadLocalInventory?.();
    });
  }
})();
