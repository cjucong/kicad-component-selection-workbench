/* Real KiCad previews are loaded as images, never inserted as executable SVG markup. */
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

window.chooseLibraryFootprint = function chooseLibraryFootprint(preview) {
  return new Promise(resolve => {
    const rename = preview.kind === 'rename';
    const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
    const overlay = document.createElement('div');
    overlay.className = 'fp-overlay';
    overlay.innerHTML = `<section class="fp-dialog fp-library-dialog" role="dialog" aria-modal="true" aria-label="选择库封装">
      <header class="fp-title"><strong>选择库封装</strong><button type="button" data-fp-close title="取消并关闭" aria-label="关闭"><svg aria-hidden="true"><use href="#toolbar-icon-x"></use></svg></button></header>
      <div class="fp-body"><div class="fp-context">当前：${escape(preview.original_ids.map(id => preview.items.find(e => e.id === id)?.label || id).join(' / ') || '未设置')}</div>
      <div class="fp-library-search"><select data-fp-library aria-label="封装库"><option value="">全部工程库</option>${preview.libraries.map(n => `<option>${escape(n)}</option>`).join('')}</select>
      <input type="text" data-fp-search placeholder="搜索封装名称" aria-label="搜索封装名称"></div>
      ${rename ? `<div class="fp-fields fp-rename-fields"><label>目标封装库<select data-fp-library aria-label="目标封装库">${preview.libraries.map(n => `<option ${n === preview.default_library ? 'selected' : ''}>${escape(n)}</option>`).join('')}</select></label><label>封装名称<input data-fp-name type="text" maxlength="120" value="${escape(preview.suggested_name)}"></label></div>` : ''}
      <select class="fp-library-list" data-fp-items size="8" aria-label="库封装列表"></select>
      <div class="fp-library-detail" data-fp-detail role="status"></div>
      <button type="button" class="fp-optional-preview" data-fp-preview title="查看所选封装预览" disabled><svg aria-hidden="true"><use href="#toolbar-icon-search"></use></svg>预览</button>
      <div class="fp-library-image" hidden><img alt="所选库封装预览"></div>
      <p class="fp-note">${rename ? '仅按铜皮焊盘编号、局部坐标和方向判断可否共用；丝印不同将采用所选基准封装的版本。' : '焊盘编号一致不代表电气兼容，请核对尺寸和引脚功能。'}</p>
      <label class="fp-ack"><input type="checkbox" data-fp-ack>已核对尺寸、焊盘编号和引脚功能</label></div>
      <footer class="fp-footer"><span class="fp-error" role="status"></span><button type="button" data-fp-cancel>取消</button><button type="button" class="fp-apply" disabled>确认选择</button></footer></section>`;
    const q = selector => overlay.querySelector(selector);
    if (rename) {
      q('.fp-library-search select').removeAttribute('data-fp-library');
      q('.fp-library-search select').setAttribute('data-fp-filter-library', '');
      q('.fp-title strong').textContent = '改名并保留原封装';
    }
    const previousFocus = document.activeElement;
    let closed = false, sequence = 0, controller = null, selected = '', detail = null, imageUrl = null;
    function enabled() {
      const original = (preview.original_ids || []).includes(selected);
      q('.fp-apply').disabled = !q('[data-fp-ack]').checked || (rename
        ? !detail || !original || !q('[data-fp-name]').value.trim() || !q('[data-fp-library]').value
        : !detail?.compatible);
    }
    function clearImage() {
      q('.fp-library-image').hidden = true;
      q('.fp-library-image img').removeAttribute('src');
      if (imageUrl) URL.revokeObjectURL(imageUrl);
      imageUrl = null;
    }
    async function load(render = false) {
      const id = selected, token = ++sequence;
      controller?.abort();
      controller = new AbortController();
      q('.fp-error').textContent = '';
      q('[data-fp-preview]').disabled = true;
      q('.fp-apply').disabled = true;
      if (!render) {
        detail = null;
        q('[data-fp-ack]').checked = false;
        clearImage();
        q('[data-fp-detail]').textContent = id ? '正在读取焊盘编号…' : '没有匹配的封装';
      }
      if (!id) return;
      try {
        const response = await fetch('/api/library_footprint', {method:'POST', headers:{'Content-Type':'application/json'},
          body:JSON.stringify({preview_id:preview.preview_id, id, render}), signal:controller.signal});
        const data = await response.json();
        if (closed || token !== sequence) return;
        if (!response.ok || !data.ok) throw new Error(data.error || '无法读取库封装');
        detail = data.detail;
        q('[data-fp-detail]').textContent = `${detail.id}\n焊盘 ${detail.pads} 个 · 编号 ${(detail.numbers || []).join(', ') || '无'}\n单焊盘 ${detail.pad_size || '—'} · 包络 ${detail.pad_envelope || '—'}${rename ? '' : '\n' + (detail.compatible ? '编号与原封装一致' : '原封装不可读取或焊盘编号不一致，不能替换')}`;
        if (render) {
          clearImage();
          imageUrl = URL.createObjectURL(new Blob([detail.svg], {type:'image/svg+xml'}));
          q('.fp-library-image img').src = imageUrl;
          q('.fp-library-image').hidden = false;
        }
        q('[data-fp-preview]').disabled = false;
        enabled();
      } catch (error) {
        if (closed || token !== sequence || error.name === 'AbortError') return;
        q('.fp-error').textContent = error.message || String(error);
        if (render && detail) { q('[data-fp-preview]').disabled = false; enabled(); }
      }
    }
    function filter() {
      const library = q(rename ? '[data-fp-filter-library]' : '[data-fp-library]').value, query = q('[data-fp-search]').value.trim().toLowerCase();
      const items = preview.items.filter(e => (!rename || (preview.original_ids || []).includes(e.id)) && (!library || e.library === library) && e.id.toLowerCase().includes(query));
      const next = items.some(e => e.id === selected) ? selected : (items[0]?.id || '');
      q('[data-fp-items]').innerHTML = items.map(e => `<option value="${escape(e.id)}">${escape(e.label || e.id)}</option>`).join('');
      q('[data-fp-items]').value = next;
      if (next !== selected || !detail) { selected = next; load(); }
    }
    function close(value) {
      if (closed) return;
      closed = true;
      ++sequence;
      controller?.abort();
      document.removeEventListener('keydown', keyboard, true);
      clearImage();
      overlay.remove();
      if (previousFocus?.isConnected) previousFocus.focus();
      resolve(value);
    }
    function keyboard(event) {
      if (event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); close(null); }
      if (event.key === 'Tab') {
        const elements = [...overlay.querySelectorAll('button,input,select')].filter(e => !e.disabled && e.getClientRects().length);
        const first = elements[0], last = elements.at(-1);
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }
    }
    q('[data-fp-search]').oninput = q(rename ? '[data-fp-filter-library]' : '[data-fp-library]').onchange = filter;
    if (rename) {
      q('[data-fp-library]').onchange = enabled;
      q('[data-fp-name]').oninput = enabled;
    }
    q('[data-fp-items]').onchange = () => { selected = q('[data-fp-items]').value; load(); };
    q('[data-fp-preview]').onclick = () => load(true);
    q('[data-fp-ack]').onchange = enabled;
    q('[data-fp-close]').onclick = q('[data-fp-cancel]').onclick = () => close(null);
    q('.fp-apply').onclick = () => {
      if (!q('.fp-apply').disabled) close({preview_id:preview.preview_id, source:rename ? 'rename' : 'existing', existing_id:selected, acknowledged:true,
        ...(rename ? {target_mode:'existing', target_library:q('[data-fp-library]').value, name:q('[data-fp-name]').value.trim()} : {})});
    };
    document.body.append(overlay);
    document.addEventListener('keydown', keyboard, true);
    selected = preview.default_id;
    filter();
    q('[data-fp-search]').focus();
  });
};

window.chooseFootprint = function chooseFootprint(preview, options = {}) {
  return new Promise(resolve => {
    const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
    const originals = new Set(preview.original_ids || preview.current?.original_ids || []);
    const references = new Set(preview.alignment_ids || [...originals]);
    const renameIds = new Set(preview.rename_ids || [...originals]);
    const overlay = document.createElement('div');
    overlay.className = 'fp-overlay';
    overlay.innerHTML = `<section class="fp-dialog" role="dialog" aria-modal="true" aria-label="封装对比与目标库">
      <header class="fp-title"><strong>封装对比与目标库</strong><span>${escape(preview.c_code)}</span>
      <button type="button" data-fp-close title="取消并关闭" aria-label="关闭"><svg aria-hidden="true"><use href="#toolbar-icon-x"></use></svg></button></header>
      <div class="fp-body">${options.manage ? `<div class="fp-management"><button type="button" data-fp-pick-library>替换库封装…</button><button type="button" data-fp-rename>仅改名保留原几何…</button><span data-fp-background role="status">${preview.c_code ? '立创封装正在后台读取…' : '无唯一 C 编码，仅提供本地封装操作'}</span></div><div class="fp-context">当前绑定：${escape(preview.original_ids.join(' / ') || '未指定')}</div>` : ''}<div class="fp-baseline"><label>对齐基准<select data-fp-existing aria-label="工程现有封装">${preview.existing.map(e => `<option value="${escape(e.id)}">${escape(e.source === 'pcb' ? `PCB 已保存 · ${e.placement.ref} · ${e.placement.footprint}` : e.id)}</option>`).join('')}</select></label><span>mm</span></div>
      <div class="fp-reference-context" data-fp-reference-context></div>
      <div class="fp-comparison">
        <div class="fp-compare-toolbar">
          <div class="fp-segments" role="group" aria-label="比较视图"><label><input type="radio" name="fp-view" data-fp-view="overlay" value="overlay" checked><span>叠加</span></label><label><input type="radio" name="fp-view" data-fp-view="side" value="side"><span>并排</span></label></div>
          <label class="fp-opacity">立创不透明度<input type="range" data-fp-opacity min="0" max="100" value="50" aria-label="立创不透明度"><output data-fp-opacity-value>50%</output></label>
          <label class="fp-marker-toggle"><input type="checkbox" data-fp-markers checked>原点 / 1号焊盘</label>
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
    </section>`;
    const q = selector => overlay.querySelector(selector);
    const previousFocus = document.activeElement;
    const cache = new Map(), layerState = new Map(), urls = new Set();
    let closed = false, sequence = 0, controller = null, detail = null;
    let localOnly = options.manage === true;
    const selectedEntry = () => preview.existing.find(e => e.id === q('[data-fp-existing]').value);
    const source = () => q('[name=fp-source]:checked').value;
    const mode = () => q('[name=fp-mode]:checked').value;
    function refresh() {
      const entry = selectedEntry(), original = !!entry && originals.has(entry.id);
      const reference = !!entry && references.has(entry.id);
      q('[name=fp-source][value=existing]').disabled = !entry || entry.source === 'pcb';
      q('[name=fp-source][value=rename]').disabled = !entry || !renameIds.has(entry.id);
      q('[name=fp-source][value=converted]').disabled = localOnly || !reference;
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
          : localOnly || !reference || !preview.compatible || detail?.alignment?.ok !== true);
    }
    function clearImages() {
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
        fieldset.innerHTML = `<legend class="${side === 'existing' ? 'fp-local-color' : 'fp-converted-color'}">${side === 'existing' ? '本地' : '转换'}</legend>`;
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
      q('[data-fp-alignment]').textContent = value.local_only ? '当前绑定的本地库封装 · mm' : alignment.ok === true
        ? `中心已对齐 · 校正 ${format(alignment.rotation_deg)}° · 焊盘中心最大偏差 ${format(alignment.max_error_mm, 4)} mm${alignment.requires_review ? ' · 间距有差异，请核对' : ''}`
        : '无法对齐，请检查焊盘编号与原封装';
      q('[data-fp-alignment]').dataset.review = String(alignment.requires_review === true);
      if (value.local_only) q('[data-fp-alignment]').removeAttribute('data-ok');
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
      q('.fp-error').textContent = '';
      clearImages();
      q('[data-fp-alignment]').textContent = '';
      const status = q('[data-fp-canvas-status]');
      status.hidden = false;
      status.textContent = entry && references.has(entry.id) ? '正在读取预览…' : '请选择编号匹配的对齐基准';
      refresh();
      if (!entry || !references.has(entry.id)) return;
      try {
        if (cache.has(entry.id)) { displayComparison(cache.get(entry.id)); return; }
        const response = await fetch('/api/footprint_comparison', {method:'POST', headers:{'Content-Type':'application/json'},
          body:JSON.stringify({preview_id:preview.preview_id, id:entry.id}), signal:controller.signal});
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
      document.removeEventListener('keydown', keyboard, true);
      overlay.remove();
      if (previousFocus?.isConnected) previousFocus.focus();
      resolve(value);
    }
    function keyboard(event) {
      if (event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); close(null); }
      if (event.key === 'Tab') {
        const elements = [...overlay.querySelectorAll('button,input,select')].filter(e => !e.disabled && e.getClientRects().length);
        const first = elements[0], last = elements.at(-1);
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }
    }
    overlay.addEventListener('change', event => {
      if (event.target.matches('[data-fp-existing]')) {
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
      if (q('.fp-apply').disabled) return;
      close({preview_id: preview.preview_id, source: source(), existing_id: q('[data-fp-existing]').value,
        target_mode: mode(), target_library: mode() === 'existing' ? q('[data-fp-library]').value : q('[data-fp-new-library]').value.trim(),
        target_folder: q('[data-fp-folder]').value.trim(), name: q('[data-fp-name]').value.trim(),
        acknowledged: q('[data-fp-ack]').checked});
    };
    document.body.append(overlay);
    document.addEventListener('keydown', keyboard, true);
    const original = preview.existing.find(entry => entry.id === preview.default_reference_id)
      || preview.existing.find(entry => originals.has(entry.id));
    if (original) q('[data-fp-existing]').value = original.id;
    const initialLocal = loadComparison();
    if (options.loadConverted && preview.c_code) {
      void (async () => {
        try {
          const converted = await options.loadConverted();
          await initialLocal;
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
    q('[data-fp-close]').focus();
  });
};

window.manageRowFootprint = async function manageRowFootprint(tr) {
  if (window.rowFootprintBusy) return;
  window.rowFootprintBusy = true;
  let saving = false;
  const anchor = rowAnchor(tr), expectedBom = loadedRowsBom;
  const request = async (url, payload) => {
    const response = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || '封装操作失败');
    return data;
  };
  try {
    await flushRowEdits();
    const current = [...rowsEl.querySelectorAll('tr[data-refs]')].find(row => row.dataset.refs === anchor.refs);
    if (!current || expectedBom !== loadedRowsBom) throw new Error('行分组或数据源已变化，请重新打开封装管理');
    const initial = await request('/api/row_footprint', {refs:current.dataset.refs, bom:expectedBom, mode:'library'});
    const parent = initial.preview.preview_id;
    const converted = async () => {
      let data = await request('/api/start_row_footprint', {parent_preview:parent, mode:'convert'});
      const id = data.job_id;
      while (!data.done) {
        await new Promise(resolve => setTimeout(resolve, 300));
        const response = await fetch('/api/selection_job?id=' + encodeURIComponent(id));
        data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || '封装转换失败');
      }
      return data.result.preview;
    };
    let choice = await window.chooseFootprint(initial.preview, {manage:true, loadConverted:converted});
    if (choice?.operation) {
      const data = await request('/api/row_footprint', {parent_preview:parent, mode:choice.operation});
      choice = await window.chooseLibraryFootprint(data.preview);
    }
    if (!choice) return;
    saving = true;
    while (choice) {
      const data = await request('/api/row_footprint', {footprint_choice:choice});
      if (data.footprint_conflict) {
        choice = await window.resolveFootprintConflict(data.footprint_conflict, choice);
        continue;
      }
      await loadRows(anchor);
      setState('封装修改已保存，请同步原理图；C 编码、Value、Comment 和物料参数未改动', 'saved');
      break;
    }
  } catch (error) {
    setState((saving ? '封装保存失败：' : '封装预览失败：') + error.message, 'error');
  } finally { window.rowFootprintBusy = false; }
};
