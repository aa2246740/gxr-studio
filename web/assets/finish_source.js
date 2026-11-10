// One small, edited, cropped photo for frame cards and backdrop caches.
import { apiBlob } from './api.js';
import { state } from './state.js';
import { draftBody } from './preview.js';
import { gpuSnapshot } from './gpu_stage.js';

const BASE_EDGE=480;

function shrink(source,w,h) {
  const scale=Math.min(1,BASE_EDGE/Math.max(w,h)),canvas=document.createElement('canvas');
  canvas.width=Math.max(1,Math.round(w*scale));canvas.height=Math.max(1,Math.round(h*scale));
  canvas.getContext('2d').drawImage(source,0,0,canvas.width,canvas.height);
  return canvas;
}

export function draftKey(view) {
  return `${view.photo.id}|${state.camdata?.dataVersion??''}|${JSON.stringify(draftBody(view,'final'))}`;
}

function displayed(view,media=null) {
  if(view.gpuActive==='final'&&view.gpuExport) {
    if(!view.gpuFinishKey) return null;
    const source=`gpu:${view.gpuFinishKey}`;
    if(view.cardBase?.source===source) return view.cardBase.base;
    const image=gpuSnapshot(cv=>cv.width&&cv.height?shrink(cv,cv.width,cv.height):null);
    return image?{image,ew:view.gpuExport.w,eh:view.gpuExport.h,source}:null;
  }
  const img=media||view.stageArea?.querySelector('.pv-img');
  if(!img?.complete||!img.naturalWidth||img.src!==view.ed?.urls.final) return null;
  const source=img.src;
  if(view.cardBase?.source===source) return view.cardBase.base;
  const k=view.ed.scale||1;
  return {image:shrink(img,img.naturalWidth,img.naturalHeight),ew:Math.round(img.naturalWidth*k),eh:Math.round(img.naturalHeight*k),source};
}

async function requestBase(view) {
  const {blob,headers}=await apiBlob(`/api/photos/${view.photo.id}/render?stage=final&max_edge=${BASE_EDGE}`,{body:draftBody(view,'final')});
  const image=await createImageBitmap(blob),k=Number(headers.get('X-GXR-Scale'))||1;
  return {image,ew:Math.round(image.width*k),eh:Math.round(image.height*k),source:null};
}

// An arriving server image changes the source identity even if its edit key was
// already captured while the older image remained on screen.
export function baseFor(view,{media=null,request=true,notify=false}={}) {
  const key=draftKey(view),kept=view.cardBase,now=displayed(view,media);
  if(now) {
    if(kept?.base===now) return Promise.resolve(now);
    view.cardBase={key,base:now,source:now.source};
    if(notify) queueMicrotask(()=>view.refreshFrameCards?.());
    return Promise.resolve(now);
  }
  if(kept?.key===key) return kept.base?Promise.resolve(kept.base):kept.promise;
  if(!request) return Promise.resolve(null);
  const queued={key,base:null,promise:null,last:kept&&(kept.base||kept.last)};
  const promise=requestBase(view).then(base=>{
    if(view.cardBase===queued) {queued.base=base;queued.source=base.source;}
    return view.cardBase?.key===key&&view.cardBase.base?view.cardBase.base:base;
  },error=>{
    if(view.cardBase!==queued&&view.cardBase?.key===key&&view.cardBase.base) return view.cardBase.base;
    throw error;
  });
  queued.promise=promise;view.cardBase=queued;
  promise.catch(()=>{if(view.cardBase===queued) view.cardBase=null;});
  return promise;
}
