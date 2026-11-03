"""A1a 代码复审第 3 轮（research/review_studio_a1a_r3/REVIEW.md）的回归用例，按类写：

- R3-P1-01 启动时第一次写图库之前就核对图库的真实位置：图库根、`.studio` 和其中每个会写的位置
  （索引及其日志文件、锁、导入日志、缩略图、暂存区、临时目录）经链接指到卡上或图库外面，一律拒绝，启动明确报错；
- R3-P1-02 私有字节检查：多段压缩每一段都完整检查；归档后面接的内容（tar 结束之后、zip 成员之间或前后）不能漏查；
- R3-P2-01 私有目录写入：字节写下去之前、之后都核对实际的父目录 fd；目录换过位置就把写进去的字节清掉，并在错误里写清楚留下了什么。

全部是现场生成的合成卡和合成"理光字节"；不碰真实卡、用户图库或应用数据，不做 git 写操作。
"""

from __future__ import annotations

import bz2
import gzip
import importlib.util
import io
import lzma
import os
import tarfile
import zipfile
from pathlib import Path

import pytest

from gxr_studio import privacy
from gxr_studio.app import create_app
from gxr_studio.devtools.synthetic_card import tree_snapshot
from gxr_studio.library.store import Library
from support.fake_card import make_fake_card

STUDIO = Path(__file__).resolve().parents[1]
RESTRICTED = b"GXRCE001" + bytes(range(256)) * 4  # 合成的"相机私有文件"：只有规则认得的开头

STUDIO_PATHS = [
    ".studio",
    ".studio/index.sqlite",
    ".studio/index.sqlite-journal",
    ".studio/studio.lock",
    ".studio/import-log.jsonl",
    ".studio/thumbs",
    ".studio/staging",
    ".studio/tmp",
]


def _link_into(card_root: Path, library: Path, rel: str) -> None:
    """把图库里 `rel` 这个位置做成指到卡上 `reserve/` 的链接（目录就链目录，文件就链一个卡上的文件位置）。"""
    reserve = card_root / "reserve"
    reserve.mkdir(exist_ok=True)
    link = library / rel
    link.parent.mkdir(parents=True, exist_ok=True)
    if rel in (".studio", ".studio/thumbs", ".studio/staging", ".studio/tmp"):
        link.symlink_to(reserve, target_is_directory=True)
    else:
        link.symlink_to(reserve / Path(rel).name)


def _location_error():
    from gxr_studio.library.location import LibraryLocationRefused

    return LibraryLocationRefused


# ---- R3-P1-01：启动时就核对图库位置 -------------------------------------------------------------------


@pytest.mark.parametrize("rel", [*STUDIO_PATHS, "library_root"])
def test_r3_startup_refuses_library_paths_linked_into_card(tmp_path, volumes, rel):
    """应用启动（第一次卡扫描之前）：这些位置被链接到插着的卡上，卡上零写入（含目录 mtime），启动明确报错。"""
    card = make_fake_card(volumes)
    library = tmp_path / "library"
    if rel == "library_root":
        (card.root / "reserve").mkdir()
        library.symlink_to(card.root / "reserve", target_is_directory=True)
    else:
        library.mkdir()
        _link_into(card.root, library, rel)
    before = tree_snapshot(card.root)
    from gxr_studio.config import Settings

    settings = Settings(
        library_dir=library, home_dir=tmp_path / "app-data", export_dir=tmp_path / "exports", volumes_roots=(volumes,), token="t", port=18765
    )
    with pytest.raises(_location_error()) as e:
        create_app(settings)
    assert tree_snapshot(card.root) == before
    assert "无法打开图库" in e.value.message and e.value.detail


@pytest.mark.parametrize("rel", [*STUDIO_PATHS, "library_root"])
def test_r3_lock_refuses_library_paths_linked_into_card(tmp_path, volumes, rel):
    """CLI 在创建应用之前先拿锁：同样在写任何东西之前核对。拒绝之后 release() 照常可以调用。"""
    from gxr_studio.library.location import card_roots
    from gxr_studio.library.lock import LibraryLock

    card = make_fake_card(volumes)
    library = tmp_path / "library"
    if rel == "library_root":
        (card.root / "reserve").mkdir()
        library.symlink_to(card.root / "reserve", target_is_directory=True)
    else:
        library.mkdir()
        _link_into(card.root, library, rel)
    before = tree_snapshot(card.root)
    lock = LibraryLock(library, card_roots([volumes]))
    try:
        with pytest.raises(_location_error()):
            lock.acquire()
    finally:
        lock.release()
    assert tree_snapshot(card.root) == before


def test_r3_library_paths_linked_outside_library_are_refused(tmp_path):
    """不在卡上、但经链接指到图库外面（例如另一个目录）：同样拒绝，外面那个位置不被写。"""
    library = tmp_path / "library"
    library.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (library / ".studio").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(_location_error()):
        Library(library).open()
    assert list(elsewhere.iterdir()) == []


def test_r3_library_on_a_volume_that_is_not_a_card_is_allowed(tmp_path, volumes):
    """对照：图库放在卷根下的普通外接盘上（没有 DCIM，不是卡）可以正常打开。"""
    disk = volumes / "Workspace"
    disk.mkdir()
    from gxr_studio.config import Settings

    settings = Settings(
        library_dir=disk / "GXR Studio",
        home_dir=tmp_path / "app-data",
        export_dir=tmp_path / "exports",
        volumes_roots=(volumes,),
        token="t",
        port=18765,
    )
    app = create_app(settings)
    app.state.ctx.lut.close()
    app.state.ctx.recorder.__exit__(None, None, None)
    app.state.ctx.lib.close()
    assert (disk / "GXR Studio" / ".studio" / "index.sqlite").is_file()


def test_r3_cli_explains_and_exits(tmp_path, volumes, monkeypatch, capsys):
    """命令行启动：图库在卡上时说清楚原因并退出（退出码 78），不静默、不绑端口、不写卡。"""
    from gxr_studio import cli

    card = make_fake_card(volumes)
    (card.root / "reserve").mkdir()
    library = tmp_path / "library"
    library.symlink_to(card.root / "reserve", target_is_directory=True)
    before = tree_snapshot(card.root)
    for k, v in {
        "GXR_STUDIO_LIBRARY": str(library),
        "GXR_STUDIO_HOME": str(tmp_path / "app-data"),
        "GXR_STUDIO_EXPORT": str(tmp_path / "exports"),
        "GXR_STUDIO_VOLUMES": str(volumes),
    }.items():
        monkeypatch.setenv(k, v)

    def no_listening(*a, **k):
        raise AssertionError("图库位置不合格时不应走到绑定端口")

    monkeypatch.setattr(cli.socket, "socket", no_listening)  # 没拒绝的话在这里立刻失败，不会真的开服务
    with pytest.raises(SystemExit) as e:
        cli.main(["--no-browser"])
    assert e.value.code == 78
    out = capsys.readouterr().out
    assert "无法打开图库" in out and "请把图库放在电脑上的普通文件夹里" in out
    assert tree_snapshot(card.root) == before


# ---- R3-P1-02：多段压缩、归档后面接的内容 ------------------------------------------------------------------


def _checker():
    spec = importlib.util.spec_from_file_location("private_bytes_r3", STUDIO / "scripts" / "private_bytes.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _archive(kind: str, data: bytes = b"ordinary notes\n") -> bytes:
    buf = io.BytesIO()
    if kind == "zip":
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
            z.writestr("notes.txt", data)
    else:
        with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as t:
            info = tarfile.TarInfo("notes.txt")
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _compress(kind: str, data: bytes) -> bytes:
    if kind == "gzip":
        return gzip.compress(data, mtime=0)
    if kind == "bzip2":
        return bz2.compress(data)
    if kind == "xz":
        return lzma.compress(data)
    from compression import zstd

    return zstd.compress(data)


def _zip_with(prefix: bytes = b"", gap: bytes = b"", suffix: bytes = b"") -> bytes:
    """合法 zip，在第一个成员前、两个成员之间或结束记录之后夹进额外字节（zipfile 照常能读，中央目录不引用这些字节）。"""
    out = io.BytesIO()
    out.write(prefix)
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("one.txt", b"one\n")
        if gap:
            z.fp.write(gap)  # 成员数据之后、下一个本地头之前
            z.start_dir = z.fp.tell()  # 下一个成员从夹带内容之后开始写（偏移照样记对），不覆盖它
        z.writestr("two.txt", b"two\n")
    data = out.getvalue() + suffix
    with zipfile.ZipFile(io.BytesIO(data)) as check:  # 确认这是 zipfile 能正常读的归档
        assert [check.read(n) for n in ("one.txt", "two.txt")] == [b"one\n", b"two\n"]
    for extra in (prefix, gap, suffix):  # 夹带的内容确实在文件里
        assert extra in data
    return data


def _cases() -> dict[str, bytes]:
    out = {}
    for comp in ("gzip", "bzip2", "xz", "zstd"):
        for inner in ("tar", "zip"):
            out[f"{inner}_then_stream_{comp}"] = _compress(comp, _archive(inner)) + _compress(comp, RESTRICTED)
    out["tar_with_trailing_bytes_in_one_gzip"] = gzip.compress(_archive("tar") + RESTRICTED, mtime=0)
    out["tar_with_trailing_bytes"] = _archive("tar") + RESTRICTED
    out["zip_with_prefix"] = _zip_with(prefix=RESTRICTED)
    out["zip_with_gap"] = _zip_with(gap=RESTRICTED)
    out["zip_with_suffix"] = _zip_with(suffix=RESTRICTED)
    out["stream_then_tar_then_stream"] = (
        gzip.compress(b"notes\n", mtime=0) + gzip.compress(_archive("tar"), mtime=0) + gzip.compress(RESTRICTED, mtime=0)
    )
    # 前面带着别的字节的 zip（自解压脚本那样）：受限文件压缩后放在成员里，原始字节里看不到魔数
    deflated = io.BytesIO()
    with zipfile.ZipFile(deflated, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("camera.dat", RESTRICTED)
    out["zip_after_stub_with_member"] = b"#!/bin/sh\nexit 0\n" * 8 + deflated.getvalue()
    return out


@pytest.mark.parametrize("name", list(_cases()))
def test_r3_every_segment_and_trailing_byte_is_checked(tmp_path, name):
    pb = _checker()
    data = _cases()[name]
    p = tmp_path / name
    p.write_bytes(data)
    hits = pb.scan_paths([p])
    assert hits, name
    assert pb.main([str(p)]) == 1


@pytest.mark.parametrize(
    "name, data",
    [
        ("tar.gz", gzip.compress(_archive("tar"), mtime=0)),
        ("tar_then_text.gz", gzip.compress(_archive("tar"), mtime=0) + gzip.compress(b"more notes\n", mtime=0)),
        ("zip", _archive("zip")),
        ("tar", _archive("tar")),
        ("zip_two_members", _zip_with()),
    ],
)
def test_r3_clean_archives_still_pass(tmp_path, name, data):
    """对照：干净的归档、归档后接干净段，不报任何命中（也不报 R0）。"""
    pb = _checker()
    assert pb.scan_bytes(name, data) == []


# ---- R3-P2-01：写字节前后都核对父目录 fd ---------------------------------------------------------------


def _move_on_write_call(monkeypatch, src: Path, dest: Path, which: int = 1, short: int | None = None):
    """第 `which` 次 os.write 调用时把 `src` 目录移到 `dest`（真实的改名，在真正写字节之前）。`short` 让第一次只写这么多字节。"""
    actual = os.write
    calls = []

    def writing(fd, content):
        calls.append(len(content))
        if len(calls) == which:
            src.rename(dest)
        if short is not None and len(calls) == 1:
            return actual(fd, bytes(content)[:short])
        return actual(fd, content)

    monkeypatch.setattr(os, "write", writing)
    return calls


@pytest.mark.parametrize("case", ["first_write", "after_short_write", "replace_file"])
def test_r3_write_leaves_no_private_bytes_after_directory_moves(tmp_path, monkeypatch, case):
    root = tmp_path / "app-data"
    target = tmp_path / "repository"
    target.mkdir()
    (target / ".git").write_text("marker\n", encoding="utf-8")
    data = b"private sample\n" * 40
    with privacy.PrivateDir(root, "test") as d:
        (root / "sub").mkdir()
        if case == "replace_file":
            d.write("sub/kept.bin", b"old")
        _move_on_write_call(
            monkeypatch,
            root / "sub",
            target / "moved",
            which=2 if case == "after_short_write" else 1,
            short=10 if case == "after_short_write" else None,
        )
        with pytest.raises(privacy.PrivatePathError) as e:
            if case == "replace_file":
                d.replace_file("sub/kept.bin", data)
            else:
                d.write("sub/sample.bin", data)
        monkeypatch.undo()
    leftovers = [p for p in (target / "moved").iterdir() if p.name != "kept.bin"]
    assert leftovers and all(p.read_bytes() == b"" for p in leftovers), [(p.name, p.stat().st_size) for p in leftovers]
    assert "0 字节" in e.value.detail and str(leftovers[0].name) in e.value.detail
