// Mirror of finish/frame_objects.py, approved R3 object frames.
import { pyRound } from './finish_layout.js';
import { Type,join,frameIndex,makeUnit,solve,rotateUnitCcw } from './finish_frame_core.js';
export const INSTANT_FORMATS={polaroid:[0.057,0.062,0.290],instax_square:[0.081,0.081,0.306],instax_wide:[0.073,0.065,0.320],instax_mini:[0.087,0.087,0.430]};
function tri(x,mid,size,fill) {
  return {k:'poly',pts:[[pyRound(x),pyRound(mid-size/2)],[pyRound(x),pyRound(mid+size/2)],[pyRound(x+size*0.9),pyRound(mid)]],fill};
}
export function film(w,h,spec,meta,m,ratio) {
  const t=new Type(m),portrait=h>w,[a,c]=portrait?[h,w]:[w,h],short=c,light=spec.table!=='black',kind=spec.fmt;
  const tableCol=light?[243,241,236]:[24,24,26],stripCol=[13,12,12],amber=kind==='135'?[214,140,56]:[210,148,66],n=frameIndex(meta);
  function unit() {
    let rebate,e,hw,hh,pitch,hy,markMid,px;
    if(kind==='135') {
      rebate=pyRound(0.229*short);e=pyRound(0.1*short); hw=pyRound(0.117*short);hh=pyRound(0.0825*short);pitch=pyRound(0.198*short);
      hy=pyRound(0.083*short-hh/2);markMid=(hy+hh+rebate)/2;px=t.pxForCap('inter_sb',0.026*short);
    } else {
      e=pyRound(0.07*short);px=t.pxForCap('inter_sb',0.017*short);rebate=Math.max(pyRound(0.065*short),pyRound(3.2*t.capH('inter_sb',px)));markMid=rebate/2;
    }
    const wl=a+2*e,hl=c+2*rebate;
    const under=[
      {k:'shadow',x:0,y:0,w:wl,h:hl,blur:Math.max(6,pyRound(0.024*short)),dy:pyRound(0.012*short),alpha:light?0.4:0,radius:pyRound(0.006*short)},
      {k:'rect',x:0,y:0,w:wl,h:hl,fill:stripCol,radius:pyRound(0.006*short)},
      {k:'rect',x:1,y:1,w:wl-2,h:hl-2,fill:null,outline:[46,42,38],stroke_w:1,radius:pyRound(0.006*short)}];
    if(kind==='135') {
      const cnt=Math.floor((wl-2*pyRound(0.03*short))/pitch),x0=Math.floor((wl-((cnt-1)*pitch+hw))/2);
      for(let i=0;i<cnt;i++) for(const yy of [hy,hl-hy-hh]) {
        under.push(light?{k:'hole',x:x0+i*pitch,y:yy,w:hw,h:hh,radius:pyRound(0.2*hh)}:
          {k:'rect',x:x0+i*pitch,y:yy,w:hw,h:hh,fill:[232,228,218],radius:pyRound(0.2*hh)});
      }
    } else for(const yy of [pyRound(0.012*short),hl-pyRound(0.012*short)]) under.push({k:'line',x1:pyRound(0.03*short),y1:yy,x2:wl-pyRound(0.03*short),y2:yy,w:1,fill:[40,37,34]});
    const over=[{k:'rect',x:e-1,y:rebate-1,w:a+2,h:c+2,fill:null,outline:[30,28,26],stroke_w:1}];
    const xl=kind==='135'?pyRound(0.05*short):e,xr=wl-(kind==='135'?pyRound(0.05*short):e),ts=Math.max(8,pyRound(0.62*t.capH('inter_sb',px)));
    const topMid=markMid,botMid=hl-markMid,topL=kind==='135'?'RICOH GXR 135':'RICOH GXR 120';
    over.push(t.capText(xl,topMid,px,'inter_sb',topL,amber,0.08));
    const label=String(kind==='135'?n:(n-1)%12+1);
    if(kind==='135') {
      const cx=wl*0.52;
      over.push(tri(cx-ts*1.4,topMid,ts,amber),t.capText(cx,topMid,px,'inter_sb',label,amber,0.06));
      over.push(t.capText(xr,topMid,px,'inter_sb',`${label}A`,amber,0.06,'r'));
      over.push(t.capText(xl,botMid,px,'inter_sb',join(['RICOH GXR',meta.lens_short||''],'  ·  '),amber,0.06));
      over.push(tri(cx-ts*1.4,botMid,ts,amber),t.capText(cx,botMid,px,'inter_sb',`${label}A`,amber,0.06));
      over.push(t.capText(xr,botMid,px,'inter_sb',meta.date||'',amber,0.06,'r'));
    } else {
      over.push(tri(xr-t.advance('inter_sb',px,label,0.06)-ts*1.5,topMid,ts,amber),t.capText(xr,topMid,px,'inter_sb',label,amber,0.06,'r'));
      const line=join(['lens_short','aperture','shutter','iso'].map(k=>meta[k]||''),'   ');
      over.push(t.capText(xl,botMid,px,'inter_sb',line,amber,0.05),t.capText(xr,botMid,px,'inter_sb',meta.date||'',amber,0.05,'r'));
    }
    const u=makeUnit(wl,hl,[e,rebate],a,c,{under,over,bg:tableCol,bgfx:{kind:'glow',falloff:light?0.07:0}});
    u.bgfx_always=true;
    if(portrait) for(const item of over) if(item.k==='text') item.tw=Math.ceil(t.advance(item.font,item.px,item.text,item.track))+2;
    return portrait?rotateUnitCcw(u):u;
  }
  const margin=pyRound(0.07*short);
  return solve(unit,ratio,w,{natural:[margin,margin,margin],f_side:0.06,f_top:0.06,f_bot:0.06,split:0.50});
}
export function instant(w,h,spec,meta,m,ratio) {
  const t=new Type(m),short=Math.min(w,h),[rs,rt,rc]=INSTANT_FORMATS[spec.fmt];
  const side=Math.max(22,pyRound(rs*short)),top=Math.max(22,pyRound(rt*short)),chin=Math.max((spec.fmt==='polaroid'?4:3)*side,pyRound(rc*short));
  const ivory=[250,248,243],stone=[232,228,220],ink=[43,43,43];
  function unit(wref) {
    const sz=t.sizes(wref),w0=w+2*side,h0=h+top+chin,rad=pyRound(0.023*w0);
    const under=[
      {k:'shadow',x:0,y:0,w:w0,h:h0,blur:Math.max(8,pyRound(0.02*short)),dy:Math.max(4,pyRound(0.012*short)),alpha:0.30,radius:rad,fx_only:true},
      {k:'rect',x:0,y:0,w:w0,h:h0,fill:ivory,outline:[214,210,200],stroke_w:1,radius:rad,fx_only:true},
      {k:'rect',x:side-1,y:top-1,w:w+2,h:h+2,fill:null,outline:[222,218,208],stroke_w:1}];
    const over=[],cy=top+h+0.46*chin;
    if(spec.text) {
      let px=Math.max(pyRound(0.15*chin),sz.p2('patrick'));px=Math.max(14,Math.min(px,99));
      const caption=t.clipText(spec.text,'patrick',px,w-2*side,0.01);
      over.push(t.capText(w0/2,cy-(spec.show_date?0.10*chin:0),px,'patrick',caption,ink,0.01,'c'));
    }
    if(spec.show_date&&meta.date) {
      const dpx=Math.max(pyRound(0.105*chin),sz.p2('patrick'));
      if(spec.text) over.push(t.capText(w0-side,top+h+0.80*chin,dpx,'patrick',meta.date,[96,94,90],0.02,'r'));
      else over.push(t.capText(w0/2,cy,dpx,'patrick',meta.date,ink,0.02,'c'));
    }
    return makeUnit(w0,h0,[side,top],w,h,{under,over,bg:ivory,table:stone});
  }
  return solve(unit,ratio,w,{natural:[0,0,0],f_side:0.06,f_top:0.06,f_bot:0.06,split:0.47});
}
