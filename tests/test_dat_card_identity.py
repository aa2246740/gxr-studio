"""DAT content reads keep the identity of the selected mount session."""

from __future__ import annotations

import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from conftest import PORT
from gxr_studio.app import create_app
from gxr_studio.camdata import intake
from gxr_studio.cards.discovery import CardReader

_CHANGE_ROOT = """
from pathlib import Path
import sys

root, base = Path(sys.argv[1]), Path(sys.argv[2])
mode, data = sys.argv[3], sys.argv[4].encode()
if mode == "replace_root":
    root.rename(base / "original-root")
    root.mkdir()
    (root / "GXRCE.DAT").write_bytes(data)
elif mode == "root_link":
    root.rename(base / "original-root")
    other = base / "other-root"
    other.mkdir()
    (other / "GXRCE.DAT").write_bytes(data)
    root.symlink_to(other, target_is_directory=True)
else:
    parent = root.parent
    parent.rename(base / "original-volumes")
    other = base / "other-parent"
    (other / root.name).mkdir(parents=True)
    (other / root.name / "GXRCE.DAT").write_bytes(data)
    parent.symlink_to(other, target_is_directory=True)
"""


@pytest.mark.parametrize("explicit", [False, True], ids=["automatic", "card_id"])
@pytest.mark.parametrize("mode", ["unchanged", "replace_root", "ancestor_link", "root_link"])
def test_dat_import_keeps_selected_root_identity(settings, volumes, tmp_path, monkeypatch, explicit, mode):
    root = volumes / "SYNTHETIC"
    root.mkdir()
    own, other = b"OWN-SYNTHETIC-DAT", b"OTHER-SYNTHETIC-DATA"
    (root / "GXRCE.DAT").write_bytes(own)
    initial = root.stat()
    changed, reads = [], []

    class ChangingReader(CardReader):
        def open_rb(self, path, **kwargs):
            if mode != "unchanged":
                # A separate process models a directory change outside the app's read-only card guard.
                subprocess.run(
                    [sys.executable, "-I", "-B", "-c", _CHANGE_ROOT, str(root), str(tmp_path), mode, other.decode()],
                    check=True,
                    timeout=10,
                )
                current = root.stat()
                assert (current.st_dev, current.st_ino) != (initial.st_dev, initial.st_ino)
                changed.append(True)
            return super().open_rb(path, **kwargs)

    real_read = intake._read_limited

    def record_read(fh):
        data = real_read(fh)
        reads.append(data)
        return data

    monkeypatch.setattr(intake, "_read_limited", record_read)
    app = create_app(settings, reader=ChangingReader())
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as client:
        cards = client.get("/api/camdata/status").json()["cards"]
        assert len(cards) == 1 and cards[0]["has_dat"]
        response = client.post("/api/camdata/import-card", json={"card_id": cards[0]["id"]} if explicit else {})
    if mode == "unchanged":
        assert response.status_code == 422 and response.json()["error"] == "dat_invalid"
        assert reads == [own]
    else:
        assert changed == [True]
        assert (response.status_code, reads) == (409, [])
        assert response.json()["error"] == "card_read_failed"
    assert not (settings.home_dir / "camera-data").exists()
