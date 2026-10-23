/* Native utility pages share project services, never the host's window controller. */
(() => {
  const query = new URLSearchParams(location.search);
  const view = query.get('view'), kind = view === 'candidate' ? 'candidate' : query.get('tool');
  const isChild = view === 'tool' || view === 'candidate';
  const handlers = new Map();
  const proposals = new Map();
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
  window.proposeNativeToolAction = (operation, value = null) => new Promise((resolve,reject) => {
    const request_id = crypto.randomUUID();
    const timer = setTimeout(() => {
      proposals.delete(request_id); reject(new Error('宿主尚未响应，请核对主窗口中的操作状态'));
    },120000);
    proposals.set(request_id,{resolve,reject,timer});
    action('propose',{action:operation,value,request_id}).catch(error => {
      clearTimeout(timer); proposals.delete(request_id); reject(error);
    });
  });
  window.receiveNativeToolResult = value => {
    const pending = proposals.get(value?.request_id);
    if (!pending) return;
    proposals.delete(value.request_id); clearTimeout(pending.timer);
    if (value.result?.ok === false) pending.reject(new Error(value.result.error || '宿主未能完成操作'));
    else pending.resolve(value.result);
  };
  window.nativeToolProposal = async message => {
    const proposal = message?.proposal;
    if (isChild || !proposal) return;
    let result;
    try {
      if (message.kind === 'plugin-update' && /^update-(status|check|preferences|prepare)$/.test(proposal.action)) {
        window.pluginUpdateBusy = true;
        try { result = await window.pywebview.api.plugin_update(proposal.action.slice(7),proposal.value); }
        finally { window.pluginUpdateBusy = false; }
      } else if (message.kind === 'list-view' && proposal.action === 'autosize') {
        autoSizeColumns(false); result = {ok:true};
      } else if ((['settings','field-mapping'].includes(message.kind) && proposal.action === 'restart') ||
                 (message.kind === 'plugin-update' && proposal.action === 'restart-update')) {
        result = {ok:true};
        await action('proposal-result',{kind:message.kind,request_id:proposal.request_id,result});
        setTimeout(()=>window.requestDesktopExit(true,proposal.action==='restart-update'),0);
        return;
      } else throw new Error('不支持的工具请求');
    } catch (error) {result={ok:false,error:error.message || String(error)};}
    try { await action('proposal-result',{kind:message.kind,request_id:proposal.request_id,result}); }
    catch (error) { window.dispatchEvent(new CustomEvent('workbench-tool-error',{detail:error.message})); }
  };
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
    window.nativeToolMounted = true;
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
  window.prepareNativeToolExit = (hostExit = false) => {
    if (exiting) return true;
    const canExit = kind === 'candidate' ? window.prepareNativeCandidateExit?.() === true
      : !handler() || handler().prepareExit?.(hostExit) === true;
    if (!canExit) return false;
    exiting = true;
    window.nativeToolExitPending = true;
    document.body.inert = true;
    return true;
  };
  window.cancelNativeToolExit = () => {
    exiting = false;
    window.nativeToolExitPending = false;
    document.body.inert = false;
    if (kind === 'candidate') window.cancelNativeCandidateExit?.();
    else handler()?.cancelExit?.();
  };
  window.nativeToolExitState = () => ({busy:kind === 'candidate'
    ? window.nativeCandidateCanExit?.() !== true : handler()?.isBusy?.() === true});
  window.nativeToolDidClose = () => {
    window.nativeToolMounted = false;
    handler()?.unmount?.();
    current = null; mounted = null;
  };
  if (isChild) {
    let closing = false;
    window.addEventListener('keydown', event => {
      if (event.key !== 'Escape' || event.defaultPrevented || event.isComposing || event.keyCode === 229 ||
          document.querySelector('[data-composing="1"],dialog[open],.fp-overlay')) return;
      event.preventDefault();
      if (closing) return;
      closing = true;
      // Use the same guarded host close as the title-bar button.
      window.closeNativeTool().catch(error => {
        window.dispatchEvent(new CustomEvent('workbench-tool-error', {detail:error.message}));
      }).finally(() => { closing = false; });
    });
    const ready = () => action('ready').catch(error => {
      window.dispatchEvent(new CustomEvent('workbench-tool-error', {detail:error.message}));
    });
    window.addEventListener('pywebviewready', ready, {once:true});
    if (window.pywebview?.api?.tool_window) void ready();
  }
})();
