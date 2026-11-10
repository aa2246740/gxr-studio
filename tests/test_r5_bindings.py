"""独立合成字节验证 r5 的有限代数关系、CRC 和完整引擎布局拒收。"""

from __future__ import annotations

import hashlib
import struct

import pytest

from gxr_studio.camdata import datcodec as dc
from gxr_studio.camdata import intake, r5
from gxr_studio.camdata.engine_bindings import EngineDescription
from test_datcodec import reseal


def synthetic_shell(monkeypatch, generation=181):
    engine = bytearray(b"independent synthetic engine....")
    struct.pack_into(">I", engine, 4, 256)
    reloc_at = 256 + len(engine)
    raw = bytearray(256) + engine + struct.pack(">II", 260, 256)
    raw[:12] = b"GXRCE001\0\1\1\0"
    struct.pack_into(">I", raw, 16, len(raw))
    struct.pack_into(">I", raw, 28, generation)
    struct.pack_into(">IIH", raw, 112, 256, 16, 1)
    cursor = 256
    for index, size in enumerate((len(engine), 8, 0, 0, 0, 0, 0)):
        struct.pack_into(">HHII", raw, 128 + 12 * index, index + 1, 0, cursor, size)
        cursor += size
    binding = EngineDescription(hashlib.sha256(engine).hexdigest(), 256, 16, ((260, 256),), len(raw))
    monkeypatch.setitem(dc.BOUND_ENGINES, generation, (binding,))
    return reseal(bytes(raw)), reloc_at


@pytest.mark.parametrize("damage", ("engine", "entry", "bss", "relocation", "linked_end", "label", "relocation_word"))
@pytest.mark.parametrize("generation", (181, 182))
def test_resealed_unregistered_engine_or_layout_is_rejected_before_library_lookup(monkeypatch, damage, generation):
    raw, reloc_at = synthetic_shell(monkeypatch, generation)
    with pytest.raises(dc.DatError, match="缺少 GXRFX.BIN"):
        dc.parse(raw)
    changed = bytearray(raw)
    offsets = {"engine": 280, "entry": 115, "bss": 119, "relocation": reloc_at + 7, "label": 31, "relocation_word": 263}
    if damage == "linked_end":
        changed.extend(bytes(4))
        struct.pack_into(">I", changed, 16, len(changed))
        struct.pack_into(">I", changed, 160, 4)
        for index in range(3, 7):
            struct.pack_into(">I", changed, 132 + 12 * index, len(changed))
    elif damage == "label":
        struct.pack_into(">I", changed, 28, 178)
    else:
        changed[offsets[damage]] ^= 1
    with pytest.raises(dc.DatError) as rejected:
        dc.parse(reseal(bytes(changed)))
    assert "缺少" not in str(rejected.value)


def synthetic_library(monkeypatch):
    cat = bytearray(256)
    cat[:12] = b"GXRCL002\0\1\1\0"
    struct.pack_into(">I", cat, 12, 2)
    struct.pack_into(">I", cat, 16, len(cat))
    struct.pack_into(">I", cat, 28, 175)  # 目录壳保留原生 bank 代数，与库头 178、DAT 181 分开
    cat = reseal(bytes(cat))
    library = bytearray(64) + cat
    library[:12] = b"GXRLB003\0\3\0@"
    struct.pack_into(">6I", library, 12, len(library), len(cat), dc.crc(cat), 7, 3, 0)
    struct.pack_into(">II", library, 36, 178, len(cat))
    struct.pack_into(">I", library, 32, dc.crc(library[:64]))
    monkeypatch.setitem(r5.LIBRARY_SHA256, 178, hashlib.sha256(library).hexdigest())
    return bytes(library), cat


def test_registered_g178_g181_and_g182_r2_share_the_library_generation(monkeypatch):
    library, cat = synthetic_library(monkeypatch)
    assert r5._catalogue(library, 178) == cat
    assert r5._catalogue(library, 181) == cat
    assert r5._catalogue(library, 182) == cat
    for generation in (177, 179, 180, 183, 999):
        with pytest.raises(dc.UnsupportedGeneration):
            r5._catalogue(library, generation)
    with pytest.raises(dc.DatError, match="不匹配"):
        r5._catalogue(library, 176)


@pytest.mark.parametrize(
    "damage", ("truncated", "trailing", "payload", "header_crc", "catalogue_crc", "library_generation", "catalogue_header")
)
@pytest.mark.parametrize("generation", (181, 182))
def test_library_hash_header_and_catalogue_checks_all_remain_enforced(monkeypatch, damage, generation):
    library, _ = synthetic_library(monkeypatch)
    changed = bytearray(library)
    if damage == "truncated":
        changed.pop()
    elif damage == "trailing":
        changed.extend(b"x")
    else:
        offsets = {"payload": 150, "header_crc": 35, "catalogue_crc": 64 + 23, "library_generation": 39, "catalogue_header": 64 + 31}
        changed[offsets[damage]] ^= 1
    with pytest.raises(dc.DatError, match="不匹配"):
        r5._catalogue(bytes(changed), generation)
    if damage not in ("truncated", "trailing"):
        # 独立登记合成摘要，才能单独执行其后的 CRC 与库内代数检查。
        if damage in ("library_generation", "catalogue_crc"):
            if damage == "catalogue_crc":
                struct.pack_into(">I", changed, 20, dc.crc(changed[64:]))
            changed[32:36] = bytes(4)
            struct.pack_into(">I", changed, 32, dc.crc(changed[:64]))
        monkeypatch.setitem(r5.LIBRARY_SHA256, 178, hashlib.sha256(changed).hexdigest())
        with pytest.raises(dc.DatError, match="CRC|代数"):
            r5._catalogue(bytes(changed), generation)


@pytest.mark.parametrize("generation", (179, 180, 183, 999))
def test_unknown_generations_are_rejected_even_with_a_registered_engine(monkeypatch, generation):
    raw, _ = synthetic_shell(monkeypatch)
    changed = bytearray(raw)
    struct.pack_into(">I", changed, 28, generation)
    with pytest.raises(intake.IntakeError) as rejected:
        intake.inspect_dat(reseal(bytes(changed)), b"synthetic library")
    assert rejected.value.code == "generation_unsupported"
    assert "181" in rejected.value.extra["detail"]
