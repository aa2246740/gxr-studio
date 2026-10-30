"""LUT 导入的服务层：保存 .cube、启动/查看/取消拟合子进程、"我的配方"（电脑端）的增删改查、LUT 预览图。

数据都放在应用数据目录（`home_dir`）里，不写图库、不写卡：
    luts/<sha256>.cube          用户拖进来的调色文件（原样字节）
    lut_jobs/<job>/             一次拟合：job.json、progress.json、result.json、log.txt
    my_recipes/<id>.json        "我的配方"：底子 + 旋钮档位 + 来源与相似度（电脑预测）。写回相机在后续任务里做
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess  # nosec B404  只用来拉起本包的拟合子进程
import sys
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, cast

import numpy as np
from PIL import Image

from ..export import srgb_icc
from ..raw import open_dng
from ..render.params import EditError, RecipeSel
from ..render.service import RenderUnavailable
from . import cube as cubemod
from .cube import Cube
from .scene import SceneError, scene_colour
from .spaces import DISPLAY, GAMUT, SPACES, Target

SCHEMA_RECIPE = "gxr-studio/my-recipe/1"
JOB_KEEP = 20
NAME_MAX = 40

SPACE_LABELS = {
    "srgb": ("普通照片 / 网页颜色（sRGB）", "display"),
    "rec709": ("视频显示（Rec.709）", "display"),
    "gamma24": ("视频显示（Gamma 2.4）", "display"),
    "flog": ("富士 F-Log", "log"),
    "flog2": ("富士 F-Log2", "log"),
    "llog": ("徕卡 L-Log", "log"),
    "llog709": ("徕卡 L-Log（Rec.709 色域）", "log"),
    "vlog": ("松下 V-Log", "log"),
    "slog3": ("索尼 S-Log3", "log"),
    "slog3-sgamut3": ("索尼 S-Log3 / S-Gamut3", "log"),
    "applelog": ("苹果 Log", "log"),
    "linear": ("线性光（高级）", "log"),
}
# 文件名/标题里的关键词 → 输入空间（先长后短）
GUESS = (
    ("f-log2", "flog2"),
    ("flog2", "flog2"),
    ("f-log", "flog"),
    ("flog", "flog"),
    ("l-log", "llog"),
    ("llog", "llog"),
    ("v-log", "vlog"),
    ("vlog", "vlog"),
    ("s-log3", "slog3"),
    ("slog3", "slog3"),
    ("apple log", "applelog"),
    ("applelog", "applelog"),
    ("srgb", "srgb"),
    ("rec709", "rec709"),
    ("rec.709", "rec709"),
    ("709", "rec709"),
)


class LutError(Exception):
    def __init__(self, status: int, code: str, message: str, extra: dict[Any, Any] | None = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra or {}


def spaces_listing() -> list[dict[Any, Any]]:
    assert set(SPACE_LABELS) == set(SPACES), "界面的色彩空间清单和 spaces.py 不一致"  # nosec B101  两份清单必须一起改
    return [{"id": k, "label": SPACE_LABELS[k][0], "kind": SPACE_LABELS[k][1], "gamut": GAMUT[k]} for k in SPACES]


def guess_space(name: str, title: str = "") -> tuple[str | None, str | None]:
    text = f"{name} {title}".lower()
    for key, space in GUESS:
        if key in text:
            return space, f"文件名/标题里有“{key}”"
    return None, None


def clean_name(name: Any) -> str:
    if not isinstance(name, str):
        raise LutError(422, "bad_name", "无法保存。名字无效。")
    name = re.sub(r"\s+", " ", name).strip()
    if not 1 <= len(name) <= NAME_MAX or any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise LutError(422, "bad_name", "无法保存。名字长度不合适。", {"detail": f"1 到 {NAME_MAX} 个字"})
    return cast(str, name)  # 上面已经确认是 str；mypy 不把这次赋值从 Any 收窄


def _write_json(path: Path, doc: dict[Any, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(doc, ensure_ascii=False, indent=2, default=lambda o: o.tolist() if hasattr(o, "tolist") else str(o)))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class LutService:
    def __init__(self, home: Path, library: Any, render: Any) -> None:
        self.home = Path(home)
        self.lib = library
        self.render = render
        self._lock = threading.RLock()
        self._procs: dict[str, subprocess.Popen[Any]] = {}
        self._cancelled: set[str] = set()
        self._cubes: OrderedDict[str, Cube] = OrderedDict()
        self._colours: dict[Any, Any] = {}

    # ---- 目录 ---------------------------------------------------------------
    @property
    def luts_dir(self) -> Path:
        return self.home / "luts"

    @property
    def jobs_dir(self) -> Path:
        return self.home / "lut_jobs"

    @property
    def recipes_dir(self) -> Path:
        return self.home / "my_recipes"

    # ---- 调色文件 -------------------------------------------------------------
    def add_cube(self, raw: bytes, filename: str = "") -> dict[Any, Any]:
        if len(raw) > cubemod.MAX_BYTES:
            raise LutError(422, "cube_too_big", "无法读取 LUT。文件过大。", {"detail": "超过 64 MB"})
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise LutError(422, "cube_unreadable", "无法读取 LUT。请改用 .cube 文件。") from exc
        digest = hashlib.sha256(raw).hexdigest()
        try:
            cube = Cube.parse(text, digest)
        except (ValueError, IndexError) as exc:
            raise LutError(422, "cube_unreadable", "无法读取 LUT。请改用 .cube 文件。", {"detail": str(exc)}) from exc
        if cube.table3 is None and cube.table1 is None:
            raise LutError(422, "cube_unsupported", "无法读取 LUT。请改用 3D 的 .cube 文件。")
        self.luts_dir.mkdir(parents=True, exist_ok=True)
        path = self.luts_dir / f"{digest}.cube"
        if not path.exists():
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_bytes(raw)
            os.replace(tmp, path)
        stored_name = Path(filename).name
        if stored_name:
            (self.luts_dir / f"{digest}.filename").write_text(stored_name + "\n", encoding="utf-8")
        guess, why = guess_space(stored_name, cube.title)
        return {
            "cube_id": digest,
            **self._describe(cube),
            "filename": Path(filename).name or None,
            "input_space_guess": guess,
            "guess_reason": why,
            "guess_note": None
            if guess
            else "文件里没有写它是给哪种画面做的，需要你来选；不知道的话可以选 sRGB 试试，结果会标“输入空间是猜的”。",
        }

    def _describe(self, cube: Cube) -> dict[Any, Any]:
        kinds = [k for k, t in (("1D", cube.table1), ("3D", cube.table3)) if t is not None]
        return {
            "title": cube.title or None,
            "kind": "+".join(kinds),
            "size_1d": 0 if cube.table1 is None else len(cube.table1),
            "size_3d": 0 if cube.table3 is None else len(cube.table3),
            "domain_min": [round(float(v), 6) for v in cube.domain_min],
            "domain_max": [round(float(v), 6) for v in cube.domain_max],
        }

    def cube(self, cube_id: str) -> Cube:
        if not re.fullmatch(r"[0-9a-f]{64}", cube_id or ""):
            raise LutError(404, "cube_not_found", "找不到这个 LUT。请重新拖进来。")
        with self._lock:
            hit = self._cubes.get(cube_id)
            if hit is not None:
                self._cubes.move_to_end(cube_id)
                return hit
        path = self.luts_dir / f"{cube_id}.cube"
        if not path.is_file():
            raise LutError(404, "cube_not_found", "找不到这个 LUT。请重新拖进来。")
        cube = Cube.read(path)
        if cube.source_sha256 != cube_id:
            raise LutError(409, "cube_changed", "这个 LUT 已改变。请重新拖进来。")
        with self._lock:
            self._cubes[cube_id] = cube
            while len(self._cubes) > 4:
                self._cubes.popitem(last=False)
        return cube

    def target(
        self,
        cube: Cube,
        space: str,
        output: str = "srgb",
        *,
        input_gamut: Any = None,
        output_gamut: Any = None,
        input_range: str = "full",
        output_range: str = "full",
    ) -> Target:
        if space not in SPACES:
            raise LutError(422, "bad_space", "请求无效。请刷新页面后重试。", {"detail": f"输入色彩空间：{space}"})
        if output not in SPACES:
            raise LutError(422, "bad_space", "请求无效。请刷新页面后重试。", {"detail": f"输出色彩空间：{output}"})
        for g in (input_gamut, output_gamut):
            if g is not None and g not in ("rec709", "rec2020", "vgamut", "sgamut3", "sgamut3cine"):
                raise LutError(422, "bad_gamut", "请求无效。请刷新页面后重试。", {"detail": f"色域：{g}"})
        for r in (input_range, output_range):
            if r not in ("full", "legal"):
                raise LutError(422, "bad_range", "请求无效。请刷新页面后重试。", {"detail": "范围只能是 full 或 legal"})
        return Target(cube, space, output, input_gamut, output_gamut, input_range, output_range)

    # ---- 预览 -----------------------------------------------------------------
    def _chart(self, w: int = 480, h: int = 320) -> np.ndarray:
        """没有照片时的示例照片：上面一条灰阶，下面是色相 × 明度的色块（sRGB 显示域，0..255）。"""
        img = np.zeros((h, w, 3))
        gh = h // 5
        img[:gh] = np.linspace(0, 255, w)[None, :, None]
        hh = h - gh
        hue = np.linspace(0, 1, w, endpoint=False)[None, :]
        val = np.linspace(1.0, 0.25, hh)[:, None]
        # 饱和度 0.7 的 HSV → RGB
        channel = [np.clip(np.abs(((hue * 6 + off) % 6) - 3) - 1, 0, 1) for off in (0, 4, 2)]
        rgb = np.stack([np.broadcast_to(c, (hh, w)) for c in channel], axis=-1)
        img[gh:] = 255 * val[..., None] * (0.3 + 0.7 * rgb)
        return img

    def _photo_base(self, shot: dict[Any, Any] | None, max_edge: int) -> tuple[Any, Any, str]:
        """(基准 sRGB 0..255 的 (h,w,3) 浮点, 场景 XYZ 或 None, 模式说明)。"""
        if shot is None:
            return self._chart(), None, "chart"
        if "dng" in shot["files"]:
            dng_rec = shot["files"]["dng"]
            try:
                codes = self.render.proxy_codes(shot, max_edge)
            except Exception as exc:  # noqa: BLE001
                raise LutError(422, "raw_unreadable", "无法读取这张照片。文件可能已损坏。", {"detail": str(exc)}) from exc
            colour = self._colour_of(dng_rec)
            xyz = None if colour is None else colour.xyz(codes.reshape(-1, 3)).reshape(codes.shape)
            try:
                base = self.render.default_look(shot, codes)
                if base is not None:
                    return base.astype(float), xyz, "04-simulation"
            except RenderUnavailable:
                pass
            from ..raw.render import NEUTRAL

            return NEUTRAL.display(codes).astype(float), xyz, "neutral"
        if "jpg" in shot["files"]:
            im = Image.open(self.lib.abspath(shot["files"]["jpg"]["relpath"]))
            im.draft("RGB", (max_edge * 2, max_edge * 2))
            from PIL import ImageOps

            im = ImageOps.exif_transpose(im).convert("RGB")  # type: ignore[assignment]
            im.thumbnail((max_edge, max_edge))
            return np.asarray(im, float), None, "camera-jpg"
        raise LutError(422, "no_image", "无法读取这张照片。没有可用的图像文件。")

    def _colour_of(self, dng_rec: dict[Any, Any]) -> Any:
        sha = dng_rec["sha256"]
        if sha not in self._colours:
            dng = open_dng(self.lib.abspath(dng_rec["relpath"]))
            try:
                self._colours[sha] = scene_colour(dng)
            except SceneError:
                self._colours[sha] = None
            finally:
                dng.release()
        return self._colours[sha]

    def preview(
        self, cube_id: str, which: str, spec: dict[Any, Any], shot: dict[Any, Any] | None, max_edge: int = 640
    ) -> tuple[bytes, dict[Any, Any]]:
        """which = original | lut。original 是"04 电脑模拟"（没有相机数据时是中性转换），lut 是套上 LUT 后的理想效果。"""
        if which not in ("original", "lut"):
            raise LutError(422, "bad_which", "请求无效。请刷新页面后重试。", {"detail": "which 只能是 original 或 lut"})
        cube = self.cube(cube_id)
        target = self.target(cube, **spec)
        base, xyz, mode = self._photo_base(shot, max_edge)
        info: dict[str, Any] = {"mode": mode}
        if which == "lut":
            flat = base.reshape(-1, 3)
            if xyz is None and spec["space"] not in DISPLAY:
                # 没有场景 XYZ：把显示画面当成线性的 Rec.709 场景光（示意，不是真的 Log 换算）
                from ..engine.colour import M709, srgb_to_linear

                xyz = srgb_to_linear(flat / 255.0) @ M709.T
                info["nominal_scene"] = True
            try:
                out = target.render(flat, None if xyz is None else xyz.reshape(-1, 3)).reshape(base.shape)
            except ValueError as exc:
                raise LutError(422, "lut_failed", "无法套用 LUT。请重试。", {"detail": str(exc)}) from exc
        else:
            out = base
        rgb = np.floor(np.clip(out, 0, 255) + 0.5).astype(np.uint8)
        buf = io.BytesIO()
        Image.fromarray(rgb, "RGB").save(buf, "JPEG", quality=88, icc_profile=srgb_icc())
        return buf.getvalue(), info

    # ---- 拟合任务 ---------------------------------------------------------------
    def pick_photos(self, ids: list[str] | None = None, limit: int = 8) -> list[dict[Any, Any]]:
        """图库里的 DNG 照片候选：优先 ISO 200，其次 1600，再按拍摄时间新的在前。子进程从前面开始取读得出来的 3 张，最后一张只验收。"""
        if ids is not None:
            shots = [self.lib.get_shot(i) for i in ids]
            if any(s is None for s in shots):
                raise LutError(404, "photo_not_found", "找不到这张照片。请刷新页面后重试。")
            shots = [s for s in shots if "dng" in s["files"]]
        else:
            shots = [s for s in self.lib.list_shots(limit=2000) if "dng" in s["files"]]
            shots.sort(key=lambda s: (s["captured_at"] or "", s["name"]), reverse=True)  # 新的在前
            shots.sort(key=lambda s: 0 if s["iso"] == 200 else 1 if s["iso"] == 1600 else 2)  # 稳定排序：ISO 优先
        out = []
        for s in shots[:limit]:
            f = s["files"]["dng"]
            out.append(
                {
                    "dng": str(self.lib.abspath(f["relpath"])),
                    "sha256": f["sha256"],
                    "label": s["name"],
                    "iso": s["iso"],
                    "shot_id": s["shot_id"],
                }
            )
        return out

    def start_fit(
        self,
        cube_id: str,
        spec: dict[Any, Any],
        *,
        unit: str = "jlaunch",
        photo_ids: list[str] | None = None,
        profile: str = "normal",
        guessed: bool = False,
    ) -> dict[Any, Any]:
        if unit != "jlaunch":
            raise LutError(
                422,
                "unit_not_calibrated",
                "28mm 和 MOUNT 还没有单独的校准数据，暂时不能导入 LUT。50mm 可以。",
            )
        if profile not in ("normal", "quick"):
            raise LutError(422, "bad_profile", "请求无效。请刷新页面后重试。", {"detail": "profile 只能是 normal 或 quick"})
        cube = self.cube(cube_id)
        self.target(cube, **spec)  # 先校验色彩空间参数
        guessed = guessed or self._filename_guess_matches(cube_id, spec["space"], cube)
        if not self.render.available():
            st = self.render.status()
            raise LutError(409, "camera_data_missing", st["hint"] or "请先导入相机配方文件", {"status": st})
        with self._lock:
            busy = next((j for j, p in self._procs.items() if p.poll() is None), None)
            if busy is not None:
                raise LutError(409, "fit_busy", "无法生成配方。已经有一个在进行。", {"job_id": busy})
            photos = self.pick_photos(photo_ids)
            job_id = uuid.uuid4().hex[:12]
            job_dir = self.jobs_dir / job_id
            job_dir.mkdir(parents=True)
            job = {
                "home": str(self.home),
                "cube": str(self.luts_dir / f"{cube_id}.cube"),
                "cube_id": cube_id,
                "input_space": spec["space"],
                "output_space": spec["output"],
                "input_gamut": spec.get("input_gamut"),
                "output_gamut": spec.get("output_gamut"),
                "input_range": spec.get("input_range", "full"),
                "output_range": spec.get("output_range", "full"),
                "photos": photos,
                "max_photos": 3,
                "profile": profile,
                "space_guessed": guessed,
                "unit": unit,
                "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
            _write_json(job_dir / "job.json", job)
            env = {
                **os.environ,
                "OPENBLAS_NUM_THREADS": "1",
                "OMP_NUM_THREADS": "1",
                "VECLIB_MAXIMUM_THREADS": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "GXR_STUDIO_PARENT_PID": str(os.getpid()),
            }  # 子进程据此发现服务没了（SIGKILL / SIGHUP）
            log = open(job_dir / "log.txt", "wb")
            proc = subprocess.Popen(  # nosec B603  参数是解释器和本包模块，没有 shell
                [sys.executable, "-m", "gxr_studio.lut.worker", str(job_dir)],
                stdout=log,
                stderr=log,
                stdin=subprocess.DEVNULL,
                env=env,
                start_new_session=True,
            )
            log.close()
            self._procs[job_id] = proc
            self._prune()
        return {"job_id": job_id, "photos": [{k: p[k] for k in ("label", "iso")} for p in photos]}

    def _job_dir(self, job_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{12}", job_id or "") or not (self.jobs_dir / job_id / "job.json").is_file():
            raise LutError(404, "fit_not_found", "找不到这次生成。请刷新页面后重试。")
        return self.jobs_dir / job_id

    def _read(self, path: Path) -> Any:
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None

    def status(self, job_id: str, *, with_result: bool = True) -> dict[Any, Any]:
        d = self._job_dir(job_id)
        job = self._read(d / "job.json")
        proc = self._procs.get(job_id)
        result = self._read(d / "result.json")
        progress = self._read(d / "progress.json")
        if job_id in self._cancelled:
            state = "cancelled"
        elif result is not None:
            state = result["state"]
        elif proc is not None and proc.poll() is None:
            state = "running"
        elif proc is not None:
            state, result = "error", {"code": "worker_died", "message": "无法生成配方。请重试。"}
        else:
            state, result = "error", {"code": "interrupted", "message": "无法生成配方。请重新开始。"}
        out = {
            "job_id": job_id,
            "state": state,
            "progress": progress,
            "input": {k: job[k] for k in ("input_space", "output_space", "profile", "space_guessed")},
            "cube_id": job["cube_id"],
            "photos": [{"label": p["label"], "iso": p["iso"]} for p in job["photos"]],
        }
        if state == "error":
            out["error"] = {"code": result.get("code"), "message": result.get("message")}
        if state == "done" and with_result:
            out["result"] = result["result"]
        return out

    def cancel(self, job_id: str) -> dict[Any, Any]:
        d = self._job_dir(job_id)
        proc = self._procs.get(job_id)
        if proc is not None and proc.poll() is None:
            (d / "cancel").write_text("1")
            proc.terminate()
            try:
                proc.wait(3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(5)
            if not (d / "result.json").exists():
                _write_json(d / "result.json", {"state": "cancelled"})
            self._cancelled.add(job_id)
        return self.status(job_id, with_result=False)

    def close(self) -> None:
        """应用关闭时：还在跑的拟合一并结束，不留孤儿进程。"""
        for job_id, proc in list(self._procs.items()):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(5)

    def _prune(self) -> None:
        if not self.jobs_dir.is_dir():
            return
        dirs = sorted((p for p in self.jobs_dir.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime)
        for p in dirs[:-JOB_KEEP]:
            if p.name not in self._procs or self._procs[p.name].poll() is not None:
                shutil.rmtree(p, ignore_errors=True)

    # ---- 我的配方 -----------------------------------------------------------------
    def _stored_filename(self, cube_id: str) -> str:
        path = self.luts_dir / f"{cube_id}.filename"
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8").strip()

    def _filename_guess_matches(self, cube_id: str, space: str, cube: Cube | None = None) -> bool:
        """选中的输入空间就是按文件名或标题猜出来的那一个。"""
        loaded = cube if cube is not None else self.cube(cube_id)
        guess, _why = guess_space(self._stored_filename(cube_id), loaded.title or "")
        return guess is not None and guess == space

    def _recipe_path(self, rid: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{12}", rid or ""):
            raise LutError(404, "recipe_not_found", "找不到这个配方。请刷新页面后重试。")
        return self.recipes_dir / f"{rid}.json"

    def save_recipe(self, job_id: str, candidate: int, name: str) -> dict[Any, Any]:
        name = clean_name(name)
        st = self.status(job_id)
        if st["state"] != "done":
            raise LutError(409, "fit_not_done", "无法保存配方。请等生成完成后再试。")
        cands = st["result"]["candidates"]
        if not isinstance(candidate, int) or isinstance(candidate, bool) or not 0 <= candidate < len(cands):
            raise LutError(422, "bad_candidate", "请求无效。请刷新页面后重试。", {"detail": "没有这个候选"})
        c = cands[candidate]
        sel = RecipeSel(c["unit"], c["slot"], tuple(sorted(c["levels"].items())))
        try:  # 再核对一遍：每个档位都必须是菜单里合法的
            self.render.menu.request_for(sel)
        except EditError as exc:
            raise LutError(422, exc.code, exc.message) from exc
        rid = uuid.uuid4().hex[:12]
        existing = len(self.list_recipes())
        doc = {
            "schema": SCHEMA_RECIPE,
            "id": rid,
            "name": name,
            "camera_short_name": f"U{existing + 1:02d}-{c['slot']}{c['entry_sha256'][:3].upper()}",
            "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "recipe": {"kind": "builtin", "unit": c["unit"], "slot": c["slot"], "levels": dict(c["levels"])},
            "source": {
                "kind": "lut",
                "cube_sha256": st["cube_id"],
                "title": st["result"].get("cube", {}).get("title"),
                "input_space": st["input"]["input_space"],
                "output_space": st["input"]["output_space"],
                "input_space_guessed": bool(st["input"]["space_guessed"])
                or self._filename_guess_matches(st["cube_id"], st["input"]["input_space"]),
                "fit_job": job_id,
            },
            "fit": {
                "prediction": True,
                "label": "电脑预测",
                "similarity": c["similarity"],
                "similarity_label": c["label"],
                "holdout": c["holdout"],
                "baseline04_holdout": c["baseline04_holdout"],
                "differences": c["differences"],
                "entry_sha256": c["entry_sha256"],
                "formula": st["result"]["formula"],
            },
            "camera_write": {
                "status": "not_supported_yet",
                "note": "暂不支持写入存储卡",
            },
        }
        _write_json(self._recipe_path(rid), doc)
        return doc

    def list_recipes(self) -> list[dict[Any, Any]]:
        if not self.recipes_dir.is_dir():
            return []
        out = []
        for p in self.recipes_dir.glob("*.json"):
            doc = self._read(p)
            if isinstance(doc, dict) and doc.get("schema") == SCHEMA_RECIPE:
                out.append(doc)
        out.sort(key=lambda d: (d["created"], d["id"]))
        return out

    def get_recipe(self, rid: str) -> dict[Any, Any]:
        p = self._recipe_path(rid)
        doc = self._read(p) if p.is_file() else None
        if not isinstance(doc, dict):
            raise LutError(404, "recipe_not_found", "找不到这个配方。请刷新页面后重试。")
        return doc

    def rename_recipe(self, rid: str, name: str) -> dict[Any, Any]:
        doc = self.get_recipe(rid)
        doc["name"] = clean_name(name)
        _write_json(self._recipe_path(rid), doc)
        return doc

    def delete_recipe(self, rid: str) -> None:
        self.get_recipe(rid)
        self._recipe_path(rid).unlink()
