"""Photo/backdrop cache follows the displayed image, including pending edits."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "web" / "assets"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is absent")
def test_cached_source_changes_only_when_the_new_gpu_look_or_server_image_is_ready(tmp_path):
    shutil.copyfile(WEB / "finish_source.js", tmp_path / "finish_source.js")
    (tmp_path / "package.json").write_text('{"type":"module"}\n')
    (tmp_path / "api.js").write_text("export const apiBlob=(...args)=>globalThis.apiBlob(...args);\n")
    (tmp_path / "state.js").write_text("export const state={camdata:{dataVersion:'one'}};\n")
    (tmp_path / "preview.js").write_text("export const draftBody=view=>({recipe:view.ed.recipe});\n")
    (tmp_path / "gpu_stage.js").write_text("export const gpuSnapshot=use=>use(globalThis.gpuImage);\n")
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
