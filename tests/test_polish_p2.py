"""复核 review-studio-fix-r1 之后新发现的 6 条 P2（research/review_studio_fix/REVIEW.md），修复说明见 research/studio_polish_p2/REPORT.md。"""

from __future__ import annotations

import errno
import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import PORT
from gxr_studio.app import create_app
from gxr_studio.cards.discovery import CardReader, find_cards
from gxr_studio.cards.importer import ImportOptions, run_import
from gxr_studio.devtools import synthetic_card as sy
from gxr_studio.library import edits, thumbs
from gxr_studio.library import sidecar as sc
from gxr_studio.library.store import Library
from support.waiting import wait_job, wait_until
from test_api import find, import_all
from test_fix_review import assert_plain, make_client, sidecar_of

WEB = Path(__file__).resolve().parents[1] / "web"
SRC = Path(__file__).resolve().parents[1] / "src"


@pytest.fixture
def client(settings, card):
    with make_client(settings) as c:
        yield c


# ---- P2-2：缩略图被清掉的瞬间不能 500 ----------------------------------------------------


def test_thumb_survives_the_cache_file_vanishing_between_ensure_and_send(client, monkeypatch):
    import_all(client)
    pid = find(client, "R0000101")["id"]
    real_ensure = thumbs.ensure
    calls = []

    def racy(lib, shot, size, render=None):
        path = real_ensure(lib, shot, size, render)
        if not calls:  # 第一次：返回路径之后文件马上被删（相当于 purge_variants 正好跑到这里）
            path.unlink()
        calls.append(path)
        return path

    monkeypatch.setattr(thumbs, "ensure", racy)
    r = client.get(f"/api/photos/{pid}/thumb")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content[:2] == b"\xff\xd8"
    assert len(calls) == 2, "文件没了要重画一次"


def test_thumb_gives_a_plain_404_when_it_keeps_vanishing(client, monkeypatch):
    import_all(client)
    pid = find(client, "R0000101")["id"]
    real_ensure = thumbs.ensure

    def always_gone(lib, shot, size, render=None):
        path = real_ensure(lib, shot, size, render)
        path.unlink()
        return path

    monkeypatch.setattr(thumbs, "ensure", always_gone)
    r = client.get(f"/api/photos/{pid}/thumb")
    assert r.status_code == 404 and r.json()["error"] == "no_image"


def test_thumb_stress_while_camera_data_is_purged_never_500(settings, volumes):
    """3 个线程不停取只有 DNG 的缩略图，另一个线程不停 purge_variants（导入相机数据/标定包时会做）：0 次 500。"""
    import shutil

    if not sy.DEFAULT_SAMPLES.is_dir():
        pytest.skip("captures/a12-50 样本不在本机")
    card = sy.make_card(volumes, "C", samples=None)
    for i, dng in enumerate(sorted(sy.DEFAULT_SAMPLES.glob("*.DNG"))[:2]):
        shutil.copy2(dng, card.root / "DCIM/100RICOH" / f"R000090{i}.DNG")  # 只有 DNG 的真实照片
    app = create_app(settings)
    stop = threading.Event()
    counts = {"ok": 0, "500": 0, "other": 0}
    lock = threading.Lock()
    headers = {"X-GXR-Token": settings.token}
    base = f"http://127.0.0.1:{PORT}"
    with TestClient(app, base_url=base, headers=headers, raise_server_exceptions=False) as c0:
        import_all(c0)
        ids = [p["id"] for p in c0.get("/api/photos").json() if p["name"].startswith("R00009")]
        assert len(ids) == 2
        lib = c0.app.state.ctx.lib

        def getter():
            c = TestClient(app, base_url=base, headers=headers, raise_server_exceptions=False)  # 不带 with：不重复启停图库
            while not stop.is_set():
                for i in ids:
                    code = c.get(f"/api/photos/{i}/thumb").status_code
                    with lock:
                        counts["ok" if code == 200 else "500" if code >= 500 else "other"] += 1

        def purger():
            while not stop.is_set():
                thumbs.purge_variants(lib)
                time.sleep(0.001)

        ths = [threading.Thread(target=getter) for _ in range(3)] + [threading.Thread(target=purger)]
        for t in ths:
            t.start()
        time.sleep(8)
        stop.set()
        for t in ths:
            t.join()
    assert counts["500"] == 0 and counts["other"] == 0 and counts["ok"] > 50, counts


# ---- P2-3：批量导出里有失效 id ------------------------------------------------------------


def test_batch_export_skips_vanished_ids_and_says_so(client, tmp_path):
    import_all(client)
    good = find(client, "R0000101")["id"]
    r = client.post("/api/export", json={"ids": [good, "0123456789abcdef", good], "dest_dir": str(tmp_path / "out")})
    assert r.status_code == 202
    job = wait_job(client, r.json()["id"])["progress"]
    assert job["state"] == "done" and job["total"] == 2 and job["done"] == 2
    ok = [x for x in job["results"] if "error" not in x]
    bad = [x for x in job["results"] if "error" in x]
    assert len(ok) == 1 and ok[0]["id"] == good and Path(ok[0]["path"]).is_file()
    assert [(x["id"], x["error"]) for x in bad] == [("0123456789abcdef", "photo_not_found")]
    assert "找不到这张照片" in bad[0]["message"] and "重新选" in bad[0]["message"]
    assert_plain(json.dumps(bad[0], ensure_ascii=False))


def test_batch_export_of_only_vanished_ids_is_not_a_404(client):
    import_all(client)
    r = client.post("/api/export", json={"ids": ["0123456789abcdef"]})
    assert r.status_code == 202
    job = wait_job(client, r.json()["id"])["progress"]
    assert job["state"] == "done" and job["results"][0]["error"] == "photo_not_found"


def test_frontend_refreshes_the_selection_and_names_unexported_photos():
    gallery = (WEB / "assets/gallery.js").read_text(encoding="utf-8")
    assert "pruneSelection" in gallery and "state.selection.delete" in gallery
    assert gallery.index("await pruneSelection()") < gallery.index("loadAll();", gallery.index("library-changed"))
    export = (WEB / "assets/export.js").read_text(encoding="utf-8")
    assert "followBatch(job, result, photos)" in export and "导出失败。请重试。" in export
    assert "errorBody" in export


# ---- P2-4：服务没了，拟合子进程跟着走 ----------------------------------------------------


def _alive(pid: int) -> bool:
    """进程还在不在。Windows 上不能用 os.kill(pid, 0)（那是 TerminateProcess），改查退出码。"""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        try:
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_worker_exits_by_itself_when_the_parent_is_killed(tmp_path):
    # 父进程阻塞在读标准输入上（POSIX 用 /bin/sh，Windows 用一个 Python），被强杀之后子进程要自己退出。
    pid_path = tmp_path / "worker.pid"
    parent_pid_path = tmp_path / "parent.pid"
    process_path = tmp_path / "worker-processes.json"
    error_path = tmp_path / "worker.stderr"
    child_code = (
        "import json, os, time\nfrom pathlib import Path\n"
        f"Path({str(pid_path)!r}).write_text(str(os.getpid()), encoding='ascii')\n"
        "from gxr_studio.lut.worker import watch_parent\n"
        "parent_pid = int(os.environ['GXR_STUDIO_PARENT_PID'])\n"
        f"process_path = Path({str(process_path)!r})\n"
        "process_path.write_text(json.dumps({'worker': os.getpid(), 'reported_parent': os.getppid(), "
        "'expected_parent': parent_pid}), encoding='utf-8')\n"
        "stop = {'flag': False}\nwatch_parent(stop, parent_pid, 0.1, 0.3)\n"
        "print(os.getpid(), flush=True)\ntime.sleep(120)\n"
    )
    if sys.platform == "win32":
        launcher = (
            "import os, subprocess, sys\nfrom pathlib import Path\n"
            f"Path({str(parent_pid_path)!r}).write_text(str(os.getpid()), encoding='ascii')\n"
            "env = {**os.environ, 'GXR_STUDIO_PARENT_PID': str(os.getpid())}\n"
            "subprocess.Popen([sys.executable, '-c', sys.argv[1]], stdin=subprocess.DEVNULL, stdout=sys.stdout, stderr=sys.stderr, env=env)\n"
            "sys.stdin.read()\n"
        )
        command = [sys.executable, "-c", launcher, child_code]
    else:
        command = ["/bin/sh", "-c", f"GXR_STUDIO_PARENT_PID=$$ {shlex.quote(sys.executable)} -c {shlex.quote(child_code)} & read _"]
    with error_path.open("wb") as errors:
        parent = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, text=True, encoding="utf-8")
    ready, lines = threading.Event(), []

    def read_pid():
        lines.append(parent.stdout.readline())
        ready.set()

    reader = threading.Thread(target=read_pid, daemon=True)
    reader.start()
    child_pid = None
    actual_parent = None
    try:
        assert ready.wait(30), error_path.read_text(encoding="utf-8", errors="replace")
        assert lines and lines[0].strip().isdigit(), (lines, error_path.read_text(encoding="utf-8", errors="replace"))
        child_pid = int(lines[0])
        actual_parent = int(parent_pid_path.read_text(encoding="ascii")) if sys.platform == "win32" else parent.pid
        processes = json.loads(process_path.read_text(encoding="utf-8"))
        assert processes["worker"] == child_pid and processes["expected_parent"] == actual_parent
        print(json.dumps({"launcher": parent.pid, "parent": actual_parent, **processes}))
        assert _alive(child_pid)  # 子进程在跑
        assert _alive(actual_parent)
        if sys.platform == "win32":
            os.kill(actual_parent, signal.SIGTERM)  # TerminateProcess：实际父进程没有机会清理。
        else:
            parent.kill()
        parent.wait(timeout=5)
        try:
            # 期限必须远小于子进程自己的 sleep(120)：守护失效时子进程睡满也会退出，期限一长测试就会误过。
            wait_until(lambda: not _alive(child_pid), "父进程被杀之后子进程退出", timeout=15, interval=0.1)
        except AssertionError as exc:
            pytest.fail(f"父进程被杀之后子进程还在（{exc}）")
    finally:
        if sys.platform == "win32" and parent_pid_path.exists():
            actual_parent = int(parent_pid_path.read_text(encoding="ascii"))
            if _alive(actual_parent):
                os.kill(actual_parent, signal.SIGTERM)
        if parent.poll() is None:
            parent.kill()
        parent.wait(timeout=5)
        if child_pid is None and pid_path.exists():
            child_pid = int(pid_path.read_text(encoding="ascii"))
        if child_pid and _alive(child_pid):
            try:
                os.kill(child_pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            except OSError:
                pass
        reader.join(timeout=5)
        assert not reader.is_alive(), "子进程的标准输出没有关闭"
        parent.stdin.close()
        parent.stdout.close()


def test_worker_sets_the_stop_flag_when_the_parent_goes_away(monkeypatch):
    from gxr_studio.lut import worker

    stop = {"flag": False}
    exits = []
    # 一个已经结束的进程当"父进程"（Windows 按句柄看它结束了没有）；POSIX 上 getppid 变成 1 = 已经被过继
    ended = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True, check=True)
    monkeypatch.setattr(worker.os, "_exit", lambda code: exits.append(code))
    monkeypatch.setattr(worker.os, "getppid", lambda: 1)
    worker.watch_parent(stop, int(ended.stdout), 0.01, 0.01).join(2)
    assert stop["flag"] is True and exits == [0]


def test_fit_worker_is_told_who_its_parent_is(monkeypatch, tmp_path):
    src = (SRC / "gxr_studio/lut/service.py").read_text(encoding="utf-8")
    assert '"GXR_STUDIO_PARENT_PID": str(os.getpid())' in src
    assert "GXR_STUDIO_PARENT_PID" in (SRC / "gxr_studio/lut/worker.py").read_text(encoding="utf-8")


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="Windows 没有 SIGHUP（关控制台窗口不是信号）")
def test_studio_shuts_down_cleanly_on_sighup(tmp_path):
    """直接关终端窗口 = SIGHUP：服务要走正常退出（拟合子进程的清理才会执行），而不是被默认动作立刻带走。"""
    env = {
        **os.environ,
        "GXR_STUDIO_LIBRARY": str(tmp_path / "lib"),
        "GXR_STUDIO_EXPORT": str(tmp_path / "exp"),
        "GXR_STUDIO_HOME": str(tmp_path / "home"),
        "GXR_STUDIO_VOLUMES": str(tmp_path / "emptyvols"),
        "GXR_STUDIO_TOKEN": "tok-" + "h" * 40,
        "HOME": str(tmp_path / "h"),
    }
    (tmp_path / "emptyvols").mkdir()
    (tmp_path / "h").mkdir()
    p = subprocess.Popen(
        [sys.executable, "-m", "gxr_studio.cli", "--no-browser"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        assert "GXR 工作室已启动" in p.stdout.readline()
        p.stdout.readline()
        time.sleep(1)
        p.send_signal(signal.SIGHUP)
        assert p.wait(20) == 0, "SIGHUP 应该是正常退出（退出码 0），不是被信号杀掉"
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()


# ---- P2-5：手改出来的编辑文件，字段类型错 ---------------------------------------------------


BAD_SHAPES = {
    "crop 是字符串": {"crop": "oops"},
    "crop 缺字段": {"crop": {"preset": "3:2"}},
    "crop rect 里有字符串": {"crop": {"preset": "3:2", "rect": [0, 0, "a", 1]}},
    "rev 是字符串": {"rev": "abc"},
    "rev 是布尔": {"rev": True},
    "frame 是列表": {"frame": [1, 2]},
    "frame.width 是字符串": {"frame": {"width": "wide", "height": 10, "orientation": 1}},
}


@pytest.mark.parametrize("name", list(BAD_SHAPES))
def test_wrongly_typed_sidecar_is_treated_as_corrupt_not_500(client, name):
    import_all(client)
    path, shot = sidecar_of(client, "R0000102")
    sid = shot["shot_id"]
    good = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**good, **BAD_SHAPES[name]}), encoding="utf-8")
    media = {k: client.app.state.ctx.lib.abspath(v["relpath"]).read_bytes() for k, v in shot["files"].items()}
    for method, url, kw in (
        ("GET", f"/api/photos/{sid}/crop", {}),
        ("GET", f"/api/photos/{sid}/edit", {}),
        ("POST", f"/api/photos/{sid}/export", {"json": {}}),
        ("GET", f"/api/photos/{sid}/thumb", {}),
        ("GET", f"/api/photos/{sid}", {}),
    ):
        r = client.request(method, url, **kw)
        assert r.status_code == 200, (name, url, r.status_code, r.text[:200])
    # 写：坏文件改名备份，重新建一份"没有编辑"的，提示用户
    r = client.put(f"/api/photos/{sid}/crop", json={"preset": "1:1"})
    assert r.status_code == 200, (name, r.text[:200])
    assert list(path.parent.glob(path.name + ".corrupt-*")), "坏文件要留着备份"
    assert sc.read(path)["crop"]["preset"] == "1:1"
    assert media == {k: client.app.state.ctx.lib.abspath(v["relpath"]).read_bytes() for k, v in shot["files"].items()}


def test_sidecar_from_a_newer_schema_is_still_left_alone(client):
    import_all(client)
    path, shot = sidecar_of(client, "R0000102")
    good = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**good, "schema": "gxr-studio/edit/99", "crop": "future-format"}), encoding="utf-8")
    before = path.read_bytes()
    assert client.put(f"/api/photos/{shot['shot_id']}/crop", json={"preset": "1:1"}).status_code == 409
    assert path.read_bytes() == before and not list(path.parent.glob("*.corrupt-*"))


def test_our_own_sidecars_pass_the_shape_check(client):
    import_all(client)
    path, shot = sidecar_of(client, "R0000102")
    assert client.put(f"/api/photos/{shot['shot_id']}/crop", json={"preset": "3:2", "angle_deg": 1.5}).status_code == 200
    assert sc.read(path)["crop"]["angle_deg"] == 1.5
    assert not list(path.parent.glob("*.corrupt-*"))


# ---- P2-6：统一叫"中性转换" ---------------------------------------------------------------


def test_one_name_for_the_neutral_look():
    from gxr_studio.raw import NEUTRAL

    assert NEUTRAL.label == "中性转换" and NEUTRAL.id == "neutral-preview"  # id/文件名标记是接口，不改
    studio = Path(__file__).resolve().parents[1]
    offenders = []
    for root in (studio / "src", studio / "web", studio / "README.md"):
        files = [root] if root.is_file() else [p for p in root.rglob("*") if p.is_file() and p.suffix in (".py", ".js", ".html", ".md", ".css")]
        offenders += [str(p) for p in files if "中性预览" in p.read_text(encoding="utf-8")]
    assert offenders == []


def test_lut_page_label_says_neutral_conversion():
    assert '"neutral": "中性转换"' in (SRC / "gxr_studio/api/lut_routes.py").read_text(encoding="utf-8")


# ---- P2-1：补全那一刻被打断 ---------------------------------------------------------------

OPTS = dict(min_free_bytes=0)


def test_completion_interrupted_after_the_move_is_resumed_not_duplicated(volumes, card, lib, monkeypatch):
    c = find_cards([volumes])[0]

    class Bad(CardReader):
        def open_rb(self, path, **kwargs):
            if Path(path).name == "R0000104.JPG":
                raise OSError(errno.EIO, "I/O error")
            return super().open_rb(path, **kwargs)

    run_import(lib, c, ImportOptions(**OPTS), reader=Bad())
    (shot,) = [s for s in lib.list_shots(limit=1000) if s["name"] == "R0000104"]
    assert set(shot["files"]) == {"dng"}
    edits.write_crop(lib, shot, preset="1:1", rect=None, angle_deg=0.0)
    side_path = lib.sidecar_path_for(shot)
    crop_before = sc.read(side_path)["crop"]
    total = lib.count()

    def killed(self, *a, **k):
        raise RuntimeError("killed here")

    with monkeypatch.context() as m:  # JPG 已经换到位，登记之前被打断
        m.setattr(Library, "replace_shot", killed)
        with pytest.raises(RuntimeError):
            run_import(lib, c, ImportOptions(**OPTS))
    assert (side_path.parent / "R0000104.JPG").exists()
    lib.close()
    lib = Library(lib.root).open()
    p = run_import(lib, c, ImportOptions(**OPTS))
    rows = [s for s in lib.list_shots(limit=1000) if s["name"] == "R0000104"]
    assert len(rows) == 1 and set(rows[0]["files"]) == {"dng", "jpg"} and lib.count() == total
    assert sc.read(side_path)["crop"] == crop_before, "裁切不能丢"
    assert rows[0]["crop"] and rows[0]["crop"]["preset"] == "1:1"
    # 假卡 DCIM 里的 ._R0000101.JPG 是 macOS 系统文件（忽略）；Thumbs.db、NOTE.TXT 不会被导入，所以不说"可以推出"
    assert p.state == "done" and p.completed_shots == 1 and p.message == "已导入 1 张照片，卡上的照片都已在图库里；另有 2 个其他文件不会导入"
    assert not list(side_path.parent.glob("R0000104*.corrupt-*"))


def test_completion_does_not_adopt_a_different_file_in_the_way(volumes, card, lib):
    """目标位置上有个内容不一样的同名文件：不能当作自己的残留，也不能覆盖它——和以前一样落到另起一张。"""
    c = find_cards([volumes])[0]

    class Bad(CardReader):
        def open_rb(self, path, **kwargs):
            if Path(path).name == "R0000104.JPG":
                raise OSError(errno.EIO, "I/O error")
            return super().open_rb(path, **kwargs)

    run_import(lib, c, ImportOptions(**OPTS), reader=Bad())
    (shot,) = [s for s in lib.list_shots(limit=1000) if s["name"] == "R0000104"]
    squatter = lib.sidecar_path_for(shot).parent / "R0000104.JPG"
    squatter.write_bytes(b"not the card's jpg")
    run_import(lib, c, ImportOptions(**OPTS))
    assert squatter.read_bytes() == b"not the card's jpg"
