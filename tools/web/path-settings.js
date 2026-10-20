/* Project-scoped settings; pasted paths never become HTML. */
(() => {
  const fields = [
    ['bom_dir', '工程选料数据目录', 'pathDataFields', 'folder'],
    ['source_bom', '导入 BOM 文件', 'pathImportFields', 'file'],
    ['converter_executable', 'EasyKiConverter 程序', 'pathConverterFields', 'file'],
    ['project_name', '工程名称', 'pathAdvancedFields'],
    ['schematic_dir', '原理图目录', 'pathAdvancedFields', 'folder'],
    ['output_overrides', '选料记录文件', 'pathAdvancedFields', 'file'],
    ['stock_cache', '库存与参数缓存文件', 'pathAdvancedFields', 'file'],
    ['input_refs', '位号清单文件', 'pathAdvancedFields', 'file'],
    ['lcsc_bom_pattern', '立创 BOM 匹配规则', 'pathAdvancedFields'],
    ['runtime_cache', '临时缓存父目录', 'pathAdvancedFields', 'folder'],
    ['desktop_home', '桌面环境与日志父目录', 'pathAdvancedFields', 'folder'],
    ['jump_cursor_behavior', '跳转后鼠标位置', 'pathNavigationFields', 'choice'],
  ];
  const dataFiles = {output_overrides: 'part_selection_overrides.json', stock_cache: 'lcsc_stock_cache.json', input_refs: 'part_selection_input.txt'};
  const inputs = new Map();
  let snapshot, initialValues, initialSource;
  let opening = false, saved = false, saving = false;
  const dialog = document.createElement('dialog');
  dialog.id = 'pathSettingsDialog';
  dialog.className = 'path-settings-dialog';
  dialog.setAttribute('aria-labelledby', 'pathSettingsTitle');
  dialog.innerHTML = `
    <form id="pathSettingsForm">
      <header><h2 id="pathSettingsTitle">工程设置</h2><button type="button" data-close-paths class="path-icon-button" aria-label="关闭路径配置" title="关闭">&times;</button></header>
      <div class="path-settings-body">
        <section class="path-section"><h3>当前工程</h3><div class="path-project-location"><span id="pathProjectRoot"></span><button type="button" class="path-icon-button" data-open-directory="project" aria-label="打开工程目录" title="打开工程目录"><svg aria-hidden="true"><use href="#toolbar-icon-folder-open"></use></svg></button></div></section>
        <fieldset class="path-section"><legend>数据来源</legend>
          <div class="path-choices"><label><input type="radio" name="path-source" id="pathSourceSchematic">原理图当前数据（默认）</label><label><input type="radio" name="path-source" id="pathSourceImport">导入 BOM 文件</label></div>
          <div id="pathImportRow" hidden><div id="pathImportFields" class="path-fields"></div><p class="path-settings-note">仅本次使用导入文件；下次启动仍读取已保存的原理图。</p></div>
        </fieldset>
        <fieldset class="path-section"><legend>工程选料数据</legend>
          <div class="path-choices"><label><input type="radio" name="path-data" id="pathDataDefault">工程目录 / BOM</label><label><input type="radio" name="path-data" id="pathDataCustom">自定义目录</label></div>
          <div id="pathDataFields" class="path-fields"></div>
          <p class="path-settings-note">保存选料记录、确认状态、备选料、订货信息、导出表及同步日志。修改目录时先确认复制，原文件保留。</p>
          <p id="pathCustomFilesNotice" class="path-settings-note" hidden>部分文件使用独立位置，已保留。可在高级路径设置中查看或统一。</p>
        </fieldset>
        <section class="path-section"><h3>封装转换</h3><div id="pathConverterFields" class="path-fields"></div><p class="path-settings-note" id="pathConverterStatus"></p></section>
        <section class="path-section"><h3>原理图跳转</h3><div id="pathNavigationFields" class="path-fields"></div></section>
        <details id="pathAdvanced"><summary>高级路径设置</summary>
          <div class="path-settings-tools"><button type="button" id="pathDefaults">恢复工程数据默认路径</button><button type="button" id="pathUnifyData">数据文件放入所选目录</button></div>
          <div id="pathAdvancedFields" class="path-fields"></div>
          <dl class="path-identity"><dt>配置文件</dt><dd id="pathConfigFile"></dd><dt>当前临时目录</dt><dd id="pathRuntimeEffective"></dd><dt>当前桌面目录</dt><dd id="pathDesktopEffective"></dd><dt>插件程序目录</dt><dd id="pathPluginDirectory"></dd></dl>
          <p class="path-settings-note">相对路径以当前工程为基准。运行路径留空时自动选择工程外位置；手动指定时须为工程外绝对路径，临时缓存还须与工程同盘。修改运行目录后需重启工作台，不会移动原理图、PCB 或工程库。</p>
        </details>
        <p id="pathSettingsStatus" role="status" aria-live="polite"></p>
      </div>
      <footer><button type="button" id="pathRestart" hidden>重启工作台</button><button type="button" data-close-paths>取消</button><button type="submit" id="savePathSettings">保存并应用</button></footer>
    </form>`;
  document.body.append(dialog);
  const form = dialog.querySelector('form');
  const status = dialog.querySelector('#pathSettingsStatus');
  const schematic = dialog.querySelector('#pathSourceSchematic');
  const imported = dialog.querySelector('#pathSourceImport');
  const dataDefault = dialog.querySelector('#pathDataDefault');
  const dataCustom = dialog.querySelector('#pathDataCustom');
  const notify = (message, failed = false) => {
    status.textContent = message;
    status.classList.toggle('path-error', failed);
  };
  const source = () => imported.checked ? 'import' : 'schematic';
  const changed = () => initialValues && (source() !== initialSource || fields.some(([key]) => inputs.get(key).value !== initialValues[key]));
  window.hasUnsavedPathSettings = () => dialog.open && !saved && Boolean(changed());
  async function api(url, options) {
    const response = await fetch(url, options);
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.error || '操作失败');
    return result;
  }
  const post = (url, body) => api(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const join = (parent, name) => parent.replace(/[\\/]+$/, '') + '/' + name;
  const normalized = value => {
    let path = value.trim().replace(/\\/g, '/').replace(/\/$/, '');
    if (!/^(?:[a-z]:\/|\/)/i.test(path)) path = join(snapshot.project_root.replace(/\\/g, '/'), path);
    return path.replace(/\/\.\//g, '/').replace(/\/+/g, '/').toLowerCase();
  };
  function updateDataState() {
    const parent = inputs.get('bom_dir').value || 'BOM';
    dialog.querySelector('#pathCustomFilesNotice').hidden = !Object.entries(dataFiles).some(([key, name]) => normalized(inputs.get(key).value) !== normalized(join(parent, name)));
  }
  function placeData(parent) {
    for (const [key, name] of Object.entries(dataFiles)) inputs.get(key).value = join(parent, name);
    updateDataState();
  }
  function updateSource() { dialog.querySelector('#pathImportRow').hidden = !imported.checked; }
  function updateDirectory() {
    dataDefault.checked = normalized(inputs.get('bom_dir').value) === normalized('BOM');
    dataCustom.checked = !dataDefault.checked;
    updateDataState();
  }
  for (const [key, label, panel, kind] of fields) {
    const row = document.createElement('div');
    row.className = 'path-field';
    const title = document.createElement('label');
    title.htmlFor = 'path-' + key;
    title.textContent = label;
    const input = document.createElement(kind === 'choice' ? 'select' : 'input');
    if (kind === 'choice') {
      input.innerHTML = '<option value="return">返回原来的鼠标位置（默认）</option><option value="stay">留在原理图画布</option>';
    }
    Object.assign(input, {id: 'path-' + key, name: key, spellcheck: false, autocomplete: 'off'});
    input.placeholder = key === 'converter_executable' ? '自动检测（可留空）' : ['runtime_cache', 'desktop_home'].includes(key) ? '自动选择工程外目录' : '';
    const control = document.createElement('div');
    control.className = 'path-field-control';
    control.append(input);
    if (kind && kind !== 'choice') {
      const browse = document.createElement('button');
      Object.assign(browse, {type: 'button', className: 'path-icon-button', title: '选择' + label});
      browse.setAttribute('aria-label', browse.title);
      browse.innerHTML = '<svg aria-hidden="true"><use href="#toolbar-icon-folder-open"></use></svg>';
      browse.addEventListener('click', async () => {
        browse.disabled = true;
        try {
          const result = await post('/api/path_picker', {field: key, value: input.value});
          if (result.path) { input.value = result.path; input.dispatchEvent(new Event('input', {bubbles: true})); }
        } catch (error) { notify(error.message, true); }
        finally { browse.disabled = false; }
      });
      control.append(browse);
    }
    row.append(title, control);
    dialog.querySelector('#' + panel).append(row);
    inputs.set(key, input);
  }
  inputs.get('bom_dir').addEventListener('input', () => { placeData(inputs.get('bom_dir').value || 'BOM'); updateDirectory(); });
  for (const key of Object.keys(dataFiles)) inputs.get(key).addEventListener('input', updateDataState);
  for (const radio of [schematic, imported]) radio.addEventListener('change', updateSource);
  dataDefault.addEventListener('change', () => { inputs.get('bom_dir').value = 'BOM'; placeData('BOM'); });
  dataCustom.addEventListener('change', () => inputs.get('bom_dir').focus());
  dialog.querySelector('#pathUnifyData').addEventListener('click', () => placeData(inputs.get('bom_dir').value || 'BOM'));
  dialog.querySelector('#pathDefaults').addEventListener('click', () => { inputs.get('bom_dir').value = 'BOM'; placeData('BOM'); updateDirectory(); });
  dialog.querySelector('[data-open-directory]').addEventListener('click', async event => {
    const button = event.currentTarget;
    button.disabled = true;
    try { await post('/api/open_settings_directory', {target: button.dataset.openDirectory}); }
    catch (error) { notify(error.message, true); }
    finally { button.disabled = false; }
  });
  function close() {
    if (saving) return;
    if (!saved && changed() && !window.confirm('放弃尚未保存的工程设置？')) return;
    dialog.close();
    if (saved) window.location.reload();
  }
  dialog.addEventListener('cancel', event => { event.preventDefault(); close(); });
  dialog.querySelectorAll('[data-close-paths]').forEach(button => button.addEventListener('click', close));
  dialog.querySelector('#pathRestart').addEventListener('click', () => window.requestDesktopExit(true));
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (saving || saved) return;
    const values = Object.fromEntries([...inputs].filter(([key, input]) => key !== 'source_bom' && input.value !== initialValues[key]).map(([key, input]) => [key, input.value]));
    if (imported.checked) {
      if (!inputs.get('source_bom').value.trim() || inputs.get('source_bom').value.trim() === '__schematic__') { notify('请选择需要导入的 BOM 文件', true); inputs.get('source_bom').focus(); return; }
      values.source_bom = inputs.get('source_bom').value.trim();
    }
    const moving = ['bom_dir', ...Object.keys(dataFiles)].some(key => normalized(inputs.get(key).value) !== normalized(initialValues[key]));
    if (moving && !window.confirm('将复制已有选料记录、缓存、位号清单、导出表及同步日志到新位置；原文件保留，目标有不同数据时停止。继续保存？')) return;
    saving = true;
    const controls = [...form.querySelectorAll('input,select,button')];
    controls.forEach(control => { control.disabled = true; });
    try {
      await flushRowEdits();
      const result = await post('/api/path_settings', {revision: snapshot.revision, values, copy_existing: moving});
      snapshot = result.settings;
      saved = true;
      notify('已保存并应用；复制 ' + result.copied_files + ' 个文件。备份：' + result.backup_path + (result.restart_required ? '。运行目录需重启工作台后生效。' : ''));
      try { sessionStorage.setItem('partSelectionSourceOnce', JSON.stringify({project: snapshot.project_root, source: imported.checked ? snapshot.import_source : '__schematic__'})); }
      catch (_) { notify('工程设置已保存，但无法记住本次导入；刷新后请在数据源中手动选择 BOM。', true); }
      controls.filter(control => control.hasAttribute('data-close-paths')).forEach(control => { control.disabled = false; if (!control.classList.contains('path-icon-button')) control.textContent = '完成并刷新'; });
      const restart = dialog.querySelector('#pathRestart');
      restart.hidden = !result.restart_required;
      restart.disabled = false;
    } catch (error) { notify(error.message, true); controls.forEach(control => { control.disabled = false; }); }
    finally { saving = false; }
  });
  window.openPathSettings = async () => {
    if (opening || dialog.open) return;
    opening = true;
    try {
      snapshot = await api('/api/path_settings');
      saved = false;
      form.querySelectorAll('input,select,button').forEach(control => { control.disabled = false; });
      dialog.querySelector('footer [data-close-paths]').textContent = '取消';
      dialog.querySelector('#pathRestart').hidden = true;
      dialog.querySelector('#pathAdvanced').open = false;
      for (const [key, input] of inputs) input.value = snapshot.values[key] || '';
      if (inputs.get('source_bom').value === '__schematic__') inputs.get('source_bom').value = '';
      initialSource = document.querySelector('#bomSelect').value === '__schematic__' ? 'schematic' : 'import';
      schematic.checked = initialSource === 'schematic'; imported.checked = !schematic.checked;
      if (imported.checked) inputs.get('source_bom').value = document.querySelector('#bomSelect').value;
      initialValues = Object.fromEntries([...inputs].map(([key, input]) => [key, input.value]));
      for (const [id, key] of [['pathProjectRoot', 'project_root'], ['pathConfigFile', 'config_path'], ['pathRuntimeEffective', 'runtime_directory'], ['pathDesktopEffective', 'desktop_home_effective'], ['pathPluginDirectory', 'plugin_directory']]) dialog.querySelector('#' + id).textContent = snapshot[key];
      dialog.querySelector('#pathConverterStatus').textContent = snapshot.converter_path ? '已检测到：' + snapshot.converter_path : '未检测到转换器；可手动选择 easykiconverter.exe。';
      updateDirectory(); updateSource(); notify(''); dialog.showModal();
    } finally { opening = false; }
  };
})();
