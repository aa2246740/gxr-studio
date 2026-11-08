// 图库的真鼠标、按键、编辑器来回，以及截图边缘像素和源缩略图的比较。
export async function walkLibrary({ call, evalIn, step }) {
  const out = {};
  await call('Emulation.setDeviceMetricsOverride', { width: 1500, height: 950, deviceScaleFactor: 1, mobile: false });
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const until = async (expression, ms = 20000) => {
    const end = Date.now() + ms;
    while (Date.now() < end) {
      const value = await evalIn(expression);
      if (value) return value;
      await sleep(60);
    }
    const state = await evalIn(`import('/assets/viewer.js').then(m=>({hash:location.hash,photo:m.currentView()?.photo.id,body:document.body.textContent.slice(-1200)}))`);
    throw new Error(`图库等不到：${expression}；当前：${JSON.stringify(state)}`);
  };
  const click = async (selector, modifiers = 0) => {
    const pos = await evalIn(`(() => {
      const el = document.querySelector(${JSON.stringify(selector)});
      if (!el) throw new Error('找不到 ${selector}');
      el.scrollIntoView({block:'nearest'});
      const r = el.getBoundingClientRect();
      const x=r.x+r.width/2,y=r.y+r.height/2;
      if(!el.contains(document.elementFromPoint(x,y))) throw new Error('点击位置被挡住：${selector}');
      return {x,y};
    })()`);
    await call('Input.dispatchMouseEvent', { type: 'mousePressed', ...pos, button: 'left', clickCount: 1, modifiers });
    await call('Input.dispatchMouseEvent', { type: 'mouseReleased', ...pos, button: 'left', clickCount: 1, modifiers });
  };
  const press = async key => {
    await call('Input.dispatchKeyEvent', { type: 'rawKeyDown', key, code: key, windowsVirtualKeyCode: { Escape: 27, ArrowLeft: 37, ArrowRight: 39 }[key] });
    await call('Input.dispatchKeyEvent', { type: 'keyUp', key, code: key });
  };
  const selected = () => evalIn(`import('/assets/state.js').then(({state})=>[...state.selection])`);
  const label = () => evalIn(`document.getElementById('btn-export-top').textContent.trim()`);
  const cell = id => `#grid-wrap [data-id="${id}"]`;
  const viewer = id => until(`import('/assets/viewer.js').then(m=>{const v=m.currentView();return v?.photo.id===${JSON.stringify(id)} && !!v.ed && !!v.crop;})`);
  await until(`document.querySelectorAll('#grid-wrap [role="option"]').length>4`);
  out.order = await evalIn(`import('/assets/state.js').then(({state})=>state.photos.map(p=>p.id))`);
  out.apiOrder = await evalIn(`fetch('/api/photos').then(r=>r.json()).then(ps=>ps.map(p=>p.id))`);
  const [a,b,c,d] = out.order;
  out.defaultLabel = await label();
  out.hdrChips = await evalIn(`document.querySelectorAll('#grid-wrap .hdr').length`);
  step('library plain click and editor navigation');
  await click(`${cell(a)} .ck`);
  out.tick = await selected();
  out.selectedLabel = await label();
  step(`library open second ${b}`);
  await click(cell(b));
  await viewer(b);
  out.plainOpened = true;
  out.selectionInEditor = await selected();
  step(`library next ${c}`);
  await click('.viewbar button[title^="下一张"]');
  await viewer(c);
  out.next = c;
  step(`library previous ${b}`);
  await click('.viewbar button[title^="上一张"]');
  await viewer(b);
  step(`library keyboard previous ${a}`);
  await press('ArrowLeft');
  await viewer(a);
  out.previous = a;
  await press('Escape');
  await until(`!!document.getElementById('grid-wrap') && document.querySelectorAll('#grid-wrap [role="option"]').length>4`);
  out.back = await selected();
  await click(cell(c), 4);
  out.command = await selected();
  await click(cell(d), 8);
  out.shift = await selected();
  step('library date clear, export scope and undo');
  const date = await evalIn(`import('/assets/state.js').then(({state})=>state.dates.find(d=>d.date!=='unknown'))`);
  await evalIn(`document.querySelectorAll('#dates button').forEach(b=>{if(b.textContent.startsWith(${JSON.stringify(date.date)})) b.click();})`);
  await until(`document.getElementById('lib-count')?.textContent===${JSON.stringify(`${date.count} 张`)}`);
  out.dateSelection = await selected();
  out.date = date;
  out.dateLabel = await label();
  out.clearToast = await evalIn(`document.getElementById('toast-root').textContent`);
  await click('#btn-export-top');
  await until(`!!document.querySelector('#modal-root [role="dialog"]')`);
  out.exportDialog = await evalIn(`document.querySelector('#modal-root [role="dialog"]').textContent`);
  out.exportFolder = await evalIn(`document.querySelector('input[aria-label="导出到"]').value`);
  out.expectedFolder = await evalIn(`import('/assets/state.js').then(({state})=>state.settings.export_dir)`);
  await press('Escape');
  await evalIn(`document.querySelectorAll('#toast-root button').forEach(b=>{if(b.textContent==='恢复选择') b.click();})`);
  await until(`import('/assets/state.js').then(({state})=>state.galleryDate===null && state.selection.size===2)`);
  out.restored = await selected();
  await press('Escape');
  out.escape = await selected();
  await click(`${cell(a)} .ck`);
  await evalIn(`document.querySelector(${JSON.stringify(cell(a))}).focus()`);
  await press('ArrowRight');
  await press('Enter');
  await viewer(b);
  out.gridKeyboardOpened = b;
  await press('Escape');
  await until(`!!document.querySelector('#grid-wrap [role="option"]')`);
  await press('Escape');
  step('library cover pixels');
  out.cover = [];
  const samples = await evalIn(`(async()=>{
    const result=[];
    for(const el of document.querySelectorAll('#grid-wrap [role="option"]')) {
      const img=el.querySelector('img'); img.loading='eager'; await img.decode();
      const ratio=img.naturalWidth/img.naturalHeight;
      if(Math.abs(ratio-1.5)>0.15) result.push(el.dataset.id);
    }
    return result.slice(0,2);
  })()`);
  for (const id of samples) {
    const clip = await evalIn(`(() => {
      const el=document.querySelector(${JSON.stringify(cell(id))}); el.scrollIntoView({block:'center'});
      for(const tag of el.querySelectorAll('.ck,.b,.e')) tag.style.visibility='hidden';
      const r=el.getBoundingClientRect(); return {x:r.x,y:r.y,width:r.width,height:r.height,scale:1};
    })()`);
    await sleep(250);
    const { data } = await call('Page.captureScreenshot', { format: 'png', clip });
    out.cover.push(await evalIn(`(async()=>{
      const img=document.querySelector(${JSON.stringify(`${cell(id)} img`)});
      const shot=new Image(); shot.src='data:image/png;base64,${data}'; await shot.decode();
      const w=shot.naturalWidth,h=shot.naturalHeight;
      const canvas=document.createElement('canvas'); canvas.width=w; canvas.height=h;
      const ctx=canvas.getContext('2d',{willReadFrequently:true});
      const scale=Math.max(w/img.naturalWidth,h/img.naturalHeight);
      const sw=img.naturalWidth*scale,sh=img.naturalHeight*scale;
      ctx.drawImage(img,(w-sw)/2,(h-sh)/2,sw,sh);
      const points=[[3,Math.floor(h/2)],[w-4,Math.floor(h/2)],[Math.floor(w/2),3],[Math.floor(w/2),h-4]];
      const expected=points.map(([x,y])=>[...ctx.getImageData(x,y,1,1).data].slice(0,3));
      ctx.clearRect(0,0,w,h);ctx.drawImage(shot,0,0);
      const actual=points.map(([x,y])=>[...ctx.getImageData(x,y,1,1).data].slice(0,3));
      return {id:${JSON.stringify(id)},width:w,height:h,source:[img.naturalWidth,img.naturalHeight],expected,actual,
        maxError:Math.max(...actual.flatMap((rgb,i)=>rgb.map((v,c)=>Math.abs(v-expected[i][c]))))};
    })()`));
  }
  step('library full-image preference');
  out.heightBefore = await evalIn(`document.querySelector('#grid-wrap .cell').getBoundingClientRect().height`);
  await evalIn(`document.querySelectorAll('#libbar button').forEach(b=>{if(b.textContent==='完整显示') b.click();})`);
  out.heightAfter = await evalIn(`document.querySelector('#grid-wrap .cell').getBoundingClientRect().height`);
  await call('Page.reload');
  await until(`document.querySelectorAll('#grid-wrap [role="option"]').length>4`);
  out.fullPersisted = await evalIn(`document.querySelector('#grid-wrap').classList.contains('show-full') && getComputedStyle(document.querySelector('#grid-wrap img')).objectFit==='contain'`);
  return out;
}

// 导入后的照片列表先到，日期汇总晚到：按钮和真正的导出对话框都要跟上完整列表。
export async function walkLibraryRefresh({ call, evalIn, step }) {
  const until = async expression => {
    const end = Date.now() + 20000;
    while (Date.now() < end) {
      if (await evalIn(expression)) return;
      await new Promise(r => setTimeout(r, 30));
    }
    throw new Error(`日期刷新等不到：${expression}`);
  };
  await until(`import('/assets/state.js').then(({state}) => state.photosComplete && !state.job?.running)`);
  step('set up a day with two photos');
  const out = await evalIn(`(async () => {
    const { state } = await import('/assets/state.js');
    const { renderGallery } = await import('/assets/gallery.js');
    const realFetch = window.fetch.bind(window);
    const photos = await (await realFetch('/api/photos')).json();
    if (photos.length < 5) throw new Error('日期刷新夹具需要五张不同的照片');
    const data = window.__dayRefresh = {
      date: '2026-11-05', list: photos.slice(0, 2), next: photos.slice(0, 5), pending: [], hold: false,
      restore: () => { window.fetch = realFetch; },
    };
    const response = value => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } });
    window.fetch = (input, options) => {
      const url = new URL(input.url || input, location.href);
      if (url.pathname === '/api/dates') {
        const reply = () => response([{ date: data.date, count: data.list.length }]);
        return data.hold ? new Promise(resolve => data.pending.push(() => resolve(reply()))) : Promise.resolve(reply());
      }
      if (url.pathname === '/api/photos' && url.searchParams.get('date') === data.date) {
        const offset = Number(url.searchParams.get('offset') || 0);
        return Promise.resolve(response(data.list.slice(offset, offset + 500).map(p => ({ ...p, date: data.date }))));
      }
      return realFetch(input, options);
    };
    state.galleryDate = data.date;
    state.selection.clear();
    await renderGallery(document.getElementById('view'));
    return { initialHeader: document.getElementById('btn-export-top').textContent.trim(), folder: state.settings.export_dir };
  })()`);
  const snapshot = () => evalIn(`import('/assets/state.js').then(({state}) => ({
    header: document.getElementById('btn-export-top').textContent.trim(),
    gridCount: document.querySelectorAll('#grid-wrap [role="option"]').length,
    datesCount: state.dates.find(d => d.date === state.galleryDate)?.count,
    sidebarCount: document.querySelector('#dates .it[aria-pressed="true"] .n')?.textContent,
    complete: state.photosComplete,
  }))`);
  const dialog = async () => {
    await evalIn(`document.getElementById('btn-export-top').click(); true`);
    await until(`!!document.querySelector('#modal-root [role="dialog"]')`);
    const result = await evalIn(`(() => {
      const scope = [...document.querySelectorAll('.export-form .row.form')].find(row => row.querySelector('.lab')?.textContent === '范围');
      return { scope: scope?.querySelector('.ctl')?.textContent.trim() ?? null,
        folder: document.querySelector('input[aria-label="导出到"]').value };
    })()`);
    await call('Input.dispatchKeyEvent', { type: 'rawKeyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 });
    await call('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Escape', code: 'Escape' });
    await until(`!document.querySelector('#modal-root [role="dialog"]')`);
    return result;
  };
  try {
    step('refresh five photos while the dates response is held');
    await evalIn(`import('/assets/state.js').then(({emit}) => {
      window.__dayRefresh.hold = true;
      window.__dayRefresh.list = window.__dayRefresh.next;
      emit('library-changed');
    })`);
    await until(`import('/assets/state.js').then(({state}) => state.photosComplete && state.photos.length === 5 && window.__dayRefresh.pending.length > 0)`);
    out.beforeDates = await snapshot();
    out.beforeDates.dialog = await dialog();
    step('release the late dates response');
    await evalIn(`(() => {
      const data = window.__dayRefresh;
      data.hold = false;
      data.pending.splice(0).forEach(resolve => resolve());
    })()`);
    await until(`document.querySelector('#dates .it[aria-pressed="true"] .n')?.textContent === '5'`);
    out.afterDates = await snapshot();
    out.afterDates.dialog = await dialog();
    return out;
  } finally {
    await evalIn(`window.__dayRefresh.restore()`);
  }
}
