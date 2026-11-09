"""路线 1：旋钮拟合（ARCHITECTURE §6.4）。对所选单元的每个底子，在 `final.json` 允许的**整数档位**上搜索，
让"底子 + 旋钮 + Amount"的电脑预测输出最接近 LUT 的理想输出。

算法（有界、确定性、不动 DAT 结构）：
  A. 排序：每个底子先在 Amount × 色调的粗网格上选 3 个起点，再各做两轮粗档位（每 3 档取 1 档）坐标下降，用约 320 个训练色样；
  B. 精修：取排名前 K 的底子，在全部合法档位上做坐标下降（最多 4 轮，一轮没有改进就停），再做"两个旋钮一起动"的成对搜索
     （对比、高光、阴影这些旋钮会互相抵消，单个旋钮的坐标下降会卡在半路），交替几轮直到没有改进；
  C. 验收：留出样本（合成色样里隔 4 取 1；至少两张照片时最后一张整张）只在这里用，搜索全程没见过。
每一步 = `CameraData.resolve`（`knobs.apply`，档位不合法直接报错，不钳位）+ `Model.predict` + ΔE00。

目标函数（只用于搜索）：loss = 0.5 × 加权平均 ΔE00 + 0.5 × 中位 ΔE00（权重：灰阶 1.5、肤色 2、其它 1）。

相似度（显示用，和 lut_convert 同一个公式）：
    相似度 = 1 − median(候选→目标 ΔE00) / median(04→目标 ΔE00)        （都在留出样本上）
分母的中位数 < 0.25 时 LUT 本来就和 04 几乎一样，相似度没有意义，显示 N/A。可以是负数（比 04 还远）。
标签（DECISIONS #4）：≥ 90% "接近"，≥ 80% "大致"，其余 "差别明显"。一律是"电脑预测"。
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass
from typing import Any, Callable, cast

import numpy as np

from ..engine import colour
from ..render.params import EditError, RecipeSel
from ..render.recipes import SLOT_SHORT
from .scene import GROUP_NAMES, Samples

SLOTS = tuple(range(1, 9))
TONE = ("amount", "contrast", "highlight", "shadow", "fade")
CONTROL_ORDER = ("amount", "contrast", "highlight", "shadow", "fade", "saturation", "warmth", "tint", "split", "filter")
MIN_DENOMINATOR = 0.25
LABEL_CLOSE, LABEL_ROUGH = 90.0, 80.0
FORMULA = "相似度 = 1 − median(候选→目标 ΔE00) / median(04→目标 ΔE00)，在留出样本上算；分母中位数 < 0.25 时显示 N/A"
HUE_NAMES = ("红橙", "橙黄", "黄绿", "绿", "青", "蓝", "紫", "洋红")
HUE_EDGES = (10, 50, 85, 125, 175, 225, 285, 335)

PROFILES = {
    # top_bases：精修几个底子；coarse_n：排序阶段的样本数；sweeps：精修最多几轮
    # starts：灰阶网格上取几个起点；full_starts：色调解出来的几个候选放开全部旋钮精修
    "normal": {
        "top_bases": 3,
        "coarse_n": 320,
        "mid_n": 520,
        "rank_sweeps": 2,
        "sweeps": 3,
        "rounds": 3,
        "starts": 6,
        "full_starts": 2,
    },
    "quick": {
        "top_bases": 2,
        "coarse_n": 160,
        "mid_n": 280,
        "rank_sweeps": 1,
        "sweeps": 2,
        "rounds": 2,
        "starts": 3,
        "full_starts": 1,
    },
}


class Cancelled(Exception):
    pass


def label_for(similarity: float | None) -> str | None:
    if similarity is None:
        return None
    return "接近" if similarity >= LABEL_CLOSE else "大致" if similarity >= LABEL_ROUGH else "差别明显"


def stats_of(de: np.ndarray) -> dict[Any, Any]:
    return {
        "median": float(np.median(de)),
        "p90": float(np.percentile(de, 90)),
        "mean": float(np.mean(de)),
        "n": int(len(de)),
    }


def similarity_of(base_median: float, cand_median: float) -> float | None:
    if base_median < MIN_DENOMINATOR:
        return None
    return float(100.0 * (1.0 - cand_median / base_median))


@dataclass
class Progress:
    phase: str = "准备"
    fraction: float = 0.0
    message: str = ""
    evaluations: int = 0
    best: dict[Any, Any] | None = None

    def to_dict(self) -> dict[Any, Any]:
        return {
            "phase": self.phase,
            "fraction": round(self.fraction, 4),
            "message": self.message,
            "evaluations": self.evaluations,
            "best": self.best,
        }


class Fitter:
    def __init__(
        self,
        cam: Any,
        model: Any,
        samples: Samples,
        stock_rgb: np.ndarray,
        ideal_rgb: np.ndarray,
        *,
        unit: str = "jlaunch",
        profile: str = "normal",
        progress: Callable[[Progress], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        self.cam, self.model, self.samples = cam, model, samples
        self.unit = unit
        self.unit_index = ("jlaunch", "qlaunch", "l08firm").index(unit)
        self.cfg = PROFILES[profile]
        self.profile = profile
        self._progress_cb, self._cancelled = progress, cancelled
        self.prog = Progress()
        self.ideal_lab = colour.lab_from_codes(ideal_rgb)
        self.stock_lab = colour.lab_from_codes(stock_rgb)
        self.stock_de = colour.delta_e00(self.stock_lab, self.ideal_lab)
        self.train = np.flatnonzero(~samples.holdout)
        self.hold = np.flatnonzero(samples.holdout)
        self.coarse = self.train[np.linspace(0, len(self.train) - 1, min(self.cfg["coarse_n"], len(self.train))).astype(int)]
        self.mid = self.train[np.linspace(0, len(self.train) - 1, min(self.cfg["mid_n"], len(self.train))).astype(int)]
        neutral = self.train[samples.group[self.train] == 0]
        self.neutral = neutral if len(neutral) >= 20 else self.coarse  # 灰阶不受饱和度/色温旋钮影响，先用它定色调
        self.neutral_small = self.neutral[::4] if len(self.neutral) >= 80 else self.neutral
        self._grid: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
        self.stock04 = cam.stock[self.unit_index]
        self._cache: dict[tuple[Any, ...], float] = {}
        self._last_emit = 0.0

    # ---- 基础 -------------------------------------------------------------
    def controls(self, slot: int) -> list[dict[Any, Any]]:
        by_name = {c["name"]: c for c in self.cam.controls(self.unit_index, slot)}
        return [by_name[n] for n in CONTROL_ORDER if n in by_name]

    def defaults(self, slot: int) -> dict[Any, Any]:
        return {c["name"]: c["default"] for c in self.controls(slot)}

    def entry_of(self, slot: int, levels: dict[Any, Any]) -> bytes:
        return cast(bytes, self.cam.resolve(RecipeSel(self.unit, slot, tuple(sorted(levels.items())))).entry)

    def pred_lab(self, slot: int, levels: dict[Any, Any], idx: np.ndarray) -> np.ndarray:
        entry = self.entry_of(slot, levels)
        return colour.lab_from_codes(self.model.predict(entry, self.samples.x[idx]))

    def loss(self, slot: int, levels: dict[Any, Any], idx: np.ndarray, tag: str) -> float:
        key = (slot, tuple(sorted(levels.items())), tag)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        if self._cancelled is not None and self._cancelled():
            raise Cancelled()
        try:
            de = colour.delta_e00(self.pred_lab(slot, levels, idx), self.ideal_lab[idx])
            w = self.samples.weight[idx]
            value = float(0.5 * np.average(de, weights=w) + 0.5 * np.median(de))
        except (EditError, ValueError):
            value = float("inf")  # 这个组合算不出来（例如色表行不对称）：当作最差
        self._cache[key] = value
        self.prog.evaluations += 1
        return value

    def emit(self, force: bool = False) -> None:
        now = time.monotonic()
        if self._progress_cb is not None and (force or now - self._last_emit > 0.25):
            self._last_emit = now
            self._progress_cb(self.prog)

    # ---- 局部搜索的积木 --------------------------------------------------------
    def _ctrl(self, slot: int, names: Any) -> list[dict[Any, Any]]:
        return [c for c in self.controls(slot) if names is None or c["name"] in names]

    def _tick(self, span: Any, frac: float, text: str) -> None:
        self.prog.fraction = span[0] + (span[1] - span[0]) * min(1.0, frac)
        self.prog.message = text
        self.emit()

    def descend(
        self,
        slot: int,
        levels: dict[Any, Any],
        idx: np.ndarray,
        tag: str,
        *,
        names: Any = None,
        coarse: bool = False,
        max_sweeps: int = 4,
        span: tuple[Any, ...] = (0.0, 0.0),
        label: str = "",
    ) -> tuple[dict[Any, Any], float]:
        """坐标下降：每次只动一个旋钮，试它所有合法档位（coarse 时每 3 档取 1 档）；严格更好才换。"""
        ctrl = self._ctrl(slot, names)
        best = self.loss(slot, levels, idx, tag)
        for sweep in range(max_sweeps):
            improved = False
            for ci, c in enumerate(ctrl):
                allowed = c["levels"]
                if coarse and c["name"] != "amount":
                    allowed = sorted(set(allowed[::3]) | {levels[c["name"]], c["default"]})
                for lv in allowed:
                    if lv == levels[c["name"]]:
                        continue
                    trial = {**levels, c["name"]: lv}
                    value = self.loss(slot, trial, idx, tag)
                    if value < best - 1e-9:
                        levels, best, improved = trial, value, True
                self._tick(span, (sweep + (ci + 1) / len(ctrl)) / max_sweeps, f"{label}：第 {sweep + 1} 轮")
            if not improved:
                break
        return levels, best

    PAIR_STEPS = (
        (1, 1),
        (1, -1),
        (-1, 1),
        (-1, -1),
        (1, 2),
        (2, 1),
        (1, -2),
        (2, -1),
        (-1, 2),
        (-2, 1),
        (-1, -2),
        (-2, -1),
    )

    def pairs(
        self,
        slot: int,
        levels: dict[Any, Any],
        idx: np.ndarray,
        tag: str,
        *,
        names: Any = None,
        span: tuple[Any, ...] = (0.0, 0.0),
        label: str = "",
    ) -> tuple[dict[Any, Any], float]:
        """成对搜索：两个旋钮一起动 1~2 档（按各自合法档位的次序，到顶标记处走不过去）；对比 / 高光 / 阴影三个一起动 ±1
        （三者合成同一条色调曲线，沿"山谷"走要一起变）。严格更好才换。"""
        ctrl = self._ctrl(slot, names)
        best = self.loss(slot, levels, idx, tag)
        total = max(1, len(ctrl) * (len(ctrl) - 1) // 2)
        done = 0
        for i in range(len(ctrl)):
            for j in range(i + 1, len(ctrl)):
                ci, cj = ctrl[i], ctrl[j]
                pi, pj = ci["levels"].index(levels[ci["name"]]), cj["levels"].index(levels[cj["name"]])
                pick = None
                for si, sj in self.PAIR_STEPS:
                    ni, nj = pi + si, pj + sj
                    if not (0 <= ni < len(ci["levels"]) and 0 <= nj < len(cj["levels"])):
                        continue
                    trial = {**levels, ci["name"]: ci["levels"][ni], cj["name"]: cj["levels"][nj]}
                    value = self.loss(slot, trial, idx, tag)
                    if value < best - 1e-9:
                        best, pick = value, trial
                if pick is not None:
                    levels = pick
                done += 1
                self._tick(span, done / total, f"{label}：成对搜索")
        tone = [c for c in ctrl if c["name"] in ("contrast", "highlight", "shadow")]
        if len(tone) == 3:
            for _ in range(8):
                pos = [c["levels"].index(levels[c["name"]]) for c in tone]
                pick = None
                for steps in itertools.product((-1, 0, 1), repeat=3):
                    new_pos = [p + st for p, st in zip(pos, steps)]
                    if not any(steps) or not all(0 <= n < len(c["levels"]) for n, c in zip(new_pos, tone)):
                        continue
                    trial = {**levels, **{c["name"]: c["levels"][n] for n, c in zip(new_pos, tone)}}
                    value = self.loss(slot, trial, idx, tag)
                    if value < best - 1e-9:
                        best, pick = value, trial
                if pick is None:
                    break
                levels = pick
        return levels, best

    def local(
        self, slot: int, levels: dict[Any, Any], idx: np.ndarray, tag: str, *, names: Any = None, rounds: int, span: Any, label: str
    ) -> tuple[Any, ...]:
        """单旋钮 → 成对 → 单旋钮 …，交替到没有改进（最多 rounds 轮）。"""
        n = 2 * rounds + 1

        def seg(k: Any) -> tuple[Any, ...]:
            return (span[0] + (span[1] - span[0]) * k / n, span[0] + (span[1] - span[0]) * (k + 1) / n)

        levels, best = self.descend(slot, levels, idx, tag, names=names, max_sweeps=self.cfg["sweeps"], span=seg(0), label=label)
        for r in range(rounds):
            before = dict(levels)
            levels, _ = self.pairs(slot, levels, idx, tag, names=names, span=seg(1 + 2 * r), label=label)
            levels, best = self.descend(slot, levels, idx, tag, names=names, max_sweeps=self.cfg["sweeps"], span=seg(2 + 2 * r), label=label)
            if levels == before:
                break
        return levels, best

    def grid_starts(self, slot: int, keep: int) -> list[dict[Any, Any]]:
        """Amount × 色调三旋钮（对比/高光/阴影，每 4 档取 1 档）的粗网格：损失函数沿"山谷"有脊，Amount 和色调又互相补偿，
        坐标下降从默认值出发会卡在半路，所以先全局扫一遍，取最好的几个点当起点。
        只用灰阶评分：饱和度、色温这些旋钮不改变灰阶，不会和色调搅在一起。"""
        key = (slot, keep)
        cached = self._grid.get(key)
        if cached is not None:
            return cached
        ctrl = self.controls(slot)
        base = self.defaults(slot)
        tone = [c for c in ctrl if c["name"] in ("contrast", "highlight", "shadow")]
        amounts = [None]
        if ctrl[0]["name"] == "amount":
            amounts = ctrl[0]["levels"] if len(ctrl[0]["levels"]) <= 9 else ctrl[0]["levels"][::2]
        scored: list[tuple[Any, ...]] = []
        for amount in amounts:
            b = base if amount is None else {**base, "amount": amount}
            for combo in itertools.product(*[c["levels"][::4] for c in tone]):
                trial = {**b, **{c["name"]: lv for c, lv in zip(tone, combo)}}
                scored.append((self.loss(slot, trial, self.neutral, "n"), len(scored), trial))
        scored.sort(key=lambda r: r[:2])
        picked = cast(list[dict[str, Any]], [r[2] for r in scored[:keep]])
        self._grid[key] = picked
        return picked

    # ---- 总流程 -------------------------------------------------------------
    def run(self) -> dict[Any, Any]:
        t0 = time.perf_counter()
        cfg = self.cfg
        # A. 给 8 个底子排序：网格上最好的起点 + 粗档位坐标下降
        ranked = []
        for i, slot in enumerate(SLOTS):
            span = (0.2 * i / len(SLOTS), 0.2 * (i + 1) / len(SLOTS))
            self.prog.phase = "比较内置配方"
            start = self.grid_starts(slot, cfg["starts"])[0]
            levels, value = self.descend(
                slot,
                start,
                self.coarse,
                "c",
                coarse=True,
                max_sweeps=cfg["rank_sweeps"],
                span=span,
                label=f"比较内置配方 {slot:02d}",
            )
            ranked.append((value, slot))
            if self.prog.best is None or value < self.prog.best["loss"]:
                self.prog.best = {"slot": slot, "loss": round(value, 3)}
        ranked.sort()
        # B. 精修排名靠前的底子：先在灰阶上多起点解 Amount+色调，再放开全部旋钮
        finalists = []
        k = cfg["top_bases"]
        for j, (_, slot) in enumerate(ranked[:k]):
            lo, hi = 0.2 + 0.8 * j / k, 0.2 + 0.8 * (j + 1) / k
            self.prog.phase = "精修旋钮"
            tone_names = [c["name"] for c in self.controls(slot) if c["name"] in TONE]
            solved = []
            for n, start in enumerate(self.grid_starts(slot, cfg["starts"])):
                a, b = lo + (hi - lo) * 0.3 * n / cfg["starts"], lo + (hi - lo) * 0.3 * (n + 1) / cfg["starts"]
                solved.append(
                    self.local(
                        slot,
                        start,
                        self.neutral_small,
                        "s",
                        names=tone_names,
                        rounds=cfg["rounds"],
                        span=(a, b),
                        label=f"精修 {slot:02d} 色调",
                    )
                )
            solved.sort(key=lambda r: r[1])
            picks, seen = [], set()
            for lv, _ in solved:
                key = tuple(sorted(lv.items()))
                if key not in seen:
                    seen.add(key)
                    picks.append(lv)
            picks = picks[: cfg["full_starts"]]
            best = None
            for n, start in enumerate(picks):
                a = lo + (hi - lo) * (0.3 + 0.7 * n / len(picks))
                b = lo + (hi - lo) * (0.3 + 0.7 * (n + 1) / len(picks))
                got = self.local(slot, start, self.mid, "m", rounds=cfg["rounds"], span=(a, b), label=f"精修 {slot:02d}")
                if best is None or got[1] < best[1]:
                    best = got
            if best is None:
                raise RuntimeError(f"slot {slot} produced no candidate")
            levels = best[0]
            finalists.append((self.loss(slot, levels, self.train, "t"), slot, levels))
            self.prog.best = {"slot": slot, "loss": round(min(f[0] for f in finalists), 3)}
            self.emit(True)
        cands = [self.describe(slot, levels) for _, slot, levels in finalists]
        cands.sort(key=lambda c: (-(c["similarity"] if c["similarity"] is not None else -1e9), c["slot"]))
        self.prog.fraction, self.prog.phase, self.prog.message = 1.0, "完成", ""
        self.emit(True)
        return self.result(cands, time.perf_counter() - t0)

    # ---- 结果 ---------------------------------------------------------------
    def describe(self, slot: int, levels: dict[Any, Any]) -> dict[Any, Any]:
        allx = np.arange(len(self.samples))
        lab = self.pred_lab(slot, levels, allx)
        de = colour.delta_e00(lab, self.ideal_lab)
        hold, train = de[self.hold], de[self.train]
        base_hold = self.stock_de[self.hold]
        sim = similarity_of(float(np.median(base_hold)), float(np.median(hold)))
        entry = self.entry_of(slot, levels)
        import hashlib

        return {
            "unit": self.unit,
            "slot": slot,
            "number": f"{slot:02d}",
            "short": SLOT_SHORT[slot - 1],
            "levels": {k: int(v) for k, v in levels.items()},
            "amount": int(levels["amount"]) if "amount" in levels else None,
            "similarity": sim,
            "label": label_for(sim),
            "holdout": stats_of(hold),
            "train": stats_of(train),
            "baseline04_holdout": stats_of(base_hold),
            "per_group": {GROUP_NAMES[g]: stats_of(de[self.samples.group == g]) for g in range(4) if (self.samples.group == g).any()},
            "differences": explain(lab, self.ideal_lab, self.samples.group),
            "entry_sha256": hashlib.sha256(entry).hexdigest(),
        }

    def result(self, cands: list[dict[Any, Any]], seconds: float) -> dict[Any, Any]:
        return {
            "schema": "gxr-studio/lut-fit/1",
            "prediction": True,
            "label": "电脑预测",
            "caution": "",
            "formula": FORMULA,
            "thresholds": {"close": LABEL_CLOSE, "rough": LABEL_ROUGH},
            "best": cands[0],
            "candidates": cands,
            "profile": self.profile,
            "samples": {**self.samples.counts(), "photos": self.samples.photos},
            "evaluations": self.prog.evaluations,
            "seconds": round(seconds, 2),
        }


# ---- "差别在哪" --------------------------------------------------------------------


def _cast(da: float, db: float, floor: float = 2.0) -> str | None:
    """候选相对目标的色偏（Lab a/b 的中位差）→ 一个词。"""
    if max(abs(da), abs(db)) < floor:
        return None
    if abs(da) < 0.5 * abs(db):
        return "偏黄" if db > 0 else "偏蓝"
    if abs(db) < 0.5 * abs(da):
        return "偏红（品红）" if da > 0 else "偏绿"
    return {(1, 1): "偏橙红", (-1, 1): "偏黄绿", (-1, -1): "偏青", (1, -1): "偏紫"}[(1 if da > 0 else -1, 1 if db > 0 else -1)]


def explain(pred_lab: np.ndarray, ideal_lab: np.ndarray, group: np.ndarray, limit: int = 4) -> list[str]:
    """把候选和目标的差别翻成几句大白话（候选 − 目标；按差别大小排序）。"""
    d = pred_lab - ideal_lab
    L = ideal_lab[:, 0]
    notes: list[tuple[float, str]] = []
    for name, m in (("暗部", L < 35), ("中间调", (L >= 35) & (L < 70)), ("高光", L >= 70)):
        if m.sum() < 8:
            continue
        dl, da, db = np.median(d[m], axis=0)
        cast = _cast(da, db)
        if cast:
            notes.append((float(np.hypot(da, db)), f"{name}{cast}"))
        if abs(dl) >= 2.5:
            notes.append((abs(float(dl)), f"{name}{'偏亮' if dl > 0 else '偏暗'}"))
    pc, ic = np.hypot(pred_lab[:, 1], pred_lab[:, 2]), np.hypot(ideal_lab[:, 1], ideal_lab[:, 2])
    chromatic = ic > 12
    if chromatic.sum() >= 20:
        ratio = float(np.median(pc[chromatic] / ic[chromatic]))
        if ratio < 0.9:
            notes.append((abs(1 - ratio) * 40, "整体饱和度偏低"))
        elif ratio > 1.1:
            notes.append((abs(1 - ratio) * 40, "整体饱和度偏高"))
    ramp = (group == 0) & (L > 15) & (L < 85)
    if ramp.sum() >= 20:
        slope = float(np.polyfit(ideal_lab[ramp, 0], pred_lab[ramp, 0], 1)[0])
        if slope < 0.93:
            notes.append((abs(1 - slope) * 40, "对比偏弱"))
        elif slope > 1.07:
            notes.append((abs(1 - slope) * 40, "对比偏强"))
    de = colour.delta_e00(pred_lab, ideal_lab)
    overall = float(np.median(de))
    hue = np.degrees(np.arctan2(ideal_lab[:, 2], ideal_lab[:, 1])) % 360
    bins = np.digitize(hue, HUE_EDGES) % len(HUE_NAMES)
    for b, name in enumerate(HUE_NAMES):
        m = chromatic & (bins == b)
        if m.sum() >= 15:
            med = float(np.median(de[m]))
            if med > max(3.0, 1.6 * overall):
                notes.append((med, f"{name}色偏差较大"))
    notes.sort(key=lambda n: -n[0])
    out = [t for _, t in notes[:limit]]
    return out


# ---- 入口 --------------------------------------------------------------------------


def fit_lut(cam: Any, model: Any, samples: Samples, stock_rgb: np.ndarray, ideal_rgb: np.ndarray, **kw: Any) -> dict[Any, Any]:
    return Fitter(cam, model, samples, stock_rgb, ideal_rgb, **kw).run()
