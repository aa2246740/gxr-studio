"""实际完整求解，非旧旋钮物化，并验证留出恶化不改变训练路线。"""

from __future__ import annotations

import json

from full_recipe_support import environment_at, quality_pack
from gxr_studio.engine.matrix_model import MatrixRow
from gxr_studio.fit_contexts import FitContexts
from gxr_studio.recipes.admission import evaluate_winner, quantization_keys
from gxr_studio.recipes.fit import default_plan, solve_full
from gxr_studio.recipes.numerics import integer_objective
from gxr_studio.recipes.types import digest


def test_actual_full_solver_and_holdout_isolation(settings, tmp_path):
    environment = environment_at(settings.home_dir)
    _manifest, target, target_entry, _groups = quality_pack(tmp_path / "pack-a", environment)
    # 所有合成旧底子相同，彩色旧旋钮不会改Y行；黑白滤镜把Y行和固定为1024。
    # 这个完整目标的Y行和不为1024，并含独立RGB曲线和不同扇区，不能物化为旧旋钮记录。
    assert MatrixRow.from_bytes(target_entry[216:244]).coef[0].sum() != 1024
    contexts = FitContexts(settings.home_dir)
    imported = contexts.import_pack(tmp_path / "pack-a")
    first = contexts.freeze(imported["id"], target=target, plan=default_plan(), environment=environment)
    quality_pack(tmp_path / "pack-b", environment, holdout_target=environment.stock)
    imported_b = contexts.import_pack(tmp_path / "pack-b")
    second = contexts.freeze(imported_b["id"], target=target, plan=default_plan(), environment=environment)
    assert first.identity != second.identity and first.training.identity == second.training.identity
    seals = []
    winner = solve_full(first.training, seal=lambda value: seals.append(digest(value.entry)))
    replay = solve_full(second.training)
    assert seals == [digest(winner.entry)] and replay.entry == winner.entry
    receipt, replay_receipt = json.loads(winner.receipt), json.loads(replay.receipt)
    print(
        json.dumps(
            {
                "baseline": receipt["baseline_objective"],
                "objective": receipt["objective"],
                "origin": receipt["origin"],
                "attempts": [{k: a[k] for k in ("id", "success", "nfev", "cost", "message")} for a in receipt["attempts"]],
                "invalid_integer_candidates": len(receipt["invalid_integer_candidates"]),
            }
        )
    )
    assert receipt["starts"] == replay_receipt["starts"]
    assert receipt["record_reference_rgb_max"] < 1e-7
    assert winner.entry[1] == 7 and winner.entry[379:412] != winner.entry[82:115]
    assert MatrixRow.from_bytes(winner.entry[216:244]).coef[0].sum() != 1024
    model = environment.model_for(200)[0]
    train = first.training.samples
    loss = integer_objective(model, winner.entry, train.x, train.want, train.sw)
    assert loss < receipt["baseline_objective"] - 1e-9
    assert abs(loss - receipt["objective"]) < 1e-8
    groups, references = contexts.evaluation(first.identity)
    original_proof = evaluate_winner(first, winner, groups, references)
    assert set(quantization_keys("original")) <= {c.key for c in original_proof.checks}
    bad_groups, bad_references = contexts.evaluation(second.identity)
    bad_proof = evaluate_winner(second, replay, bad_groups, bad_references)
    assert "primary/original" in bad_proof.failures
    # 替换参照必须完整参与所有门，不能只沿用原优化参照的通过结论。
    replacement_proof = evaluate_winner(first, winner, groups, {**references, "replacement": target_entry})
    assert set(quantization_keys("replacement")) <= {c.key for c in replacement_proof.checks}
    assert "primary/replacement" in replacement_proof.failures
