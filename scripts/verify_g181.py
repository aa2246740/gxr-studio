"""只读核对 g178/g181/g182 r2 完整记录及 RAW+JPG EXIF，私有结果仅写研究 local/。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

from PIL import Image

from gxr_studio import meta
from gxr_studio.camdata import datcodec, store
from gxr_studio.render.camera_recipe import recipe_tag_payload, recorded_recipe
from gxr_studio.render.params import EditError, RecipeSel
from gxr_studio.render.recipes import CameraData

DAT_HASHES = {
    178: "2cb4d5817c9bcad91194f9c2d02b6c135a83524bfcfa5cdc349afb81bb91fae6",
    181: "1d3cd3294e677cbea04d528ca7034a398d982f35310cf4e192ed645bd1ac8c59",
    182: "8f8cff3dc0f353c9402cfd9b8b17034876dafb53b591d9aec06c7bd9333f2696",
}
LIBRARY_HASH = "0af19a0cca12e58d96fbc783491624696dc477db0f6aade0b220df7821b07b50"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_records(root: Path, home_root: Path) -> tuple[CameraData, list[dict]]:
    cameras = {}
    for generation, package in (
        (178, root / "research/build_g178_r1/local/model"),
        (181, root / "firmware/package_v310b_g181/local/g181-daily"),
        (182, root / "firmware/package_v310b_g182/local/g182-daily-r2"),
    ):
        assert sha(package / "GXRCE.DAT") == DAT_HASHES[generation], "受审 DAT 漂移"
        assert sha(package / "GXRFX.BIN") == LIBRARY_HASH, "受审库漂移"
        raw, library = (package / "GXRCE.DAT").read_bytes(), (package / "GXRFX.BIN").read_bytes()
        assert datcodec.pack(datcodec.parse(raw, library)) == raw, "DAT 精确往返失败"
        home = home_root / f"home-{generation}"
        store.import_from_dat(raw, home, library=library)
        cameras[generation] = CameraData(home)
    rows = []
    review = root / "research/review_g181_r1/local"
    registry_path = root / "research/fit_v4_r2/local/registry/all_full_registry.json"
    registry = json.loads(registry_path.read_text())
    assert registry["chosen_per_unit"] == 23 and registry["per_unit_cap"] == 25 and registry["spare_positions"] == [24, 25]
    assert len(registry["records"]) == 69 and registry["model"] == "native-C040-v2.0"
    exceptions = [row for row in registry["records"] if row.get("owner_materialized_fallback")]
    assert len(exceptions) == 7
    assert all(
        row["admission_basis"] == "OWNER_AUTHORIZED_BIT_IDENTICAL_MATERIALIZATION" and row["original_admission_unpassed"] for row in exceptions
    )
    registered = {(row["unit_index"], row["position"]): row for row in registry["records"]}
    for unit, unit_label in enumerate(("50mm", "28mm", "mount")):
        listing = cameras[181].listing()[unit]
        assert len(listing["recipes"]) == 23
        for position, info in enumerate(listing["recipes"], 1):
            selection = RecipeSel(listing["id"], info["slot"], kind=info.get("kind", "builtin"), id=info.get("id"))
            record = cameras[181].resolve(selection).entry
            old = cameras[178].resolve(selection).entry
            newest = cameras[182].resolve(selection).entry
            assert len(record) == 552 and record == old == newest, (unit, position, "Studio g178/g181/g182 r2")
            row = registered[unit, position]
            source = root / row["record_file"]
            assert sha(source) == row["record_sha256"] and record == source.read_bytes(), (unit, position, "r2 最终登记")
            assert row["unit_mask"] == 1 << unit and row["flags"] == record[1] and row["control_binding"] == datcodec.u16(record, 445)
            assert row["full_record"] and row["independent_per_unit_record"] and not row["shared_curve_Look"]
            for start, size, field in datcodec.CRC_FIELDS:
                assert datcodec.u32(record, field) == datcodec.crc(record[start : start + size]), (unit, position, field)
            for generation in (178, 181):
                source = review / f"colours-v2-{unit_label}/records/g{generation}/{position:02d}.bin"
                expected = source.read_bytes()
                assert len(expected) == 552 and record == expected, (unit, position, generation, "独立复审完整记录")
            rows.append(
                {
                    "unit": listing["id"],
                    "position": position,
                    "bytes_compared": 552,
                    "all_bytes_equal": True,
                    "studio_generations": [178, 181, 182],
                    "flags": row["flags"],
                    "unit_mask": row["unit_mask"],
                    **{key: row.get(key) for key in ("admission_basis", "original_admission_unpassed", "acceptance_note")},
                }
            )
    assert len(rows) == 69
    return cameras[182], rows


def verify_pairs(camera: CameraData, captures: Path) -> list[dict]:
    pairs = []
    for raw_path in sorted(captures.glob("*.DNG")):
        jpg_path = raw_path.with_suffix(".JPG")
        assert jpg_path.is_file(), "本轮验收需要 RAW+JPG 对"
        raw_meta, jpg_meta = meta.read_dng_meta(raw_path), meta.read_jpeg_meta(jpg_path)
        assert raw_meta.error is None and jpg_meta.error is None
        originals = []
        for path in (raw_path, jpg_path):
            with Image.open(path) as im:
                originals.append(im.getexif().get(meta.COPYRIGHT))
        tag = jpg_meta.recipe_tag
        assert tag is not None and raw_meta.recipe_tag is not None, "实拍文件缺少可解析的 GXR1 标签"
        payload = recipe_tag_payload(tag, camera)
        assert payload is not None
        selection, reason = None, None
        try:
            selection = recorded_recipe(tag, camera)
        except EditError as error:
            reason = error.code
        if payload["name"] is None:
            assert selection is None
            reason = "position_not_in_catalog"
        elif selection is not None:
            assert len(camera.resolve(selection).entry) == 552
        pairs.append(
            {
                "stem": raw_path.stem,
                "sha256": {path.suffix: sha(path) for path in (raw_path, jpg_path)},
                "original_copyright_repr": [repr(value) for value in originals],
                "original_values_equal": originals[0] == originals[1],
                "copyright": [raw_meta.copyright, jpg_meta.copyright],
                "copyright_equal": raw_meta.copyright == jpg_meta.copyright,
                "parsed_tags_equal": raw_meta.recipe_tag == tag,
                "unit": tag["unit"],
                "position": tag["position"],
                "studio": payload,
                "selection": None if selection is None else {"kind": selection.kind, "slot": selection.slot, "id": selection.id},
                "unknown_reason": reason,
            }
        )
    assert pairs, "没有 RAW+JPG 对"
    return pairs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--research", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    assert sys.flags.isolated and sys.dont_write_bytecode and os.getpriority(os.PRIO_PROCESS, 0) == 15
    root, out = args.research.resolve(), args.out.resolve()
    assert out.is_relative_to(root / "research/studio_g181_codec_r1/local")
    out.mkdir(parents=True, exist_ok=True)
    registry_path = root / "research/fit_v4_r2/local/registry/all_full_registry.json"
    result = {
        "status": "RUNNING",
        "script_sha256": sha(Path(__file__)),
        "registry": str(registry_path.relative_to(root)),
        "registry_sha256": sha(registry_path),
        "scope": "record equality, CRC and paired EXIF; no new model, fitting or admission acceptance",
    }
    try:
        with tempfile.TemporaryDirectory(prefix="gxr-g181-") as tmp:
            camera, records = verify_records(root, Path(tmp))
            result["records"] = records
            pairs = verify_pairs(camera, root / "captures/g181-field-20261110")
            result["pairs"] = pairs
        assert all(pair["copyright_equal"] and pair["parsed_tags_equal"] for pair in pairs), "成对标签不一致，保留实际原文"
        result["status"] = "PASS"
        print(f"{len(records)} records compared byte by byte; {len(pairs)} RAW+JPG pairs inspected")
    except BaseException as error:
        result.update(status="FAIL", error=str(error))
        raise
    finally:
        (out / "verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
