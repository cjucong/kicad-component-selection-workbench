/* Native utility pages share project services, never the host's window controller. */
(() => {
  const query = new URLSearchParams(location.search);
  const view = query.get('view'), kind = view === 'candidate' ? 'candidate' : query.get('tool');
  const isChild = view === 'tool' || view === 'candidate';
  const handlers = new Map();
  let current = null, mounted = null, exiting = false;
  async function action(name, value = null) {
    const bridge = window.pywebview?.api?.tool_window;
    if (!bridge) throw new Error('独立工具窗口尚未就绪，请重启已更新的工作台');
    const result = await bridge(name, value);
    if (!result?.ok) throw new Error(result?.error || '工具窗口操作失败');
    return result;
  }
  window.openNativeTool = (tool, payload = {}) => action('open', {
    kind:tool, request_id:crypto.randomUUID(), payload
  });
  window.notifyNativeToolChanged = () => action('changed');
  window.closeNativeTool = () => action('close');
  window.minimizeNativeTool = () => action('minimize');
  window.restoreNativeTool = tool => action('restore', {kind:tool});
  window.nativeToolVisibility = value => {
    if (value?.kind === 'candidate') window.nativeCandidateVisibility?.(value);
    window.dispatchEvent(new CustomEvent('workbench-tool-visibility', {detail:value}));
  };
  window.nativeToolChanged = async message => {
    if (message?.kind === 'candidate') return window.nativeCandidateSaved?.(message.payload);
    window.dispatchEvent(new CustomEvent('workbench-tool-changed', {detail:message}));
  };

  function handler() { return handlers.get(kind); }
  function candidateReady() { return typeof window.receiveNativeCandidateRequest === 'function'; }
  function mount() {
    if (!current || mounted === current.request_id) return;
    if (kind === 'candidate') {
      if (!candidateReady()) return;
      const result = window.receiveNativeCandidateRequest(current.payload);
      if (result?.ok !== true) throw new Error(result?.error || '更换型号窗口未能接受请求');
    } else {
      if (!handler()) return;
      const promise = handler().mount(current.payload, {...current});
      if (promise?.catch) promise.catch(error => {
        window.dispatchEvent(new CustomEvent('workbench-tool-error', {detail:String(error.message || error)}));
      });
    }
    mounted = current.request_id;
  }
  window.registerWorkbenchTool = (name, value) => {
    if (!value || typeof value.mount !== 'function') throw new Error('工具页缺少挂载入口');
    handlers.set(name, value);
    if (isChild && name === kind) mount();
  };
  window.nativeToolCanReplace = () => {
    if (exiting) return false;
    if (kind === 'candidate') return window.nativeCandidateCanExit?.() === true;
    if (!handler()) return true;
    if (handler().canReplace) return handler().canReplace() === true;
    const result = handler().prepareExit?.() === true;
    handler().cancelExit?.();
    return result;
  };
  window.receiveNativeToolRequest = value => {
    try {
      if (!isChild || !value || value.kind !== kind || !value.request_id ||
          typeof value.host !== 'string' || typeof value.project !== 'string' ||
          !value.payload || typeof value.payload !== 'object') throw new Error('工具请求身份无效');
      if (current && (value.host !== current.host || value.project !== current.project)) {
        throw new Error('不能切换到其他工作台或工程');
      }
      if (exiting) throw new Error('工具窗口正在退出');
      if (current?.request_id === value.request_id) return {ok:true};
      if (current && !window.nativeToolCanReplace()) throw new Error('工具页有未保存编辑或正在处理，已保留当前内容');
      const previous = current;
      current = value;
      try { mount(); }
      catch (error) { current = previous; throw error; }
      return {ok:true};
    } catch (error) { return {ok:false,error:error.message || String(error)}; }
  };
  window.prepareNativeToolExit = () => {
    if (exiting) return true;
    const canExit = kind === 'candidate' ? window.prepareNativeCandidateExit?.() === true
      : !handler() || handler().prepareExit?.() === true;
    if (!canExit) return false;
    exiting = true;
    document.body.inert = true;
    return true;
  };
  window.cancelNativeToolExit = () => {
    exiting = false;
    document.body.inert = false;
    if (kind === 'candidate') window.cancelNativeCandidateExit?.();
    else handler()?.cancelExit?.();
  };
  if (isChild) {
    const ready = () => action('ready').catch(error => {
      window.dispatchEvent(new CustomEvent('workbench-tool-error', {detail:error.message}));
    });
    window.addEventListener('pywebviewready', ready, {once:true});
    if (window.pywebview?.api?.tool_window) void ready();
  }
})();
