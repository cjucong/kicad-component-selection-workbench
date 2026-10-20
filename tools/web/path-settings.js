/* Project-scoped settings. DOM text properties keep pasted paths out of HTML. */
(() => {
  const fields = [
    ['project_name', '工程名称', 'data'],
    ['schematic_dir', '原理图目录', 'data', 'folder'],
    ['bom_dir', 'BOM 导出与同步日志目录', 'data', 'folder'],
    ['output_overrides', '选料记录文件', 'data', 'file'],
    ['stock_cache', '库存与参数缓存文件', 'data', 'file'],
    ['input_refs', '位号清单文件', 'data', 'file'],
    ['source_bom', '默认数据源', 'data', 'file'],
    ['lcsc_bom_pattern', '立创 BOM 匹配规则', 'data'],
    ['converter_executable', 'EasyKiConverter 程序', 'runtime', 'file'],
    ['runtime_cache', '临时缓存父目录', 'runtime', 'folder'],
    ['desktop_home', '桌面运行环境与日志父目录', 'runtime', 'folder'],
  ];
  let snapshot;
  let opening = false;
  const dialog = document.createElement('dialog');
  dialog.id = 'pathSettingsDialog';
  dialog.className = 'path-settings-dialog';
  dialog.setAttribute('aria-labelledby', 'pathSettingsTitle');
  dialog.innerHTML = `
    <form id="pathSettingsForm">
      <header><h2 id="pathSettingsTitle">工程与路径配置</h2><button type="button" data-close-paths class="path-icon-button" aria-label="关闭路径配置" title="关闭">&times;</button></header>
      <div class="path-settings-body">
        <dl class="path-identity"><dt>当前工程</dt><dd id="pathProjectRoot"></dd><dt>配置文件</dt><dd id="pathConfigFile"></dd></dl>
        <div class="path-tabs" role="tablist" aria-label="配置类别">
          <button type="button" role="tab" id="pathDataTab" aria-controls="pathDataPanel" aria-selected="true" data-path-tab="data">工程数据</button>
          <button type="button" role="tab" id="pathRuntimeTab" aria-controls="pathRuntimePanel" aria-selected="false" data-path-tab="runtime">转换与运行</button>
        </div>
        <section id="pathDataPanel" role="tabpanel" aria-labelledby="pathDataTab" data-path-panel="data">
          <div class="path-settings-tools"><button type="button" id="pathDefaults">使用工程默认路径</button><button type="button" id="pathUnifyData">数据文件放入导出目录</button></div>
          <div id="pathDataFields" class="path-fields"></div>
          <label class="path-migration"><input type="checkbox" id="pathCopyExisting" checked>复制已有记录、缓存、导出表和同步日志到新位置，保留原文件</label>
        </section>
        <section id="pathRuntimePanel" role="tabpanel" aria-labelledby="pathRuntimeTab" data-path-panel="runtime" hidden>
          <div id="pathRuntimeFields" class="path-fields"></div>
          <dl class="path-identity"><dt>当前临时目录</dt><dd id="pathRuntimeEffective"></dd><dt>当前桌面目录</dt><dd id="pathDesktopEffective"></dd><dt>插件程序目录</dt><dd id="pathPluginDirectory"></dd></dl>
          <p class="path-settings-note">转换器留空时自动查找。临时目录和桌面目录留空时使用启动环境或默认位置；修改后完整退出助手并重启对应后台生效。临时缓存须在工程外且同盘。</p>
        </section>
        <p id="pathSettingsStatus" role="status" aria-live="polite"></p>
      </div>
      <footer><button type="button" data-close-paths>取消</button><button type="submit" id="savePathSettings">保存并应用</button></footer>
    </form>`;
  document.body.append(dialog);
  const form = dialog.querySelector('form');
  const status = dialog.querySelector('#pathSettingsStatus');
  const inputs = new Map();
  let saved = false;
  let saving = false;
  const notify = (message, failed = false) => {
    status.textContent = message;
    status.classList.toggle('path-error', failed);
  };
  const changed = () => fields.some(([key]) => inputs.get(key).value !== (snapshot?.values[key] || ''));
  async function api(url, options) {
    const response = await fetch(url, options);
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.error || '操作失败');
    return result;
  }
  const post = (url, body) => api(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  for (const [key, label, panel, kind] of fields) {
    const row = document.createElement('div');
    row.className = 'path-field';
    const title = document.createElement('label');
    title.htmlFor = `path-${key}`;
    title.textContent = label;
    const input = document.createElement('input');
    input.id = `path-${key}`;
    input.name = key;
    input.type = 'text';
    input.spellcheck = false;
    input.autocomplete = 'off';
    input.placeholder = key === 'source_bom' ? '__schematic__' : key === 'converter_executable' ? '自动查找' : '';
    const control = document.createElement('div');
    control.className = 'path-field-control';
    control.append(input);
    if (kind) {
      const browse = document.createElement('button');
      browse.type = 'button';
      browse.className = 'path-icon-button';
      browse.title = `选择${label}`;
      browse.setAttribute('aria-label', browse.title);
      browse.innerHTML = '<svg aria-hidden="true"><use href="#toolbar-icon-folder-open"></use></svg>';
      browse.addEventListener('click', async () => {
        browse.disabled = true;
        try {
          const result = await post('/api/path_picker', {field: key, value: input.value});
          if (result.path) input.value = result.path;
        } catch (error) { notify(error.message, true); }
        finally { browse.disabled = false; }
      });
      control.append(browse);
    }
    row.append(title, control);
    dialog.querySelector(panel === 'data' ? '#pathDataFields' : '#pathRuntimeFields').append(row);
    inputs.set(key, input);
  }
  function tab(name) {
    dialog.querySelectorAll('[data-path-tab]').forEach(button => button.setAttribute('aria-selected', String(button.dataset.pathTab === name)));
    dialog.querySelectorAll('[data-path-panel]').forEach(panel => { panel.hidden = panel.dataset.pathPanel !== name; });
  }
  dialog.querySelectorAll('[data-path-tab]').forEach(button => button.addEventListener('click', () => tab(button.dataset.pathTab)));
  dialog.querySelector('.path-tabs').addEventListener('keydown', event => {
    if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
    event.preventDefault();
    const name = dialog.querySelector('[data-path-tab][aria-selected="true"]').dataset.pathTab === 'data' ? 'runtime' : 'data';
    tab(name);
    dialog.querySelector(`[data-path-tab="${name}"]`).focus();
  });
  function close() {
    if (saving) return;
    if (!saved && changed() && !window.confirm('放弃尚未保存的路径配置？')) return;
    dialog.close();
    if (saved) window.location.reload();
  }
  dialog.addEventListener('cancel', event => { event.preventDefault(); close(); });
  dialog.querySelectorAll('[data-close-paths]').forEach(button => button.addEventListener('click', close));
  const join = (parent, name) => `${parent.replace(/[\\/]+$/, '')}/${name}`;
  function placeData(parent) {
    inputs.get('output_overrides').value = join(parent, 'part_selection_overrides.json');
    inputs.get('stock_cache').value = join(parent, 'lcsc_stock_cache.json');
    inputs.get('input_refs').value = join(parent, 'part_selection_input.txt');
  }
  dialog.querySelector('#pathUnifyData').addEventListener('click', () => placeData(inputs.get('bom_dir').value || 'BOM'));
  dialog.querySelector('#pathDefaults').addEventListener('click', () => {
    inputs.get('bom_dir').value = 'BOM';
    placeData('BOM');
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (saving) return;
    saving = true;
    const controls = [...form.querySelectorAll('input,button')];
    controls.forEach(control => { control.disabled = true; });
    try {
      const result = await post('/api/path_settings', {revision: snapshot.revision,
        values: Object.fromEntries([...inputs].map(([key, input]) => [key, input.value])),
        copy_existing: dialog.querySelector('#pathCopyExisting').checked});
      snapshot = result.settings;
      saved = true;
      // Prevent new edits until the workbench has reloaded with its new data paths.
      notify(`已保存并应用；复制 ${result.copied_files} 个文件。备份：${result.backup_path}${result.restart_required ? '。运行目录将在完整重启助手和对应后台后生效。' : ''}`);
      controls.filter(control => control.hasAttribute('data-close-paths')).forEach(control => {
        control.disabled = false;
        if (!control.classList.contains('path-icon-button')) control.textContent = '完成并刷新';
      });
    } catch (error) {
      notify(error.message, true);
      controls.forEach(control => { control.disabled = false; });
    } finally { saving = false; }
  });
  window.openPathSettings = async () => {
    if (opening || dialog.open) return;
    opening = true;
    try {
      snapshot = await api('/api/path_settings');
      saved = false;
      form.querySelectorAll('input,button').forEach(control => { control.disabled = false; });
      dialog.querySelector('footer [data-close-paths]').textContent = '取消';
      for (const [key, input] of inputs) input.value = snapshot.values[key] || '';
      for (const [id, key] of [['pathProjectRoot', 'project_root'], ['pathConfigFile', 'config_path'],
        ['pathRuntimeEffective', 'runtime_directory'], ['pathDesktopEffective', 'desktop_home_effective'], ['pathPluginDirectory', 'plugin_directory']]) {
        dialog.querySelector(`#${id}`).textContent = snapshot[key];
      }
      dialog.querySelector('#pathCopyExisting').checked = true;
      tab('data');
      notify('');
      dialog.showModal();
    } finally { opening = false; }
  };
})();
