/* Real KiCad previews are loaded as images, never inserted as executable SVG markup. */
window.chooseLibraryFootprint = function chooseLibraryFootprint(preview) {
  return new Promise(resolve => {
    const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
    const overlay = document.createElement('div');
    overlay.className = 'fp-overlay';
    overlay.innerHTML = `<section class="fp-dialog fp-library-dialog" role="dialog" aria-modal="true" aria-label="选择库封装">
      <header class="fp-title"><strong>选择库封装</strong><button type="button" data-fp-close title="取消并关闭" aria-label="关闭"><svg aria-hidden="true"><use href="#toolbar-icon-x"></use></svg></button></header>
      <div class="fp-body"><div class="fp-context">当前：${escape(preview.original_ids.join(' / ') || '未设置')}</div>
      <div class="fp-library-search"><select data-fp-library aria-label="封装库"><option value="">全部工程库</option>${preview.libraries.map(n => `<option>${escape(n)}</option>`).join('')}</select>
      <input type="text" data-fp-search placeholder="搜索封装名称" aria-label="搜索封装名称"></div>
      <select class="fp-library-list" data-fp-items size="8" aria-label="库封装列表"></select>
      <div class="fp-library-detail" data-fp-detail role="status"></div>
      <button type="button" class="fp-optional-preview" data-fp-preview title="查看所选封装预览" disabled><svg aria-hidden="true"><use href="#toolbar-icon-search"></use></svg>预览</button>
      <div class="fp-library-image" hidden><img alt="所选库封装预览"></div>
      <p class="fp-note">焊盘编号一致不代表电气兼容，请核对尺寸和引脚功能。</p>
      <label class="fp-ack"><input type="checkbox" data-fp-ack>已核对尺寸、焊盘编号和引脚功能</label></div>
      <footer class="fp-footer"><span class="fp-error" role="status"></span><button type="button" data-fp-cancel>取消</button><button type="button" class="fp-apply" disabled>确认选择</button></footer></section>`;
    const q = selector => overlay.querySelector(selector);
    const previousFocus = document.activeElement;
    let closed = false, sequence = 0, controller = null, selected = '', detail = null, imageUrl = null;
    function enabled() {
      q('.fp-apply').disabled = !detail?.compatible || !q('[data-fp-ack]').checked;
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
        q('[data-fp-detail]').textContent = `${detail.id}\n焊盘 ${detail.pads} 个 · 编号 ${detail.numbers.join(', ') || '无'}\n单焊盘 ${detail.pad_size || '—'} · 包络 ${detail.pad_envelope || '—'}\n${detail.compatible ? '编号与原封装一致' : '原封装不可读取或焊盘编号不一致，不能替换'}`;
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
      const library = q('[data-fp-library]').value, query = q('[data-fp-search]').value.trim().toLowerCase();
      const items = preview.items.filter(e => (!library || e.library === library) && e.id.toLowerCase().includes(query));
      const next = items.some(e => e.id === selected) ? selected : (items[0]?.id || '');
      q('[data-fp-items]').innerHTML = items.map(e => `<option value="${escape(e.id)}">${escape(e.id)}</option>`).join('');
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
    q('[data-fp-search]').oninput = q('[data-fp-library]').onchange = filter;
    q('[data-fp-items]').onchange = () => { selected = q('[data-fp-items]').value; load(); };
    q('[data-fp-preview]').onclick = () => load(true);
    q('[data-fp-ack]').onchange = enabled;
    q('[data-fp-close]').onclick = q('[data-fp-cancel]').onclick = () => close(null);
    q('.fp-apply').onclick = () => {
      if (!q('.fp-apply').disabled) close({preview_id:preview.preview_id, source:'existing', existing_id:selected, acknowledged:true});
    };
    document.body.append(overlay);
    document.addEventListener('keydown', keyboard, true);
    selected = preview.default_id;
    filter();
    q('[data-fp-search]').focus();
  });
};

window.chooseFootprint = function chooseFootprint(preview) {
  return new Promise(resolve => {
    const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
    const overlay = document.createElement('div');
    overlay.className = 'fp-overlay';
    overlay.innerHTML = `<section class="fp-dialog" role="dialog" aria-modal="true" aria-label="封装对比与目标库">
      <header class="fp-title"><strong>封装对比与目标库</strong><span>${escape(preview.c_code)}</span>
      <button type="button" data-fp-close title="取消并关闭" aria-label="关闭"><svg aria-hidden="true"><use href="#toolbar-icon-x"></use></svg></button></header>
      <div class="fp-body"><div class="fp-context"><span>当前工程库与立创转换结果</span><span>独立适配显示 · 单位 mm</span></div>
      <div class="fp-pair">
        <section class="fp-side"><h3>现有封装<select data-fp-existing aria-label="工程现有封装">${preview.existing.map(e => `<option value="${escape(e.id)}">${escape(e.id)}</option>`).join('')}</select></h3>
        <div class="fp-preview"><span class="fp-badge">KiCad 工程库</span><img data-fp-old alt="现有封装预览"><div class="fp-metrics" data-fp-old-metrics></div></div>
        <label class="fp-source"><input type="radio" name="fp-source" value="existing">使用现有封装</label></section>
        <section class="fp-side"><h3>立创转换封装<span>${escape(preview.suggested_name)}</span></h3>
        <div class="fp-preview"><span class="fp-badge">EasyKiConverter · KiCad 原生渲染</span><img data-fp-new alt="立创转换封装预览"><div class="fp-metrics" data-fp-new-metrics></div></div>
        <label class="fp-source"><input type="radio" name="fp-source" value="converted" checked>使用转换封装</label></section>
      </div>
      <div class="fp-destination"><legend>目标封装库</legend><div class="fp-modes">
        <label><input type="radio" name="fp-mode" value="existing" checked>合并到现有库</label>
        <label><input type="radio" name="fp-mode" value="new">新建封装库</label>
        <label><input type="radio" name="fp-mode" value="independent">独立转换库</label></div>
        <div class="fp-fields">
          <label data-fp-library-label>工程封装库<select data-fp-library>${preview.libraries.filter(n => !n.startsWith('LCSC_')).map(n => `<option ${n === preview.default_library ? 'selected' : ''}>${escape(n)}</option>`).join('')}</select></label>
          <label data-fp-new-label hidden>新库名称<input type="text" data-fp-new-library value="SelectedParts" maxlength="120"></label>
          <label>封装名称<input type="text" data-fp-name value="${escape(preview.suggested_name)}" maxlength="120"></label>
          <label data-fp-folder-label hidden>工程内目录<input type="text" data-fp-folder value="Libraries/DSMU"></label>
        </div><p class="fp-final" data-fp-final></p>
        <p class="fp-note">同名同几何会复用；同名不同几何自动追加 C 编码，保留原封装。</p>
        <p class="fp-note">${escape(preview.warning || '焊盘编号一致不代表电气兼容；请核对数据手册、引脚功能与尺寸。')}</p>
        <label class="fp-ack"><input type="checkbox" data-fp-ack>已核对尺寸、编号与引脚功能，并保存关闭库编辑器</label>
      </div></div>
      <footer class="fp-footer"><span class="fp-error" role="status"></span><button type="button" data-fp-cancel>取消</button><button type="button" class="fp-apply">确认选择</button></footer>
    </section>`;
    const q = selector => overlay.querySelector(selector);
    const previousFocus = document.activeElement;
    const urls = [];
    function imageUrl(svg) {
      const url = URL.createObjectURL(new Blob([svg], {type:'image/svg+xml'}));
      urls.push(url);
      return url;
    }
    function metrics(info) {
      return `<span>焊盘 ${escape(info.pads ?? '—')} 个</span><span>单焊盘 ${escape(info.pad_size || '—')}</span><span>编号 ${escape((info.numbers || []).join(', ') || '—')}</span><span>焊盘包络 ${escape(info.pad_envelope || '—')}</span>`;
    }
    q('[data-fp-new]').src = imageUrl(preview.converted_svg);
    q('[data-fp-new-metrics]').innerHTML = metrics(preview.converted_info);
    const oldUrls = new Map(preview.existing.map(e => [e.id, imageUrl(e.svg)]));
    function source() { return q('[name=fp-source]:checked').value; }
    function mode() { return q('[name=fp-mode]:checked').value; }
    function refresh() {
      const entry = preview.existing.find(e => e.id === q('[data-fp-existing]').value);
      if (entry) {
        q('[data-fp-old]').src = oldUrls.get(entry.id);
        q('[data-fp-old-metrics]').innerHTML = metrics(entry);
      } else {
        q('[data-fp-old]').removeAttribute('src');
        q('[data-fp-old-metrics]').textContent = '无法读取现有工程封装';
      }
      q('[name=fp-source][value=existing]').disabled = !entry;
      const converted = source() === 'converted';
      q('.fp-destination').hidden = !converted;
      q('[data-fp-library-label]').hidden = mode() !== 'existing';
      q('[data-fp-new-label]').hidden = mode() !== 'new';
      q('[data-fp-folder-label]').hidden = mode() !== 'new';
      const name = q('[data-fp-name]').value.trim();
      const lib = mode() === 'existing' ? q('[data-fp-library]').value : mode() === 'new' ? q('[data-fp-new-library]').value.trim() : 'LCSC_' + preview.c_code + '_Selected';
      const conflict = preview.existing.find(e => e.id === lib + ':' + name && e.geometry_hash !== preview.converted_info.geometry_hash);
      q('[data-fp-final]').textContent = '目标：' + lib + ':' + name + (conflict ? '_' + preview.c_code : '');
      q('.fp-apply').disabled = converted ? !preview.compatible || !q('[data-fp-ack]').checked || !name || !lib : !entry;
    }
    function close(value) {
      document.removeEventListener('keydown', keyboard, true);
      overlay.remove();
      urls.forEach(url => URL.revokeObjectURL(url));
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
    overlay.addEventListener('change', refresh);
    overlay.addEventListener('input', refresh);
    overlay.querySelectorAll('.fp-preview img').forEach(img => {
      img.title = '放大 / 还原封装预览';
      img.onclick = () => img.parentElement.classList.toggle('fp-zoomed');
    });
    q('[data-fp-close]').onclick = q('[data-fp-cancel]').onclick = () => close(null);
    q('.fp-apply').onclick = () => {
      if (q('.fp-apply').disabled) return;
      close({preview_id: preview.preview_id, source: source(), existing_id: q('[data-fp-existing]').value,
        target_mode: mode(), target_library: mode() === 'existing' ? q('[data-fp-library]').value : q('[data-fp-new-library]').value.trim(),
        target_folder: q('[data-fp-folder]').value.trim(), name: q('[data-fp-name]').value.trim(),
        acknowledged: q('[data-fp-ack]').checked});
    };
    document.body.append(overlay);
    document.addEventListener('keydown', keyboard, true);
    refresh();
    q('[data-fp-close]').focus();
  });
};
