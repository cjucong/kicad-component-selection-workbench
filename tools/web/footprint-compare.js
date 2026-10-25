/* Real KiCad previews are loaded as images, never inserted as executable SVG markup. */
function ownsFootprintKeyboard(event, overlay) {
  if (event.defaultPrevented || event.isComposing || event.keyCode === 229 ||
      document.querySelector('[data-composing="1"]')) return false;
  const top = [...document.querySelectorAll('.fp-overlay')].filter(el => el.getClientRects().length).at(-1);
  return top ? top === overlay : overlay.getClientRects().length > 0;
}

window.resolveFootprintConflict = function resolveFootprintConflict(conflict, choice) {
  return new Promise(resolve => {
    const overlay = document.createElement('div');
    overlay.className = 'fp-overlay';
    overlay.innerHTML = `<section class="fp-dialog fp-conflict-dialog" role="dialog" aria-modal="true" aria-label="已有同名封装">
      <header class="fp-title"><strong>已有同名封装</strong><button type="button" data-fp-close title="取消并关闭" aria-label="关闭"><svg aria-hidden="true"><use href="#toolbar-icon-x"></use></svg></button></header>
      <div class="fp-body"><p class="fp-final" data-fp-conflict-name></p>
        <p class="fp-note" data-fp-conflict-note></p>
        <div class="fp-modes"><label><input type="radio" name="fp-conflict-mode" value="save-as" checked>另存新名称</label>
          <label><input type="radio" name="fp-conflict-mode" value="overwrite">覆盖已有封装</label></div>
        <label data-fp-save-as>新封装名称<input type="text" data-fp-new-name maxlength="120" aria-label="新封装名称"></label>
        <label class="fp-ack" data-fp-overwrite-warning hidden><input type="checkbox" data-fp-overwrite-ack>已知晓：其他引用此库封装的器件也可能受影响</label>
      </div><footer class="fp-footer"><span class="fp-error" role="status"></span><button type="button" data-fp-cancel>取消</button><button type="button" class="fp-apply" disabled>保存新名称</button></footer></section>`;
    const q = selector => overlay.querySelector(selector);
    const previousFocus = document.activeElement;
    let closed = false;
    q('[data-fp-conflict-name]').textContent = conflict.footprint;
    q('[data-fp-conflict-note]').textContent = conflict.changed
      ? '目标文件已变化，请重新确认。覆盖前会备份。'
      : '同名封装内容不同。覆盖前会备份。';
    const overwrite = () => q('[name=fp-conflict-mode]:checked').value === 'overwrite';
    function refresh() {
      const replacing = overwrite(), name = q('[data-fp-new-name]').value.trim();
      q('[data-fp-save-as]').hidden = replacing;
      q('[data-fp-overwrite-warning]').hidden = !replacing;
      q('.fp-apply').textContent = replacing ? '覆盖已有封装' : '保存新名称';
      q('.fp-apply').disabled = replacing ? !q('[data-fp-overwrite-ack]').checked
        : !name || name.toLowerCase() === conflict.name.toLowerCase() || /[<>:"/\\|?*\x00-\x1f]/.test(name) || /[. ]$/.test(name);
    }
    function close(value) {
      if (closed) return;
      closed = true;
      document.removeEventListener('keydown', keyboard, true);
      overlay.remove();
      if (previousFocus?.isConnected) previousFocus.focus();
      resolve(value);
    }
    function keyboard(event) {
      if (!ownsFootprintKeyboard(event, overlay)) return;
      if (event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); close(null); }
      if (event.key === 'Tab') {
        const elements = [...overlay.querySelectorAll('button,input')].filter(e => !e.disabled && e.getClientRects().length);
        const first = elements[0], last = elements.at(-1);
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }
    }
    overlay.addEventListener('change', refresh);
    overlay.addEventListener('input', refresh);
    q('[data-fp-close]').onclick = q('[data-fp-cancel]').onclick = () => close(null);
    q('.fp-apply').onclick = () => {
      if (q('.fp-apply').disabled) return;
      const next = {...choice};
      delete next.collision;
      delete next.collision_token;
      close(overwrite() ? {...next, collision:'overwrite', collision_token:conflict.token}
        : {...next, name:q('[data-fp-new-name]').value.trim()});
    };
    document.body.append(overlay);
    document.addEventListener('keydown', keyboard, true);
    q('[data-fp-new-name]').focus();
  });
};

window.chooseLibraryFootprint = function chooseLibraryFootprint(preview, options = {}) {
  if (preview.kind !== 'rename') {
    const existing = preview.existing || preview.items;
    return window.chooseFootprint({...preview, existing, default_reference_id:preview.default_id}, {...options, library:true});
  }
  return new Promise(resolve => {
    const overlay = document.createElement('div');
    overlay.className = options.mount ? 'fp-page' : 'fp-overlay';
    overlay.innerHTML = '<section class="' + (options.mount ? 'fp-embedded' : 'fp-dialog fp-rename-dialog') + '">' +
      '<header class="fp-title"><strong>库封装改名</strong><button type="button" data-fp-close title="关闭" aria-label="关闭"><svg aria-hidden="true"><use href="#toolbar-icon-x"></use></svg></button></header>' +
      '<div class="fp-body fp-rename-body">' +
        '<label class="fp-rename-field">改名前<input type="text" data-fp-before aria-label="改名前" readonly></label>' +
        '<label class="fp-rename-field">改名后<input type="text" data-fp-name aria-label="改名后" maxlength="241" spellcheck="false"></label>' +
        '<p class="fp-rename-hint">保留原封装几何，不改变 PCB 位置和方向。</p>' +
      '</div><footer class="fp-footer"><span class="fp-error" role="status">正在读取原封装…</span><button type="button" data-fp-retry hidden>重试</button><button type="button" data-fp-cancel>取消</button><button type="button" class="fp-apply" disabled>应用改名</button></footer></section>';
    const q = selector => overlay.querySelector(selector), previousFocus = document.activeElement;
    const controller = new AbortController();
    let closed = false, ready = false;
    const before = (preview.current_footprints || preview.original_ids).join(' / ');
    q('[data-fp-before]').value = before;
    q('[data-fp-name]').value = preview.default_library + ':' + preview.suggested_name;
    function target() {
      const text = q('[data-fp-name]').value.trim(), colon = text.indexOf(':');
      if (colon < 1 || colon === text.length - 1) return null;
      const library = text.slice(0, colon), name = text.slice(colon + 1);
      if (!preview.libraries.includes(library) || /[<>:"/\\|?*\x00-\x1f]/.test(name) || /[. ]$/.test(name)) return null;
      return {library, name};
    }
    function refresh() {
      q('.fp-apply').disabled = !ready || !target() || q('[data-fp-name]').value.trim() === before;
    }
    function close(value) {
      if (closed) return;
      closed = true;
      controller.abort();
      options.signal?.removeEventListener('abort', cancel);
      document.removeEventListener('keydown', keyboard, true);
      overlay.remove();
      if (!options.mount && previousFocus?.isConnected) previousFocus.focus();
      resolve(value);
    }
    const cancel = () => close(null);
    function keyboard(event) {
      if (!ownsFootprintKeyboard(event, overlay)) return;
      if (event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); cancel(); }
    }
    q('[data-fp-name]').oninput = refresh;
    q('[data-fp-close]').onclick = q('[data-fp-cancel]').onclick = cancel;
    q('.fp-apply').onclick = () => {
      const next = target();
      if (q('.fp-apply').disabled || !next) return;
      close({preview_id:preview.preview_id, source:'rename', existing_id:preview.default_id,
        target_mode:'existing', target_library:next.library, name:next.name, acknowledged:true});
    };
    (options.mount || document.body).append(overlay);
    if (!options.mount) { document.addEventListener('keydown', keyboard, true); q('[data-fp-name]').focus(); }
    options.signal?.addEventListener('abort', cancel, {once:true});
    if (options.signal?.aborted) { cancel(); return; }
    async function loadOriginal() {
      q('[data-fp-retry]').hidden = true;
      q('.fp-error').textContent = '正在读取原封装…';
      try {
        const response = await fetch('/api/library_footprint', {method:'POST', headers:{'Content-Type':'application/json'},
          body:JSON.stringify({preview_id:preview.preview_id, id:preview.default_id, render:false}), signal:controller.signal});
        const data = await response.json();
        if (closed) return;
        if (!response.ok || !data.ok) throw new Error(data.error || '原封装不可读取');
        ready = true;
        q('.fp-error').textContent = '';
        refresh();
      } catch (error) {
        if (!closed && error.name !== 'AbortError') {
          q('.fp-error').textContent = error.message;
          q('[data-fp-retry]').hidden = false;
        }
      }
    }
    q('[data-fp-retry]').onclick = () => void loadOriginal();
    void loadOriginal();
  });
};

window.chooseFootprint = function chooseFootprint(preview, options = {}) {
  return new Promise(resolve => {
    const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
    const originals = new Set(preview.original_ids || preview.current?.original_ids || []);
    const references = new Set(preview.alignment_ids || [...originals]);
    const renameIds = new Set(preview.rename_ids || [...originals]);
    const overlay = document.createElement('div');
    overlay.className = options.mount ? 'fp-page' : 'fp-overlay';
    overlay.innerHTML = `<form class="fp-radio-owner"><section class="${options.mount ? 'fp-embedded' : 'fp-dialog'} ${options.library ? 'fp-library-workspace' : ''}" ${options.mount ? '' : 'role="dialog" aria-modal="true"'} aria-label="封装对比与目标库">
      <header class="fp-title"><strong>封装对比与目标库</strong><span>${escape(preview.c_code)}</span>
      <button type="button" data-fp-close title="取消并关闭" aria-label="关闭"><svg aria-hidden="true"><use href="#toolbar-icon-x"></use></svg></button></header>
      ${options.library ? `<div class="fp-current-binding">当前封装：${escape(preview.original_ids.join(' / ') || '未指定')}</div>` : ''}
      <div class="fp-body">${options.manage ? `<div class="fp-management"><button type="button" data-fp-pick-library>替换库封装…</button><button type="button" data-fp-rename>仅改名保留原几何…</button><span data-fp-background role="status">${preview.c_code ? '立创封装正在后台读取…' : '无唯一 C 编码，仅提供本地封装操作'}</span></div><div class="fp-context">当前绑定：${escape(preview.original_ids.join(' / ') || '未指定')}</div>` : ''}<div class="fp-baseline"><label>对齐基准<select data-fp-existing aria-label="工程现有封装">${preview.existing.map(e => `<option value="${escape(e.id)}">${escape(e.source === 'pcb' ? `PCB 已保存 · ${e.placement.ref} · ${e.placement.footprint}` : e.id)}</option>`).join('')}</select></label><span>mm</span></div>
      ${options.library ? `<div class="fp-library-search"><select data-fp-filter-library aria-label="封装库"><option value="">全部工程库</option>${preview.libraries.map(n => `<option>${escape(n)}</option>`).join('')}</select><input type="text" data-fp-search placeholder="搜索封装名称" aria-label="搜索封装名称"></div><select class="fp-library-list" data-fp-items size="8" aria-label="库封装列表"></select>` : ''}
      <div class="fp-reference-context" data-fp-reference-context></div>
      <div class="fp-comparison">
        <div class="fp-compare-toolbar">
          <div class="fp-segments" role="group" aria-label="比较视图"><label><input type="radio" name="fp-view" data-fp-view="overlay" value="overlay" checked><span>叠加</span></label><label><input type="radio" name="fp-view" data-fp-view="side" value="side"><span>并排</span></label></div>
          <label class="fp-opacity">${options.library || options.readonly ? '目标' : '立创'}不透明度<input type="range" data-fp-opacity min="0" max="100" value="50" aria-label="${options.library || options.readonly ? '目标' : '立创'}不透明度"><output data-fp-opacity-value>50%</output></label>
          <label class="fp-marker-toggle"><input type="checkbox" data-fp-markers checked>原点 / 1号焊盘</label>
          ${options.mount ? '<button type="button" data-fp-layer-toggle aria-expanded="false">图层 ▾</button>' : ''}
        </div>
        <div class="fp-compare-content">
          <div class="fp-viewport" data-fp-layout="overlay"><div class="fp-scene" hidden>
            <div class="fp-stage" data-fp-stage="existing"><div class="fp-layer-stack" data-fp-source-layers="existing"></div><div class="fp-marker-stack" data-fp-marker-source="existing"></div></div>
            <div class="fp-stage" data-fp-stage="converted"><div class="fp-layer-stack" data-fp-source-layers="converted"></div><div class="fp-marker-stack" data-fp-marker-source="converted"></div></div>
          </div><div class="fp-canvas-status" data-fp-canvas-status role="status">正在读取预览…</div></div>
          <aside class="fp-layer-panel" aria-label="比较图层"><div data-fp-layer-controls></div></aside>
        </div>
        <div class="fp-alignment" data-fp-alignment role="status"></div>
      </div>
      <div class="fp-sources" role="group" aria-label="采用的封装">
        <label ${options.manage ? 'hidden' : ''}><input type="radio" name="fp-source" value="existing">复用现有封装</label>
        <label ${options.manage ? 'hidden' : ''}><input type="radio" name="fp-source" value="rename">仅改名，保留原几何</label>
        <label><input type="radio" name="fp-source" value="converted" checked>替换立创封装</label>
      </div>
      <div class="fp-destination"><legend>目标封装库</legend><div class="fp-modes">
        <label><input type="radio" name="fp-mode" value="existing" checked>合并到现有库</label>
        <label><input type="radio" name="fp-mode" value="new">新建封装库</label>
        <label><input type="radio" name="fp-mode" value="independent">独立转换库</label></div>
        <div class="fp-fields">
          <label data-fp-library-label>目标封装库<select data-fp-library>${preview.libraries.map(n => `<option ${n === preview.default_library ? 'selected' : ''}>${escape(n)}</option>`).join('')}</select></label>
          <label data-fp-new-label hidden>新库名称<input type="text" data-fp-new-library value="SelectedParts" maxlength="120"></label>
          <label>封装名称<input type="text" data-fp-name value="${escape(preview.suggested_name)}" maxlength="120"></label>
          <label data-fp-folder-label hidden>工程内目录<input type="text" data-fp-folder value="Libraries/DSMU"></label>
        </div><p class="fp-final" data-fp-final></p>
        ${preview.warning ? `<p class="fp-note">${escape(preview.warning)}</p>` : ''}
        <label class="fp-ack"><input type="checkbox" data-fp-ack>已核对焊盘差异与引脚功能，并保存关闭库编辑器</label>
      </div></div>
      <footer class="fp-footer"><span class="fp-error" role="status"></span><button type="button" data-fp-cancel>取消</button><button type="button" class="fp-apply" disabled>确认选择</button></footer>
    </section></form>`;
    const q = selector => overlay.querySelector(selector);
    q('.fp-radio-owner').onsubmit = event => event.preventDefault();
    const previousFocus = document.activeElement;
    const cache = new Map(), layerState = new Map(), urls = new Set();
    let closed = false, sequence = 0, controller = null, detail = null, libraryDetail = null;
    let localOnly = options.manage === true;
    const adjustment = window.mountFootprintAdjustment?.(overlay, {
      enabled: !options.readonly,
      requireMapping: !options.readonly,
      changed: () => { q('[data-fp-ack]').checked = false; refresh(); }
    });
    const selectedEntry = () => options.library ? preview.items.find(e => e.id === q('[data-fp-items]').value)
      : preview.existing.find(e => e.id === q('[data-fp-existing]').value);
    const source = () => q('[name=fp-source]:checked').value;
    const mode = () => q('[name=fp-mode]:checked').value;
    if (options.mount) {
      q('.fp-layer-panel').hidden = true;
      q('[data-fp-layer-toggle]').onclick = () => {
        q('.fp-layer-panel').hidden = !q('.fp-layer-panel').hidden;
        q('[data-fp-layer-toggle]').setAttribute('aria-expanded', String(!q('.fp-layer-panel').hidden));
      };
    }
    function refresh() {
      const entry = selectedEntry(), original = !!entry && originals.has(entry.id);
      const reference = !!entry && references.has(entry.id);
      adjustment?.setActive(source() === 'converted');
      if (options.readonly) {
        for (const selector of ['.fp-baseline', '.fp-sources', '.fp-destination', '.fp-apply']) q(selector).hidden = true;
        q('.fp-title strong').textContent = 'PCB 原位封装对比';
        q('[data-fp-cancel]').textContent = '返回预检';
        return;
      }
      if (options.library) {
        const mapped = adjustment?.isMapped() === true;
        q('.fp-destination').hidden = !mapped;
        q('.fp-sources').hidden = true;
        q('[data-fp-library-label]').hidden = mode() !== 'existing';
        q('[data-fp-new-label]').hidden = q('[data-fp-folder-label]').hidden = mode() !== 'new';
        q('[name=fp-mode][value=independent]').closest('label').hidden=true;
        const library=mode()==='new'?q('[data-fp-new-library]').value.trim():q('[data-fp-library]').value;
        q('[data-fp-final]').textContent = mapped ? '目标：'+library+':'+q('[data-fp-name]').value.trim() : '';
        q('.fp-apply').disabled = !(mapped ? detail && adjustment.isValid() && q('[data-fp-name]').value.trim() && library : libraryDetail?.compatible) || !q('[data-fp-ack]').checked;
        q('[data-fp-reference-context]').textContent = libraryDetail
          ? `${entry.id} · 焊盘 ${libraryDetail.pads} 个 · 编号 ${(libraryDetail.numbers || []).join(', ')}${libraryDetail.compatible ? '' : ' · 可通过手动适配替换'}` : '';
        return;
      }
      q('[name=fp-source][value=existing]').disabled = !entry || entry.source === 'pcb';
      q('[name=fp-source][value=rename]').disabled = !entry || !renameIds.has(entry.id);
      q('[name=fp-source][value=converted]').disabled = localOnly || (!reference && !adjustment?.isMapped());
      q('[data-fp-reference-context]').textContent = entry?.source === 'pcb'
        ? `PCB 放置：${entry.placement.position_mm.join(', ')} mm · ${entry.placement.rotation_deg}° · ${entry.placement.back ? '背面' : '正面'}`
        : reference && !original ? '手动库基准：用于恢复旧封装位置，请确认来源正确' : '';
      q('.fp-destination').hidden = localOnly || source() === 'existing';
      q('[data-fp-library-label]').hidden = mode() !== 'existing';
      q('[data-fp-new-label]').hidden = mode() !== 'new';
      q('[data-fp-folder-label]').hidden = mode() !== 'new';
      const name = q('[data-fp-name]').value.trim();
      const lib = mode() === 'existing' ? q('[data-fp-library]').value : mode() === 'new' ? q('[data-fp-new-library]').value.trim() : 'LCSC_' + preview.c_code + '_Selected';
      q('[data-fp-final]').textContent = '目标：' + lib + ':' + name;
      const acknowledged = q('[data-fp-ack]').checked && !!name && !!lib;
      q('.fp-apply').disabled = source() === 'existing' ? !entry || entry.source === 'pcb'
        : !acknowledged || (source() === 'rename' ? !entry || !renameIds.has(entry.id)
          : localOnly || (!adjustment?.isMapped() && (!reference || !preview.compatible || detail?.alignment?.ok !== true)) || adjustment?.isValid() === false);
    }
    function clearImages() {
      adjustment?.clear();
      overlay.querySelectorAll('[data-fp-source-layers], [data-fp-marker-source]').forEach(e => e.replaceChildren());
      urls.forEach(url => URL.revokeObjectURL(url));
      urls.clear();
      q('.fp-scene').hidden = true;
      q('[data-fp-layer-controls]').replaceChildren();
    }
    function updateDisplay() {
      q('[data-fp-layout]').dataset.fpLayout = q('[name=fp-view]:checked').value;
      const opacity = Number(q('[data-fp-opacity]').value) / 100;
      q('[data-fp-source-layers=converted]').style.opacity = String(opacity);
      q('[data-fp-opacity-value]').value = Math.round(opacity * 100) + '%';
      overlay.querySelectorAll('[data-fp-marker-source]').forEach(e => { e.hidden = !q('[data-fp-markers]').checked; });
      overlay.querySelectorAll('[data-fp-layer-image]').forEach(img => {
        img.hidden = layerState.get(img.dataset.source + ':' + img.dataset.layer) === false;
      });
    }
    function displayComparison(value) {
      const box = value.view_box;
      if (!Array.isArray(box) || box.length !== 4 || !box.every(Number.isFinite) || box[2] <= 0 || box[3] <= 0) throw new Error('比较坐标范围无效');
      detail = value;
      clearImages();
      q('.fp-scene').style.setProperty('--fp-ratio', `${box[2]} / ${box[3]}`);
      q('.fp-scene').style.setProperty('--fp-aspect', String(box[2] / box[3]));
      const controls = q('[data-fp-layer-controls]');
      for (const side of ['existing', 'converted']) {
        const fieldset = document.createElement('fieldset');
        fieldset.innerHTML = `<legend class="${side === 'existing' ? 'fp-local-color' : 'fp-converted-color'}">${side === 'existing' ? options.readonly ? '当前 PCB' : '本地' : options.library || options.readonly ? '目标库' : '转换'}</legend>`;
        const stack = q(`[data-fp-source-layers=${side}]`);
        for (const layer of value.layers || []) {
          const svg = value[side + '_layers']?.[layer.id];
          if (typeof svg !== 'string' || !svg.trim()) continue;
          const key = side + ':' + layer.id;
          if (!layerState.has(key)) layerState.set(key, layer.default_visible !== false);
          const label = document.createElement('label'), check = document.createElement('input');
          check.type = 'checkbox'; check.dataset.fpLayer = side; check.value = layer.id;
          check.checked = layerState.get(key);
          label.append(check, document.createTextNode(layer.label || layer.id));
          fieldset.append(label);
          const img = document.createElement('img');
          const url = URL.createObjectURL(new Blob([svg], {type:'image/svg+xml'}));
          urls.add(url);
          img.src = url; img.alt = `${side === 'existing' ? '本地' : '转换'} ${layer.label || layer.id}`;
          img.dataset.fpLayerImage = ''; img.dataset.source = side; img.dataset.layer = layer.id;
          stack.append(img);
        }
        controls.append(fieldset);
        const markers = q(`[data-fp-marker-source=${side}]`);
        for (const [kind, point] of [['origin', value.origin_mm], ['pad', value[side + '_pad_one']]]) {
          if (!Array.isArray(point) || point.length !== 2 || !point.every(Number.isFinite)) continue;
          const marker = document.createElement('span');
          marker.className = `fp-marker fp-marker-${kind} ${side === 'existing' ? 'fp-local-color' : 'fp-converted-color'}`;
          if (kind === 'origin') marker.dataset.fpOrigin = '';
          else marker.dataset.fpPadOne = side;
          marker.style.left = (point[0] - box[0]) / box[2] * 100 + '%';
          marker.style.top = (point[1] - box[1]) / box[3] * 100 + '%';
          marker.title = `${kind === 'origin' ? '原点' : '1号焊盘'} (${point.join(', ')}) mm`;
          marker.textContent = kind === 'origin' ? '+' : '1';
          markers.append(marker);
        }
      }
      const alignment = value.alignment || {};
      const format = (number, digits = 3) => Number.isFinite(number) ? String(Number(number.toFixed(digits))) : '—';
      q('[data-fp-alignment]').dataset.ok = String(alignment.ok === true);
      q('[data-fp-alignment]').textContent = value.pcb_comparison ? `当前 PCB / 目标库封装 · ${alignment.method === 'center' ? '铜焊盘中心对齐，允许焊盘偏移' : '按编号原位对齐'} · mm` : value.library_comparison ? `当前库封装 / 所选库封装 · 同原点、同比例 · mm${value.compatible === false ? ` · 仅预览，编号不匹配：本地 ${value.reference_numbers?.join(', ')} / 目标 ${value.target_numbers?.join(', ')}` : ''}` : value.local_only ? '当前绑定的本地库封装 · mm' : alignment.ok === true
        ? `中心已对齐 · 校正 ${format(alignment.rotation_deg)}° · 焊盘中心最大偏差 ${format(alignment.max_error_mm, 4)} mm${alignment.requires_review ? ' · 间距有差异，请核对' : ''}`
        : '仅预览，未配准：' + (alignment.reason || '请检查焊盘编号与原封装');
      q('[data-fp-alignment]').dataset.review = String(alignment.requires_review === true);
      if (value.local_only || value.library_comparison) q('[data-fp-alignment]').removeAttribute('data-ok');
      adjustment?.setComparison(value);
      q('.fp-scene').hidden = false;
      q('[data-fp-canvas-status]').hidden = true;
      updateDisplay();
      refresh();
    }
    async function loadComparison() {
      const entry = selectedEntry(), token = ++sequence;
      controller?.abort();
      controller = new AbortController();
      detail = null;
      libraryDetail = null;
      q('.fp-error').textContent = '';
      clearImages();
      q('[data-fp-alignment]').textContent = '';
      const status = q('[data-fp-canvas-status]');
      status.hidden = false;
      status.textContent = entry || options.loadComparison ? '正在读取预览…' : '请选择要预览的封装';
      refresh();
      if (!entry && !options.loadComparison) return;
      try {
        if (options.loadComparison) {
          const data = await options.loadComparison();
          if (closed || token !== sequence) return;
          displayComparison(data);
          return;
        }
        if (options.library) {
          const response = await fetch('/api/library_footprint', {method:'POST', headers:{'Content-Type':'application/json'},
            body:JSON.stringify({preview_id:preview.preview_id, id:entry.id, render:false}), signal:controller.signal});
          const data = await response.json();
          if (closed || token !== sequence) return;
          if (!response.ok || !data.ok) throw new Error(data.error || '无法读取库封装');
          libraryDetail = data.detail;
          q('[data-fp-name]').value = entry.id.split(':').slice(1).join(':') + '_mapped';
          refresh();
        }
        if (cache.has(entry.id)) { displayComparison(cache.get(entry.id)); return; }
        const response = await fetch('/api/footprint_comparison', {method:'POST', headers:{'Content-Type':'application/json'},
          body:JSON.stringify({preview_id:preview.preview_id, id:options.library ? (preview.default_reference_id || preview.default_id) : entry.id,
            ...(options.library ? {target_id:entry.id} : {})}), signal:controller.signal});
        const data = await response.json();
        if (closed) return;
        if (!response.ok || !data.ok) throw new Error(data.error || '无法读取比较预览');
        cache.set(entry.id, data.detail);
        if (token !== sequence) return;
        displayComparison(data.detail);
      } catch (error) {
        if (closed || token !== sequence || error.name === 'AbortError') return;
        detail = null;
        clearImages();
        status.hidden = false;
        status.textContent = '比较预览不可用';
        q('[data-fp-alignment]').textContent = '配准失败：' + (error.message || String(error));
        q('[data-fp-alignment]').dataset.ok = 'false';
        refresh();
      }
    }
    function close(value) {
      if (closed) return;
      closed = true;
      ++sequence;
      controller?.abort();
      clearImages();
      cache.clear();
      options.signal?.removeEventListener('abort', cancel);
      document.removeEventListener('keydown', keyboard, true);
      overlay.remove();
      if (!options.mount && previousFocus?.isConnected) previousFocus.focus();
      resolve(value);
    }
    const cancel = () => close(null);
    function keyboard(event) {
      if (!ownsFootprintKeyboard(event, overlay)) return;
      if (event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); close(null); }
      if (event.key === 'Tab') {
        const elements = [...overlay.querySelectorAll('button,input,select')].filter(e => !e.disabled && e.getClientRects().length);
        const first = elements[0], last = elements.at(-1);
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }
    }
    overlay.addEventListener('change', event => {
      if (event.target.matches('[data-fp-existing], [data-fp-items]')) {
        q('[data-fp-ack]').checked = false;
        void loadComparison();
      } else if (event.target.matches('[data-fp-layer]')) {
        layerState.set(event.target.dataset.fpLayer + ':' + event.target.value, event.target.checked);
        updateDisplay();
      } else { updateDisplay(); refresh(); }
    });
    overlay.addEventListener('input', () => { updateDisplay(); refresh(); });
    q('[data-fp-close]').onclick = q('[data-fp-cancel]').onclick = () => close(null);
    if (options.manage) {
      q('[data-fp-pick-library]').onclick = () => close({operation:'library'});
      q('[data-fp-rename]').onclick = () => close({operation:'rename'});
    }
    q('.fp-apply').onclick = () => {
      if (options.readonly) return;
      if (q('.fp-apply').disabled) return;
      if (options.library) {
        close({preview_id:preview.preview_id, source:'existing', existing_id:selectedEntry().id, acknowledged:true,
          reference_id:preview.default_reference_id || preview.default_id,
          ...(adjustment?.isMapped() ? {target_mode:mode(), target_library:mode()==='new'?q('[data-fp-new-library]').value.trim():q('[data-fp-library]').value,
            target_folder:q('[data-fp-folder]').value.trim(),
            name:q('[data-fp-name]').value.trim(), ...adjustment.choice()} : {})});
        return;
      }
      close({preview_id: preview.preview_id, source: source(), existing_id: q('[data-fp-existing]').value,
        target_mode: mode(), target_library: mode() === 'existing' ? q('[data-fp-library]').value : q('[data-fp-new-library]').value.trim(),
        target_folder: q('[data-fp-folder]').value.trim(), name: q('[data-fp-name]').value.trim(),
        acknowledged: q('[data-fp-ack]').checked,
        ...(source() === 'converted' ? adjustment?.choice() : {})});
    };
    (options.mount || document.body).append(overlay);
    if (!options.mount || options.readonly) document.addEventListener('keydown', keyboard, true);
    options.signal?.addEventListener('abort', cancel, {once:true});
    if (options.signal?.aborted) { cancel(); return; }
    const original = preview.existing.find(entry => entry.id === preview.default_reference_id)
      || preview.existing.find(entry => originals.has(entry.id));
    if (original) q('[data-fp-existing]').value = original.id;
    if (options.library) {
      q('.fp-baseline').hidden = true;
      const ack = q('[data-fp-ack]').closest('label');
      q('.fp-body').append(ack);
      ack.classList.add('fp-library-ack');
      ack.lastChild.textContent = '已核对尺寸、焊盘编号和引脚功能';
      const filter = () => {
        const library = q('[data-fp-filter-library]').value, text = q('[data-fp-search]').value.trim().toLowerCase();
        const previous = q('[data-fp-items]').value;
        const selected = previous || preview.default_id;
        const items = preview.items.filter(e => (!library || e.library === library) && e.id.toLowerCase().includes(text));
        q('[data-fp-items]').innerHTML = items.map(e => `<option value="${escape(e.id)}">${escape(e.label || e.id)}</option>`).join('');
        q('[data-fp-items]').value = items.some(e => e.id === selected) ? selected : items[0]?.id || '';
        if (!previous || q('[data-fp-items]').value !== previous) {
          q('[data-fp-ack]').checked = false;
          void loadComparison();
        }
      };
      q('[data-fp-search]').oninput = q('[data-fp-filter-library]').onchange = filter;
      q('.fp-apply').textContent = '应用替换';
      filter();
    } else { void loadComparison(); }
    if (options.loadConverted && preview.c_code) {
      void (async () => {
        try {
          const converted = await options.loadConverted();
          if (closed) return;
          preview = converted;
          localOnly = false;
          originals.clear(); references.clear(); renameIds.clear(); cache.clear();
          preview.original_ids.forEach(id => originals.add(id));
          preview.alignment_ids.forEach(id => references.add(id));
          (preview.rename_ids || []).forEach(id => renameIds.add(id));
          q('[data-fp-existing]').innerHTML = preview.existing.map(e => `<option value="${escape(e.id)}">${escape(e.source === 'pcb' ? `PCB 已保存 · ${e.placement.ref} · ${e.placement.footprint}` : e.id)}</option>`).join('');
          q('[data-fp-existing]').value = preview.default_reference_id;
          q('[data-fp-name]').value = preview.suggested_name;
          q('[data-fp-library]').value = preview.default_library;
          q('[data-fp-background]').textContent = '立创封装已就绪；原位替换按下方基准配准';
          q('[data-fp-ack]').checked = false;
          void loadComparison();
        } catch (error) {
          if (!closed) q('[data-fp-background]').textContent = '立创预览不可用：' + error.message + '；本地操作仍可使用';
        }
      })();
    }
    if (!options.mount) q('[data-fp-close]').focus();
  });
};

window.openFootprintManager = function openFootprintManager(options) {
  return new Promise(resolve => {
    const overlay = document.createElement('div');
    overlay.className = 'fp-overlay';
    overlay.innerHTML = `<section class="fp-dialog fp-workflow" role="dialog" aria-modal="true" aria-label="封装管理">
      <header class="fp-title"><strong>封装管理</strong><button type="button" data-fp-close title="关闭" aria-label="关闭"><svg aria-hidden="true"><use href="#toolbar-icon-x"></use></svg></button></header>
      <nav class="fp-tabs" role="tablist" aria-label="封装操作"><button type="button" role="tab" data-fp-tab="library">替换库封装</button><button type="button" role="tab" data-fp-tab="convert">替换立创封装</button><button type="button" role="tab" data-fp-tab="rename">库封装改名</button></nav>
      <nav class="fp-history" aria-label="封装页面历史"><button type="button" data-fp-back title="返回上一页" aria-label="返回上一页" disabled><svg aria-hidden="true"><use href="#toolbar-icon-arrow-left"></use></svg></button><button type="button" data-fp-forward class="fp-forward" title="前进" aria-label="前进" disabled><svg aria-hidden="true"><use href="#toolbar-icon-arrow-left"></use></svg></button></nav>
      <div class="fp-pages"></div></section>`;
    const q = selector => overlay.querySelector(selector), previousFocus = document.activeElement;
    if (document.documentElement.classList.contains('native-utility-page')) {
      overlay.classList.add('fp-native-workflow');
      q('.fp-title').hidden = true;
    }
    const controller = new AbortController(), pages = new Map();
    let closed = false, history = [options.initialMode || 'library'], index = 0;
    function close(value) {
      if (closed) return;
      closed = true;
      controller.abort();
      overlay.remove();
      document.removeEventListener('keydown', keyboard, true);
      if (previousFocus?.isConnected) previousFocus.focus();
      resolve(value);
    }
    function load(mode, retry = false) {
      if (pages.has(mode) && !retry) return;
      const page = pages.get(mode) || document.createElement('div');
      page.className = 'fp-page';
      page.dataset.fpPage = mode;
      page.hidden = history[index] !== mode;
      pages.set(mode, page);
      if (!page.isConnected) q('.fp-pages').append(page);
      page.innerHTML = '<div class="fp-loading" role="status">正在读取封装资料…</div>';
      void (async () => {
        try {
          const preview = await options.load(mode);
          if (closed) return;
          if (mode === 'library' && preview.c_code) load('convert');
          page.replaceChildren();
          const choice = await (mode === 'convert' ? window.chooseFootprint(preview, {mount:page, signal:controller.signal})
            : window.chooseLibraryFootprint(preview, {mount:page, signal:controller.signal}));
          if (!closed) close(choice);
        } catch (error) {
          if (closed) return;
          page.replaceChildren();
          const status = document.createElement('div');
          status.className = 'fp-loading fp-load-error'; status.role = 'status'; status.textContent = error.message;
          const retryButton = document.createElement('button');
          retryButton.type = 'button'; retryButton.textContent = '重试';
          retryButton.onclick = () => load(mode, true);
          status.append(document.createElement('br'), retryButton);
          page.append(status);
        }
      })();
    }
    function show(mode, push = true) {
      if (push && mode !== history[index]) { history = history.slice(0, index + 1); history.push(mode); index++; }
      overlay.querySelectorAll('[data-fp-tab]').forEach(button => button.setAttribute('aria-selected', String(button.dataset.fpTab === mode)));
      q('.fp-workflow').classList.toggle('fp-rename-dialog', mode === 'rename');
      q('.fp-workflow > .fp-title strong').textContent = mode === 'rename' ? '库封装改名' : '封装管理';
      pages.forEach((page, key) => { page.hidden = key !== mode; });
      q('[data-fp-back]').disabled = index === 0;
      q('[data-fp-forward]').disabled = index === history.length - 1;
      load(mode);
    }
    function keyboard(event) {
      if (!ownsFootprintKeyboard(event, overlay)) return;
      if (event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); close(null); }
      if (event.altKey && event.key === 'ArrowLeft') { event.preventDefault(); q('[data-fp-back]').click(); }
      if (event.altKey && event.key === 'ArrowRight') { event.preventDefault(); q('[data-fp-forward]').click(); }
      if (event.key === 'Tab') {
        const elements = [...overlay.querySelectorAll('button,input,select')].filter(e => !e.disabled && e.getClientRects().length);
        const first = elements[0], last = elements.at(-1);
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }
    }
    q('[data-fp-back]').onclick = () => { if (index > 0) show(history[--index], false); };
    q('[data-fp-forward]').onclick = () => { if (index < history.length - 1) show(history[++index], false); };
    q('[data-fp-close]').onclick = () => close(null);
    overlay.querySelectorAll('[data-fp-tab]').forEach(button => { button.onclick = () => show(button.dataset.fpTab); });
    document.body.append(overlay);
    document.addEventListener('keydown', keyboard, true);
    show(history[0], false);
    q('[data-fp-close]').focus();
  });
};

window.manageRowFootprint = async function manageRowFootprint(row) {
  if (window.rowFootprintBusy) return {finished:false, error:'封装窗口仍在处理上一项操作，请稍后重试'};
  window.rowFootprintBusy = true;
  window.rowFootprintSaving = false;
  window.cancelRowFootprintPreview = () => {
    const overlay = [...document.querySelectorAll('.fp-overlay')].filter(el => el.getClientRects().length).at(-1);
    const close = overlay?.querySelector('[data-fp-close]');
    if (!close) return false;
    close.click();
    return true;
  };
  let saving = false;
  let anchor, expectedBom;
  const request = async (url, payload) => {
    const response = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || '封装操作失败');
    return data;
  };
  try {
    let closed = false;
    let initial = null;
    const loadInitial = () => initial ||= (async () => {
      const tr = typeof row === 'function' ? await row() : row;
      anchor = rowAnchor(tr); expectedBom = loadedRowsBom;
      await flushRowEdits();
      if (closed) throw new Error('封装窗口已关闭');
      const current = [...rowsEl.querySelectorAll('tr[data-refs]')].find(row => row.dataset.refs === anchor.refs);
      if (!current || expectedBom !== loadedRowsBom) throw new Error('行分组或数据源已变化，请重新打开封装管理');
      return request('/api/row_footprint', {refs:current.dataset.refs, bom:expectedBom, mode:'library'});
    })().catch(error => { initial = null; throw error; });
    const load = async mode => {
      const first = await loadInitial(), parent = first.preview.preview_id;
      if (closed) throw new Error('封装窗口已关闭');
      if (mode === 'library') return first.preview;
      if (mode === 'rename') return (await request('/api/row_footprint', {parent_preview:parent, mode:'rename'})).preview;
      let data = await request('/api/start_row_footprint', {parent_preview:parent, mode:'convert'});
      const id = data.job_id;
      while (!data.done) {
        if (closed) throw new Error('封装窗口已关闭');
        await new Promise(resolve => setTimeout(resolve, 300));
        const response = await fetch('/api/selection_job?id=' + encodeURIComponent(id));
        data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || '封装转换失败');
      }
      return data.result.preview;
    };
    let choice;
    try { choice = await window.openFootprintManager({load}); } finally { closed = true; }
    if (!choice) return {finished:true};
    saving = true;
    window.rowFootprintSaving = true;
    while (choice) {
      const data = await request('/api/row_footprint', {footprint_choice:choice});
      if (data.footprint_conflict) {
        choice = await window.resolveFootprintConflict(data.footprint_conflict, choice);
        continue;
      }
      await loadRows(anchor);
      if (typeof toolOnly !== 'undefined' && toolOnly) await window.notifyNativeToolChanged();
      setState('封装修改已保存，请同步原理图；C 编码、Value、Comment 和物料参数未改动', 'saved');
      break;
    }
    return {finished:true};
  } catch (error) {
    setState((saving ? '封装保存失败：' : '封装预览失败：') + error.message, 'error');
    return {finished:false, error:(saving ? '封装保存失败：' : '封装预览失败：') + error.message};
  } finally {
    window.rowFootprintSaving = false;
    window.rowFootprintBusy = false;
    delete window.cancelRowFootprintPreview;
  }
};
