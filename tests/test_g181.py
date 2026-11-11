"""外部受审文件可用时核对共享完整记录和成对实拍 EXIF，不分发私有输入。"""

from __future__ import annotations

import hashlib
import struct

import pytest

import e0_support as t
from g178_support import need_bundle
from gxr_studio import meta
from gxr_studio.camdata import datcodec as dc
from gxr_studio.camdata import store
from gxr_studio.camdata.recipe_tags import parse_recipe_tag, recipe_payload
from gxr_studio.render.camera_recipe import recorded_recipe
from gxr_studio.render.recipes import CameraData
from test_datcodec import reseal
from test_recipe_tag_render import SyntheticCamera
from test_recipe_tags import sample

SYNTHETIC_TAG = "GXR1 0123456789 50 17 799999999-"


@pytest.mark.parametrize("ending", ("", "\0"))
@pytest.mark.parametrize("as_bytes", (False, True))
def test_fixed_width_copyright_padding_preserves_the_canonical_tag(ending, as_bytes):
    text = SYNTHETIC_TAG.ljust(69) + ending
    assert parse_recipe_tag(text.encode("ascii") if as_bytes else text) == parse_recipe_tag(SYNTHETIC_TAG)


@pytest.mark.parametrize(
    "text",
    (
        SYNTHETIC_TAG + " ",
        SYNTHETIC_TAG.ljust(68),
        SYNTHETIC_TAG.ljust(70),
        SYNTHETIC_TAG.ljust(69) + "\0\0",
        SYNTHETIC_TAG.ljust(68) + "\t",
        SYNTHETIC_TAG.ljust(68) + "x",
        SYNTHETIC_TAG.ljust(68) + "é",
        (" " + SYNTHETIC_TAG).ljust(69),
        (SYNTHETIC_TAG + "\0x").ljust(69),
    ),
)
def test_other_padding_and_non_ascii_do_not_become_a_recipe_tag(text):
    assert parse_recipe_tag(text) is None


def test_a_padded_raw_and_jpg_pair_keeps_its_original_copyright_and_reparses_the_old_cache(lib, monkeypatch):
    from PIL import Image

    copyright = SYNTHETIC_TAG.ljust(69)
    for suffix, read in (("DNG", meta.read_dng_meta), ("JPG", meta.read_jpeg_meta)):
        path = sample(lib.root / f"2026/2026-11-10/R0000001.{suffix}", copyright)
        parsed = read(path)
        assert parsed.copyright == copyright and parsed.recipe_tag == parse_recipe_tag(SYNTHETIC_TAG)
    lib.rebuild()
    lib.db.execute("UPDATE shots SET recipe_tag=NULL")
    monkeypatch.setattr(Image, "open", lambda *_a, **_kw: pytest.fail("填充格式的缓存升级不能重读照片"))
    (photo,) = lib.list_shots()
    assert photo["copyright"] == copyright and photo["recipe_tag"] == parse_recipe_tag(SYNTHETIC_TAG)


@pytest.mark.parametrize(("generation", "other"), ((178, 181), (181, 178), (178, 182), (181, 182), (182, 178), (182, 181)))
def test_shared_library_does_not_allow_exchanging_engine_labels(generation, other):
    raw, library = need_bundle(generation)
    changed = bytearray(raw)
    struct.pack_into(">I", changed, 28, other)
    with pytest.raises(dc.DatError, match="引擎或链接布局未登记"):
        dc.parse(reseal(bytes(changed)), library)


def verifier():
    return t.load_by_path("verify_g181", t.STUDIO / "scripts/verify_g181.py")


def test_the_rejected_g182_e4_candidate_is_not_registered():
    _, library = need_bundle(182)
    path = t.REPO / "firmware/package_v310b_g182/local/g182-daily-REVIEW_PENDING/GXRCE.DAT"
    if not path.is_file():
        pytest.skip("已否决的 g182 e4 对照副本不在本机")
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == "e4d288b8ed608c48841720500bfdb56386f73495e492de05b4d281927830d504"
    with pytest.raises(dc.DatError, match="引擎或链接布局未登记"):
        dc.parse(raw, library)


@pytest.mark.parametrize("position", (24, 25, 30))
@pytest.mark.parametrize("generation", (181, 182))
def test_empty_and_out_of_catalog_positions_never_borrow_a_name_or_selection(position, generation):
    camera = SyntheticCamera()
    camera.manifest["generation"] = generation
    tag = parse_recipe_tag(f"GXR1 6a3a5a3c02 28 {position:02d} A99999999-")
    payload = recipe_payload(tag)
    assert payload["name"] is None and payload["catalogued"] is False
    assert recorded_recipe(tag, camera) is None


def test_all_69_default_records_match_both_studio_and_reviewed_firmware_generations(tmp_path):
    need_bundle(178)
    need_bundle(181)
    need_bundle(182)
    if not (t.RESEARCH / "review_g181_r1/local/colours-v2-28mm/records").is_dir():
        pytest.skip("g181 独立复审完整记录不在本机")
    _, rows = verifier().verify_records(t.REPO, tmp_path)
    assert len(rows) == 69 and sum(row["bytes_compared"] for row in rows) == 38088
    assert all(row["all_bytes_equal"] for row in rows)


@pytest.mark.parametrize("generation", (181, 182))
def test_field_raw_and_jpg_tags_agree_and_unknown_positions_remain_unnamed(tmp_path, generation):
    captures = t.REPO / "captures/g181-field-20261110"
    if not captures.is_dir():
        pytest.skip("g181 成对实拍文件不在本机")
    raw, library = need_bundle(generation)
    store.import_from_dat(raw, tmp_path, library=library)
    pairs = verifier().verify_pairs(CameraData(tmp_path), captures)
    assert len(pairs) == 7
    assert all(pair["original_values_equal"] and pair["copyright_equal"] and pair["parsed_tags_equal"] for pair in pairs)
    assert sorted(pair["position"] for pair in pairs) == [1, 6, 6, 6, 9, 30, 30]
    for pair in pairs:
        if pair["position"] == 30:
            assert pair["studio"]["name"] is None and pair["unknown_reason"] == "position_not_in_catalog"
        else:
            assert pair["selection"] is not None and pair["unknown_reason"] is None
            assert pair["studio"]["amount_unknown"] is False
