"""Blur work stays on the visible ratio until the frame panel has idle time."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "web" / "assets"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is absent")
def test_only_the_current_blur_is_eager_and_hidden_panels_cancel_idle_work(tmp_path):
    shutil.copyfile(WEB / "finish_background.js", tmp_path / "finish_background.js")
    (tmp_path / "package.json").write_text('{"type":"module"}\n')
    (tmp_path / "finish_fonts.js").write_text("export const metrics={},tableReady=()=>true;")
    (tmp_path / "finish_texture.js").write_text("export const paintGrain=()=>{};")
    (tmp_path / "finish_layout.js").write_text(
        "export const OUTPUT_RATIOS={'1:1':[1,1],'4:5':[4,5]};"
        "export function frameLayout(w,h,s){return {width:80+['1:1','4:5','original'].indexOf(s.output_ratio)*10,"
        "height:60+(s.show_params?10:0),bgfx:{kind:'blur',sigma:1,veil:0.9,sat:1.15}};}"
    )
    script = """
      import {primeBlur,backgroundStats} from './finish_background.js';
      import {frameLayout} from './finish_layout.js';
      const idle=[];
      globalThis.requestIdleCallback=run=>idle.push(run);
      globalThis.ImageData=class {constructor(data,width,height){Object.assign(this,{data,width,height});}};
      globalThis.document={createElement(){return {getContext(){return {
        drawImage(){},putImageData(){},getImageData(x,y,w,h){return {data:new Uint8ClampedArray(w*h*4).fill(100)};}
      };}};}};
      const photo={width:40,height:30},current=frameLayout(40,30,{output_ratio:'1:1',show_params:false});
      let active=true;
      const prime=()=>primeBlur(photo,40,30,{},current,()=>active);
      const sample=()=>({builds:backgroundStats().builds,queued:idle.length});
      prime();const eager=sample();
      idle.shift()({timeRemaining:()=>0});const busy=sample();
      active=false;idle.shift()({timeRemaining:()=>50});const hidden=sample();
      active=true;prime();const resumed=sample();
      let maxPerIdle=0;
      while(idle.length){const before=backgroundStats().builds;idle.shift()({timeRemaining:()=>50});
        maxPerIdle=Math.max(maxPerIdle,backgroundStats().builds-before);}
      const warmed=sample();
      prime();const cached=sample();
      active=false;prime();const hiddenAgain=sample();
      console.log(JSON.stringify({eager,busy,hidden,resumed,warmed,cached,hiddenAgain,maxPerIdle}));
    """
    (tmp_path / "check.mjs").write_text(script)
    result = subprocess.run(["node", "check.mjs"], cwd=tmp_path, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "eager": {"builds": 1, "queued": 1},
        "busy": {"builds": 1, "queued": 1},
        "hidden": {"builds": 1, "queued": 0},
        "resumed": {"builds": 1, "queued": 1},
        "warmed": {"builds": 6, "queued": 0},
        "cached": {"builds": 6, "queued": 0},
        "hiddenAgain": {"builds": 6, "queued": 0},
        "maxPerIdle": 1,
    }
