// Blur small copies of the edited photo. Build the visible ratio now and warm
// the other ratios one at a time during idle, only while the frame panel is visible.
import { frameLayout, OUTPUT_RATIOS } from './finish_layout.js';
import { metrics, tableReady } from './finish_fonts.js';
import { paintGrain } from './finish_texture.js';

const rgb=c=>`rgb(${c[0]},${c[1]},${c[2]})`;
const cache=new WeakMap();
const stats={builds:0,hits:0,ms:0,pending:0};
export const backgroundStats=()=>({...stats});
const keyFor=f=>`${f.width}x${f.height}|${f.bgfx.sigma}|${f.bgfx.veil}|${f.bgfx.sat}`;

function entry(photo) {
  if(!cache.has(photo)) cache.set(photo,{canvases:new Map(),primed:new Set(),warming:new Set()});
  return cache.get(photo);
}

function canvas(w,h) {
  const cv=document.createElement('canvas');cv.width=w;cv.height=h;return cv;
}

// Separable Gaussian with clamped edges. The small copy is opaque, so no dark
// transparent band can bleed into its edges when the blur reaches outside it.
function gaussian(data,w,h,sigma) {
  const radius=Math.ceil(3*sigma),kernel=[];
  let sum=0;
  for(let i=-radius;i<=radius;i++) {const k=Math.exp(-i*i/(2*sigma*sigma));kernel.push(k);sum+=k;}
  for(let i=0;i<kernel.length;i++) kernel[i]/=sum;
  const tmp=new Float32Array(w*h*3),out=new Uint8ClampedArray(w*h*4);
  for(let y=0;y<h;y++) for(let x=0;x<w;x++) for(let c=0;c<3;c++) {
    let value=0;
    for(let k=-radius;k<=radius;k++) value+=data[(y*w+Math.max(0,Math.min(w-1,x+k)))*4+c]*kernel[k+radius];
    tmp[(y*w+x)*3+c]=value;
  }
  for(let y=0;y<h;y++) for(let x=0;x<w;x++) {
    const p=(y*w+x)*4;
    for(let c=0;c<3;c++) {
      let value=0;
      for(let k=-radius;k<=radius;k++) value+=tmp[(Math.max(0,Math.min(h-1,y+k))*w+x)*3+c]*kernel[k+radius];
      out[p+c]=value;
    }
    out[p+3]=255;
  }
  return out;
}

export function blurBackdrop(photo,frame) {
  const kept=entry(photo).canvases,key=keyFor(frame);
  if(kept.has(key)) {stats.hits++;return kept.get(key);}
  const start=performance.now(),fx=frame.bgfx,k=Math.min(1,768/Math.max(frame.width,frame.height));
  const w=Math.max(1,Math.round(frame.width*k)),h=Math.max(1,Math.round(frame.height*k));
  const scale=Math.max(frame.width/photo.width,frame.height/photo.height);
  const bw=Math.max(frame.width,Math.round(photo.width*scale)),bh=Math.max(frame.height,Math.round(photo.height*scale));
  const sw=Math.max(8,Math.floor(bw*k/8)),sh=Math.max(8,Math.floor(bh*k/8));
  const small=canvas(sw,sh),sc=small.getContext('2d');
  sc.imageSmoothingQuality='low';sc.drawImage(photo,0,0,sw,sh);
  sc.putImageData(new ImageData(gaussian(sc.getImageData(0,0,sw,sh).data,sw,sh,Math.max(0.25,fx.sigma*k/8)),sw,sh),0,0);
  const cv=canvas(w,h),ctx=cv.getContext('2d');ctx.imageSmoothingQuality='high';
  ctx.drawImage(small,-Math.floor((bw-frame.width)/2)*k,-Math.floor((bh-frame.height)/2)*k,bw*k,bh*k);
  const pixels=ctx.getImageData(0,0,w,h),data=pixels.data;
  for(let i=0;i<data.length;i+=4) {
    const luma=data[i]*0.299+data[i+1]*0.587+data[i+2]*0.114;
    for(let c=0;c<3;c++) data[i+c]=Math.floor((luma+(data[i+c]-luma)*fx.sat)*fx.veil);
  }
  ctx.putImageData(pixels,0,0);kept.set(key,cv);stats.builds++;stats.ms+=performance.now()-start;
  return cv;
}

export function primeBlur(photo,w,h,meta,current,active=()=>false) {
  if(current?.bgfx?.kind==='blur') blurBackdrop(photo,current);
  if(!active()||typeof requestIdleCallback!=='function'||!tableReady('inter_sb')||!tableReady('inter')) return;
  const kept=entry(photo),key=`${w}x${h}`;
  if(kept.primed.has(key)||kept.warming.has(key)) return;
  const options=[];
  for(const output_ratio of [...Object.keys(OUTPUT_RATIOS),'original']) for(const show_params of [false,true]) options.push({output_ratio,show_params});
  kept.warming.add(key);stats.pending++;
  let index=0;
  const warm=deadline=>{
    if(!active()) {kept.warming.delete(key);stats.pending--;return;}
    if(deadline.timeRemaining()<4) {requestIdleCallback(warm);return;}
    const frame=frameLayout(w,h,{style:'blur_fill',...options[index++]},meta,metrics);
    if(frame.bgfx?.kind==='blur') blurBackdrop(photo,frame);
    if(index<options.length) requestIdleCallback(warm);
    else {kept.warming.delete(key);kept.primed.add(key);stats.pending--;}
  };
  requestIdleCallback(warm);
}

export function paintBackground(ctx,frame,s,photo=null) {
  ctx.save();ctx.fillStyle=rgb(frame.bg);ctx.fillRect(0,0,frame.width*s,frame.height*s);
  if(frame.bgfx?.kind==='blur'&&photo) ctx.drawImage(blurBackdrop(photo,frame),0,0,frame.width*s,frame.height*s);
  if(frame.bgfx?.kind==='glow'&&frame.bgfx.falloff) {
    ctx.translate(frame.width*s/2,frame.height*s/2);ctx.scale(frame.width*s/2,frame.height*s/2);
    const g=ctx.createRadialGradient(0,0,0,0,0,Math.SQRT2);
    for(let i=0;i<=32;i++) {const f=i/32,k=1-frame.bgfx.falloff*f*f;g.addColorStop(f,rgb(frame.bg.map(c=>Math.floor(c*k))));}
    ctx.fillStyle=g;ctx.fillRect(-1,-1,2,2);
  }
  ctx.restore();
  if(frame.grain) paintGrain(ctx,frame,s);
}
