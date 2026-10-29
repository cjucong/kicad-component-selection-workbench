/* Main workbench facets. Icons use Lucide list-filter/x (ISC; LUCIDE-LICENSE). */
(function (root) {
  'use strict';
  const FIELDS = Object.freeze({
    comment: 'Comment', value: 'Value', footprint: 'Footprint', page_refs: '\u9875\u9762'
  });
  const blankLabel = '\uff08\u7a7a\u767d\uff09';
  const normalize = value => String(value ?? '').trim();

  function rowFilterValues(row, field) {
    if (!Object.hasOwn(FIELDS, field)) return [];
    if (field !== 'page_refs') return [normalize(row?.[field])];
    const links = Array.isArray(row?.ref_links) ? row.ref_links : [];
    return [...new Set(links.length ? links.map(link => normalize(link?.page)) : [normalize(row?.page)])];
  }

  function restoreRowFilters(saved) {
    const selected = new Map();
    for (const field of Object.keys(FIELDS)) {
      if (!Array.isArray(saved?.[field])) continue;
      const values = new Set(saved[field].filter(value => typeof value === 'string').map(normalize));
      if (values.size) selected.set(field, values);
    }
    return selected;
  }

  function serializeRowFilters(selected) {
    return Object.fromEntries(Object.keys(FIELDS).filter(field => selected.get(field)?.size)
      .map(field => [field, [...selected.get(field)].sort()]));
  }

  function rowMatchesFilters(row, selected, exceptField = '') {
    for (const [field, values] of selected) {
      if (field === exceptField || !Object.hasOwn(FIELDS, field) || !values.size) continue;
      if (!rowFilterValues(row, field).some(value => values.has(value))) return false;
    }
    return true;
  }

  function rowFilterFacets(rows, selected, field) {
    const counts = new Map();
    for (const value of selected.get(field) || []) counts.set(value, 0);
    for (const row of rows) {
      for (const value of rowFilterValues(row, field)) if (!counts.has(value)) counts.set(value, 0);
      if (!rowMatchesFilters(row, selected, field)) continue;
      for (const value of rowFilterValues(row, field)) counts.set(value, (counts.get(value) || 0) + 1);
    }
    return new Map([...counts].sort(([a], [b]) => a.localeCompare(b, 'zh-CN', {numeric: true})));
  }

  function searchRowFilterOptions(facets, query) {
    const term = normalize(query).toLocaleLowerCase();
    return [...facets].filter(([value]) => (value || blankLabel).toLocaleLowerCase().includes(term));
  }

  function projectRowFilters(settings, project) {
    const projects = settings?.rowFiltersByProject;
    return restoreRowFilters(projects && Object.hasOwn(projects, project) ? projects[project] : null);
  }

  function rowFilterSettingsUpdate(settings, project, selected) {
    return {rowFiltersByProject: {...settings?.rowFiltersByProject, [project]: serializeRowFilters(selected)}};
  }

  function createRowFilters({document, project, getSettings, saveSettings, onChange}) {
    const search = document.getElementById('bomSearch');
    if (!search) return null;
    let selected;
    try { selected = projectRowFilters(getSettings(), project); }
    catch (_error) { selected = new Map(); }
    let contextRows = [], activeField = '', opener = null;
    const buttons = new Map();
    const make = (tag, className, text) => {
      const element = document.createElement(tag);
      if (className) element.className = className;
      if (text !== undefined) element.textContent = text;
      return element;
    };
    const icon = name => {
      const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      svg.setAttribute('viewBox', '0 0 24 24');
      svg.setAttribute('aria-hidden', 'true');
      const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', name === 'filter' ? 'M3 6h18M7 12h10M10 18h4' : 'M18 6 6 18M6 6l12 12');
      svg.append(path);
      return svg;
    };
    const button = (label, className, action, iconName) => {
      const control = make('button', className);
      control.type = 'button';
      control.title = label;
      control.setAttribute('aria-label', label);
      if (iconName) control.append(icon(iconName));
      else control.textContent = label;
      control.addEventListener('click', action);
      return control;
    };

    const container = make('section', 'row-filters');
    container.id = 'rowFilters';
    container.setAttribute('aria-label', '\u5217\u7b5b\u9009');
    const tools = make('div', 'row-filter-tools');
    const chooser = make('select', 'row-filter-chooser');
    chooser.id = 'rowFilterField';
    chooser.setAttribute('aria-label', '\u9009\u62e9\u7b5b\u9009\u5217');
    const placeholder = make('option', '', '\u5217\u7b5b\u9009');
    placeholder.value = '';
    chooser.append(placeholder);
    for (const [field, label] of Object.entries(FIELDS)) {
      const option = make('option', '', label);
      option.value = field;
      chooser.append(option);
    }
    const tags = make('div', 'row-filter-tags');
    tags.id = 'rowFilterTags';
    const clear = button('\u6e05\u9664\u5217\u7b5b\u9009', 'row-filter-clear', () => reset(true));
    clear.id = 'clearColumnFilters';
    tools.append(chooser, tags, clear);
    const panel = make('div', 'row-filter-panel');
    panel.id = 'rowFilterPanel';
    panel.hidden = true;
    const heading = make('div', 'row-filter-heading');
    const title = make('strong');
    title.id = 'rowFilterTitle';
    const optionSearch = make('input', 'row-filter-search');
    optionSearch.id = 'rowFilterOptionSearch';
    optionSearch.type = 'search';
    optionSearch.placeholder = '\u641c\u7d22\u9009\u9879';
    optionSearch.setAttribute('aria-label', '\u641c\u7d22\u7b5b\u9009\u9009\u9879');
    const all = button('\u5168\u90e8', 'row-filter-all', () => {
      selected.delete(activeField);
      changed();
    });
    const close = button('\u5173\u95ed\u5217\u7b5b\u9009', 'row-filter-icon', () => hide(true), 'x');
    heading.append(title, optionSearch, all, close);
    const options = make('div', 'row-filter-options');
    options.id = 'rowFilterOptions';
    options.setAttribute('role', 'group');
    options.setAttribute('aria-labelledby', title.id);
    const count = make('output', 'row-filter-option-count');
    count.id = 'rowFilterOptionCount';
    count.setAttribute('aria-live', 'polite');
    panel.append(heading, options, count);
    container.append(tools);
    // The sticky toolbar is transformed during horizontal scrolling.
    // Keep the fixed popup outside that containing block.
    document.body.append(panel);
    const bar = document.querySelector('.filter-bar');
    const quickFilters = bar?.querySelector('.filter-tabs');
    if (quickFilters) quickFilters.after(container);
    else search.closest('.bom-search').after(container);

    function renderOptions() {
      if (!activeField) return;
      const focusedValue = options.contains(document.activeElement) ? document.activeElement.value : null;
      const scrollTop = options.scrollTop;
      const facets = rowFilterFacets(contextRows, selected, activeField);
      const entries = searchRowFilterOptions(facets, optionSearch.value);
      const fragment = document.createDocumentFragment();
      for (const [value, total] of entries) {
        const label = make('label', 'row-filter-option');
        const input = make('input');
        input.type = 'checkbox';
        input.value = value;
        input.checked = Boolean(selected.get(activeField)?.has(value));
        input.setAttribute('aria-label', value || blankLabel);
        input.addEventListener('change', () => {
          const values = new Set(selected.get(activeField));
          if (input.checked) values.add(value);
          else values.delete(value);
          if (values.size) selected.set(activeField, values);
          else selected.delete(activeField);
          changed();
        });
        const text = make('span', 'row-filter-option-text', value || blankLabel);
        text.title = value || blankLabel;
        label.append(input, text, make('span', 'row-filter-option-total', String(total)));
        fragment.append(label);
      }
      if (!entries.length) fragment.append(make('span', 'row-filter-no-options', '\u6ca1\u6709\u5339\u914d\u7684\u9009\u9879'));
      options.replaceChildren(fragment);
      if (focusedValue !== null) {
        [...options.querySelectorAll('input')].find(input => input.value === focusedValue)?.focus({preventScroll: true});
      }
      options.scrollTop = scrollTop;
      count.textContent = `${entries.length} / ${facets.size} \u9879\uff1b\u5df2\u9009 ${selected.get(activeField)?.size || 0} \u9879`;
    }

    function render() {
      for (const [field, trigger] of buttons) {
        const active = Boolean(selected.get(field)?.size);
        trigger.classList.toggle('active', active);
        trigger.setAttribute('aria-pressed', String(active));
        trigger.setAttribute('aria-expanded', String(activeField === field));
        trigger.title = `${FIELDS[field]} \u7b5b\u9009${active ? '\uff08' + selected.get(field).size + '\uff09' : ''}`;
      }
      tags.replaceChildren();
      for (const [field, values] of selected) {
        const tag = make('span', 'row-filter-tag');
        const label = `${FIELDS[field]}: ${[...values].map(value => value || blankLabel).join(' / ')}`;
        const edit = button(label, 'row-filter-tag-edit', () => show(field, edit));
        const remove = button(`\u6e05\u9664 ${FIELDS[field]} \u7b5b\u9009`, 'row-filter-icon', () => {
          selected.delete(field);
          changed();
          chooser.focus();
        }, 'x');
        tag.append(edit, remove);
        tags.append(tag);
      }
      clear.hidden = !selected.size;
      renderOptions();
      if (!panel.hidden) positionPanel();
    }

    function changed() {
      try { saveSettings(rowFilterSettingsUpdate(getSettings(), project, selected)); }
      catch (_error) { /* Filtering still works when browser storage is unavailable. */ }
      render();
      onChange?.();
    }
    function hide(restoreFocus = false) {
      activeField = '';
      panel.hidden = true;
      chooser.value = '';
      render();
      if (restoreFocus) (opener?.isConnected ? opener : chooser).focus();
    }
    function show(field, control) {
      if (!Object.hasOwn(FIELDS, field)) return;
      opener = control;
      activeField = field;
      title.textContent = FIELDS[field];
      chooser.value = field;
      optionSearch.value = '';
      panel.hidden = false;
      render();
      optionSearch.focus({preventScroll: true});
      positionPanel();
    }
    function positionPanel() {
      if (panel.hidden) return;
      const owner = container.getBoundingClientRect(), viewport = document.documentElement.clientWidth;
      const width = Math.min(640, viewport - 24);
      const left = Math.max(12, Math.min(owner.left, viewport - width - 12));
      panel.style.left = `${left}px`;
      panel.style.top = `${Math.max(12, Math.min(owner.bottom + 5, innerHeight - panel.offsetHeight - 12))}px`;
    }
    root.addEventListener?.('resize', positionPanel);
    root.addEventListener?.('scroll', positionPanel, true);
    document.addEventListener('pointerdown', event => {
      if (!panel.hidden && !container.contains(event.target) && !panel.contains(event.target) && !event.target.closest?.('.row-filter-trigger')) hide();
    });
    function reset(notify = false) {
      selected.clear();
      hide();
      if (notify) changed();
      else {
        try { saveSettings(rowFilterSettingsUpdate(getSettings(), project, selected)); }
        catch (_error) { /* See changed(). */ }
      }
    }
    chooser.addEventListener('change', () => {
      if (chooser.value) show(chooser.value, chooser);
    });
    optionSearch.addEventListener('input', renderOptions);
    const escapePanel = event => {
      if (event.key !== 'Escape' || !activeField) return;
      event.preventDefault();
      event.stopPropagation();
      hide(true);
    };
    container.addEventListener('keydown', escapePanel);
    panel.addEventListener('keydown', escapePanel);
    for (const [field, label] of Object.entries(FIELDS)) {
      const header = document.querySelector(`main thead th[data-col="${field}"]`);
      if (!header) continue;
      const trigger = button(`${label} \u7b5b\u9009`, 'row-filter-trigger row-filter-icon', event => {
        event.stopPropagation();
        if (activeField === field) hide(true);
        else show(field, trigger);
      }, 'filter');
      trigger.dataset.rowFilter = field;
      trigger.setAttribute('aria-controls', panel.id);
      trigger.addEventListener('mousedown', event => event.stopPropagation());
      trigger.addEventListener('dblclick', event => event.stopPropagation());
      trigger.addEventListener('keydown', event => {
        if (event.key === 'Escape') { event.preventDefault(); hide(true); }
      });
      header.append(trigger);
      buttons.set(field, trigger);
    }
    render();
    return {
      matches: row => rowMatchesFilters(row, selected),
      active: () => selected.size > 0,
      key: () => serializeRowFilters(selected),
      refresh: rows => { contextRows = rows; render(); },
      reset
    };
  }

  const api = {FIELDS, rowFilterValues, restoreRowFilters, serializeRowFilters, rowMatchesFilters,
    rowFilterFacets, searchRowFilterOptions, projectRowFilters, rowFilterSettingsUpdate, createRowFilters};
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.RowFilters = api;
})(globalThis);
