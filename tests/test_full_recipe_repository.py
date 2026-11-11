"""长期引用通过真实数据库重开、私有短写和终态交错验证。"""

from __future__ import annotations

import dataclasses
import json
import os
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from gxr_studio.engine import knobs
from gxr_studio.privacy import PrivateDir, fd_path
from gxr_studio.recipes import FitProduct, GateResult, Quality, RecipeError, RecipeRepository, RenderEnvironment
from gxr_studio.recipes.types import canonical, digest
from gxr_studio.render.params import RecipeSel
from gxr_studio.render.recipes import CameraData
from test_clone_coverage import _install_camera, _render_entry


@pytest.fixture
def product(settings):
    entry = bytearray(_render_entry())
    entry[1] = 7
    entry[244:272] = entry[216:244]
    knobs.checksum(entry)
    _install_camera(settings.home_dir, bytes(entry))
    environment = RenderEnvironment.capture(settings.home_dir, CameraData(settings.home_dir), "jlaunch")
    return FitProduct(
        "jlaunch",
        1,
        bytes(entry),
        environment.data,
        digest(b"context"),
        canonical({"cube_sha256": digest(b"cube"), "attribution": {"author": "original author"}}),
        Quality(("private_holdout",), (GateResult("private_holdout", "missing"),)),
        canonical({"solver": "repository test does not claim a solver"}),
    )


def retained(settings, product, run="0123456789ab"):
    repo = RecipeRepository(settings.home_dir)
    repo.start_run(run, product.context)
    state = repo.retain_fit(run, product)
    return repo, state["recipe"]


def test_candidate_and_saved_reference_survive_restart_and_hide(settings, product):
    repo, reference = retained(settings, product)
    selection = RecipeSel("jlaunch", 1, kind="local", id=reference["id"])
    assert repo.resolve(selection).entry == product.entry
    saved = repo.publish("0123456789ab", name="三曲线")
    assert saved["recipe"] == reference
    assert saved["quality"]["state"] == "needs_evidence"
    assert repo.publish("0123456789ab", name="三曲线")["id"] == saved["id"]
    with pytest.raises(RecipeError, match="改名"):
        repo.publish("0123456789ab", name="第二个名字")
    renamed = repo.rename(saved["id"], name="新名", expected_revision=1)
    assert renamed["revision"] == 2 and renamed["record_sha256"] == saved["record_sha256"]
    with pytest.raises(RecipeError, match="刷新"):
        repo.retire(saved["id"], expected_revision=1)
    repo.retire(saved["id"], expected_revision=2)
    reopened = RecipeRepository(settings.home_dir)
    assert reopened.listing() == []
    assert reopened.get(saved["id"])["name"] == "新名"
    renamed_hidden = reopened.rename(saved["id"], name="仍然隐藏", expected_revision=3)
    assert renamed_hidden["hidden"] and reopened.listing() == []
    assert renamed_hidden["record_sha256"] == saved["record_sha256"]
    resolved = reopened.resolve(selection, photo_unit="jlaunch", iso=200)
    assert resolved.entry == product.entry and resolved.assessed_entry and resolved.calibrated
    assert resolved.record_sha256 == resolved.entry_sha256
    assert reopened.run("0123456789ab")["state"] == "done"


def test_cancel_and_done_first_commit_wins_after_restart(settings, product):
    repo = RecipeRepository(settings.home_dir)
    repo.start_run("0123456789ab", product.context)
    repo.finish_run("0123456789ab", "cancelled", {"reason": "user"})
    assert repo.retain_fit("0123456789ab", product)["state"] == "cancelled"
    assert RecipeRepository(settings.home_dir).run("0123456789ab")["state"] == "cancelled"
    with pytest.raises(RecipeError, match="没有完整结果"):
        repo.publish("0123456789ab", name="不可保存")
    repo.start_run("abcdef012345", product.context)
    repo.retain_fit("abcdef012345", product)
    assert repo.finish_run("abcdef012345", "cancelled", {})["state"] == "done"
    assert RecipeRepository(settings.home_dir).publish("abcdef012345", name="已完成")["quality"]["state"] == "needs_evidence"


def test_failed_atomic_database_write_leaves_no_partial_publication(settings, product, monkeypatch):
    repo, reference = retained(settings, product)
    before = (settings.home_dir / "recipes/recipes.sqlite").read_bytes()
    original = PrivateDir.replace_file

    def fail_database(directory, name, data):
        if name == "recipes.sqlite":
            raise OSError("simulated disk full before publication")
        return original(directory, name, data)

    with monkeypatch.context() as patch:
        patch.setattr(PrivateDir, "replace_file", fail_database)
        with pytest.raises(OSError, match="disk full"):
            repo.publish("0123456789ab", name="短写失败")
    assert (settings.home_dir / "recipes/recipes.sqlite").read_bytes() == before
    reopened = RecipeRepository(settings.home_dir)
    assert reopened.listing() == []
    assert reopened.get(reference["id"])["revision"] == 0
    assert reopened.publish("0123456789ab", name="重新保存")["revision"] == 1


def test_frozen_environment_controls_and_iso_are_not_current_data(settings, product):
    repo, reference = retained(settings, product)
    selection = RecipeSel("jlaunch", 1, kind="local", id=reference["id"])
    before = repo.resolve(selection, photo_unit="jlaunch")
    for path in (settings.home_dir / "calib").glob("*"):
        path.unlink()
    controls = dataclasses.replace(selection, levels=(("contrast", 1),))
    changed = repo.resolve(controls)
    assert changed.record_sha256 == before.record_sha256
    assert changed.entry_sha256 != changed.record_sha256 and not changed.assessed_entry
    raw = np.array([[300.0, 650.0, 900.0]])
    assert np.array_equal(before.model.predict(before.entry, raw), repo.resolve(selection).model.predict(product.entry, raw))
    assert not repo.resolve(selection, photo_unit="jlaunch", iso=400).calibrated
    with pytest.raises(RecipeError, match="镜头单元"):
        repo.resolve(selection, photo_unit="qlaunch")
    with pytest.raises(RecipeError, match="控制绑定"):
        repo.resolve(dataclasses.replace(selection, slot=2))


def test_alias_updates_serialize_and_first_failure_is_durable(settings, product):
    repo, _reference = retained(settings, product)
    with ThreadPoolExecutor(max_workers=2) as executor:
        saved = list(executor.map(lambda _: repo.publish("0123456789ab", name="同一保存"), range(2)))
    assert saved[0]["id"] == saved[1]["id"] and len(repo.listing()) == 1
    repo.start_run("abcdef012345", product.context)
    repo.finish_run("abcdef012345", "error", {"code": "first failure"})
    repo.finish_run("abcdef012345", "interrupted", {"code": "late restart"})
    assert RecipeRepository(settings.home_dir).run("abcdef012345")["detail"] == {"code": "first failure"}
    assert repo.claim_failure_report("abcdef012345")
    assert not RecipeRepository(settings.home_dir).claim_failure_report("abcdef012345")


def test_corruption_and_unsupported_contract_are_explicit(settings, product):
    repo = RecipeRepository(settings.home_dir)
    repo.start_run("0123456789ab", product.context)
    bad = bytearray(product.entry)
    bad[83] ^= 1
    with pytest.raises(RecipeError, match="CRC"):
        repo.retain_fit("0123456789ab", dataclasses.replace(product, entry=bytes(bad)))
    environment = json.loads(product.environment)
    environment["model"] = "arbitrary-old-model"
    with pytest.raises(RecipeError, match="契约"):
        repo.retain_fit("0123456789ab", dataclasses.replace(product, environment=canonical(environment)))
    assert repo.run("0123456789ab")["state"] == "running"


def test_actual_partial_write_keeps_last_committed_database(settings, product, monkeypatch):
    repo, reference = retained(settings, product)
    database = settings.home_dir / "recipes/recipes.sqlite"
    before = database.read_bytes()
    write = os.write
    partial = []

    def short_write(fd, data):
        path = fd_path(fd)
        if path and ".recipes.sqlite." in str(path):
            if partial:
                return 0
            partial.append(write(fd, data[:37]))
            return partial[0]
        return write(fd, data)

    with monkeypatch.context() as patch:
        patch.setattr(os, "write", short_write)
        with pytest.raises(OSError, match="write returned 0"):
            repo.publish("0123456789ab", name="部分字节")
    assert partial == [37] and database.read_bytes() == before
    assert not list(database.parent.glob(".*.tmp"))
    reopened = RecipeRepository(settings.home_dir)
    assert reopened.listing() == [] and reopened.get(reference["id"])["revision"] == 0


def test_owned_child_directory_never_follows_a_symlink(settings, tmp_path):
    target = tmp_path / "unrelated-private-directory"
    target.mkdir()
    sentinel = target / "keep"
    sentinel.write_bytes(b"untouched")
    settings.home_dir.mkdir()
    (settings.home_dir / "recipes").symlink_to(target, target_is_directory=True)
    with pytest.raises(OSError):
        RecipeRepository(settings.home_dir).listing()
    assert sorted(p.name for p in target.iterdir()) == ["keep"]
    assert sentinel.read_bytes() == b"untouched"


def test_style_replacement_is_compare_and_swap_and_keeps_losing_candidate(settings, product):
    repo = RecipeRepository(settings.home_dir)
    style = digest(b"same style")
    original = dataclasses.replace(product, style=style)
    repo.start_run("000000000001", original.context)
    repo.retain_fit("000000000001", original)
    first = repo.publish("000000000001", name="第一版")
    replacement = dataclasses.replace(original, replacement=first["id"])
    for run in ("000000000002", "000000000003"):
        changed = dataclasses.replace(replacement, source=canonical({"attempt": run}))
        repo.start_run(run, changed.context)
        repo.retain_fit(run, changed)
    second = repo.publish("000000000002", name="第二版")
    with pytest.raises(RecipeError, match="同风格配方已更新"):
        repo.publish("000000000003", name="过期替换")
    reopened = RecipeRepository(settings.home_dir)
    assert reopened.reference_for_style(style, product.unit) == (second["id"], product.entry)
    assert reopened.run("000000000003")["state"] == "done"
    assert reopened.get(reopened.run("000000000003")["recipe"]["id"])["revision"] == 0


def test_equal_record_bytes_keep_distinct_unit_and_model_bindings(settings, product):
    repo, first = retained(settings, product)
    environment = RenderEnvironment.capture(settings.home_dir, CameraData(settings.home_dir), "qlaunch")
    other = dataclasses.replace(product, unit="qlaunch", environment=environment.data)
    repo.start_run("000000000004", other.context)
    second = repo.retain_fit("000000000004", other)
    assert second["record_sha256"] == repo.get(first["id"])["record_sha256"]
    assert first["id"] != second["recipe"]["id"]
    assert second["binding_sha256"] != repo.get(first["id"])["binding_sha256"]
    selection = RecipeSel("qlaunch", 1, kind="local", id=second["recipe"]["id"])
    assert repo.resolve(selection, photo_unit="qlaunch").entry == product.entry
    with pytest.raises(RecipeError, match="控制绑定"):
        repo.resolve(dataclasses.replace(selection, unit="jlaunch"))
