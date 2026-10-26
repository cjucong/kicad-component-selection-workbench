(() => {
  if (window.workbenchEditMenu) return;
  const generation = /*workbench-generation*/ 0;
  let sequence = 0;
  const request = context => window.pywebview.api.workbench_edit({
    ...context, generation, sequence: ++sequence
  });
  const writable = el => el && !el.disabled && !el.readOnly &&
    (el.isContentEditable || el.tagName === 'TEXTAREA' || (el.tagName === 'INPUT' &&
      ['text','search','url','tel','email','password','number'].includes(el.type)));
  let requested = false;
  const dismiss = () => {
    if (!requested) return;
    requested = false;
    request({dismiss: true});
  };
  // WebView2 consumes clicks before WinForms' outside-click menu filter.
  document.addEventListener('pointerdown', event => { if (event.isTrusted) dismiss(); }, true);
  document.addEventListener('scroll', dismiss, true);
  window.addEventListener('resize', dismiss);
  window.addEventListener('blur', dismiss);
  document.addEventListener('contextmenu', event => {
    if (!event.isTrusted) return;
    if (event.target.closest('.fp-viewport')) return;
    const el = event.target.closest('input,textarea,[contenteditable]');
    const inputSelection = el && typeof el.selectionStart === 'number' && el.selectionEnd > el.selectionStart;
    const selected = inputSelection || !!getSelection()?.toString();
    if (!el && !selected) return;
    event.preventDefault();
    event.stopPropagation();
    // Native menus do not focus DOM nodes, so the original selection survives.
    if (el) el.focus({preventScroll:true});
    requested = true;
    request({
      selected: !!selected, writable: !!writable(el),
      x: event.clientX, y: event.clientY, width: innerWidth, height: innerHeight
    });
  }, true);
  window.workbenchEditMenu = true;
})();
