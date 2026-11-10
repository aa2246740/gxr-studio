"""只读训练输入的完整iterate/exact求解和唯一整数胜者。

本产品路线明确使用可移植SciPy reference TRF/SVD完成两个阶段，不声称执行
研究的fast normal-matrix kernel。起点、容差、正则、33点整数回溯固定在训练前。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from scipy.optimize import least_squares

from ..fit_contexts.types import TrainingInput
from .fixtures import GuardedProblem, guard
from .numerics import FullProblem, curve_x, integer_objective, predict_mapped
from .types import RecipeError, canonical, digest

SOLVER_CONTRACT = "gxr-studio/full-reference-fit/1"
SETTINGS = {"ftol": 2e-6, "xtol": 2e-6, "gtol": 2e-5, "x_scale": "jac", "method": "trf", "tr_solver": "exact"}


class FitCancelled(Exception):
    pass


@dataclass(frozen=True)
class Winner:
    entry: bytes
    receipt: bytes


@dataclass
class _Candidate:
    entry: bytes
    problem: FullProblem
    theta: np.ndarray
    origin: str
    attempt: str
    loss: float


def default_plan() -> dict[str, Any]:
    return {
        "contract": SOLVER_CONTRACT,
        "iterate_nfev": 160,
        "exact_nfev": 160,
        "settings": dict(SETTINGS),
        "backtracking_points": 33,
        "stock_sector_seed": "exact_identity_for_equal_integer_words",
        "source_sha256": {
            name: digest(Path(__file__).with_name(name).read_bytes()) for name in ("fit.py", "numerics.py", "fixtures.py", "admission.py")
        },
    }


def _starts(problem: FullProblem, records: tuple[bytes, ...], iterate: _Candidate | None) -> list[tuple[str, np.ndarray]]:
    result = [
        (f"integer_{j}", problem.seed(entry)) for j, entry in enumerate(records) if j == 0 or np.array_equal(curve_x(entry), problem.axis)
    ]
    neutral = np.ptp(problem.x, axis=1) < 1e-8
    if neutral.any():
        desired = problem.want[neutral]
        if problem.nc == 1:
            desired = (desired @ np.array([0.299, 0.587, 0.114]))[:, None]
        order = np.argsort(problem.x[neutral, 0])
        gx = problem.x[neutral, 0][order]
        q = np.array([np.maximum.accumulate(np.interp(problem.axis, gx, desired[order, ch])) for ch in range(problem.nc)])
        start = problem.seed(records[0])
        start[problem.qstart : problem.qend] = (q - problem.stock_curve).ravel()
        result.append(("target_gray_axis", start))
    if iterate is not None and np.array_equal(iterate.problem.axis, problem.axis):
        result.append(("iterate_continuous_winner", iterate.theta))
    distinct: list[tuple[str, np.ndarray]] = []
    for name, start in result:
        start = np.clip(start, problem.lower + 1e-9, problem.upper - 1e-9)
        if not any(np.array_equal(start, old) for _name, old in distinct):
            distinct.append((name, start))
    return distinct


def _choose(candidates: dict[bytes, _Candidate], training: TrainingInput) -> _Candidate:
    model = training.environment.model_for(200)[0]
    ordered = sorted(candidates.values(), key=lambda c: (c.loss, digest(c.entry)))
    for candidate in ordered:
        if guard(model, candidate.entry, training.original)["passes"]:
            return candidate
    # 保留结构合法的最低训练误差诊断结果；准入仍记录固定夹具失败。
    return ordered[0]


def solve_full(
    training: TrainingInput,
    *,
    cancelled: Callable[[], bool] = lambda: False,
    progress: Callable[[dict[str, Any]], None] = lambda _value: None,
    seal: Callable[[Winner], None] = lambda _winner: None,
) -> Winner:
    plan = json.loads(training.plan)
    if plan != default_plan():
        raise RecipeError("unsupported_solver", "这份拟合计划当前不可用，请重新冻结输入。")
    if training.family not in ("color", "bw"):
        raise RecipeError("fixed_native_record", "原生01到04保持原记录，不运行新配方优化。")
    started = time.monotonic()
    records = (training.original, *training.seeds)
    axes: list[tuple[str, np.ndarray]] = []
    for label, entry in (
        ("stock04", training.environment.stock),
        ("original", training.original),
        *[(f"seed{i}", b) for i, b in enumerate(records)],
    ):
        axis = curve_x(entry)
        if not any(np.array_equal(axis, old) for _name, old in axes):
            axes.append((label, axis))
    model = training.environment.model_for(200)[0]
    baseline = integer_objective(model, training.original, training.samples.x, training.samples.want, training.samples.sw)
    stock = integer_objective(model, training.environment.stock, training.samples.x, training.samples.want, training.samples.sw)
    starts: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    iterate: _Candidate | None = None
    winner: _Candidate | None = None
    for phase in ("iterate", "exact"):
        candidates: dict[bytes, _Candidate] = {}
        phase_records = records + ((iterate.entry,) if iterate is not None else ())
        for axis_name, axis in axes:
            problem = FullProblem(training, axis)
            initial = problem.seed(training.original)
            guarded = None if problem.bw else GuardedProblem(problem)
            for label, start in _starts(problem, phase_records, iterate):
                if cancelled():
                    raise FitCancelled
                attempt = f"{phase}/{axis_name}/{label}"
                start_doc = {
                    "id": attempt,
                    "phase": phase,
                    "axis": axis.astype(int).tolist(),
                    "start": label,
                    "vector_sha256": digest(start.tobytes()),
                    "vector": start.tolist(),
                    "bounds_sha256": digest(problem.lower.tobytes() + problem.upper.tobytes()),
                    "max_nfev": plan[f"{phase}_nfev"],
                    "settings": SETTINGS,
                }
                starts.append(start_doc)
                progress({"stage": phase, "start": attempt, "completed_starts": len(attempts)})

                def evaluate(point: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
                    if cancelled():
                        raise FitCancelled
                    return problem.evaluate(point) if guarded is None else guarded.evaluate(point)

                began = time.monotonic()
                optimum = least_squares(
                    lambda p: evaluate(p)[0],
                    start,
                    jac=lambda p: evaluate(p)[1],
                    bounds=(problem.lower, problem.upper),
                    max_nfev=plan[f"{phase}_nfev"],
                    **SETTINGS,
                )
                attempts.append(
                    {
                        "id": attempt,
                        "success": bool(optimum.success),
                        "status": int(optimum.status),
                        "nfev": int(optimum.nfev),
                        "njev": int(optimum.njev),
                        "cost": float(optimum.cost),
                        "optimality": float(optimum.optimality),
                        "message": str(optimum.message),
                        "seconds": time.monotonic() - began,
                        "final_vector_sha256": digest(optimum.x.tobytes()),
                    }
                )
                for ratio in (None, *np.linspace(1.0, 0.0, 33)):
                    if cancelled():
                        raise FitCancelled
                    point = start if ratio is None else initial + ratio * (optimum.x - initial)
                    try:
                        entry = problem.encode(point)
                    except (RecipeError, ValueError) as exc:
                        invalid.append({"attempt": attempt, "ratio": ratio, "reason": str(exc)})
                        continue
                    if entry not in candidates:
                        loss = integer_objective(model, entry, training.samples.x, training.samples.want, training.samples.sw)
                        candidates[entry] = _Candidate(entry, problem, point.copy(), f"{attempt}/{ratio}", attempt, loss)
        if not candidates:
            raise RecipeError("fit_no_valid_record", "完整优化没有产生格式合法的记录。")
        winner = _choose(candidates, training)
        if phase == "iterate":
            iterate = winner
    if winner is None:
        raise RecipeError("fit_no_valid_record", "完整优化没有产生记录。")
    reference_rgb = winner.problem.predict(winner.theta, training.samples.x, quantized=True)
    literal_rgb = predict_mapped(model, winner.entry, training.samples.x)
    maximum = float(abs(reference_rgb - literal_rgb).max())
    receipt = {
        "contract": SOLVER_CONTRACT,
        "phase": "exact",
        "backend": "portable SciPy reference TRF/SVD for iterate and exact",
        "training_identity": training.identity,
        "training_only_selection": True,
        "evaluation_data_used_for_fitting": False,
        "plan": plan,
        "starts": starts,
        "attempts": attempts,
        "invalid_integer_candidates": invalid,
        "objective": winner.loss,
        "baseline_objective": baseline,
        "stock_objective": stock,
        "origin": winner.origin,
        "winner_attempt": winner.attempt,
        "record_sha256": digest(winner.entry),
        "winner_vector": winner.theta.tolist(),
        "winner_axis": winner.problem.axis.astype(int).tolist(),
        "record_reference_rgb_max": maximum,
        "parameters": winner.problem.parameters(winner.theta),
        "source_sha256": plan["source_sha256"],
        "seconds": time.monotonic() - started,
    }
    result = Winner(winner.entry, canonical(receipt))
    # 生产worker持久封存后才加载evaluation；优化器没有context路径。
    seal(result)
    return result
