// Preview-sized deterministic fibre grain and a seeded filed-carrier contour.
// Export uses NumPy's reference RNG; preview uses a portable seeded generator.
const grains=new Map(),contours=new Map();
const rgb=c=>`rgb(${c[0]},${c[1]},${c[2]})`;

function random(seed) {
  let state=seed>>>0;
  return ()=>{state=(Math.imul(state,1664525)+1013904223)>>>0;return (state+0.5)/4294967296;};
}

function normal(rng) {return Math.sqrt(-2*Math.log(rng()))*Math.cos(2*Math.PI*rng());}
function remember(map,key,value) {if(map.size>=8) map.delete(map.keys().next().value);map.set(key,value);return value;}

export function paintGrain(ctx,frame,s) {
  const w=Math.ceil(frame.width*s),h=Math.ceil(frame.height*s),key=`${w}x${h}|${s}|${frame.grain}`;
  let cv=grains.get(key);
  if(!cv) {
    cv=document.createElement('canvas');cv.width=w;cv.height=h;
    const sc=cv.getContext('2d'),pixels=sc.createImageData(w,h),rng=random(42),sigma=255*frame.grain*Math.min(1,s);
    const lowW=Math.max(4,Math.ceil(w/6)),lowH=Math.max(4,Math.ceil(h/6));
    const low=Float32Array.from({length:lowW*lowH},()=>normal(rng)*40*frame.grain*0.8);
    for(let y=0;y<h;y++) for(let x=0;x<w;x++) {
      const i=(y*w+x)*4,noise=normal(rng)*sigma+low[Math.min(lowH-1,Math.floor(y/6))*lowW+Math.min(lowW-1,Math.floor(x/6))];
      for(let c=0;c<3;c++) pixels.data[i+c]=Math.floor(frame.bg[c]+noise);
      pixels.data[i+3]=255;
    }
    sc.putImageData(pixels,0,0);remember(grains,key,cv);
  }
  ctx.drawImage(cv,0,0);
}

function wobble(n,rng,wavelength,notches,notchW) {
  const out=new Float64Array(n);
  [1,0.55,0.3].forEach((amp,k)=>{
    const wl=wavelength/(1+1.7*k),phase=rng()*6.28;
    for(let i=0;i<n;i++) out[i]+=amp*Math.sin(2*Math.PI*i/wl+phase)/1.85;
  });
  for(let j=0;j<notches;j++) {
    const pos=rng()*n,wid=(0.5+rng()*0.8)*notchW,amp=(rng()<0.5?-1:1)*(0.7+rng()*0.7);
    for(let i=0;i<n;i++) out[i]+=amp*Math.exp(-(((i-pos)/wid)**2));
  }
  for(let i=0;i<n;i++) out[i]=Math.max(-1.4,Math.min(1.4,out[i]+normal(rng)*0.05));
  return out;
}

function carrier(item) {
  const {w,h,c,jit,seed}=item,key=`${w}x${h}|${c}|${jit}|${seed}`;
  if(contours.has(key)) return contours.get(key);
  const short=Math.min(w,h),rng=random(seed),wob={},gaps={};
  for(const side of 'trbl') wob[side]=wobble(Math.floor(('tb'.includes(side)?w:h)/2)+4,rng,0.35*short,4,0.04*short);
  for(const side of 'trbl') gaps[side]=wobble(wob[side].length,rng,0.2*short,3,0.03*short).map(v=>Math.max(0,Math.min(3.2,v*1.4+1.2)));
  const outer=[],inner=[];
  for(const [side,length] of [['t',w],['r',h],['b',w],['l',h]]) for(let i=0;i<=Math.floor(length/2);i++) {
    const d=c+0.55*jit*wob[side][i]+0.45*jit,g=gaps[side][i],a=2*i;
    if(side==='t') {outer.push([a,-d]);inner.push([a,-g]);}
    else if(side==='r') {outer.push([w+d,a]);inner.push([w+g,a]);}
    else if(side==='b') {outer.push([w-a,h+d]);inner.push([w-a,h+g]);}
    else {outer.push([-d,h-a]);inner.push([-g,h-a]);}
  }
  return remember(contours,key,[outer,inner]);
}

export function paintCarrier(ctx,item,s) {
  const rings=carrier(item);
  rings.forEach((points,index)=>{
    ctx.fillStyle=rgb(index?item.paper:item.fill);ctx.beginPath();
    points.forEach(([x,y],i)=>ctx[i?'lineTo':'moveTo']((item.x+x)*s,(item.y+y)*s));
    ctx.closePath();ctx.fill();
  });
}
