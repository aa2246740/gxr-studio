"""首次引导、配方页提示、导出 HDR 开关。界面行为在 Node 里跑，不依赖 macOS。"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "web" / "assets"
SCENE = Path(__file__).resolve().parent / "support" / "ui_scene.mjs"
HINT = "相机关机再开后，按 ADJ 再按 OK 套用上次的配方"
STEPS = [
    ["插入存储卡", "自动导入照片", "1/4"],
    ["在图库里浏览、编辑", "裁剪、曝光、边框", "2/4"],
    ["用 LUT 库管理 LUT", "套用到照片", "3/4"],
    ["导出", "可选 HDR", "4/4"],
]


def test_guide_copy_is_on_the_page_and_help_opens_it():
    onboarding = (ASSETS / "onboarding.js").read_text(encoding="utf-8")
    app = (ASSETS / "app.js").read_text(encoding="utf-8")
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    lut = (ASSETS / "lut.js").read_text(encoding="utf-8")
    export = (ASSETS / "export.js").read_text(encoding="utf-8")
    for title, text, _count in STEPS:
        assert f"['{title}', '{text}']" in onboarding
    assert "裁切" not in onboarding
    assert "getElementById('btn-help').addEventListener('click', showHelpMenu)" in app
    assert "if (e.key === '?')" in app and "showHelp();" in app
    assert 'title="帮助">帮助</button>' in html
    assert HINT in lut
    assert "dataset: { role: 'recipe-hint' }" in lut
    assert "rememberHdr" in export and "export_hdr" in export
    assert "if (!ok) hdr = false" not in export


@pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node")
def test_guide_shows_once_then_reopens_from_help_and_settings(tmp_path):
    report = _run_scene(tmp_path)
    assert report["seen"] == STEPS
    assert report["back"] == "用 LUT 库管理 LUT"
    assert report["last"] == "导出"
    assert report["afterFinishOpen"] == 0
    assert report["hiddenAfterFinish"] == 0
    assert report["finished"] == [{"onboarding_dismissed": True}]
    assert report["afterSkipOpen"] == 0
    assert report["hiddenAfterSkip"] == 0
    assert report["skipped"] == [{"onboarding_dismissed": True}]
    assert report["helpTitle"] == "插入存储卡"
    assert report["settingsTitle"] == "插入存储卡"


@pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node")
def test_recipe_hint_stays_and_hdr_remembers_without_enabling_jpeg_only(tmp_path):
    report = _run_scene(tmp_path)
    assert report["hint"] == [HINT]
    assert report["first"] == {"checked": False, "disabled": False, "note": False}
    assert report["turnedOn"] == {"checked": True, "disabled": False}
    assert report["turnedOff"] == {"checked": False, "disabled": False}
    assert report["noRaw"] == {"checked": False, "disabled": True, "note": True}
    assert report["noRawPuts"] == 0
    assert report["memory"] is True
    assert report["rawAgain"] == {"checked": True, "disabled": False, "note": False}


def _run_scene(tmp_path: Path) -> dict:
    node = shutil.which("node")
    assert node is not None
    folder = tmp_path / "web"
    folder.mkdir()
    for name in ("dom.js", "controls.js", "onboarding.js", "export.js", "lut.js"):
        shutil.copy(ASSETS / name, folder / name)
    (folder / "package.json").write_text('{"type":"module"}\n', encoding="utf-8")
    (folder / "api.js").write_text(
        "\n".join(
            [
                "export class ApiError extends Error {}",
                "export async function apiBlob() { throw new Error('no blob'); }",
                "export async function api(path, opts = {}) {",
                "  const body = opts.body || null;",
                "  globalThis.uiCalls.push({ path, method: opts.method || 'GET', body });",
                "  if (path === '/api/prefs' && opts.method === 'PUT') {",
                "    Object.assign(globalThis.uiStore, body);",
                "    return { ...globalThis.uiStore };",
                "  }",
                "  if (path === '/api/prefs') return { ...globalThis.uiStore };",
                "  if (path === '/api/settings') return {",
                "    library_dir: '/lib', export_dir: '/exp', undated_policy: 'mtime', photo_count: 0, free_bytes: 0",
                "  };",
                "  if (path === '/api/lut/spaces') return { spaces: [] };",
                "  if (path === '/api/my-recipes') return { recipes: [] };",
                "  if (path === '/api/library/rebuild') return {};",
                "  throw new Error('unexpected ' + path);",
                "}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (folder / "state.js").write_text(
        "export const state = globalThis.uiState;\nexport function emit() {}\nexport function on() {}\n",
        encoding="utf-8",
    )
    (folder / "camdata.js").write_text(
        "\n".join(
            [
                "const box = () => document.createElement('div');",
                "export function calibrationImportForm() { return box(); }",
                "export function camdataSummary() { return { camera: '无', calibration: '无' }; }",
                "export function cameraImportControls() { return box(); }",
                "export async function loadCamdata() {}",
                "export function camdataGuide() { return box(); }",
                "export function camdataReady() { return true; }",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (folder / "preview.js").write_text("export const BASE_NAMES = [];\nexport const KNOB_NAMES = {};\n", encoding="utf-8")
    (folder / "lut_library.js").write_text(
        "\n".join(
            [
                "export async function loadLutLibrary() {}",
                "export function librarySection() { return document.createElement('section'); }",
                "export function attributionFields() { return document.createElement('div'); }",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (folder / "lut_files.js").write_text(
        "\n".join(
            [
                "export function selectedFiles() { return []; }",
                "export async function droppedFiles() { return []; }",
                "export async function importFiles() { return []; }",
                "",
            ]
        ),
        encoding="utf-8",
    )
    out = subprocess.run([node, str(SCENE), str(folder)], capture_output=True, text=True, timeout=30, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)
