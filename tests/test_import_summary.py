"""界面里"导入结果怎么说"和"没有 JPG 的照片画面是什么"的文字规则。

这些规则放在不碰页面的纯函数模块里（web/assets/import_summary.js、look_notes.js），这里用 node 直接跑它们。
没有 node 的机器跳过（走查脚本本来就要 node）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ASSETS = Path(__file__).resolve().parents[1] / "web" / "assets"
node = shutil.which("node")
needs_node = pytest.mark.skipif(node is None, reason="没有 node")


def run_js(expr: str):
    """在 node 里 import 两个纯函数模块，对 expr 求值并返回 JSON 结果。"""
    script = (
        f"import * as S from {json.dumps((ASSETS / 'import_summary.js').as_uri())};\n"
        f"import * as L from {json.dumps((ASSETS / 'look_notes.js').as_uri())};\n"
        f"console.log(JSON.stringify({expr}));\n"
    )
    out = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


BASE = dict(
    state="done",
    imported_shots=14,
    seen_shots=0,
    duplicate_shots=1,
    failed_shots=0,
    completed_shots=0,
    unreadable=[],
    verify_failed=[],
    scan_errors=[],
    message="",
    coverage=dict(complete=True, dcim="present", total=15, covered=15, uncovered=[], not_importable=[], all_imported=True),
)


def progress(**kw) -> str:
    return json.dumps({**BASE, **kw})


@needs_node
def test_clean_import_may_say_eject():
    r = run_js(f"[S.summarizeImport({progress()}), S.canEject({progress()})]")
    assert r[1] is True and r[0]["kind"] == "ok" and r[0]["text"] == "已导入 14 张照片"


@needs_node
@pytest.mark.parametrize(
    "kw",
    [
        dict(unreadable=["DCIM/100RICOH/R0000104.JPG"], failed_shots=1),
        dict(verify_failed=["DCIM/100RICOH/R0000102.JPG"], failed_shots=1),
        dict(scan_errors=["DCIM/101RICOH/"]),
        dict(unreadable=["a/b.DNG"], verify_failed=["a/c.JPG"], scan_errors=["x/"], failed_shots=2, completed_shots=1),
    ],
)
def test_any_problem_means_no_eject_and_a_red_banner(kw):
    p = progress(**kw)
    r = run_js(f"[S.summarizeImport({p}), S.canEject({p}), S.doneStatusText({p})]")
    summary, can_eject, status = r
    assert can_eject is False
    assert summary["kind"] == "bad" and summary["retry"] is True
    assert "安全弹出" not in summary["text"] and "安全弹出" not in status
    assert "请勿格式化" in summary["text"] and status == "存储卡：有未导入的照片"
    assert summary["detail"].startswith("未导入：")


@needs_node
def test_problem_banner_names_the_files_and_counts_photos():
    p = progress(
        unreadable=["DCIM/100RICOH/R0000104.JPG"],
        verify_failed=["DCIM/100RICOH/R0000102.DNG"],
        scan_errors=["DCIM/101RICOH/"],
        failed_shots=2,
    )
    (s,) = [run_js(f"S.summarizeImport({p})")]
    assert "R0000104.JPG" in s["detail"] and "R0000102.DNG" in s["detail"] and "文件夹 101RICOH" in s["detail"]
    assert s["text"] == "有 3 个文件未能导入。请勿格式化存储卡，重新插入后再试一次。"


def coverage(**kw) -> dict:
    return {**BASE["coverage"], **kw}


@needs_node
@pytest.mark.parametrize(
    "cov",
    [
        None,  # 没有核对结果
        dict(complete=False),  # 严格扫描不完整：照片数为 0 不代表没有照片
        dict(dcim="link"),
    ],
)
def test_unverifiable_card_is_never_called_all_imported(cov):
    p = progress(imported_shots=0, coverage=None if cov is None else coverage(**cov))
    summary, can_eject, status = run_js(f"[S.summarizeImport({p}), S.canEject({p}), S.doneStatusText({p})]")
    assert can_eject is False and summary["kind"] == "bad" and summary["retry"] is True
    assert summary["text"] == "无法核对存储卡上的照片，请勿格式化存储卡。"
    assert "都已导入" not in summary["text"] and status == "存储卡：有未导入的照片"


@needs_node
def test_uncovered_files_are_problems():
    p = progress(coverage=coverage(covered=14, uncovered=["DCIM/100RICOH/R0000105.JPG"]))
    summary, can_eject = run_js(f"[S.summarizeImport({p}), S.canEject({p})]")
    assert can_eject is False and summary["kind"] == "bad" and "R0000105.JPG" in summary["detail"]
    assert summary["text"] == "有 1 个文件未能导入。请勿格式化存储卡，重新插入后再试一次。"


@needs_node
def test_not_importable_files_are_counted_not_called_all_imported():
    extra = ["DCIM/100RICOH/R0000200.MOV", "DCIM/NOTE.TXT"]
    p = progress(imported_shots=0, duplicate_shots=15, coverage=coverage(not_importable=extra))
    summary, can_eject, status = run_js(f"[S.summarizeImport({p}), S.canEject({p}), S.doneStatusText({p})]")
    assert summary["text"] == "存储卡上有 2 个文件不会被导入" and "R0000200.MOV" in summary["detail"]
    assert can_eject is False and status == "存储卡：已插入"
    p = progress(imported_shots=3, coverage=coverage(not_importable=extra))
    assert run_js(f"S.summarizeImport({p})")["text"] == "已导入 3 张照片。存储卡上有 2 个文件不会被导入"


@needs_node
def test_all_imported_and_videos():
    p = progress(imported_shots=0, duplicate_shots=15)
    assert run_js(f"S.summarizeImport({p})")["text"] == "这张存储卡上的照片都已导入"
    p = progress(imported_shots=2, imported_videos=1)
    summary, can_eject = run_js(f"[S.summarizeImport({p}), S.canEject({p})]")
    assert summary["text"] == "已导入 2 张照片和 1 个视频" and can_eject is True


@needs_node
def test_long_problem_lists_are_cut_off():
    files = [f"DCIM/100RICOH/R00001{i:02d}.JPG" for i in range(10)]
    p = progress(unreadable=files, failed_shots=10)
    assert "等共 10 个" in run_js(f"S.listProblems({p})")


@needs_node
@pytest.mark.parametrize("state", ["interrupted", "paused_no_space", "cancelled", "failed"])
def test_other_states_never_say_eject(state):
    p = progress(state=state, unreadable=["a.JPG"])
    r = run_js(f"[S.summarizeImport({p}), S.canEject({p})]")
    assert "安全弹出" not in r[0]["text"] and r[1] is False


@needs_node
def test_several_cards_are_announced():
    one = run_js("S.multiCardNote([{label:'A'}])")
    two = run_js("S.multiCardNote([{label:'A'},{label:'B'}])")
    assert one is None
    assert two == "已插入 2 张存储卡，先导入「A」"


@needs_node
def test_raw_only_pictures_are_never_called_the_photo_itself():
    look = {
        "stock04-prediction": {"kind": "stock04-prediction", "label": "电脑预测（原厂标准）", "prediction": True},
        "neutral": {"kind": "neutral", "label": "中性转换", "prediction": False},
        "embedded": {"kind": "embedded", "label": "RAW 内嵌的小预览", "prediction": False},
        "jpg": {"kind": "camera-jpg", "label": None, "prediction": False},
    }
    out = run_js(
        f"Object.fromEntries(Object.entries({json.dumps(look)}).map(([k,v]) => [k, "
        "{note: L.lookNoteText(v), orig: L.originalNoteText(v), badge: L.rawOnlyBadgeText(v)}]))"
    )
    pred, neutral, jpg = out["stock04-prediction"], out["neutral"], out["jpg"]
    assert jpg["note"] == "相机 JPG" and "电脑预测" not in jpg["note"]
    assert pred["note"] == "电脑预测" and pred["badge"] == "电脑预测" and pred["orig"] == "原图"
    assert neutral["note"] == "中性转换" and neutral["badge"] == "RAW" and neutral["orig"] == "原图"
    emb = out["embedded"]
    assert emb["note"] == "内嵌预览" and emb["badge"] == "RAW" and emb["orig"] == "原图"
    assert jpg["badge"] == "RAW"
