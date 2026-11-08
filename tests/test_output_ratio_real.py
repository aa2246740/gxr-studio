"""现场照片的成品比例、尺寸与旧版导出字节；样本和导出均不进仓库。"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

from gxr_studio.crop import default_crop
from gxr_studio.finish import frame as frame_mod
from gxr_studio.finish.serve import export_finish
from gxr_studio.finish.sizes import output_sizes
from gxr_studio.finish.spec import FrameSpec, StampSpec
from gxr_studio.render.params import EditState
from gxr_studio.render.service import RenderService

ROOT = Path(__file__).resolve().parents[1]
BASE = "654cbe6"


@pytest.fixture
def real_photo(lib, settings):
    parent = Path(os.environ.get("GXR_STUDIO_TEST_PARENT", ROOT.parent))
    source = parent / "captures/a12-50/2026-10-19/stage2c-first-run/R8051145.JPG"
    if not source.is_file():
        pytest.skip("没有现场 a12-50 样本")
    path = lib.root / "sample.JPG"
    shutil.copyfile(source, path)
    with Image.open(path) as image:
        w, h = image.size
        if image.getexif().get(274, 1) in (5, 6, 7, 8):
            w, h = h, w
    shot = {"shot_id": "real-sample", "name": "sample", "width": w, "height": h, "files": {"jpg": {"relpath": path.name}}}
    return RenderService(settings.home_dir, lib), shot, w, h


def export(lib, settings, render, shot, edit, side, edge=None):
    return export_finish(render, lib, shot, edit, side, settings.export_dir, [], quality=95, long_edge=edge, fmt="jpeg")


@pytest.mark.parametrize("style", ("gallery", "instant"))
def test_real_3_by_4_with_frame_and_stamp_matches_the_preflight_size(lib, settings, real_photo, style):
    render, shot, w, h = real_photo
    crop = default_crop("4:3", w, h, flip=w >= h)
    side = {"crop": crop.to_json()}
    edit = EditState(frame=FrameSpec(style=style), stamp=StampSpec(enabled=True))
    sizes = output_sizes(render, lib, shot, edit, crop.rect, crop.preset)
    for tier, edge in (("original", None), ("large", 3000), ("medium", 2048), ("small", 1600)):
        out = export(lib, settings, render, shot, edit, side, edge)
        with Image.open(out.path) as image:
            assert list(image.size) == sizes["sizes"][tier] == [out.width, out.height]
        assert out.width * 4 == out.height * 3


@pytest.mark.parametrize("style", ("gallery", "leica", "hasselblad", "film", "instant", "canvas"))
@pytest.mark.parametrize("preset", ("original", "free"))
def test_real_legacy_frames_export_byte_identically_to_main(lib, settings, real_photo, tmp_path, monkeypatch, style, preset):
    """只替换实际主分支的版面函数；编码、EXIF、颜色、裁剪、时间戳与导出全部走真管线。"""
    result = subprocess.run(["git", "show", f"{BASE}:src/gxr_studio/finish/layout.py"], cwd=ROOT, capture_output=True, text=True, check=True)
    frozen = tmp_path / "baseline_layout.py"
    frozen.write_text(result.stdout, encoding="utf-8")
    name = "gxr_studio.finish._output_ratio_baseline"
    spec = importlib.util.spec_from_file_location(name, frozen)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    render, shot, w, h = real_photo
    crop = default_crop(preset, w, h)
    side = {"crop": crop.to_json()}
    edit = EditState(frame=FrameSpec(style=style, text="Hello"), stamp=StampSpec(enabled=True))
    current = export(lib, settings, render, shot, edit, side)
    monkeypatch.setattr(frame_mod, "frame_layout", lambda w, h, f, meta, m, preset=None: module.frame_layout(w, h, f, meta, m))
    previous = export(lib, settings, render, shot, edit, side)
    assert (current.width, current.height) == (previous.width, previous.height)
    assert current.path.read_bytes() == previous.path.read_bytes()
