// Mirror of finish/frame_core.py. Ordered underlays → photo → overlays.
import { pyRound } from './finish_layout.js';
export const COLORS = { white: [255,255,255], black: [14,14,16], cream: [244,239,230] };
export const join = (parts, sep = '  ') => parts.filter(Boolean).join(sep);
export class Type {
  constructor(m) { this.m = m; }
  pens(font, text, track = 0) {
    const chars = Array.from(text), upm = this.m.upm(font, false);
    const extra = chars.some(ch => ch.codePointAt(0) >= 0x2e80) ? 0 : pyRound(track * upm);
    const pens = [0];
    chars.forEach((ch,i) => {
      const cjk = ch.codePointAt(0) >= 0x2e80;
      const adv = this.m.glyph(font,ch,cjk)[0] * upm / this.m.upm(font,cjk);
      pens.push(pens.at(-1) + adv + (i < chars.length-1 ? extra : 0));
    });
    return pens;
  }
  advance(font,px,text,track=0) { return this.pens(font,text,track).at(-1)*px/this.m.upm(font,false); }
  capBox(font,px) {
    const box=this.m.glyph(font,'H',false)[1], scale=px/this.m.upm(font,false), asc=this.m.ascent(font,px,false);
    return [Math.floor(asc-box[3]*scale),Math.ceil(asc-box[1]*scale)];
  }
  capH(font,px) { const [top,bottom]=this.capBox(font,px); return bottom-top; }
  pxForCap(font,target) { let px=8; while(this.capH(font,px)<target) px++; return px; }
  sizes(wref) { return { p1: f=>this.pxForCap(f,0.015*wref), p2:f=>this.pxForCap(f,0.011*wref), cap:(f,r)=>this.pxForCap(f,r*wref) }; }
  text(x,y,px,font,text,fill,track=0) {
    if(Array.from(text).some(ch=>ch.codePointAt(0)>=0x2e80)) track=0;
    return { k:'text',x,y:Math.floor(y+0.5),px,font,text,fill,track,cjk:false,
      xs:this.pens(font,text,track).slice(0,-1).map(p=>Math.floor(x+p*px/this.m.upm(font,false)+0.5)) };
  }
  capText(x,mid,px,font,text,fill,track=0,anchor='l') {
    const width=this.advance(font,px,text,track), x0=anchor==='l'?x:x-(anchor==='c'?width/2:width), [top,bottom]=this.capBox(font,px);
    return this.text(x0,mid-(top+bottom)/2,px,font,text,fill,track);
  }
  baseText(x,baseline,px,font,text,fill,track=0,anchor='l') {
    const width=this.advance(font,px,text,track), x0=anchor==='l'?x:x-(anchor==='c'?width/2:width);
    return this.text(x0,baseline-this.m.ascent(font,px,false),px,font,text,fill,track);
  }
  clipText(text,font,px,maxW,track=0) {
    if(this.advance(font,px,text,track)<=maxW) return text;
    let chars=Array.from(text);
    while(chars.length>1 && this.advance(font,px,chars.join('')+'...',track)>maxW) chars.pop();
    return chars.join('').trimEnd()+'...';
  }
}
export function frameIndex(meta) {
  const text=meta.frame_no||'0';
  const n=/^[+-]?\d+$/.test(text)?Number(text):0;
  return n ? ((n-1)%36+36)%36+1 : 1;
}
export function shiftItems(items,dx,dy) {
  return items.map(orig=>{
    const it={...orig};
    for(const key of ['x','x1','x2','cx','gx']) if(key in it) it[key]+=dx;
    for(const key of ['y','y1','y2','cy','gy']) if(key in it) it[key]+=dy;
    if('xs' in it) it.xs=it.xs.map(x=>x+dx);
    for(const key of ['pts','hole_pts']) if(key in it) it[key]=it[key].map(p=>[p[0]+dx,p[1]+dy]);
    return it;
  });
}
export function makeUnit(w,h,[px,py],pw,ph,{under=[],over=[],bg=[255,255,255],table=null,bgfx=null,grain=0}={}) {
  return {w,h,px,py,pw,ph,under,over,bg,table,bgfx,grain};
}
export function assemble(u,width,height,x,y,fixed) {
  const under=u.under.filter(i=>fixed||!i.fx_only),over=u.over.filter(i=>fixed||!i.fx_only);
  const photo={k:'photo',x:u.px+x,y:u.py+y,w:u.pw,h:u.ph};
  return {width,height,photo:[photo.x,photo.y],bg:fixed&&u.table!==null?u.table:u.bg,
    bgfx:fixed||u.bgfx_always?u.bgfx:null, items:[...shiftItems(under,x,y),photo,...shiftItems(over,x,y)],
    unit_box:[x,y,u.w,u.h],fixed,grain:u.grain||0};
}
export function solve(unitFn,ratio,wHint,{natural,f_side:fSide=0.06,f_top:fTop=0.06,f_bot:fBottom=0.04,split=0.46}) {
  const [ms,mt,mb]=natural,capRef=pyRound(1.9*wHint);
  if(!ratio) {
    let u=unitFn(wHint+2*ms);
    for(let i=0;i<2;i++) u=unitFn(Math.min(u.w+2*ms,capRef));
    return assemble(u,u.w+2*ms,u.h+mt+mb,ms,mt,false);
  }
  const [rw,rh]=ratio;
  let u=unitFn(wHint);
  let k=Math.max(Math.ceil(u.w*100/(pyRound((1-2*fSide)*100)*rw)),Math.ceil(u.h*100/(pyRound((1-fTop-fBottom)*100)*rh)),1);
  let width,height,tm,bm;
  while(true) {
    width=k*rw; height=k*rh;
    u=unitFn(Math.min(Math.max(width,pyRound(0.62*height)),capRef));
    const sm=Math.ceil(fSide*width); tm=Math.ceil(fTop*height); bm=Math.ceil(fBottom*height);
    if(width-u.w>=2*sm && height-u.h>=tm+bm) break;
    k++;
  }
  const free=height-u.h,y=Math.max(tm,Math.min(free-bm,pyRound(split*free)));
  return assemble(u,width,height,Math.floor((width-u.w)/2),y,true);
}
export function rotateUnitCcw(unit) {
  const wl=unit.w;
  const rot=it=>{
    const n={...it};
    if(['rect','hole','shadow'].includes(it.k)) {
      [n.x,n.y,n.w,n.h]=[it.y,wl-it.x-it.w,it.h,it.w];
      if('dy' in it) [n.dx,n.dy]=[it.dy,0];
    } else if(it.k==='poly') n.pts=it.pts.map(p=>[p[1],wl-p[0]]);
    else if(it.k==='line') [n.x1,n.y1,n.x2,n.y2]=[it.y1,wl-it.x1,it.y2,wl-it.x2];
    else if(it.k==='text') Object.assign(n,{rot:true,gx:Math.trunc(it.y),gy:wl-pyRound(it.x)-it.tw,xs0:it.xs.map(x=>x-pyRound(it.x))});
    return n;
  };
  return {...unit,w:unit.h,h:wl,px:unit.py,py:wl-unit.px-unit.pw,pw:unit.ph,ph:unit.pw,under:unit.under.map(rot),over:unit.over.map(rot)};
}

export const LOGO_STYLES = ['leica','hasselblad','thin_keyline','exhibition_label','cinema','slide_mount','postage_stamp','blur_fill'];
export const SIZE_MULT = { small: 0.80, medium: 1.00, large: 1.25 };
const ASPECT_PTS = [[0.5,2.6],[1.0,2.5],[1.5,2.5],[2.5,2.3],[4.0,1.5],[6.0,1.04],[9.0,0.88]];

export function aspectMult(aspect) {
  if (aspect <= ASPECT_PTS[0][0]) return ASPECT_PTS[0][1];
  for (let i = 0; i < ASPECT_PTS.length - 1; i++) {
    const [a0, m0] = ASPECT_PTS[i], [a1, m1] = ASPECT_PTS[i + 1];
    if (aspect <= a1) return m0 + ((m1 - m0) * (aspect - a0)) / (a1 - a0);
  }
  return ASPECT_PTS.at(-1)[1];
}

export function coverageComp(coverage) {
  return Math.min(1.25, Math.max(0.90, (0.35 / Math.max(coverage, 0.02)) ** 0.25));
}

export function fitLogo(logo, cap, maxH, maxW, size = 'medium') {
  const aspect = logo.w / Math.max(1, logo.h);
  const mult = SIZE_MULT[size] ?? 1.0;
  let targetH = cap * aspectMult(aspect) * coverageComp(logo.coverage) * mult;
  targetH = Math.min(targetH, maxH, maxW / aspect);
  const ih = Math.max(8, pyRound(targetH));
  return [Math.max(8, pyRound(ih * aspect)), ih];
}

export function pickLogoMode(logo, mode, bgLum) {
  if (logo.opaque) return 'orig';
  if (mode === 'mono' || mode === 'orig') return mode;
  return Math.abs(logo.lum - bgLum) < 70 ? 'mono' : 'orig';
}

export function clogoItem(logo, x, y, w, h, mode, ink, bgLum) {
  return {
    k: 'clogo',
    id: logo.id,
    x,
    y,
    w,
    h,
    mode: pickLogoMode(logo, mode, bgLum),
    ink: [...ink],
    auto: mode === 'auto' && !logo.opaque,
  };
}

let logoList = [];
let logoMap = Object.create(null);
let loaded = false;
let logoRev = 0;
let loadingLogos = null;
const imgCache = new Map();

export function useLogos(list) {
  logoList = Array.isArray(list) ? list.map((x) => ({ ...x })) : [];
  logoMap = Object.create(null);
  for (const item of logoList) if (item && item.id) logoMap[item.id] = item;
  for (const key of [...imgCache.keys()]) if (!logoMap[key]) imgCache.delete(key);
  loaded = true;
  logoRev += 1;
  return logoList;
}

export const logosLoaded = () => loaded;
export const logosRevision = () => logoRev;
export const getLogoList = () => logoList;
export const getLogoMap = () => logoMap;

export function ensureLogosLoaded() {
  if (loaded) return Promise.resolve(logoList);
  if (!loadingLogos) {
    loadingLogos = (async () => {
      const res = await fetch('/api/logos', { credentials: 'same-origin' });
      if (!res.ok) throw new Error(`http_${res.status}`);
      const data = await res.json();
      return useLogos((data && data.logos) || []);
    })().catch((e) => {
      loadingLogos = null;
      throw e;
    });
  }
  return loadingLogos;
}

export function customLogoImage(id, onReady) {
  if (!id || typeof Image !== 'function') return null;
  let rec = imgCache.get(id);
  if (!rec) {
    const img = new Image();
    rec = { img, loaded: false, waiting: [] };
    imgCache.set(id, rec);
    img.addEventListener('load', () => {
      rec.loaded = true;
      for (const fn of rec.waiting.splice(0)) fn();
    });
    img.addEventListener('error', () => { imgCache.delete(id); });
    img.src = `/api/logos/${encodeURIComponent(id)}/image`;
  }
  if (rec.loaded) return rec.img;
  if (onReady) rec.waiting.push(onReady);
  return null;
}
