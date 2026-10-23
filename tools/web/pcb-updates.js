/* PCB writes are always explicit, scoped, and independent of schematic sync. */
(() => {
  window.pcbUpdateBusy = false;
  const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const number = value => Number(value).toFixed(6).replace(/\.?0+$/, '');
  function padSummary(value) {
    const geometry = value?.geometry;
    if (!geometry) return value ? JSON.stringify(value) : '未提供';
    const size = item => `${number(Number(item?.x_nm || 0)/1e6)} × ${number(Number(item?.y_nm || 0)/1e6)} mm`;
    const copper = (geometry.copper_layers || []).map(layer =>
      `${String(layer.layer || '').replace('BL_', '')} ${size(layer.size)} ${String(layer.shape || '').replace('PSS_', '')}`).join('；');
    const drill = geometry.drill?.diameter ? `；钻孔 ${size(geometry.drill.diameter)}` : '';
    return (copper || '复杂焊盘') + drill + '；层 ' + (geometry.layers || []).map(layer => String(layer).replace('BL_', '')).join(', ');
  }
  async function post(path, payload) {
    const response = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || 'PCB 请求失败');
    return data;
  }
  window.previewSelectedPcbUpdate = async () => {
    if (window.openDesignSync) return window.openDesignSync({refs:selectedSyncRefs(),destination:'pcb',bom:loadedRowsBom});
    if (window.pcbUpdateBusy) return;
    if (bulkSyncRunning || window.rowFootprintBusy || document.querySelector('[data-candidate-saving="1"]')) {
      setState('请先完成当前同步或封装操作', 'error'); return;
    }
    window.pcbUpdateBusy = true;
    let closed = false, started = false, polling = false, preview = null, job = null;
    let comparing = false, sequence = 0;
    const dialog = document.createElement('dialog');
    dialog.className = 'pcb-dialog';
    dialog.setAttribute('aria-label', '更新 PCB（所选器件）');
    dialog.innerHTML = `<header><h2>更新 PCB（所选器件）</h2><button type="button" data-pcb-close title="关闭" aria-label="关闭"><svg aria-hidden="true"><use href="#toolbar-icon-x"></use></svg></button></header>
      <section class="pcb-update-body"><label class="pcb-alignment">对齐方式 <select data-pcb-alignment aria-label="对齐方式" disabled><option value="center">中心对齐</option><option value="rigid">焊盘原位</option></select></label><p class="pcb-update-status" role="status">正在读取当前 PCB 并预检…</p>
      <div class="pcb-update-table-wrap" hidden><table class="pcb-update-table"><thead><tr><th>位号</th><th>封装</th><th>X / Y（mm）</th><th>角度</th><th>焊盘核对</th></tr></thead><tbody></tbody></table></div>
      <p class="pcb-update-note" hidden>以当前 PCB 焊盘位置为基准。仅更新所选器件；不自动保存 PCB。已错位的器件不能据此找回历史位置。</p></section>
      <footer><label hidden><input type="checkbox" data-pcb-ack><span data-pcb-ack-text>已核对焊盘编号、尺寸、钻孔及引脚功能</span></label><div><button type="button" data-pcb-cancel>取消</button><button type="button" data-pcb-retry hidden>查询结果</button><button type="button" data-pcb-apply disabled>更新 PCB</button></div></footer>`;
    document.body.append(dialog); dialog.showModal();
    const q = selector => dialog.querySelector(selector);
    function status(message, error = false) { q('.pcb-update-status').textContent = message; q('.pcb-update-status').dataset.error = String(error); }
    function close() {
      if (started || comparing) return;
      closed = true; dialog.close(); dialog.remove(); window.pcbUpdateBusy = false;
    }
    q('[data-pcb-close]').onclick = close;
    q('[data-pcb-cancel]').onclick = close;
    dialog.addEventListener('cancel', event => { event.preventDefault(); close(); });
    q('[data-pcb-ack]').onchange = () => { q('[data-pcb-apply]').disabled = !q('[data-pcb-ack]').checked || !preview; };
    async function poll() {
      if (polling) return;
      polling = true; q('[data-pcb-retry]').hidden = true;
      try {
        for (;;) {
          const query = job ? 'id=' + encodeURIComponent(job) : 'preview_id=' + encodeURIComponent(preview.preview_id);
          const response = await fetch('/api/pcb_update_job?' + query);
          const data = await response.json();
          if (data.job_id) job = data.job_id;
          if (!response.ok) throw new Error('状态服务暂时不可用');
          if (data.done) {
            started = false;
            q('[data-pcb-close]').disabled = false; q('[data-pcb-cancel]').disabled = false;
            q('[data-pcb-alignment]').disabled = false;
            q('[data-pcb-cancel]').textContent = '关闭';
            if (!data.ok || !data.result?.ok) {
              status((data.error || 'PCB 更新未成功') + '\n任务：' + job, true);
            } else {
              status('已更新到 PCB 编辑器，尚未保存。' + (data.result.warning ? '\n' + data.result.warning : ''));
              setState('所选器件已更新到 PCB 编辑器，尚未保存；可在 KiCad 中一次撤销。', 'saved');
            }
            break;
          }
          status('正在原位替换并校验 PCB…');
          await new Promise(resolve => setTimeout(resolve, 500));
        }
      } catch (error) {
        status('连接中断，结果尚未确认。请勿重复更新。\n任务：' + job, true);
        q('[data-pcb-retry]').hidden = false;
      } finally { polling = false; }
    }
    q('[data-pcb-retry]').onclick = poll;
    q('[data-pcb-apply]').onclick = async () => {
      if (started || !preview || !q('[data-pcb-ack]').checked) return;
      started = true;
      q('[data-pcb-alignment]').disabled = true;
      q('[data-pcb-apply]').disabled = true; q('[data-pcb-close]').disabled = true; q('[data-pcb-cancel]').disabled = true;
      try {
        const data = await post('/api/start_pcb_update', {preview_id:preview.preview_id, acknowledged:true});
        job = data.job_id;
        await poll();
      } catch (error) {
        // Resolve by preview token; a lost start reply must not cause a new write.
        status('正在核对更新任务是否启动，请勿重复提交…');
        await poll();
      }
    };
    async function preflight() {
      if (started || comparing || closed) return;
      const token = ++sequence, alignment = q('[data-pcb-alignment]').value;
      preview = null; job = null;
      q('[data-pcb-apply]').disabled = true; q('[data-pcb-ack]').checked = false;
      q('.pcb-update-table-wrap').hidden = true; q('footer label').hidden = true;
      q('.pcb-update-note').hidden = true;
      status('正在读取当前 PCB 并预检…');
      try {
      await flushRowEdits();
      if (closed || token !== sequence) return;
      q('[data-pcb-alignment]').disabled = false;
      const refs = selectedSyncRefs();
      if (!refs.length) throw new Error('请先勾选需要更新 PCB 的器件行');
      const result = await post('/api/pcb_update_preview', {refs:refs.join(','), bom:loadedRowsBom, alignment});
      if (closed || token !== sequence) return;
      preview = result;
      q('tbody').innerHTML = preview.items.map(item => `<tr><td>${escape(item.ref)}<small>${item.back ? '背面' : '正面'}</small></td>
        <td>${escape(item.before)}<small>→ ${escape(item.after)}</small><button type="button" data-pcb-compare="${escape(item.ref)}" title="查看当前 PCB 与目标库封装叠加对比">封装对比</button></td>
        <td>${item.old_position_mm.map(number).join(' / ')}<small>→ ${item.position_mm.map(number).join(' / ')}</small></td>
        <td>${number(item.old_rotation_deg)}°<small>→ ${number(item.rotation_deg)}°</small></td>
        <td>${alignment === 'center' ? `封装中心偏差 ${number(item.center_error_mm || 0)} mm<small>焊盘最大移动 ${number(item.max_error_mm)} mm</small>` : `焊盘中心偏差 ${number(item.max_error_mm)} mm`}${(item.pad_errors || []).some(p=>p.error_mm > .001) ? `<details><summary>焊盘移动明细</summary>${item.pad_errors.map(p=>`<small>${escape(p.number)}：${number(p.error_mm)} mm</small>`).join('')}</details>` : ''}${item.differences.map(change => `<details><summary>焊盘 ${escape(change.number)} 属性有差异</summary><small>原：${escape(padSummary(change.before))}</small><small>新：${escape(padSummary(change.after))}</small></details>`).join('')}</td></tr>`).join('');
      q('.pcb-update-note').textContent = alignment === 'center'
        ? '按当前 PCB 铜焊盘中心对齐并校正方向，保留目标库几何。焊盘允许移动，原有走线不移动；更新后请检查连接和 DRC。不自动保存 PCB。'
        : '以当前 PCB 焊盘位置为基准。仅更新所选器件；不自动保存 PCB。已错位的器件不能据此找回历史位置。';
      q('[data-pcb-ack-text]').textContent = alignment === 'center'
        ? '已核对封装及引脚，接受焊盘偏移，并将在更新后检查走线与 DRC'
        : '已核对焊盘编号、尺寸、钻孔及引脚功能';
      q('.pcb-update-table-wrap').hidden = false; q('.pcb-update-note').hidden = false;
      q('footer label').hidden = false;
      status(`已预检 ${preview.items.length} 个器件`);
      q('tbody').onclick = async event => {
        const button = event.target.closest('[data-pcb-compare]');
        if (!button || comparing || started) return;
        comparing = true;
        const body = q('.pcb-update-body'), footer = q('footer'), mount = document.createElement('div');
        body.hidden = true; footer.hidden = true; mount.className = 'pcb-comparison-page';
        footer.before(mount); q('[data-pcb-close]').disabled = true;
        const id = 'PCB:' + button.dataset.pcbCompare;
        try {
          await window.chooseFootprint({preview_id:preview.preview_id, c_code:button.dataset.pcbCompare,
            original_ids:[id], existing:[{id}], alignment_ids:[id], default_reference_id:id,
            libraries:[], compatible:true}, {mount, readonly:true,
            loadComparison:async()=> (await post('/api/pcb_update_comparison',{
              preview_id:preview.preview_id,ref:button.dataset.pcbCompare})).detail});
        } finally {
          mount.remove(); body.hidden = false; footer.hidden = false;
          q('[data-pcb-close]').disabled = false; comparing = false;
        }
      };
      } catch (error) {
        if (!closed && token === sequence) status(error.message || String(error), true);
      }
    }
    q('[data-pcb-alignment]').onchange = preflight;
    await preflight();
  };
  document.getElementById('updateSelectedPcb')?.addEventListener('click', window.previewSelectedPcbUpdate);
})();
