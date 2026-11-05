"""复审 review-studio-m2-r1 之后的修复：P0-1 导入不完整、P1-1 编辑记录坏了、P2 若干。

每一条都对应复审报告里的一项（research/review_studio_m2/REVIEW.md），修复说明见 research/studio_fix_review/REPORT.md。
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import PORT
from gxr_studio import audit, cli, osplat, privacy
from gxr_studio.app import create_app
from gxr_studio.camdata import store
from gxr_studio.cards.discovery import CardReader, find_cards
from gxr_studio.cards.importer import ImportOptions
from gxr_studio.devtools import synthetic_card as sy
from gxr_studio.library.lock import BUSY_EXIT_CODE, LibraryBusy, LibraryLock
from gxr_studio.library.store import SIDECAR_RESET_NOTE
from test_api import find, import_all

WEB = Path(__file__).resolve().parents[1] / "web"


def make_client(settings, **kw):
    app = create_app(settings, **kw)
    return TestClient(app, base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token})


@pytest.fixture
def client(settings, card):
    with make_client(settings) as c:
        yield c


def sidecar_of(client, name):
    lib = client.app.state.ctx.lib
    shot = lib.get_shot(find(client, name)["id"])
    return lib.sidecar_path_for(shot), shot


# ---- P0-1：导入不完整时不能说"可以安全弹出" ---------------------------------------------


def test_unreadable_jpg_is_listed_and_the_good_dng_is_imported(settings, card):
    class Bad(CardReader):
        def open_rb(self, path, **kwargs):
            if Path(path).name == "R0000104.JPG":
                raise OSError(errno.EIO, "I/O error")
            return super().open_rb(path, **kwargs)

    with make_client(settings, reader=Bad()) as c:
        p = import_all(c)["progress"]
        assert p["state"] == "done" and p["unreadable"] == ["DCIM/100RICOH/R0000104.JPG"]
        assert p["failed_shots"] == 1
        assert "安全弹出" not in p["message"] and "请勿格式化" in p["message"]
        shot = find(c, "R0000104")
        assert shot["has_raw"] and not shot["has_jpg"]  # 好的那个照常导入
        assert len(c.get("/api/photos").json()) == len(card.unique_shots)


def test_verify_mismatch_is_a_failure_not_a_green_banner(settings, card):
    def corrupt(part: Path, cf):
        if cf.relpath.endswith("R0000102.JPG"):
            with open(part, "r+b") as fh:
                fh.seek(100)
                b = fh.read(1)
                fh.seek(100)
                fh.write(bytes([b[0] ^ 0xFF]))

    with make_client(settings, import_options=ImportOptions(before_verify=corrupt, min_free_bytes=0)) as c:
        p = import_all(c)["progress"]
        assert p["verify_failed"] == ["DCIM/100RICOH/R0000102.JPG"] and p["failed_shots"] == 1
        assert "安全弹出" not in p["message"] and "请勿格式化" in p["message"]


def test_clean_import_still_says_it_is_safe_to_eject(client, card):
    for rel in card.junk:  # DCIM 里的非照片文件不会被导入，有它们就不能说"都已导入"（PH03 另测）
        if rel.startswith("DCIM/"):
            (card.root / rel).unlink()
    p = import_all(client)["progress"]
    assert p["failed_shots"] == 0 and not p["unreadable"] and not p["verify_failed"] and not p["scan_errors"]
    assert "现在可以推出存储卡了" in p["message"]
    p2 = import_all(client)["progress"]
    assert p2["message"] == "这张存储卡上的照片都已导入"


def test_import_js_uses_the_summary_rules_everywhere():
    js = (WEB / "assets" / "import.js").read_text(encoding="utf-8")
    assert "import_summary.js" in js and "doneStatusText(" in js and "summarizeImport(" in js
    assert "安全弹出" not in js, "文字规则只放在 import_summary.js 里，import.js 不能自己再写一份"
    assert "hasProblems(fresh.progress)" in js and "handled.delete(" in js, "有文件没读进来时不能算「处理完了」"


# ---- P1-1：编辑记录（sidecar）坏了/不见了 ----------------------------------------------


def break_sidecar(path: Path, how: str):
    if how == "corrupt":
        path.write_text("{ not json", encoding="utf-8")
    elif how == "deleted":
        path.unlink()
    elif how == "not-a-sidecar":
        path.write_text('{"hello": 1}', encoding="utf-8")


def assert_plain(text: str, *secrets: str):
    for s in ("Expecting", "Traceback", "JSONDecodeError", "SidecarCorrupt", "/gxrs.json", *secrets):
        assert s not in text, f"提示里不该出现 {s!r}：{text[:200]}"


@pytest.mark.parametrize("how", ["corrupt", "deleted", "not-a-sidecar"])
def test_crop_save_and_export_survive_a_broken_sidecar(client, settings, how):
    import_all(client)
    path, shot = sidecar_of(client, "R0000102")
    media = {k: (client.app.state.ctx.lib.abspath(v["relpath"]).read_bytes()) for k, v in shot["files"].items()}
    break_sidecar(path, how)
    root = str(settings.library_dir)
    before = path.read_bytes() if path.exists() else None

    r = client.get(f"/api/photos/{shot['shot_id']}/crop")
    assert r.status_code == 200 and r.json()["active"] is False and r.json()["warning"] == SIDECAR_RESET_NOTE
    assert_plain(r.text, root)
    r = client.get(f"/api/photos/{shot['shot_id']}/edit")
    assert r.status_code == 200 and r.json()["warning"] == SIDECAR_RESET_NOTE and r.json()["adjust"]["contrast"] == 0
    assert_plain(r.text, root)
    assert (path.read_bytes() if path.exists() else None) == before, "只读接口不改磁盘"

    # 导出：按没有裁切、没有编辑处理，并带白话提示（_crop_for_export 的防护）
    r = client.post(f"/api/photos/{shot['shot_id']}/export", json={})
    assert r.status_code == 200, r.text
    assert r.json()["warning"] == "无法读取编辑记录，已按原图显示"
    assert_plain(r.text, root)

    # 保存裁切：自动重建一份"没有编辑"的记录，坏的改名留着
    r = client.put(f"/api/photos/{shot['shot_id']}/crop", json={"preset": "3:2"})
    assert r.status_code == 200, r.text
    assert r.json()["active"] and r.json()["warning"] == SIDECAR_RESET_NOTE
    assert_plain(r.text, root)
    assert json.loads(path.read_text(encoding="utf-8"))["crop"]["preset"] == "3:2"
    backups = list(path.parent.glob(path.name + ".corrupt-*"))
    assert len(backups) == (0 if how == "deleted" else 1)
    if how == "corrupt":
        assert backups[0].read_text(encoding="utf-8") == "{ not json"
    # 修好以后不再有提示
    r = client.put(f"/api/photos/{shot['shot_id']}/crop", json={"preset": "1:1"})
    assert r.status_code == 200 and "warning" not in r.json()
    # 照片文件一个字节没动
    after = {k: client.app.state.ctx.lib.abspath(v["relpath"]).read_bytes() for k, v in shot["files"].items()}
    assert after == media


@pytest.mark.parametrize("how", ["corrupt", "deleted"])
def test_edit_save_survives_a_broken_sidecar(client, settings, how):
    import_all(client)
    path, shot = sidecar_of(client, "R0000102")
    break_sidecar(path, how)
    r = client.put(f"/api/photos/{shot['shot_id']}/edit", json={"adjust": {"contrast": 5}})
    assert r.status_code == 200, r.text
    assert r.json()["adjust"]["contrast"] == 5 and r.json()["warning"] == SIDECAR_RESET_NOTE
    assert_plain(r.text, str(settings.library_dir))
    r = client.delete(f"/api/photos/{shot['shot_id']}/edit")
    assert r.status_code == 200 and "warning" not in r.json()


def test_export_with_a_broken_sidecar_ignores_the_old_crop(client):
    import_all(client)
    path, shot = sidecar_of(client, "R0000102")
    sid = shot["shot_id"]
    assert client.put(f"/api/photos/{sid}/crop", json={"preset": "xpan-65-24"}).status_code == 200
    cropped = client.post(f"/api/photos/{sid}/export", json={"source": "camera-jpg"}).json()
    assert cropped["height"] < shot["height"]
    path.write_text("{ not json", encoding="utf-8")
    plain = client.post(f"/api/photos/{sid}/export", json={"source": "camera-jpg"})
    assert plain.status_code == 200 and (plain.json()["width"], plain.json()["height"]) == (
        shot["width"],
        shot["height"],
    )


def test_batch_export_with_a_broken_sidecar_and_plain_failures(client, monkeypatch):
    import_all(client)
    path, shot = sidecar_of(client, "R0000102")
    path.write_text("{ not json", encoding="utf-8")
    r = client.post("/api/export", json={"ids": [shot["shot_id"]]})
    assert r.status_code == 202
    job = wait_job(client, r.json()["id"])
    (item,) = job["progress"]["results"]
    assert "error" not in item and item["warning"] == "无法读取编辑记录，已按原图显示"
    assert_plain(json.dumps(item, ensure_ascii=False))

    # P2-5：单张失败的提示是白话 + 错误代码，不带异常原文和路径
    def boom(*a, **k):
        raise RuntimeError("/secret/path/inside.jpg exploded")

    monkeypatch.setattr("gxr_studio.finish.serve.export_finish", boom)
    other = find(client, "R0000101")["id"]
    job = wait_job(client, client.post("/api/export", json={"ids": [other]}).json()["id"])
    (bad,) = job["progress"]["results"]
    assert bad["error"] == "failed" and bad["code"] == "RuntimeError"
    assert bad["message"] == "导出失败。请重试。" and "RuntimeError" not in bad["message"]
    assert "/secret" not in bad["message"] and "exploded" not in bad["message"]


def wait_job(client, job_id):
    for _ in range(400):
        j = client.get(f"/api/jobs/{job_id}").json()
        if not j["running"]:
            return j
        time.sleep(0.05)
    raise AssertionError("任务超时")


def test_unreadable_sidecar_that_cannot_be_reset_is_a_plain_422_not_a_500(client, monkeypatch):
    """兜底：连重建都做不到（比如磁盘只读）也只给白话 422，不泄露路径。"""
    from gxr_studio.library import sidecar as sc

    import_all(client)
    path, shot = sidecar_of(client, "R0000102")
    path.write_text("{ not json", encoding="utf-8")

    def fail(*a, **k):
        raise sc.SidecarCorrupt(f"{path}: Expecting property name")

    monkeypatch.setattr(sc, "backup_corrupt", fail)
    r = client.put(f"/api/photos/{shot['shot_id']}/crop", json={"preset": "3:2"})
    assert r.status_code == 422 and r.json()["error"] == "sidecar_corrupt"
    assert_plain(r.text, str(path))


# ---- P2-1：rebuild 遇到坏 sidecar 先备份 ----------------------------------------------


def test_rebuild_keeps_a_copy_of_a_corrupt_sidecar(client):
    import_all(client)
    path, shot = sidecar_of(client, "R0000102")
    path.write_text("{ not json", encoding="utf-8")
    r = client.post("/api/library/rebuild")
    assert r.status_code == 200
    (kept,) = r.json()["corrupt_backups"]
    assert kept.endswith(".gxrs.json") is False and ".corrupt-" in kept
    lib = client.app.state.ctx.lib
    assert lib.abspath(kept).read_text(encoding="utf-8") == "{ not json"
    assert json.loads(path.read_text(encoding="utf-8"))["schema"] == "gxr-studio/edit/1"
    assert r.json()["sidecars_created"] == 1
    # 再 rebuild 不会把备份当照片，也不会再备份
    r2 = client.post("/api/library/rebuild")
    assert r2.json()["corrupt_backups"] == [] and r2.json()["shots"] == r.json()["shots"]


# ---- P2-2：同一个图库只开一个 ------------------------------------------------------------


def test_second_lock_on_the_same_library_is_refused(tmp_path):
    a = LibraryLock(tmp_path / "lib").acquire()
    with pytest.raises(LibraryBusy):
        LibraryLock(tmp_path / "lib").acquire()
    LibraryLock(tmp_path / "other").acquire().release()  # 别的图库不受影响
    a.release()
    LibraryLock(tmp_path / "lib").acquire().release()  # 放掉以后可以再拿


def test_lock_dies_with_the_process(tmp_path):
    code = (
        "import sys,time\nfrom pathlib import Path\nfrom gxr_studio.library.lock import LibraryLock\n"
        "LibraryLock(Path(sys.argv[1])).acquire()\nprint('locked', flush=True)\ntime.sleep(60)\n"
    )
    p = subprocess.Popen([sys.executable, "-I", "-c", code, str(tmp_path / "lib")], stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "locked"
        with pytest.raises(LibraryBusy):
            LibraryLock(tmp_path / "lib").acquire()
    finally:
        p.kill()
        p.wait()
    LibraryLock(tmp_path / "lib").acquire().release()  # 被杀掉的进程不会留下"锁死"


def test_second_studio_says_so_in_plain_words(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GXR_STUDIO_LIBRARY", str(tmp_path / "lib"))
    monkeypatch.setenv("GXR_STUDIO_EXPORT", str(tmp_path / "exp"))
    monkeypatch.setenv("GXR_STUDIO_VOLUMES", str(tmp_path))
    started = []
    monkeypatch.setattr(cli, "create_app", lambda s: started.append(s))
    with LibraryLock(tmp_path / "lib"):
        with pytest.raises(SystemExit) as e:
            cli.main(["--no-browser"])
    assert e.value.code == BUSY_EXIT_CODE
    out = capsys.readouterr().out
    assert "工作室已经开着了" in out and "第一个" in out
    assert started == [], "第二个实例不能去碰图库"
    command = (Path(__file__).resolve().parents[1] / "GXR Studio.command").read_text(encoding="utf-8")
    assert "-ne 75" in command and BUSY_EXIT_CODE == 75


# ---- P2-4：系统不让读外接卡 --------------------------------------------------------------


class Denied(CardReader):
    def __init__(self, deny):
        self.deny = deny

    def scandir(self, path):
        if self.deny(Path(path)):
            raise PermissionError(errno.EPERM, "Operation not permitted")
        return super().scandir(path)


def test_permission_denied_on_the_volumes_folder_is_reported(settings, volumes):
    reader = Denied(lambda p: p == volumes)
    with make_client(settings, reader=reader) as c:
        assert c.get("/api/cards").json() == []
        st = c.get("/api/cards/status").json()
        assert st["access_denied"] is True and st["cards"] == 0
        assert "存储卡的权限" in st["message"] and "系统设置" in st["message"]


def test_permission_denied_on_one_card_is_reported(settings, volumes, card):
    reader = Denied(lambda p: p == card.root)
    with make_client(settings, reader=reader) as c:
        st = c.get("/api/cards/status").json()
        assert st["access_denied"] is True and "存储卡的权限" in st["message"]


def test_no_warning_when_a_card_is_readable_or_simply_absent(settings, volumes, card):
    with make_client(settings) as c:
        assert c.get("/api/cards/status").json() == {"cards": 1, "access_denied": False, "message": None}
    audit.unprotect(card.root)  # 发现过的卡会被登记保护（写入即拒绝）；这里是测试自己"拔卡"
    shutil.rmtree(card.root)
    with make_client(settings) as c:
        assert c.get("/api/cards/status").json() == {"cards": 0, "access_denied": False, "message": None}


def test_find_cards_collects_denied_volumes_only_when_asked(volumes, card):
    denied: list[str] = []
    find_cards([volumes], Denied(lambda p: p == card.root), denied)
    assert denied == ["NO NAME"]
    assert find_cards([volumes], Denied(lambda p: p == card.root)) == []  # 老调用方式不变


def test_readme_explains_the_removable_volumes_permission():
    readme = (WEB.parent / "README.md").read_text(encoding="utf-8")
    assert "可移除的卷" in readme[: readme.index("## 更详细的说明")], "新手那三步里就要写明"
    js = (WEB / "assets" / "import.js").read_text(encoding="utf-8")
    assert "/api/cards/status" in js and "cardAccess" in js


# ---- P2-6：标定路径预填 -----------------------------------------------------------------


def test_status_offers_the_project_folder_and_the_ui_uses_it(client):
    s = client.get("/api/camdata/status").json()
    project = s.get("project_dir")
    if not project or not (Path(project) / "research").is_dir() or not (Path(project) / "studio").is_dir():
        pytest.skip("工作室旁边没有 research/ 和 studio/，跳过项目文件夹预填")
    assert (Path(project) / "research").is_dir()
    assert (Path(project) / "studio").is_dir()
    js = (WEB / "assets" / "camdata.js").read_text(encoding="utf-8")
    assert "项目文件夹" in js and "projectDir" in js and "state.camdata?.projectDir" in js


# ---- P2-7：标签页角标 --------------------------------------------------------------------


def test_recipes_tab_has_no_coming_soon_badge_but_the_camera_tab_keeps_it():
    index = (WEB / "index.html").read_text(encoding="utf-8")
    assert "即将推出" not in index, "「配方」页已经能用（导入调色文件→做成配方），页签上不能再挂「即将推出」"
    camera = (WEB / "assets" / "camera_page.js").read_text(encoding="utf-8")
    assert "暂不支持写入存储卡" in camera
    assert "import { renderCamera } from './camera_page.js'" in (WEB / "assets" / "app.js").read_text(encoding="utf-8")
    recipes = (WEB / "assets" / "recipes.js").read_text(encoding="utf-8")
    import re

    assert not re.search(r"'[^'\n]*M3[^'\n]*'", recipes), "界面文字里不要出现内部代号"


# ---- P2-8：相机数据/标定目录切换 ---------------------------------------------------------


def make_dir(path: Path, text: str) -> Path:
    path.mkdir(parents=True)
    (path / "manifest.json").write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize("atomic", [True, False])
def test_swap_in_replaces_the_directory_and_leaves_nothing_behind(tmp_path, monkeypatch, atomic):
    if not atomic:
        monkeypatch.setattr(privacy, "_exchange_at", lambda fd, a, b: False)
    elif sys.platform != "darwin":
        pytest.skip("原子对调只在 macOS 上有")
    base = tmp_path / "base"
    root = make_dir(base / "camera-data", "old")
    new = make_dir(base / "camera-data.new", "new")
    store.swap_in(new, root)
    assert (root / "manifest.json").read_text(encoding="utf-8") == "new"
    assert sorted(p.name for p in base.iterdir()) == ["camera-data"]


def test_swap_in_works_when_there_is_nothing_to_replace(tmp_path):
    new = make_dir(tmp_path / "home" / "calib.new", "new")
    store.swap_in(new, tmp_path / "home" / "calib")
    assert (tmp_path / "home" / "calib" / "manifest.json").read_text(encoding="utf-8") == "new"


def test_atomic_exchange_never_exposes_a_missing_directory(tmp_path):
    if sys.platform != "darwin":
        pytest.skip("原子对调只在 macOS 上有")
    a, b = make_dir(tmp_path / "a", "A"), make_dir(tmp_path / "b", "B")
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        if not privacy._exchange_at(fd, "a", "b"):
            pytest.skip("这个文件系统不支持 renameatx_np")
    finally:
        os.close(fd)
    assert (a / "manifest.json").read_text(encoding="utf-8") == "B" and (b / "manifest.json").read_text(encoding="utf-8") == "A"


def test_interrupted_fallback_swap_is_recovered_at_startup(tmp_path, monkeypatch):
    """退回的两步换法在两步之间被打断：留下 camera-data.old，下次启动自动改回去。"""
    monkeypatch.setattr(privacy, "_exchange_at", lambda fd, a, b: False)
    home = tmp_path / "home"
    root = make_dir(home / "camera-data", "old")
    new = make_dir(home / "camera-data.new", "new")
    real_rename = osplat.rename_at
    calls = []

    def killed(dfd, src, dst):  # 平台原语：两个平台都经过它
        calls.append((src, dst))
        if len(calls) == 2:
            raise KeyboardInterrupt("断电")
        return real_rename(dfd, src, dst)

    monkeypatch.setattr(osplat, "rename_at", killed)
    with pytest.raises(KeyboardInterrupt):
        store.swap_in(new, root)
    monkeypatch.setattr(osplat, "rename_at", real_rename)
    assert not root.exists() and (home / "camera-data.old" / "manifest.json").read_text(encoding="utf-8") == "old"
    assert store.recover_interrupted_swaps(home) == ["camera-data: restored from .old"]
    assert (root / "manifest.json").read_text(encoding="utf-8") == "old" and not (home / "camera-data.old").exists()


def test_recovery_removes_a_stale_old_and_ignores_clean_homes(tmp_path):
    home = tmp_path / "home"
    assert store.recover_interrupted_swaps(home) == []  # 目录都不存在
    make_dir(home / "calib", "new")
    make_dir(home / "calib.old", "stale")
    assert store.recover_interrupted_swaps(home) == ["calib: removed stale .old"]
    assert (home / "calib" / "manifest.json").read_text(encoding="utf-8") == "new" and not (home / "calib.old").exists()
    assert store.recover_interrupted_swaps(home) == []


def test_starting_the_app_recovers_an_interrupted_swap(settings):
    make_dir(settings.home_dir / "camera-data.old", "restore-me")
    make_dir(settings.home_dir / "calib.old", "me-too")
    with make_client(settings):
        pass
    assert (settings.home_dir / "camera-data" / "manifest.json").read_text(encoding="utf-8") == "restore-me"
    assert (settings.home_dir / "calib" / "manifest.json").read_text(encoding="utf-8") == "me-too"
    assert not list(settings.home_dir.glob("*.old"))


def test_import_paths_use_the_atomic_swap():
    # 复审 A1a P1-05 以后：相机数据和标定都经 store.write_tree → PrivateDir.swap_in，按已核对的目录 fd 换目录
    base = Path(store.__file__).parent
    store_src, calib_src = (
        (base / "store.py").read_text(encoding="utf-8"),
        (base / "calib.py").read_text(encoding="utf-8"),
    )
    privacy_src = Path(privacy.__file__).read_text(encoding="utf-8")
    assert "d.swap_in(tmp, name)" in store_src and "store.write_tree(" in calib_src
    assert "os.replace(" not in store_src and "os.replace(" not in calib_src and ".old" not in calib_src
    assert privacy_src.count("osplat.rename_at(self.fd, root, old)") == 1, "两步换法只留在 swap_in 的退路里"


# ---- P2-10：同时插着多张卡 ---------------------------------------------------------------


def test_two_cards_are_both_listed_and_the_ui_announces_it(settings, volumes, card):
    sy.make_card(volumes, "SECOND", samples=None)
    with make_client(settings) as c:
        cards = c.get("/api/cards").json()
        assert [x["label"] for x in cards] == ["NO NAME", "SECOND"]
    js = (WEB / "assets" / "import.js").read_text(encoding="utf-8")
    summary = (WEB / "assets" / "import_summary.js").read_text(encoding="utf-8")
    assert "multiCardNote(state.cards)" in js and "张存储卡" in summary


# ---- P2-12：Content-Security-Policy ------------------------------------------------------


def test_csp_header_on_pages_assets_and_api(client):
    for url in ("/", "/assets/app.js", "/assets/app.css", "/api/health"):
        r = client.get(url)
        csp = r.headers["content-security-policy"]
        assert csp.startswith("default-src 'self'"), (url, csp)
        assert "unsafe-inline" not in csp and "unsafe-eval" not in csp and "http" not in csp
        assert "img-src 'self' blob: data:" in csp  # 预览图是 object URL


def test_frontend_is_compatible_with_the_csp():
    """CSP 不允许内联脚本/样式属性/外部资源：前端不能有这些写法（浏览器走查里再用真 Chrome 核对一遍）。"""
    import re

    index = (WEB / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"<script(?![^>]*\bsrc=)", index) and not re.search(r"<style", index)
    assert not re.search(r"\son[a-z]+=", index) and "style=" not in index and not re.search(r"https?://", index)
    for js in (WEB / "assets").glob("*.js"):
        text = js.read_text(encoding="utf-8")
        assert "setAttribute('style'" not in text and 'setAttribute("style"' not in text, js.name
        assert "eval(" not in text and "new Function" not in text, js.name
        for m in re.finditer(r"innerHTML\s*=\s*([^;]+);", text):
            assert "style=" not in m.group(1) and "<script" not in m.group(1), (js.name, m.group(1)[:80])
