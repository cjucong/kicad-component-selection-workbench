/* Only the owned native host can download or install releases. */
(() => {
  const dialog = document.createElement('dialog');
  dialog.id = 'pluginUpdatesDialog';
  dialog.className = 'path-settings-dialog';
  dialog.setAttribute('aria-labelledby', 'pluginUpdatesTitle');
  dialog.innerHTML = '<header><h2 id="pluginUpdatesTitle">插件更新</h2><button type="button" class="path-icon-button" data-close-update aria-label="关闭插件更新" title="关闭">&times;</button></header>' +
    '<div class="path-settings-body"><p id="pluginVersion"></p>' +
    '<div class="path-field"><label for="pluginUpdateMode">更新方式</label><div class="path-field-control"><select id="pluginUpdateMode" style="width:100%;min-height:30px"><option value="off">手动检查</option><option value="check">启动时检查并提醒（默认）</option><option value="auto">自动下载，退出工作台后更新</option></select></div></div>' +
    '<p id="pluginUpdateStatus" role="status" aria-live="polite"></p><pre id="pluginReleaseNotes" style="white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;max-height:220px;overflow:auto"></pre></div>' +
    '<footer><button type="button" id="checkPluginUpdate">检查更新</button><button type="button" id="installPluginUpdate" hidden>更新并重启</button><button type="button" data-close-update>关闭</button></footer>';
  document.body.append(dialog);
  const menu = document.querySelector('#openPluginUpdates');
  const status = dialog.querySelector('#pluginUpdateStatus');
  const checkButton = dialog.querySelector('#checkPluginUpdate');
  const installButton = dialog.querySelector('#installPluginUpdate');
  const mode = dialog.querySelector('#pluginUpdateMode');
  let state, started = false;
  const notify = (message, failed = false) => {
    status.textContent = message;
    status.style.color = failed ? '#b02d33' : '#236b53';
  };
  async function call(action, value) {
    const api = window.pywebview?.api?.plugin_update;
    if (!api) throw new Error('请从 KiCad 插件入口重新打开新版独立工作台');
    const result = await api(action, value ?? null);
    if (!result.ok) throw new Error(result.error || '更新操作失败');
    return result;
  }
  function render(result) {
    state = result;
    dialog.querySelector('#pluginVersion').textContent = '当前版本：' + result.current_version + (result.latest_version ? '　最新版本：' + result.latest_version : '');
    mode.value = result.mode;
    installButton.hidden = !result.available || !result.installed;
    mode.disabled = !result.installed;
    dialog.querySelector('#pluginReleaseNotes').textContent = result.notes || '';
    menu.textContent = result.ready ? '插件更新…（已下载）' : result.available ? '插件更新…（有新版本）' : '插件更新…';
    notify(!result.installed ? '源码运行不覆盖更新，请更新 Git 仓库后重新安装。' :
      result.ready ? '更新包已校验；可立即更新，自动模式也会在正常退出后安装。' :
      result.message || result.last_error || (result.available ? '发现新版本，可更新并重启。工程记录和现有转换器将保留。' : result.last_checked ? '已检查，无更新版本。' : '尚未检查更新。'), Boolean(result.last_error));
  }
  async function busy(work) {
    if (window.pluginUpdateBusy) return;
    window.pluginUpdateBusy = true;
    checkButton.disabled = installButton.disabled = mode.disabled = true;
    try { await work(); }
    catch (error) { notify(error.message || String(error), true); }
    finally {
      window.pluginUpdateBusy = false;
      checkButton.disabled = installButton.disabled = false;
      mode.disabled = !state?.installed;
    }
  }
  const check = async () => {
    notify('正在检查正式发布版本…');
    const result = await call('check');
    render(result);
    if (!result.available && !result.message) notify('已是最新正式版本。');
    return result;
  };
  menu.addEventListener('click', () => {
    closeToolbarMenus();
    if (!dialog.open) dialog.showModal();
    busy(async () => render(await call('status')));
  });
  dialog.querySelectorAll('[data-close-update]').forEach(button => button.addEventListener('click', () => dialog.close()));
  checkButton.addEventListener('click', () => busy(check));
  mode.addEventListener('change', () => busy(async () => {
    const result = await call('preferences', mode.value);
    render(result);
    notify('更新方式已保存，下次启动生效。');
  }));
  installButton.addEventListener('click', async () => {
    if (!window.confirm('更新后将保存行编辑并重启工作台。工程文件、选料记录与转换器保持不变。继续？')) return;
    let ready = false;
    await busy(async () => {
      notify('正在下载并校验更新包…');
      render(await call('prepare'));
      ready = true;
    });
    if (ready) {
      if (window.hasUnsavedPathSettings?.()) { notify('请先保存或取消工程设置，再更新并重启。', true); return; }
      dialog.close();
      await window.requestDesktopExit(true, true);
    }
  });
  async function startup() {
    if (started || !window.pywebview?.api?.plugin_update) return;
    started = true;
    await busy(async () => {
      const current = await call('status');
      render(current);
      if (!current.installed || current.mode === 'off') return;
      const update = await check();
      if (update.available && update.mode === 'auto') {
        render(await call('prepare'));
        setState('新版插件已下载，正常退出工作台后自动安装', 'status');
      } else if (update.available) setState('发现新版插件，可在“设置 → 插件更新”中更新', 'status');
    });
  }
  window.addEventListener('pywebviewready', startup);
  startup();
})();
