"""内置的默认校准（机主 2026-11-24 决定公开）：手机上没导入校准时用作者三支单元实拍拟合的那份。

- 包本身：就是记下的那一个文件，只有拟合参数（d0、d1、输入系数和色相分区模型），没有原厂表和固件字节；
- 私有字节检查只放行这个路径上的这一份字节，换了字节、换了路径都拦；
- 浏览器里：起始页没有校准这一步；新的配置文件按默认校准画、导出和手动导入同一个 zip 一样；导入别的包盖过它、恢复默认又回来；
  默认校准下载不下来或文件被改过时是中性转换并写明原因。

conftest 默认把开发服务器的默认校准关着（`GXR_MOBILE_DEFAULT_CALIB=off`）；这里的浏览器用例各自打开或换成别的文件。
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

import numpy as np
import pytest

from gxr_studio.camdata import calib, calib_export
from support import samples
from support.fake_card import fake_dat
from support.mobile_ui_calib import pack_files

REPO = Path(__file__).resolve().parents[1]
REL = "mobile/public/calib/gxr-default-calib.zip"
BUNDLED = REPO / REL
TS = REPO / "mobile" / "src" / "lib" / "default_calib.ts"
DRIVER = REPO / "tests" / "support" / "mobile_default_calib_browser.mjs"
SHA256 = "0bac0fe18613794fa39715e80c0067701e5718a985beb568af23c1848764f0da"


def _guard():
    spec = importlib.util.spec_from_file_location("private_bytes", REPO / "scripts" / "private_bytes.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _entries(blob: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        return {info.filename: archive.read(info) for info in archive.infolist()}


def test_bundled_pack_is_the_recorded_file_and_holds_fitted_parameters_only() -> None:
    blob = BUNDLED.read_bytes()
    assert hashlib.sha256(blob).hexdigest() == SHA256
    ts = TS.read_text(encoding="utf-8")
    assert re.search(r"DEFAULT_CALIB_SHA256 = '([0-9a-f]{64})'", ts).group(1) == SHA256  # type: ignore[union-attr]
    assert "DEFAULT_CALIB_PATH = 'calib/gxr-default-calib.zip'" in ts
    assert _guard().PUBLIC_CALIB == (REL, SHA256)
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        infos = archive.infolist()
        assert infos[0].filename == "manifest.json"
        assert all(info.compress_type == zipfile.ZIP_STORED for info in infos)
    files = _entries(blob)
    manifest = json.loads(files["manifest.json"])
    assert manifest["schema"] == calib.SCHEMA
    assert sorted(manifest["units"]) == ["jlaunch", "l08firm", "qlaunch"]
    assert {u: m["isos"] for u, m in manifest["units"].items()} == {"jlaunch": [200, 1600], "qlaunch": [200], "l08firm": [200]}
    assert sorted(files) == sorted(["manifest.json", *manifest["files"]])
    for name, meta in manifest["files"].items():
        assert meta == {"size": len(files[name]), "sha256": hashlib.sha256(files[name]).hexdigest()}, name
        if name.endswith(".npz"):
            # 只有 d0、d1 和输入系数：原厂色表行（stock、S_eff）不在包里，运行时取用户自己的原厂 04。
            with np.load(io.BytesIO(files[name]), allow_pickle=False) as arrays:
                assert sorted(arrays.files) == ["d0", "d1", "input_coefficients"], name
        else:
            assert json.loads(files[name])["schema"] == "gxr-sector-model/2", name
    assert all(not name.lower().endswith((".dat", ".bin", ".entry")) for name in files)


def test_private_bytes_allows_exactly_the_public_pack() -> None:
    guard = _guard()
    shas = guard.known_shas()
    blob = BUNDLED.read_bytes()
    assert guard.scan_bytes(REL, blob, shas) == []
    assert guard.scan_bytes(f"staged:{REL}", blob, shas) == []
    assert guard.scan_paths([BUNDLED], shas) == []

    def rejected(name: str, data: bytes, rule: str = "R5 calib-pack") -> bool:
        hits = guard.scan_bytes(name, data, shas)
        return bool(hits) and all(found == rule for _, found in hits)

    # 同样的字节换个路径：拦。
    assert rejected("mobile/public/calib/copy.zip", blob)
    assert rejected("mobile/src/assets/gxr-default-calib.zip", blob)
    assert rejected(f"staged:{REL}.bak", blob)
    # 同一个路径换一个 zip：拦（另一份合成的包；同样的文件换个打包时间）。
    stock = {"jlaunch": bytes(552), "qlaunch": bytes(552), "l08firm": bytes(552)}
    other = calib_export.zip_bytes(pack_files(stock), (2026, 11, 24, 9, 0, 0))
    assert rejected(REL, other)
    assert rejected(f"staged:{REL}", other)
    repacked = calib_export.zip_bytes(_entries(blob), (2026, 11, 24, 9, 0, 0))
    assert repacked != blob and rejected(REL, repacked)
    # 散放的校准文件和数组：拦；源码里的 schema 字面量不算。
    files = _entries(blob)
    assert rejected("docs/manifest.json", files["manifest.json"])
    assert rejected("tests/data/ring.json", files["ring_jlaunch_200.json"])
    assert rejected("tests/data/ctx.npz", files["ctx_jlaunch_200.npz"])
    assert guard.scan_bytes("src/x.py", b'SCHEMA = "gxr-studio/calib/2"\nmanifest = {"schema": SCHEMA}\n', shas) == []
    # DAT 字节放在这个路径上照样拦（R1），zip 里夹一份 DAT 也拦。
    assert rejected(REL, fake_dat(7), "R1 dat-head")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
        archive.writestr("GXRCE.DAT", fake_dat(8))
    assert any(rule == "R1 dat-head" for _, rule in guard.scan_bytes(REL, buf.getvalue(), shas))


def _sdist(member: str, data: bytes) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(member)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_private_bytes_accepts_the_public_pack_inside_the_sdist_only_at_its_path() -> None:
    guard = _guard()
    shas = guard.known_shas()
    blob = BUNDLED.read_bytes()
    assert guard.scan_bytes("dist/gxr_studio-0.1.0.tar.gz", _sdist(f"gxr_studio-0.1.0/{REL}", blob), shas) == []
    moved = guard.scan_bytes("dist/gxr_studio-0.1.0.tar.gz", _sdist("gxr_studio-0.1.0/mobile/public/other.zip", blob), shas)
    assert moved and all(rule == "R5 calib-pack" for _, rule in moved)
    nested = guard.scan_bytes("dist/gxr_studio-0.1.0.tar.gz", _sdist(f"gxr_studio-0.1.0/x/{REL}", blob), shas)
    assert nested and all(rule == "R5 calib-pack" for _, rule in nested)


# ---- 浏览器 ----


def _inputs(tmp_path: Path) -> tuple[list[Path], Path]:
    from test_mobile_ui_browser import DATS, MODULES, PKG, RUNTIME, _need, _pairs

    pairs = _pairs()
    dat = next((path for path in DATS if path.is_file()), None)
    if len(pairs) < 2:
        samples.missing("need a DNG+JPG pair (GXR_STUDIO_TEST_PARENT)")
    if dat is None:
        samples.missing("no local GXRCE.DAT fixture (GXR_STUDIO_TEST_PARENT)")
    _need(PKG.is_file(), "wasm package is missing; run make core-wasm")
    _need(MODULES.is_dir(), "mobile/node_modules is missing")
    _need(shutil.which("node") is not None, "node is required")
    _need(RUNTIME.is_file(), "pinned Playwright runtime is missing")
    assert dat is not None
    return pairs[:2], dat


def _run(tmp_path: Path, mode: str, extra: dict[str, object]) -> dict:
    from test_mobile_ui_browser import _drive

    pair, dat = _inputs(tmp_path)
    out_dir = tmp_path / "shared"
    out_dir.mkdir()
    manifest = tmp_path / f"{mode}.json"
    report = tmp_path / f"{mode}-out.json"
    files = [str(p) for p in (*pair, dat)]
    spec = {"mode": mode, "files": files, "stem": pair[0].stem, "outDir": str(out_dir), "out": str(report), **extra}
    manifest.write_text(json.dumps(spec), encoding="utf-8")
    result = _drive(DRIVER, manifest, tmp_path, "", timeout=900)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data.get("error") is None, data.get("error")
    assert data["pageErrors"] == []
    return data


def _shared(tmp_path: Path, prefix: str, names: list[str]) -> bytes:
    assert len(names) == 1, names
    return (tmp_path / "shared" / f"{prefix}{names[0]}").read_bytes()


@pytest.mark.browser_serial
def test_default_calibration_renders_and_exports_like_a_hand_import(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GXR_MOBILE_DEFAULT_CALIB", raising=False)  # 照常发 public/ 里的那份
    from gxr_studio.camdata import store

    _pair, dat = _inputs(tmp_path)
    # 别的包：合成的参数，原厂 04 指纹跟着这份 DAT 走（导进去能用，颜色和默认校准不同）。
    _parsed, entries = store.entries_from_dat(dat.read_bytes())
    stock = {name: entries[(unit, 4)] for unit, name in enumerate(("jlaunch", "qlaunch", "l08firm"))}
    other = tmp_path / "GXR 校准数据 别的.zip"
    other.write_bytes(calib_export.zip_bytes(pack_files(stock), (2026, 11, 24, 9, 0, 0)))
    bundled = tmp_path / "gxr-default-calib.zip"
    bundled.write_bytes(BUNDLED.read_bytes())
    extra: dict[str, object] = {"bundled": str(bundled), "other": str(other)}
    # 手动截图时给一个文件夹（不进仓库）；平时不截。
    if os.environ.get("GXR_DEFAULT_CALIB_SHOTS"):
        extra["shots"] = os.environ["GXR_DEFAULT_CALIB_SHOTS"]
    data = _run(tmp_path, "bundled", extra)
    steps = data["steps"]

    # 起始页：没有校准这一步，能点的只有选照片和帮助。
    assert steps["start"]["cards"] == 0
    assert steps["start"]["buttons"][0] == "从存储卡选照片" and all("校准" not in text for text in steps["start"]["buttons"])
    assert steps["bootState"]["kind"] == "default" and steps["bootState"]["units"] == ["jlaunch", "qlaunch", "l08firm"]

    # A：不导入，按默认校准画，卡上写「默认校准（基于样机）」和那一句说明；整次会话只下载一次。
    a = steps["defaultA"]
    assert a["strip"]["state"] == "calibrated" and a["strip"]["tileState"] != "no-calibration"
    assert a["sheet"]["calib"] == "default" and a["sheet"]["detail"] == "默认校准（基于样机）"
    assert "默认校准（基于样机）" in a["sheet"]["text"] and "用作者的三支单元实拍拟合；想更准可以换成自己的校准。" in a["sheet"]["text"]
    assert "换成自己的校准…" in a["sheet"]["text"] and "未校准" not in a["sheet"]["text"]
    assert a["fetches"] == 1

    # B：另一个配置文件手动导入同一个 zip，导出的字节和 A 一样。
    b = steps["handB"]
    assert b["strip"]["state"] == "calibrated" and b["state"]["kind"] == "user"
    assert (b["strip"]["tile"], b["strip"]["tileState"]) == (a["strip"]["tile"], a["strip"]["tileState"])
    default_bytes = _shared(tmp_path, "default-", a["files"])
    assert _shared(tmp_path, "hand-", b["files"]) == default_bytes

    # 导入别的包：盖过默认校准，颜色跟着变；恢复默认校准：回到默认，不再下载，导出又和 A 一样。
    o = steps["otherB"]
    assert o["state"]["kind"] == "user" and o["after"]["calib"] == "user"
    assert "已导入" in o["after"]["text"] and "恢复默认校准" in o["after"]["text"] and "换成自己的校准…" in o["after"]["text"]
    assert _shared(tmp_path, "other-", o["files"]) != default_bytes
    r = steps["restoredB"]
    assert r["state"]["kind"] == "default" and r["after"]["calib"] == "default" and "默认校准（基于样机）" in r["after"]["text"]
    assert _shared(tmp_path, "restored-", r["files"]) == default_bytes
    assert r["fetches"] == 1
    assert steps["reopenedB"]["state"]["kind"] == "default" and steps["reopenedB"]["fetches"] == 2


@pytest.mark.browser_serial
@pytest.mark.parametrize("mode", ["missing", "tampered"])
def test_missing_or_tampered_default_falls_back_to_neutral_with_a_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    if mode == "tampered":
        blob = bytearray(BUNDLED.read_bytes())
        blob[len(blob) // 2] ^= 0x01
        tampered = tmp_path / "tampered.zip"
        tampered.write_bytes(bytes(blob))
        monkeypatch.setenv("GXR_MOBILE_DEFAULT_CALIB", str(tampered))
    else:
        monkeypatch.setenv("GXR_MOBILE_DEFAULT_CALIB", "off")
    data = _run(tmp_path, mode, {})
    steps = data["steps"]
    reason = "默认校准没有下载下来，可能没联网。" if mode == "missing" else "默认校准文件没有通过核对。"
    assert steps["start"]["cards"] == 0
    assert steps["bootState"] == {"kind": None, "loading": False, "error": reason, "units": []}
    broken = steps["broken"]
    assert broken["strip"]["state"] == "uncalibrated" and broken["tag"] == "未校准"
    assert broken["reason"] == "default" and "未校准：" in broken["banner"] and reason in broken["banner"]
    assert "中性转换" in broken["banner"]
    assert broken["sheet"]["calib"] == "none" and broken["sheet"]["detail"] == "未校准"
    assert reason in broken["sheet"]["text"] and "选择校准数据文件" in broken["sheet"]["text"]
    assert broken["fetches"] == 1
    if mode == "missing":
        _check_book(steps["book"])


# 导出前最多等默认校准这么久（mobile/src/app/export_calib_wait.ts 的 CALIB_WAIT_MS）。
CALIB_WAIT_MS = 4000


GATE_UNIT = REPO / "tests" / "support" / "mobile_calib_gate_unit.mjs"


def test_queued_export_taps_share_one_wait() -> None:
    """calibGate：等的时候连点只等一次、按先后开始（去重照旧）；到时以后再点不再等；清空照片后的新点击不多等。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required")
    result = subprocess.run(
        [node, "--experimental-strip-types", "--test", str(GATE_UNIT)], cwd=REPO, check=False, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-2000:]


def test_export_wait_is_short_and_matches_the_source() -> None:
    src = (REPO / "mobile" / "src" / "app" / "export_calib_wait.ts").read_text(encoding="utf-8")
    assert f"export const CALIB_WAIT_MS = {CALIB_WAIT_MS};" in src
    assert 1000 <= CALIB_WAIT_MS <= 6000


@pytest.mark.browser_serial
def test_export_started_while_the_default_downloads_waits_for_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """默认校准还在路上（开发服务器压着不回）时点导出：落定了就按它导出，和落定后再导出的字节一样；
    一直不来就等满 CALIB_WAIT_MS 照旧开始（中性转换），不卡住。连点「存到相册」只等一次、只做一批；关机图也等。"""
    hold = tmp_path / "hold"
    hold.mkdir()
    monkeypatch.setenv("GXR_MOBILE_DEFAULT_CALIB", f"hold:{hold}")
    data = _run(tmp_path, "held", {"holdDir": str(hold)})
    a = data["steps"]["heldA"]
    # 点导出的时候默认校准还没到；一秒后这一批还没开始（在等它），而不是先按中性转换做起来。
    assert a["before"]["kind"] is None and a["before"]["loading"]
    assert a["waiting"] == {"batch": False, "loading": True}
    assert a["landed"]["kind"] == "default"
    held = _shared(tmp_path, "held-", a["heldFiles"])
    assert held == _shared(tmp_path, "after-", a["afterFiles"]), "an export started while the default was downloading came out uncalibrated"
    b = data["steps"]["heldB"]
    # 一直不放行：等满几秒就照旧开始，用的是中性转换，和放行后校准过的导出不同。
    assert CALIB_WAIT_MS - 500 <= b["startedAfter"] <= CALIB_WAIT_MS + 8000, b["startedAfter"]
    assert b["stillLoading"] == {"loading": True, "kind": None}
    timed_out = _shared(tmp_path, "timeout-", b["timeoutFiles"])
    later = _shared(tmp_path, "later-", b["laterFiles"])
    assert later == held and timed_out != later
    # 编辑器里连点三次「存到相册」，默认校准还压着，一秒后放行：只做一批一张，按默认校准做，之后也没有第二批。
    c = data["steps"]["quickC"]
    assert c["waiting"] == {"batch": False, "loading": True}
    assert c["ready"]["items"] == 1 and c["ready"]["phase"] == "ready" and c["ready"]["first"], c["ready"]
    assert c["settled"]["shares"] == 1 and c["settled"]["batch"]["first"], c["settled"]
    quick_held = _shared(tmp_path, "quick-held-", c["quickFiles"])
    assert quick_held == _shared(tmp_path, "quick-after-", c["quickAfter"]), "quick export during the wait came out uncalibrated"
    # 一直不放行：三次点击只等一次（约 4 秒，不是 12 秒），一批一张、中性转换；开始后再点、分享后再等 9 秒都不会再做一批。
    d = data["steps"]["quickD"]
    assert CALIB_WAIT_MS - 500 <= d["startedAfter"] <= CALIB_WAIT_MS + 3000, d["startedAfter"]
    assert d["started"]["items"] == 1 and d["started"]["first"], d["started"]
    assert d["ready"]["items"] == 1 and d["ready"]["first"], d["ready"]
    assert d["later"]["shares"] == 1 and d["later"]["batch"]["first"] and d["later"]["loading"], d["later"]
    quick_timeout = _shared(tmp_path, "quick-timeout-", d["timeoutFiles"])
    assert quick_timeout != _shared(tmp_path, "quick-calibrated-", d["calibratedFiles"]), "the timed-out export should be neutral"
    # 关机图：默认校准还压着时打开，一秒后放行：预览一直等着，做出来的 GXROFF.JPG 和落定后再做的一样。
    e = data["steps"]["offE"]
    assert e["waiting"] == {"ready": False, "loading": True}, e["waiting"]
    off_held = _shared(tmp_path, "off-held-", e["heldFiles"])
    off_after = _shared(tmp_path, "off-after-", e["afterFiles"])
    assert off_held == off_after, "GXROFF.JPG made while the default was downloading came out uncalibrated"


def _check_book(book: dict) -> None:
    fresh = book["fresh"]
    assert fresh["beforeRestore"] == 0, "the default is fetched only after the stored pack is known to be absent"
    assert fresh["loading"]["loading"] and fresh["loading"]["unit"] == "checking" and fresh["loading"]["kind"] is None
    assert fresh["ready"]["kind"] == "default" and fresh["ready"]["name"] == "默认校准（基于样机）" and fresh["ready"]["calls"]["default"] == 1
    assert fresh["bumped"]
    late = book["late"]
    assert late["imported"] is True and late["view"]["kind"] == "user" and late["sameGeneration"]
    restored = book["restored"]
    assert restored["view"]["kind"] == "default" and restored["view"]["calls"]["default"] == 1 and restored["bumped"]
    failed = book["failed"]
    offline, bad = "默认校准没有下载下来，可能没联网。", "默认校准文件没有通过核对。"
    for case, text in (("offline", offline), ("tampered", bad), ("crashed", offline)):
        got = failed[case]
        assert got["kind"] is None and got["error"] == text and got["unit"] == "none" and not got["loading"], case
    assert failed["retrying"]["loading"] and failed["retrying"]["calls"]["default"] == 2
    assert failed["retried"]["kind"] == "default" and failed["retried"]["error"] is None
    assert book["stored"]["kind"] == "user" and book["stored"]["calls"]["default"] == 0
    broken = book["storedBroken"]
    assert broken["kind"] == "default" and broken["packError"] == "校准数据被改动，请重新导入" and broken["calls"]["default"] == 1
    deadline = book["deadline"]
    # 一轮一个截止时间：150 ms 后再等的人和第一个人一起在 ~300 ms 好，不是 ~450 ms。
    assert 250 <= deadline["firstMs"] < 400 and 250 <= deadline["secondMs"] < 400, deadline
    assert deadline["afterDeadlineMs"] < 60, deadline
    assert 90 <= deadline["nextRoundMs"] < 400 and deadline["loading"] and deadline["calls"]["default"] == 2, deadline
    bad = book["badImport"]
    assert bad["before"]["error"] == "默认校准没有下载下来，可能没联网。" and bad["before"]["calls"]["default"] == 1
    assert bad["imported"] is False and not bad["flashed"], bad
    assert bad["after"]["calls"]["default"] == 1 and bad["after"]["error"] == bad["before"]["error"] and not bad["after"]["loading"], bad
    settled = book["settled"]
    assert settled["idle"] and settled["landed"] and settled["listenersAfterLanded"] == settled["listenersBefore"]
    assert settled["timedOut"] and settled["listenersAfterTimeout"] == settled["listenersBefore"]
    assert 40 <= settled["timeoutMs"] < 1000 and settled["stillLoading"]
