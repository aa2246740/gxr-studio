"""编辑过的半格一对：成片按编辑缓存，没人等的全尺寸出图在安全点停下（UNIT hf-render）。

- 同一对、同一份编辑、同一档 stage/size/视图再要一次：`X-GXR-Cache: hit`，字节和第一次、和清掉缓存后重算的都一样；
- 改了编辑：miss，字节不同；
- 发请求的连接断了（页面刷新、换照片、中止 fetch）：服务器在下一个条带停下，不进缓存；之后的新请求照常算出同样的字节；
- 缓存键覆盖每个会改变输出的输入（不需要样本）。

实拍样本用 r6-field-20261118/card250 的前两对（和 test_halfframe_p1a_editor_browser 一样）。不打印文件名和 EXIF。
"""

from __future__ import annotations

import dataclasses
import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi.testclient import TestClient

from conftest import PORT
from gxr_studio.app import create_app
from gxr_studio.cards.discovery import find_cards
from gxr_studio.cards.importer import ImportOptions, run_import
from gxr_studio.library.store import Library
from gxr_studio.render.params import Adjust, EditState, RecipeSel
from test_halfframe_p0 import _clear_desktop_cache, _field_folder, _setup_g250_calibrated_home, _sorted_pairs, _stage_volume_pairs
from test_halfframe_p1a_field import _index_shots_by_jpg_sha

FULL = "/api/photos/{}/render?stage=final&size=full"
NO_FRAME = {"style": "none"}
NO_STAMP = {"enabled": False}


def _page_body(ev: float) -> dict[str, Any]:
    """和桌面编辑器 `draftBody` 发的形状一样：没改过配方的一对发 recipe null，边框和时间戳在浏览器里画。"""
    return {"recipe": None, "lut": None, "adjust": {"exposure_ev": ev}, "curve": None, "geometry": None, "frame": NO_FRAME, "stamp": NO_STAMP}


def _pair_library(settings: Any, lib: Library, volumes: Path) -> str:
    _clear_desktop_cache()
    pairs = _sorted_pairs(_field_folder("card250"))
    _stage_volume_pairs(volumes, pairs[:2])
    _setup_g250_calibrated_home(settings)
    run_import(lib, find_cards((volumes,))[0], ImportOptions(min_free_bytes=0))
    shot = _index_shots_by_jpg_sha(lib, pairs[:2])[2]
    assert shot.get("pair", {}).get("role") == "second"
    lib.close()
    return str(shot["shot_id"])


def _client(settings: Any) -> TestClient:
    return TestClient(create_app(settings), base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token})


def _stage_entries(client_app: Any) -> int:
    return sum(1 for k in list(client_app.state.ctx.render._results._d) if isinstance(k, tuple) and k[:1] == ("stage",))


def test_edited_pair_full_render_twice_is_miss_then_hit_with_equal_bytes(settings: Any, lib: Library, volumes: Path) -> None:
    p2 = _pair_library(settings, lib, volumes)
    with _client(settings) as c:
        assert c.put(f"/api/photos/{p2}/edit", json={"adjust": {"exposure_ev": 0.5}}).status_code == 200
        first = c.post(FULL.format(p2), json=_page_body(0.5))
        assert first.status_code == 200, first.text
        assert first.headers["X-GXR-Cache"] == "miss"
        again = c.post(FULL.format(p2), json=_page_body(0.5))
        assert again.status_code == 200, again.text
        assert again.headers["X-GXR-Cache"] == "hit", "同一份编辑的全尺寸成片又算了一遍"
        assert again.content == first.content
        for name in ("X-GXR-Label", "X-GXR-Calibrated", "X-GXR-Scale", "X-GXR-Prediction"):
            assert again.headers.get(name) == first.headers.get(name), name
        # 单张视图（第 2 张）同样缓存
        half = c.post(FULL.format(p2) + "&view=second", json=_page_body(0.5))
        assert half.status_code == 200 and half.headers["X-GXR-Cache"] == "miss"
        half2 = c.post(FULL.format(p2) + "&view=second", json=_page_body(0.5))
        assert half2.headers["X-GXR-Cache"] == "hit" and half2.content == half.content
        # 缓存给的字节就是重新算出来的字节
        c.app.state.ctx.render._results.clear()  # type: ignore[attr-defined]
        fresh = c.post(FULL.format(p2), json=_page_body(0.5))
        assert fresh.headers["X-GXR-Cache"] == "miss" and fresh.content == first.content


def test_edited_pair_full_render_after_edit_is_miss_with_new_bytes(settings: Any, lib: Library, volumes: Path) -> None:
    p2 = _pair_library(settings, lib, volumes)
    with _client(settings) as c:
        assert c.put(f"/api/photos/{p2}/edit", json={"adjust": {"exposure_ev": 0.5}}).status_code == 200
        before = c.post(FULL.format(p2), json=_page_body(0.5))
        assert before.status_code == 200 and before.headers["X-GXR-Cache"] == "miss"
        assert c.put(f"/api/photos/{p2}/edit", json={"adjust": {"exposure_ev": 1.0}}).status_code == 200
        after = c.post(FULL.format(p2), json=_page_body(1.0))
        assert after.status_code == 200, after.text
        assert after.headers["X-GXR-Cache"] == "miss", "改了编辑还拿到了缓存"
        assert after.content != before.content
        assert c.post(FULL.format(p2), json=_page_body(1.0)).headers["X-GXR-Cache"] == "hit"


_CLIENT = r"""
import http.client, json, sys
port, path, token, body_file, out_file, mode = sys.argv[1:7]
body = open(body_file, "rb").read()
conn = http.client.HTTPConnection("127.0.0.1", int(port), timeout=600)
conn.request("POST", path, body, {"X-GXR-Token": token, "Content-Type": "application/json"})
if mode == "abandon":
    print("sent", flush=True)
    sys.stdin.readline()
    conn.sock.shutdown(2)
    conn.close()
    print("closed", flush=True)
    sys.exit(0)
res = conn.getresponse()
data = res.read()
open(out_file, "wb").write(data)
print(json.dumps({"status": res.status, "cache": res.getheader("X-GXR-Cache")}), flush=True)
"""


def _wait(pred: Any, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.05)
    raise AssertionError(f"timeout waiting for {what}")


def test_abandoned_full_render_stops_and_newer_render_gets_right_bytes(
    settings: Any, lib: Library, volumes: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真 uvicorn、真 TCP：客户端发完请求就断开（浏览器刷新页面、中止 fetch 时就是这样）。

    条带计数器 + 每条带 0.2 秒的慢步骤：断开后服务器在下一个条带停下，不跑到最后，也不把半张图存进缓存；
    之后同一份编辑的新请求是 miss，字节和事先单独算的一样。"""
    from gxr_studio import halfframe_compose as hfc
    from gxr_studio.finish import serve

    p2 = _pair_library(settings, lib, volumes)
    body = tmp_path / "body.json"
    body.write_text(json.dumps(_page_body(0.5)), encoding="utf-8")
    with _client(settings) as c:
        assert c.put(f"/api/photos/{p2}/edit", json={"adjust": {"exposure_ev": 0.5}}).status_code == 200
        reference = c.post(FULL.format(p2), json=_page_body(0.5))
        assert reference.status_code == 200, reference.text

    strips = {"n": 0, "slow": True}
    real_strips = hfc.demosaic_strips

    def slow_strips(dng: Any, *args: Any, **kwargs: Any) -> Any:
        for item in real_strips(dng, *args, **kwargs):
            strips["n"] += 1
            if strips["slow"]:
                time.sleep(0.2)
            yield item

    outcomes: list[str] = []
    real_stage = serve.render_stage

    def watched_stage(*args: Any, **kwargs: Any) -> Any:
        try:
            out = real_stage(*args, **kwargs)
        except Exception as exc:
            outcomes.append(type(exc).__name__)
            raise
        outcomes.append("done")
        return out

    monkeypatch.setattr(hfc, "demosaic_strips", slow_strips)
    monkeypatch.setattr(serve, "render_stage", watched_stage)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    app = create_app(settings.with_(port=port))
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()

    def client(mode: str, out: Path) -> subprocess.Popen[str]:
        args = [sys.executable, "-I", "-c", _CLIENT, str(port), FULL.format(p2), settings.token, str(body), str(out), mode]
        return subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

    try:
        _wait(lambda: server.started, 10, "server start")
        gone = client("abandon", tmp_path / "unused.jpg")
        try:
            assert gone.stdout is not None and gone.stdout.readline().strip() == "sent"
            _wait(lambda: strips["n"] >= 3, 120, "abandoned render to reach strip 3")
            at_close = strips["n"]
            assert gone.stdin is not None
            gone.stdin.write("\n")
            gone.stdin.flush()
            assert gone.stdout.readline().strip() == "closed"
        finally:
            gone.communicate(timeout=30)
        _wait(lambda: bool(outcomes), 120, "abandoned render to end")
        stopped_at = strips["n"]
        time.sleep(0.6)
        assert strips["n"] == stopped_at, "断开之后还在算条带"
        # 一张 card250 DNG 是 12 个条带，一对 24 个；断开时在第 at_close 个
        assert outcomes == ["RenderCancelled"], f"没人等的出图跑到了最后：{outcomes}, 条带 {stopped_at}（断开时 {at_close}）"
        assert stopped_at <= at_close + 3, (stopped_at, at_close)
        assert _stage_entries(app) == 0, "取消的出图进了缓存"

        strips["slow"] = False
        newer = client("wait", tmp_path / "newer.jpg")
        line, _ = newer.communicate(timeout=300)
        got = json.loads(line.strip().splitlines()[-1])
        assert got == {"status": 200, "cache": "miss"}, got
        assert (tmp_path / "newer.jpg").read_bytes() == reference.content
        assert outcomes == ["RenderCancelled", "done"]
    finally:
        server.should_exit = True
        thread.join(timeout=30)
        sock.close()


def test_stage_cache_key_changes_with_every_output_input(settings: Any, lib: Library, monkeypatch: pytest.MonkeyPatch) -> None:
    """不需要样本：`_stage_key` 对每个会改变成片的输入都敏感，同样的输入（新拷贝）得到同一个键；本机配方不缓存。"""
    import gxr_studio
    from gxr_studio.finish import serve
    from gxr_studio.finish.spec import FrameSpec, StampSpec
    from gxr_studio.render import service as service_mod
    from gxr_studio.render.service import RenderService

    render = RenderService(settings.home_dir, lib)
    src = lib.root / "a.dng"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"x" * 10)
    shot = {"shot_id": "s", "files": {"dng": {"relpath": "a.dng", "sha256": "1" * 64}}, "orientation": 1, "pair": {"role": "second"}}
    side = {"rev": 1, "adjust": {"exposure_ev": 0.5}}
    edit = EditState(adjust=Adjust(exposure_ev=0.5))
    plan = (None, FrameSpec(), StampSpec(), edit)
    args = {"stage": "final", "max_edge": None, "quality": 88, "base": "auto"}

    def key(**over: Any) -> str | None:
        a = {**args, **{k: v for k, v in over.items() if k in args}}
        return serve._stage_key(render, lib, over.get("shot", shot), over.get("side", side), over.get("plan", plan), **a)

    base = key()
    assert base is not None
    assert (
        key(
            shot=json.loads(json.dumps(shot)), side=dict(side), plan=(None, FrameSpec(), StampSpec(), EditState(adjust=Adjust(exposure_ev=0.5)))
        )
        == base
    )
    changed = {
        "edit": key(plan=(None, FrameSpec(), StampSpec(), EditState(adjust=Adjust(exposure_ev=1.0)))),
        "recipe": key(plan=(None, FrameSpec(), StampSpec(), dataclasses.replace(edit, recipe=RecipeSel("jlaunch", 3)))),
        "edit revision": key(side={**side, "rev": 2}),
        "default frame": key(plan=(None, dataclasses.replace(FrameSpec(), style="white"), StampSpec(), edit)),
        "default stamp": key(plan=(None, FrameSpec(), dataclasses.replace(StampSpec(), enabled=True), edit)),
        "file sha": key(shot={**shot, "files": {"dng": {"relpath": "a.dng", "sha256": "2" * 64}}}),
        "stage": key(stage="uncropped"),
        "size": key(max_edge=1600),
        "quality": key(quality=90),
        "base": key(base="neutral"),
    }
    src.write_bytes(b"y" * 11)  # 文件换了（大小和修改时间）
    changed["file content"] = key()
    src.write_bytes(b"x" * 10)
    monkeypatch.setattr(render, "data_version", lambda: "other-calib")
    changed["camera data / calibration"] = key()
    monkeypatch.undo()
    monkeypatch.setattr(render, "engine_pin", lambda: "gxr_render/2:other")
    changed["engine pin"] = key()
    monkeypatch.undo()
    monkeypatch.setattr(service_mod, "ENGINE", "gxr_render/next")
    changed["engine version"] = key()
    monkeypatch.undo()
    monkeypatch.setattr(gxr_studio, "__version__", "9.9.9")
    changed["app version"] = key()
    monkeypatch.undo()
    same = [name for name, k in changed.items() if k is None or k == base]
    assert not same, f"缓存键没有随这些输入变化：{same}"
    assert len(set(changed.values())) == len(changed)
    local = dataclasses.replace(edit, recipe=RecipeSel("jlaunch", 1, kind="local", id="r1"))
    assert key(plan=(None, FrameSpec(), StampSpec(), local)) is None, "本机配方的内容不在键里，不能缓存"
