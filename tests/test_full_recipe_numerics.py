"""完整前向和雅可比必须包含原厂lookup导数，并只映射一次。"""

from __future__ import annotations

import dataclasses

import numpy as np

from full_recipe_support import environment_at, target_record, training_input
from gxr_studio.camdata.unit_model import input_codes
from gxr_studio.recipes import check_record
from gxr_studio.recipes.fixtures import GuardedProblem
from gxr_studio.recipes.numerics import FullProblem, curve_x, predict_mapped


def test_nonidentity_mapping_and_independent_forward(settings):
    environment = environment_at(settings.home_dir)
    entry = target_record(environment)
    training = training_input(environment, target=entry)
    model = environment.model_for(200)[0]
    assert not np.array_equal(training.samples.raw_wb, training.samples.x)
    predicted = predict_mapped(model, entry, training.samples.x)
    assert np.max(abs(predicted - model.predict(entry, training.samples.raw_wb))) < 1e-8
    twice = input_codes(training.samples.x, model.input_coefficients)
    assert np.max(abs(predicted - predict_mapped(model, entry, twice))) > 0.01
    check_record(entry)
    assert entry[1] == 7 and entry[379:412] != entry[82:115] and entry[412:445] != entry[82:115]
    assert entry[216:244] != environment.stock[216:244] and entry[115:211] != environment.stock[115:211]
    assert entry[456:518] == entry[20:82] and entry[518:551] == entry[82:115]


def test_full_v2_curve_matrix_and_sector_jacobian(settings):
    environment = environment_at(settings.home_dir)
    training = training_input(environment, target=target_record(environment))
    problem = FullProblem(training, curve_x(environment.stock))
    point = problem.seed(environment.stock)
    point[problem.qstart : problem.qend] += np.tile(np.sin(np.linspace(0, np.pi, 33)), 3)
    _residual, derivative = problem.evaluate(point)
    columns = (0, 1, 2, 26, problem.qstart + 16, problem.qstart + 33 + 12, problem.qstart + 66 + 18, problem.matrix_start, problem.size - 1)
    for column in columns:
        step = np.zeros(problem.size)
        step[column] = 1e-5
        finite = (problem.evaluate(point + step)[0] - problem.evaluate(point - step)[0]) / 2e-5
        scale = np.maximum(1, abs(finite))
        assert np.percentile(abs(finite - derivative[:, column]) / scale, 99) < 0.01, column


def test_gray_family_is_explicit_and_uses_physical_zero_chroma(settings):
    environment = environment_at(settings.home_dir)
    training = dataclasses.replace(training_input(environment), family="bw", slot=3)
    problem = FullProblem(training, curve_x(environment.stock))
    assert problem.nc == 1 and problem.size == 36
    entry = problem.encode(problem.seed(environment.stock))
    check_record(entry)
    assert entry[0] == 3 and entry[1] == 1 and entry[115:211] == bytes(96)
    from gxr_studio.engine.matrix_model import MatrixRow

    assert not MatrixRow.from_bytes(entry[216:244]).coef[1:].any()
    assert not MatrixRow.from_bytes(entry[244:272]).coef[1:].any()
    controls = {c["name"] for c in environment.controls(3)}
    assert controls == {"contrast", "highlight", "shadow", "fade", "filter"}
    rgb = predict_mapped(environment.model_for(200)[0], entry, training.samples.x)
    assert np.max(np.ptp(rgb, axis=1)) < 1e-8


def test_fixed_hue_penalty_derivative_uses_the_v2_lookup(settings):
    environment = environment_at(settings.home_dir)
    primary = FullProblem(training_input(environment), curve_x(environment.stock))
    guarded = GuardedProblem(primary)
    point = primary.seed(environment.stock)
    point[26:50] += 0.4 * np.sin(np.arange(24) * 1.7)
    primary_size = len(primary.evaluate(point)[0])
    residual, derivative = guarded.evaluate(point)
    active = abs(residual[primary_size:]) > 1e-6
    assert active.sum() >= 8
    for column in (0, 2, 26, primary.qstart + 16, primary.matrix_start + 1):
        step = np.zeros(primary.size)
        step[column] = 1e-5
        finite = (guarded.evaluate(point + step)[0] - guarded.evaluate(point - step)[0]) / 2e-5
        error = abs(finite[primary_size:] - derivative[primary_size:, column]) / np.maximum(1, abs(finite[primary_size:]))
        assert np.percentile(error[active], 95) < 0.01, column
