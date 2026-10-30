"""拟合子进程：`python -m gxr_studio.lut.worker <任务目录>`。

任务目录里：
- `job.json`（输入，见 `service.LutService.start_fit`）；
- `progress.json`（子进程不断整体替换）；
- `result.json`（结束时写：`state` = done / error / cancelled，done 时带 `result`）。

取消 = 父进程给它发 SIGTERM（或在任务目录里放一个 `cancel` 文件）；子进程在每次评估之前检查，干净地写下 cancelled。
子进程不联网、不写任务目录以外的地方、不碰相机和卡；相机数据只读（父进程已经核对过）。
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
import traceback
from pathlib import Path


def _write(path: Path, doc: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, default=lambda o: o.tolist() if hasattr(o, "tolist") else str(o)))
    os.replace(tmp, path)


def load_photos(entries: list[dict], max_photos: int = 3) -> tuple[list[dict], list[dict]]:
    """[{"dng": 路径, "label": 名字}, …] → (抽样用的照片, 跳过的照片)。读不出来的跳过，凑够 max_photos 张就停。"""
    from ..raw import RawDecodeError, half_res_codes, open_dng, orient_array
    from ..render.service import shrink_codes
    from .scene import SceneError, scene_colour

    photos: list[dict] = []
    skipped: list[dict] = []
    for p in entries:
        if len(photos) >= max_photos:
            break
        try:
            dng = open_dng(p["dng"])
            try:
                codes = shrink_codes(orient_array(half_res_codes(dng), dng.orientation), 480)
                try:
                    colour = scene_colour(dng)
                except SceneError:
                    colour = None
            finally:
                dng.release()
        except RawDecodeError as exc:  # 读不出来的照片跳过，不让整个拟合失败
            skipped.append({"label": p.get("label"), "reason": str(exc)})
            continue
        photos.append({"codes": codes, "colour": colour, "label": p.get("label")})
    return photos, skipped


PARENT_POLL_SECONDS = 1.0
PARENT_GRACE_SECONDS = 5.0  # 父进程没了之后，给拟合这么久自己干净地停下；还没停就直接退出


def watch_parent(stop: dict, parent_pid: int, poll: float = PARENT_POLL_SECONDS, grace: float = PARENT_GRACE_SECONDS) -> threading.Thread:
    """服务被强杀或终端被关（SIGKILL / SIGHUP）时，子进程不会收到任何信号，只会被系统过继给 1 号进程。
    这里每秒看一眼父进程还在不在：不在了就按"取消"处理，宽限期后仍没停就直接退出（没有人会来读结果了）。"""

    def loop():
        while os.getppid() == parent_pid:
            time.sleep(poll)
        stop["flag"] = True
        time.sleep(grace)
        os._exit(0)

    t = threading.Thread(target=loop, name="parent-watch", daemon=True)
    t.start()
    return t


def run(job_dir: Path) -> int:
    spec = json.loads((job_dir / "job.json").read_text())
    stop = {"flag": False}

    def on_term(*_):
        stop["flag"] = True

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    parent = os.environ.get("GXR_STUDIO_PARENT_PID")
    watch_parent(stop, int(parent) if parent and parent.isdigit() else os.getppid())

    def cancelled() -> bool:
        return stop["flag"] or (job_dir / "cancel").exists()

    started = time.time()

    def progress(p) -> None:
        _write(job_dir / "progress.json", {**p.to_dict(), "elapsed": round(time.time() - started, 1)})

    # 重的 import 放在这里，让信号处理先装好
    from ..camdata import calib
    from ..render.recipes import CameraData
    from .cube import Cube
    from .fit import Cancelled, Fitter
    from .scene import SceneError, build_samples, target_rgb
    from .spaces import Target

    try:
        home = Path(spec["home"])
        cube = Cube.read(spec["cube"])
        cam = CameraData(home)
        model = calib.model_for(home, 200)[0]  # 拟合固定用 ISO 200 的标定
        photos, skipped = load_photos(spec.get("photos", []), spec.get("max_photos", 3))
        samples = build_samples(model, cam.stock[0], spec["input_space"], photos)
        target = Target(
            cube,
            spec["input_space"],
            spec["output_space"],
            spec.get("input_gamut"),
            spec.get("output_gamut"),
            spec.get("input_range", "full"),
            spec.get("output_range", "full"),
        )
        stock, ideal = target_rgb(model, cam.stock[0], samples, target)
        fitter = Fitter(
            cam,
            model,
            samples,
            stock,
            ideal,
            profile=spec.get("profile", "normal"),
            progress=progress,
            cancelled=cancelled,
        )
        result = fitter.run()
        result["samples"]["skipped_photos"] = skipped
        result["target"] = target.description()
        result["cube"] = cube.description()
        _write(job_dir / "result.json", {"state": "done", "result": result})
        return 0
    except Cancelled:
        _write(job_dir / "result.json", {"state": "cancelled"})
        return 0
    except SceneError as exc:
        _write(job_dir / "result.json", {"state": "error", "code": exc.code, "message": exc.message})
        return 1
    except Exception as exc:  # noqa: BLE001  子进程里任何问题都要写成结果文件，父进程才能告诉用户
        _write(
            job_dir / "result.json",
            {
                "state": "error",
                "code": "fit_failed",
                "message": f"拟合没有完成：{type(exc).__name__}: {exc}",
                "trace": traceback.format_exc()[-2000:],
            },
        )
        return 1


if __name__ == "__main__":
    sys.exit(run(Path(sys.argv[1])))
