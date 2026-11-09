"""不依赖研究副本的协议拒收与 16 位行索引测试，数据全部独立合成。"""

from __future__ import annotations

import struct
import zlib

import pytest

from gxr_studio.camdata import datcodec as dc
from gxr_studio.camdata import intake, r5
from test_camdata_api import client as client
from test_camdata_api import reader as reader


def test_wide_record_uses_all_ten_independent_rows_and_16_bit_indices():
    sizes = (62, 33, 96, 56)
    shared = bytearray(80)
    pools = []
    for kind, size in enumerate(sizes):
        pools.append((1, size, len(shared)))
        shared.extend(bytes([kind + 1]) * size)
    shared.extend(bytes((-len(shared)) % 4))
    struct.pack_into(">I", shared, 64, len(shared))
    area = bytearray(48 + 44)
    struct.pack_into(">HH", area, 8, 1, 1)
    for kind, size in enumerate(sizes):
        struct.pack_into(">H", area, 16 + 2 * kind, 131)
        struct.pack_into(">H", area, 24 + 2 * kind, 131)
        area.extend(bytes([65 + kind]) * (131 * size))
        area.extend(bytes((-len(area)) % 4))
    shared.extend(area)
    desc = bytearray(44)
    desc[:4] = bytes((6, 7, 0, 2))
    desc[4:8] = b"test"
    for i in range(10):
        struct.pack_into(">H", desc, 8 + 2 * i, 32768 | 130)
    entry = r5._entry(bytes(desc), bytes(shared), pools, wide=True)
    assert len(entry) == 552 and entry[:2] == bytes((6, 7)) and entry[445:447] == bytes((0, 2))
    assert entry[20:82] == b"A" * 62 and entry[284:346] == b"A" * 62 and entry[456:518] == b"A" * 62
    assert entry[82:115] == b"B" * 33 and entry[346:445] == b"B" * 99 and entry[518:551] == b"B" * 33
    assert entry[115:211] == b"C" * 96 and entry[216:272] == b"D" * 56
    assert dc.u32(entry, 16) == zlib.crc32(b"C" * 96)
    struct.pack_into(">H", desc, 8, 32768 | 131)
    with pytest.raises(dc.DatError, match="索引越界"):
        r5._entry(bytes(desc), bytes(shared), pools, wide=True)


@pytest.mark.parametrize("header", ("", "abc", "-1", "255", "999999", "１２３", "9" * 5000))
def test_malformed_two_file_upload_never_writes_data(client, settings, header):
    response = client.post("/api/camdata/import-upload", content=bytes(300), headers={"X-GXR-Dat-Bytes": header.encode("utf-8")})
    assert response.status_code == 422 and response.json()["error"] == "dat_invalid"
    assert not (settings.home_dir / "camera-data").exists()


def test_r5_file_picker_reads_the_companion_with_a_bounded_length(tmp_path):
    raw = bytearray(256)
    raw[:8] = b"GXRCE001"
    struct.pack_into(">I", raw, 28, 178)
    path = tmp_path / "GXRCE.DAT"
    path.write_bytes(raw)
    (tmp_path / "gxrfx.bin").write_bytes(b"synthetic-library")
    assert intake.read_bundle_from_path(path) == (bytes(raw), "GXRCE.DAT", b"synthetic-library")
    (tmp_path / "gxrfx.bin").write_bytes(bytes(r5.MAX_LIBRARY_BYTES + 1))
    with pytest.raises(intake.IntakeError, match="过大"):
        intake.read_bundle_from_path(path)
