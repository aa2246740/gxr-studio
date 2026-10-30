"""引擎冻结 E0：studio 的 knobs/menu 与研究代码逐字节一致（552 B 深层行 + 统计）。

三层证据：
1. 黄金清单（只含哈希，在仓库里）——研究代码一次性生成，没有研究目录和理光数据以外的依赖；
2. 现场对拍——研究目录在本机时，同一批用例再用研究的 knobs.py / menu.py 算一遍，逐字节比较；
3. 抽取是逐字的：knobs.py、final.json 与研究件字节相同，menu.py 只改了两行。
"""

from __future__ import annotations

import hashlib
import json
import struct
import zlib

import pytest

import e0_support as t
from gxr_studio.camdata import datcodec as dc, store
from gxr_studio.engine import knobs, menu

ENGINE = t.REPO / "studio/src/gxr_studio/engine"


@pytest.fixture(scope="module")
def dat():
    raw = t.fixture_dat(127)
    if raw is None:
        pytest.skip("g127 夹具 DAT 不在本机")
    return dc.parse(raw)


@pytest.fixture(scope="module")
def data():
    return menu.load()


@pytest.fixture(scope="module")
def cases(data):
    return t.knob_cases(data)


def test_case_set_covers_everything(data, cases):
    assert len(cases) >= 2000
    assert len(data["recipes"]) == 24 and len(data["items"]) == 201
    single = [c for c in cases if c["id"].startswith("s/")]
    assert len(single) == sum(len(i["points"]) for i in data["items"]) == 3430      # 全部单旋钮状态
    assert {(c["unit"], c["slot"]) for c in cases} == {(u, s) for u in range(3) for s in range(1, 9)}
    controls = {data["controls"][data["items"][i]["control"]]["name"]
                for r in data["recipes"] for i in range(r["first_item"], r["first_item"] + r["item_count"])}
    assert controls == {c["name"] for c in data["controls"]}                       # 10 个旋钮全部出现
    raw = [c for c in cases if c["kind"] == "raw"]
    assert {c["wb_method"] for c in raw} == {0, 1} and {c["tone_method"] for c in raw} == {0, 1}


def test_studio_engine_matches_the_golden_list(dat, data, cases):
    golden = json.loads((t.GOLDEN / "knobs_cases.json").read_text())
    assert golden["dat_sha256"] == t.FIXTURE_SHA256[127] and golden["cases"] == len(cases)
    bad = []
    for c in cases:
        out = t.run_case(knobs, menu, data, c, dat.entry_of(c["unit"], 4), dat.entry_of(c["unit"], c["slot"]))
        if t.outcome_digest(out) != golden["digests"][c["id"]]:
            bad.append(c["id"])
    assert bad == []


def test_the_golden_list_notices_a_one_step_change(dat, data, cases, monkeypatch):
    """反向检查：把指数表里若干项各改 1，黄金清单必须报出不一致（否则这套测试没有牙）。"""
    golden = json.loads((t.GOLDEN / "knobs_cases.json").read_text())
    table = list(knobs.EXP_Q12)
    for j in range(10, 250, 3):
        table[j] += 1
    monkeypatch.setattr(knobs, "EXP_Q12", tuple(table))
    bad = 0
    for c in cases:
        out = t.run_case(knobs, menu, data, c, dat.entry_of(c["unit"], 4), dat.entry_of(c["unit"], c["slot"]))
        bad += t.outcome_digest(out) != golden["digests"][c["id"]]
    assert bad > 0


def test_studio_engine_matches_the_research_code_byte_for_byte(dat, data, cases):
    if not (t.KNOBS_SRC / "knobs.py").is_file():
        pytest.skip("研究代码不在本机")
    r_knobs, r_menu = t.research_knobs_and_menu()
    r_data = r_menu.load()
    assert r_data == data
    compared = 0
    for c in cases:
        stock, base = dat.entry_of(c["unit"], 4), dat.entry_of(c["unit"], c["slot"])
        ours = t.run_case(knobs, menu, data, c, stock, base)
        theirs = t.run_case(r_knobs, r_menu, r_data, c, stock, base)
        assert ours == theirs, c["id"]                                        # 条目、统计、错误信息全部相同
        if ours[0] == "ok":
            assert len(ours[1]) == 552
            store.check_entry(ours[1])                                        # 每个输出的 9 处 CRC 自洽
            compared += 1
    assert compared >= 2000


def test_menu_requests_equal_the_research_menu(data, cases):
    if not (t.KNOBS_SRC / "menu.py").is_file():
        pytest.skip("研究代码不在本机")
    r_knobs, r_menu = t.research_knobs_and_menu()
    for c in (c for c in cases if c["kind"] == "menu"):
        a = menu.request(data, c["unit"], c["slot"], c["levels"])
        b = r_menu.request(data, c["unit"], c["slot"], c["levels"])
        assert (a.amount, a.doses, a.wb_method, a.tone_method) == (b.amount, b.doses, b.wb_method, b.tone_method)


def test_menu_refuses_instead_of_clamping(data):
    r = data["recipes"][0]
    items = data["items"][r["first_item"]:r["first_item"] + r["item_count"]]
    levels = [i["default_level"] for i in items]
    for bad in (levels[:-1], levels + [0]):
        with pytest.raises(ValueError):
            menu.request(data, 0, 1, bad)
    worse = list(levels)
    worse[1] = 10 ** 6
    with pytest.raises(ValueError):
        menu.request(data, 0, 1, worse)
    worse[1] = 1.0
    with pytest.raises(ValueError):
        menu.request(data, 0, 1, worse)
    for unit, slot in ((3, 1), (0, 0), (0, 9), (-1, 1), (True, 1)):
        with pytest.raises(ValueError):
            menu.request(data, unit, slot, levels)


def test_apply_is_pure_and_deterministic(dat, data, cases):
    stock, base = dat.entry_of(0, 4), dat.entry_of(0, 5)
    snapshot = (bytes(stock), bytes(base))
    req = knobs.Request(80, (400, -300, 100, 200, 500, -100, 50, 0, 0, 0))
    a = knobs.apply(stock, base, req)
    b = knobs.apply(stock, base, req)
    assert a == b and (stock, base) == snapshot
    # 默认请求原样返回底子
    assert knobs.apply(stock, base)[0] == base


def test_apply_never_returns_a_broken_entry(dat, data, cases):
    """属性：长度 552、9 处 CRC 自洽、曲线单调（前 33 项）。"""
    for c in cases[::7]:
        out = t.run_case(knobs, menu, data, c, dat.entry_of(c["unit"], 4), dat.entry_of(c["unit"], c["slot"]))
        if out[0] != "ok":
            continue
        e = out[1]
        assert len(e) == 552
        for off, size, crc_off in dc.CRC_FIELDS:
            assert struct.unpack_from(">I", e, crc_off)[0] == zlib.crc32(e[off:off + size]) & 0xFFFFFFFF
        for at in (82, 346, 518):
            assert list(e[at:at + 33]) == sorted(e[at:at + 33]), (c["id"], at)


# ---------------------------------------------------------------------------------- 抽取是逐字的

def test_engine_files_match_their_recorded_sources():
    prov = json.loads((ENGINE / "PROVENANCE.json").read_text())
    for rel, sha in prov["engine_file_sha256"].items():
        assert hashlib.sha256((ENGINE / rel).read_bytes()).hexdigest() == sha, rel
    # 没有被记录的文件混进来
    on_disk = {str(p.relative_to(ENGINE)) for p in ENGINE.rglob("*") if p.is_file() and p.name != "PROVENANCE.json"
               and "__pycache__" not in p.parts}
    assert on_disk == set(prov["engine_file_sha256"])


def test_copies_equal_the_research_sources():
    prov = json.loads((ENGINE / "PROVENANCE.json").read_text())
    for rel, meta in prov["files"].items():
        src = t.REPO / meta["source"]
        if not src.is_file():
            pytest.skip("研究目录不在本机")
        assert hashlib.sha256(src.read_bytes()).hexdigest() == meta["source_sha256"], f"{meta['source']} 已变动"
        if meta["kind"] == "byte-identical copy":
            assert (ENGINE / rel).read_bytes() == src.read_bytes()


def test_ast_segments_equal_the_originals():
    """逐字子集：每个被复制的函数/类/常量，AST 与原件一致（缩进、注释以外不可能不同）。"""
    import ast

    prov = json.loads((ENGINE / "PROVENANCE.json").read_text())

    def nodes(path):
        out = {}
        for n in ast.parse(path.read_text()).body:
            name = getattr(n, "name", None)
            if name is None and isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name):
                name = n.targets[0].id
            if name:
                out[name] = ast.dump(n)
        return out

    checked = 0
    for rel in ("matrix_model.py", "colour.py", "sector_model.py", "pretable.py"):
        meta = prov["files"][rel]
        src = t.REPO / meta["source"]
        if not src.is_file():
            pytest.skip("研究目录不在本机")
        original = nodes(src)
        for name, dump in nodes(ENGINE / rel).items():
            if name in original:
                assert dump == original[name], f"{rel}:{name}"
                checked += 1
    # model.py：类 Model 里搬来的方法 + 三个顶层函数
    meta = prov["files"]["model.py"]
    src = t.REPO / meta["source"]
    original_tree = ast.parse(src.read_text())
    original = {n.name: ast.dump(n) for n in original_tree.body if isinstance(n, ast.FunctionDef)}
    cls = next(n for n in original_tree.body if isinstance(n, ast.ClassDef) and n.name == "Model")
    original_methods = {n.name: ast.dump(n) for n in cls.body if isinstance(n, ast.FunctionDef)}
    ours_tree = ast.parse((ENGINE / "model.py").read_text())
    ours_cls = next(n for n in ours_tree.body if isinstance(n, ast.ClassDef) and n.name == "Model")
    moved = [n.name for n in ours_cls.body if isinstance(n, ast.FunctionDef) and n.name != "__init__"]
    assert moved == ["pre", "predict", "lab", "de"]
    for n in ours_cls.body:
        if isinstance(n, ast.FunctionDef) and n.name != "__init__":
            assert ast.dump(n) == original_methods[n.name], n.name
            checked += 1
    for n in ours_tree.body:
        if isinstance(n, ast.FunctionDef):
            assert ast.dump(n) == original[n.name], n.name
            checked += 1
    assert checked == 39


def test_engine_does_not_import_research_or_firmware_code():
    import re

    bad = re.compile(r"^\s*(from|import)\s+(research|firmware|support|closed_loop|model_adapter)\b", re.M)
    for p in list((t.REPO / "studio/src/gxr_studio/engine").glob("*.py")) + list(
            (t.REPO / "studio/src/gxr_studio/camdata").glob("*.py")):
        assert not bad.search(p.read_text()), p.name
        assert "/Volumes/" not in p.read_text(), p.name
