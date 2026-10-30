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


BASE = dict(state="done", imported_shots=14, seen_shots=0, duplicate_shots=1, failed_shots=0, completed_shots=0,
            unreadable=[], verify_failed=[], scan_errors=[], message="")


def progress(**kw) -> str:
    return json.dumps({**BASE, **kw})


@needs_node
def test_clean_import_may_say_eject():
    r = run_js(f"[S.summarizeImport({progress()}), S.canEject({progress()})]")
    assert r[1] is True and r[0]["kind"] == "ok" and "可以安全弹出卡了" in r[0]["text"]


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
    assert "先别格式化" in summary["text"] and "先别格式化" in status
    assert summary["detail"].startswith("没导进来的：")


@needs_node
def test_problem_banner_names_the_files_and_counts_photos():
    p = progress(unreadable=["DCIM/100RICOH/R0000104.JPG"], verify_failed=["DCIM/100RICOH/R0000102.DNG"],
                 scan_errors=["DCIM/101RICOH/"], failed_shots=2)
    (s,) = [run_js(f"S.summarizeImport({p})")]
    assert "R0000104.JPG" in s["detail"] and "R0000102.DNG" in s["detail"] and "文件夹 101RICOH" in s["detail"]
    assert "有 2 张没有完整导进来" in s["text"] and "共 3 个文件" in s["text"]


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
    assert "2 张卡" in two and "「A」" in two and "「B」" in two and "一次只处理第一张" in two


@needs_node
def test_raw_only_pictures_are_never_called_the_photo_itself():
    look = {"stock04-prediction": {"kind": "stock04-prediction", "label": "电脑预测（原厂标准）", "prediction": True},
            "neutral": {"kind": "neutral", "label": "中性转换", "prediction": False},
            "embedded": {"kind": "embedded", "label": "RAW 内嵌的小预览", "prediction": False},
            "jpg": {"kind": "camera-jpg", "label": None, "prediction": False}}
    out = run_js(f"Object.fromEntries(Object.entries({json.dumps(look)}).map(([k,v]) => [k, "
                 "{note: L.lookNoteText(v), orig: L.originalNoteText(v), badge: L.rawOnlyBadgeText(v)}]))")
    pred, neutral, jpg = out["stock04-prediction"], out["neutral"], out["jpg"]
    assert "不是预测" in jpg["note"] and "照片本身" in jpg["note"]
    assert "电脑预测（原厂标准）" in pred["note"] and "照片本身" not in pred["note"] and "不是相机拍出的 JPG" in pred["note"]
    assert "电脑预测" in pred["orig"] and "照片本身" not in pred["orig"] and "电脑预测" in pred["badge"]
    assert "中性转换" in neutral["note"] and "照片本身" not in neutral["note"] and "不是相机的颜色" in neutral["note"]
    assert "不是相机的颜色" in neutral["orig"] and "照片本身" not in neutral["orig"] and "中性" in neutral["badge"]
    emb = out["embedded"]
    assert "内嵌的小预览" in emb["note"] and "不是配方预测" in emb["note"] and "照片本身" not in emb["note"]
    assert "内嵌" in emb["orig"] and "照片本身" not in emb["orig"] and "预测" not in emb["badge"]
    assert jpg["badge"] == "只有 DNG"
