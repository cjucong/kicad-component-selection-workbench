/* The native host calls this same path for its title-bar close button. */
(() => {
  let exiting = false;
  window.requestDesktopExit = async (restart, update = false) => {
    if (exiting) return;
    if (!window.pywebview?.api?.finish_exit) {
      setState('仅支持独立工作台，请从 KiCad 插件入口启动', 'error');
      return;
    }
    exiting = true;
    const wasInert = document.body.inert;
    try {
      if (bulkSyncRunning) throw new Error('正在同步原理图，请完成后再关闭或重启工作台');
      if (window.designSyncBusy) throw new Error('正在同步设计，请完成后再关闭或重启工作台');
      if (window.pcbUpdateBusy) throw new Error('请先完成或关闭 PCB 更新窗口，再关闭或重启工作台');
      if (window.rowFootprintBusy) throw new Error('请先完成或取消封装管理，再关闭或重启工作台');
      if (window.localInventoryBusy) throw new Error('正在导入本地库存，请完成后再关闭或重启工作台');
      if (window.pluginUpdateBusy) throw new Error('正在检查或下载插件更新，请完成后再关闭');
      if (document.querySelector('[data-composing="1"]')) throw new Error('请先完成输入法编辑');
      if (window.hasUnsavedPathSettings?.() && !window.confirm('路径配置尚未保存，放弃这些配置改动并退出？')) return;
      window.desktopExitPreparing = true;
      await window.pauseDurableStockRefresh?.();
      cancelStockQueries();
      cancelRowStockQueries();
      document.body.inert = true;
      await flushRowEdits();
      cancelRowStockQueries();
      setState(restart ? '正在重启工作台…' : '正在退出工作台…', 'status');
      const deadline = Date.now() + 60000;
      while (true) {
        const result = await window.pywebview.api.finish_exit(Boolean(restart), Boolean(update));
        if (result.ok) break;
        if (!result.retryable || Date.now() >= deadline) throw new Error(result.error || '后台未能退出');
        setState('正在等待当前任务结束，随后自动退出…', 'status');
        await new Promise(resolve => setTimeout(resolve, 300));
      }
    } catch (error) {
      setState(error.message || String(error), 'error');
    } finally {
      document.body.inert = wasInert;
      window.desktopExitPreparing = false;
      exiting = false;
    }
  };
  for (const [id, restart] of [['restartWorkbench', true], ['exitWorkbench', false]]) {
    document.getElementById(id).addEventListener('click', () => {
      closeToolbarMenus();
      window.requestDesktopExit(restart);
    });
  }
})();
