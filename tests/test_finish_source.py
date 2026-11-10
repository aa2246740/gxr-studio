"""Photo/backdrop cache follows the displayed image, including pending edits."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "web" / "assets"


def _source_modules(tmp_path):
    shutil.copyfile(WEB / "finish_source.js", tmp_path / "finish_source.js")
    (tmp_path / "package.json").write_text('{"type":"module"}\n')
    (tmp_path / "api.js").write_text("export const apiBlob=(...args)=>globalThis.apiBlob(...args);\n")
    (tmp_path / "state.js").write_text("export const state={camdata:{dataVersion:'one'}};\n")
    (tmp_path / "preview.js").write_text("export const draftBody=view=>({recipe:view.ed.recipe});\n")
    (tmp_path / "gpu_stage.js").write_text("export const gpuSnapshot=use=>use(globalThis.gpuImage);\n")


@pytest.mark.skipif(shutil.which("node") is None, reason="node is absent")
def test_cached_source_changes_only_when_the_new_gpu_look_or_server_image_is_ready(tmp_path):
    _source_modules(tmp_path)
    script = """
      import {baseFor} from './finish_source.js';
      let copies=0;
      globalThis.apiBlob=()=>{throw new Error('displayed photos need no render request');};
      globalThis.document={createElement(){const cv={};cv.getContext=()=>({drawImage(src){copies++;cv.pixel=src.pixel;}});return cv;}};
      globalThis.gpuImage={width:360,height:240,pixel:[18,32,48]};
      const view={photo:{id:'p'},ed:{recipe:1,urls:{}},gpuActive:'final',gpuExport:{w:360,h:240},gpuFinishKey:'installed-look-1'};
      const first=await baseFor(view);
      view.ed.recipe=2; // the new recipe table is still loading, old frame remains visible
      const waiting=await baseFor(view);
      globalThis.gpuImage={width:360,height:240,pixel:[220,180,90]};
      view.gpuFinishKey='installed-look-2';
      const committed=await baseFor(view);
      view.ed.frame={style:'blur_fill',output_ratio:'1:1'};
      const option=await baseFor(view);
      const gpuCopies=copies;
      view.gpuActive=null;
      const img={complete:true,naturalWidth:360,naturalHeight:240,src:'blob:old',pixel:[60,80,100]};
      view.ed.urls.final=img.src;
      const server=await baseFor(view,{media:img});
      view.ed.recipe=3; // draft changed before the new server preview arrives
      const serverWaiting=await baseFor(view,{media:img});
      img.src=view.ed.urls.final='blob:new';img.pixel=[150,170,190];
      const arrived=await baseFor(view,{media:img});
      const totalCopies=copies;
      // A small fallback render can finish after the displayed preview. Its
      // completion must neither replace the cache nor return an older source.
      let finish;
      globalThis.apiBlob=()=>new Promise(resolve=>{finish=resolve;});
      globalThis.createImageBitmap=async image=>image;
      const loading={photo:{id:'q'},ed:{recipe:1,urls:{}},gpuActive:null};
      const fallback=baseFor(loading);
      const ready={complete:true,naturalWidth:360,naturalHeight:240,src:'blob:ready',pixel:[90,110,130]};
      loading.ed.urls.final=ready.src;
      const shown=await baseFor(loading,{media:ready});
      finish({blob:{width:360,height:240,pixel:[10,20,30]},headers:{get(){return '1';}}});
      const resolved=await fallback;
      console.log(JSON.stringify({first:first.image.pixel,waiting:waiting.image.pixel,committed:committed.image.pixel,
        option:option.image.pixel,gpuCopies,totalCopies,server:server.image.pixel,serverWaiting:serverWaiting.image.pixel,arrived:arrived.image.pixel,
        resolved:resolved.image.pixel,cached:loading.cardBase.base.image.pixel,keptDisplayed:shown===resolved}));
    """
    (tmp_path / "check.mjs").write_text(script)
    result = subprocess.run(["node", "check.mjs"], cwd=tmp_path, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "first": [18, 32, 48],
        "waiting": [18, 32, 48],
        "committed": [220, 180, 90],
        "option": [220, 180, 90],
        "gpuCopies": 2,
        "totalCopies": 4,
        "server": [60, 80, 100],
        "serverWaiting": [60, 80, 100],
        "arrived": [150, 170, 190],
        "resolved": [90, 110, 130],
        "cached": [90, 110, 130],
        "keptDisplayed": True,
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="node is absent")
@pytest.mark.parametrize("mode", ["gpu", "server"])
def test_frame_sources_wait_for_settled_edits_and_ignore_hidden_cards(tmp_path, mode):
    _source_modules(tmp_path)
    script = """
      import {baseFor,refreshFrameSource} from './finish_source.js';
      let copies=0,notifications=0,repaints=0,now=0,serial=0;
      const timers=new Map();
      globalThis.setTimeout=(run,delay)=>{const id=++serial;timers.set(id,{run,at:now+delay});return id;};
      globalThis.clearTimeout=id=>timers.delete(id);
      const advance=async ms=>{
        now+=ms;
        for(const [id,job] of [...timers]) if(job.at<=now){timers.delete(id);job.run();}
        await Promise.resolve();
      };
      globalThis.apiBlob=()=>{throw new Error('displayed photos need no render request');};
      globalThis.document={createElement(){const cv={};cv.getContext=()=>({drawImage(src){copies++;cv.pixel=[...src.pixel];}});return cv;}};
      const mode=MODE;
      const media={complete:true,naturalWidth:360,naturalHeight:240,src:'blob:1',pixel:[18,32,48]};
      globalThis.gpuImage={width:360,height:240,pixel:[18,32,48]};
      const view={photo:{id:'p'},mode:'final',tab:'adjust',collapsed:false,
        ed:{recipe:1,urls:{final:media.src},frame:{style:'none'}},
        gpuActive:mode==='gpu'?'final':null,gpuExport:{w:360,h:240},gpuFinishKey:'look-1',
        refreshFrameCards(){notifications++;this.frameCardsStale=false;},onChange(){repaints++;}};
      const refresh=()=>refreshFrameSource(view,{media});
      const change=(n,pixel)=>{
        view.ed.recipe=n;view.gpuFinishKey='look-'+n;media.src=view.ed.urls.final='blob:'+n;
        media.pixel=pixel;globalThis.gpuImage.pixel=pixel;
      };
      // Visiting the frame panel once must not leave a live readback consumer.
      refresh();await advance(500);
      const hiddenCopies=copies;
      view.tab='frame';refresh();await advance(0);
      const initial={copies,notifications,pixel:view.cardBase.base.image.pixel};
      // A held pointer can pause longer than the debounce interval. It still
      // keeps the previous backdrop and cannot build intermediate blur sources.
      view.tab='adjust';view.ed.frame.style='blur_fill';view.pending='before';
      for(const [n,pixel] of [[2,[50,70,90]],[3,[100,120,140]],[4,[220,180,90]]]){
        change(n,pixel);refresh();await advance(240);
      }
      const dragging={copies,notifications,repaints,pixel:(await baseFor(view,{media})).image.pixel};
      view.pending=null;await advance(119);
      const beforeSettled=copies;
      await advance(1);
      const settled={copies,notifications,repaints,pixel:view.cardBase.base.image.pixel};
      for(const output_ratio of ['1:1','4:5','3:2']){
        view.ed.frame.output_ratio=output_ratio;refresh();await baseFor(view,{media});
      }
      await advance(500);
      const optionCopies=copies;
      // Hidden and collapsed cards may become stale, then catch up once opened.
      view.ed.frame.style='gallery';view.tab='frame';view.collapsed=true;
      change(5,[80,100,120]);refresh();await advance(500);
      const collapsedCopies=copies;
      view.collapsed=false;refresh();await advance(120);
      const reopened={copies,pixel:view.cardBase.base.image.pixel};
      view.frameCardsStale=true;const previousNotifications=notifications;refresh();refresh();
      const resumed={notifications:notifications-previousNotifications,copies};
      change(6,[1,2,3]);refresh();view.disposed=true;await advance(120);
      const disposedCopies=copies;
      view.disposed=false;view.mode='uncropped';refresh();await advance(500);
      const cropCopies=copies;
      console.log(JSON.stringify({hiddenCopies,initial,dragging,beforeSettled,settled,
        optionCopies,collapsedCopies,reopened,resumed,disposedCopies,cropCopies,timers:timers.size}));
    """.replace("MODE", json.dumps(mode))
    (tmp_path / "check.mjs").write_text(script)
    result = subprocess.run(["node", "check.mjs"], cwd=tmp_path, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "hiddenCopies": 0,
        "initial": {"copies": 1, "notifications": 1, "pixel": [18, 32, 48]},
        "dragging": {"copies": 1, "notifications": 1, "repaints": 0, "pixel": [18, 32, 48]},
        "beforeSettled": 1,
        "settled": {"copies": 2, "notifications": 2, "repaints": 1, "pixel": [220, 180, 90]},
        "optionCopies": 2,
        "collapsedCopies": 2,
        "reopened": {"copies": 3, "pixel": [80, 100, 120]},
        "resumed": {"notifications": 1, "copies": 3},
        "disposedCopies": 3,
        "cropCopies": 3,
        "timers": 0,
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="node is absent")
def test_retained_card_callback_does_no_work_after_the_panel_is_hidden(tmp_path):
    shutil.copyfile(WEB / "finish_cards.js", tmp_path / "finish_cards.js")
    (tmp_path / "package.json").write_text('{"type":"module"}\n')
    (tmp_path / "dom.js").write_text("export const h=()=>({style:{},getContext:()=>({})});")
    (tmp_path / "finish.js").write_text("export const DEFAULT_FRAME={},selectFrame=(frame)=>frame;")
    (tmp_path / "stage_finish.js").write_text("export const makeDeco=()=>({width:400,height:300});")
    (tmp_path / "finish_draw.js").write_text("export const drawDecor=()=>{globalThis.paints++;};")
    (tmp_path / "output_photo_size.js").write_text("export const photoSize=()=>({value:{photo:[360,240]}});")
    (tmp_path / "finish_source.js").write_text(
        "export const draftKey=()=>1,baseFor=view=>{globalThis.reads++;return Promise.resolve(view.cardBase.base);};"
    )
    (tmp_path / "finish_background.js").write_text("export const primeBlur=()=>{globalThis.primes++;};")
    script = """
      import {paintCards} from './finish_cards.js';
      globalThis.paints=0;globalThis.reads=0;globalThis.primes=0;
      const view={tab:'adjust',ed:{frame:{style:'blur_fill'}},cardBase:{base:{image:{},ew:360,eh:240}}};
      const holder={isConnected:true,classList:{remove(){}},removeAttribute(){},querySelector(){return this.cv;},
        replaceChildren(cv){this.cv=cv;cv.parentElement=this;}};
      paintCards(view,[{style:{id:'blur_fill'},holder}]);
      const initiallyHidden={paints,reads,primes,stale:view.frameCardsStale};
      view.tab='frame';view.refreshFrameCards();
      const first={paints,reads,primes};
      view.tab='adjust';view.refreshFrameCards();view.refreshFrameCards();
      const hidden={paints,reads,primes,stale:view.frameCardsStale};
      view.tab='frame';view.collapsed=true;view.refreshFrameCards();
      const collapsed={paints,reads,primes};
      view.collapsed=false;view.refreshFrameCards();
      const reopened={paints,reads,primes,stale:view.frameCardsStale};
      view.disposed=true;view.refreshFrameCards();
      console.log(JSON.stringify({initiallyHidden,first,hidden,collapsed,reopened,disposed:{paints,reads,primes}}));
    """
    (tmp_path / "check.mjs").write_text(script)
    result = subprocess.run(["node", "check.mjs"], cwd=tmp_path, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "initiallyHidden": {"paints": 0, "reads": 0, "primes": 0, "stale": True},
        "first": {"paints": 1, "reads": 1, "primes": 1},
        "hidden": {"paints": 1, "reads": 1, "primes": 1, "stale": True},
        "collapsed": {"paints": 1, "reads": 1, "primes": 1},
        "reopened": {"paints": 2, "reads": 2, "primes": 2, "stale": False},
        "disposed": {"paints": 2, "reads": 2, "primes": 2},
    }
