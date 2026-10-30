"""camdata：从 DAT 提取三个单元 × 8 个配方 + 原厂 04 到应用数据目录；标定包拆分；理光字节不进仓库。"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import numpy as np
import pytest

import e0_support as t
from gxr_studio.camdata import calib, store
from gxr_studio.camdata import datcodec as dc

GENS = (127, 129, 131, 135)


def need(gen):
    raw = t.fixture_dat(gen)
    if raw is None:
        pytest.skip(f"g{gen} 夹具 DAT 不在本机")
    return raw


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "appdata"  # conftest 已经在 tmp_path 下建了 home/，这里另用一个干净目录
    h.mkdir()
    return h


def tree(root: Path):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


@pytest.mark.parametrize("gen", GENS)
def test_import_extracts_three_units_by_eight_plus_stock(home, gen):
    raw = need(gen)
    m = store.import_from_dat(raw, home, source_name="GXRCE.DAT")
    assert m["generation"] == gen and m["source"]["sha256"] == hashlib.sha256(raw).hexdigest()
    names = tree(home / "camera-data")
    want = ["manifest.json", "dat-template/GXRCE.DAT"]
    for unit in dc.NAMES:
        want.append(f"units/{unit}/stock04.entry")
        want += [f"units/{unit}/recipes/{s:02d}.entry" for s in range(1, 9)]
    assert sorted(names) == sorted(want) and len(names) == 1 + 1 + 3 * 9
    dat = dc.parse(raw)
    for u, unit in enumerate(dc.NAMES):
        assert store.load_stock04(home, unit) == dat.entry_of(u, 4) == store.load_entry(home, unit, 4)
        for s in range(1, 9):
            assert store.load_entry(home, u, s) == dat.entry_of(u, s)
    assert store.load_template(home) == raw
    assert store.verify(home)["generation"] == gen
    # 提取只写 home，不写别处
    assert [p.name for p in home.iterdir()] == ["camera-data"]


def test_verify_detects_tampering_and_loss(home):
    store.import_from_dat(need(127), home)
    p = home / "camera-data/units/qlaunch/recipes/05.entry"
    good = p.read_bytes()
    p.write_bytes(good[:100] + bytes([good[100] ^ 1]) + good[101:])
    with pytest.raises(store.CamdataError):
        store.verify(home)
    p.write_bytes(good)
    store.verify(home)
    p.unlink()
    with pytest.raises(store.CamdataError):
        store.verify(home)


def test_entry_crc_check_catches_a_flipped_byte(home):
    raw = need(127)
    e = dc.parse(raw).entry_of(1, 2)
    store.check_entry(e)
    bad = bytearray(e)
    bad[300] ^= 1
    with pytest.raises(store.CamdataError):
        store.check_entry(bytes(bad))
    with pytest.raises(store.CamdataError):
        store.check_entry(e[:-1])


def test_reimport_replaces_and_a_bad_dat_leaves_the_old_data(home):
    store.import_from_dat(need(127), home)
    before = {n: (home / "camera-data" / n).read_bytes() for n in tree(home / "camera-data")}
    with pytest.raises(dc.DatError):
        store.import_from_dat(need(135)[:-8], home)
    assert {n: (home / "camera-data" / n).read_bytes() for n in tree(home / "camera-data")} == before
    store.import_from_dat(need(135), home)
    assert store.read_manifest(home)["generation"] == 135
    assert sorted(p.name for p in home.iterdir()) == ["camera-data"]  # 没有 .new / .old 残留
    store.verify(home)


def test_unregistered_generation_is_not_imported(home):
    import struct
    import zlib

    b = bytearray(need(127))
    struct.pack_into(">I", b, 28, 130)
    struct.pack_into(">I", b, 24, zlib.crc32(b[256:]) & 0xFFFFFFFF)
    struct.pack_into(">I", b, 20, 0)
    struct.pack_into(">I", b, 20, zlib.crc32(b[:256]) & 0xFFFFFFFF)
    with pytest.raises(dc.UnsupportedGeneration):
        store.import_from_dat(bytes(b), home)
    assert not (home / "camera-data").exists()


# ----------------------------------------------------------------------------- 标定包


@pytest.fixture(scope="module")
def sources():
    src = calib.dev_sources(t.REPO)
    if not all(p.is_file() for pair in src.values() for p in pair):
        pytest.skip("研究目录里的标定文件不在本机")
    return src


def test_calibration_pack_has_no_stock_and_rebuilds_s_eff_bit_for_bit(home, sources):
    store.import_from_dat(need(127), home)
    m = calib.import_calibration(home, sources)
    assert sorted(m["files"]) == ["ctx_1600.npz", "ctx_200.npz", "ring_model_1600.json", "ring_model_200.json"]
    for iso in (200, 1600):
        with np.load(home / "calib" / f"ctx_{iso}.npz", allow_pickle=False) as z:
            assert sorted(z.files) == ["d0", "d1"]  # 没有 stock，也没有 S_eff
        with np.load(sources[iso][0], allow_pickle=False) as z:
            model, info = calib.model_for(home, iso)
            assert info == {"iso": iso, "calibrated": True, "model_iso": iso}
            c = model.engine.ctx
            assert np.array_equal(c.S_eff, z["S_eff"]) and np.array_equal(c.stock, z["stock"])
            assert np.array_equal(c.d0, z["d0"]) and np.array_equal(c.d1, z["d1"])


def test_other_isos_use_the_nearest_calibration_and_say_so(home, sources):
    store.import_from_dat(need(127), home)
    calib.import_calibration(home, sources)
    for iso, model_iso in ((100, 200), (400, 200), (800, 200), (3200, 200), (6400, 200)):
        _, info = calib.model_for(home, iso)
        assert info == {"iso": iso, "calibrated": False, "model_iso": model_iso}


def test_calibration_with_a_different_stock_is_refused(home, sources):
    store.import_from_dat(need(127), home)
    with np.load(sources[200][0], allow_pickle=False) as z:
        arrays = {k: z[k] for k in z.files}
    arrays["stock"] = arrays["stock"].copy()
    arrays["stock"][3, 0] += 1
    forged = home.parent / "forged.npz"
    np.savez(forged, **arrays)
    with pytest.raises(calib.CalibError):
        calib.import_calibration(home, {200: (forged, sources[200][1])})


def test_calibration_tampering_is_detected(home, sources):
    store.import_from_dat(need(127), home)
    calib.import_calibration(home, sources)
    ring = home / "calib/ring_model_200.json"
    ring.write_text(ring.read_text().replace("0.99", "0.98", 1))
    with pytest.raises(calib.CalibError):
        calib.model_for(home, 200)


# ----------------------------------------------------------------------------- 理光字节不进仓库


def ricoh_row_needles():
    needles = {}
    for gen in GENS:
        raw = t.fixture_dat(gen)
        if raw is None:
            continue
        dat = dc.parse(raw)
        for unit in range(3):
            for slot in range(1, 9):
                e = dat.entry_of(unit, slot)
                for off, size in zip(dc.OFFSETS, dc.ROW_BYTES):
                    row = bytes(e[off : off + size])
                    if len(set(row)) > 3:  # 全 0 / 全相同的行没有区分度
                        needles[row] = f"{gen}/{unit}/{slot}@{off}"
    return needles


def find_rows(roots, needles):
    """返回 (扫描文件数, 命中列表)。原始字节和十六进制（不分大小写）两种写法都查。"""
    hits, scanned = [], 0
    hex_needles = {row.hex().encode(): where for row, where in needles.items()}
    for root in roots:
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in {"local", "__pycache__", ".venv"}]
            for name in files:
                blob = (Path(dirpath) / name).read_bytes()
                scanned += 1
                low = blob.lower()
                hits += [f"{name}: 原始 {where}" for row, where in needles.items() if row in blob]
                hits += [f"{name}: 十六进制 {where}" for h, where in hex_needles.items() if h in low]
    return scanned, hits


def test_row_scanner_finds_planted_rows(tmp_path):
    needles = ricoh_row_needles()
    if not needles:
        pytest.skip("没有夹具 DAT")
    row = next(iter(needles))
    (tmp_path / "a.bin").write_bytes(b"xx" + row + b"yy")
    (tmp_path / "b.txt").write_text("data = '" + row.hex().upper() + "'\n")
    (tmp_path / "c.txt").write_text("nothing here\n")
    scanned, hits = find_rows([tmp_path], needles)
    assert scanned == 3 and len(hits) >= 2


def test_no_ricoh_rows_in_repo_source_or_tests():
    """24 个配方条目里每一种行（A5/B5/色表/矩阵/…），原始字节和十六进制写法都不能出现在代码、测试、黄金清单、报告里。"""
    needles = ricoh_row_needles()
    if not needles:
        pytest.skip("没有夹具 DAT")
    roots = [t.STUDIO / "src", t.STUDIO / "tests", t.STUDIO / "web"]
    for extra in (
        t.REPO / "research/studio_engine_e0",
        t.REPO / "research/studio_design",
        t.REPO / "research/studio_m1",
    ):
        if extra.is_dir():
            roots.append(extra)
    scanned, hits = find_rows(roots, needles)
    assert scanned > 30 and hits == []


def test_no_dat_or_entry_files_outside_ignored_dirs():
    bad = []
    roots = [t.STUDIO]
    research_e0 = t.REPO / "research/studio_engine_e0"
    if research_e0.is_dir():
        roots.append(research_e0)
    for root in roots:
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in {"local", "__pycache__", ".venv"}]
            bad += [str(Path(dirpath) / n) for n in files if n.lower().endswith((".dat", ".entry", ".bin"))]
    assert bad == []


def test_golden_lists_hold_digests_only():
    for name in ("knobs_cases.json", "render_frames.json"):
        p = t.GOLDEN / name
        if not p.is_file():
            pytest.skip("黄金清单不在")
        text = p.read_text()
        data = json.loads(text)
        blob = json.dumps(data)
        # 单个字符串最长 64 个十六进制（SHA-256），没有 552 B（1104 个十六进制字符）的条目
        import re

        assert max((len(m) for m in re.findall(r"[0-9a-f]{40,}", blob)), default=0) <= 64
