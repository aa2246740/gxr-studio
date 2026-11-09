// Temporary CI census: do not read styles until after the existing measurement.
export async function installRatioTrace({evalIn}) {
  await evalIn(`(async () => {
    const view = (await import('/assets/viewer.js')).currentView();
    const fonts = await import('/assets/finish_fonts.js');
    const nativeChange = view.onChange;
    const ids = new WeakMap();
    let nextId = 0;
    const id = object => { if (!object) return null; if (!ids.has(object)) ids.set(object, ++nextId); return ids.get(object); };
    let active = false;
    let frame = 0;
    let origin = 0;
    let events = [];
    let pulse = null;
    const pv = () => view.stageArea.querySelector('.pv');
    const add = (step, data = {}) => { if (active && events.length < 100) events.push({frame, ms:Math.round((performance.now()-origin)*10)/10, step, ...data}); };
    const stack = () => new Error().stack.split('\\n').slice(2,7).join('\\n');
    view.onChange = (...args) => {
      add('onChange-enter',{ratio:view.ed.frame.output_ratio,stack:stack()});
      const value = nativeChange(...args);
      const deco = view.decor?.deco;
      add('onChange-exit',{ratio:view.ed.frame.output_ratio,deco:deco?[deco.width,deco.height]:null});
      return value;
    };
    const fontEvent = () => add('fonts-loadingdone');
    document.fonts.addEventListener('loadingdone',fontEvent);
    const snapshot = (label, measured) => {
      const current = pv();
      const css = getComputedStyle(current);
      const image = current.querySelector('.pv-img');
      const computed = {};
      for(const property of ['width','height','minWidth','maxWidth','minHeight','maxHeight','flex','flexBasis','flexShrink','boxSizing','aspectRatio','transform','scale','translate','rotate','zoom','contain','contentVisibility','display']) computed[property]=css[property];
      return {label, frame, measured, styleId:id(current.style), inline:current.getAttribute('style'),computed,
        offset:[current.offsetWidth,current.offsetHeight],client:[current.clientWidth,current.clientHeight],
        sameNode:current===document.querySelector('.pv.framed'),pvs:document.querySelectorAll('.pv').length,
        ancestors:[current.parentElement,current.parentElement.parentElement].map(node=>({class:node.className,inline:node.getAttribute('style'),width:getComputedStyle(node).width,height:getComputedStyle(node).height})),
        afterComputed:(() => {const r=current.getBoundingClientRect();return [r.width,r.height];})(),
        images:{natural:[image?.naturalWidth,image?.naturalHeight],complete:image?.complete,srcMatches:current.dataset.src===view.ed.urls.final},
        key:current.querySelector('.pv-deco')?.dataset.key, ratio:view.ed.frame.output_ratio,mode:current.dataset.mode,gpu:view.gpuActive,
        fonts:['atkinson','inter','courier','patrick','dseg'].map(font=>[font,fonts.tableReady(font),fonts.fontReady(font)]),
        animations:current.getAnimations().map(animation=>({time:animation.currentTime,frames:animation.effect?.getKeyframes()})),
        browser:navigator.userAgent,events};
    };
    window.__ratioTrace = {
      begin(label) {
        if(pulse!==null)cancelAnimationFrame(pulse);
        active=true;frame=0;origin=performance.now();events=[];
        add('begin',{label,ratio:view.ed.frame.output_ratio});
        const tick=()=>{frame++;add('animation-frame');if(frame<8)pulse=requestAnimationFrame(tick);};
        pulse=requestAnimationFrame(tick);
      },
      mark(label,measured) {add(label,{measured});return snapshot(label,measured);},
      end(){active=false;if(pulse!==null)cancelAnimationFrame(pulse);},
      close(){this.end();document.fonts.removeEventListener('loadingdone',fontEvent);view.onChange=nativeChange;delete window.__ratioTrace;},
    };
  })()`);
}
