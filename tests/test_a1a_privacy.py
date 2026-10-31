"""A1a：理光字节不进公开仓库（设计 §5.6；§13.6 PR01–PR04，§13.11 test_no_ricoh_bytes_in_repo）。

"仓库"在这里是临时目录里放一个 `.git` 目录或文件模拟的（不跑任何 git 写操作）；
所有"理光字节"都是 tests/support/fake_card.py 现场生成的带魔数的随机字节。
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import os
import shutil
import tarfile
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import PORT
from gxr_studio import audit, privacy
from gxr_studio.app import create_app
from gxr_studio.camdata import store
from gxr_studio.cards.discovery import find_cards
from support.fake_card import fake_dat, fake_gxrfx, make_fake_card
from support.fake_git import FakeGit

STUDIO = Path(__file__).resolve().parents[1]
SCRIPT = STUDIO / "scripts" / "private_bytes.py"


def load_checker():
    spec = importlib.util.spec_from_file_location("private_bytes", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fake_repo(base: Path, as_file: bool = False) -> Path:
    repo = base / "repo"
    repo.mkdir(parents=True)
    if as_file:
        (repo / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n")  # git worktree 的 .git 是文件
    else:
        (repo / ".git").mkdir()
    return repo


def client_for(settings):
    app = create_app(settings)
    return TestClient(app, base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token})


def everything_under(path: Path) -> list[str]:
    return sorted(p.relative_to(path).as_posix() for p in path.rglob("*")) if path.exists() else []


# ---- PR01 -------------------------------------------------------------------


@pytest.mark.parametrize("how", ["inside", "symlink", "worktree-file", "studio-repo"])
def test_private_paths_cannot_resolve_inside_repository(tmp_path, settings, how):
    """PR01：应用数据目录直接在仓库里、经链接进入仓库、在 .git 是文件的工作区里，或就是 studio 仓库：
    写任何私有字节之前拒绝，不留半个输出。"""
    if how == "studio-repo":
        home = STUDIO / "local-home-should-never-exist"
    else:
        repo = fake_repo(tmp_path / "r", as_file=how == "worktree-file")
        home = repo / "app-home"
        if how == "symlink":
            link = tmp_path / "home-link"
            link.symlink_to(repo, target_is_directory=True)
            home = link / "app-home"
    with pytest.raises(privacy.PrivatePathError) as e:
        privacy.check_private_dir(home, "camera_data")
    assert e.value.code == "private_path_in_repository" and e.value.message == privacy.MESSAGE
    # 产品入口：上传相机配方文件 → 422，应用数据目录里一个字节都没有（studio 仓库本身不起应用，免得碰它）
    if how != "studio-repo":
        _upload_rejected(settings.with_(home_dir=home), "private_path_in_repository")
    with pytest.raises(privacy.PrivatePathError):
        store.import_from_dat(fake_dat(2), home)
    with pytest.raises(privacy.PrivatePathError):
        privacy.private_write(home, "blob", fake_dat(3), "blob")
    assert not home.exists(), "拒绝时不能留下任何目录或文件"


def _upload_rejected(settings, code):
    with client_for(settings) as client:
        r = client.post("/api/camdata/import-upload", content=fake_dat(1), headers={"X-GXR-Filename": "GXRCE.DAT"})
        assert r.status_code == 422 and r.json()["error"] == code
        assert r.json()["message"] == "无法导出到这个文件夹。请选择其他文件夹。" and r.json()["detail"]


def test_path_rechecked_on_every_operation(tmp_path):
    """PR01：检查通过以后目录变成在仓库里（比如上级被做成了仓库）：下一次操作、整体替换之前都重新检查，不留半个输出。"""
    home = tmp_path / "app-data"
    out = privacy.private_write(home, "a.bin", fake_dat(1), "blob")
    assert out.read_bytes() == fake_dat(1)
    (tmp_path / ".git").mkdir()  # 上级突然成了一个 Git 工作区
    with pytest.raises(privacy.PrivatePathError):
        privacy.private_write(home, "b.bin", fake_dat(2), "blob")
    assert sorted(p.name for p in home.iterdir()) == ["a.bin"], "第二次被拒绝：没有 b.bin，也没有临时文件"
    new = home / "camera-data.new"
    new.mkdir()
    (new / "x").write_bytes(b"x")
    with pytest.raises(privacy.PrivatePathError):
        store.swap_in(new, home / "camera-data")
    assert not new.exists() and not (home / "camera-data").exists()


# ---- PR02 -------------------------------------------------------------------


@pytest.mark.parametrize("target", ["card_root", "dcim", "library", "volumes_root"])
def test_private_paths_cannot_alias_card_or_dcim(tmp_path, settings, volumes, target):
    """PR02：应用数据目录指向卡、DCIM、图库或卷根（直接或经链接）：拒绝，卡和照片一字不动。"""
    card = make_fake_card(volumes)
    before = everything_under(card.root)
    dest = {
        "card_root": card.root,
        "dcim": card.root / "DCIM" / "100RICOH",
        "library": settings.library_dir,
        "volumes_root": volumes,
    }[target]
    dest.mkdir(parents=True, exist_ok=True)
    alias = tmp_path / "alias"
    alias.symlink_to(dest, target_is_directory=True)
    forbidden = privacy.forbidden_for(settings, [card.root])
    for path in (dest, alias, alias / "sub"):
        with pytest.raises(privacy.PrivatePathError) as e:
            privacy.private_write(path, "blob", fake_dat(1), "blob", forbidden)
        assert e.value.code == "private_path_forbidden"
    _upload_rejected(settings.with_(home_dir=alias), "private_path_forbidden")
    assert everything_under(card.root) == before


def test_alias_swapped_after_check_cannot_write_into_card(tmp_path, volumes, monkeypatch):
    """PR02：检查时是普通目录，检查之后被换成指向 DCIM 的链接：按不跟随链接的目录 fd 写，写不进去，卡不变。"""
    card = make_fake_card(volumes)
    audit.unprotect(card.root)  # 这里要证明的是写入路径本身挡得住，不靠审计钩子
    before = everything_under(card.root)
    home = tmp_path / "app-data"
    home.mkdir()
    real_check = privacy.check_private_dir
    calls = []

    def check_then_swap(path, purpose, forbidden=()):
        out = real_check(path, purpose, forbidden)
        if not calls:
            shutil.rmtree(home)
            home.symlink_to(card.root / "DCIM", target_is_directory=True)
        calls.append(path)
        return out

    monkeypatch.setattr(privacy, "check_private_dir", check_then_swap)
    with pytest.raises(privacy.PrivatePathError) as e:
        privacy.private_write(home, "R0010001.JPG", fake_dat(1), "blob")
    assert e.value.code == "private_path_changed"
    assert everything_under(card.root) == before
    assert card.path("DCIM/100RICOH/R0010001.JPG").read_bytes() == card.files["DCIM/100RICOH/R0010001.JPG"]


def test_startup_check_reports_but_does_not_block(tmp_path, settings):
    repo = fake_repo(tmp_path / "r")
    res = privacy.startup_check(settings.with_(home_dir=repo / "home", export_dir=tmp_path / "ok"))
    assert res == {"home_dir": "private_path_in_repository", "export_dir": None}
    assert privacy.startup_check(settings) == {"home_dir": None, "export_dir": None}


# ---- PR03 -------------------------------------------------------------------


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def _targz(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def restricted_products(dest: Path) -> dict[str, bytes]:
    """自己生成的受限产物：无后缀 blob、GXRFX.BIN、大小写变体、改名 DAT、改名 ZIP、嵌套 gxrkit、编码串。"""
    dat = fake_dat(7)
    files = {
        "camera/blobs/" + "ab" * 32: dat,
        "GXRFX.BIN": fake_gxrfx(1),
        "gxrce.dat": dat,
        "Gxrce.Dat.bak": dat,
        "notes.txt": dat,  # 改名的 DAT（文本后缀，开头是魔数）
        "holiday.jpg": _zip({"GXRCE.DAT": dat}),  # 改名的 ZIP
        "backup.gxrkit": _zip({"manifest.json": b"{}", "inner.zip": _zip({"blobs/x": dat})}),  # zip 套 zip
        "fixture_hex.py": b"BLOB = '" + dat[:2048].hex().encode() + b"'\n",
        "fixture_b64.json": b'{"b": "' + base64.b64encode(dat[:2048]) + b'"}',
        "dump.bin": b"\0" * 64 + b"GXRRV201\0\2\0 " + bytes(100),
        "pool.bin": b"\0" * 10 + b"GXDP\x00\x01\x00\x40" + bytes(100),
    }
    for rel, data in files.items():
        p = dest / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return files


def test_repository_and_distribution_guard_covers_renames(tmp_path, monkeypatch):
    """PR03：仓库（提交前暂存区）和分发包检查按内容拦住改名、去后缀、归档套归档、编码串；只看文件名不算数。"""
    pb = load_checker()
    files = restricted_products(tmp_path / "work")
    paths = [tmp_path / "work" / r for r in files]
    hits = dict(pb.scan_paths(paths))
    for rel in files:
        assert any(n.endswith(rel) or n.split("!")[0].endswith(rel) for n in hits), f"{rel} 没有被拦住"
    # 提交前检查：读暂存区的 blob（在子进程层模拟 git 的输出；不碰真实索引）
    FakeGit({f"staged/{rel}": data for rel, data in files.items()}).install(monkeypatch, pb)
    staged_hits = {n.split("!")[0] for n, _ in pb.scan_staged()}
    assert staged_hits == {f"staged:staged/{rel}" for rel in files}
    assert pb.main(["--staged"]) == 1
    # 分发检查：wheel（zip）和 sdist（tar.gz）里的成员，含 zip 套 zip
    wheel = tmp_path / "dist" / "pkg-0.1-py3-none-any.whl"
    wheel.parent.mkdir()
    wheel.write_bytes(_zip({"pkg/__init__.py": b"", "pkg/data/blob": fake_dat(8), "pkg/k.gxrkit": _zip({"x": fake_dat(9)})}))
    sdist = tmp_path / "dist" / "pkg-0.1.tar.gz"
    sdist.write_bytes(_targz({"pkg-0.1/fixtures/renamed.txt": fake_dat(10), "pkg-0.1/ok.py": b"x = 1\n"}))
    monkeypatch.setattr(pb, "dist_files", lambda out: [wheel, sdist])
    assert pb.main(["--dist"]) == 1
    names = [n for n, _ in pb.scan_paths([wheel, sdist])]
    assert any(n.endswith("!pkg/data/blob") for n in names) and any(n.endswith("!x") for n in names)
    assert any(n.endswith("!pkg-0.1/fixtures/renamed.txt") for n in names)
    # 登记过的 SHA：内容本身没有魔数，也能按哈希拦住
    plain = tmp_path / "plain.bin"
    plain.write_bytes(b"\x01\x02" * 100)
    assert pb.scan_paths([plain]) == []
    assert pb.scan_paths([plain], frozenset({hashlib.sha256(plain.read_bytes()).hexdigest()})) == [(str(plain), "R4 registered-sha")]


def test_checker_allows_source_literals_and_does_not_echo_content(tmp_path, capsys):
    """源码里的短字面量（datcodec.py 本来就有 b"GXRCE001"）不算；命中时只打印路径和规则编号。"""
    pb = load_checker()
    src = tmp_path / "codec.py"
    src.write_text('MAGIC = b"GXRCE001"\nOTHER = b"GXRRV201"\nUI = b"GXUI124\\0"\n')
    assert pb.scan_paths([src]) == []
    bad = tmp_path / "x.bin"
    bad.write_bytes(fake_dat(1))
    assert pb.main([str(bad)]) == 1
    out = capsys.readouterr()
    assert "R1 dat-head" in out.out and "GXRCE001" not in out.out and "GXRCE001" not in out.err


def test_private_byte_ignore_rules_present():
    """忽略规则只是辅助，但也要齐（设计 §5.6 第二道）。"""
    text = (STUDIO / ".gitignore").read_text(encoding="utf-8")
    for rule in ("camera/", "*.gxrkit", "GXRFX.BIN", "GXRFX.TMP", "GXRCE.*", "gxrce*.dat", "*.DAT"):
        assert rule in text.splitlines(), rule
    hook = (STUDIO / ".githooks" / "pre-commit").read_text(encoding="utf-8")
    assert "scripts/private_bytes.py --staged" in hook
    assert "scripts/private_bytes.py --tracked --dist" in (STUDIO / "Makefile").read_text(encoding="utf-8")


# ---- PR04 / test_no_ricoh_bytes_in_repo ------------------------------------


def test_clean_ci_uses_generated_fixtures_only(tmp_path, volumes):
    """PR04：A1a 的卡、DAT、效果文件都是现场生成在 tmp_path 里的；仓库（含测试和夹具）里没有理光字节。"""
    pb = load_checker()
    card = make_fake_card(volumes, videos=1, root_extra={"GXRCE.DAT": fake_dat(1), "GXRFX.BIN": fake_gxrfx(1)})
    assert all(card.path(r).resolve().is_relative_to(tmp_path.resolve()) for r in card.files)
    assert find_cards([volumes])[0].root == card.root
    hits = pb.scan_paths(pb.repository_files())
    assert hits == [], hits
    support = STUDIO / "tests" / "support" / "fake_card.py"
    assert support in pb.repository_files()


def test_no_ricoh_bytes_in_repo():
    """§13.11：由 PR03 扩展：按内容检查整个工作区（已跟踪和未忽略的文件），没有一处命中。"""
    pb = load_checker()
    files = pb.repository_files()
    assert len(files) > 100
    assert pb.scan_paths(files) == []
    assert not [p for p in files if p.suffix.lower() in (".dat", ".gxrkit") or p.name.upper() in ("GXRFX.BIN", "GXRCE.DAT")]
    assert os.path.basename(str(SCRIPT)) == "private_bytes.py"
