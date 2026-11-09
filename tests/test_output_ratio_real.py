"""现场照片的成品比例与实际导出尺寸；样本和导出均不进仓库。"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from PIL import Image

from gxr_studio.crop import default_crop
from gxr_studio.finish.serve import export_finish
from gxr_studio.finish.sizes import output_sizes
from gxr_studio.finish.spec import FrameSpec, StampSpec
from gxr_studio.render.params import EditState
from gxr_studio.render.service import RenderService

ROOT = Path(__file__).resolve().parents[1]


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
def test_real_natural_frames_match_the_preflight_size(lib, settings, real_photo, style, preset):
    render, shot, w, h = real_photo
    crop = default_crop(preset, w, h)
    side = {"crop": crop.to_json()}
    edit = EditState(frame=FrameSpec(style=style, output_ratio="original", text="Hello"), stamp=StampSpec(enabled=True))
    sizes = output_sizes(render, lib, shot, edit, crop.rect, crop.preset)
    current = export(lib, settings, render, shot, edit, side)
    with Image.open(current.path) as image:
        assert list(image.size) == sizes["sizes"]["original"] == [current.width, current.height]
