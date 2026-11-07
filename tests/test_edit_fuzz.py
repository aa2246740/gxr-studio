"""裁剪 / 几何的随机交错测试（tests/support/edit_fuzz.mjs；种子固定，失败时带种子、步骤和请求记录，可以原样重现）。

前端（viewer / preview / correct / crop / saves / undo）在 node 里真跑；服务器是真的：这里起一个真服务器（真的路由、sidecar、
收框、校验，合成的假卡里一张 640×480 的照片），node 这一头的「替身」只在请求前加随机延迟、挂住、500、rev_conflict，其余原样转发。
写编辑、写框走 tests/support/fit_checks.py 的包装路由：先调用真正的处理函数，再用真代码检查写下去的框。
每条序列 6–10 步，从旋转、翻转、垂直 / 水平 / 畸变滑块、拉直、自动、点比例、挪框、撤销、重做、重试、等写完里抽
（比例包括原始、自由、1:1、4:5、3:2、16:9、XPan 65:24、1.91:1、小红书 3:4；界面的比例菜单里没有 16:10、2.35:1，所以不抽）。

每条序列等写完（重试到记录清空）以后必须：
- 服务器的几何、框、画布和界面上的一致；状态不是「还在保存」或「有修改没保存」；存下来的框通得过 validate_crop；
- 服务器自己算的居中框只出现在点比例的时候（别的地方不能让服务器编框，用户摆好的框不能被悄悄换掉）；
- 每一次 `fit` 写入：结果落在有效区里、目标中心处放得下同样大的框就必须保留中心、没有比必要的收得更小；
- 串行（每一步开始时上一步已经写完）且没有故障的序列，和「每步等写完」的理想情形几何、比例、框（连大小，1.5 像素以内）都一样；
  有重叠或故障的（不含自动）：几何一样、框的中心在同一处；含自动的有重叠或故障的不比（自动的结果晚到，最后写进去的才算数，几何本来就可以不同）；
- 最后停下来的状态是用户见过的，或者见过的框被服务器收小的结果；写完以后撤销、重做，也只走到这样的状态。

用法：想多跑一些（或换一段种子），设 `GXR_FUZZ_SEEDS=起始:条数`，例如 `GXR_FUZZ_SEEDS=1000:300`。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue

import pytest
import uvicorn
from fastapi.testclient import TestClient

from conftest import PORT
from gxr_studio.app import create_app
from support import fit_checks
from test_api import find, import_all
from test_edit_route import SUPPORT, prepare

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node，跳过前端随机测试")

CHUNK = 20  # 一个 node 进程跑的条数：每条序列都开一张新照片，太多会撑大内存
START, COUNT = (int(x) for x in os.environ.get("GXR_FUZZ_SEEDS", "1:200").split(":"))


@pytest.fixture
def server(settings, card):
    """真服务器 + 测试专用的检查路由；合成的假卡，只听 127.0.0.1。"""
    with TestClient(create_app(settings), base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as c:
        import_all(c)
        # 640×480 的几张：并行的 node 进程各用一张，互不干扰
        photos = [find(c, name) for name in ("R0000102", "R0000103", "R0000104", "R0000106")]
        photo_ids = [p["id"] for p in photos if (p["width"], p["height"]) == (640, 480)]
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    app = create_app(settings.with_(port=port))  # 服务器只认 Host: 127.0.0.1:<自己的端口>
    fit_checks.install(app)
    instance = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    thread = threading.Thread(target=instance.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    for _ in range(200):
        if instance.started:
            break
        time.sleep(0.05)
    assert instance.started, "服务器没有起来"
    yield {"port": port, "token": settings.token, "photos": photo_ids}
    instance.should_exit = True
    thread.join(timeout=20)
    sock.close()


def _run(tmp_path: Path, folder: Path, server: dict, start: int, count: int, trace: bool = False, photo: str | None = None) -> dict:
    env = {"PATH": os.environ.get("PATH", ""), "FUZZ_KEEP": "500", **({"FUZZ_TRACE": "1"} if trace else {})}
    res = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "node",
            "--max-old-space-size=900",
            str(tmp_path / "edit_fuzz.mjs"),
            str(folder),
            str(start),
            str(count),
            str(server["port"]),
            server["token"],
            photo or server["photos"][0],
        ],
        capture_output=True,
        text=True,
        timeout=900,
        encoding="utf-8",
        env=env,
    )
    assert res.stdout.strip(), f"node 没有输出（种子 {start}…{start + count - 1}）：{res.stderr[-1500:]}"
    return json.loads(res.stdout.strip().splitlines()[-1])  # type: ignore[no-any-return]


def test_random_crop_and_geometry_interleavings_end_consistent(tmp_path, server):
    folder = prepare(tmp_path, ("adjust.js", "crop.js", "correct.js"))
    shutil.copy(SUPPORT / "edit_fuzz.mjs", tmp_path / "edit_fuzz.mjs")
    # 每 CHUNK 条一个 node 进程，几个进程并行（各用一张照片，服务器那头是真的多线程）
    chunks = [(first, min(CHUNK, START + COUNT - first)) for first in range(START, START + COUNT, CHUNK)]
    photos = server["photos"]
    free: Queue[str] = Queue()
    for shot in photos:
        free.put(shot)

    def work(chunk: tuple[int, int]) -> dict:
        shot = free.get()  # 一张照片同一时间只给一个进程用
        try:
            return _run(tmp_path, folder, server, chunk[0], chunk[1], photo=shot)
        finally:
            free.put(shot)

    with ThreadPoolExecutor(max_workers=len(photos)) as pool:
        outs = list(pool.map(work, chunks))
    failures: list[dict] = [f for out in outs for f in out["failures"]]
    total = sum(out["sequences"] for out in outs)
    compared = sum(out["serialCompared"] for out in outs)
    failures.sort(key=lambda f: f["seed"])
    assert total == COUNT
    assert compared >= COUNT // 10, f"串行、没有故障的序列只有 {compared} 条，和理想情形的对比几乎没有跑到"
    if failures:
        worst = failures[0]
        traced = _run(tmp_path, folder, server, worst["seed"], 1, trace=True, photo=photos[0])["failures"]
        seeds = [f["seed"] for f in failures][:20]
        lines = [f"{len(failures)} / {total} 条序列不满足不变量。种子（GXR_FUZZ_SEEDS={worst['seed']}:1 可以重现）：{seeds}"]
        for f in failures[:3]:
            history = json.dumps(f.get("history"), ensure_ascii=False)
            lines += [f"seed {f['seed']}", "  步骤：" + " ".join(f["steps"]), *(f"  ! {p}" for p in f["problems"]), "  撤销重做：" + history]
        if traced:
            lines += ["", f"seed {worst['seed']} 的请求记录："] + [f"  {t[:300]}" for t in (traced[0].get("trace") or [])[:120]]
        pytest.fail("\n".join(lines))
