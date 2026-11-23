"""g262 测试用的合成 DNG+JPG、Look+微调标签编码与桌面/手机 UI 夹具构造（不含任何实拍数据或理光字节）。"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from gxr_studio.camdata import calib, store
from gxr_studio.camdata import datcodec as dc
from gxr_studio.camdata.recipe_tags import KNOB_ORDER, parse_recipe_tag
from gxr_studio.render.camera_recipe import recipe_tag_payload, recorded_recipe
from gxr_studio.render.recipes import CameraData
from raw_support import bayer_scene, build_dng
from support import samples
from support.dng_writer import COLOR_MATRIX
from support.mobile_ui_calib import pack_files, write_pack
from test_g250 import KUBIK_LIB_SHA256

G262_DAT_SHA256 = "e8fb42e059bb811a46f5bc96d03df09d3b927f2f51f5f9ac25e033c941dd0c72"
G261_DAT_SHA256 = "ee9dfb5a5b7a357af29e7893b0dcb2dc2f1f51e68c55bc4ece076e0286761666"
G262_REL_DIR = "firmware/package_v310b_tweak_per_look_r2/local/test262-REVIEW_PENDING"
G261_REL_DIR = "firmware/package_v310b_tweak_per_look_r1/local/test261-REVIEW_PENDING"
KUBIK_CATALOG_ID = "0ccc86c19b"
LENS_50 = "GR LENS A12 50mm F2.5 MACRO"
_TAG_ALPHA = "0123456789ABCDEFGHI"

# KUBIK 目录（0ccc86c19b）50mm 位置码：
# 01 GRPos, 03 HC BW, 04 Std, 56 Acros, 59 Tri-X 400, 62 K-0815, 65 K-PACIFIC, 68 K-KINO。
KINO_FINETUNE: dict[str, int] = {
    "amount": 80,
    "contrast": -2,
    "highlight": 3,
    "shadow": -1,
    "fade": 2,
    "saturation": 4,
    "warmth": -3,
    "tint": 2,
    "split": -1,
}
KINO_ZERO_AMOUNT: dict[str, int] = {
    "amount": 0,
    "contrast": -2,
    "highlight": 0,
    "shadow": 0,
    "fade": 0,
    "saturation": 0,
    "warmth": 0,
    "tint": 0,
    "split": 0,
}
KINO_ZERO: dict[str, int] = {
    "amount": 100,
    "contrast": 0,
    "highlight": 0,
    "shadow": 0,
    "fade": 0,
    "saturation": 0,
    "warmth": 0,
    "tint": 0,
    "split": 0,
}
ACROS_FINETUNE: dict[str, int] = {"contrast": -3, "highlight": 8, "shadow": 2, "fade": 4, "filter": -5}
GRPOS_FINETUNE: dict[str, int] = {
    "amount": 75,
    "contrast": 2,
    "highlight": -1,
    "shadow": -3,
    "fade": 1,
    "saturation": -4,
    "warmth": 3,
    "tint": -2,
    "split": 1,
}
HCBW_FINETUNE: dict[str, int] = {"contrast": 4, "highlight": -2, "shadow": 1, "fade": 3, "filter": -4}


def encode_tag(unit: str, position: int, levels: dict[str, int]) -> str:
    chars = ["-" if name not in levels else _TAG_ALPHA[levels[name] // 10 if name == "amount" else levels[name] + 9] for name in KNOB_ORDER]
    return f"GXR1 {KUBIK_CATALOG_ID} {unit} {position:02d} {''.join(chars)}"


_MAX_AMT: dict[str, int] = {**KINO_ZERO, "amount": 150, "split": -9}
_ACROS_ZERO: dict[str, int] = {"contrast": 0, "highlight": 0, "shadow": 0, "fade": 0, "filter": 0}
_TRIX_POS: dict[str, int] = {"contrast": 5, "highlight": -4, "shadow": -3, "fade": 2, "filter": 6}
_STD_FT: dict[str, int] = {"contrast": 3, "highlight": -2, "shadow": 2, "fade": 1, "saturation": -3, "warmth": 4, "tint": -1, "split": 2}

TAG_CASES: tuple[tuple[str, str, str, str, dict[str, int]], ...] = (
    ("kino_finetune_50", encode_tag("50", 68, KINO_FINETUNE), "record", "K-KINO", KINO_FINETUNE),
    ("kino_zero_amount_28", encode_tag("28", 69, KINO_ZERO_AMOUNT), "record", "K-KINO", KINO_ZERO_AMOUNT),
    ("kino_max_amount_mt", encode_tag("MT", 70, _MAX_AMT), "record", "K-KINO", _MAX_AMT),
    ("pacific_zero_50", encode_tag("50", 65, KINO_ZERO), "record", "K-PACIFIC", KINO_ZERO),
    ("acros_finetune_50", encode_tag("50", 56, ACROS_FINETUNE), "record", "Acros", ACROS_FINETUNE),
    ("acros_zero_28", encode_tag("28", 57, _ACROS_ZERO), "record", "Acros", _ACROS_ZERO),
    ("trix_pos_filter_mt", encode_tag("MT", 61, _TRIX_POS), "record", "Tri-X 400", _TRIX_POS),
    ("grpos_finetune_50", encode_tag("50", 1, GRPOS_FINETUNE), "builtin", "GRPos", GRPOS_FINETUNE),
    ("hcbw_finetune_28", encode_tag("28", 3, HCBW_FINETUNE), "builtin", "HC BW", HCBW_FINETUNE),
    ("std_finetune_mt", encode_tag("MT", 4, _STD_FT), "builtin", "Std", _STD_FT),
)


def need_g262() -> tuple[bytes, bytes, Path, Path]:
    parent = samples.sample_parent()
    dat_path = parent / G262_REL_DIR / "GXRCE.DAT"
    fx_path = parent / "firmware/package_v310b_kubik_lib_r6/local/test-REVIEW_PENDING/GXRFX.BIN"
    if not dat_path.is_file():
        samples.missing("g262 本地 DAT 不在本机")
    if not fx_path.is_file():
        samples.missing("KUBIK 本地测试库不在本机")
    raw, library = dat_path.read_bytes(), fx_path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == G262_DAT_SHA256, "g262 DAT 摘要已变化"
    assert hashlib.sha256(library).hexdigest() == KUBIK_LIB_SHA256, "KUBIK 库摘要已变化"
    return raw, library, dat_path, fx_path


def need_g261() -> tuple[bytes, Path]:
    dat_path = samples.sample_parent() / G261_REL_DIR / "GXRCE.DAT"
    if not dat_path.is_file():
        samples.missing("g261 本地 DAT 不在本机")
    raw = dat_path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == G261_DAT_SHA256, "g261 DAT 摘要已变化"
    return raw, dat_path


def synth_tagged_pair(
    out_dir: Path,
    stem: str,
    tag: str,
    *,
    width: int = 64,
    height: int = 48,
    seed: int = 262,
    when: str = "2026:11:22 10:00:00",
) -> tuple[Path, Path]:
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float64)
    scene = np.stack(
        [
            np.clip(0.15 + 0.55 * xx / max(1, width - 1) + 0.08 * rng.random((height, width)), 0.02, 0.92),
            np.clip(0.18 + 0.45 * yy / max(1, height - 1) + 0.08 * rng.random((height, width)), 0.02, 0.92),
            np.clip(0.62 - 0.38 * xx / max(1, width - 1) + 0.08 * rng.random((height, width)), 0.02, 0.92),
        ],
        axis=-1,
    )
    neutral = (0.55, 1.0, 0.65)
    dng_bytes = build_dng(
        bayer_scene(height, width, scene * np.array(neutral), white=4095),
        bps=16,
        white=4095,
        neutral=neutral,
        orientation=1,
        iso=200,
        when=when,
        lens=f"RICOH {LENS_50}",
        exif_entries=[(42036, "ASCII", LENS_50)],
        root_extra=[
            (33432, "ASCII", tag),
            (50721, "SRATIONAL", COLOR_MATRIX),
            (50727, "RATIONAL", [1.0, 1.0, 1.0]),
            (50778, "SHORT", [21]),
        ],
    )
    dng_path, jpg_path = out_dir / f"{stem}.DNG", out_dir / f"{stem}.JPG"
    dng_path.write_bytes(dng_bytes)
    exif = Image.Exif()
    exif[271], exif[272], exif[274], exif[306], exif[33432] = "RICOH", "GXR A12", 1, when, tag
    sub = exif.get_ifd(0x8769)
    sub[34855], sub[36867], sub[36868], sub[42036] = 200, when, when, LENS_50
    rgb8 = np.clip(np.round(np.power(scene, 1.0 / 2.2) * 255.0), 0, 255).astype(np.uint8)
    Image.fromarray(rgb8, "RGB").save(jpg_path, "JPEG", quality=92, exif=exif)
    return dng_path, jpg_path


def install_g262_calibrated_home(home: Path, raw: bytes, library: bytes) -> CameraData:
    home.mkdir(parents=True, exist_ok=True)
    store.import_from_dat(raw, home, library=library)
    _dat, entries = store.entries_from_dat(raw, library)
    write_pack(calib.calib_dir(home), pack_files({name: entries[(unit, 4)] for unit, name in enumerate(dc.NAMES)}))
    return CameraData(home)


def build_ui_fixture(cam: CameraData) -> dict[str, Any]:
    tags = {
        "kino_ft": encode_tag("50", 68, KINO_FINETUNE),
        "kino_amt0": encode_tag("50", 68, KINO_ZERO_AMOUNT),
        "pacific_zero": encode_tag("50", 65, KINO_ZERO),
        "acros_ft": encode_tag("50", 56, ACROS_FINETUNE),
        "grpos_ft": encode_tag("50", 1, GRPOS_FINETUNE),
    }

    def _pair(cid: str, tag_text: str) -> tuple[dict[str, Any], dict[str, Any]]:
        parsed = parse_recipe_tag(tag_text)
        assert parsed is not None
        sel = recorded_recipe(parsed, cam)
        assert sel is not None
        photo = {
            "id": cid,
            "name": f"{cid}.DNG",
            "has_raw": True,
            "has_jpg": True,
            "iso": 200,
            "lens": LENS_50,
            "copyright": tag_text,
            "recipe_tag": recipe_tag_payload(parsed, cam),
        }
        return (
            {"id": cid, "photo": photo, "editRecipe": sel.to_json(), "recipeFromCamera": True},
            {"id": cid, "tag": tag_text, "lens": LENS_50, "recipe": sel.to_json()},
        )

    built = [_pair(cid, tag_text) for cid, tag_text in tags.items()]
    return {
        "listing": cam.listing(),
        "desktopCases": [d for d, _ in built],
        "mobileAdoptCases": [m for _, m in built],
        "halfframe": {
            "first": {
                "id": "hf1",
                "name": "R0000101",
                "has_raw": True,
                "has_jpg": True,
                "pair": {"role": "first", "partner_id": "hf2", "first_half": "left"},
                "copyright": tags["kino_ft"],
                "recipe_tag": recipe_tag_payload(parse_recipe_tag(tags["kino_ft"]), cam),
            },
            "second": {
                "id": "hf2",
                "name": "R0000102",
                "has_raw": True,
                "has_jpg": True,
                "diptych": True,
                "pair": {"role": "second", "partner_id": "hf1", "first_half": "left"},
                "copyright": tags["acros_ft"],
                "recipe_tag": recipe_tag_payload(parse_recipe_tag(tags["acros_ft"]), cam),
            },
        },
        "mobileHalfframeTags": [
            {"id": "kino_ft", "tag": tags["kino_ft"], "amountLevels": list(range(0, 151, 10))},
            {"id": "kino_amt0", "tag": tags["kino_amt0"], "amountLevels": list(range(0, 151, 10))},
            {"id": "pacific_zero", "tag": tags["pacific_zero"], "amountLevels": list(range(0, 151, 10))},
            {"id": "acros_ft", "tag": tags["acros_ft"], "amountLevels": None},
        ],
    }


def stage_web_assets(repo: Path, dst: Path) -> Path:
    dst.mkdir(parents=True, exist_ok=True)
    for path in (repo / "web/assets").glob("*.js"):
        shutil.copyfile(path, dst / path.name)
    (dst / "package.json").write_text('{"type":"module"}\n', encoding="utf-8")
    (dst / "api.js").write_text(
        "export class ApiError extends Error {}\n"
        f'const META = {{ lens: "{LENS_50}", iso: 200 }};\n'
        'export const api = async (u) => (u.includes("/luts") ? { luts: [] } : u.includes("/meta") ? META : []);\n'
        'export const apiBlob = async () => { throw new Error("unexpected apiBlob"); };\n',
        encoding="utf-8",
    )
    return dst
