"""UI-M3-HDR：打开 HDR 导出，在无头 Chrome 里走一遍（`browser_serial`）。

四对 RAW+JPG 和一张只有 JPG 的照片在界面里编辑，开 HDR 导出原始尺寸 / 高，再用记住的开关导出小 / 低。每个文件都和桌面
`export_finish(hdr=True)` 逐字节相同（ICC 时间戳照导出管线的办法固定）；`hdr`、`hdr_reason`、`file_name` 和桌面一样。
也核对面板、进度和结果上的 HDR 文案，HDR 批次的待存上限，以及开着 HDR 时的内存高水位。

照片只经 `support.samples` 找；比较一律走 `support.export_pipeline_cases` 的脱敏函数，不打印照片数据。这个文件在导出管线
隐私 lint 的范围里。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from support import mobile_ui_export as mx
from support import samples
from support.export_pipeline_cases import check, note, same_file_redacted, same_redacted
from test_mobile_ui_export_browser import RESULT_KEYS, Desktop, _local_dat, _prereq, pinned_icc, run_driver

pytestmark = pytest.mark.browser_serial

REPO = Path(__file__).resolve().parents[1]
HDR_PARITY = REPO / "tests" / "support" / "mobile_ui_export_hdr_parity.mjs"
MIB = 1024 * 1024
HDR_FIELDS = ("file_name", "hdr", "hdr_reason", *RESULT_KEYS)

__all__ = ["pinned_icc"]


def _desktop_hdr(desk: Desktop, shot: dict[str, Any], side: dict[str, Any], quality: int, long_edge: int | None) -> Any:
    from gxr_studio.finish.serve import export_finish
    from gxr_studio.render.params import parse_edit

    dest = desk.export_dir / f"{shot['name']}-hdr-{quality}-{long_edge}"
    return export_finish(
        desk.render, desk.lib, shot, parse_edit(side), side, dest, [], quality=quality, long_edge=long_edge, fmt="jpeg", base="auto", hdr=True
    )


def _hdr_text(outcomes: list[tuple[str, bool, str | None]]) -> tuple[str, list[str]]:
    """The 05c / 05e line and per-photo notes the phone should show for these desktop outcomes
    (design wording; desktop ``hdrSummary`` and ``HDR_NOTES``)."""
    from gxr_studio.api.routes import HDR_NOTES, HDR_UNSUPPORTED

    made = sum(1 for _s, hdr, _r in outcomes if hdr)
    jpg_only = sum(1 for _s, hdr, r in outcomes if not hdr and r == "no_raw")
    notes = [f"{s}：{HDR_NOTES.get(r or '', HDR_UNSUPPORTED)}" for s, hdr, r in outcomes if not hdr and r != "no_raw"]
    parts = [f"{made} 张 HDR"] if made else []
    if jpg_only:
        parts.append(f"{jpg_only} 张只有 JPG，存普通版本")
    if notes:
        parts.append(f"{len(notes)} 张未生成 HDR")
    return " · ".join(parts), notes


def _memory(facts: dict[str, Any]) -> dict[str, Any]:
    mem = facts["mem"]
    return {
        "wasm_mib": round(mem["wasm"] / MIB, 1),
        "js_mib": round(mem["js"] / MIB, 1),
        "staged_mib": round(facts["stagedHigh"] / MIB, 1),
        "workers": mem["workers"],
        "ms_per_photo": [round(t) for t in facts["times"]],
    }


def test_mobile_ui_export_hdr_files_match_desktop(tmp_path: Path, pinned_icc: bytes) -> None:  # noqa: F811
    """UI-M3-HDR item 5: four pairs and a JPG-only photo with HDR on equal desktop ``export_finish(hdr=True)``."""
    pairs = mx.real_pairs(4)
    if len(pairs) < 4:
        samples.missing(f"need four DNG+JPG pairs in {mx.SAMPLE_DIR}")
    dat = _local_dat()
    _prereq()
    desk = Desktop(tmp_path, dat)
    try:
        jpg_only = mx.copy_as(pairs[1][1], tmp_path / "R9990002.JPG")
        photos: dict[str, tuple[Path | None, Path | None]] = {dng.stem: (dng, jpg) for dng, jpg in pairs}
        photos[jpg_only.stem] = (None, jpg_only)
        stems = list(photos)
        a, b, c, d = (p[0].stem for p in pairs)
        # A: RAW recipe + knobs. B: untouched (the camera-JPG original branch). C: knobs only (edited
        # camera JPG). D: another recipe. The JPG-only photo: knobs (HDR gives no_raw).
        plans: dict[str, Any] = {
            a: {"card": "builtin:1", "knobs": {"exposure_ev": 0.3, "contrast": 12}},
            b: {},
            c: {"knobs": {"shadow": 20, "warm": -8}},
            d: {"card": "builtin:2"},
            jpg_only.stem: {"knobs": {"exposure_ev": -0.2, "saturation": 15}},
        }
        files = [str(p) for pair in pairs for p in pair] + [str(jpg_only), str(dat)]
        out_dir = tmp_path / "phone"
        out_dir.mkdir()
        report = run_driver(
            tmp_path,
            {
                "mode": "parity",
                "parityDriver": str(HDR_PARITY),
                "files": files,
                "pack": str(desk.zip),
                "count": len(stems),
                "plans": plans,
                "order": stems,
                "smallOrder": stems,
                "jpgOnly": jpg_only.stem,
                "binding": desk.binding(),
                "outDir": str(out_dir),
            },
            timeout=2400,
        )
        check(report["pageErrors"] == [], "no page errors")
        steps = report["steps"]
        full, small = steps["full"], steps["small"]

        # 05a: off by default, enabled with RAW selected, the design line; the mix line only when on.
        before, after = full["before"], full["after"]
        check(before["disabled"] is False and before["checked"] is False, "HDR switch enabled, off by default")
        check(before["role"] == "switch", "HDR switch announces itself as a switch")
        check("在 iPhone 上看，亮处会更亮，更接近你当时看到的；别的设备照常显示。" in before["text"], "05a HDR line")
        check("这项下一步做" not in full["sheet"]["text"], "no 下一步做 label on the HDR row")
        check(before["mix"] is not None and before["mix"]["visible"] is False, "mix line hidden while HDR is off")
        check(after["checked"] is True and after["mix"]["visible"] is True, "mix line shown once HDR is on")
        check(after["mix"]["text"] == "HDR 需要 RAW 文件：1 张只有 JPG，会存普通版本。", "05a mix line wording")
        check(full["stored"] == "1", "choice remembered in gxr.mobile.export_hdr")
        check(small["row"]["checked"] is True and small["row"]["disabled"] is False, "remembered choice used next time")
        jpg_row = steps["jpgOnly"]["row"]
        check(jpg_row["disabled"] is True and jpg_row["checked"] is False, "JPG-only selection: switch disabled")
        check("HDR 需要 RAW 文件" in jpg_row["text"], "JPG-only selection: the desktop HDR_NO_RAW line")
        check(steps["jpgOnly"]["stored"] == "1", "a disabled switch does not change the remembered choice")

        check(full["cap"] == 256 * MIB, "HDR batches use the lower staging cap")

        edits = {e["stem"]: e for e in steps["edits"]}
        shots = {stem: desk.shot(stem, *photos[stem]) for stem in stems}
        sides = {stem: desk.sidecar(shots[stem], desk.changes(shots[stem], edits[stem])) for stem in stems}
        table: dict[str, int] = {}
        for tag, step, quality, long_edge in (("full-", full, 95, None), ("small-", small, 70, 1600)):
            outcomes: list[tuple[str, bool, str | None]] = []
            for stem, fact in step["facts"]["facts"].items():
                shot, side = shots[stem], sides[stem]
                res = _desktop_hdr(desk, shot, side, quality, long_edge)
                path = out_dir / f"{tag}{res.path.name}"
                phone = path.read_bytes() if path.is_file() else None
                same_file_redacted(phone, res.path.read_bytes(), "phone HDR file equals desktop export_finish(hdr=True) (ICC pinned)")
                got = {k: fact["result"].get(k) for k in HDR_FIELDS}
                want = {"file_name": res.path.name, "hdr": res.hdr, "hdr_reason": res.hdr_reason, **{k: getattr(res, k) for k in RESULT_KEYS}}
                same_redacted(got, want, "result JSON equals the desktop ExportResult (hdr, hdr_reason, file_name)")
                check(fact["result"]["file_name"].endswith("_hdr.jpg") == (fact["result"]["hdr"] is True), "_hdr exactly when hdr is true")
                key = f"{tag}{'hdr' if res.hdr else 'sdr/' + str(res.hdr_reason)}"
                table[key] = table.get(key, 0) + 1
                outcomes.append((stem, bool(res.hdr), res.hdr_reason))
            # 05b / 05c / 05e: the wording follows the desktop outcome of the same photos.
            line, notes = _hdr_text(outcomes)
            check("生成 HDR 版本要多等几秒。可以接着看照片。" in step["running"], "05b HDR line")
            check("5 张已做好" in step["ready"], "05c main line")
            check(line in step["ready"].splitlines(), "05c HDR line from the desktop outcomes")
            check(all(n in step["ready"].splitlines() for n in notes), "05c per-photo HDR notes (desktop HDR_NOTES)")
            check(line in step["shared"]["saved"].splitlines(), "05e HDR line")
            check("好了：5 张" in step["shared"]["saved"] and "已存到相册" not in step["shared"]["saved"], "05e main line")
            check('选了「存储图像」的在相册里，选了「存储到"文件"」的在「文件」里。' in step["shared"]["saved"], "05e share-done line (HDR)")
            check(all(c["active"] is True for c in step["files"]), "share() called inside the tap")
        check(sum(table.values()) == 10, "five full-size and five small HDR exports compared")
        check(table.get("full-sdr/no_raw") == 1 and table.get("small-sdr/no_raw") == 1, "the JPG-only photo is no_raw at both sizes")
        check(table.get("full-hdr", 0) >= 2 and table.get("small-hdr", 0) >= 2, "at least two pairs made HDR at each size")

        mem = full["facts"]["mem"]
        check(0 < mem["wasm"] <= 640 * MIB, "full-size HDR export WASM high-water mark within 640 MiB")
        note("ui_m3_hdr_parity", table)
        note("ui_m3_hdr_memory", {"full": _memory(full["facts"]), "small": _memory(small["facts"])})
    finally:
        desk.lib.close()
