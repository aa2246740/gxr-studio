"""同一个成片键在算的时候又来一个请求：等那一次，不另算（UNIT join-render）。

真 uvicorn、真 TCP，出图换成一个可控的慢步骤（`finish.serve._stage_rgb`，带安全点和计数器），用合成假卡，不需要样本：
- 两个同样的请求同时到：只算一次，两边字节一样（后到的标 `X-GXR-Cache: joined`）；
- 先到的客户端断开、后到的还在等：接着算完，后到的拿到字节，进缓存；
- 两个都断开：在安全点停下，不进缓存；
- 不同的键各算各的；
- 出图出错：两个请求都拿到同样的错误。
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import uvicorn

from gxr_studio.app import create_app
from gxr_studio.cards.discovery import find_cards
from gxr_studio.cards.importer import ImportOptions, run_import
from gxr_studio.library.store import Library
from gxr_studio.render.params import EditError

FULL = "/api/photos/{}/render?stage=final&size=full"


def _body(ev: float) -> str:
    body = {
        "recipe": None,
        "lut": None,
        "adjust": {"exposure_ev": ev},
        "curve": None,
        "geometry": None,
        "frame": {"style": "none"},
        "stamp": {"enabled": False},
    }
    return json.dumps(body)


def _wait(pred: Any, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.02)
    raise AssertionError(f"timeout waiting for {what}")


class FakeStage:
    """代替真正的出图：每 0.02 秒一个安全点，等 `release` 才交图（或 `fail` 时抛 EditError）。字节由曝光决定。"""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.fail = False
        self.started = 0
        self.steps = 0
        self.outcomes: list[str] = []
        self._lock = threading.Lock()

    def __call__(self, render: Any, lib: Any, shot: Any, side: Any, plan: Any, **kw: Any) -> tuple[np.ndarray, dict[str, str]]:
        from gxr_studio.render.cancel import checkpoint

        with self._lock:
            self.started += 1
        try:
            deadline = time.monotonic() + 60
            while not self.release.is_set() and time.monotonic() < deadline:
                checkpoint()
                self.steps += 1
                time.sleep(0.02)
            if self.fail:
                raise EditError("bad_adjust", "这份编辑算不出来。")
            level = int(round(plan[3].adjust.exposure_ev * 100)) % 256
            rgb = np.full((48, 64, 3), level, dtype=np.uint8)
            rgb[::7, ::5] = 255 - level
        except Exception as exc:
            self.outcomes.append(type(exc).__name__)
            raise
        self.outcomes.append("done")
        return rgb, {"kind": "prediction", "label": "prediction", "text": "预测", "id": "fake", "calibrated": "0"}


class Studio:
    def __init__(self, app: Any, port: int, token: str, shot_id: str, fake: FakeStage, tmp: Path) -> None:
        self.app, self.port, self.token, self.shot_id, self.fake, self.tmp = app, port, token, shot_id, fake, tmp
        self.entered = 0  # 进了 render_stage 的请求数

    def stage_entries(self) -> int:
        return sum(1 for k in list(self.app.state.ctx.render._results._d) if isinstance(k, tuple) and k[:1] == ("stage",))


_CLIENT = r"""
import http.client, json, os, socket, sys, threading
port, path, token, body, out_file = sys.argv[1:6]
conn = http.client.HTTPConnection("127.0.0.1", int(port), timeout=120)
conn.request("POST", path, body.encode(), {"X-GXR-Token": token, "Content-Type": "application/json"})
print("sent", flush=True)

def drop():
    if sys.stdin.readline().strip() == "drop":
        conn.sock.shutdown(socket.SHUT_RDWR)
        conn.close()
        print("closed", flush=True)
        os._exit(0)

threading.Thread(target=drop, daemon=True).start()
res = conn.getresponse()
data = res.read()
open(out_file, "wb").write(data)
print(json.dumps({"status": res.status, "headers": {k.lower(): v for k, v in res.getheaders()}}), flush=True)
os._exit(0)
"""


class Call:
    """一个 HTTP 客户端（单独的进程：测试进程里不许连网）；`drop()` 像浏览器刷新那样关掉连接。"""

    _n = 0

    def __init__(self, studio: Studio, ev: float) -> None:
        Call._n += 1
        self.out = studio.tmp / f"call{Call._n}.bin"
        args = [sys.executable, "-I", "-c", _CLIENT, str(studio.port), FULL.format(studio.shot_id), studio.token, _body(ev), str(self.out)]
        self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        assert self.proc.stdout is not None and self.proc.stdout.readline().strip() == "sent"

    def drop(self) -> None:
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.proc.stdin.write("drop\n")
        self.proc.stdin.flush()
        assert self.proc.stdout.readline().strip() == "closed"
        self.proc.communicate(timeout=30)

    def get(self) -> tuple[int, dict[str, str], bytes]:
        line, _ = self.proc.communicate(timeout=120)
        got = json.loads(line.strip().splitlines()[-1])
        return got["status"], got["headers"], self.out.read_bytes()


@pytest.fixture
def studio(settings: Any, lib: Library, volumes: Path, card: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Studio]:
    from gxr_studio.finish import serve

    run_import(lib, find_cards((volumes,))[0], ImportOptions(min_free_bytes=0))
    shot_id = next(s["shot_id"] for s in lib.list_shots() if "jpg" in s["files"] and not s.get("pair"))
    lib.close()
    fake = FakeStage()
    monkeypatch.setattr(serve, "_stage_rgb", fake)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    app = create_app(settings.with_(port=port))
    st = Studio(app, port, settings.token, shot_id, fake, tmp_path)
    real_stage = serve.render_stage

    def counted(*args: Any, **kwargs: Any) -> Any:
        st.entered += 1
        return real_stage(*args, **kwargs)

    monkeypatch.setattr(serve, "render_stage", counted)
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        _wait(lambda: server.started, 10, "server start")
        yield st
    finally:
        fake.release.set()
        server.should_exit = True
        thread.join(timeout=30)
        sock.close()


def _second_arrives(st: Studio, entered: int) -> None:
    """等第 `entered` 个请求进了 render_stage，再留一点时间给它算键、挂到在算的那一次上（或者在旧代码里自己开算）。"""
    _wait(lambda: st.entered >= entered, 10, f"request {entered} to reach render_stage")
    time.sleep(0.4)


def test_two_identical_concurrent_requests_run_one_render_with_equal_bytes(studio: Studio) -> None:
    a = Call(studio, 0.5)
    _wait(lambda: studio.fake.started == 1, 10, "first render to start")
    b = Call(studio, 0.5)
    _second_arrives(studio, 2)
    studio.fake.release.set()
    sa, ha, ba = a.get()
    sb, hb, bb = b.get()
    assert (sa, sb) == (200, 200)
    assert studio.fake.started == 1, f"同样的请求算了 {studio.fake.started} 次"
    assert ba == bb and ba[:2] == b"\xff\xd8"
    assert ha["x-gxr-cache"] == "miss" and hb["x-gxr-cache"] == "joined"
    for name in ("content-type", "x-gxr-label", "x-gxr-calibrated", "x-gxr-prediction", "cache-control"):
        assert ha.get(name) == hb.get(name), name
    assert studio.stage_entries() == 1


def test_first_client_leaves_while_second_waits_render_continues(studio: Studio) -> None:
    a = Call(studio, 0.5)
    _wait(lambda: studio.fake.started == 1, 10, "first render to start")
    b = Call(studio, 0.5)
    _second_arrives(studio, 2)
    a.drop()
    time.sleep(1.0)  # 好几次询问"客户端还在吗"
    assert studio.fake.outcomes == [], f"还有人等的出图停下了：{studio.fake.outcomes}"
    studio.fake.release.set()
    status, headers, data = b.get()
    assert status == 200 and data[:2] == b"\xff\xd8"
    assert studio.fake.started == 1, f"后到的请求从头又算了一遍（算了 {studio.fake.started} 次）"
    assert studio.fake.outcomes == ["done"]
    assert headers["x-gxr-cache"] == "joined"
    assert studio.stage_entries() == 1
    again = Call(studio, 0.5).get()
    assert again[0] == 200 and again[1]["x-gxr-cache"] == "hit" and again[2] == data


def test_both_clients_leave_render_stops_and_is_not_cached(studio: Studio) -> None:
    a = Call(studio, 0.5)
    _wait(lambda: studio.fake.started == 1, 10, "first render to start")
    b = Call(studio, 0.5)
    _second_arrives(studio, 2)
    assert studio.fake.started == 1, f"同样的请求算了 {studio.fake.started} 次"
    a.drop()
    b.drop()
    _wait(lambda: bool(studio.fake.outcomes), 10, "abandoned render to stop")
    stopped = studio.fake.steps
    time.sleep(0.5)
    assert studio.fake.outcomes == ["RenderCancelled"]
    assert studio.fake.steps == stopped, "没人等了还在算"
    assert studio.stage_entries() == 0, "取消的出图进了缓存"
    # 之后同样的请求照常从头算
    studio.fake.release.set()
    later = Call(studio, 0.5).get()
    assert later[0] == 200 and later[1]["x-gxr-cache"] == "miss"
    assert studio.fake.started == 2


def test_different_key_runs_separately_while_identical_one_joins(studio: Studio) -> None:
    a = Call(studio, 0.5)
    _wait(lambda: studio.fake.started == 1, 10, "first render to start")
    b = Call(studio, 0.5)
    c = Call(studio, 1.0)
    _second_arrives(studio, 3)
    _wait(lambda: studio.fake.started >= 2, 10, "the other key's render to start")
    studio.fake.release.set()
    ra, rb, rc = a.get(), b.get(), c.get()
    assert (ra[0], rb[0], rc[0]) == (200, 200, 200)
    assert studio.fake.started == 2, f"两个键应该各算一次，实际算了 {studio.fake.started} 次"
    assert ra[2] == rb[2] and rc[2] != ra[2]
    assert (ra[1]["x-gxr-cache"], rb[1]["x-gxr-cache"], rc[1]["x-gxr-cache"]) == ("miss", "joined", "miss")
    assert studio.stage_entries() == 2


def test_render_error_reaches_every_waiter(studio: Studio) -> None:
    studio.fake.fail = True
    a = Call(studio, 0.5)
    _wait(lambda: studio.fake.started == 1, 10, "first render to start")
    b = Call(studio, 0.5)
    _second_arrives(studio, 2)
    studio.fake.release.set()
    ra, rb = a.get(), b.get()
    assert studio.fake.started == 1, f"同样的请求算了 {studio.fake.started} 次"
    assert ra[0] == rb[0] == 422
    assert json.loads(ra[2]) == json.loads(rb[2]) == {"error": "bad_adjust", "message": "这份编辑算不出来。"}
    assert studio.stage_entries() == 0
    # 出错的那一次不挡后面的请求
    studio.fake.fail = False
    later = Call(studio, 0.5).get()
    assert later[0] == 200 and studio.fake.started == 2
