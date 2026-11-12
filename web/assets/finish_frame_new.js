// Approved R3 geometry. Mirror finish/frame_new.py; textures are drawing operations.
import { Type, join, frameIndex, makeUnit, solve, COLORS, fitLogo, clogoItem } from './finish_frame_core.js';
import { pyRound as round } from './finish_layout.js';

function palette(color) {
  const dark = color === 'black';
  return { bg: COLORS[color] || COLORS.white, ink: dark ? [244,244,246] : [22,22,26],
    muted: dark ? [160,160,168] : color === 'cream' ? [112,106,96] : [112,112,120] };
}
const kindOf = (spec) => (spec._logoKind === 'custom' && spec._customLogo ? 'custom' : (spec._logoKind === 'none' ? 'none' : 'ricoh'));

export function thinKeyline(w,h,spec,meta,m,ratio) {
  const t=new Type(m),short=Math.min(w,h),color=spec.color||'cream',pal=palette(color);
  const line=color==='black'?[90,90,96]:[52,50,46],kind=kindOf(spec),custom=spec._customLogo,logoMode=spec.logo_mode||'auto',logoSize=spec.logo_size||'medium';
  const g=Math.max(10,round(0.017*short)),stroke=Math.max(2,round(0.0024*short)),ms=Math.max(36,round(0.080*short));
  const unit=wref=>{
    const sz=t.sizes(wref),px1=Math.max(sz.p1('inter_sb'),round(0.016*short)),px2=Math.max(sz.p2('inter'),round(0.015*short));
    const cap1=t.capH('inter_sb',px1),gap=Math.max(12,round(1.1*cap1)),uw=w+2*g,kh=h+2*g;
    const over=[{k:'rect',x:0,y:0,w:uw,h:kh,fill:null,outline:line,stroke_w:stroke}];
    let rowH,baseline,brandW;
    if(kind==='custom'&&custom) {
      const [lw,lh]=fitLogo(custom,cap1,round(3.4*cap1),round(0.30*uw),logoSize);
      rowH=Math.max(cap1,lh);
      const yMid=kh+gap+rowH/2,bgL=color==='black'?18:(color==='cream'?238:245);
      over.push(clogoItem(custom,0,round(yMid-lh/2),lw,lh,logoMode,pal.ink,bgL));
      brandW=lw; baseline=yMid+t.capH('inter',px2)/2;
    } else if(kind==='none') {
      rowH=cap1; baseline=kh+gap+cap1; brandW=0;
    } else {
      rowH=cap1; baseline=kh+gap+cap1;
      over.push(t.baseText(0,baseline,px1,'inter_sb','RICOH GXR',pal.ink,0.12));
      brandW=t.advance('inter_sb',px1,'RICOH GXR',0.12);
    }
    let right=join(['lens_short','aperture','shutter','iso','date'].map(k=>meta[k]),'  ·  ');
    const padLeft=brandW?brandW+40:0;
    while(right&&t.advance('inter',px2,right,0.02)+padLeft>uw) right=right.includes('  ·  ')?right.split('  ·  ').slice(0,-1).join('  ·  '):'';
    if(right) over.push(t.baseText(uw,baseline,px2,'inter',right,pal.muted,0.02,'r'));
    return makeUnit(uw,kh+gap+rowH+round(0.4*cap1),[g,g],w,h,{over,bg:pal.bg});
  };
  return solve(unit,ratio,w,{natural:[ms,ms,round(0.07*short)],f_side:0.07,f_top:0.07,f_bot:0.07,split:0.44});
}

export function exhibitionLabel(w,h,spec,meta,m,ratio) {
  const t=new Type(m),short=Math.min(w,h),pal=palette(spec.color||'white'),dark=spec.color==='black',text=spec.text||'';
  const cardBg=dark?[30,30,34]:[251,250,247],cardLine=dark?[62,62,70]:[220,216,208],ms=Math.max(40,round(0.085*short));
  const kind=kindOf(spec),custom=spec._customLogo,logoMode=spec.logo_mode||'auto',logoSize=spec.logo_size||'medium',ricoh=kind==='ricoh';
  const unit=wref=>{
    const sz=t.sizes(wref),px1=Math.max(sz.cap('inter_sb',0.017),round(0.022*short)),px2=Math.max(sz.cap('inter',0.013),round(0.018*short));
    const cap1=t.capH('inter_sb',px1),cap2=t.capH('inter',px2);
    let head=text||(ricoh?'RICOH GXR':'');
    const details=[...(ricoh&&text?['RICOH GXR']:[]),join([meta.lens_short,meta.aperture]),join([meta.shutter,meta.iso]),meta.date].filter(Boolean);
    const padX=round(1.3*px2),padY=round(1.1*px2),lgap=round(0.6*cap2);
    const want=Math.max(head?t.advance('inter_sb',px1,head,0.02):0,...details.map(d=>t.advance('inter',px2,d,0.02)));
    const cardW=Math.min(round(0.52*w),Math.max(round(0.26*w),Math.ceil(want)+2*padX+2));
    head=head?t.clipText(head,'inter_sb',px1,cardW-2*padX,0.02):'';
    const [lw,lh]=kind==='custom'&&custom?fitLogo(custom,cap1,round(3.2*cap1),cardW-2*padX,logoSize):[0,0];
    const headH=(kind==='custom'?lh+round(0.7*cap1):0)+(head?cap1+round(0.9*cap1):0);
    const detailGaps=ricoh?(details.length-1)*lgap:Math.max(0,details.length-1)*lgap;
    const cardH=2*padY+headH+details.length*cap2+detailGaps+round(0.3*cap2);
    const gap=Math.max(26,round(0.055*short)),cx=w-cardW,cy=h+gap;
    const over=[{k:'shadow',x:cx,y:cy,w:cardW,h:cardH,blur:Math.max(4,round(0.006*short)),dy:Math.max(2,round(0.004*short)),alpha:dark?0.5:0.2},
      {k:'rect',x:cx,y:cy,w:cardW,h:cardH,fill:cardBg,outline:cardLine,stroke_w:1}];
    let y=cy+padY;
    if(kind==='custom'&&custom) { over.push(clogoItem(custom,cx+padX,y,lw,lh,logoMode,pal.ink,dark?22:250)); y+=lh+round(0.7*cap1); }
    if(head) { over.push(t.baseText(cx+padX,y+cap1,px1,'inter_sb',head,pal.ink,0.02)); y+=cap1+round(0.9*cap1); }
    for(const d of details) { over.push(t.baseText(cx+padX,y+cap2,px2,'inter',d,pal.muted,0.02)); y+=cap2+lgap; }
    return makeUnit(w,h+gap+cardH,[0,0],w,h,{over,bg:pal.bg});
  };
  return solve(unit,ratio,w,{natural:[ms,ms,ms],f_side:0.07,f_top:0.07,f_bot:0.05,split:0.40});
}

export function blurFill(w,h,spec,meta,m,ratio) {
  const t=new Type(m),short=Math.min(w,h),m0=Math.max(28,round(0.065*short)),white=[248,248,250],soft=[226,228,232];
  const kind=kindOf(spec),custom=spec._customLogo,logoMode=spec.logo_mode||'auto',logoSize=spec.logo_size||'medium',showParams=spec.show_params!==false;
  const unit=wref=>{
    const sz=t.sizes(wref),under=[{k:'shadow',x:0,y:0,w,h,blur:Math.max(16,round(0.045*short)),dy:Math.max(8,round(0.022*short)),alpha:0.55}],over=[];
    let total=h;
    if(showParams||kind==='custom') {
      const px1=Math.max(sz.p1('inter_sb'),round(0.02*short)),px2=Math.max(sz.p2('inter'),round(0.017*short));
      const cap1=t.capH('inter_sb',px1),cap2=t.capH('inter',px2),sub=join(['lens_short','aperture','shutter','iso'].map(k=>meta[k]),'  ·  ');
      const gap0=Math.max(22,round(1.9*cap1)),g=Math.max(8,round(0.8*cap2)),y=h+gap0;
      if(kind==='custom'&&custom) {
        const [lw,hdrH]=fitLogo(custom,cap1,round(3.4*cap1),round(0.30*w),logoSize);
        over.push({...clogoItem(custom,round(w/2-lw/2),round(y),lw,hdrH,logoMode,white,128),auto_ink:[white,[28,28,32]]});
        if(showParams) {
          over.push({...t.baseText(w/2,y+hdrH+g+cap2,px2,'inter',sub,soft,0.04,'c'),auto_ink:[soft,[52,52,58]]});
          total=y+hdrH+g+cap2;
        } else total=y+hdrH;
      } else if(kind==='ricoh') {
        over.push({...t.baseText(w/2,y+cap1,px1,'inter_sb','RICOH GXR',white,0.18,'c'),auto_ink:[white,[28,28,32]]});
        over.push({...t.baseText(w/2,y+cap1+g+cap2,px2,'inter',sub,soft,0.04,'c'),auto_ink:[soft,[52,52,58]]});
        total=y+cap1+g+cap2;
      } else {
        over.push({...t.baseText(w/2,y+cap2,px2,'inter',sub,soft,0.04,'c'),auto_ink:[soft,[52,52,58]]});
        total=y+cap2;
      }
    }
    return {...makeUnit(w,total,[0,0],w,h,{under,over,bg:[24,24,28],bgfx:{kind:'blur',sigma:Math.max(90,round(0.16*Math.max(w,h))),veil:0.90,sat:1.15}}),bgfx_always:true};
  };
  return solve(unit,ratio,w,{natural:[m0,m0,round(0.065*short)],f_side:0.06,f_top:0.06,f_bot:0.06,split:0.45});
}

export function cinema(w,h,spec,meta,m,ratio) {
  const t=new Type(m),short=Math.min(w,h),black=[8,8,10],yellow=[244,198,68],dim=[150,152,160],gate=Math.max(20,round(0.03*short)),text=spec.text||'';
  const kind=kindOf(spec),custom=spec._customLogo,logoMode=spec.logo_mode||'auto',logoSize=spec.logo_size||'medium';
  const unit=wref=>{
    const sz=t.sizes(wref),pxM=Math.max(sz.p2('inter_sb'),round(0.015*short)),pxS=Math.max(sz.p1('inter_sb'),round(0.022*short));
    const pxE=Math.max(sz.cap('courier',text?0.011:0.015),round(0.016*short)),capM=t.capH('inter_sb',pxM),capS=t.capH('inter_sb',pxS),capE=t.capH('courier',pxE);
    const uw=w+2*gate,bt=Math.max(round(0.10*short),round(3.4*capM));
    const [lw,lh]=kind==='custom'&&custom?fitLogo(custom,capM,round(0.70*bt),round(0.30*w),logoSize):[0,0];
    const bb=text?Math.max(bt,round(0.15*short),round(1.3*capS+1.4*capE+2.2*capS)):bt,ym=bt/2;
    const over=[{k:'rect',x:gate-1,y:bt-1,w:w+2,h:h+2,fill:null,outline:[38,40,46],stroke_w:1}];
    if(kind==='custom'&&custom) {
      over.push(clogoItem(custom,gate,round(ym-lh/2),lw,lh,logoMode,[236,236,240],8));
      if(meta.lens_short) over.push(t.capText(gate+lw+round(1.4*capM),ym,pxM,'inter_sb',meta.lens_short,dim,0.16));
    } else if(kind==='ricoh') over.push(t.capText(gate,ym,pxM,'inter_sb',join(['RICOH GXR',meta.lens_short],'  ·  '),dim,0.16));
    else if(meta.lens_short) over.push(t.capText(gate,ym,pxM,'inter_sb',meta.lens_short,dim,0.16));
    if(meta.date) over.push(t.capText(gate+w,ym,pxM,'inter_sb',meta.date,dim,0.10,'r'));
    const exp=join(['aperture','shutter','iso'].map(k=>meta[k]),'   ·   '),y0=bt+h;
    if(text) {
      const sub=t.clipText(text,'inter_sb',pxS,w-2*gate,0.02);
      over.push(t.capText(uw/2,y0+bb*0.36,pxS,'inter_sb',sub,yellow,0.02,'c'));
      over.push(t.capText(uw/2,y0+bb*0.74,pxE,'courier',exp,dim,0.08,'c'));
    } else over.push(t.capText(uw/2,y0+bb/2,pxE,'courier',exp,dim,0.08,'c'));
    return makeUnit(uw,bt+h+bb,[gate,bt],w,h,{over,bg:black});
  };
  return solve(unit,ratio,w,{natural:[0,0,0],f_side:0.05,f_top:0.05,f_bot:0.03,split:0.48});
}

export function slideMount(w,h,spec,meta,m,ratio) {
  const t=new Type(m),short=Math.min(w,h),dark=spec.color==='black',mount=dark?[30,31,35]:[242,238,229];
  const ink=dark?[232,230,224]:[38,36,34],red=dark?[224,62,52]:[198,44,38],rule=dark?[64,66,72]:[206,200,188],table=dark?[226,222,214]:[22,22,26];
  const kind=kindOf(spec),custom=spec._customLogo,logoMode=spec.logo_mode||'auto',logoSize=spec.logo_size||'medium';
  const unit=wref=>{
    const sz=t.sizes(wref),px1=Math.max(sz.p1('inter_sb'),round(0.017*short)),px2=Math.max(sz.p2('courier'),round(0.016*short)),cap1=t.capH('inter_sb',px1);
    const ry=Math.max(round(0.145*short),round(3.6*cap1)),rx=Math.max(round(0.115*short),round(0.11*w)),w0=w+2*rx,h0=h+2*ry;
    const bz=Math.max(6,round(0.012*short)),rad=Math.max(8,round(0.018*short));
    const under=[{k:'shadow',x:0,y:0,w:w0,h:h0,blur:Math.max(8,round(0.022*short)),dy:Math.max(4,round(0.012*short)),alpha:dark?0.30:0.45,radius:rad,fx_only:true},
      {k:'rect',x:0,y:0,w:w0,h:h0,fill:mount,outline:rule,stroke_w:1,radius:rad,fx_only:true},
      {k:'rect',x:rx-bz,y:ry-bz,w:w+2*bz,h:h+2*bz,fill:dark?[18,19,22]:[222,216,204],outline:rule,stroke_w:2,radius:Math.max(4,round(0.010*short))},
      {k:'line',x1:rx,y1:round(ry*0.22),x2:w0-rx,y2:round(ry*0.22),w:1,fill:rule},
      {k:'line',x1:rx,y1:h0-round(ry*0.22),x2:w0-rx,y2:h0-round(ry*0.22),w:1,fill:rule}];
    const ymT=ry*0.60,ymB=h0-ry*0.58;
    const over=[t.capText(rx,ymT,px1,'inter_sb',String(frameIndex(meta)),red,0.06),t.capText(rx,ymB,px2,'courier',meta.date||'',red,0.06)];
    if(kind==='custom'&&custom) {
      const [lw,lh]=fitLogo(custom,cap1,round(0.60*ry),round(0.40*w),logoSize);
      over.push(clogoItem(custom,round(w0/2-lw/2),round(ymT-lh/2),lw,lh,logoMode,ink,dark?30:240));
    } else if(kind==='ricoh') over.push(t.capText(w0/2,ymT,px1,'inter_sb',join(['RICOH GXR',meta.lens_short],'  ·  '),ink,0.14,'c'));
    let text=join(['aperture','shutter','iso'].map(k=>meta[k]));
    while(text&&t.advance('courier',px2,text,0.03)+t.advance('courier',px2,meta.date||'',0.06)+40>w) text=text.split('  ').slice(0,-1).join('  ');
    if(text) over.push(t.capText(w0-rx,ymB,px2,'courier',text,ink,0.03,'r'));
    return makeUnit(w0,h0,[rx,ry],w,h,{under,over,bg:mount,table});
  };
  return solve(unit,ratio,w,{natural:[0,0,0],f_side:0.06,f_top:0.06,f_bot:0.06,split:0.48});
}

export function darkroom(w,h,spec,meta,m,ratio) {
  const t=new Type(m),short=Math.min(w,h),paper=[246,242,233],black=[14,14,15],pencil=[104,102,98];
  const c=Math.max(10,round(0.017*short)),jit=Math.max(4,round(0.5*c)),b=c+jit+2,ms=Math.max(38,round(0.082*short));
  const unit=wref=>{
    const under=[{k:'carrier',x:b,y:b,w,h,c,jit,seed:19790324,fill:black,paper}];
    const sz=t.sizes(wref),px1=Math.max(sz.cap('patrick',0.019),round(0.032*short)),px2=Math.max(sz.cap('patrick',0.016),round(0.028*short));
    const noteY=b+h+b+Math.max(10,round(0.014*short)),cap=t.capH('patrick',px1),left=spec.text||meta.date||'';
    const right=join([meta.lens_short,meta.aperture,meta.shutter]),over=[];
    if(left) over.push(t.baseText(b,noteY+cap,px1,'patrick',left,pencil,0.01));
    if(right&&t.advance('patrick',px2,right,0.01)+t.advance('patrick',px1,left,0.01)+40<w) over.push(t.baseText(b+w,noteY+cap,px2,'patrick',right,pencil,0.01,'r'));
    return makeUnit(w+2*b,noteY+cap+round(0.35*cap),[b,b],w,h,{under,over,bg:paper,grain:0.016});
  };
  return solve(unit,ratio,w,{natural:[Math.max(0,ms-b),Math.max(0,ms-b),round(0.05*short)],f_side:0.07,f_top:0.07,f_bot:0.06,split:0.44});
}

export function postageStamp(w,h,spec,meta,m,ratio) {
  const t=new Type(m),short=Math.min(w,h),stock=[22,23,27],paper=[250,247,238],ink=[32,32,36],muted=[96,92,86],eng=[78,74,68];
  const pad=Math.max(32,round(0.068*short)),hr=Math.max(6,round(0.013*short)),pitch=Math.max(18,round(0.042*short)),eg=Math.max(6,round(0.011*short));
  const focal=/(\d+(?:\.\d+)?)\s*mm/.exec(meta.lens_short||''),inscription=join(['RICOH GXR',focal?.[1]],'  ·  ');
  const kind=kindOf(spec),custom=spec._customLogo,logoMode=spec.logo_mode||'auto',logoSize=spec.logo_size||'medium';
  const unit=wref=>{
    const sz=t.sizes(wref),px1=Math.max(sz.p1('inter_sb'),round(0.018*short)),px2=Math.max(sz.p2('inter'),round(0.016*short)),cap1=t.capH('inter_sb',px1);
    const [lw,lh]=kind==='custom'&&custom?fitLogo(custom,cap1,round(3.2*cap1),round(0.30*w),logoSize):[0,0];
    const padBot=Math.max(round(0.105*short),round(3.4*cap1)+eg,lh+3*eg+hr),sw=w+2*pad,sh=h+pad+padBot;
    const under=[{k:'rect',x:0,y:0,w:sw,h:sh,fill:paper},{k:'rect',x:pad-eg,y:pad-eg,w:w+2*eg,h:h+2*eg,fill:null,outline:eng,stroke_w:1}];
    const nx=Math.max(4,round(sw/pitch)),ny=Math.max(4,round(sh/pitch));
    for(let i=0;i<nx;i++) for(const cy of [0,sh]) under.push({k:'ellipse',cx:round((i+0.5)*sw/nx),cy,r:hr,fill:stock});
    for(let j=0;j<ny;j++) for(const cx of [0,sw]) under.push({k:'ellipse',cx,cy:round((j+0.5)*sh/ny),r:hr,fill:stock});
    const yb=pad+h+eg+(padBot-eg-hr)*0.52+cap1/2;
    let over,leftW;
    if(kind==='custom'&&custom) {
      over=[clogoItem(custom,pad,round(yb-cap1/2-lh/2),lw,lh,logoMode,ink,250)];
      leftW=lw;
    } else if(kind==='none') {
      over=[]; leftW=0;
    } else {
      over=[t.baseText(pad,yb,px1,'inter_sb',inscription,ink,0.10)];
      leftW=t.advance('inter_sb',px1,inscription,0.10);
    }
    let right=join(['lens_short','aperture','shutter','date'].map(k=>meta[k]),'  ·  ');
    const padLeft=leftW?leftW+30:0;
    while(right&&t.advance('inter',px2,right,0.02)+padLeft>w) right=right.includes('  ·  ')?right.split('  ·  ').slice(0,-1).join('  ·  '):'';
    if(right) over.push(t.baseText(pad+w,yb,px2,'inter',right,muted,0.02,'r'));
    return makeUnit(sw,sh,[pad,pad],w,h,{under,over,bg:stock});
  };
  const margin=Math.max(34,round(0.072*short));
  return solve(unit,ratio,w,{natural:[margin,margin,margin],f_side:0.07,f_top:0.07,f_bot:0.07,split:0.47});
}
