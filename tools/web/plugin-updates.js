/* Only the owned native host can download or install releases. */
(() => {
  const dialog = document.createElement('dialog');
  dialog.id = 'pluginUpdatesDialog';
  dialog.className = 'path-settings-dialog';
  dialog.setAttribute('aria-labelledby', 'pluginUpdatesTitle');
  dialog.innerHTML = '<header><h2 id="pluginUpdatesTitle">插件更新</h2><button type="button" class="path-icon-button" data-close-update aria-label="关闭插件更新" title="关闭">&times;</button></header>' +
    '<div class="path-settings-body"><p id="pluginVersion"></p><p id="pluginUpdateChecked" class="path-settings-note"></p>' +
    '<div class="path-field"><label for="pluginUpdateMode">更新方式</label><div class="path-field-control"><select id="pluginUpdateMode"><option value="off">手动检查</option><option value="check">启动时检查并提醒（默认）</option><option value="auto">自动下载，退出工作台后更新</option></select></div></div>' +
    '<p id="pluginUpdateStatus" role="status" aria-live="polite"></p><progress id="pluginUpdateProgress" aria-label="插件更新操作进行中" hidden></progress>' +
    '<p id="pluginUpdateRetry" class="path-settings-note" hidden></p>' +
    '<details id="pluginUpdateNotes" open><summary>更新内容</summary><pre id="pluginReleaseNotes"></pre></details></div>' +
    '<footer><button type="button" id="checkPluginUpdate">检查更新</button><button type="button" id="downloadPluginUpdate" hidden>下载更新</button><button type="button" id="installPluginUpdate" hidden>重启并更新</button><button type="button" data-close-update>关闭</button></footer>';
  document.body.append(dialog);
  const menu = document.querySelector('#openPluginUpdates');
  const status = dialog.querySelector('#pluginUpdateStatus');
  const checkButton = dialog.querySelector('#checkPluginUpdate');
  const installButton = dialog.querySelector('#installPluginUpdate');
  const downloadButton = dialog.querySelector('#downloadPluginUpdate');
  const progress = dialog.querySelector('#pluginUpdateProgress');
  const mode = dialog.querySelector('#pluginUpdateMode');
  let state, started = false;
  const notify = (message, failed = false) => {
    status.textContent = message;
    status.style.color = failed ? '#b02d33' : '#236b53';
  };
  async function call(action, value) {
    if (typeof toolOnly !== 'undefined' && toolOnly) return window.proposeNativeToolAction('update-'+action,value ?? null);
    const api = window.pywebview?.api?.plugin_update;
    if (!api) throw new Error('请从 KiCad 插件入口重新打开新版独立工作台');
    const result = await api(action, value ?? null);
    if (!result.ok) throw new Error(result.error || '更新操作失败');
    return result;
  }
  function render(result) {
    state = result;
    dialog.querySelector('#pluginVersion').textContent = '当前版本：' + result.current_version + (result.latest_version ? '　最新正式版本：' + result.latest_version : '');
    mode.value = result.mode;
    installButton.hidden = !result.ready || !result.installed;
    downloadButton.hidden = !result.available || result.ready || !result.installed;
    mode.disabled = !result.installed;
    const checked = result.last_checked && new Date(result.last_checked);
    dialog.querySelector('#pluginUpdateChecked').textContent = checked && Number.isFinite(checked.getTime())
      ? '上次成功检查：' + checked.toLocaleString() : '本次尚未成功检查更新';
    const retry = dialog.querySelector('#pluginUpdateRetry');
    retry.hidden = !(result.retry_after_seconds > 0);
    retry.textContent = result.retry_after_seconds > 0
      ? '可重试时间：' + new Date(Date.now() + result.retry_after_seconds * 1000).toLocaleTimeString() : '';
    dialog.querySelector('#pluginReleaseNotes').textContent = result.notes || '';
    dialog.querySelector('#pluginUpdateNotes').hidden = !result.notes;
    menu.textContent = result.ready ? '插件更新…（已下载）' : result.available ? '插件更新…（有新版本）' : '插件更新…';
    notify(!result.installed ? '源码运行不覆盖更新，请更新 Git 仓库后重新安装。' :
      result.last_error || (result.ready ? '更新包已校验，可重启并更新。工程记录和现有转换器将保留。' :
      result.message || (result.available ? '发现新版本。' : result.last_checked ? '已检查，当前没有更高的正式发布版本。' : '尚未检查更新。')), Boolean(result.last_error));
  }
  async function busy(work) {
    if (window.pluginUpdateBusy) return;
    window.pluginUpdateBusy = true;
    checkButton.disabled = installButton.disabled = downloadButton.disabled = mode.disabled = true;
    progress.hidden = false;
    try { await work(); }
    catch (error) {
      try { render(await call('status')); } catch (_) { /* Keep the last known controls if the bridge is unavailable. */ }
      if (state) mode.value = state.mode;
      notify(error.message || String(error), true);
    }
    finally {
      window.pluginUpdateBusy = false;
      progress.hidden = true;
      checkButton.disabled = installButton.disabled = downloadButton.disabled = false;
      mode.disabled = !state?.installed;
    }
  }
  const check = async (force = true) => {
    notify('正在检查正式发布版本…');
    const result = await call('check', force);
    render(result);
    return result;
  };
  menu.addEventListener('click', () => {
    closeToolbarMenus();
    if (!dialog.open) dialog.showModal();
    busy(async () => render(await call('status')));
  });
  dialog.querySelectorAll('[data-close-update]').forEach(button => button.addEventListener('click', () => dialog.close()));
  checkButton.addEventListener('click', () => busy(() => check(true)));
  mode.addEventListener('change', () => busy(async () => {
    const result = await call('preferences', mode.value);
    render(result);
    notify('更新方式已保存。');
  }));
  downloadButton.addEventListener('click', () => busy(async () => {
    notify('正在下载并校验更新包…');
    render(await call('prepare'));
  }));
  installButton.addEventListener('click', async () => {
    if (window.pluginUpdateBusy || !state?.ready) return;
    if (window.hasUnsavedPathSettings?.()) { notify('请先保存或取消工程设置，再更新并重启。', true); return; }
    if (!window.confirm('更新后将保存行编辑并重启工作台。工程文件、选料记录与转换器保持不变。继续？')) return;
    dialog.close();
    if (typeof toolOnly !== 'undefined' && toolOnly) await window.proposeNativeToolAction('restart-update');
    else await window.requestDesktopExit(true, true);
  });
  async function startup() {
    if (started || !window.pywebview?.api?.plugin_update) return;
    started = true;
    await busy(async () => {
      const current = await call('status');
      render(current);
      if (!current.installed || current.mode === 'off') return;
      const update = await check(false);
      if (update.available && update.mode === 'auto') {
        notify('正在下载并校验更新包…');
        render(await call('prepare'));
        setState('新版插件已下载，正常退出工作台后自动安装', 'status');
      } else if (update.available) setState('发现新版插件，可在“设置 → 插件更新”中更新', 'status');
    });
  }
  window.addEventListener('pywebviewready', startup);
  startup();
})();
