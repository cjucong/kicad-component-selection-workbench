/* One explicit scope, two independently verified CAD stages. */
(() => {
  const child = new URLSearchParams(location.search).get('tool') === 'design-sync';
  const destinations = {schematic:'同步原理图', pcb:'同步 PCB', both:'全部同步'};
  const kinds = {rename:'封装仅改名',replace:'更换封装',fields:'字段更新',unknown:'类型待核对',none:'无需更新'};
  const fields = {comment:'Comment',value:'Value',footprint:'封装',lcsc_c_code:'C 编码',
    description:'物料参数',lcsc_url:'商品链接',lcsc_alt:'备选料',lcsc_alt_info:'备选料信息',
    procurement_url:'订货链接',procurement_info:'订货信息'};
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let current = null;
  async function request(path, payload) {
    const response = await fetch(path, payload === undefined ? {} : {
      method:'POST', headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const value = await response.json();
    if (!response.ok || (payload !== undefined && !value.ok)) {
      const error = new Error(value.error || '同步请求未能完成');
      error.rejected = value.ok === false;
      throw error;
    }
    return value;
  }
  const notify = () => child ? window.notifyNativeToolChanged?.() :
    window.dispatchEvent(new CustomEvent('workbench-design-synced'));

  function mount(payload) {
    if (current?.busy) throw new Error('同步任务尚未结束，请先查询当前任务状态');
    current?.dispose();
    const refs = [...new Set((Array.isArray(payload.refs) ? payload.refs : String(payload.refs || payload.ref || '').split(','))
      .map(ref => String(ref).trim().toUpperCase()).filter(Boolean))];
    if (!refs.length || !destinations[payload.destination]) throw new Error('请选择器件范围及同步目标');
    const state = {payload:{refs,destination:payload.destination,bom:payload.bom || '__schematic__',
      alignment:payload.alignment || 'center'},busy:false,closed:false,sequence:0,job:null,preview:null,
      continuation:null,polling:false,notified:new Set()};
    const panel = document.createElement(child ? 'section' : 'dialog');
    panel.className = 'design-sync-panel'; panel.dataset.designPanel = '';
    panel.setAttribute('aria-label','同步设计');
    panel.innerHTML = `<header><div><h2>同步设计</h2><p data-design-scope></p></div>
      <button type="button" data-design-close aria-label="关闭" title="关闭"><svg class="toolbar-icon" aria-hidden="true"><use href="#toolbar-icon-x"></use></svg></button></header>
      <div class="design-sync-content"><div data-design-phases class="design-sync-phases"></div>
      <p data-design-status role="status">正在读取设计数据…</p>
      <div class="design-progress" data-design-progress hidden><progress aria-label="同步进度" data-design-bar></progress>
      <span data-design-elapsed></span></div>
      <ol data-design-steps class="design-sync-steps" aria-label="同步阶段" aria-live="polite"></ol>
      <details data-design-timing hidden><summary>阶段耗时</summary><div data-design-timing-items></div></details>
      <div class="design-sync-options"><label data-design-alignment-label>封装对齐 <select data-design-alignment aria-label="封装对齐"><option value="center">中心对齐</option><option value="rigid">焊盘原位</option></select></label></div>
      <div data-design-changes></div>
      <div class="design-sync-confirmations"><label hidden><input type="checkbox" data-design-geometry>已核对目标封装、焊盘编号及引脚功能</label>
      <label hidden><input type="checkbox" data-design-movement>接受焊盘偏移；更新后检查连接与 DRC，走线不会随封装移动</label>
      <label hidden><input type="checkbox" data-design-drops>确认移除所列原端子，保留现有走线、过孔和区域，并检查悬空及铜皮接触</label>
      <label hidden><input type="checkbox" data-design-connected>再次确认：放弃的端子已分配网络，可能使电路断开；接受此修改并人工检查连接与 DRC</label></div>
      <p class="design-sync-safety" data-design-safety></p></div>
      <footer><button type="button" data-design-navigation hidden>仅重试打开 / 定位</button><button type="button" data-design-refresh hidden>重新预检</button><button type="button" data-design-query hidden>查询任务状态</button>
      <button type="button" data-design-both hidden>改为全部同步</button><button type="button" data-design-apply disabled></button></footer>`;
    document.body.append(panel);
    if (!child) panel.showModal();
    const q = selector => panel.querySelector(selector);
    const cancelPreflight = key => key ? request('/api/cancel_design_sync_preview',{job_id:key}).catch(()=>{}) : Promise.resolve();
    state.panel = panel;
    state.dispose = () => {
      state.closed = true; ++state.sequence; clearInterval(state.timer);
      void cancelPreflight(state.preflightJob); state.preflightJob = null; panel.remove();
    };
    current = state;
    q('[data-design-scope]').textContent = `${refs.length} 个器件 · ${refs.slice(0,8).join(', ')}${refs.length>8?' …':''} · ${payload.destination==='both'?'原理图 + PCB':destinations[payload.destination]}`;
    q('[data-design-alignment]').value = state.payload.alignment;
    function status(message, error = false) {
      q('[data-design-status]').textContent = message; q('[data-design-status]').dataset.error = String(error);
    }
    function progress(value, active) {
      const now = performance.now();
      state.progress = {value,active,at:now};
      q('[data-design-progress]').hidden = false;
      const bar = q('[data-design-bar]');
      if (active) bar.removeAttribute('value');
      else {bar.max=1;bar.value=value.ok===false?0:1;}
      const timings = value.timings || [];
      const statuses = {running:'进行中',waiting:'等待',succeeded:'已完成',failed:'未完成',skipped:'已跳过'};
      q('[data-design-steps]').innerHTML = (value.steps || []).map(step =>
        `<li data-step-status="${escape(step.status)}"><span>${escape(statuses[step.status] || step.status)}</span><span>${escape(step.message)}</span><time>${Number(step.seconds || 0).toFixed(1)} s</time></li>`).join('');
      q('[data-design-timing]').hidden = !timings.length;
      q('[data-design-timing-items]').innerHTML = timings.map(item=>
        `<div>${escape(item.message)} <span>${Number(item.seconds).toFixed(1)} s</span></div>`).join('');
      tick();
    }
    function tick() {
      if (!state.progress) return;
      const {value,active,at} = state.progress, extra = active ? (performance.now()-at)/1000 : 0;
      q('[data-design-elapsed]').textContent = `已用 ${(Number(value.elapsed_seconds || 0)+extra).toFixed(1)} s · 当前阶段 ${(Number(value.stage_elapsed_seconds || 0)+extra).toFixed(1)} s`;
    }
    state.timer = setInterval(tick,200);
    function busy(value) {
      state.busy = value;
      window.designSyncBusy = value;
      q('[data-design-close]').disabled = value;
      q('[data-design-alignment]').disabled = value;
      q('[data-design-refresh]').disabled = value;
      q('[data-design-navigation]').disabled = value;
      q('[data-design-apply]').disabled = true;
    }
    function enabled() {
      const pcb = state.preview?.pcb_preview;
      const geometry = (pcb?.items || []).some(item => item.needs_ack);
      const movement = (pcb?.items || []).some(item => item.max_error_mm > .001);
      const drops = (pcb?.items || []).flatMap(item=>item.dropped_terminals || []);
      q('[data-design-apply]').disabled = state.busy || state.loading || !state.preview || state.preview.blocked || pcb?.blocked ||
        (geometry && !q('[data-design-geometry]').checked) || (movement && !q('[data-design-movement]').checked) ||
        (drops.length && !q('[data-design-drops]').checked) || (drops.some(p=>p.net_assigned) && !q('[data-design-connected]').checked);
    }
    function changeText(item) {
      if (item.name === 'footprint') return `${kinds[item.kind] || kinds.unknown}（待 PCB 核验）: ${item.before || '—'} → ${item.after || '—'}`;
      return item.name === 'description' || item.name === 'Description' ? '物料参数更新' :
        `${fields[item.name] || item.name}: ${item.before || '—'} → ${item.after || '—'}`;
    }
    function render(preview) {
      state.preview = preview;
      const pcb = preview.pcb_preview, items = pcb?.items || [], schematic = preview.schematic_changes || [];
      q('[data-design-alignment-label]').hidden = !items.some(item => item.kind === 'replace');
      q('[data-design-geometry]').checked = false; q('[data-design-movement]').checked = false;
      q('[data-design-geometry]').parentElement.hidden = !items.some(item => item.needs_ack);
      q('[data-design-movement]').parentElement.hidden = !items.some(item => item.max_error_mm > .001);
      const drops=items.flatMap(item=>item.dropped_terminals || []);
      q('[data-design-drops]').checked=false;q('[data-design-connected]').checked=false;
      q('[data-design-drops]').parentElement.hidden=!drops.length;
      q('[data-design-connected]').parentElement.hidden=!drops.some(p=>p.net_assigned);
      q('[data-design-changes]').innerHTML = (schematic.length ? `<h3>原理图字段</h3><table><thead><tr><th>位号</th><th>改动</th></tr></thead><tbody>${schematic.map(item =>
        `<tr><td>${escape(item.ref)}</td><td>${escape(changeText(item))}</td></tr>`).join('')}</tbody></table>` : '') +
        (items.length ? `<h3>PCB 更新</h3><table><thead><tr><th>位号</th><th>改动类型</th><th>具体改动</th></tr></thead><tbody>${items.map(item =>
          `<tr><td>${escape(item.ref)}</td><td>${escape(kinds[item.kind] || kinds.unknown)}</td><td>${['rename','replace','unknown'].includes(item.kind) ?
            `<div class="design-footprint-change">${escape(item.before)} → ${escape(item.after)}</div>` : ''}${(item.field_changes || []).map(change =>
              `<div>${escape(changeText(change))}</div>`).join('')}${item.kind === 'unknown' ? `<div class="design-warning">${escape(item.reason || '请在封装管理中明确操作后重新预检')}</div>` : ''}${item.kind === 'replace' ?
                `<div class="design-geometry-info">中心偏差 ${Number(item.center_error_mm || 0).toFixed(4)} mm · 焊盘最大移动 ${Number(item.max_error_mm || 0).toFixed(4)} mm</div><button type="button" data-design-compare="${escape(item.ref)}">封装对比</button>${(item.pad_errors || []).length ?
                  `<details><summary>逐焊盘移动</summary>${item.pad_errors.map(p => `<div>${escape(p.number)}：${Number(p.error_mm).toFixed(6)} mm</div>`).join('')}</details>` : ''}
                  ${(item.pad_mapping || []).length?`<details><summary>焊盘对应与编号</summary>${item.pad_mapping.map(p=>`<div>原端子 ${escape(p.source)} ← 目标原编号 ${escape(p.target)} → 保存编号 ${escape(p.source)} · ${p.align?'参与对齐':'不参与对齐，仍继承网络'}</div>`).join('')}</details>`:''}
                  ${(item.differences || []).length?`<details><summary>焊盘几何差异</summary>${item.differences.map(p=>`<div>焊盘 ${escape(p.number)} · ${escape(p.fields.join(', '))}<pre>${escape(JSON.stringify(p.before,null,2))} → ${escape(JSON.stringify(p.after,null,2))}</pre></div>`).join('')}</details>`:''}
                  ${(item.extra_pads || []).length?`<div>额外焊盘：${escape(item.extra_pads.join(', '))} · 保留，未分配网络（已有适配实例接网不会被清空）</div>`:''}
                  ${(item.dropped_terminals || []).map(p=>`<div class="design-warning">放弃端子 ${escape(p.number)} · 网络 ${escape(p.net_name || (p.net_assigned?'编号 '+p.net_code:'未分配'))}<br>${escape(p.physical_connection)}<br>同网走线 ${p.same_net_objects.segment+p.same_net_objects.arc} / 过孔 ${p.same_net_objects.via} / 区域 ${p.same_net_objects.zone}<br>${escape(p.warning)}</div>`).join('')}` : ''}</td></tr>`).join('')}</tbody></table>` : '');
      q('[data-design-safety]').textContent = state.payload.destination === 'schematic' ? '仅写入所选原理图字段，不修改 PCB。' :
        state.payload.destination === 'both' && !state.continuation ? '先保存并关闭当前工程，同步原理图后核验 PCB 并确认。' :
        '仅更新所选器件，不改网络或走线。自动保存当前工程（含此前编辑），重新打开并定位。';
      q('[data-design-both]').hidden = preview.suggested_destination !== 'both';
      q('[data-design-apply]').textContent = state.continuation ? '继续同步 PCB' : destinations[state.payload.destination];
      status(preview.reason || (preview.blocked || pcb?.blocked ? '存在待核对的封装，尚未写入 PCB' :
        !items.length && !schematic.length && state.payload.destination !== 'both' ? '所选字段无需更新' : '已完成预检，请核对改动'));
      enabled();
    }
    function phases(value) {
      q('[data-design-phases]').textContent = Object.entries(value || {}).map(([name,result]) =>
        `${name==='schematic'?'原理图':'PCB'}：${result.ok?'已完成':result.uncertain?'结果待核验':'未完成'}`).join('   ·   ');
      const schematicDone = value?.schematic?.ok;
      if (schematicDone && !state.notified.has('schematic')) {
        state.notified.add('schematic'); Promise.resolve(notify()).catch(()=>{});
      }
    }
    async function preflight() {
      if (state.busy || state.closed) return;
      const generation = ++state.sequence;
      const previous = state.preflightJob; state.preflightJob = null;
      state.loading = true; state.preview = null;
      q('[data-design-apply]').disabled = true; q('[data-design-query]').hidden = true;
      q('[data-design-navigation]').hidden = true;
      q('[data-design-geometry]').checked = false; q('[data-design-movement]').checked = false;
      status('正在读取设计并预检…');
      progress({},true);
      try {
        await cancelPreflight(previous);
        if (state.closed || generation !== state.sequence) return;
        let value = await request('/api/start_design_sync_preview',state.payload);
        if (state.closed || generation !== state.sequence) {void cancelPreflight(value.job_id);return;}
        state.preflightJob = value.job_id;
        while (!state.closed && generation === state.sequence) {
          progress(value,!value.done); status(value.message || '正在预检…',value.ok===false);
          if (value.done) break;
          await new Promise(resolve=>setTimeout(resolve,400));
          if (state.closed || generation !== state.sequence) return;
          value = await request('/api/design_sync_job?id='+encodeURIComponent(value.job_id));
        }
        if (state.closed || generation !== state.sequence) return;
        state.preflightJob = null;
        if (!value.ok || !value.preview) throw new Error(value.error || '设计预检未完成');
        const result = value.preview;
        state.loading = false;
        if (state.continuation) state.continuation = result.preview_id;
        render(result);
      } catch (error) {
        if (!state.closed && generation === state.sequence) {
          state.loading = false; status(error.message,true); q('[data-design-refresh]').hidden = false;
          progress({...state.progress?.value,ok:false},false);
        }
      }
    }
    async function poll() {
      if (state.polling || state.closed) return;
      state.polling = true; q('[data-design-query]').hidden = true;
      try {
        for (;;) {
          const query = state.job ? 'id='+encodeURIComponent(state.job) : 'preview_id='+encodeURIComponent(state.preview.preview_id);
          const value = await request('/api/design_sync_job?'+query);
          if (state.closed) return;
          if (state.rejected && (!value.job_id || (value.done &&
              ['awaiting_pcb_confirmation','partial','expired'].includes(value.stage)))) {
            busy(false); state.preview = null;
            if (state.continuation) state.continuation = 'needs-preview';
            q('[data-design-refresh]').hidden = false;
            status(state.rejected,true); state.rejected = null;
            return;
          }
          if (!value.job_id) throw new Error('尚未确认任务编号，请继续查询，不要重复提交');
          state.job = value.job_id;
          phases(value.phase_results); status(value.message || '正在同步设计'); progress(value,!value.done);
          if (value.done) {
            busy(false);
            if (value.stage === 'awaiting_pcb_confirmation') {
              state.continuation = value.pcb_preview.preview_id;
              state.payload.destination = 'pcb';
              render({pcb_preview:value.pcb_preview,blocked:value.pcb_preview.blocked,schematic_changes:[]});
            } else {
              state.preview = null;
              if (value.stage === 'partial') {
                state.payload.destination = 'pcb'; state.continuation = 'needs-preview';
                q('[data-design-refresh]').textContent = '重新预检 PCB';
              }
              q('[data-design-refresh]').hidden = !['partial','failed'].includes(value.stage);
              q('[data-design-query]').hidden = value.stage !== 'uncertain';
              q('[data-design-navigation]').hidden = value.stage !== 'navigation_failed';
              status(value.message || value.error || '同步结束', !value.ok);
              if (value.stage === 'complete' && !state.notified.has('complete')) {
                state.notified.add('complete'); Promise.resolve(notify()).catch(()=>{});
              }
            }
            return;
          }
          await new Promise(resolve => setTimeout(resolve,500));
        }
      } catch (error) {
        progress({...state.progress?.value,ok:false},false);
        status('连接中断，结果尚未确认，请勿重复同步。'+(state.job?' 任务：'+state.job:''),true);
        q('[data-design-query]').hidden = false;
      } finally {state.polling = false;}
    }
    q('[data-design-close]').onclick = () => {
      if (state.busy) return;
      state.dispose(); if (child) void window.closeNativeTool?.();
    };
    panel.addEventListener('cancel',event => {event.preventDefault();q('[data-design-close]').click();});
    q('[data-design-alignment]').onchange = () => {state.payload.alignment=q('[data-design-alignment]').value;void preflight();};
    q('[data-design-geometry]').onchange = enabled; q('[data-design-movement]').onchange = enabled;
    q('[data-design-drops]').onchange = () => {q('[data-design-connected]').checked=false;enabled();};
    q('[data-design-connected]').onchange = enabled;
    q('[data-design-refresh]').onclick = preflight;
    q('[data-design-query]').onclick = poll;
    q('[data-design-navigation]').onclick = async () => {
      if (state.busy || !state.job) return;
      busy(true); q('[data-design-navigation]').hidden = true;
      try {
        await request('/api/retry_design_navigation', {job_id:state.job});
      } catch (error) {
        status(error.message,true);
      }
      await poll();
    };
    q('[data-design-both]').onclick = () => {state.payload.destination='both';state.continuation=null;void preflight();};
    q('[data-design-apply]').onclick = async () => {
      if (q('[data-design-apply]').disabled || state.busy) return;
      busy(true); status('正在提交同步任务…');
      progress({},true);
      state.rejected = null;
      const acknowledgments = {geometry:q('[data-design-geometry]').checked,pad_movement:q('[data-design-movement]').checked,
        dropped_terminals:q('[data-design-drops]').checked, connected_terminals:q('[data-design-connected]').checked};
      try {
        const value = state.continuation ? await request('/api/continue_design_sync',{
          job_id:state.job,preview_id:state.continuation,acknowledgments}) :
          await request('/api/start_design_sync',{preview_id:state.preview.preview_id,acknowledgments});
        state.job = value.job_id;
      } catch (error) {
        if (error.rejected) state.rejected = error.message;
        status('正在核对同步任务是否启动，请勿重复提交…');
      }
      await poll();
    };
    q('[data-design-changes]').onclick = async event => {
      const button = event.target.closest('[data-design-compare]');
      if (!button || state.busy || state.loading) return;
      const content = q('.design-sync-content'), footer = q('footer'), mount = document.createElement('div');
      mount.className = 'design-sync-comparison'; footer.before(mount);
      content.hidden = true; footer.hidden = true; busy(true);
      const previewToken = state.preview.pcb_preview.preview_id, ref = button.dataset.designCompare, id = 'PCB:'+ref;
      try {
        await window.chooseFootprint({preview_id:previewToken,c_code:ref,original_ids:[id],
          existing:[{id}],alignment_ids:[id],default_reference_id:id,libraries:[],compatible:true}, {
          mount,readonly:true,loadComparison:async()=> (await request('/api/pcb_update_comparison',{
            preview_id:previewToken,ref})).detail});
      } catch (error) {status(error.message,true);}
      finally {mount.remove();content.hidden=false;footer.hidden=false;busy(false);enabled();}
    };
    return preflight();
  }
  window.openDesignSync = async payload => {
    if (!child && window.pywebview?.api?.tool_window) {
      await flushRowEdits();
      return window.openNativeTool('design-sync',{...payload,refs:Array.isArray(payload.refs)?payload.refs.join(','):payload.refs});
    }
    if (typeof flushRowEdits === 'function') await flushRowEdits();
    return mount(payload);
  };
  window.registerWorkbenchTool?.('design-sync',{
    mount,canReplace:()=>!current?.busy,
    prepareExit:()=>!current?.busy,
    isBusy:()=>Boolean(current?.busy),
    unmount:()=>{current?.dispose();current=null;},
    cancelExit:()=>{}
  });
  window.addEventListener('workbench-tool-error',event=>{
    if(child && !current) document.body.textContent=event.detail;
  });
})();
