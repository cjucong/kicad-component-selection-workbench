/* Copy individual electrical properties, never a product description or CAD data. */
const ValueParameters = (() => {
  const schemas = {
    resistor: [
      ['nominal', '阻值', ['阻值', '电阻值', 'resistance', 'nominal resistance']],
      ['tolerance', '精度', ['精度', '容差', '公差', 'tolerance']],
      ['tcr', '温度系数', ['温度系数', 'tcr', 'temperature coefficient', '电阻温度系数']],
      ['voltage', '最大工作电压', ['工作电压', '最大工作电压', 'working voltage', 'maximum working voltage']],
      ['power', '功率', ['功率', '额定功率', 'power', 'rated power', 'power rating']],
    ],
    capacitor: [
      ['nominal', '容值', ['容值', '容量', '电容量', 'capacitance', 'capacity']],
      ['voltage', '额定电压', ['额定电压', '电压', '工作电压', 'rated voltage', 'voltage rating', 'voltage']],
      ['dielectric', '温度系数 / 介质', ['温度系数', '温度特性', '介质', 'dielectric', 'temperature coefficient']],
      ['tolerance', '精度', ['精度', '容差', '公差', 'tolerance']],
    ],
    inductor: [
      ['nominal', '感值', ['感值', '电感值', '电感量', 'inductance']],
      ['rms', 'RMS Current', ['温升电流', 'rms current', 'rms', '额定电流', 'rated current', 'current rating']],
      ['sat', 'Sat Current', ['饱和电流', '饱和电流(isat)', 'sat current', 'sat', 'isat', 'saturation current']],
      ['dcr', '直流电阻 (DCR)', ['直流电阻(dcr)', '直流电阻', 'dcr', 'dc resistance']],
      ['type', '电感类型', ['电感类型', '类型', 'type', 'inductor type']],
      ['tolerance', '精度', ['精度', '容差', '公差', 'tolerance']],
    ],
  };
  const number = '(?:\\d+(?:\\.\\d+)?|\\.\\d+)';
  const sign = '(?:±|\\+/-|[+-])?\\s*';
  const ohm = '(?:Ω|Ω|ohms?|R)';
  const patterns = kind => [
    ...(kind === 'inductor' ? [
      ['rms', `(?:RMS(?: Current)?|Rated Current|温升电流|额定电流)\\s*[:：=]\\s*(?:${number}\\s*[munµμ]?A|not specified)`],
      ['sat', `(?:Sat(?: Current)?|Saturation Current|饱和电流(?:\\s*[(（]Isat[)）])?|Isat)\\s*[:：=]\\s*(?:${number}\\s*[munµμ]?A|not specified)`],
      ['dcr', `(?:DCR|直流电阻(?:\\(DCR\\))?)\\s*[:：=]\\s*${number}\\s*[munµμkKM]?${ohm}`],
      ['type', '(?:Type|电感类型)\\s*[:：=]\\s*.+'],
    ] : []),
    ...(kind === 'resistor' ? [['tcr', `(?:TCR\\s*[:：=]\\s*)?${sign}${number}\\s*ppm(?:\\s*/\\s*(?:℃|°C|K))?`]] : []),
    ['tolerance', `${sign}${number}\\s*%`],
    ...(kind === 'capacitor' ? [['dielectric', '(?:C0G|NP0|X[5678][RSTUV]|Y5V|Z5U)']] : []),
    ['voltage', `${number}\\s*[munµμkK]?V(?:AC|DC)?`],
    ['power', `(?:${number}(?:/${number})?)\\s*[munµμkK]?W`],
  ].map(([key, pattern]) => [key, new RegExp(`^(?:${pattern})(?=$|\\s)`, 'i')]);

  function nominalPattern(kind) {
    const unit = {resistor: ohm, capacitor: 'F', inductor: 'H'}[kind];
    const chinese = {resistor: '(?:千|兆|毫|微)?欧(?:姆)?', capacitor: '(?:微|纳|皮|毫)?法(?:拉)?', inductor: '(?:微|毫|纳)?亨(?:利)?'}[kind];
    const suffix = kind === 'resistor' ? `(?:[pnumkKMG]?\\s*${ohm}|[pnumkKMG])?` : `(?:[pnumkKMGµμ]\\s*${unit}?|${unit})`;
    return new RegExp(`^(?:${number}\\s*${chinese}|\\d+[RrkpnumKMGµμ]\\d+(?:${unit})?|${number}\\s*${suffix})(?=$|\\s)`);
  }

  function tokensFor(kind, value) {
    const result = [], known = patterns(kind);
    let first = true;
    for (let segment of String(value).split(/[,，;；\r\n]+/)) {
      segment = segment.trim();
      if (!segment) continue;
      // Unrecognized tails stay intact, including user-written design constraints.
      const parsed = [];
      while (segment) {
        let match = first && nominalPattern(kind).exec(segment), key = 'nominal';
        if (!match) {
          const item = known.find(([, pattern]) => pattern.test(segment));
          if (item) { key = item[0]; match = item[1].exec(segment); }
        }
        if (!match) { parsed.push({key: '', text: segment}); break; }
        first = false;
        parsed.push({key, text: match[0].trim()});
        segment = segment.slice(match[0].length).trim();
      }
      // A qualifier followed by prose is one special requirement, not a bare rating.
      if (parsed.length > 1 && !parsed.at(-1).key && parsed.at(-2).key !== 'nominal') {
        const tail = parsed.splice(-2);
        parsed.push({key: '', text: tail.map(item => item.text).join(' ')});
      }
      result.push(...parsed);
      first = false;
    }
    return result;
  }

  const normalize = value => String(value ?? '').replace(/[µμ]/g, 'u').replace(/Ω/g, 'Ω')
    .replace(/\+\/-/g, '±').replace(/\s+/g, '').replace(/°C/g, '℃');
  const available = value => !!String(value || '').trim() && !/^(?:[-—–]+|未提供|未列出|商城未列出|渠道未返回|待核实|待确认|待补全|未读取|正在读取|未知|暂无|无|n\/?a|unknown|not specified|not provided|not available|null|undefined)$/i.test(String(value).trim());
  const unitSuffix = /\s*[(（]\s*(ppm(?:\/(?:°C|℃|K))?|[pnumµμkKM]?(?:Ω|Ω|ohms?|F|H|A|V|W)|%)\s*[)）]$/i;
  function formatted(key, text) {
    const value = String(text).trim().replace(/[µμ]/g, 'u').replace(/\+\/-/g, '±');
    const prefix = {rms: 'RMS Current: ', sat: 'Sat Current: ', dcr: 'DCR: ', type: 'Type: '}[key];
    return prefix ? prefix + value : value;
  }

  function buildModel(kind, original, pairs) {
    const schema = schemas[kind] || [], tokens = schema.length ? tokensFor(kind, original) : [];
    const options = schema.map(([key, label, aliases]) => {
      const values = new Map();
      for (const pair of pairs) {
        const name = String(pair.key || pair.label || '').trim();
        if (!aliases.includes(name.replace(unitSuffix, '').toLowerCase()) || !available(pair.value)) continue;
        const unit = name.match(unitSuffix)?.[1];
        let value = String(pair.value).trim();
        if (unit && new RegExp(`^${sign}${number}$`).test(value)) value += unit;
        values.set(normalize(value), value);
      }
      const valid = values.size === 1, value = valid ? [...values.values()][0] : '';
      return {key, label, value, text: formatted(key, value), available: valid,
        reason: values.size > 1 ? '参数冲突：' + [...values.values()].join(' / ') : '物料参数未提供',
        selected: valid && tokens.some(token => token.key === key)};
    });
    return {kind, original, tokens, options};
  }

  function compose(model, selected) {
    const replacements = new Map(model.options.filter(item => item.available && selected.includes(item.key)).map(item => [item.key, item.text]));
    if (!replacements.size) return model.original;
    const output = [], applied = new Set();
    for (const token of model.tokens) {
      if (!replacements.has(token.key)) output.push(token.text);
      else if (!applied.has(token.key)) { output.push(replacements.get(token.key)); applied.add(token.key); }
    }
    for (const [key, text] of replacements) if (!applied.has(key)) {
      if (key === 'nominal') output.unshift(text); else output.push(text);
    }
    return output.join(', ');
  }

  function selectedSource(row, code) {
    if (!code) throw new Error('请先选择主料 C 编码');
    if (row.selection_codes_mixed) throw new Error('该行 C 编码混合，请先统一主料');
    const fields = row.override?.field_overrides || {};
    if (fields.description && fields.description_c_code === code) return {c_code: code, param: fields.description};
    if (String(row.stock?.c_code || '').toUpperCase() !== code) throw new Error('未取得当前 C 编码的物料参数，请先刷新库存');
    return row.stock;
  }

  return {buildModel, compose, selectedSource};
})();
if (typeof module !== 'undefined') module.exports = ValueParameters;

if (typeof document !== 'undefined') (() => {
  if (candidateOnly) return;
  const dialog = document.createElement('dialog');
  dialog.id = 'valueParametersDialog';
  dialog.setAttribute('aria-labelledby', 'valueParametersTitle');
  dialog.innerHTML = `<header><h2 id="valueParametersTitle">补充 Value</h2><button type="button" data-value-close title="关闭" aria-label="关闭"><svg class="toolbar-icon" aria-hidden="true"><use href="#toolbar-icon-x"></use></svg></button></header>
    <div class="value-parameter-body"><p data-value-source></p><div data-value-options></div>
    <label class="value-preview-label">当前 Value<output data-value-before></output></label>
    <label class="value-preview-label">预览 Value<output data-value-preview></output></label>
    <p role="alert" data-value-error hidden></p></div>
    <footer><button type="button" data-value-cancel>取消</button><button type="button" data-value-apply>应用</button></footer>`;
  document.body.appendChild(dialog);
  let context = null;
  const apply = dialog.querySelector('[data-value-apply]');
  const errorBox = dialog.querySelector('[data-value-error]');
  const chosen = () => [...dialog.querySelectorAll('[data-value-key]:checked')].map(input => input.dataset.valueKey);
  window.hasUnsavedValueParameters = () => Boolean(dialog.dataset.saving ||
    (context && dialog.open && chosen().join(',') !== context.initialChosen));
  const findRow = refs => [...rowsEl.querySelectorAll('tr[data-refs]')].find(tr => tr.dataset.refs === refs);
  const identifyKind = row => {
    const kinds = new Set((row.refs || row.ref || '').split(/[,\s]+/).map(ref => /^([RCL])\d+$/i.exec(ref)?.[1]?.toUpperCase()).filter(Boolean));
    return kinds.size === 1 ? {R: 'resistor', C: 'capacitor', L: 'inductor'}[[...kinds][0]] : candidateParameterKind({item: {}}, row);
  };
  const sourceFor = row => ValueParameters.selectedSource(row, stockCode(row));
  function preview() {
    const value = ValueParameters.compose(context.model, chosen());
    dialog.querySelector('[data-value-preview]').textContent = value || '（空）';
    apply.disabled = !chosen().length || value === context.original;
  }
  window.openValueParameters = async refs => {
    const tr = findRow(refs), row = rowDataForElement(tr);
    if (!tr || !row || rowsLoading) throw new Error('分组已变化，请在当前行重新点击补充 Value');
    const source = sourceFor(row), original = tr.querySelector('[data-field="value"]').value;
    const kind = identifyKind(row), model = ValueParameters.buildModel(kind, original, candidateParameterPairs(source, kind));
    if (!model.options.length) throw new Error('暂只支持电阻、电容、电感的 Value 参数补充');
    context = {refs, code: stockCode(row), original, model, source: JSON.stringify(source), bom: loadedRowsBom, version: rowsLoadVersion};
    const container = dialog.querySelector('[data-value-options]');
    container.replaceChildren();
    for (const item of model.options) {
      const label = document.createElement('label');
      label.className = 'value-parameter-option' + (item.available ? '' : ' unavailable');
      const check = document.createElement('input');
      check.type = 'checkbox'; check.dataset.valueKey = item.key;
      check.checked = item.selected; check.disabled = !item.available;
      const name = document.createElement('span'), value = document.createElement('span');
      name.textContent = item.label; value.textContent = item.available ? item.value : item.reason;
      label.append(check, name, value); container.appendChild(label);
    }
    dialog.querySelector('[data-value-source]').textContent = `${refs} · ${context.code}`;
    dialog.querySelector('[data-value-before]').textContent = original || '（空）';
    errorBox.hidden = true;
    context.initialChosen = chosen().join(',');
    preview(); dialog.showModal();
  };
  const close = () => { if (!dialog.dataset.saving) dialog.close(); };
  dialog.querySelector('[data-value-close]').addEventListener('click', close);
  dialog.querySelector('[data-value-cancel]').addEventListener('click', close);
  dialog.addEventListener('cancel', event => { if (dialog.dataset.saving) event.preventDefault(); });
  dialog.addEventListener('change', preview);
  dialog.addEventListener('close', () => { context = null; });
  apply.addEventListener('click', async () => {
    if (!context || dialog.dataset.saving) return;
    dialog.dataset.saving = '1'; apply.disabled = true; errorBox.hidden = true;
    try {
      await flushRowEdits();
      const tr = findRow(context.refs), row = rowDataForElement(tr), input = tr?.querySelector('[data-field="value"]');
      if (!row || rowsLoading || rowsLoadVersion !== context.version || loadedRowsBom !== context.bom ||
          stockCode(row) !== context.code || input.value !== context.original || JSON.stringify(sourceFor(row)) !== context.source) {
        throw new Error('该行或物料参数已变化，请关闭后重新选择');
      }
      input.value = ValueParameters.compose(context.model, chosen());
      queueRowInput(input);
      await flushRowEdits();
      if (toolOnly) await window.notifyNativeToolChanged();
      dialog.close();
    } catch (error) {
      errorBox.textContent = error.message; errorBox.hidden = false;
      setState(error.message, 'error');
    } finally { delete dialog.dataset.saving; if (context) preview(); }
  });
})();
