// KiCad angles rotate counterclockwise in a canvas whose Y axis points down.
window.mountFootprintAdjustment = function mountFootprintAdjustment(root, {enabled, changed}) {
  const q = selector => root.querySelector(selector);
  const tools = document.createElement('div');
  tools.className = 'fp-adjust-tools'; tools.dataset.fpAdjust = ''; tools.hidden = true;
  const icon = name => `<svg aria-hidden="true"><use href="#toolbar-icon-${name}"></use></svg>`;
  tools.innerHTML = `<div class="fp-adjust-actions">
    <div class="fp-adjust-modes" role="group" aria-label="立创封装操作">
      <button type="button" data-fp-tool="move" title="拖动立创封装" aria-label="拖动立创封装" aria-pressed="true">${icon('move')}</button>
      <button type="button" data-fp-tool="rotate" title="旋转立创封装" aria-label="旋转立创封装" aria-pressed="false">${icon('rotate-cw')}</button>
      <button type="button" data-fp-tool="pivot" title="设置旋转中心" aria-label="设置旋转中心" aria-pressed="false">${icon('crosshair')}</button>
    </div>
    <button type="button" data-fp-align="center" title="封装中心对齐" aria-label="封装中心对齐">${icon('scan')}<span>中心对齐</span></button>
    <button type="button" data-fp-align="one" title="1 号引脚中心对齐" aria-label="1 号引脚中心对齐">${icon('pin')}<span>1 号对齐</span></button>
    <button type="button" data-fp-align="direction" title="按引脚编号自动定向" aria-label="按引脚编号自动定向">${icon('rotate-cw')}<span>自动定向</span></button>
    <button type="button" data-fp-align="reset" title="重置手动调整" aria-label="重置手动调整">${icon('refresh-cw')}</button>
  </div>
  <div class="fp-adjust-values">
    <label>X <input type="number" step="any" min="-1000" max="1000" value="0" data-fp-dx aria-label="立创 X 偏移 mm"> mm</label>
    <label>Y <input type="number" step="any" min="-1000" max="1000" value="0" data-fp-dy aria-label="立创 Y 偏移 mm"> mm</label>
    <label>θ <input type="number" step="any" min="-360" max="360" value="0" data-fp-angle aria-label="立创旋转角度"> °</label>
    <span data-fp-adjust-status role="status"></span>
  </div>`;
  q('.fp-compare-toolbar').after(tools);
  const numbers = document.createElement('label'); numbers.className = 'fp-marker-toggle';
  numbers.innerHTML = '<input type="checkbox" data-fp-pad-labels checked>引脚编号';
  q('.fp-compare-toolbar').append(numbers);
  let detail = null, pads = {existing:[], converted:[]}, state = {rotation_deg:0, offset_mm:[0,0]};
  let pivot = [0,0], mode = 'move', gesture = null, valid = true, active = true, initialStatus = '';
  const stage = q('[data-fp-stage=converted]');
  const axis = document.createElement('span'); axis.className = 'fp-rotation-axis'; axis.textContent = '+';
  axis.title = '旋转中心'; axis.hidden = true; stage.append(axis);
  const inputs = [q('[data-fp-dx]'), q('[data-fp-dy]'), q('[data-fp-angle]')];
  const finitePoint = point => Array.isArray(point) && point.length === 2 && point.every(Number.isFinite);
  const normalize = angle => ((angle + 180) % 360 + 360) % 360 - 180;
  const rotate = (point, angle) => {
    const a = angle * Math.PI / 180, c = Math.cos(a), s = Math.sin(a);
    return [c*point[0]+s*point[1], -s*point[0]+c*point[1]];
  };
  const transformed = point => rotate(point, state.rotation_deg).map((v,i) => v + state.offset_mm[i]);
  const center = points => [0,1].map(i => (Math.min(...points.map(p=>p[i]))+Math.max(...points.map(p=>p[i])))/2);
  const fmt = value => String(Number(value.toFixed(6)));
  const dirty = () => Math.abs(state.rotation_deg)>1e-9 || state.offset_mm.some(v=>Math.abs(v)>1e-9);
  const absoluteTransform = () => {
    const base=detail?.alignment?.ok?detail.alignment:{rotation_deg:0,offset_mm:[0,0]};
    return {rotation_deg:normalize(base.rotation_deg+state.rotation_deg),
      offset_mm:rotate(base.offset_mm,state.rotation_deg).map((v,i)=>v+state.offset_mm[i])};
  };
  const bounded = transform => Number.isFinite(transform.rotation_deg) &&
    transform.offset_mm.every(v=>Number.isFinite(v)&&Math.abs(v)<=1000);
  const geometryValid = () => bounded(state)&&(!dirty()||bounded(absoluteTransform()));
  const entries = list => Array.isArray(list) ? list.filter(p=>typeof p.number==='string' && p.number && finitePoint(p.position_mm)) : [];
  const unique = list => {
    const map = new Map();
    for (const pad of list) { if (map.has(pad.number)) return null; map.set(pad.number,pad.position_mm); }
    return map;
  };
  function pairs() {
    const a=unique(pads.existing), b=unique(pads.converted);
    return !a || !b ? [] : [...a].filter(([n])=>b.has(n)).map(([n,p])=>[p,transformed(b.get(n))]);
  }
  function pointStyle(element, point) {
    const box=detail.view_box;
    element.style.left=(point[0]-box[0])/box[2]*100+'%';
    element.style.top=(point[1]-box[1])/box[3]*100+'%';
  }
  function setMode(value) {
    mode=value; stage.dataset.fpAdjustMode=value;
    tools.querySelectorAll('[data-fp-tool]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.fpTool===value)));
  }
  function render(syncInputs=true) {
    if (!detail) return;
    const box=detail.view_box, stack=q('[data-fp-source-layers=converted]');
    stack.style.transformOrigin=`${-box[0]/box[2]*100}% ${-box[1]/box[3]*100}%`;
    stack.style.transform=`translate(${state.offset_mm[0]/box[2]*100}%, ${state.offset_mm[1]/box[3]*100}%) rotate(${-state.rotation_deg}deg)`;
    for (const side of ['existing','converted']) {
      root.querySelectorAll(`[data-fp-pad-number][data-fp-side=${side}]`).forEach((label,i)=>{
        pointStyle(label,side==='existing'?pads[side][i].position_mm:transformed(pads[side][i].position_mm));
      });
    }
    const origin=q('[data-fp-marker-source=converted] [data-fp-origin]');
    if (origin) pointStyle(origin,transformed(detail.origin_mm || [0,0]));
    const one=q('[data-fp-pad-one=converted]');
    if (one && finitePoint(detail.converted_pad_one)) pointStyle(one,transformed(detail.converted_pad_one));
    pointStyle(axis,pivot);
    if (syncInputs) [state.offset_mm[0],state.offset_mm[1],state.rotation_deg].forEach((v,i)=>inputs[i].value=fmt(v));
    const a=unique(pads.existing), b=unique(pads.converted);
    const compatible=a&&b&&a.size>0&&a.size===b.size&&[...a.keys()].every(n=>b.has(n));
    const errors=pairs().map(([p,t])=>Math.hypot(p[0]-t[0],p[1]-t[1]));
    const error=errors.length?Math.max(...errors):null;
    q('[data-fp-adjust-status]').textContent=!valid ? '请输入有效坐标与角度' : !compatible
      ? `仅预览 · 本地 ${pads.existing.length} / 立创 ${pads.converted.length} 个编号焊盘不匹配，可库封装改名`
      : `本地固定 · ${dirty()?'已手动调整 · ':''}最大偏差 ${error===null?'—':fmt(error)} mm`;
    if (detail.alignment?.ok && compatible) {
      q('[data-fp-alignment]').textContent=dirty()
        ? `手动配准 · 校正 ${fmt(normalize(detail.alignment.rotation_deg+state.rotation_deg))}° · 焊盘中心最大偏差 ${fmt(error)} mm${error>0.025?' · 请核对偏移与连接':''}`
        : initialStatus;
      q('[data-fp-alignment]').dataset.review=String(dirty()?error>0.025:detail.alignment.requires_review===true);
    }
    q('[data-fp-align=one]').disabled=![pads.existing,pads.converted].every(list=>list.filter(p=>p.number==='1').length===1);
    q('[data-fp-align=direction]').disabled=pairs().length<2;
  }
  function notify(syncInputs=true) {
    valid=(syncInputs||valid)&&geometryValid();
    render(syncInputs); changed();
  }
  function turn(angle) {
    const delta=normalize(angle-state.rotation_deg), r=rotate(state.offset_mm.map((v,i)=>v-pivot[i]),delta);
    state={rotation_deg:normalize(angle),offset_mm:r.map((v,i)=>v+pivot[i])};
  }
  function setComparison(value) {
    clear(); detail=value;initialStatus=q('[data-fp-alignment]').textContent;
    pads={existing:entries(value.existing_pads),converted:entries(value.converted_pads)};
    for (const side of ['existing','converted']) {
      const labels=document.createElement('div'); labels.className='fp-pad-label-stack'; labels.dataset.fpPadLabelStack=side;
      for (const pad of pads[side]) {
        const label=document.createElement('span'); label.className=`fp-pad-number fp-${side==='existing'?'local':'converted'}-color`;
        label.dataset.fpPadNumber=pad.number; label.dataset.fpSide=side;
        label.textContent=pad.number; label.title=`${side==='existing'?'本地':'立创'}引脚 ${pad.number}`;
        labels.append(label);
      }
      q(`[data-fp-stage=${side}]`).append(labels);
    }
    q('.fp-scene').classList.toggle('fp-numbered-pads',pads.existing.length>0);
    setActive(active);
    numbers.hidden=!pads.existing.length;
    if (!tools.hidden) pivot=center(pads.converted.map(p=>p.position_mm));
    setMode('move'); render(); toggleLabels();
  }
  function toggleLabels() {
    root.querySelectorAll('[data-fp-pad-label-stack]').forEach(stack=>stack.hidden=!q('[data-fp-pad-labels]').checked);
    q('.fp-scene').classList.toggle('fp-numbered-pads',q('[data-fp-pad-labels]').checked&&pads.existing.length>0);
  }
  tools.querySelectorAll('[data-fp-tool]').forEach(button=>button.onclick=()=>setMode(button.dataset.fpTool));
  tools.querySelectorAll('[data-fp-align]').forEach(button=>button.onclick=()=>{
    if (!detail || tools.hidden) return;
    valid=true;
    const action=button.dataset.fpAlign;
    if (action==='reset') {state={rotation_deg:0,offset_mm:[0,0]};pivot=center(pads.converted.map(p=>p.position_mm));}
    else if (action==='direction') {
      const matched=pairs(); if(matched.length<2) return;
      const means=[0,1].map(side=>[0,1].map(i=>matched.reduce((sum,p)=>sum+p[side][i],0)/matched.length));
      let dot=0,cross=0;
      for(const [target,source] of matched){
        const x=source[0]-means[1][0],y=source[1]-means[1][1],u=target[0]-means[0][0],v=target[1]-means[0][1];
        dot+=x*u+y*v;cross+=y*u-x*v;
      }
      if(Math.hypot(dot,cross)<1e-12){q('[data-fp-adjust-status]').textContent='对应引脚无法确定方向，请手动旋转';return;}
      turn(state.rotation_deg+Math.atan2(cross,dot)*180/Math.PI);
    } else {
      const a=action==='one'?pads.existing.find(p=>p.number==='1').position_mm:center(pads.existing.map(p=>p.position_mm));
      const b=action==='one'?transformed(pads.converted.find(p=>p.number==='1').position_mm):center(pads.converted.map(p=>transformed(p.position_mm)));
      const delta=a.map((v,i)=>v-b[i]);
      state.offset_mm=state.offset_mm.map((v,i)=>v+delta[i]);pivot=pivot.map((v,i)=>v+delta[i]);
    }
    notify();
  });
  inputs.forEach((input,index)=>input.oninput=()=>{
    valid=inputs.every(e=>e.value!==''&&e.validity.valid&&Number.isFinite(e.valueAsNumber));
    if(valid){
      if(index===2) turn(input.valueAsNumber);
      else {const delta=input.valueAsNumber-state.offset_mm[index];state.offset_mm[index]=input.valueAsNumber;pivot[index]+=delta;}
    }
    notify(false);
    if(valid) [state.offset_mm[0],state.offset_mm[1],state.rotation_deg].forEach((v,i)=>{if(i!==index)inputs[i].value=fmt(v);});
  });
  q('[data-fp-pad-labels]').onchange=toggleLabels;
  const point=event=>{
    const box=detail.view_box,r=stage.getBoundingClientRect();
    return [box[0]+(event.clientX-r.x)*box[2]/r.width,box[1]+(event.clientY-r.y)*box[3]/r.height];
  };
  stage.addEventListener('pointerdown',event=>{
    if(event.button!==0||tools.hidden||!detail||gesture) return;
    event.preventDefault();
    if(mode==='pivot'){pivot=point(event);setMode('rotate');render();return;}
    gesture={id:event.pointerId,point:point(event),state:structuredClone(state),pivot:[...pivot]};
    stage.setPointerCapture(event.pointerId);
  });
  stage.addEventListener('pointermove',event=>{
    if(!gesture||event.pointerId!==gesture.id) return;
    const p=point(event);state=structuredClone(gesture.state);pivot=[...gesture.pivot];valid=true;
    if(mode==='rotate'){
      const a=gesture.point.map((v,i)=>v-pivot[i]),b=p.map((v,i)=>v-pivot[i]);
      if(Math.hypot(...a)<1e-6||Math.hypot(...b)<1e-6)return;
      turn(state.rotation_deg-(Math.atan2(b[1],b[0])-Math.atan2(a[1],a[0]))*180/Math.PI);
    } else {
      const delta=p.map((v,i)=>v-gesture.point[i]);
      state.offset_mm=state.offset_mm.map((v,i)=>v+delta[i]);pivot=pivot.map((v,i)=>v+delta[i]);
    }
    notify();
  });
  function finish(event){
    if(!gesture||event.pointerId!==gesture.id)return;
    if(event.type!=='pointerup'){state=gesture.state;pivot=gesture.pivot;notify();}
    gesture=null;if(stage.hasPointerCapture(event.pointerId))stage.releasePointerCapture(event.pointerId);
    changed();
  }
  stage.addEventListener('pointerup',finish);stage.addEventListener('pointercancel',finish);
  stage.addEventListener('lostpointercapture',finish);
  function clear(){
    if(gesture&&stage.hasPointerCapture(gesture.id))stage.releasePointerCapture(gesture.id);
    gesture=null;detail=null;state={rotation_deg:0,offset_mm:[0,0]};valid=true;
    tools.hidden=true;axis.hidden=true;stage.classList.remove('fp-adjustable');
    numbers.hidden=true;q('.fp-scene').classList.remove('fp-numbered-pads');
    root.querySelectorAll('[data-fp-pad-label-stack]').forEach(e=>e.remove());
    q('[data-fp-source-layers=converted]').style.transform='';
  }
  function setActive(value){
    active=value;
    tools.hidden=!(active&&enabled&&detail&&!detail.local_only&&!detail.library_comparison&&pads.existing.length&&pads.converted.length);
    axis.hidden=tools.hidden;stage.classList.toggle('fp-adjustable',!tools.hidden);
  }
  return {setComparison,clear,setActive,isValid:()=>valid&&!gesture&&geometryValid(),
    choice(){
      if(tools.hidden||!dirty())return {};
      return {alignment_transform:absoluteTransform()};
    }};
};
