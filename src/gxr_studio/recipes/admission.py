"""冻结D65门的完整AND；只在唯一训练胜者封存以后运行。"""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable
from typing import Any

import numpy as np

from ..engine import colour
from ..engine import matrix_model as mm
from ..fit_contexts.types import FrozenContext, SampleGroup
from .fit import FitCancelled, Winner, default_plan
from .fixtures import guard
from .numerics import FullProblem, integer_objective, literal_mapped, predict_mapped
from .record import check_record
from .types import GateResult, GateState, Quality, RecipeError, canonical, digest

ORDERS = ("YCbCr8_then_RGB8", "RGB8_then_YCbCr8")
PHASES = tuple((y, cb, cr) for y, cb, cr in itertools.product(range(4), repeat=3))


def quantization_keys(role: str) -> tuple[str, ...]:
    return tuple(f"quant/{role}/{order}/{a}{b}{c}" for order in ORDERS for a, b, c in PHASES)


def required_checks(roles: tuple[str, ...]) -> tuple[str, ...]:
    keys = [
        "record",
        "input_mapping",
        "provenance",
        "primary_source",
        "other_iso",
        "scenes",
        "integer_training",
        "integer_objective",
        "baseline_objective",
        "exact_starts",
        "winner_continuous",
        "literal_forward",
    ]
    for role in roles:
        keys.extend((f"guard/{role}", f"nominal/{role}", f"primary/{role}", *quantization_keys(role)))
    return tuple(keys)


def phase_output(rgb: np.ndarray, phase: tuple[int, int, int], order: str) -> np.ndarray:
    if order not in ORDERS or phase not in PHASES:
        raise ValueError("unknown quantization branch")
    if order == "RGB8_then_YCbCr8":
        rgb = np.rint(rgb)
    y, chroma = mm.ycbcr(rgb)
    ycc = np.stack([y, chroma.real + 128, chroma.imag + 128], axis=1)
    nearest = np.rint(ycc)
    ycc = np.where(abs(ycc - nearest) < 1e-9, nearest, ycc)
    quantized = np.clip(np.floor(ycc + np.asarray(phase) / 4), 0, 255)
    return np.rint(np.clip(mm.rgb_from_ycc(quantized[:, 0], (quantized[:, 1] - 128) + 1j * (quantized[:, 2] - 128)), 0, 255))


def _metric(error: np.ndarray, denominator: np.ndarray) -> dict[str, Any]:
    median = float(np.median(error))
    base = float(np.median(denominator))
    return {
        "n": len(error),
        "median": median,
        "p95": float(np.percentile(error, 95)),
        "similarity_percent": None if base < 0.25 else 100 * (1 - median / base),
    }


def assess_groups(
    unit: str,
    groups: dict[str, SampleGroup],
    photos: list[dict[str, Any]],
    before: dict[str, np.ndarray],
    after: dict[str, np.ndarray],
) -> dict[str, Any]:
    failures, results = [], {}
    for name, values in groups.items():
        target = colour.lab_from_codes(values.want)
        denominator = colour.delta_e00(colour.lab_from_codes(values.base_new), target)
        old = colour.delta_e00(colour.lab_from_codes(before[name]), target)
        new = colour.delta_e00(colour.lab_from_codes(after[name]), target)
        ids = values.photo_id.astype(int)
        pids = np.unique(ids)
        segments = [np.flatnonzero(ids == pid) for pid in pids]
        maximum = max(map(len, segments))
        indices = np.zeros((len(segments), maximum), int)
        valid = np.zeros_like(indices, bool)
        for i, segment in enumerate(segments):
            indices[i, : len(segment)], valid[i, : len(segment)] = segment, True
        denphoto = np.nanmedian(np.where(valid, denominator[indices], np.nan), axis=1)
        x0, x1 = np.where(valid, old[indices], np.nan), np.where(valid, new[indices], np.nan)
        med0, med1 = np.nanmedian(x0, axis=1), np.nanmedian(x1, axis=1)
        p950, p951 = np.nanpercentile(x0, 95, axis=1), np.nanpercentile(x1, 95, axis=1)
        defined = denphoto >= 0.25
        none_ok = (med1 <= med0 + np.maximum(32 * abs(np.spacing(med0)), 1e-13)) & (
            p951 <= p950 + np.maximum(32 * abs(np.spacing(p950)), 1e-13)
        )
        if not none_ok[~defined].all():
            failures.append(f"{name}/None_absolute")
        delta = None
        if defined.any():
            s0, s1 = 100 * (1 - med0[defined] / denphoto[defined]), 100 * (1 - med1[defined] / denphoto[defined])
            delta = float(np.median(s1) - np.median(s0))
            if unit == "l08firm" and delta < -0.1 - 1e-12:
                failures.append(f"{name}/0.1pp")
        current, candidate = _metric(old, denominator), _metric(new, denominator)
        if unit == "qlaunch" and (candidate["median"] > current["median"] + 1e-10 or candidate["p95"] > current["p95"] + 0.25 + 1e-10):
            failures.append(f"{name}/overall_median_p95")
        scenes: list[tuple[str, np.ndarray]] = []
        if name == "photo_iso200":
            labels = np.array([photos[int(pid)].get("scene", "") for pid in ids])
            scenes = [(label, np.flatnonzero(labels == label)) for label in sorted(set(labels)) if label]
        elif name == "new7_RAW_target":
            scenes = [("whole_collection", np.arange(len(ids)))]
        scene_results = []
        for scene, segment in scenes:
            a0, a1 = _metric(old[segment], denominator[segment]), _metric(new[segment], denominator[segment])
            passes = all(a1[k] <= a0[k] + 1e-10 for k in ("median", "p95"))
            if not passes:
                failures.append(f"{name}/scene/{scene}")
            scene_results.append({"scene": scene, "current": a0, "candidate": a1, "passes": passes})
        results[name] = {
            "current": current,
            "candidate": candidate,
            "photo_median_delta_pp": delta,
            "photos": len(pids),
            "None_photos": int((~defined).sum()),
            "scenes": scene_results,
            "per_photo": [
                {
                    "photo_id": int(pid),
                    "current_median": float(med0[j]),
                    "candidate_median": float(med1[j]),
                    "current_p95": float(p950[j]),
                    "candidate_p95": float(p951[j]),
                    "current_similarity": None if not defined[j] else float(100 * (1 - med0[j] / denphoto[j])),
                    "candidate_similarity": None if not defined[j] else float(100 * (1 - med1[j] / denphoto[j])),
                }
                for j, pid in enumerate(pids)
            ],
        }
    return {"passes": bool(results) and not failures, "failures": failures, "groups": results, "metric": "frozen engine D65 DE00"}


def _primary(values: SampleGroup, before: np.ndarray, after: np.ndarray, *, strict: bool) -> bool:
    target = colour.lab_from_codes(values.want)
    old = colour.delta_e00(colour.lab_from_codes(before), target)
    new = colour.delta_e00(colour.lab_from_codes(after), target)
    median_ok = np.median(new) < np.median(old) - 1e-9 if strict else np.median(new) <= np.median(old) + 1e-10
    return bool(median_ok and np.percentile(new, 95) <= np.percentile(old, 95) + 0.25 + 1e-10)


def _exact(receipt: dict[str, Any]) -> tuple[GateState, dict[str, Any]]:
    starts = [s for s in receipt.get("starts", []) if s.get("phase") == "exact"]
    attempts = [a for a in receipt.get("attempts", []) if a.get("id", "").startswith("exact/")]
    if not starts or not attempts:
        return "missing", {"reason": "no exact starts or attempts"}
    start_keys, attempt_keys = [s["id"] for s in starts], [a["id"] for a in attempts]
    distinct = len(set(start_keys)) == len(start_keys) and len(set(attempt_keys)) == len(attempt_keys)
    complete = distinct and set(start_keys) == set(attempt_keys)
    pinned = all(
        s.get("settings") == default_plan()["settings"] and digest(np.array(s["vector"]).tobytes()) == s["vector_sha256"] for s in starts
    )
    passed = complete and pinned and all(a.get("success") is True for a in attempts)
    return ("pass" if passed else "fail" if complete else "missing"), {
        "starts": len(starts),
        "attempts": len(attempts),
        "unique": distinct,
        "complete": complete,
        "failed": [a["id"] for a in attempts if a.get("success") is not True],
        "pinned": pinned,
    }


def evaluate_winner(
    context: FrozenContext,
    winner: Winner,
    groups: dict[str, SampleGroup],
    references: dict[str, bytes],
    *,
    cancelled: Callable[[], bool] = lambda: False,
    progress: Callable[[dict[str, Any]], None] = lambda _value: None,
) -> Quality:
    training = context.training
    model = training.environment.model_for(200)[0]
    manifest, receipt = context.document, json.loads(winner.receipt)
    roles = tuple(role for role in ("original", "additional", "replacement") if role in references)
    expected = required_checks(roles)
    checks = []

    def add(key: str, passes: bool, detail: Any = None, *, missing: bool = False) -> None:
        checks.append(GateResult(key, "missing" if missing else "pass" if passes else "fail", canonical(detail or {})))

    check_record(winner.entry)
    add("record", digest(winner.entry) == receipt.get("record_sha256"), {"sha256": digest(winner.entry)})
    all_groups = {"train": training.samples, **groups}
    mapping = True
    for values in all_groups.values():
        try:
            values.verify_model(training.environment)
        except RecipeError:
            mapping = False
    add("input_mapping", mapping)
    add(
        "provenance",
        bool(manifest.get("sources")) and receipt.get("training_identity") == training.identity,
        {"context": context.identity, "target": digest(canonical(manifest["target"]))},
    )
    add("primary_source", "photo_iso200" in groups, missing="photo_iso200" not in groups)
    add("other_iso", "photo_other_iso" in groups, {"scope": "primary-model transfer diagnosis"}, missing="photo_other_iso" not in groups)
    scene_missing = [item for item in manifest["missing"] if "/scene/" in item]
    add("scenes", not scene_missing, {"missing": scene_missing}, missing=bool(scene_missing) or "photo_iso200" not in groups)

    def loss(entry: bytes) -> float:
        return integer_objective(model, entry, training.samples.x, training.samples.want, training.samples.sw)

    candidate_loss, original_loss, stock_loss = loss(winner.entry), loss(training.original), loss(training.environment.stock)
    add(
        "integer_training",
        candidate_loss < min(original_loss, stock_loss) - 1e-9,
        {"candidate": candidate_loss, "original": original_loss, "factory04": stock_loss},
    )
    add("integer_objective", abs(candidate_loss - receipt["objective"]) < 1e-8)
    add("baseline_objective", abs(original_loss - receipt["baseline_objective"]) < 1e-8)
    state, exact = _exact(receipt)
    checks.append(GateResult("exact_starts", state, canonical(exact)))
    successful = {a["id"] for a in receipt.get("attempts", []) if a.get("success") is True and a["id"].startswith("exact/")}
    add("winner_continuous", receipt.get("winner_attempt") in successful and receipt.get("phase") == "exact")
    problem = FullProblem(training, np.asarray(receipt["winner_axis"]))
    vector = np.asarray(receipt["winner_vector"])
    encoded = problem.encode(vector)
    reference = problem.predict(vector, training.samples.x, quantized=True)
    literal = predict_mapped(model, winner.entry, training.samples.x)
    maximum = float(abs(reference - literal).max())
    cross = max(float(abs(predict_mapped(model, winner.entry, g.x) - model.predict(winner.entry, g.raw_wb)).max()) for g in all_groups.values())
    independent = max(
        float(abs(predict_mapped(model, winner.entry, g.x) - literal_mapped(model, winner.entry, g.x)).max()) for g in all_groups.values()
    )
    add(
        "literal_forward",
        encoded == winner.entry and maximum < 1e-7 and cross < 1e-8 and independent < 1e-8,
        {"record_reference_rgb_max": maximum, "model_rgb_max": cross, "independent_literal_rgb_max": independent},
    )
    outputs = {name: predict_mapped(model, winner.entry, values.x) for name, values in groups.items()}
    for role in roles:
        if cancelled():
            raise FitCancelled
        reference_record = references[role]
        guarded = guard(model, winner.entry, reference_record)
        add(f"guard/{role}", bool(guarded["passes"]), guarded)
        before = {name: predict_mapped(model, reference_record, values.x) for name, values in groups.items()}
        nominal = assess_groups(
            training.unit, groups, manifest["photos"], {k: np.rint(v) for k, v in before.items()}, {k: np.rint(v) for k, v in outputs.items()}
        )
        add(f"nominal/{role}", nominal["passes"], nominal, missing=not groups)
        primary = groups.get("photo_iso200")
        add(
            f"primary/{role}",
            primary is not None
            and _primary(primary, np.rint(before["photo_iso200"]), np.rint(outputs["photo_iso200"]), strict=role == "original"),
            missing=primary is None,
        )
        for order in ORDERS:
            for phase in PHASES:
                if cancelled():
                    raise FitCancelled
                old = {k: phase_output(v, phase, order) for k, v in before.items()}
                new = {k: phase_output(v, phase, order) for k, v in outputs.items()}
                result = assess_groups(training.unit, groups, manifest["photos"], old, new)
                primary_ok = primary is not None and _primary(primary, old["photo_iso200"], new["photo_iso200"], strict=False)
                add(
                    f"quant/{role}/{order}/{phase[0]}{phase[1]}{phase[2]}",
                    result["passes"] and primary_ok,
                    {**result, "primary_overall": primary_ok},
                    missing=primary is None,
                )
                if len(checks) % 32 == 0:
                    progress({"stage": "admission", "role": role, "completed_checks": len(checks), "total_checks": len(expected)})
    return Quality(expected, tuple(checks))
