"""现场照片的成品比例、尺寸与旧版导出字节；样本和导出均不进仓库。"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

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
BASE = "0d3b08d50b3e81c5fd37243cdea3bdfcfb4a5e87"


@pytest.fixture(scope="session")
def real_sample():
    parent = Path(os.environ.get("GXR_STUDIO_TEST_PARENT", ROOT.parent))
    source = parent / "captures/a12-50/2026-10-19/stage2c-first-run/R8051145.JPG"
    if not source.is_file():
        pytest.skip("没有现场 a12-50 样本")
    return source


@pytest.fixture
def real_photo(lib, settings, real_sample):
    path = lib.root / "sample.JPG"
    shutil.copyfile(real_sample, path)
    with Image.open(path) as image:
        w, h = image.size
        if image.getexif().get(274, 1) in (5, 6, 7, 8):
            w, h = h, w
    shot = {"shot_id": "real-sample", "name": "sample", "width": w, "height": h, "files": {"jpg": {"relpath": path.name}}}
    return RenderService(settings.home_dir, lib), shot, w, h


def export(lib, settings, render, shot, edit, side, edge=None):
    return export_finish(render, lib, shot, edit, side, settings.export_dir, [], quality=95, long_edge=edge, fmt="jpeg")


@pytest.fixture(scope="session")
def frozen_layout(tmp_path_factory):
    result = subprocess.run(["git", "show", f"{BASE}:src/gxr_studio/finish/layout.py"], cwd=ROOT, capture_output=True, text=True)
    if result.returncode:
        pytest.skip(f"检出没有冻结主分支 {BASE[:7]} 的历史；完整历史检出可运行字节对照")
    frozen = tmp_path_factory.mktemp("output-ratio-baseline") / "baseline_layout.py"
    frozen.write_text(result.stdout, encoding="utf-8")
    name = "gxr_studio.finish._output_ratio_baseline"
    spec = importlib.util.spec_from_file_location(name, frozen)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(name, None)


@pytest.mark.parametrize(("style", "ratio", "preset", "portrait"), [("gallery", "3:4", "original", False), ("instant", "16:9", "16:9", True)])
def test_real_frame_ratio_and_stamp_match_the_preflight_size(lib, settings, real_photo, style, ratio, preset, portrait):
    render, shot, w, h = real_photo
    crop = default_crop(preset, w, h, flip=portrait == (w >= h))
    side = {"crop": crop.to_json()}
    edit = EditState(frame=FrameSpec(style=style, output_ratio=ratio), stamp=StampSpec(enabled=True))
    sizes = output_sizes(render, lib, shot, edit, crop.rect, crop.preset)
    for tier, edge in (("original", None), ("large", 3000), ("medium", 2048), ("small", 1600)):
        out = export(lib, settings, render, shot, edit, side, edge)
        with Image.open(out.path) as image:
            assert list(image.size) == sizes["sizes"][tier] == [out.width, out.height]
        rw, rh = map(int, ratio.split(":"))
        assert abs(out.width * rh - out.height * rw) <= max(rw, rh)
        if tier == "original":
            assert out.width * rh == out.height * rw


@pytest.mark.parametrize("style", ("gallery", "leica", "hasselblad", "film", "instant"))
@pytest.mark.parametrize("preset", ("original", "free", "4:3", "xpan-65-24"))
def test_real_legacy_frames_export_byte_identically_to_main(lib, settings, real_photo, frozen_layout, monkeypatch, style, preset):
    """只替换实际主分支的版面函数；编码、EXIF、颜色、裁剪、时间戳与导出全部走真管线。"""
    render, shot, w, h = real_photo
    crop = default_crop(preset, w, h)
    side = {"crop": crop.to_json()}
    edit = EditState(frame=FrameSpec(style=style, output_ratio="original", text="Hello"), stamp=StampSpec(enabled=True))
    current = export(lib, settings, render, shot, edit, side)

    def main_layout(w, h, frame, meta, metrics, preset=None):
        old = SimpleNamespace(**frame.to_json(), keep_ratio=False)
        return frozen_layout.frame_layout(w, h, old, meta, metrics, preset)

    monkeypatch.setattr(frame_mod, "frame_layout", main_layout)
    previous = export(lib, settings, render, shot, edit, side)
    assert (current.width, current.height) == (previous.width, previous.height)
    assert current.path.read_bytes() == previous.path.read_bytes()
