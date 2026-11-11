"""完整配方测试只生成合成模型、数值和来源身份。"""

from __future__ import annotations

import dataclasses

import numpy as np

from gxr_studio.camdata import calib
from gxr_studio.engine import knobs
from gxr_studio.fit_contexts import SampleGroup, TrainingInput, build_quality_pack
from gxr_studio.recipes import RenderEnvironment
from gxr_studio.recipes.admission import evaluate_winner
from gxr_studio.recipes.fit import Winner, default_plan
from gxr_studio.recipes.numerics import FullProblem, curve_x, predict_mapped
from gxr_studio.recipes.types import canonical, digest
from gxr_studio.render.recipes import CameraData
from test_clone_coverage import _install_camera, _render_entry
from test_unit_calibration import sources


def environment_at(home, unit="jlaunch"):
    entry = bytearray(_render_entry())
    entry[1] = 7
    entry[244:272] = entry[216:244]
    knobs.checksum(entry)
    _install_camera(home, bytes(entry))
    calib.import_calibration(home, sources(home))
    return RenderEnvironment.capture(home, CameraData(home), unit)


def sample_group(environment, seed, *, photo=0, iso=200, count=64, target=None):
    rng = np.random.default_rng(seed)
    raw = rng.uniform(150, 3700, (count, 3))
    raw[:8] = np.linspace(200, 3500, 8)[:, None]
    model = environment.model_for(200)[0]
    from gxr_studio.camdata.unit_model import input_codes

    mapped = input_codes(raw, model.input_coefficients)
    base = predict_mapped(model, environment.stock, mapped)
    want = base.copy() if target is None else predict_mapped(model, target, mapped)
    return SampleGroup(
        raw, mapped, raw / 4095, want, base, np.ones(count), np.full(count, photo, dtype="<i8"), np.full(count, iso, dtype="<i8")
    )


def training_input(environment, *, target=None):
    group = sample_group(environment, 40, photo=-1, count=96, target=target)
    return TrainingInput(environment.unit, 1, "color", group, environment, environment.stock, environment.stock, (), canonical(default_plan()))


def target_record(environment):
    training = training_input(environment)
    problem = FullProblem(training, curve_x(environment.stock))
    theta = problem.seed(environment.stock)
    phase = np.linspace(0, np.pi, 33)
    changes = np.array([4 * np.sin(phase), -3 * np.sin(phase) ** 2, 2 * np.sin(phase * 2)])
    theta[problem.qstart : problem.qend] += changes.ravel()
    theta[0], theta[1] = 0.015, -0.01
    theta[problem.matrix_start :] = (0.012, 0.005, -0.003, 0.004, -0.003, 0.004, -0.002)
    theta[2:26] += 0.12 * np.sin(np.arange(24) * 0.8)
    theta[26:50] += 0.08 * np.cos(np.arange(24) * 0.7)
    return problem.encode(theta)


def quality_pack(folder, environment, *, holdout_target=None, missing_other=False, missing_scene=False):
    target = target_record(environment)
    groups = {
        "train": sample_group(environment, 1, photo=0, target=target),
        "photo_iso200": sample_group(environment, 2, photo=1, target=holdout_target or target),
    }
    if not missing_other:
        groups["photo_other_iso"] = sample_group(environment, 3, photo=2, iso=1600, target=target)
    photos = [
        {
            "id": i,
            "raw_sha256": digest(f"raw-{i}".encode()),
            "jpg_sha256": digest(f"jpg-{i}".encode()),
            "family_sha256": digest(f"family-{i}".encode()),
            "sampling_sha256": digest(b"synthetic-sampler/1"),
            "iso": 1600 if i == 2 else 200,
            "scene": "" if missing_scene and i == 1 else "synthetic-scene",
        }
        for i in range(3)
    ]
    definition = {"kind": "synthetic-record-target", "sha256": digest(target)}
    manifest = build_quality_pack(
        folder,
        environment=environment,
        groups=groups,
        photos=photos,
        references={"template": environment.stock, "original": environment.stock, "factory04": environment.stock},
        target=definition,
        sources=[{"kind": "synthetic", "sha256": digest(b"synthetic-sampler/1"), "description": "public synthetic fixture"}],
        slot=1,
    )
    return manifest, definition, target, groups


def replace_want(group, value):
    return dataclasses.replace(group, want=np.broadcast_to(np.asarray(value, float), group.want.shape).copy())


def source_for_service(service, folder, unit="jlaunch"):
    import json

    if not service.render.available(unit):
        environment_at(service.home, unit)
    environment = RenderEnvironment.capture(service.home, service.render._require(unit), unit)
    quality_pack(folder, environment)
    path = folder / "quality-pack.json"
    manifest = json.loads(path.read_bytes())
    manifest["target"] = {"kind": "sample-corpus"}
    path.write_bytes(canonical(manifest))
    return service.contexts.import_pack(folder)["id"]


def plant_pending(service, cube_id, job_id, *, unit="jlaunch", space="srgb", guessed=False):
    """生命周期夹具：真实快照和记录，不声称运行数值求解或通过准入。"""
    from gxr_studio.lut.metadata import FIELDS

    source = source_for_service(service, service.home.parent / f"quality-{job_id}", unit)
    environment = RenderEnvironment.capture(service.home, service.render._require(unit), unit)
    target = service.target(service.cube(cube_id), space)
    definition = {"kind": "lut", "cube_sha256": cube_id, "spaces": target.description()}
    raw, item = service.library.snapshot(cube_id)
    context = service.contexts.freeze(
        source, target=definition, plan=default_plan(), environment=environment, target_renderer=target, target_asset=raw
    )
    job = {
        "home": str(service.home),
        "context": context.identity,
        "cube_id": cube_id,
        "unit": unit,
        "input_space": space,
        "output_space": "srgb",
        "profile": "normal",
        "space_guessed": guessed,
        "photos": [],
        "source": {
            "kind": "controlled-terminal-fixture",
            "cube_sha256": cube_id,
            "target": definition,
            "title": item.get("name"),
            "attribution": {key: item[key] for key in FIELDS},
            "input_space_guessed": guessed or service._filename_guess_matches(cube_id, space),
        },
    }
    service._write_json(service.jobs_dir / job_id / "job.json", job)
    service.recipes.start_run(
        job_id,
        context.identity,
        {
            "unit": unit,
            "cube_id": cube_id,
            "input": {key: job[key] for key in ("input_space", "output_space", "profile", "space_guessed")},
            "photos": [],
            "source_sha256": digest(canonical(job["source"])),
        },
    )
    return context


def publish_controlled_terminal(service, job_id):
    """只供退出/取消竞态测试发布诚实评估草稿；未跑exact阶段，全部门由evaluate_winner实算。"""
    import json

    job = service._read(service.jobs_dir / job_id / "job.json")
    context = service.contexts.open(job["context"])
    problem = FullProblem(context.training, curve_x(context.training.original))
    entry = problem.encode(problem.seed(context.training.original))
    receipt = canonical(
        {
            "contract": default_plan()["contract"],
            "plan": json.loads(context.training.plan),
            "source_sha256": context.document["plan"]["source_sha256"],
            "training_identity": context.training.identity,
            "record_sha256": digest(entry),
            "controlled_terminal_fixture": True,
        }
    )
    winner = Winner(entry, receipt)
    groups, references = service.contexts.evaluation(context.identity)
    quality = canonical(evaluate_winner(context, winner, groups, references).to_json())
    with service.library.private() as directory:
        prefix = f"lut_jobs/{job_id}"
        directory.replace_file(f"{prefix}/candidate.record", entry)
        directory.replace_file(f"{prefix}/winner.json", receipt)
        directory.replace_file(f"{prefix}/quality.json", quality)
        directory.replace_file(
            f"{prefix}/winner-seal.json",
            canonical(
                {
                    "context": context.identity,
                    "record_sha256": digest(entry),
                    "receipt_sha256": digest(receipt),
                }
            ),
        )
    result = {
        "schema": "gxr-studio/full-fit-result/1",
        "state": "done",
        "context": context.identity,
        "record_sha256": digest(entry),
        "receipt_sha256": digest(receipt),
        "quality_sha256": digest(quality),
    }
    service._write_json(service.jobs_dir / job_id / "result.json", result)
    return json.loads(canonical(result))
