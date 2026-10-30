"""datcodec：按代数分派的 DAT 解析/封装。夹具 DAT 从本机现有文件现场读取（不在仓库里），找不到就跳过。"""

from __future__ import annotations

import struct
import zlib

import pytest

import e0_support as t
from gxr_studio.camdata import datcodec as dc

GENS = (127, 129, 131, 135)


def need(gen):
    raw = t.fixture_dat(gen)
    if raw is None:
        pytest.skip(f"g{gen} 夹具 DAT 不在本机")
    return raw


def reseal(blob: bytes) -> bytes:
    b = bytearray(blob)
    struct.pack_into(">I", b, 24, zlib.crc32(b[256:]) & 0xFFFFFFFF)
    struct.pack_into(">I", b, 20, 0)
    struct.pack_into(">I", b, 20, zlib.crc32(b[:256]) & 0xFFFFFFFF)
    return bytes(b)


@pytest.mark.parametrize("gen", GENS)
def test_round_trip_is_byte_identical(gen):
    raw = need(gen)
    dat = dc.parse(raw)
    assert dat.generation == gen
    assert dc.pack(dat) == raw
    # 解析结果不是原字节的影子：重新解析封装结果得到同样的结构
    again = dc.parse(dc.pack(dat))
    assert [sorted(m.entries) for m in again.modules] == [sorted(m.entries) for m in dat.modules]


@pytest.mark.parametrize("gen", GENS)
def test_every_unit_has_eight_entries_with_valid_crcs(gen):
    from gxr_studio.camdata import store

    dat = dc.parse(need(gen))
    for unit in range(3):
        assert sorted(dat.modules[unit].entries) == list(range(1, 9))
        for slot in range(1, 9):
            e = dat.entry_of(unit, slot)
            assert len(e) == 552 and e[0] == slot
            store.check_entry(e)
        assert zlib.crc32(dat.entry_of(unit, 4)[:448]) & 0xFFFFFFFF == dc.STOCK_ENTRY_CRC[unit]


def test_recipe_entries_are_the_same_in_every_generation():
    dats = {g: dc.parse(need(g)) for g in GENS}
    for unit in range(3):
        for slot in range(1, 9):
            assert len({d.entry_of(unit, slot) for d in dats.values()}) == 1


def test_entries_match_the_research_parser_on_the_g123_dat(tmp_path):
    """独立对照：研究代码 `support.parse()`（读 g123 DAT）解出的 24 条，与本编解码器从 g127 解出的逐字节相同。

    在独立进程里运行研究的 support.parse()（它会改 sys.path、环境变量），只取回条目的十六进制。
    """
    import json
    import subprocess
    import sys

    g123 = t.REPO / "firmware/package_v36_g123/SD_ROOT/GXRCE.DAT"
    pack_py = t.REPO / "firmware/engine_v45_verify/candidate/containers/product/pack.py"
    if not (g123.is_file() and pack_py.is_file() and (t.KNOBS_SRC / "support.py").is_file()):
        pytest.skip("研究代码或 g123 DAT 不在本机")
    script = tmp_path / "parse_ref.py"
    script.write_text(
        "import json, sys\n"
        f"sys.path.insert(0, {str(t.KNOBS_SRC)!r})\n"
        "import support\n"
        "mods = support.parse()\n"
        "print(json.dumps([{str(s): e.hex() for s, e in m['entries'].items()} for m in mods]))\n"
    )
    cp = subprocess.run(
        [sys.executable, "-B", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    assert cp.returncode == 0, cp.stderr[-1500:]
    theirs = json.loads(cp.stdout)
    ours = dc.parse(need(127))
    assert len(theirs) == 3
    for unit in range(3):
        assert sorted(int(k) for k in theirs[unit]) == list(range(1, 9))
        for slot in range(1, 9):
            assert bytes.fromhex(theirs[unit][str(slot)]) == ours.entry_of(unit, slot), (unit, slot)


def test_gxui_matches_the_research_ui_data_on_g127():
    """用户条目：研究 ui_data.parse_dat / replace_users 与本编解码器逐字节一致。"""
    ui_path = t.REPO / "firmware/engine_v53_g135/candidate/ui_data.py"
    if not ui_path.is_file():
        pytest.skip("研究代码不在本机")
    ui = t.load_by_path("research_ui_data", ui_path)
    raw = need(127)
    meta, ext, banks, users = ui.parse_dat(raw)
    dat = dc.parse(raw)
    assert dat.users == users == []
    assert dat.ext_config == ext[64 : dc.USER_OFFSET]
    # g127 的菜单只露出前四项（Cont/High/Shad/Satu），后面的项必须是默认 0
    users = [
        {"base": 1, "levels": [1, -1, 2, 0] + [0] * 7},
        {"base": 5, "levels": [0] * 11},
        {"base": 8, "levels": [2, 2, -2, 3] + [0] * 7},
    ]
    want = ui.replace_users(raw, users)
    got = dc.pack(dc.replace_users(dat, users))
    assert got == want
    back = dc.parse(got)
    assert back.users == users
    assert dc.pack(back) == got


@pytest.mark.parametrize("gen", (129, 131, 135))
def test_users_round_trip_on_later_generations(gen):
    dat = dc.parse(need(gen))
    users = [{"base": s, "levels": [0, 1, 0, 0] + [0] * 7} for s in (1, 2, 3, 4, 5, 6, 7, 8)] * 2
    out = dc.pack(dc.replace_users(dat, users))
    back = dc.parse(out)
    assert len(back.users) == 16 and dc.pack(back) == out
    assert back.kv2_block == dat.kv2_block and back.engine == dat.engine


def test_user_entry_rules():
    dat = dc.parse(need(127))
    with pytest.raises(dc.DatError):
        dc.replace_users(dat, [{"base": 9, "levels": [0] * 11}])
    with pytest.raises(dc.DatError):
        dc.replace_users(dat, [{"base": 1, "levels": [0] * 10}])
    with pytest.raises(dc.DatError):
        dc.replace_users(dat, [{"base": 1, "levels": [99] + [0] * 10}])
    with pytest.raises(dc.DatError):
        dc.replace_users(dat, [{"base": 1, "levels": [0] * 11}] * 33)


def test_unregistered_generation_is_refused_not_guessed():
    raw = need(127)
    b = bytearray(raw)
    struct.pack_into(">I", b, 28, 123)
    with pytest.raises(dc.UnsupportedGeneration):
        dc.parse(reseal(bytes(b)))
    for other in (124, 126, 130):
        p = t.REPO / f"firmware/package_v36_g{other}/local/SD_ROOT/GXRCE.DAT"
        p2 = t.REPO / f"firmware/package_v36_g{other}/SD_ROOT/GXRCE.DAT"
        for q in (p, p2):
            if q.is_file():
                with pytest.raises(dc.UnsupportedGeneration):
                    dc.parse(q.read_bytes())
                break


@pytest.mark.parametrize("gen", GENS)
def test_damage_is_rejected(gen):
    raw = need(gen)
    dat = dc.parse(raw)
    assert len(raw) < dc.PROFILES[gen].max_file
    cases = {
        "truncated": raw[:-4],
        "extended": raw + b"\0\0\0\0",
        "bad magic": b"GXRCE002" + raw[8:],
        "body bit flip (crc)": raw[:5000] + bytes([raw[5000] ^ 1]) + raw[5001:],
        "header bit flip (crc)": raw[:40] + bytes([raw[40] ^ 1]) + raw[41:],
    }
    for name, blob in cases.items():
        with pytest.raises(dc.DatError):
            dc.parse(blob)
    # CRC 重新封好之后，结构性改动也必须被别的检查拦住
    # 1) 行池里的一个字节（行池 CRC）
    shared = struct.unpack_from(">II", raw, 128 + 12 * 5 + 4)[0]
    b = bytearray(raw)
    b[shared + 70] ^= 0xFF
    with pytest.raises(dc.DatError):
        dc.parse(reseal(bytes(b)))
    # 2) 原厂 04 钉子：把 map128 改一个字节
    map_at = struct.unpack_from(">II", raw, 128 + 12 * 3 + 4)[0]
    b = bytearray(raw)
    b[map_at + 40] ^= 1
    with pytest.raises(dc.DatError):
        dc.parse(reseal(bytes(b)))
    # 3) 头部里没有建模的字节（保留区）
    b = bytearray(raw)
    b[240] = 1
    with pytest.raises(dc.DatError):
        dc.parse(reseal(bytes(b)))
    # 4) 引擎字节不属于我们解释的范围，改了也能解析，但原样搬回（往返仍然相同）
    b = bytearray(raw)
    b[300] ^= 0x55
    changed = dc.parse(reseal(bytes(b)))
    assert dc.pack(changed) == reseal(bytes(b)) and changed.engine != dat.engine


def test_pack_rejects_overfull_and_bad_entries():
    dat = dc.parse(need(127))
    import copy

    bad = copy.deepcopy(dat)
    bad.modules[0].entries[1] = bad.modules[0].entries[1][:500]
    with pytest.raises(dc.DatError):
        dc.pack(bad)
    bad = copy.deepcopy(dat)
    e = bytearray(bad.modules[0].entries[2])
    e[0] = 3  # 所有者槽位对不上
    bad.modules[0].entries[2] = bytes(e)
    with pytest.raises(dc.DatError):
        dc.pack(bad)


@pytest.mark.parametrize("gen", GENS)
def test_an_edited_entry_survives_pack_and_parse(gen):
    """S9b 的底子：改一个槽位的深层行再封装，重新解析得到的就是改过的条目，其余条目不变。"""
    import copy

    dat = dc.parse(need(gen))
    e = bytearray(dat.modules[0].entries[7])
    e[82 + 10] = min(255, e[82 + 10] + 1)  # B5 曲线里的一个节点
    struct.pack_into(">I", e, 12, zlib.crc32(e[82:115]) & 0xFFFFFFFF)
    edited = copy.deepcopy(dat)
    edited.modules[0].entries[7] = bytes(e)
    out = dc.pack(edited)
    assert len(out) > len(dc.pack(dat)) - 1 and out != dc.pack(dat)
    back = dc.parse(out)
    assert back.modules[0].entries[7] == bytes(e)
    for unit in range(3):
        for slot in range(1, 9):
            if (unit, slot) != (0, 7):
                assert back.entry_of(unit, slot) == dat.entry_of(unit, slot)
    assert dc.pack(back) == out
