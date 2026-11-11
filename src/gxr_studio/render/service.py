"""渲染服务：把"DNG + 编辑参数"变成电脑预测的图（ARCHITECTURE §5）。

链：白平衡后码值 → 曝光 / 色温色调增益 → 相机配方（底子 + 旋钮 + Amount，552 B 深层行）→ 电脑预测模型
→ 显示域调子表（画面对比/高光/阴影 + 主曲线）→ [裁切与拉直] → 8 位输出。

- 预览：半分辨率（2×2 合并，不去马赛克）的码值图，烘焙路径（`chain.bake_chroma`）；
- 导出：全分辨率条带，同一个烘焙（更细的网格），或 `precise` 时逐像素精确链；
- 缓存：解码后的码值图、烘焙结果、渲染结果三层，键按 §5.3：源文件 SHA-256、方向、调整、配方条目 SHA-256、曲线、
  引擎版本、目标宽度；同一张照片同一组参数不重复计算；
- 没有导入相机数据（或没有标定包）时抛 `RenderUnavailable`，调用方返回明确的状态，而不是渲染一张不对的图。

每一张图都是"电脑预测"。仅对应单元的实拍校准 ISO 算已校准；未知镜头或跨单元配方另标未校准。
"""

from __future__ import annotations

import hashlib
import io
import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from .. import crop as cropmod
from ..camdata import calib, datcodec, store
from ..camdata.store import CamdataError
from ..camdata.unit_model import UnitModel
from ..export import crop_image, srgb_icc
from ..lut.library import LutLibrary
from ..raw import Renderer, half_res_codes, open_dng, orient_array
from ..recipes import RecipeError, RecipeRepository
from . import chain
from .params import EditState, RecipeSel
from .recipes import CameraData, Menu, unit_of_lens
from .tones import channel_gains, display_table

ENGINE = "gxr_render/2"
PREVIEW_STEP = 0.375  # 现行逐单元模型需要更密的网格，保持 p99 ΔE00 ≤ 0.3
EXPORT_STEP = 0.375  # 导出保持相同的色度网格精度
ISO_SPLIT = 566  # 200 和 1600 的几何中点：低于它用 ISO 200 的模型，否则用 ISO 1600 的
UNCALIBRATED_LABEL = "电脑预测 未校准"
CALIBRATED_LABEL = "电脑预测"
MODEL_NOTE = "镜头单元、配方单元或 ISO 不在实拍校准范围"


class RenderUnavailable(Exception):
    """不能渲染：相机数据/标定包缺失或不合格。`state` 给界面做分支。"""

    def __init__(self, code: str, message: str, state: str) -> None:
        super().__init__(message)
        self.code, self.message, self.state = code, message, state


class NoRaw(Exception):
    """这张照片没有 RAW，没法套配方。"""


class ByteLRU[T]:
    def __init__(self, limit_bytes: int) -> None:
        self.limit = limit_bytes
        self._d: OrderedDict[Any, tuple[T, int]] = OrderedDict()
        self._size = 0
        self._lock = threading.Lock()

    def get(self, key: Any) -> T | None:
        with self._lock:
            hit = self._d.get(key)
            if hit is not None:
                self._d.move_to_end(key)
            return None if hit is None else hit[0]

    def put(self, key: Any, value: T, nbytes: int) -> None:
        with self._lock:
            old = self._d.pop(key, None)
            if old is not None:
                self._size -= old[1]
            self._d[key] = (value, nbytes)
            self._size += nbytes
            while self._size > self.limit and len(self._d) > 1:
                _, (_, n) = self._d.popitem(last=False)
                self._size -= n

    def clear(self) -> None:
        with self._lock:
            self._d.clear()
            self._size = 0

    def __len__(self) -> int:
        return len(self._d)


@dataclass(frozen=True)
class Plan:
    unit: str
    slot: int
    defaulted: bool  # 没选配方，用的是该单元的原厂 04（电脑模拟）
    levels: dict[Any, Any]
    entry: bytes
    entry_sha256: str
    model_iso: int
    calibrated: bool
    reasons: tuple[str, ...]
    label: str
    model: UnitModel | None
    record_sha256: str = ""
    binding_sha256: str = ""
    selection_id: str | None = None
    quality: dict[str, Any] | None = None


@dataclass
class RenderOut:
    jpeg: bytes
    width: int
    height: int
    key: str
    cache_hit: bool
    plan: Plan
    ms: dict[Any, Any] = field(default_factory=dict[Any, Any])


def shrink_codes(codes: np.ndarray, max_edge: int | None) -> np.ndarray:
    """线性码值图缩到长边 max_edge 以内（PIL 的 F 模式，缩小时带抗锯齿）；不放大。"""
    h, w = codes.shape[:2]
    if not max_edge or max(h, w) <= max_edge:
        return codes
    s = max_edge / max(h, w)
    nw, nh = max(1, round(w * s)), max(1, round(h * s))
    out = np.empty((nh, nw, 3), np.float32)
    for c in range(3):
        im = Image.fromarray(np.ascontiguousarray(codes[..., c], np.float32), mode="F")
        out[..., c] = np.asarray(im.resize((nw, nh), Image.Resampling.BILINEAR))
    return out


def _encode_jpeg(rgb: np.ndarray, quality: int = 88) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(rgb, "RGB").save(buf, "JPEG", quality=quality, subsampling=0 if quality >= 90 else 2, icc_profile=srgb_icc())
    return buf.getvalue()


def _stat_token(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


class RenderService:
    def __init__(
        self,
        home: Path,
        library: Any,
        *,
        proxy_bytes: int = 640 << 20,
        result_bytes: int = 96 << 20,
        max_concurrent: int = 2,
        forbidden: tuple[Path, ...] = (),
    ) -> None:
        self.home = Path(home)
        self.lib = library
        self.luts = LutLibrary(self.home, (*forbidden, library.root))
        self.recipes = RecipeRepository(self.home, (*forbidden, library.root))
        self.menu = Menu()
        self._lock = threading.RLock()
        self._gate = threading.BoundedSemaphore(max_concurrent)
        self._state_token = None
        self._camera: CameraData | None = None
        self._camera_error: tuple[str, str] | None = None
        self._import_generation: int | None = None
        self._calib_manifest_sha: str | None = None
        self._calib_error: str | None = None
        self._unit_calibration: dict[str, dict[str, Any]] = {}
        self._models: dict[Any, Any] = {}
        self._look_labels: dict[tuple[Any, Any, Any], str | None] = {}
        self._proxies: ByteLRU[np.ndarray] = ByteLRU(proxy_bytes)
        self._bakes: ByteLRU[chain.ChromaBake] = ByteLRU(64 << 20)
        self._results: ByteLRU[tuple[bytes, int, int]] = ByteLRU(result_bytes)
        self._approx: dict[tuple[Any, ...], np.ndarray] = {}
        self._warmed: set[tuple[Any, ...]] = set()
        self.stats = {"decodes": 0, "bakes": 0, "renders": 0, "result_hits": 0}

    # ---- 状态 -------------------------------------------------------------
    def _refresh(self) -> None:
        token = (
            _stat_token(store.camera_data_dir(self.home) / "manifest.json"),
            _stat_token(calib.calib_dir(self.home) / "manifest.json"),
            tuple((p.name, _stat_token(p)) for p in sorted(calib.calib_dir(self.home).glob("*"))),
        )
        with self._lock:
            if token == self._state_token:
                return
            self._state_token = token  # type: ignore[assignment]
            self._camera, self._camera_error = None, None
            self._calib_manifest_sha, self._calib_error = None, None
            self._models.clear()
            self._bakes.clear()
            self._approx.clear()
            if token[0] is None:
                self._camera_error = ("missing", "还没有导入相机配方文件")
            else:
                try:
                    self._camera = CameraData(self.home)
                except (CamdataError, OSError, ValueError, KeyError):
                    self._camera_error = ("invalid", "相机配方文件读不出来或被改动。请重新导入后再试。")
            self._unit_calibration = {unit: calib.unit_status(self.home, unit) for unit in calib.UNIT_ISOS}
            usable = any(st["state"] == "ready" for st in self._unit_calibration.values())
            self._calib_error = None if usable else next(iter(self._unit_calibration.values()))["message"]
            if token[1] is not None:
                self._calib_manifest_sha = hashlib.sha256(repr(token[1:]).encode()).hexdigest()

    def available(self, unit: str | None = None) -> bool:
        self._refresh()
        return self._camera is not None and (
            self._unit_calibration.get(unit, {}).get("state") == "ready" if unit is not None else self._calib_error is None
        )

    def invalidate(self) -> None:
        """相机数据或标定包刚被换掉：下次访问重新读盘，烘焙和渲染结果缓存清空（解码后的码值图与相机数据无关，保留）。"""
        with self._lock:
            self._state_token = None
            self._models.clear()
            self._look_labels.clear()
            self._bakes.clear()
            self._results.clear()
            self._warmed.clear()

    def replace_data(self, action: Any) -> Any:
        """在渲染服务的锁里执行 `action`（换掉应用数据目录里的相机数据/标定包），然后让所有缓存失效。

        持锁期间 `_refresh` 进不来，所以不会看到换到一半（旧目录已移走、新目录还没就位）的状态。
        """
        with self._lock:
            try:
                return action()
            finally:
                self.invalidate()

    def data_version(self) -> str:
        """相机数据 + 标定包的版本标记；缩略图/预览的 URL 带上它，浏览器缓存的旧图就不会再被用到。"""
        self._refresh()
        cam = self._camera.manifest_sha256[:8] if self._camera else "none"
        return f"{cam}-{(self._calib_manifest_sha or 'none')[:6]}"

    def note_import_generation(self, generation: int | None) -> None:
        """仅记住本次会话中未能导入的代数，不保存文件，也不替换已导入的数据。"""
        with self._lock:
            self._refresh()
            self._import_generation = generation if self._camera is None else None

    def status(self) -> dict[Any, Any]:
        self._refresh()
        cam = self._camera
        cam_state = "ready" if cam else self._camera_error[0]  # type: ignore[index]
        cal_state = "ready" if self._calib_error is None else ("missing" if "缺少" in self._calib_error else "invalid")
        ready = cam is not None and self._calib_error is None
        unsupported = cam is None and self._import_generation is not None
        camera_message = None if cam else self._camera_error[1]  # type: ignore[index]
        detail = None
        if unsupported:
            camera_message = datcodec.unsupported_message(self._import_generation)  # type: ignore[arg-type]
            detail = datcodec.unsupported_detail(self._import_generation)  # type: ignore[arg-type]
        return {
            "ready": ready,
            "camera_data": {
                "state": "unsupported" if unsupported else cam_state,
                "message": camera_message,
                "generation": cam.manifest["generation"] if cam else None,
                "detail": detail,
                "source_sha256": cam.manifest["source"]["sha256"] if cam else None,
                "imported_at": cam.manifest["imported_at"] if cam else None,
            },
            "calibration": {
                "state": cal_state,
                "message": self._calib_error,
                "isos": sorted({iso for st in self._unit_calibration.values() for iso in st["isos"]}),
                "units": self._unit_calibration,
            },
            "engine": self.engine_pin() if ready else None,
            "hint": camera_message if cam is None else self._calib_error,
        }

    def _require(self, unit: str | None = None) -> CameraData:
        self._refresh()
        if self._camera is None:
            if self._import_generation is not None:
                raise RenderUnavailable("generation_unsupported", datcodec.unsupported_message(self._import_generation), "unsupported")
            state, _msg = self._camera_error  # type: ignore[misc]
            text = "无法生成电脑预测。请先导入相机配方文件。" if state == "missing" else "无法读取相机配方文件。请重新导入后再试。"
            raise RenderUnavailable("camera_data_missing" if state == "missing" else "camera_data_invalid", text, state)
        if self._calib_error is not None or (unit is not None and not self.available(unit)):
            raise RenderUnavailable("calibration_missing", "无法生成电脑预测。请先导入校准数据。", "calibration")
        return self._camera

    def engine_pin(self) -> str:
        cam = self._camera
        if cam is None:
            return f"{ENGINE}:no-current-camera"
        return f"{ENGINE}:{cam.table_sha256[:12]}:{cam.manifest_sha256[:12]}:{(self._calib_manifest_sha or '')[:12]}"

    def model(self, model_iso: int, unit: str = "jlaunch") -> Any:
        self._require(unit)
        with self._lock:
            m = self._models.get((unit, model_iso))
            if m is None:
                m = self._models[(unit, model_iso)] = calib.model_for(self.home, model_iso, unit=unit)[0]
            return m

    def catalog(self) -> list[dict[Any, Any]]:
        """配方列表：相机数据已导入时带条目 SHA-256，没导入也能列（菜单表不含理光字节）。"""
        return [
            {
                **u,
                "calibrated": self.available(u["id"]),
                "calibration_note": self._unit_calibration[u["id"]]["message"],
                "calibrated_isos": self._unit_calibration[u["id"]]["isos"],
                "exposure_note": self._unit_calibration[u["id"]]["exposure_note"],
            }
            for u in self.recipe_menu().listing()
        ]

    def recipe_menu(self) -> Menu:
        self._refresh()
        return self._camera or self.menu

    def photo_unit(self, shot: dict[Any, Any]) -> str | None:
        return unit_of_lens(shot.get("lens"))

    # ---- 计划 -------------------------------------------------------------
    def plan(self, shot: dict[Any, Any], edit: EditState) -> Plan:
        photo_unit = self.photo_unit(shot)
        defaulted = edit.recipe is None
        sel = edit.recipe or RecipeSel(photo_unit or "jlaunch", 4)
        if sel.kind == "local":
            if not {"dng", "jpg"} <= set(shot.get("files", {})):
                raise RenderUnavailable("local_recipe_needs_pair", "完整本机配方需要RAW+JPG照片。", "unsupported")
            try:
                local = self.recipes.resolve(sel, photo_unit=photo_unit, iso=int(shot.get("iso") or 200))
            except RecipeError as exc:
                raise RenderUnavailable(exc.code, exc.message, "local_recipe") from exc
            local_reasons = () if local.calibrated else (MODEL_NOTE,)
            quality = {**local.quality.summary(), "assessed_entry": local.assessed_entry}
            return Plan(
                sel.unit,
                sel.slot,
                False,
                local.levels,
                local.entry,
                local.entry_sha256,
                local.model_iso,
                local.calibrated,
                local_reasons,
                CALIBRATED_LABEL if local.calibrated else UNCALIBRATED_LABEL,
                local.model,
                local.record_sha256,
                local.binding_sha256,
                sel.id,
                quality,
            )
        res = self._require(sel.unit).resolve(sel)
        iso = shot.get("iso")
        model_iso = 1600 if iso and iso >= ISO_SPLIT else 200
        crossed = photo_unit is None or iso not in calib.UNIT_ISOS[sel.unit] or sel.unit != photo_unit
        reasons = [MODEL_NOTE] if crossed else []
        return Plan(
            sel.unit,
            sel.slot,
            defaulted,
            res.levels,
            res.entry,
            res.entry_sha256,
            model_iso,
            not reasons,
            tuple(reasons),
            CALIBRATED_LABEL if not reasons else UNCALIBRATED_LABEL,
            None,
            res.entry_sha256,
            self.engine_pin(),
        )

    # ---- 缓存件 -----------------------------------------------------------
    def _proxy(self, shot: dict[Any, Any], max_edge: int | None) -> np.ndarray:
        """半分辨率、已转正的白平衡码值图（可再缩小）；按 (DNG SHA-256, max_edge) 缓存。"""
        sha = shot["files"]["dng"]["sha256"]
        key = (sha, max_edge)
        hit = self._proxies.get(key)
        if hit is not None:
            return hit
        with self._lock:
            base = self._proxies.get((sha, None))
            if base is None:
                dng = open_dng(self.lib.abspath(shot["files"]["dng"]["relpath"]))
                try:
                    base = np.ascontiguousarray(orient_array(half_res_codes(dng), dng.orientation), np.float32)
                finally:
                    dng.release()
                self._proxies.put((sha, None), base, base.nbytes)
                self.stats["decodes"] += 1
            if max_edge is None:
                return base
            small = shrink_codes(base, max_edge)
            if small is not base:
                self._proxies.put(key, small, small.nbytes)
            return small

    def proxy_codes(self, shot: dict[Any, Any], max_edge: int | None) -> np.ndarray:
        """半分辨率、已转正的白平衡码值图（给 LUT 导入的预览和抽样用）。"""
        return self._proxy(shot, max_edge)

    def _bake(self, plan: Plan, step: float) -> chain.ChromaBake:
        key = (plan.unit, plan.entry_sha256, plan.model_iso, step, plan.binding_sha256, ENGINE)
        hit = self._bakes.get(key)
        if hit is not None:
            return hit
        bk = chain.bake_chroma(plan.model or self.model(plan.model_iso, plan.unit), plan.entry, step)
        self._bakes.put(key, bk, bk.gain.nbytes + bk.pre.nbytes)
        self.stats["bakes"] += 1
        return bk

    def preview_bake(self, plan: Plan) -> chain.ChromaBake:
        """预览网格（PREVIEW_STEP）的烘焙表，和 `render_preview` 用的是同一份缓存。给显卡预览下发。"""
        return self._bake(plan, PREVIEW_STEP)

    def warm_preview_bakes(self, shot: dict[Any, Any]) -> threading.Thread | None:
        """后台把这张照片所在单元、所在 ISO 模型的 8 个内置配方（默认档位）先烘焙好。

        第一次烘焙一个条目要 0.1–0.4 秒；先做掉，换到同单元的照片时显卡预览不用等。每个组合只做一次。"""
        if "dng" not in shot["files"] or not self.available():
            return None
        unit = self.photo_unit(shot) or "jlaunch"
        iso = shot.get("iso")
        key = (unit, 1600 if iso and iso >= ISO_SPLIT else 200, self._state_token)
        with self._lock:
            if key in self._warmed:
                return None
            self._warmed.add(key)

        def run() -> None:
            for slot in range(1, 9):
                try:
                    self._bake(self.plan(shot, EditState(recipe=RecipeSel(unit, slot))), PREVIEW_STEP)
                except Exception:  # noqa: BLE001  预热失败不影响前台：真用到时再算一次，错误在那时报
                    return

        thread = threading.Thread(target=run, name="gxr-bake-warm", daemon=True)
        thread.start()
        return thread

    def approx_lut(self, unit: str, slot: int, iso: int) -> np.ndarray:
        from ..finish.approx import lut_for_entries
        from .params import RecipeSel

        model_iso = 200 if int(iso) < ISO_SPLIT else 1600
        key = (unit, int(slot), model_iso, self.engine_pin())
        hit = self._approx.get(key)
        if hit is not None:
            return hit
        cam = self._require()
        stock = cam.resolve(RecipeSel(unit, 4)).entry
        recipe = cam.resolve(RecipeSel(unit, int(slot))).entry
        lut = lut_for_entries(self.model(model_iso, unit), stock, recipe)
        self._approx[key] = lut
        return lut

    def render_key(self, shot: dict[Any, Any], edit: EditState, plan: Plan, *, max_edge: Any, exact: bool, crop: Any, quality: int = 88) -> str:
        parts = {
            "source": shot["files"]["dng"]["sha256"],
            "orientation": shot.get("orientation") or 1,
            "adjust": edit.adjust.to_json(),
            "recipe_entry": plan.entry_sha256,
            "model_iso": plan.model_iso,
            "model_unit": plan.unit,
            "curve": edit.curve.to_json() if edit.curve else None,
            "engine": ENGINE,
            "binding": getattr(plan, "binding_sha256", ""),
            "width": max_edge,
            "exact": exact,
            "step": PREVIEW_STEP,
            "crop": crop.to_json() if crop else None,
            "quality": quality,
        }
        return hashlib.sha256(json.dumps(parts, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    # ---- 预览 -------------------------------------------------------------
    def render_preview(
        self,
        shot: dict[Any, Any],
        edit: EditState,
        *,
        max_edge: int | None = None,
        exact: bool = False,
        crop: cropmod.Crop | None = None,
        quality: int = 88,
    ) -> RenderOut:
        if "dng" not in shot["files"]:
            raise NoRaw()
        plan = self.plan(shot, edit)
        key = self.render_key(shot, edit, plan, max_edge=max_edge, exact=exact, crop=crop, quality=quality)
        hit = self._results.get((key, quality))
        if hit is not None:
            self.stats["result_hits"] += 1
            jpeg, w, h = hit
            return RenderOut(jpeg, w, h, key, True, plan, {"total": 0.0})
        t0 = time.perf_counter()
        with self._gate:
            ms = {}
            t = time.perf_counter()
            decodes = self.stats["decodes"]
            codes = self._proxy(shot, max_edge)
            ms["decode"] = (time.perf_counter() - t) * 1000 if self.stats["decodes"] != decodes else 0.0
            gains = channel_gains(edit.adjust)
            table = display_table(edit.adjust, edit.curve)
            t = time.perf_counter()
            if exact:
                rgb = chain.exact_u8(plan.model or self.model(plan.model_iso, plan.unit), plan.entry, codes, gains, table)
                ms["bake"] = 0.0
            else:
                bakes = self.stats["bakes"]
                bk = self._bake(plan, PREVIEW_STEP)
                ms["bake"] = (time.perf_counter() - t) * 1000 if self.stats["bakes"] != bakes else 0.0
                t = time.perf_counter()
                rgb = chain.chroma_u8(codes, bk, gains, table)
            ms["apply"] = (time.perf_counter() - t) * 1000
            t = time.perf_counter()
            if crop is not None:
                rgb = np.asarray(crop_image(Image.fromarray(rgb, "RGB"), crop))
            jpeg = _encode_jpeg(rgb, quality)
            ms["encode"] = (time.perf_counter() - t) * 1000
        ms["total"] = (time.perf_counter() - t0) * 1000
        self.stats["renders"] += 1
        h, w = rgb.shape[:2]
        self._results.put((key, quality), (jpeg, w, h), len(jpeg))
        return RenderOut(jpeg, w, h, key, False, plan, ms)

    # ---- 导出 -------------------------------------------------------------
    def renderer(self, shot: dict[Any, Any], edit: EditState, *, precise: bool = False) -> Renderer:
        """给 `export.export_shot` 用的渲染器：条带逐块调用 `display`。导出文件名和 EXIF 都写明"电脑预测"。"""
        plan = self.plan(shot, edit)
        gains = channel_gains(edit.adjust)
        table = display_table(edit.adjust, edit.curve)
        if precise:
            model = plan.model or self.model(plan.model_iso, plan.unit)

            def display(codes: np.ndarray) -> np.ndarray:
                return chain.exact_u8(model, plan.entry, codes, gains, table)
        else:
            bk = chain.bake_chroma(plan.model or self.model(plan.model_iso, plan.unit), plan.entry, EXPORT_STEP)

            def display(codes: np.ndarray) -> np.ndarray:
                return chain.chroma_u8(codes, bk, gains, table)

        desc = (
            f"GXR Studio computer prediction (not camera-measured): {plan.unit} recipe {plan.slot:02d}"
            f"{' (stock 04 simulation)' if plan.defaulted else ''}, ISO model {plan.model_iso}"
            f"{'' if plan.calibrated else ', NOT calibrated for this lens unit/ISO'}"
            f"{', exact chain' if precise else ''}. {plan.label}"
            f" Record SHA256 {plan.record_sha256}; entry SHA256 {plan.entry_sha256}; binding {plan.binding_sha256}."
        )
        return Renderer(
            id="recipe-prediction" if plan.calibrated else "recipe-prediction-uncalibrated",
            label=plan.label,
            filename_tag="",
            calibrated=plan.calibrated,
            display=display,
            description=desc,
            recipe_binding={
                "record_sha256": plan.record_sha256,
                "entry_sha256": plan.entry_sha256,
                "binding_sha256": plan.binding_sha256,
                "selection_id": plan.selection_id or "",
                "quality_state": (plan.quality or {}).get("state", "unattached"),
                "assessed_entry": "1" if (plan.quality or {}).get("assessed_entry") else "0",
            }
            if plan.selection_id
            else None,
        )

    # ---- 缩略图 / 默认外观 --------------------------------------------------
    def thumb_variant(self, shot: dict[Any, Any]) -> str:
        """DNG-only 缩略图缓存的变体名：相机数据变了（或单元不同）就是另一张图。"""
        if not self.available():
            return "n"
        unit = self.photo_unit(shot) or "jlaunch"
        return f"r{unit[0]}{self._camera.manifest_sha256[:8]}{(self._calib_manifest_sha or '')[:4]}"  # type: ignore[union-attr]

    def default_look_label(self, shot: dict[Any, Any]) -> str | None:
        """`default_look` 画出来的图该怎么标：不可用（调用方退回中性转换）返回 None。永远是"电脑预测"，不是相机 JPG。"""
        self._refresh()
        key = (self.photo_unit(shot), shot.get("iso"), self._state_token)
        with self._lock:
            if key in self._look_labels:
                return self._look_labels[key]
        try:
            plan = self.plan(shot, EditState())
        except (RenderUnavailable, ValueError):
            label = None
        else:
            label = CALIBRATED_LABEL if plan.calibrated else UNCALIBRATED_LABEL
        with self._lock:
            if len(self._look_labels) > 256:
                self._look_labels.clear()
            self._look_labels[key] = label
        return label

    def default_look(self, shot: dict[Any, Any], codes: np.ndarray) -> np.ndarray | None:
        """没有任何编辑时 DNG 的样子：该单元的原厂 04 电脑模拟。不可用时返回 None（调用方退回中性转换）。"""
        try:
            plan = self.plan(shot, EditState())
        except (RenderUnavailable, ValueError):
            return None
        return chain.chroma_u8(codes, self._bake(plan, PREVIEW_STEP), None, None)
