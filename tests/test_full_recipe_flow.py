"""实际worker/API/重启/Undo/CPU/GPU/RAW成片，输入和图像均现场合成。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import unquote

import numpy as np
from fastapi.testclient import TestClient
from PIL import Image

from conftest import PORT, REAL_HOME
from full_recipe_support import environment_at, quality_pack
from gpu_gate import evaluate
from gpu_support import _Server
from gxr_studio.app import create_app
from gxr_studio.gpu.source import raw_layout
from gxr_studio.library.store import FileRecord, ShotRecord, sha256_file
from gxr_studio.raw import half_res_codes, open_dng, orient_array, render_full
from gxr_studio.render.chain import chroma_u8
from gxr_studio.render.params import EditState, parse_recipe
from test_clone_coverage import _commit_dng, _tiny_dng
from test_lut import wait
from test_lut_library import cube_bytes, upload

SUPPORT = Path(__file__).parent / "support"


def paired_photo(lib, identity):
    shot = _commit_dng(lib, _tiny_dng(w=64, h=48), identity)
    rel = f"{shot['folder']}/{identity}.JPG"
    path = lib.root / rel
    Image.new("RGB", (64, 48), (220, 15, 35)).save(path, quality=95)
    files = [
        FileRecord(k, f["relpath"], f["sha256"], f["size"], lib.abspath(f["relpath"]).stat().st_mtime_ns) for k, f in shot["files"].items()
    ]
    files.append(FileRecord("jpg", rel, sha256_file(path), path.stat().st_size, path.stat().st_mtime_ns))
    lib.commit_shot(
        ShotRecord(
            identity,
            identity,
            shot["folder"],
            shot["date"],
            shot["date_source"],
            shot["captured_at"],
            shot["unit"],
            shot["lens"],
            shot["iso"],
            1,
            64,
            48,
            shot["added_at"],
            files,
        )
    )
    return lib.get_shot(identity)


def _gpu_pixels(tmp_path, raw, bake, meta, layout):
    case = {
        "id": "actual-local-record",
        "source": "full-raw",
        "raw": {"url": "/raw.dng", "layout": layout},
        "bake": {"url": "/bake.bin", "key": meta["key"], "meta": meta},
        "look": {"recipe": meta["key"], "lut": None, "lutStrength": 1, "adjust": {}, "curve": None},
        "geometry": {"straighten": 0, "rotate90": 0, "flip": False, "box": None},
    }
    files = {"/raw.dng": raw, "/bake.bin": bake, "/cases.json": json.dumps([case]).encode()}
    with _Server(files, SUPPORT / "full_recipe_gpu_harness.mjs") as server:
        result = subprocess.run(
            ["node", str(SUPPORT / "full_recipe_gpu_driver.mjs"), str(REAL_HOME / ".codex/playwright-runtime/runtime.mjs"), server.url],
            capture_output=True,
            text=True,
            timeout=180,
            env={**os.environ, "HOME": str(REAL_HOME)},
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert server.done.is_set() and "error" not in server.summary
        w, h, data = server.results[case["id"]]
        return np.frombuffer(data, np.uint8).reshape(h, w, 4)[..., :3].copy(), server.summary


def test_actual_full_worker_saved_reference_restart_gpu_and_export(settings, tmp_path, record_property):
    environment = environment_at(settings.home_dir)
    quality_pack(tmp_path / "quality", environment)
    manifest_path = tmp_path / "quality/quality-pack.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["target"] = {"kind": "sample-corpus"}
    manifest_path.write_text(json.dumps(manifest))
    base_url = f"http://127.0.0.1:{PORT}"
    headers = {"X-GXR-Token": settings.token}
    with TestClient(create_app(settings), base_url=base_url, headers=headers) as client:
        context = client.app.state.ctx
        paired_photo(context.lib, "full1")
        paired_photo(context.lib, "full2")

        def transform(x):
            return np.clip((x ** np.array([1.12, 0.93, 1.04])) @ np.array([[1.01, -0.01, 0], [0.015, 0.99, 0.005], [0, 0.01, 0.99]]), 0, 1)

        cube = upload(client, cube_bytes(transform, size=9, title="independent RGB target")).json()
        missing = client.post("/api/lut/fit", json={"cube_id": cube["cube_id"]})
        assert missing.status_code == 422 and missing.json()["error"] == "quality_source_required"
        imported = client.post("/api/lut/quality", json={"folder": str(tmp_path / "quality")})
        assert imported.status_code == 200, imported.text
        started = client.post("/api/lut/fit", json={"cube_id": cube["cube_id"], "quality_source": imported.json()["id"]})
        assert started.status_code == 200, started.text
        job = started.json()["job_id"]
        terminal = wait(client, job, limit=900)
        assert terminal["state"] == "done", terminal
        assert len(terminal["result"]["candidates"]) == 1
        candidate = terminal["result"]["candidates"][0]
        assert candidate["recipe"]["kind"] == "local"
        saved_response = client.post("/api/my-recipes", json={"job_id": job, "candidate": 0, "name": "完整RGB"})
        assert saved_response.status_code == 200, saved_response.text
        saved = saved_response.json()
        record_property("record_sha256", saved["record_sha256"])
        record_property("quality", json.dumps(saved["quality"]))
        selection = saved["recipe"]
        assert selection == candidate["recipe"] and len(saved["id"]) == 64
        assert client.post("/api/my-recipes", json={"job_id": job, "candidate": 1, "name": "第二名"}).status_code == 422
        source_before = saved["source"]
        assert client.patch(f"/api/luts/{cube['cube_id']}", json={"author": "changed later"}).status_code == 200
        assert client.delete(f"/api/luts/{cube['cube_id']}").status_code == 200
        assert client.get(f"/api/my-recipes/{saved['id']}").json()["source"] == source_before
        with context.lut.library.private() as directory:
            directory.remove(f"lut_jobs/{job}")
        assert client.get(f"/api/lut/fit/{job}").json()["state"] == "done"
        put = client.put("/api/photos/full1/edit", json={"recipe": selection, "expected_rev": 0})
        assert put.status_code == 200, put.text
        assert put.json()["render"]["record_sha256"] == saved["record_sha256"]
        assert client.put("/api/photos/full1/edit", json={"recipe": selection, "expected_rev": 0}).status_code == 409
        previous = {**selection, "levels": {"contrast": 1}, "memory": {"jlaunch/1": {"contrast": 2}}}
        assert client.put("/api/photos/full2/edit", json={"recipe": previous, "expected_rev": 0}).status_code == 200
        pasted = client.post(
            "/api/edits/paste", json={"groups": ["recipe"], "source": {"recipe": selection}, "targets": [{"id": "full2", "expected_rev": 1}]}
        )
        assert pasted.status_code == 200 and pasted.json()["counts"]["applied"] == 1, pasted.text
        assert client.delete(f"/api/my-recipes/{saved['id']}?expected_revision=1").status_code == 200
        restored = client.post("/api/edits/paste/undo", json={"batch_id": pasted.json()["batch_id"]})
        assert restored.status_code == 200 and restored.json()["counts"]["restored"] == 1
        edit = client.get("/api/photos/full2/edit").json()
        assert edit["recipe"]["id"] == saved["id"] and edit["recipe"]["levels"] == {"contrast": 1}
        assert edit["memory"] == previous["memory"]
    # 当前校准文件消失后，保存版本仍以自己的模型重放；不是重新绑定当前数据。
    shutil.rmtree(settings.home_dir / "calib")
    with TestClient(create_app(settings), base_url=base_url, headers=headers) as client:
        context = client.app.state.ctx
        edit = client.get("/api/photos/full1/edit").json()
        assert edit["recipe"] == selection and edit["render"]["available"]
        assert edit["local_recipe"]["hidden"]
        shot = context.lib.get_shot("full1")
        plan = context.render.plan(shot, EditState(parse_recipe(selection)))
        assert plan.record_sha256 == plan.entry_sha256 == saved["record_sha256"]
        raw_response = client.get("/api/photos/full1/raw")
        bake_response = client.get("/api/photos/full1/bake", params={"recipe": json.dumps(selection)})
        assert raw_response.status_code == bake_response.status_code == 200
        metadata = json.loads(unquote(bake_response.headers["X-GXR-Bake"]))
        assert metadata["record_sha256"] == plan.record_sha256 and metadata["binding_sha256"] == saved["binding_sha256"]
        dng = open_dng(raw_response.content)
        try:
            layout, reason = raw_layout(dng)
            assert layout is not None, reason
            codes = np.ascontiguousarray(orient_array(half_res_codes(dng), dng.orientation), np.float32)
            expected = render_full(dng, context.render.renderer(shot, EditState(parse_recipe(selection))))
        finally:
            dng.release()
        cpu = chroma_u8(codes, context.render.preview_bake(plan), None, None, workers=1)
        gpu, runtime = _gpu_pixels(tmp_path, raw_response.content, bake_response.content, metadata, layout)
        pixel_gate = evaluate(gpu, cpu)
        distinct, spread = np.unique(gpu.reshape(-1, 3), axis=0).shape[0], float(gpu.std())
        record_property("gpu", json.dumps({"gate": pixel_gate, "distinct": distinct, "std": spread, "runtime": runtime}))
        assert pixel_gate["pass"], pixel_gate
        assert distinct >= 12 and spread >= 4
        exported = client.post("/api/photos/full1/export", json={"format": "png", "quality": 95, "size": "original"})
        assert exported.status_code == 200, exported.text
        result = exported.json()
        assert result["prediction"] and result["recipe_binding"]["record_sha256"] == saved["record_sha256"]
        with Image.open(result["path"]) as image:
            output = np.asarray(image.convert("RGB"))
        record_property(
            "export", json.dumps({"shape": list(output.shape), "cpu_mean_abs": float(np.mean(abs(output.astype(float) - expected)))})
        )
        assert output.shape == expected.shape and np.mean(abs(output.astype(float) - expected)) < 5
        assert np.mean(abs(output.astype(float) - np.array([220, 15, 35]))) > 20
        assert client.get("/api/my-recipes").json()["recipes"] == []
        before = sorted(settings.export_dir.iterdir())
        context.lib.abspath(shot["files"]["dng"]["relpath"]).write_bytes(b"damaged RAW")
        failed = client.post("/api/photos/full1/export", json={"format": "png", "quality": 95})
        assert failed.status_code == 409 and failed.json()["error"] == "raw_unreadable", failed.text
        assert sorted(settings.export_dir.iterdir()) == before
