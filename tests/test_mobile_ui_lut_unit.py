"""UI-M4-LUT 单元测试：记录构建、输入色彩空间选择、配方+LUT 粘贴规则、出图令牌及与桌面 Python 的逐项对比。"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from gxr_studio.lut.errors import LutError
from gxr_studio.lut.library import LutLibrary, _token
from gxr_studio.lut.page import input_choice
from gxr_studio.lut.spaces import DISPLAY
from gxr_studio.lut.spaces import SPACES as DESKTOP_SPACES
from gxr_studio.render import paste
from gxr_studio.render.recipes import Menu
from support.export_pipeline_cases import check, same_redacted
from support.lut_cases import IDENTIFY_CASES, cube_text

REPO = Path(__file__).resolve().parents[1]
LUT_UNIT = REPO / "tests" / "support" / "mobile_ui_lut_unit.mjs"
GROUP_UNIT = REPO / "tests" / "support" / "mobile_ui_group_unit.mjs"


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    return node


def _run_node_tests(script: Path) -> None:
    proc = subprocess.run(
        [_node(), "--experimental-strip-types", "--test", str(script)],
        cwd=REPO,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    check(proc.returncode == 0, "node:test runner exited zero")
    check("ℹ fail 0" in proc.stdout or "# fail 0" in proc.stdout, "node:test reported zero failures")


def _eval_ts(code: str, payload: Any) -> Any:
    proc = subprocess.run(
        [_node(), "--experimental-strip-types", "--input-type=module", "-e", code],
        cwd=REPO,
        input=json.dumps(payload),
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    check(proc.returncode == 0, "node ts helper exited zero")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_mobile_lut_node_units() -> None:
    _run_node_tests(LUT_UNIT)


def test_mobile_group_paste_no_regression() -> None:
    _run_node_tests(GROUP_UNIT)


def test_mobile_lut_library_byte_cap_is_single_constant() -> None:
    """规范要求：256 MiB 上限只写在一个常量里（LUT 相关模块和 IDB 都引用 MAX_LUT_LIBRARY_BYTES）。"""
    hits: list[str] = []
    for path in sorted((REPO / "mobile" / "src").rglob("*.ts")):
        if path.name not in {"lut.ts", "idb.ts", "lut_book.ts", "worker.ts", "api.ts"}:
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if "256 << 20" in line or "268435456" in line or "256 * 1024 * 1024" in line:
                hits.append(f"{path.relative_to(REPO)}:{lineno}")
    same_redacted(hits, ["mobile/src/lib/lut.ts:7"], "single 256 MiB cap constant")


def test_mobile_spaces_and_input_choice_match_desktop(tmp_path: Path) -> None:
    """12 个色彩空间、computeInputChoice（含 IDENTIFY_CASES）、lutRenderToken 和桌面 Python 完全一致。"""
    from gxr_studio.lut.service import SPACE_LABELS

    lib = LutLibrary(tmp_path / "app-data")
    records: list[dict[str, Any]] = []
    for idx, (header, title, fname, fn) in enumerate(IDENTIFY_CASES[:30]):
        raw = cube_text(fn, size=2, title=title, comments=header).encode("utf-8")
        rec = lib._record(raw, fname or f"case_{idx}.cube")
        records.append(rec)

    py_choices = [input_choice(rec) for rec in records]
    py_tokens = [_token(rec["cube_id"], rec) for rec in records]
    manual_rec = {**records[0], "input_space": "applelog", "input_space_manual": True, "output_gamut": "rec2020"}
    py_choices.append(input_choice(manual_rec))
    py_tokens.append(_token(manual_rec["cube_id"], manual_rec))

    res = _eval_ts(
        """
        import { readFileSync } from 'node:fs';
        import { computeInputChoice, lutRenderToken, SPACES } from './mobile/src/lib/lut.ts';
        const { items } = JSON.parse(readFileSync(0, 'utf8'));
        const choices = items.map((r) => computeInputChoice(r));
        const tokens = items.map((r) => lutRenderToken(r));
        const spaces = SPACES.map((s) => [s.id, s.label, s.kind]);
        console.log(JSON.stringify({ choices, tokens, spaces }));
        """,
        {"items": [*records, manual_rec]},
    )
    same_redacted([list(row) for row in res["spaces"]], [[k, v[0], v[1]] for k, v in SPACE_LABELS.items()], "12 spaces order and labels")
    same_redacted(sorted(row[0] for row in res["spaces"]), sorted(DESKTOP_SPACES), "12 spaces ids")
    same_redacted(res["choices"], py_choices, "computeInputChoice parity")
    same_redacted(res["tokens"], py_tokens, "lutRenderToken parity")


def test_mobile_paste_recipe_with_lut_matches_desktop() -> None:
    """扩展 ui-m3-group 桌面对比：带 LUT 的配方组在手机与桌面 render/paste.py 上的跳过与套用完全一致。"""
    menu = Menu()
    display_lut = "a" * 64
    log_lut = "b" * 64
    missing_lut = "c" * 64

    def py_lut_needs_raw(cube_id: str) -> bool:
        if cube_id == missing_lut:
            raise LutError(404, "cube_not_found", "找不到这个 LUT")
        if cube_id == log_lut:
            return True
        return "srgb" not in DISPLAY

    py_env = paste.Env(menu=menu, lut_needs_raw=py_lut_needs_raw, recipe_without_raw=False)

    targets = [
        {"id": "raw_same", "lens": "50mm", "files": {"dng": {"relpath": "1.DNG"}, "jpg": {"relpath": "1.JPG"}}},
        {"id": "jpg_only", "lens": "50mm", "files": {"jpg": {"relpath": "2.JPG"}}},
        {"id": "other_unit", "lens": "28mm", "files": {"dng": {"relpath": "3.DNG"}, "jpg": {"relpath": "3.JPG"}}},
    ]
    sources = [
        {
            "name": "recipe_and_display_lut",
            "recipe": {"kind": "builtin", "unit": "jlaunch", "slot": 3, "levels": {}},
            "lut": display_lut,
        },
        {
            "name": "only_log_lut",
            "recipe": None,
            "lut": log_lut,
        },
        {
            "name": "recipe_and_missing_lut",
            "recipe": {"kind": "builtin", "unit": "jlaunch", "slot": 3, "levels": {}},
            "lut": missing_lut,
        },
    ]

    py_outcomes: list[dict[str, Any]] = []
    for src in sources:
        for t in targets:
            skips: list[paste.Skip] = []
            changes = paste._edit_changes({}, t, {"recipe": src["recipe"], "lut": src["lut"]}, ["recipe"], py_env, skips)
            applied = ["recipe"] if changes else []
            status = "applied" if applied and not skips else ("skipped" if not applied else "partial")
            py_outcomes.append(
                {
                    "src": src["name"],
                    "target": t["id"],
                    "status": status,
                    "applied": applied,
                    "skipped": [{"group": s.group, "reason": s.reason, "part": s.part} for s in skips],
                }
            )

    ts_outcomes = _eval_ts(
        """
        import { readFileSync } from 'node:fs';
        import { registerHooks } from 'node:module';
        registerHooks({
          resolve(spec, ctx, next) {
            try { return next(spec, ctx); }
            catch (err) {
              if (spec.startsWith('.') && !/\\.[a-z]+$/.test(spec)) return next(`${spec}.ts`, ctx);
              throw err;
            }
          },
        });
        const { planOne } = await import('./mobile/src/lib/paste.ts');
        const { displayLut, logLut, missingLut } = JSON.parse(readFileSync(0, 'utf8'));
        const env = {
          listed: (unit, id) => unit === 'jlaunch' && id === 'builtin:3',
          recipeWithoutRaw: false,
          lutNeedsRaw: (id) => (id === missingLut ? 'missing' : id === logLut),
        };
        const knobs = () => ({
          exposure_ev: 0, contrast: 0, highlight: 0, shadow: 0, black_point: 0,
          warm: 0, tint: 0, saturation: 0, vibrance: 0, vignette: 0,
        });
        const targets = [
          { id: 'raw_same', raw: true, unit: 'jlaunch' },
          { id: 'jpg_only', raw: false, unit: 'jlaunch' },
          { id: 'other_unit', raw: true, unit: 'qlaunch' },
        ];
        const sources = [
          {
            name: 'recipe_and_display_lut',
            clip: {
              groups: ['recipe'], from: { id: 's', stem: 'S' },
              recipe: { choice: { id: 'builtin:3', unit: 'jlaunch' }, levels: { amount: 80 }, memory: new Map() },
              lut: displayLut, knobs: null,
            },
          },
          {
            name: 'only_log_lut',
            clip: { groups: ['recipe'], from: { id: 's', stem: 'S' }, recipe: null, lut: logLut, knobs: null },
          },
          {
            name: 'recipe_and_missing_lut',
            clip: {
              groups: ['recipe'], from: { id: 's', stem: 'S' },
              recipe: { choice: { id: 'builtin:3', unit: 'jlaunch' }, levels: {}, memory: new Map() },
              lut: missingLut, knobs: null,
            },
          },
        ];
        const out = [];
        for (const src of sources) {
          for (const t of targets) {
            const photo = { id: t.id, rev: 0, choice: null, lut: null, levels: new Map(), memory: new Map(), knobs: knobs() };
            const { outcome } = planOne({ photo, raw: t.raw, unit: t.unit }, src.clip, ['recipe'], env);
            out.push({
              src: src.name,
              target: t.id,
              status: outcome.status,
              applied: outcome.applied,
              skipped: outcome.skipped.map((s) => ({ group: s.group, reason: s.reason, part: s.part ?? null })),
            });
          }
        }
        console.log(JSON.stringify(out));
        """,
        {"displayLut": display_lut, "logLut": log_lut, "missingLut": missing_lut},
    )
    same_redacted(ts_outcomes, py_outcomes, "paste outcomes with LUT match desktop")
