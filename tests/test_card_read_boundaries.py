"""Card content reads stay relative to the checked root handle."""

from __future__ import annotations

import hashlib
import threading

import pytest

from gxr_studio import audit
from gxr_studio.cards.coverage import hash_card_file
from gxr_studio.cards.discovery import Card, CardFile, CardReader, scan_card
from gxr_studio.cards.importer import CardReadError, ImportOptions, stage_file


def _read(operation, reader, card, cf, lib, expect=None):
    if operation == "stage":
        return stage_file(reader, card, cf, lib, ImportOptions(min_free_bytes=0), threading.Event(), expect=expect)
    return hash_card_file(reader, cf.path, cf.size, root=card.root, expect=expect)


@pytest.mark.parametrize("operation", ["stage", "hash"])
@pytest.mark.parametrize("when", ["already_linked", "after_scan"])
def test_content_reads_refuse_a_linked_parent(tmp_path, lib, operation, when):
    root = tmp_path / "synthetic-card"
    folder = root / "DCIM" / "100RICOH"
    folder.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    own, other = b"OWN-SYNTHETIC-PHOTO", b"OTHER-SYNTHETIC-PIC"
    path = folder / "R0000001.JPG"
    path.write_bytes(own)
    (outside / path.name).write_bytes(other)
    card = Card(root, "synthetic")
    cf = scan_card(card)[0].files["jpg"]
    changed = []

    def change():
        folder.rename(folder.with_name("100RICOH.saved"))
        folder.symlink_to(outside, target_is_directory=True)
        changed.append(True)

    class ChangingReader(CardReader):
        def open_rb(self, path, **kwargs):
            if not changed:
                change()
            return super().open_rb(path, **kwargs)

    if when == "already_linked":
        change()
    with audit.AuditRecorder(record_reads=True) as recorder:
        with pytest.raises((OSError, CardReadError)):
            _read(operation, ChangingReader(), card, cf, lib)
    assert changed == [True]
    assert recorder.reads_under(outside) == []
    assert list(lib.staging_dir.iterdir()) == []
    assert (outside / path.name).read_bytes() == other


@pytest.mark.parametrize("operation", ["stage", "hash"])
def test_content_reads_refuse_a_different_card_root(tmp_path, lib, operation):
    root = tmp_path / "synthetic-card"
    path = root / "DCIM/100RICOH/R0000001.JPG"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"original")
    card = Card(root, "synthetic")
    cf = scan_card(card)[0].files["jpg"]
    st = root.stat()
    root.rename(root.with_name("original-card"))
    path.parent.mkdir(parents=True)
    path.write_bytes(b"replaced")
    with pytest.raises((OSError, CardReadError)):
        _read(operation, CardReader(), card, cf, lib, (st.st_dev, st.st_ino))
    assert list(lib.staging_dir.iterdir()) == []


def test_rooted_binary_reads_preserve_every_byte(tmp_path, lib):
    root = tmp_path / "synthetic-card"
    path = root / "DCIM/100RICOH/R0000001.JPG"
    path.parent.mkdir(parents=True)
    content = bytes(range(256)) * 16 + b"\r\n\x1a\x00"
    path.write_bytes(content)
    st = path.stat()
    cf = CardFile("DCIM/100RICOH/R0000001.JPG", path, len(content), st.st_mtime_ns, "jpg")
    staged = _read("stage", CardReader(), Card(root, "synthetic"), cf, lib)
    try:
        assert staged.path.read_bytes() == content
        assert _read("hash", CardReader(), Card(root, "synthetic"), cf, lib)[0] == hashlib.sha256(content).hexdigest()
    finally:
        staged.path.unlink()


@pytest.mark.parametrize("relative", ["../outside.JPG", "DCIM/../../outside.JPG"])
def test_rooted_reads_refuse_parent_components(tmp_path, relative):
    root = tmp_path / "synthetic-card"
    root.mkdir()
    with pytest.raises(OSError):
        CardReader().open_rb(root / relative, root=root)
