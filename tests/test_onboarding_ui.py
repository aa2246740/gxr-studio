"""快捷键表、设置、配方页提示、导出 HDR 开关。界面行为在 Node 里跑，不依赖 macOS。"""

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


def test_guide_dialog_is_gone_and_help_opens_the_shortcut_table():
    """U9：打开页面就弹的四步引导删掉了，第一次使用的说明放在图库的空状态里；顶栏键盘按钮直接打开快捷键表。"""
    onboarding = (ASSETS / "onboarding.js").read_text(encoding="utf-8")
    app = (ASSETS / "app.js").read_text(encoding="utf-8")
    gallery = (ASSETS / "gallery.js").read_text(encoding="utf-8")
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    lut = (ASSETS / "lut.js").read_text(encoding="utf-8")
    export = (ASSETS / "export.js").read_text(encoding="utf-8")
    for gone in ("maybeShowOnboarding", "showGuide", "GUIDE_STEPS", "onboarding_dismissed"):
        assert gone not in app, gone
        assert gone not in gallery, gone
    assert "裁切" not in onboarding
    assert "getElementById('btn-help').addEventListener('click', showHelp)" in app
    assert "if (e.key === '?')" in app and "showHelp();" in app
    assert 'id="btn-help"' in html and 'title="快捷键（?）"' in html
    # 空图库里的说明：导入、编辑、导出三步
    assert "function emptyLibrary()" in gallery and "只读复制，卡上的文件不会被改动。" in gallery
    assert HINT in lut
    assert "dataset: { role: 'recipe-hint' }" in lut
    assert "rememberHdr" in export and "export_hdr" in export
    assert "if (!ok) hdr = false" not in export


@pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node")
def test_help_and_settings_open_without_a_guide(tmp_path):
    report = _run_scene(tmp_path)
    assert report["guideExports"] == []
    assert report["helpTitle"] == "快捷键"
    assert report["groups"] == ["全局", "图库", "编辑", "裁剪"]
    # 浏览器保留的组合键不用（HANDOFF）：⌘W/⌘T/⌘N/⌘R/⌘L/⌘Q 都不在表里
    for reserved in ("⌘W", "⌘T", "⌘N", "⌘R", "⌘L", "⌘Q"):
        assert reserved not in report["keys"]
    assert "⌘E" in report["keys"] and "\\" in report["keys"] and "Y" in report["keys"] and "Z" in report["keys"]
    assert report["afterHelpClose"] == 0
    # Windows 上写 Ctrl+，重做是 Ctrl+Y
    assert "Ctrl+E" in report["winKeys"] and "Ctrl+Y" in report["winKeys"]
    assert not any("⌘" in k for k in report["winKeys"])
    assert report["settingsTitle"] == "设置"
    assert "引导" not in report["settingsText"]
    assert report["prefsWrites"] == 0


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
    for name in ("dom.js", "controls.js", "onboarding.js", "export.js", "lut.js", "saves.js", "jobs.js", "busy.js", "gpu_pref.js", "poll.js"):
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
    (folder / "preview.js").write_text(
        "export const BASE_NAMES = [];\nexport const KNOB_NAMES = {};\nexport function slotName(_u, s) { return String(s); }\n",
        encoding="utf-8",
    )
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
