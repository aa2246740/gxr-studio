"""显式质量输入的字节、来源、留出隔离及不可变快照。"""

from __future__ import annotations

import dataclasses
import io
import json
import subprocess
import sys

import numpy as np
import pytest

from full_recipe_support import environment_at, quality_pack
from gxr_studio.fit_contexts import FitContexts
from gxr_studio.recipes import RecipeError
from gxr_studio.recipes.fit import default_plan
from gxr_studio.recipes.types import canonical, digest


def test_pack_import_freeze_coverage_and_read_only_training(settings, tmp_path):
    environment = environment_at(settings.home_dir)
    _manifest, target, _record, _groups = quality_pack(tmp_path / "pack", environment)
    contexts = FitContexts(settings.home_dir)
    imported = contexts.import_pack(tmp_path / "pack")
    assert imported["coverage"] == {"train": 64, "photo_iso200": 64, "photo_other_iso": 64}
    assert imported["missing"] == []
    context = contexts.freeze(imported["id"], target=target, plan=default_plan(), environment=environment)
    assert set(json.loads(context.training.plan)["source_sha256"]) == {"fit.py", "numerics.py", "fixtures.py", "admission.py"}
    assert contexts.open(context.identity).training.identity == context.training.identity
    assert not hasattr(context.training, "holdout") and not hasattr(context.training, "path")
    with pytest.raises(ValueError):
        context.training.samples.x[0, 0] = 1
    with pytest.raises(ValueError):
        context.training.samples.x.setflags(write=True)
    with pytest.raises(RecipeError, match="冻结目标"):
        contexts.freeze(imported["id"], target={"changed": True}, plan=default_plan(), environment=environment)


def test_missing_groups_and_scene_are_evidence_not_pass(settings, tmp_path):
    environment = environment_at(settings.home_dir)
    quality_pack(tmp_path / "pack", environment, missing_other=True, missing_scene=True)
    imported = FitContexts(settings.home_dir).import_pack(tmp_path / "pack")
    assert set(imported["missing"]) == {"photo_other_iso", "photo_iso200/scene/1"}


def test_training_and_holdout_family_or_raw_duplicates_are_rejected(settings, tmp_path):
    environment = environment_at(settings.home_dir)
    manifest, _target, _record, _groups = quality_pack(tmp_path / "pack", environment)
    for key in ("family_sha256", "raw_sha256"):
        changed = json.loads(canonical(manifest))
        changed["photos"][1][key] = changed["photos"][0][key]
        (tmp_path / "pack/quality-pack.json").write_bytes(canonical(changed))
        with pytest.raises(RecipeError, match="同一照片"):
            FitContexts(settings.home_dir).import_pack(tmp_path / "pack")


def test_source_tampering_and_double_mapping_are_rejected(settings, tmp_path):
    environment = environment_at(settings.home_dir)
    manifest, _target, _record, groups = quality_pack(tmp_path / "pack", environment)
    path = tmp_path / "pack/train.npz"
    original = path.read_bytes()
    path.write_bytes(original[:-1] + bytes((original[-1] ^ 1,)))
    with pytest.raises(RecipeError, match="来源摘要"):
        FitContexts(settings.home_dir).import_pack(tmp_path / "pack")
    forged = dataclasses.replace(groups["train"], x=groups["train"].x + 1).to_bytes()
    path.write_bytes(forged)
    manifest["files"]["train.npz"] = {"size": len(forged), "sha256": digest(forged)}
    (tmp_path / "pack/quality-pack.json").write_bytes(canonical(manifest))
    with pytest.raises(RecipeError, match="重复映射"):
        FitContexts(settings.home_dir).import_pack(tmp_path / "pack")


def test_worker_training_open_does_not_read_holdout_before_winner(settings, tmp_path, monkeypatch):
    environment = environment_at(settings.home_dir)
    _manifest, target, _record, _groups = quality_pack(tmp_path / "pack", environment)
    contexts = FitContexts(settings.home_dir)
    imported = contexts.import_pack(tmp_path / "pack")
    context = contexts.freeze(imported["id"], target=target, plan=default_plan(), environment=environment)
    from gxr_studio.privacy import PrivateDir

    read = PrivateDir.read

    def fence(directory, name):
        if "photo_iso200.npz" in name or "photo_other_iso.npz" in name:
            raise AssertionError("holdout was opened before winner sealing")
        return read(directory, name)

    with monkeypatch.context() as patch:
        patch.setattr(PrivateDir, "read", fence)
        opened = contexts.open(context.identity)
        assert np.array_equal(opened.training.samples.x, context.training.samples.x)
    assert set(contexts.evaluation(context.identity)[0]) == {"photo_iso200", "photo_other_iso"}


def test_archived_numeric_source_builds_through_isolated_cli(settings, tmp_path):
    environment = environment_at(settings.home_dir)
    manifest, _target, _record, groups = quality_pack(tmp_path / "example", environment)
    source = tmp_path / "sealed-source"
    source.mkdir()
    members = {"environment.json": environment.data}
    for role, name in manifest["references"].items():
        members[name] = (tmp_path / "example" / name).read_bytes()
    for name, group in groups.items():
        stream = io.BytesIO()
        np.savez(stream, **{key: getattr(group, key) for key in ("raw_wb", "xyz", "sw", "photo_id", "iso")})
        members[f"{name}.npz"] = stream.getvalue()
    description = {
        **manifest,
        "schema": "gxr-studio/sample-source/1",
        "files": {name: {"size": len(data), "sha256": digest(data)} for name, data in members.items()},
    }
    for name, data in members.items():
        (source / name).write_bytes(data)
    (source / "source.json").write_bytes(canonical(description))
    destination = tmp_path / "built-quality"
    result = subprocess.run(
        [sys.executable, "-I", "-m", "gxr_studio.fit_contexts", str(source), str(destination)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert set(json.loads(result.stdout)["groups"]) == {"train", "photo_iso200", "photo_other_iso"}
    imported = FitContexts(settings.home_dir).import_pack(destination)
    assert imported["coverage"] == {"train": 64, "photo_iso200": 64, "photo_other_iso": 64}
    package = json.loads((destination / "quality-pack.json").read_bytes())
    assert package["target"] == {"kind": "sample-corpus"}
    assert package["sources"][-1]["sha256"] == digest(canonical(description))
