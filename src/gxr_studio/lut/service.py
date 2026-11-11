"""LUT 导入的服务层：保存 .cube、启动/查看/取消拟合子进程、"我的配方"（电脑端）的增删改查、LUT 预览图。

数据都放在应用数据目录（`home_dir`）里，不写图库、不写卡：
    luts/<sha256>.cube          用户拖进来的调色文件（原样字节）
    lut_jobs/<job>/             一次拟合：job.json、progress.json、result.json
    recipes/                   完整记录、冻结模型、质量证据和名称，RecipeRepository唯一写入
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess  # nosec B404  只用来拉起本包的拟合子进程
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, cast

import numpy as np
from PIL import Image

from ..alerts import StudioNotifier, on_job_failed
from ..export import srgb_icc
from ..fit_contexts import FitContexts
from ..jobs import Job
from ..raw import open_dng
from ..recipes import FitProduct, Quality, RecipeError, RecipeRepository, RenderEnvironment, check_record
from ..recipes.admission import verify_archived_proof
from ..recipes.fit import Winner, default_plan
from ..recipes.types import canonical, digest
from ..render.edits import read as read_edit
from ..render.params import EditError, EditState
from ..render.service import RenderUnavailable
from ..tracking import scrub_text
from .cube import Cube
from .errors import LutError as LutError
from .library import LutLibrary
from .metadata import FIELDS
from .page import input_choice
from .scene import SceneError, scene_colour
from .spaces import DISPLAY, GAMUT, SPACES, Target

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


def spaces_listing() -> list[dict[Any, Any]]:
    assert set(SPACE_LABELS) == set(SPACES), "界面的色彩空间清单和 spaces.py 不一致"  # nosec B101  两份清单必须一起改
    return [{"id": k, "label": SPACE_LABELS[k][0], "kind": SPACE_LABELS[k][1], "gamut": GAMUT[k]} for k in SPACES]


def clean_name(name: Any) -> str:
    if not isinstance(name, str):
        raise LutError(422, "bad_name", "无法保存。名称无效。")
    name = re.sub(r"\s+", " ", name).strip()
    if not 1 <= len(name) <= NAME_MAX:
        raise LutError(422, "bad_name", f"配方名称需为 1–{NAME_MAX} 个字符。")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise LutError(422, "bad_name", "无法保存。名称无效。")
    return cast(str, name)  # 上面已经确认是 str；mypy 不把这次赋值从 Any 收窄


class LutService:
    def __init__(self, home: Path, library: Any, render: Any, *, notifier: StudioNotifier | None = None) -> None:
        self.home = Path(home)
        self.lib = library
        self.render = render
        self.library: LutLibrary = render.luts
        self.recipes: RecipeRepository = render.recipes
        self.contexts = FitContexts(self.home, tuple(self.library.forbidden))
        self._lock = threading.RLock()
        self._procs: dict[str, subprocess.Popen[Any]] = {}
        self._colours: dict[Any, Any] = {}
        self._notifier = notifier or StudioNotifier()
        self._reported_failures: set[str] = set()
        self._failure_lock = threading.Lock()

    def _write_file(self, relative: str, data: bytes) -> None:
        try:
            with self.library.private() as directory:
                directory.replace_file(relative, data)
        except OSError as exc:
            raise LutError(422, "lut_io_error", "无法读写 LUT 库。请检查应用数据。", {"detail": str(exc)}) from exc

    def _write_json(self, path: Path, doc: dict[Any, Any]) -> None:
        data = json.dumps(doc, ensure_ascii=False, indent=2, default=lambda o: o.tolist() if hasattr(o, "tolist") else str(o)).encode()
        self._write_file(path.relative_to(self.home).as_posix(), data)

    # ---- 目录 ---------------------------------------------------------------
    @property
    def luts_dir(self) -> Path:
        return self.home / "luts"

    @property
    def jobs_dir(self) -> Path:
        return self.home / "lut_jobs"

    # ---- 调色文件 -------------------------------------------------------------
    def add_cube(self, raw: bytes, filename: str = "", group: str | None = None) -> dict[Any, Any]:
        item = self.library.add(raw, filename or "LUT.cube", group)
        if item.get("skipped"):
            return item
        return {**self._describe(self.library.load(item["cube_id"])), **item, **input_choice(item)}

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
        return self.library.load(cube_id)

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
                edit, _side = read_edit(self.lib, shot, self.render.recipe_menu())
                if edit.recipe is not None:
                    rgb = self.render.renderer(shot, EditState(recipe=edit.recipe)).display(codes)
                    return rgb.astype(float), xyz, "recipe-prediction"
                base = self.render.default_look(shot, codes)
                if base is not None:
                    return base.astype(float), xyz, "04-simulation"
            except RenderUnavailable:
                if edit.recipe is not None:
                    raise LutError(409, "camera_prediction_unavailable", "无法生成拍摄配方的电脑预测。请检查相机配方文件和校准数据。") from None
                pass
            except EditError as exc:
                raise LutError(422, exc.code, exc.message) from exc
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
        """which = original | lut。基准沿用有效配方及旋钮；没有配方时用 04，缺相机数据时用中性转换。"""
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
    def pick_photos(self, ids: list[str] | None = None, limit: int = 8, *, unit: str | None = None) -> list[dict[Any, Any]]:
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
        if unit is not None:
            shots = [s for s in shots if self.render.photo_unit(s) == unit]
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
        quality_source: str | None = None,
    ) -> dict[Any, Any]:
        if profile not in ("normal", "quick"):
            raise LutError(422, "bad_profile", "请求无效。请刷新页面后重试。", {"detail": "profile 只能是 normal 或 quick"})
        cube = self.cube(cube_id)
        self.target(cube, **spec)  # 先校验色彩空间参数
        guessed = guessed or self._filename_guess_matches(cube_id, spec["space"])
        if not self.render.available():
            st = self.render.status()
            raise LutError(409, "camera_data_missing", st["hint"] or "请先导入相机配方文件", {"status": st})
        if not self.render.available(unit):
            raise LutError(422, "unit_not_calibrated", "这个单元的校准数据尚未导入或已失效，请重新导入。", {"unit": unit})
        with self._lock, self.library.lock:
            busy = next((j for j, p in self._procs.items() if p.poll() is None), None)
            if busy is not None:
                raise LutError(409, "fit_busy", "无法生成配方。已经有一个在进行。", {"job_id": busy})
            if quality_source is None:
                raise LutError(422, "quality_source_required", "请先导入该单元的私有质量输入包。训练和留出分组会在生成前冻结。")
            # 包内的want绑定完整目标解释，不能把另一LUT或当前照片编辑当作相同目标。
            target_bytes, source_lut = self.library.snapshot(cube_id)
            environment = RenderEnvironment.capture(self.home, self.render._require(unit), unit)
            target = {"kind": "lut", "cube_sha256": cube_id, "spaces": self.target(cube, **spec).description()}
            try:
                context = self.contexts.freeze(
                    quality_source,
                    target=target,
                    plan=default_plan(),
                    environment=environment,
                    style=cube_id,
                    replacement=self.recipes.reference_for_style(cube_id, unit),
                    target_renderer=self.target(cube, **spec),
                    target_asset=target_bytes,
                )
            except RecipeError as exc:
                raise LutError(422, exc.code, exc.message) from exc
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise LutError(422, "quality_input_unreadable", "无法读取冻结质量输入，请检查私有质量包。") from exc
            photos: list[dict[str, Any]] = []
            job_id = uuid.uuid4().hex[:12]
            job_dir = self.jobs_dir / job_id
            job = {
                "home": str(self.home),
                "context": context.identity,
                "cube_id": cube_id,
                "lut_version": source_lut["version"],
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
                "source": {
                    "kind": "lut",
                    "cube_sha256": cube_id,
                    "title": source_lut.get("name") or source_lut.get("title"),
                    "attribution": {key: source_lut[key] for key in FIELDS},
                    "target": target,
                    "input_space_guessed": guessed,
                    "fit_job": job_id,
                },
            }
            self._write_json(job_dir / "job.json", job)
            self.recipes.start_run(
                job_id,
                context.identity,
                {
                    "unit": unit,
                    "cube_id": cube_id,
                    "input": {k: job[k] for k in ("input_space", "output_space", "profile", "space_guessed")},
                    "photos": photos,
                    "source_sha256": digest(canonical(job["source"])),
                },
            )
            env = {
                **os.environ,
                "OPENBLAS_NUM_THREADS": "1",
                "OMP_NUM_THREADS": "1",
                "VECLIB_MAXIMUM_THREADS": "1",
                "MKL_NUM_THREADS": "1",
                "NUMEXPR_NUM_THREADS": "1",
                "BLIS_NUM_THREADS": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "GXR_STUDIO_PARENT_PID": str(os.getpid()),
                "GXR_STUDIO_LUT_FORBIDDEN": json.dumps([str(p) for p in self.library.forbidden]),
                "GXR_STUDIO_LUT_HOME": str(self.home),
            }  # 子进程据此发现服务没了（SIGKILL / SIGHUP）
            try:
                proc = subprocess.Popen(  # nosec B603  参数是解释器和本包模块，没有 shell
                    [sys.executable, "-I", "-m", "gxr_studio.lut.worker", str(job_dir)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    env=env,
                    start_new_session=True,
                )
            except OSError as exc:
                self.recipes.finish_run(job_id, "error", {"code": "worker_start_failed", "message": "无法启动配方计算进程。"})
                self._report_failure(job_id, None, {"code": "worker_start_failed", "message": "无法启动配方计算进程。"})
                raise LutError(422, "worker_start_failed", "无法启动配方计算进程。") from exc
            self._procs[job_id] = proc
            self._prune()
        return {"job_id": job_id, "unit": unit, "photos": [{k: p[k] for k in ("label", "iso")} for p in photos]}

    def _job_dir(self, job_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{12}", job_id or "") or not (self.jobs_dir / job_id / "job.json").is_file():
            raise LutError(404, "fit_not_found", "找不到这次生成。请刷新页面后重试。")
        return self.jobs_dir / job_id

    def _read(self, path: Path) -> dict[str, Any] | None:
        return self._observe(path)[1]

    @staticmethod
    def _observe(path: Path) -> tuple[bool, dict[str, Any] | None]:
        """读一次文件：(这次是否真的读到了字节, 解析出的对象；不是规范的 JSON 对象就是 None)。

        "没读到"和"读到了但内容无效"必须来自同一次读取。先读不到、随后 worker 原子发布了结果时，
        不能再看一眼文件存在，就把前一次的 None 当成坏文件（PR #121 第四轮复审的 P1）。
        """
        try:
            raw = path.read_bytes()
        except OSError:
            return False, None
        try:
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                return True, None
            canonical(data)
            return True, data
        except (TypeError, ValueError):
            return True, None

    def status(self, job_id: str, *, with_result: bool = True) -> dict[Any, Any]:
        if not re.fullmatch(r"[0-9a-f]{12}", job_id or ""):
            raise LutError(404, "fit_not_found", "找不到这次生成。请刷新页面后重试。")
        archived = self.recipes.run(job_id)
        if archived is not None and archived["state"] != "running":
            return self._archived_status(job_id, archived, with_result)
        d = self._job_dir(job_id)
        job = self._read(d / "job.json")
        proc = self._procs.get(job_id)
        # Poll before reading, so a worker that publishes its result and exits cannot be mistaken for a crash.
        exit_code = proc.poll() if proc is not None else None
        result_seen, result = self._observe(d / "result.json")
        progress = self._read(d / "progress.json")
        if archived is None:
            # 旧任务没有冻结质量输入，不按旧算法解释成完整结果。
            raise LutError(409, "quality_context_missing", "这次旧任务没有完整配方上下文，请重新生成。")
        if result is not None:
            if result.get("state") == "done":
                try:
                    self._archive_result(job_id, job, result)
                except RecipeError as exc:
                    self.recipes.finish_run(
                        job_id,
                        "error",
                        {
                            "code": "invalid_full_result",
                            "reason_code": exc.code,
                            "message": f"完整配方结果未能核验，无法保存。\n{exc.message}",
                        },
                    )
                except OSError as exc:
                    self.recipes.finish_run(
                        job_id,
                        "error",
                        {
                            "code": "invalid_full_result",
                            "message": "完整配方结果未能核验，无法保存。\n结果文件缺失或无法读取。",
                            "trace": str(exc),
                        },
                    )
                except (ValueError, KeyError, TypeError) as exc:
                    self.recipes.finish_run(
                        job_id,
                        "error",
                        {
                            "code": "invalid_full_result",
                            "message": "完整配方结果未能核验，无法保存。\n结果文件格式无效或缺少必要字段。",
                            "trace": str(exc),
                        },
                    )
            else:
                state = "cancelled" if result.get("state") == "cancelled" else "error"
                self.recipes.finish_run(job_id, state, result)
        elif result_seen:
            self.recipes.finish_run(
                job_id,
                "error",
                {
                    "code": "invalid_full_result",
                    "message": "完整配方结果未能核验，无法保存。\n结果文件格式无效或缺少必要字段。",
                },
            )
        elif proc is None or exit_code is not None:
            detail = (
                {"code": "interrupted", "message": "工作室已重启，这次配方计算已中断。请重新生成。"}
                if proc is None
                else {
                    "code": "worker_died",
                    "message": "配方计算进程意外退出，未能取得结果。请查看工作室日志。",
                    "exit_code": exit_code,
                }
            )
            self.recipes.finish_run(job_id, "interrupted" if proc is None else "error", detail)
        current = self.recipes.run(job_id)
        if current is None:
            raise LutError(404, "fit_not_found", "找不到这次生成。")
        return self._archived_status(job_id, current, with_result, progress)

    def _archive_result(self, job_id: str, job: dict[str, Any] | None, result: dict[str, Any]) -> None:
        if (
            not isinstance(job, dict)
            or not isinstance(job.get("context"), str)
            or not isinstance(job.get("source"), dict)
            or result.get("schema") != "gxr-studio/full-fit-result/1"
            or result.get("context") != job.get("context")
        ):
            raise RecipeError("result_binding_mismatch", "拟合结果的上下文身份不符。")
        with self.library.private() as directory:
            entry = directory.read(f"lut_jobs/{job_id}/candidate.record")
            receipt = directory.read(f"lut_jobs/{job_id}/winner.json")
            proof = directory.read(f"lut_jobs/{job_id}/quality.json")
            seal = json.loads(directory.read(f"lut_jobs/{job_id}/winner-seal.json"))
        for key, blob in (("record_sha256", entry), ("receipt_sha256", receipt), ("quality_sha256", proof)):
            if digest(blob) != result.get(key):
                raise RecipeError("result_source_changed", "拟合结果成员的摘要不符。")
        if seal != {"context": job["context"], "record_sha256": digest(entry), "receipt_sha256": digest(receipt)}:
            raise RecipeError("winner_not_sealed", "训练胜者没有完整封存。")
        context = self.contexts.open(job["context"])
        archived = self.recipes.run(job_id)
        receipt_doc = json.loads(receipt)
        source_doc = job["source"]
        if (
            archived is None
            or archived["context"] != job.get("context")
            or not isinstance(receipt_doc, dict)
            or canonical(receipt_doc) != receipt
            or archived["metadata"].get("source_sha256") != digest(canonical(source_doc))
            or context.training.unit != job.get("unit")
            or source_doc.get("cube_sha256") != job.get("cube_id")
            or context.document["target"] != source_doc.get("target")
            or (context.document["target"].get("kind") == "lut" and context.document["target"].get("cube_sha256") != job.get("cube_id"))
            or receipt_doc.get("training_identity") != context.training.identity
            or receipt_doc.get("record_sha256") != digest(entry)
            or receipt_doc.get("contract") != default_plan()["contract"]
            or receipt_doc.get("plan") != json.loads(context.training.plan)
            or receipt_doc.get("source_sha256") != context.document["plan"]["source_sha256"]
        ):
            raise RecipeError("result_binding_mismatch", "完整记录、训练回执或冻结来源不匹配。")
        check_record(entry)
        if entry[1] != (1 if context.training.family == "bw" else 7):
            raise RecipeError("result_binding_mismatch", "完整记录、训练回执或冻结来源不匹配。")
        proof_doc = json.loads(proof)
        if not isinstance(proof_doc, dict) or canonical(proof_doc) != proof:
            raise RecipeError("bad_proof", "质量回执字节不是规范编码。")
        claimed = Quality.from_json(proof_doc)
        groups, references = self.contexts.evaluation(context.identity)
        quality = verify_archived_proof(context, Winner(entry, receipt), groups, references, claimed)
        self.recipes.retain_fit(
            job_id,
            FitProduct(
                context.training.unit,
                context.training.slot,
                entry,
                context.training.environment.data,
                context.identity,
                canonical(source_doc),
                quality,
                receipt,
                context.document.get("style"),
                context.document.get("replacement_version"),
            ),
        )

    def _archived_status(
        self,
        job_id: str,
        archived: dict[str, Any],
        with_result: bool,
        progress: Any = None,
    ) -> dict[str, Any]:
        state = archived["state"]
        out = {"job_id": job_id, **archived["metadata"], "state": "error" if state == "interrupted" else state, "progress": progress}
        if state in ("error", "interrupted"):
            detail = archived["detail"]
            out["error"] = {k: detail[k] for k in ("code", "message", "exit_code") if k in detail}
            self._report_failure(job_id, progress, detail)
        if state == "done" and with_result:
            candidate = {k: archived[k] for k in ("id", "recipe", "quality", "controls", "record_sha256", "binding_sha256", "fit")}
            candidate.update(unit=archived["recipe"]["unit"], slot=archived["recipe"]["slot"], similarity=archived["fit"]["similarity"])
            out["result"] = {"candidates": [candidate], "formula": "100 × (1 − 配方ΔE00中位 / 原厂04ΔE00中位)，D65电脑预测"}
        return out

    def _report_failure(self, job_id: str, progress: Any, result: dict[Any, Any]) -> None:
        if not self.recipes.claim_failure_report(job_id):
            return
        with self._failure_lock:
            if job_id in self._reported_failures:
                return
            self._reported_failures.add(job_id)
        failure = Job("lut_fit", progress or {})
        failure.id = job_id
        details = [f"{result.get('code')}: {result.get('message')}"]
        if result.get("reason_code"):
            details.append(f"reason_code={result['reason_code']}")
        if "exit_code" in result:
            details.append(f"exit_code={result['exit_code']}")
        if result.get("trace"):
            details.append(result["trace"])
        failure.error = scrub_text("\n".join(details))
        on_job_failed(failure, self._notifier)

    def cancel(self, job_id: str) -> dict[Any, Any]:
        current = self.status(job_id, with_result=False)
        if current["state"] != "running":
            return current
        self.recipes.finish_run(job_id, "cancelled", {"code": "user_cancelled", "message": "已取消生成。"})
        proc = self._procs.get(job_id)
        if proc is not None and proc.poll() is None:
            self._write_file(f"lut_jobs/{job_id}/cancel", b"1")
            proc.terminate()
            try:
                proc.wait(3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(5)
        return self.status(job_id, with_result=False)

    def invalidate_cube(self, cube_id: str) -> None:
        """空间改选或删除之后，停止仍在使用旧记录的拟合。"""
        with self._lock:
            for job_id, proc in list(self._procs.items()):
                job = self._read(self.jobs_dir / job_id / "job.json")
                if proc.poll() is None and job and job.get("cube_id") == cube_id:
                    self.cancel(job_id)

    def close(self) -> None:
        """应用关闭时：还在跑的拟合一并结束，不留孤儿进程。"""
        for job_id, proc in list(self._procs.items()):
            if proc.poll() is None:
                self.recipes.finish_run(job_id, "interrupted", {"code": "interrupted", "message": "工作室已关闭，这次计算已中断。"})
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
                archived = self.recipes.run(p.name)
                if archived is not None and archived["state"] == "running":
                    self.status(p.name, with_result=False)
                    archived = self.recipes.run(p.name)
                if archived is None or archived["state"] == "running":
                    continue
                with self.library.private() as directory:
                    directory.remove(f"lut_jobs/{p.name}")
                with self._failure_lock:
                    self._reported_failures.discard(p.name)

    # ---- 我的配方 -----------------------------------------------------------------
    def _filename_guess_matches(self, cube_id: str, space: str) -> bool:
        """配方沿用页面的推测标记；文件声明和手选均不标推测。"""
        record = self.library.get(cube_id)
        choice = input_choice(record)
        return bool(choice["default_input_source"] == "guessed" and choice["default_input_space"] == space)

    def save_recipe(self, job_id: str, candidate: int, name: str) -> dict[Any, Any]:
        name = clean_name(name)
        if self.status(job_id)["state"] != "done":
            raise LutError(409, "fit_not_done", "无法保存配方。请等生成完成后再试。")
        if isinstance(candidate, bool) or candidate != 0:
            raise LutError(422, "bad_candidate", "完整拟合只保存训练选定的唯一胜者。")
        return self.recipes.publish(job_id, name=name)

    def list_recipes(self) -> list[dict[Any, Any]]:
        recipes = self.recipes.listing()
        active = {item["cube_id"] for item in self.library.listing()["luts"]}
        return [{**r, "source_available": r["source"].get("cube_sha256") in active} for r in recipes]

    def get_recipe(self, rid: str) -> dict[Any, Any]:
        return self.recipes.get(rid)

    def rename_recipe(self, rid: str, name: str, expected_revision: int) -> dict[Any, Any]:
        return self.recipes.rename(rid, name=clean_name(name), expected_revision=expected_revision)

    def delete_recipe(self, rid: str, expected_revision: int) -> None:
        self.recipes.retire(rid, expected_revision=expected_revision)
