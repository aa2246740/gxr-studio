"""证明移植（P1-01）、目标改签旧want（P1-02）与长期依赖完整性（P2-02）真实行为回归。"""

from __future__ import annotations

import dataclasses
import json
import re
import sqlite3
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from conftest import PORT
from full_recipe_support import environment_at, plant_pending, publish_controlled_terminal, quality_pack
from gxr_studio.app import create_app
from gxr_studio.fit_contexts import FitContexts
from gxr_studio.lut.cube import Cube
from gxr_studio.lut.spaces import Target
from gxr_studio.recipes import FitProduct, GateResult, Quality, RecipeError, RecipeRepository
from gxr_studio.recipes.fit import default_plan
from gxr_studio.recipes.types import canonical, digest
from gxr_studio.render.params import parse_recipe
from test_full_recipe_flow import paired_photo
from test_lut import wait
from test_lut_library import cube_bytes, upload


def _assert_rejected_fit_status(payload: dict, expected_phrase: str) -> None:
    assert payload["state"] == "error", payload
    assert payload["error"]["code"] == "invalid_full_result", payload
    message = payload["error"]["message"]
    assert message.startswith("完整配方结果未能核验，无法保存。\n"), message
    assert expected_phrase in message, message
    assert "/" not in message and "\\" not in message, message
    assert re.search(r"[0-9a-f]{12,}", message) is None, message


def _lut_pack_matching_record(folder: Path, environment, *, missing_other: bool = False):
    """构造真实3D LUT及其逐组由Target.render生成的样本语料，使真实worker在当前算法下143项全过。"""
    size = 9
    grid = np.linspace(0.0, 1.0, size)
    mesh = np.stack(np.meshgrid(grid, grid, grid, indexing="ij"), axis=-1).reshape(-1, 3)
    rgb_order = mesh[:, [2, 1, 0]]
    lut_table = np.clip(
        (rgb_order ** np.array([1.12, 0.93, 1.04])) @ np.array([[1.01, -0.01, 0.0], [0.015, 0.99, 0.005], [0.0, 0.01, 0.99]]),
        0.0,
        1.0,
    )
    lines = ['TITLE "current-algorithm-donor"', f"LUT_3D_SIZE {size}"]
    lines.extend(f"{r:.6f} {g:.6f} {b:.6f}" for r, g, b in lut_table)
    raw_cube = ("\n".join(lines) + "\n").encode("utf-8")
    cube = Cube.parse(raw_cube.decode("utf-8-sig"), digest(raw_cube))
    renderer = Target(cube, "srgb", "srgb")
    manifest, _def, _rec, _groups = quality_pack(folder, environment, missing_other=missing_other)
    manifest["target"] = {"kind": "sample-corpus"}
    (folder / "quality-pack.json").write_bytes(canonical(manifest))
    return raw_cube, renderer


def test_relabeled_or_partial_holdout_lut_pack_is_rejected_and_all_groups_bind_target(settings, tmp_path):
    environment = environment_at(settings.home_dir)
    manifest, _old_target, _record, groups = quality_pack(tmp_path / "pack", environment)
    raw = cube_bytes(lambda x: 1.0 - x, size=5, title="different independent target")
    cube = Cube.parse(raw.decode("utf-8-sig"), digest(raw))
    renderer = Target(cube, "srgb", "srgb")
    new_target = {"kind": "lut", "cube_sha256": digest(raw), "spaces": renderer.description()}
    contexts = FitContexts(settings.home_dir)

    first = contexts.import_pack(tmp_path / "pack")["id"]
    with pytest.raises(RecipeError) as unedited_exc:
        contexts.freeze(first, target=new_target, plan=default_plan(), environment=environment, target_renderer=renderer, target_asset=raw)
    assert unedited_exc.value.code == "quality_target_mismatch"

    # P1-02 反例1：只改签 quality-pack.json 的 target 声明，保留全部旧 NPZ want
    relabeled_dir = tmp_path / "pack-relabeled"
    relabeled_dir.mkdir()
    for path in (tmp_path / "pack").iterdir():
        (relabeled_dir / path.name).write_bytes(path.read_bytes())
    relabeled_manifest = json.loads(canonical(manifest))
    relabeled_manifest["target"] = new_target
    (relabeled_dir / "quality-pack.json").write_bytes(canonical(relabeled_manifest))
    with pytest.raises(RecipeError) as relabeled_exc:
        relabeled_id = contexts.import_pack(relabeled_dir)["id"]
        contexts.freeze(
            relabeled_id, target=new_target, plan=default_plan(), environment=environment, target_renderer=renderer, target_asset=raw
        )
    assert relabeled_exc.value.code == "quality_target_mismatch"

    # P1-02 反例2：只更新 train.npz 为新 Target.render，留出组仍保留旧 want
    partial_dir = tmp_path / "pack-partial-holdout"
    partial_dir.mkdir()
    for path in relabeled_dir.iterdir():
        (partial_dir / path.name).write_bytes(path.read_bytes())
    new_train = dataclasses.replace(groups["train"], want=renderer.render(groups["train"].base_new, groups["train"].xyz)).to_bytes()
    (partial_dir / "train.npz").write_bytes(new_train)
    partial_manifest = json.loads((partial_dir / "quality-pack.json").read_bytes())
    partial_manifest["files"]["train.npz"] = {"size": len(new_train), "sha256": digest(new_train)}
    (partial_dir / "quality-pack.json").write_bytes(canonical(partial_manifest))
    with pytest.raises(RecipeError) as partial_exc:
        partial_id = contexts.import_pack(partial_dir)["id"]
        contexts.freeze(partial_id, target=new_target, plan=default_plan(), environment=environment, target_renderer=renderer, target_asset=raw)
    assert partial_exc.value.code == "quality_target_mismatch"

    # 正例：sample-corpus 冻结后，训练与全部留出组 want 均严格等于实际 Target.render
    corpus_dir = tmp_path / "pack-corpus"
    corpus_dir.mkdir()
    for path in (tmp_path / "pack").iterdir():
        (corpus_dir / path.name).write_bytes(path.read_bytes())
    corpus_manifest = json.loads(canonical(manifest))
    corpus_manifest["target"] = {"kind": "sample-corpus"}
    (corpus_dir / "quality-pack.json").write_bytes(canonical(corpus_manifest))
    corpus_id = contexts.import_pack(corpus_dir)["id"]
    frozen = contexts.freeze(
        corpus_id, target=new_target, plan=default_plan(), environment=environment, target_renderer=renderer, target_asset=raw
    )
    assert np.array_equal(frozen.training.samples.want, renderer.render(frozen.training.samples.base_new, frozen.training.samples.xyz))
    eval_groups, _refs = contexts.evaluation(frozen.identity)
    assert set(eval_groups) == {"photo_iso200", "photo_other_iso"}
    for group in eval_groups.values():
        assert np.array_equal(group.want, renderer.render(group.base_new, group.xyz))


def _wait_worker_file(service, job_id: str, timeout: float = 240.0) -> Path:
    proc = service._procs[job_id]
    exit_code = proc.wait(timeout=timeout)
    assert exit_code == 0, f"worker {job_id} exited with {exit_code}"
    job_dir = service.jobs_dir / job_id
    assert (job_dir / "result.json").is_file()
    return job_dir


def test_real_worker_proof_transplant_and_check_flip_are_rejected_while_draft_remains_needs_evidence(settings, tmp_path):
    environment = environment_at(settings.home_dir)
    raw_donor, _renderer = _lut_pack_matching_record(tmp_path / "donor-quality", environment)
    _lut_pack_matching_record(tmp_path / "draft-quality", environment, missing_other=True)
    base_url = f"http://127.0.0.1:{PORT}"
    headers = {"X-GXR-Token": settings.token}

    with TestClient(create_app(settings), base_url=base_url, headers=headers) as client:
        service = client.app.state.ctx.lut

        # 1. 真正生产 worker 在当前算法下产出全绿 donor 正例
        donor_cube = upload(client, raw_donor, "donor.cube").json()["cube_id"]
        donor_source = client.post("/api/lut/quality", json={"folder": str(tmp_path / "donor-quality")}).json()["id"]
        donor_job = client.post("/api/lut/fit", json={"cube_id": donor_cube, "quality_source": donor_source}).json()["job_id"]
        donor_dir = _wait_worker_file(service, donor_job)
        donor_proof_bytes = (donor_dir / "quality.json").read_bytes()
        donor_terminal = wait(client, donor_job, limit=50)
        assert donor_terminal["state"] == "done", donor_terminal
        donor_saved = client.post("/api/my-recipes", json={"job_id": donor_job, "name": "当前算法正例"})
        assert donor_saved.status_code == 200, donor_saved.text
        assert donor_saved.json()["quality"]["state"] == "passed"

        # 2. P1-01 反例A：真正生产 worker 跑恒等 LUT（stock_loss=0 无法严格优于原厂04，真实结论必为 rejected），归档前单独移植 donor proof
        rx_raw = cube_bytes(lambda x: x, size=5, title="identity receiver")
        rx_cube = upload(client, rx_raw, "receiver.cube").json()["cube_id"]
        rx_job = client.post("/api/lut/fit", json={"cube_id": rx_cube, "quality_source": donor_source}).json()["job_id"]
        rx_dir = _wait_worker_file(service, rx_job)
        original_rx_quality = Quality.from_json(json.loads((rx_dir / "quality.json").read_bytes()))
        assert original_rx_quality.state == "rejected"
        rx_result = json.loads((rx_dir / "result.json").read_bytes())
        (rx_dir / "quality.json").write_bytes(donor_proof_bytes)
        rx_result["quality_sha256"] = digest(donor_proof_bytes)
        (rx_dir / "result.json").write_bytes(canonical(rx_result))
        rx_status = client.get(f"/api/lut/fit/{rx_job}")
        assert rx_status.status_code == 200
        _assert_rejected_fit_status(rx_status.json(), "身份绑定")
        assert client.post("/api/my-recipes", json={"job_id": rx_job, "name": "真实receiver移植证明"}).status_code == 409

        # 3. P1-01 反例B：无 exact 尝试的受控 receiver 单独移植 donor proof，以及同身份仅把 check 全翻成 pass
        no_exact_run = "fb0000000001"
        plant_pending(service, rx_cube, no_exact_run)
        no_exact_result = publish_controlled_terminal(service, no_exact_run)
        (service.jobs_dir / no_exact_run / "quality.json").write_bytes(donor_proof_bytes)
        no_exact_result["quality_sha256"] = digest(donor_proof_bytes)
        (service.jobs_dir / no_exact_run / "result.json").write_bytes(canonical(no_exact_result))
        _assert_rejected_fit_status(client.get(f"/api/lut/fit/{no_exact_run}").json(), "身份绑定")
        assert client.post("/api/my-recipes", json={"job_id": no_exact_run, "name": "无exact移植"}).status_code == 409

        flipped_run = "fb0000000002"
        plant_pending(service, rx_cube, flipped_run)
        flipped_result = publish_controlled_terminal(service, flipped_run)
        flipped_dir = service.jobs_dir / flipped_run
        flipped_quality = json.loads((flipped_dir / "quality.json").read_bytes())
        flipped_quality["checks"] = [{**c, "state": "pass"} for c in flipped_quality["checks"]]
        flipped_bytes = canonical(flipped_quality)
        (flipped_dir / "quality.json").write_bytes(flipped_bytes)
        flipped_result["quality_sha256"] = digest(flipped_bytes)
        (flipped_dir / "result.json").write_bytes(canonical(flipped_result))
        _assert_rejected_fit_status(client.get(f"/api/lut/fit/{flipped_run}").json(), "检查结论或明细")
        assert client.post("/api/my-recipes", json={"job_id": flipped_run, "name": "翻绿检查"}).status_code == 409

        rebound_run = "fb0000000003"
        plant_pending(service, rx_cube, rebound_run)
        rebound_result = publish_controlled_terminal(service, rebound_run)
        rebound_dir = service.jobs_dir / rebound_run
        rx_own_q = json.loads((rebound_dir / "quality.json").read_bytes())
        rx_id_details = {c["key"]: c["detail"] for c in rx_own_q["checks"] if c["key"] in {"record", "provenance"}}
        forged_donor_q = json.loads(donor_proof_bytes)
        forged_donor_q["binding"] = rx_own_q["binding"]
        forged_donor_q["checks"] = [
            {**c, "detail": rx_id_details[c["key"]]} if c["key"] in rx_id_details else c for c in forged_donor_q["checks"]
        ]
        forged_donor_bytes = canonical(forged_donor_q)
        (rebound_dir / "quality.json").write_bytes(forged_donor_bytes)
        rebound_result["quality_sha256"] = digest(forged_donor_bytes)
        (rebound_dir / "result.json").write_bytes(canonical(rebound_result))
        rebound_status = client.get(f"/api/lut/fit/{rebound_run}").json()
        _assert_rejected_fit_status(rebound_status, "检查结论或明细")
        assert client.post("/api/my-recipes", json={"job_id": rebound_run, "name": "改写绑定仍移植"}).status_code == 409

        # 3b. P1-01 七项绑定轴篡改（record / context / target / environment / receipt / plan / source）逐项拒收
        donor_entry = (donor_dir / "candidate.record").read_bytes()
        other_sha = digest(b"tampered-axis-value")
        for idx, (axis, phrase) in enumerate(
            (
                ("record", "不匹配"),
                ("context", "身份不符"),
                ("target", "身份绑定"),
                ("environment", "身份绑定"),
                ("receipt", "不匹配"),
                ("plan", "不匹配"),
                ("source", "不匹配"),
            ),
            start=11,
        ):
            axis_run = f"fb00000000{idx:02d}"
            plant_pending(service, rx_cube, axis_run)
            axis_res = publish_controlled_terminal(service, axis_run)
            axis_dir = service.jobs_dir / axis_run
            if axis == "record":
                (axis_dir / "candidate.record").write_bytes(donor_entry)
                axis_res["record_sha256"] = digest(donor_entry)
                seal = json.loads((axis_dir / "winner-seal.json").read_bytes())
                seal["record_sha256"] = digest(donor_entry)
                (axis_dir / "winner-seal.json").write_bytes(canonical(seal))
                (axis_dir / "result.json").write_bytes(canonical(axis_res))
            elif axis == "context":
                axis_res["context"] = other_sha
                (axis_dir / "result.json").write_bytes(canonical(axis_res))
            elif axis in ("target", "environment"):
                q_doc = json.loads((axis_dir / "quality.json").read_bytes())
                q_doc["binding"][f"{axis}_sha256"] = other_sha
                q_bytes = canonical(q_doc)
                (axis_dir / "quality.json").write_bytes(q_bytes)
                axis_res["quality_sha256"] = digest(q_bytes)
                (axis_dir / "result.json").write_bytes(canonical(axis_res))
            elif axis in ("receipt", "plan"):
                w_doc = json.loads((axis_dir / "winner.json").read_bytes())
                if axis == "receipt":
                    w_doc["training_identity"] = other_sha
                else:
                    w_doc["plan"] = {**w_doc["plan"], "iterate_nfev": 999}
                w_bytes = canonical(w_doc)
                (axis_dir / "winner.json").write_bytes(w_bytes)
                axis_res["receipt_sha256"] = digest(w_bytes)
                seal = json.loads((axis_dir / "winner-seal.json").read_bytes())
                seal["receipt_sha256"] = digest(w_bytes)
                (axis_dir / "winner-seal.json").write_bytes(canonical(seal))
                (axis_dir / "result.json").write_bytes(canonical(axis_res))
            elif axis == "source":
                job_doc = json.loads((axis_dir / "job.json").read_bytes())
                job_doc["source"]["title"] = "tampered-source-title"
                (axis_dir / "job.json").write_bytes(canonical(job_doc))
            _assert_rejected_fit_status(client.get(f"/api/lut/fit/{axis_run}").json(), phrase)
            assert client.post("/api/my-recipes", json={"job_id": axis_run, "name": f"篡改{axis}"}).status_code == 409

        # 4. 正向草稿语义：缺 photo_other_iso 的真实 worker 运行仍可 done 并保存为 needs_evidence 草稿，但绝不是 passed
        draft_source = client.post("/api/lut/quality", json={"folder": str(tmp_path / "draft-quality")}).json()["id"]
        draft_job = client.post("/api/lut/fit", json={"cube_id": donor_cube, "quality_source": draft_source}).json()["job_id"]
        draft_terminal = wait(client, draft_job, limit=900)
        assert draft_terminal["state"] == "done", draft_terminal
        draft_saved = client.post("/api/my-recipes", json={"job_id": draft_job, "name": "缺项草稿"}).json()
        assert draft_saved["quality"]["state"] == "needs_evidence"


def test_corrupted_or_swapped_repository_dependencies_fail_explicitly_across_restart_and_http(settings, tmp_path):
    environment = environment_at(settings.home_dir)
    raw_donor, _renderer = _lut_pack_matching_record(tmp_path / "donor-quality", environment)
    _lut_pack_matching_record(tmp_path / "draft-quality", environment, missing_other=True)
    base_url = f"http://127.0.0.1:{PORT}"
    headers = {"X-GXR-Token": settings.token}

    with TestClient(create_app(settings), base_url=base_url, headers=headers) as client:
        paired_photo(client.app.state.ctx.lib, "proof1")
        donor_cube = upload(client, raw_donor, "donor.cube").json()["cube_id"]
        donor_source = client.post("/api/lut/quality", json={"folder": str(tmp_path / "donor-quality")}).json()["id"]
        donor_job = client.post("/api/lut/fit", json={"cube_id": donor_cube, "quality_source": donor_source}).json()["job_id"]
        assert wait(client, donor_job, limit=900)["state"] == "done"
        admitted = client.post("/api/my-recipes", json={"job_id": donor_job, "name": "持久正例"}).json()
        assert admitted["quality"]["state"] == "passed"
        assert client.put("/api/photos/proof1/edit", json={"recipe": admitted["recipe"], "expected_rev": 0}).status_code == 200

        # 同时保存另一条真实 worker 草稿配方（不同记录与目标），供跨记录替换 receipt / quality 反例检验
        other_cube = upload(client, cube_bytes(lambda x: np.clip(x * 0.92 + 0.03, 0.0, 1.0), size=5), "draft.cube").json()["cube_id"]
        draft_source = client.post("/api/lut/quality", json={"folder": str(tmp_path / "draft-quality")}).json()["id"]
        draft_job = client.post("/api/lut/fit", json={"cube_id": other_cube, "quality_source": draft_source}).json()["job_id"]
        assert wait(client, draft_job, limit=900)["state"] == "done"
        other_saved = client.post("/api/my-recipes", json={"job_id": draft_job, "name": "另一配方"}).json()
        assert other_saved["quality"]["state"] == "needs_evidence"
        assert other_saved["record_sha256"] != admitted["record_sha256"]

    selection = admitted["recipe"]
    other_selection = other_saved["recipe"]
    db_bytes = (settings.home_dir / "recipes/recipes.sqlite").read_bytes()

    def _tamper_clone(name: str, mutate) -> Path:
        clone_home = tmp_path / name
        (clone_home / "recipes").mkdir(parents=True)
        db = sqlite3.connect(":memory:")
        db.deserialize(db_bytes)
        doc = json.loads(db.execute("SELECT document FROM versions WHERE id=?", (admitted["id"],)).fetchone()[0])
        other_doc = json.loads(db.execute("SELECT document FROM versions WHERE id=?", (other_saved["id"],)).fetchone()[0])
        mutate(db, doc, other_doc)
        db.commit()
        (clone_home / "recipes/recipes.sqlite").write_bytes(db.serialize())
        db.close()
        return clone_home

    # 1. 只损坏正例配方的真实 receipt BLOB 并重开仓库
    corrupt_receipt_home = _tamper_clone(
        "corrupt-receipt-home",
        lambda db, doc, _other: db.execute(
            "UPDATE objects SET data=? WHERE kind='receipt' AND sha=?",
            (b"damaged receipt blob", doc["receipt"]),
        ),
    )
    reopened = RecipeRepository(corrupt_receipt_home)
    for fn in (
        lambda: reopened.get(admitted["id"]),
        reopened.listing,
        lambda: reopened.resolve(parse_recipe(selection), photo_unit="jlaunch"),
    ):
        with pytest.raises(RecipeError) as exc:
            fn()
        assert exc.value.code == "recipe_asset_missing"

    # 2. 只损坏/删除正例配方的真实 source BLOB 并重开仓库
    corrupt_source_home = _tamper_clone(
        "corrupt-source-home",
        lambda db, doc, _other: db.execute(
            "UPDATE objects SET data=? WHERE kind='source' AND sha=?",
            (b"damaged source blob", doc["source"]),
        ),
    )
    reopened_source = RecipeRepository(corrupt_source_home)
    for fn in (
        lambda: reopened_source.get(admitted["id"]),
        reopened_source.listing,
        lambda: reopened_source.resolve(parse_recipe(selection), photo_unit="jlaunch"),
    ):
        with pytest.raises(RecipeError) as src_exc:
            fn()
        assert src_exc.value.code == "recipe_asset_missing"

    # 3. 跨记录把正例 quality 嫁接到另一条未通过记录，以及跨记录把另一条 receipt 换到正例记录，均必须拒收
    def _swap_quality_and_receipt(db, doc, other_doc):
        forged_q = {**other_doc, "quality": doc["quality"]}
        q_payload = canonical(forged_q)
        q_id = digest(q_payload)
        db.execute("INSERT INTO versions VALUES (?,?,?,?)", (q_id, forged_q["unit"], forged_q["slot"], q_payload))
        db.execute("INSERT INTO names VALUES (?,?,?,?,1,0,'2026-11-11T00:00:00+0800')", (q_id, "swap00000001", "嫁接Q", "嫁接Q"))

        forged_r = {**doc, "receipt": other_doc["receipt"]}
        r_payload = canonical(forged_r)
        r_id = digest(r_payload)
        db.execute("INSERT INTO versions VALUES (?,?,?,?)", (r_id, forged_r["unit"], forged_r["slot"], r_payload))
        db.execute("INSERT INTO names VALUES (?,?,?,?,1,0,'2026-11-11T00:00:01+0800')", (r_id, "swap00000002", "嫁接R", "嫁接R"))

    swapped_home = _tamper_clone("swapped-quality-home", _swap_quality_and_receipt)
    reopened_swap = RecipeRepository(swapped_home)
    with pytest.raises(RecipeError):
        reopened_swap.listing()
    conn = sqlite3.connect(swapped_home / "recipes/recipes.sqlite")
    q_id = conn.execute("SELECT id FROM names WHERE run='swap00000001'").fetchone()[0]
    r_id = conn.execute("SELECT id FROM names WHERE run='swap00000002'").fetchone()[0]
    conn.close()
    for bad_id in (q_id, r_id):
        with pytest.raises(RecipeError):
            reopened_swap.get(bad_id)
        with pytest.raises(RecipeError):
            reopened_swap.resolve(parse_recipe({**other_selection, "id": bad_id}), photo_unit="jlaunch")

    # 4. 分别覆盖原 home_dir 的 sqlite 为仅损坏 receipt BLOB、再仅损坏 source BLOB 的副本，模拟两次真实重启后的 HTTP / 渲染 / 导出边界
    for tampered_home in (corrupt_receipt_home, corrupt_source_home):
        (settings.home_dir / "recipes/recipes.sqlite").write_bytes((tampered_home / "recipes/recipes.sqlite").read_bytes())
        with TestClient(create_app(settings), base_url=base_url, headers=headers) as restarted:
            assert restarted.get(f"/api/my-recipes/{admitted['id']}").status_code == 422
            assert restarted.get("/api/my-recipes").status_code == 422
            edit_view = restarted.get("/api/photos/proof1/edit")
            assert edit_view.status_code == 200
            assert edit_view.json()["render"]["available"] is False
            assert edit_view.json()["render"]["reason"] == "recipe_asset_missing"
            assert edit_view.json()["local_recipe"] is None
            assert restarted.get("/api/photos/proof1/bake", params={"recipe": json.dumps(selection)}).status_code == 409
            assert restarted.get("/api/photos/proof1/render").status_code == 409
            assert restarted.post("/api/photos/proof1/output-size", json={}).status_code == 409
            assert restarted.post("/api/photos/proof1/export", json={"format": "png", "quality": 95}).status_code == 409

    # 5. 直接向仓库塞入未绑定/伪造全绿 Quality 必须拒收
    clean_repo = RecipeRepository(tmp_path / "clean-repo")
    clean_repo.start_run("000000000099", digest(b"unbound-ctx"))
    forged_product = FitProduct(
        "jlaunch",
        1,
        environment.stock,
        environment.data,
        digest(b"unbound-ctx"),
        canonical({"kind": "lut", "cube_sha256": digest(b"cube")}),
        Quality(("only_gate",), (GateResult("only_gate", "pass"),)),
        canonical({"solver": "none"}),
    )
    with pytest.raises(RecipeError):
        clean_repo.retain_fit("000000000099", forged_product)


def test_truncated_or_malformed_candidate_record_and_result_fail_cleanly_across_restarts(settings):
    environment_at(settings.home_dir)
    base_url = f"http://127.0.0.1:{PORT}"
    headers = {"X-GXR-Token": settings.token}

    with TestClient(create_app(settings), base_url=base_url, headers=headers) as setup_client:
        cube_id = upload(setup_client, cube_bytes(lambda x: x, size=3, title="short-record-repro"), "short.cube").json()["cube_id"]
        service = setup_client.app.state.ctx.lut
        bad_records = (
            ("fc0000000008", b"", "552字节"),
            ("fc0000000009", b"\x07", "552字节"),
            ("fc0000000010", bytes([1, 0]) + b"\x00" * 550, "完整彩色或黑白字段"),
        )
        for run, raw_record, _phrase in bad_records:
            plant_pending(service, cube_id, run)
            result_doc = publish_controlled_terminal(service, run)
            folder = service.jobs_dir / run
            rec_sha = digest(raw_record)
            (folder / "candidate.record").write_bytes(raw_record)

            winner_doc = json.loads((folder / "winner.json").read_bytes())
            winner_doc["record_sha256"] = rec_sha
            winner_bytes = canonical(winner_doc)
            (folder / "winner.json").write_bytes(winner_bytes)

            quality_doc = json.loads((folder / "quality.json").read_bytes())
            quality_doc["binding"]["record_sha256"] = rec_sha
            quality_doc["binding"]["receipt_sha256"] = digest(winner_bytes)
            quality_bytes = canonical(quality_doc)
            (folder / "quality.json").write_bytes(quality_bytes)

            seal_bytes = canonical({"context": result_doc["context"], "record_sha256": rec_sha, "receipt_sha256": digest(winner_bytes)})
            (folder / "winner-seal.json").write_bytes(seal_bytes)

            result_doc["record_sha256"] = rec_sha
            result_doc["receipt_sha256"] = digest(winner_bytes)
            result_doc["quality_sha256"] = digest(quality_bytes)
            (folder / "result.json").write_bytes(canonical(result_doc))

        for run, bad_text in (
            ("fc0000000011", "[1, 2]\n"),
            ("fc0000000012", '{"state": "error", "code": "nan_payload", "value": NaN}\n'),
        ):
            plant_pending(service, cube_id, run)
            (service.jobs_dir / run / "result.json").write_text(bad_text, encoding="utf-8")

    expected_cases = [
        ("fc0000000008", "552字节", "bad_record"),
        ("fc0000000009", "552字节", "bad_record"),
        ("fc0000000010", "完整彩色或黑白字段", "bad_record"),
        ("fc0000000011", "格式无效", None),
        ("fc0000000012", "格式无效", None),
    ]
    for restart in (1, 2):
        with TestClient(create_app(settings), base_url=base_url, headers=headers, raise_server_exceptions=False) as client:
            svc = client.app.state.ctx.lut
            for run, phrase, reason_code in expected_cases:
                status = client.get(f"/api/lut/fit/{run}")
                cancel = client.post(f"/api/lut/fit/{run}/cancel")
                save = client.post("/api/my-recipes", json={"job_id": run, "name": "bad-record"})
                assert status.status_code == 200, (restart, run, status.text)
                assert cancel.status_code == 200, (restart, run, cancel.text)
                assert save.status_code == 409, (restart, run, save.text)
                _assert_rejected_fit_status(status.json(), phrase)
                assert cancel.json()["state"] == "error"
                archived = svc.recipes.run(run)
                assert archived is not None and archived["state"] == "error"
                assert archived["detail"]["code"] == "invalid_full_result"
                if reason_code is not None:
                    assert archived["detail"].get("reason_code") == reason_code
            alerts = [a["job_id"] for a in client.app.state.ctx.alerts.recent() if a["handler"] == "on_job_failed"]
            if restart == 1:
                assert sorted(alerts) == [run for run, _, _ in expected_cases]
            else:
                assert alerts == []
