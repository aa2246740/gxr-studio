"""用独立合成文件证明新旧副本共存时只读指定根目录，不自动挑代数。"""

from __future__ import annotations

import struct

import pytest

from gxr_studio.camdata import intake
from test_camdata_api import client as client
from test_camdata_api import reader as reader


def shell(generation):
    raw = bytearray(256)
    raw[:8] = b"GXRCE001"
    struct.pack_into(">I", raw, 28, generation)
    return bytes(raw)


@pytest.mark.parametrize("generation", (156, 176, 178, 181, 182))
@pytest.mark.parametrize("source", ("directory", "card"))
def test_named_root_wins_over_renamed_and_nested_backups(volumes, reader, generation, source):
    root = volumes / "copy"
    root.mkdir()
    selected = shell(generation)
    backup = shell(178 if generation == 156 else 156)
    (root / "GXRCE.DAT").write_bytes(selected)
    (root / "GXRFX.BIN").write_bytes(b"synthetic-companion")
    (root / "GXRCE-backup.DAT").write_bytes(backup)
    folder = root / "backup"
    folder.mkdir()
    (folder / "GXRCE.DAT").write_bytes(backup)
    if source == "directory":
        raw, name, library = intake.read_bundle_from_path(root)
        assert name == "GXRCE.DAT"
    else:
        scans = intake.scan_cards_for_dat([volumes], reader)
        assert len(scans) == 1
        stat = root.stat()
        raw, library = intake.read_bundle_from_card(reader, scans[0], expect=(stat.st_dev, stat.st_ino))
        assert reader.opened == [str(root / "GXRCE.DAT")] + ([str(root / "GXRFX.BIN")] if generation >= 176 else [])
    assert raw == selected
    assert library == (b"synthetic-companion" if generation >= 176 else None)
    assert (root / "GXRCE-backup.DAT").read_bytes() == backup and (folder / "GXRCE.DAT").read_bytes() == backup


def test_multiple_cards_need_selection_without_reading_either_payload(volumes, reader, client):
    for label, generation in (("old", 156), ("new", 178)):
        root = volumes / label
        root.mkdir()
        (root / "GXRCE.DAT").write_bytes(shell(generation))
    response = client.post("/api/camdata/import-card", json={})
    assert response.status_code == 409 and response.json()["error"] == "multiple_cards"
    assert {card["label"] for card in response.json()["cards"]} == {"old", "new"}
    assert reader.opened == []


def test_explicit_upload_of_a_legacy_dat_with_a_library_explains_the_rejection(client, settings):
    raw = shell(156)
    response = client.post("/api/camdata/import-upload", content=raw + b"synthetic-companion", headers={"X-GXR-Dat-Bytes": str(len(raw))})
    assert response.status_code == 422 and response.json()["error"] == "dat_invalid"
    assert "只选择 GXRCE.DAT" in response.json()["message"]
    assert not (settings.home_dir / "camera-data").exists()
