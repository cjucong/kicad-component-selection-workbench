(() => {
  if (window.workbenchEditMenu) return;
  const writable = el => el && !el.disabled && !el.readOnly &&
    (el.isContentEditable || el.tagName === 'TEXTAREA' || (el.tagName === 'INPUT' &&
      ['text','search','url','tel','email','password','number'].includes(el.type)));
  document.addEventListener('contextmenu', event => {
    if (!event.isTrusted) return;
    const el = event.target.closest('input,textarea,[contenteditable]');
    const inputSelection = el && typeof el.selectionStart === 'number' && el.selectionEnd > el.selectionStart;
    const selected = inputSelection || !!getSelection()?.toString();
    if (!el && !selected) return;
    event.preventDefault();
    event.stopPropagation();
    // Native menus do not focus DOM nodes, so the original selection survives.
    if (el) el.focus({preventScroll:true});
    window.pywebview.api.workbench_edit({
      selected: !!selected, writable: !!writable(el),
      x: event.clientX, y: event.clientY, width: innerWidth, height: innerHeight
    });
  }, true);
  window.workbenchEditMenu = true;
})();
