// Mirror of finish/frame_styles.py, approved R3 plain-canvas styles.
import { pyRound } from './finish_layout.js';
import { COLORS,Type,join,makeUnit,solve } from './finish_frame_core.js';
export const GALLERY_WEIGHTS={thin:[0.035,1.20,0.06],medium:[0.060,1.25,0.09],thick:[0.100,1.28,0.13]};
function palette(color) {
  const dark=color==='black',cream=color==='cream';
  return {bg:COLORS[color],ink:dark?[244,244,246]:[22,22,26],muted:dark?[160,160,168]:(cream?[112,106,96]:[112,112,120]),rule:dark?[82,82,90]:[204,202,196]};
}
export function gallery(w,h,spec,meta,m,ratio) {
  const short=Math.min(w,h),[rs,b,f]=GALLERY_WEIGHTS[spec.weight],ms=Math.max(14,pyRound(rs*short)),mb=Math.max(18,pyRound(ms*b));
  return solve(()=>makeUnit(w,h,[0,0],w,h,{bg:COLORS[spec.color]}),ratio,w,{natural:[ms,ms,mb],f_side:f,f_top:f,f_bot:f,split:0.45});
}
export function leica(w,h,spec,meta,m,ratio) {
  const t=new Type(m),short=Math.min(w,h),pal=palette(spec.color),m0=Math.max(18,pyRound(0.038*short)),baseBar=Math.max(84,pyRound(0.125*short),pyRound(0.075*w));
  function unit(wref) {
    const sz=t.sizes(wref),typeBar=Math.max(baseBar,pyRound(0.082*wref)),px1=Math.max(sz.p1('inter_sb'),pyRound(0.17*baseBar)),px2=Math.max(sz.p2('inter'),pyRound(0.13*baseBar));
    const gxrPx=Math.max(sz.cap('inter_sb',0.0155),pyRound(0.16*baseBar)),cap1=t.capH('inter_sb',px1),cap2=t.capH('inter',px2),gapY=Math.max(7,pyRound(0.72*cap2));
    let l1=spec.show_params?join(['lens_short','aperture','shutter','iso'].map(k=>meta[k]||'')):'';
    const main=spec.show_date?(meta.date||''):(spec.show_params&&meta.lens!==meta.lens_short?(meta.lens||''):'');
    let l2=join([main,spec.text],'  ·  ');
    if(!l1&&l2) [l1,l2]=[l2,''];
    const gxrCap=t.capH('inter_sb',gxrPx),lh=Math.max(14,pyRound(gxrCap*1.04*433/412)),lw=pyRound(2400*lh/433),gapLogo=Math.max(10,pyRound(0.55*gxrCap));
    const mw=t.advance('inter_sb',gxrPx,'GXR',0.02),leftW=spec.show_logo?lw+gapLogo+mw:0,ruleGap=Math.max(12,pyRound(0.9*cap1));
    const widths=(a,b)=>Math.max(t.advance('inter_sb',px1,a,0.01),b?t.advance('inter',px2,b,0.02):0);
    const avail=w-leftW-ruleGap-pyRound(1.2*cap1)-8;
    if(l1&&widths(l1,l2)>avail&&meta.lens_short&&spec.show_params&&l1.startsWith(meta.lens_short)) {
      l1=join(['aperture','shutter','iso'].map(k=>meta[k]||'')); l2=join([meta.lens_short,l2],'  ·  ');
    }
    const stackH=l2?cap1+gapY+cap2:cap1,need=l1?leftW+ruleGap+pyRound(widths(l1,l2))+pyRound(1.2*cap1)+8:leftW;
    const uw=Math.max(w,pyRound(need)),bar=Math.max(typeBar,pyRound(stackH*2.3),pyRound(gxrCap*3.2),spec.show_logo?pyRound(lh*1.9):0),ym=h+0.49*bar,items=[];
    if(spec.show_logo) {
      const logoMid=lh*(9+420)/(2*433);
      items.push({k:'logo',x:0,y:pyRound(ym-logoMid),w:lw,h:lh});
      items.push(t.capText(lw+gapLogo,ym,gxrPx,'inter_sb','GXR',pal.ink,0.02));
    }
    if(l1) {
      const tx=uw-widths(l1,l2),top=ym-stackH/2;
      items.push(t.baseText(tx,top+cap1,px1,'inter_sb',l1,pal.ink,0.01));
      if(l2) items.push(t.baseText(tx,top+cap1+gapY+cap2,px2,'inter',l2,pal.muted,0.02));
      const rx=pyRound(tx-ruleGap);
      if(rx>leftW+8) items.push({k:'line',x1:rx,y1:pyRound(top),x2:rx,y2:pyRound(top+stackH),w:Math.max(1,pyRound(0.016*typeBar)),fill:pal.rule});
    }
    return makeUnit(uw,h+bar,[Math.floor((uw-w)/2),0],w,h,{over:items,bg:pal.bg});
  }
  return solve(unit,ratio,w,{natural:[m0,m0,0],f_side:0.06,f_top:0.06,f_bot:0.04,split:0.46});
}
export function hasselblad(w,h,spec,meta,m,ratio) {
  const t=new Type(m),short=Math.min(w,h),pal=palette(spec.color),accent=spec.color==='black'?[202,154,88]:[188,138,72];
  const m0=Math.max(18,pyRound(0.036*short)),baseBar=Math.max(96,pyRound(0.145*short),pyRound(0.085*w));
  function unit(wref) {
    const sz=t.sizes(wref),typeBar=Math.max(baseBar,pyRound(0.095*wref)),px1=Math.max(sz.p1('inter_sb'),pyRound(0.15*baseBar)),px2=Math.max(sz.p2('inter'),pyRound(0.115*baseBar));
    const cap1=t.capH('inter_sb',px1),cap2=t.capH('inter',px2),bits=spec.show_params?['lens_short','aperture','shutter','iso'].map(k=>meta[k]||''):[];
    const tail=[spec.show_date?(meta.date||''):'',spec.text],one=join([...bits,...tail],'  ·  ');
    let lines=one?[one]:[];
    if(one&&t.advance('inter',px2,one,0.03)>w-pyRound(0.08*short)) lines=[join(bits,'  ·  '),join(tail,'  ·  ')].filter(Boolean);
    const g=Math.max(8,pyRound(0.7*cap1)),ruleH=Math.max(2,pyRound(0.012*typeBar)),lgap=Math.max(6,pyRound(0.55*cap2));
    const subH=lines.length*cap2+Math.max(0,lines.length-1)*lgap,hdrH=cap1,hdrW=t.advance('inter_sb',px1,'RICOH GXR',0.26);
    const stack=hdrH+(lines.length?g+ruleH+g+subH:0),bar=Math.max(typeBar,pyRound(stack*2.1)),top=h+0.47*bar-stack/2;
    const widest=Math.max(...lines.map(ln=>t.advance('inter',px2,ln,0.03)),hdrW),uw=Math.max(w,pyRound(widest)+pyRound(0.08*short)),cx=uw/2;
    const items=[t.baseText(cx,top+cap1,px1,'inter_sb','RICOH GXR',pal.ink,0.26,'c')];
    if(lines.length) {
      const rw=Math.max(28,pyRound(0.28*typeBar)),ry=pyRound(top+hdrH+g);
      items.push({k:'line',x1:pyRound(cx-rw/2),y1:ry,x2:pyRound(cx+rw/2),y2:ry,w:ruleH,fill:accent});
      let y=top+hdrH+g+ruleH+g;
      for(const ln of lines) { items.push(t.baseText(cx,y+cap2,px2,'inter',ln,pal.muted,0.03,'c'));y+=cap2+lgap; }
    }
    return makeUnit(uw,h+bar,[Math.floor((uw-w)/2),0],w,h,{over:items,bg:pal.bg});
  }
  return solve(unit,ratio,w,{natural:[m0,m0,0],f_side:0.06,f_top:0.06,f_bot:0.04,split:0.46});
}
