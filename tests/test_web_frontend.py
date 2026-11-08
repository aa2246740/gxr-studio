"""前端第二阶段的纯函数测试（geom.js、finish.js）和 COPY.md 的几条硬规则。

纯函数用 node 直接跑（和 test_import_summary.py 一样，不碰页面）；文案规则只读源文件。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ASSETS = Path(__file__).resolve().parents[1] / "web" / "assets"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node，跳过前端纯函数测试")


def run_node(tmp_path: Path, body: str) -> object:
    """把 geom.js、finish.js 放进临时目录里的 .mjs 副本运行，只读取结果，不在源目录里执行任何东西。"""
    for name in ("geom.js", "finish.js"):
        (tmp_path / name).write_text((ASSETS / name).read_text(encoding="utf-8"), encoding="utf-8")
    script = tmp_path / "check.mjs"
    script.write_text(
        "import * as G from './geom.js';\nimport * as F from './finish.js';\n"
        f"const out = (() => {{\n{body}\n}})();\nconsole.log(JSON.stringify(out));\n",
        encoding="utf-8",
    )
    res = subprocess.run(["node", str(script)], capture_output=True, text=True, cwd=tmp_path, encoding="utf-8")
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("previous-photo-after-import", {"before": "none", "current": "full", "revisited": "full", "requests": 2, "metaRequests": 1}),
        ("data-change-pending-caps", {"resolved": "full", "cached": "full", "requests": 2}),
        ("data-change-pending-catalog", {"resolved": 10, "cached": 10, "requests": 2}),
        ("data-change-pending-caps-error", {"resolved": "full", "cached": "full", "requests": 2}),
        ("data-change-pending-catalog-error", {"resolved": 10, "cached": 10, "requests": 2}),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_recipe_cache_after_data_change(tmp_path, scenario, expected):
    """运行实际缓存模块，用延迟完成的请求检查旧视图拿到的结果。"""
    modules = tmp_path / "modules"
    modules.mkdir()
    for path in ASSETS.glob("*.js"):
        shutil.copyfile(path, modules / path.name)
    (modules / "package.json").write_text('{"type":"module"}\n', encoding="utf-8")
    (modules / "api.js").write_text(
        "export class ApiError extends Error {}\n"
        "export const api = (...args) => globalThis.cacheApi(...args);\n"
        "export async function apiBlob() { throw new Error('本场景不读取图片'); }\n",
        encoding="utf-8",
    )
    script = Path(__file__).parent / "support/recipe_cache.mjs"
    result = subprocess.run(["node", str(script), str(modules), scenario], capture_output=True, text=True, timeout=30, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == expected


# ---- 裁切框的几何（geom.js） ------------------------------------------------------


def test_fit_into_keeps_a_rect_that_is_already_inside(tmp_path):
    out = run_node(tmp_path, "const v={x:0.1,y:0.1,w:0.8,h:0.8}; return G.fitInto({x:0.2,y:0.2,w:0.5,h:0.5}, v);")
    assert out["changed"] is False
    assert out["rect"] == {"x": 0.2, "y": 0.2, "w": 0.5, "h": 0.5}


def test_fit_into_moves_a_rect_that_only_crosses_the_edge(tmp_path):
    out = run_node(tmp_path, "const v={x:0,y:0,w:1,h:1}; return G.fitInto({x:0.8,y:0.2,w:0.4,h:0.4}, v);")
    assert out["changed"] is True
    assert out["rect"]["w"] == pytest.approx(0.4) and out["rect"]["x"] + out["rect"]["w"] <= 1 + 1e-9


def test_fit_into_shrinks_a_rect_bigger_than_the_valid_area(tmp_path):
    out = run_node(tmp_path, "const v={x:0.2,y:0.2,w:0.6,h:0.6}; return G.fitInto({x:0,y:0,w:1,h:1}, v);")
    r = out["rect"]
    assert out["changed"] is True
    assert r["w"] == pytest.approx(0.6) and r["h"] == pytest.approx(0.6)
    assert r["x"] >= 0.2 - 1e-9 and r["x"] + r["w"] <= 0.8 + 1e-9


def test_rotate_four_times_and_flip_twice_are_identity(tmp_path):
    out = run_node(
        tmp_path,
        "let r={x:0.1,y:0.2,w:0.5,h:0.3}; for(let i=0;i<4;i++) r=G.rotateRect90(r);"
        "const f=G.flipRect(G.flipRect({x:0.1,y:0.2,w:0.5,h:0.3}));"
        "return {r, f};",
    )
    original = {"x": 0.1, "y": 0.2, "w": 0.5, "h": 0.3}
    assert all(abs(out["r"][k] - v) < 1e-9 for k, v in original.items())
    assert all(abs(out["f"][k] - v) < 1e-9 for k, v in original.items())


def test_rotate90_maps_the_rect_the_way_the_picture_turns(tmp_path):
    # 逆时针转 90°：左上角那块跑到左下角（y 变成 1 - x - w）
    out = run_node(tmp_path, "return G.rotateRect90({x:0,y:0,w:0.25,h:0.5});")
    assert out == {"x": 0, "y": 0.75, "w": 0.5, "h": 0.25}


def test_legacy_valid_region_is_the_full_picture_when_straight_and_shrinks_when_tilted(tmp_path):
    out = run_node(
        tmp_path,
        "const c={w:4288,h:2848}; const a=G.legacyValid(0,c); const b=G.legacyValid(8,c);return {a, b};",
    )
    assert out["a"] == {"x": 0, "y": 0, "w": 1, "h": 1}
    # 8° 拉直后，同比例的有效画面约为画布的 83%（4288 × 2848 的 3:2 画布，与 crop.py 的 box_inside 一致）
    assert 0.8 < out["b"]["w"] < 0.9 and out["b"]["w"] == pytest.approx(out["b"]["h"])


def test_max_rect_keeps_the_ratio_and_stays_centred(tmp_path):
    out = run_node(
        tmp_path,
        "const c={w:4288,h:2848}; const v={x:0,y:0,w:1,h:1};return G.maxRectFor(v, c, 1);",
    )
    # 1:1 的最大框：高是 2848，宽也是 2848（归一化后宽 = 2848 / 4288）
    assert out["h"] == pytest.approx(1.0)
    assert out["w"] == pytest.approx(2848 / 4288)
    assert out["x"] == pytest.approx((1 - out["w"]) / 2)


def test_inside_straightened_is_the_server_check(tmp_path):
    """前端收框的判断和后端校验框的 crop.box_inside 一样（在画布内，四角按拉直反转回去也在原画面内）。"""
    import random

    from gxr_studio.crop import box_inside

    rng = random.Random(3)
    cases = []
    for _ in range(400):
        w_px, h_px = rng.choice([(4288, 2848), (2848, 4288), (3000, 3000)])
        angle = rng.uniform(-15, 15)
        w, h = rng.uniform(0.2, 1.0), rng.uniform(0.2, 1.0)
        x, y = rng.uniform(-0.05, 1.05 - w), rng.uniform(-0.05, 1.05 - h)
        cases.append([w_px, h_px, angle, x, y, w, h])
    body = "([W, H, a, x, y, w, h]) => G.insideStraightened({x, y, w, h}, {w: W, h: H}, a)"
    got = run_node(tmp_path, f"return {json.dumps(cases)}.map({body});")
    assert isinstance(got, list)
    for case, inside in zip(cases, got, strict=True):
        w_px, h_px, angle, x, y, w, h = case
        on_canvas = x >= -1e-9 and y >= -1e-9 and x + w <= 1 + 1e-9 and y + h <= 1 + 1e-9
        assert inside == (on_canvas and box_inside((x * w_px, y * h_px, w * w_px, h * h_px), w_px, h_px, angle, 1e-6)), case
    assert 50 < sum(got) < 350  # 两种结果都有足够多


def test_fit_straightened_moves_first_then_shrinks(tmp_path):
    out = run_node(
        tmp_path,
        "const c = {w: 4288, h: 2848};"
        "const best = G.centeredMaxRect(c, 1, 4);"
        "const moved = G.fitStraightened({...best, x: best.x + 0.05}, c, 4);"
        "const whole = G.fitStraightened({x: 0, y: 0, w: 1, h: 1}, c, 4);"
        "const small = G.fitStraightened({x: 0.4, y: 0.4, w: 0.1, h: 0.1}, c, 4);"
        "return {best, moved, whole, small, canvasBox: G.centeredMaxRect(c, null, 4),"
        " ok: [moved.rect, whole.rect].map((r) => G.insideStraightened(r, c, 4))};",
    )
    assert isinstance(out, dict)
    assert out["small"]["changed"] is False
    assert out["ok"] == [True, True]
    # 最大的 1:1 只能放在中心：挪回中心，大小不变
    assert out["moved"]["changed"] is True
    assert out["moved"]["rect"]["w"] == pytest.approx(out["best"]["w"])
    assert out["moved"]["rect"]["x"] == pytest.approx(out["best"]["x"], abs=1e-6)
    # 整张画布挪不进去：在中心等比缩小到拉直以后最大的同比例框
    for k in ("x", "y", "w", "h"):
        assert out["whole"]["rect"][k] == pytest.approx(out["canvasBox"][k], abs=1e-6)


def test_fit_straightened_keeps_the_ratio_of_a_box_bigger_than_the_canvas(tmp_path):
    """PR #39 复审（Grok 第三轮）：角拉出画面、框比画布还宽时，宽高按同一个比例缩（原来各自截到 1，16:9 变成 1.65）。"""
    out = run_node(
        tmp_path,
        "const c = {w: 4288, h: 2848};"
        "const wide = {x: 0, y: 0.1, w: 1.08, h: 1.08 * 4288 / 16 * 9 / 2848};"
        "return [0, 4, -8].map((a) => { const f = G.fitStraightened(wide, c, a); "
        "return {ratio: (f.rect.w * c.w) / (f.rect.h * c.h), ok: G.insideStraightened(f.rect, c, a), w: f.rect.w}; });",
    )
    assert isinstance(out, list)
    for case in out:
        assert case["ratio"] == pytest.approx(16 / 9, rel=1e-9) and case["ok"] is True and case["w"] <= 1 + 1e-12, case


def test_crop_tilt_turns_around_when_the_picture_is_mirrored(tmp_path):
    geos = "[{straighten: 8}, {straighten: 8, flip: true}, {straighten: 8, rotate90: 1}, null]"
    out = run_node(tmp_path, f"return {geos}.map((g) => G.cropTilt(g));")
    assert out == [8, -8, 8, 0]


def test_resize_with_ratio_lock_keeps_the_pixel_ratio(tmp_path):
    out = run_node(
        tmp_path,
        "const c={w:4000,h:3000}; const r=G.resizeRect({x:0.1,y:0.1,w:0.4,h:0.4},'br',{x:0.7,y:0.5},c,4/3);return {w:r.w*c.w, h:r.h*c.h};",
    )
    assert out["w"] / out["h"] == pytest.approx(4 / 3, rel=1e-6)


def test_array_and_object_rects_round_trip(tmp_path):
    out = run_node(tmp_path, "return {back: G.toArray(G.fromArray([0.1,0.2,0.3,0.4]))};")
    assert out["back"] == [0.1, 0.2, 0.3, 0.4]


def test_fmt_deg_writes_one_decimal_with_sign(tmp_path):
    out = run_node(tmp_path, "return {a: G.fmtDeg(1.23), b: G.fmtDeg(-3), c: G.fmtDeg(0)};")
    assert out == {"a": "+1.2°", "b": "-3.0°", "c": "0.0°"}


# ---- 默认值和优先级（finish.js） ------------------------------------------------


def test_photo_own_frame_wins_over_the_default(tmp_path):
    out = run_node(
        tmp_path,
        "const d={frame:{style:'gallery'}, stamp:{enabled:true}};"
        "const own=F.resolveFinish({frame:{style:'leica'}}, d);"
        "const dflt=F.resolveFinish({}, d);"
        "return {own:[own.frame.style, own.frameFrom], dflt:[dflt.frame.style, dflt.frameFrom, dflt.stamp.enabled]};",
    )
    assert out["own"] == ["leica", "photo"]
    assert out["dflt"] == ["gallery", "default", True]


def test_no_default_means_no_frame_and_stamp_off(tmp_path):
    out = run_node(tmp_path, "const r=F.resolveFinish({}, null); return {s:r.frame.style, e:r.stamp.enabled};")
    assert out == {"s": "none", "e": False}


def test_null_style_is_read_as_none(tmp_path):
    out = run_node(tmp_path, "return F.normalizeFrame({style:null}).style;")
    assert out == "none"


def test_text_is_capped_at_24_characters(tmp_path):
    out = run_node(tmp_path, "return F.clipText('一二三四五六七八九十一二三四五六七八九十一二三四五六七八九十').length;")
    assert out == 24


def test_only_real_dates_are_accepted(tmp_path):
    out = run_node(tmp_path, "return ['2026-10-23','2026-02-30','2026-1-3','abc'].map(F.isDateString);")
    assert out == [True, False, False, False]


@pytest.mark.parametrize(
    ("fmt", "expect"),
    [
        ("'yy mm dd", "'26 10 23"),
        ("yy mm dd", "26 10 23"),
        ("yyyy.mm.dd", "2026.10.23"),
        ("mm dd 'yy", "10 23 '26"),
    ],
)
def test_stamp_date_formats_match_the_four_options(tmp_path, fmt, expect):
    out = run_node(tmp_path, f"return F.stampDateText({json.dumps(fmt)}, '2026-10-23', '14:05', false);")
    assert out == expect


def test_stamp_line_adds_time_and_text_and_skips_empty_parts(tmp_path):
    out = run_node(
        tmp_path,
        "const p={date:'2026-10-23', time:'14:05'};"
        "return {a: F.stampLine({content:'date+text', date_format:'yy mm dd', show_time:true, text:'TOKYO', date_override:null}, p),"
        " b: F.stampLine({content:'text', date_format:'yy mm dd', text:'GXR', date_override:null}, p)};",
    )
    assert out == {"a": "26 10 23 14:05  TOKYO", "b": "GXR"}


def test_frame_body_never_sends_a_frame_number_switch(tmp_path):
    # 胶片的帧号由样式自带，不单独做开关（COPY.md 3.9），所以 frame 里不发 show_frame_no
    out = run_node(
        tmp_path,
        "const f=F.normalizeFrame({style:'film'});return Object.keys(F.frameBody(f)).includes('show_frame_no');",
    )
    assert out is False


# ---- COPY.md 的硬规则（只读源文件） -------------------------------------------------


# 大图标签：配方 / 调整 / 裁剪 / 边框 / 时间戳 / 信息（COPY.md 第 1 节和 3 节；ORCH_DECISIONS：导出改成对话框 ⌘E，加「信息」I）
def test_viewer_tabs_use_the_copy_names_in_order():
    viewer = (ASSETS / "viewer.js").read_text(encoding="utf-8")
    tabs = re.findall(r"\['(recipe|adjust|crop|frame|stamp|info|export)', '([^']+)', '([A-Z])', '[a-z0-9-]+'\]", viewer)
    assert [t[1] for t in tabs] == ["配方", "调整", "裁剪", "边框", "时间戳", "信息"]
    assert [t[2] for t in tabs] == ["R", "A", "C", "F", "T", "I"]


# 这些说法在 COPY.md 里被明确禁止，不能出现在界面文字里（注释不算）
FORBIDDEN = [
    "原图没有被改动",
    "随时可以还原",
    "往右拉",
    "往外鼓",
    "正在看没有任何编辑",
    "还没准备好",
    "全部归零",
    "调色文件",
    "导入相机数据",
    "写回相机",
    "安全弹出",
    "底子",
    "裁切与纠正",
    "比例与框",
    "构图辅助线",
    "调节（仅电脑）",
    "近似（这张没有 RAW）",
    # COPY 3.10 删掉的提示、标签和说法（画面上看得到结果的操作不弹提示）
    "已重置",
    "已自动拉直",
    "没有可以撤销的内容",
    "已存储为我的配方",
    "发现存储卡",
    "未全部导入",
    "照片本身",
    "看原图，松开恢复",
    "确认删除",
    "中位误差",
    "计算方法",
    "与 04 标准几乎相同",
    "输入空间为推测值",
    "LUT 类型",
    "导出内容",
    "精确",
    "请输入文件夹的完整路径",
    "把 LUT 文件拖到这里",
    "相机 ±5",
    "已重建索引（",
    "无法读取。请重试。",
    # COPY 3.11：后端的说明长句不显示
    "标准色块",
    # COPY 3.11：后端已改成「示例照片」，前端不再映射
    "示意图",
    # COPY 3.12：附录里改掉的词
    "双击归零",
    "关掉这条提示",
    "导入结束",
    "/完整路径/到/",
]


def _visible_lines():
    for path in sorted(ASSETS.glob("*.js")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("//"):
                continue
            # 行尾的注释不算界面文字
            yield path.name, lineno, re.sub(r"\s//.*$", "", line)


def test_forbidden_phrases_are_gone_from_user_visible_text():
    hits = [f"{name}:{no}: {w}" for name, no, line in _visible_lines() for w in FORBIDDEN if w in line]
    assert hits == [], "\n".join(hits)


def test_new_panels_use_copy_button_names():
    frame = (ASSETS / "frame_panel.js").read_text(encoding="utf-8")
    stamp = (ASSETS / "stamp_panel.js").read_text(encoding="utf-8")
    crop = (ASSETS / "correct.js").read_text(encoding="utf-8")
    assert "'设为默认'" in frame
    assert "'白边'" in frame and "'徕卡水印'" in frame and "'哈苏水印'" in frame and "'胶片'" in frame
    assert "'数码管'" in stamp and "'打字机'" in stamp and "'无衬线'" in stamp
    assert "'向左旋转'" in crop and "'水平翻转'" in crop and "'重置'" in crop


def test_section_3_9_sentences_are_used_verbatim():
    """COPY.md 3.9 的固定文案：这些句子必须原样出现在前端代码里。"""
    names = ("preview.js", "recipes.js", "import_summary.js", "export.js", "camdata.js")
    texts = {name: (ASSETS / name).read_text(encoding="utf-8") for name in names}
    # 「未校准」的提示气泡不再是一句固定的话，按具体原因写（calibration_note.js，tests/test_feedback_copy.py 测）；
    # 它现在在配方标签里当前配方的下面，不在照片上（tests/test_look_cards.py）
    assert "uncalibratedReason" in texts["recipes.js"] and "使用 A12 50mm 的颜色模型" not in texts["recipes.js"]
    assert "近似效果只支持默认参数" in texts["recipes.js"]
    assert "导入已中断。重新插入存储卡后可以继续。" in texts["import_summary.js"]
    assert "请勿格式化存储卡，重新插入后再试一次。" in texts["import_summary.js"]
    assert "导入失败。请重试。" in texts["import_summary.js"]
    assert "导出失败。请重试。" in texts["export.js"]
    assert "未导入相机配方文件" in texts["camdata.js"]
    assert "校准数据与相机配方文件不匹配" in texts["camdata.js"]


def test_section_3_10_sentences_are_used_verbatim():
    """COPY.md 3.3 和 3.10 的固定文案：配方不能用的说明、LUT 与校准、存储卡、大图、错误。"""
    names = ("recipes.js", "lut.js", "camdata.js", "import.js", "import_summary.js", "onboarding.js", "viewer.js", "stage.js", "export.js")
    texts = {name: (ASSETS / name).read_text(encoding="utf-8") for name in names}
    # 3.3：只有 JPG 时的主句和小字
    assert "配方需要 RAW 文件" in texts["recipes.js"] and "拍摄时将画质设为 RAW+JPEG" in texts["recipes.js"]
    # 3.10 第 4 项：LUT 与校准
    for phrase in ("输入色彩空间", "示例照片", "接近 04 标准", "色差 ", "将 LUT 文件拖到这里，或点击选择", "删除「"):
        assert phrase in texts["lut.js"], phrase
    assert "校准数据文件夹" in texts["camdata.js"] and "请输入完整路径" in texts["camdata.js"]
    # 3.10 第 5 项：存储卡
    assert "已插入存储卡" in texts["import.js"]
    assert "有未导入的照片" in texts["import_summary.js"] and "张存储卡，先导入「" in texts["import_summary.js"]
    # 3.10 第 6、9 项：大图和错误
    assert "查看原图" in texts["onboarding.js"] and "按住空格" in texts["onboarding.js"]
    assert "无法读取编辑记录。请重试。" in texts["viewer.js"] and "无法读取裁剪。请重试。" in texts["viewer.js"]
    assert "无法读取这张照片。请重试。" in texts["stage.js"]
    # 3.10 第 2 项：导出面板不再提供精确，固定用默认的烘焙表
    assert "const precise = false" in texts["export.js"]


def test_recipe_and_adjust_tags_follow_3_10():
    """3.10 第 3 项：旋钮不打「相机」，写不进旧版相机配方文件的打「仅电脑」；调整标题旁不打标签。"""
    recipes = (ASSETS / "recipes.js").read_text(encoding="utf-8")
    adjust = (ASSETS / "adjust.js").read_text(encoding="utf-8")
    assert "'相机'" not in recipes and "相机 ±5" not in recipes
    assert "'仅电脑'" in recipes
    assert "仅电脑" not in adjust.split("export function adjustPanel", 1)[1]


def test_section_3_11_lut_wording():
    """COPY.md 3.11：LUT 文件过大时第二句写原因；相似度旁是「电脑预测」标签，不再显示后端的说明长句。"""
    lut = (ASSETS / "lut.js").read_text(encoding="utf-8")
    assert "无法读取 LUT。文件过大。" in lut
    assert "'电脑预测'" in lut
    assert "res.caution" not in lut


def test_section_3_12_wording():
    """COPY.md 3.12：在访达中显示；双击重置；关闭；导入完成；校准数据文件夹没有占位文字。"""
    export = (ASSETS / "export.js").read_text(encoding="utf-8")
    assert "'在访达中显示'" in export and "/reveal`" in export
    assert "'双击重置'" in (ASSETS / "controls.js").read_text(encoding="utf-8")
    assert "title: '关闭'" in (ASSETS / "import.js").read_text(encoding="utf-8")
    assert "'导入完成'" in (ASSETS / "import_summary.js").read_text(encoding="utf-8")
    assert "/完整路径/到/" not in (ASSETS / "camdata.js").read_text(encoding="utf-8")


def _function_body(source: str, header: str) -> str:
    """取出一个顶层函数的正文：从函数头到下一个列首的 `}`。"""
    start = source.index(header)
    return source[start : source.index("\n}\n", start)]


def test_each_panel_reset_touches_only_its_panel():
    """各面板的「重置」只清自己那一面板。
    「复原」的顺序和目标（先清编辑、再把裁剪写回整张 0°，切图后不写别的照片）由 test_edit_route.py 真正执行来测。"""
    correct = (ASSETS / "correct.js").read_text(encoding="utf-8")
    adjust = (ASSETS / "adjust.js").read_text(encoding="utf-8")
    recipes = (ASSETS / "recipes.js").read_text(encoding="utf-8")

    # 调整的重置：只清调整和曲线
    adj_reset = _function_body(adjust, "function resetButton(view)")
    for word in ("recipe", "geometry", "crop", "frame", "stamp"):
        assert word not in adj_reset, word
    # 配方的重置：只清旋钮，不碰调整、裁剪和边框
    rec_reset = _function_body(recipes, "function resetButton(view, usable)")
    for word in ("adj", "geometry", "crop", "frame", "stamp", "curve"):
        assert word not in rec_reset, word
    # 裁剪的重置：只清几何和裁剪
    crop_reset = _function_body(correct, "export async function resetCorrections(view)")
    for word in ("adj", "recipe", "frame", "stamp", "curve"):
        assert word not in crop_reset, word

    # 下一帧画拉直刻度时，照片可能已经离开：先看还在不在（否则会在旧的 view 上报错）
    assert "box.isConnected && view.ed" in correct


def test_pixel_size_text_is_counted_on_the_original_frame():
    """裁切的「W × H 像素」按原图（sidecar 的 frame 宽高）算，不按预览图。"""
    crop = (ASSETS / "crop.js").read_text(encoding="utf-8")
    body = _function_body(crop, "export function pixelsText(view)")
    assert "fullCanvas(view)" in body
    # 全尺寸画布：接口给的 frame 宽高。它已经是旋转、透视之后的画布，不能再按 rotate90 转一次（点比例、收框也用它）
    full = _function_body(crop, "export function fullCanvas(view)")
    assert "view.crop.frame" in full and "canvasAfterRotation" not in full


def test_frame_cards_use_two_columns_and_colours_are_swatches_only():
    """边框页：卡片两列、随面板收缩；颜色只用色块，颜色名放在提示里（不再有文字按钮）。"""
    frame = (ASSETS / "frame_panel.js").read_text(encoding="utf-8")
    css = (ASSETS / "app.css").read_text(encoding="utf-8")
    assert "swatch(COLOR_HEX[c]" in frame
    assert "[c, COLOR_NAMES[c]]" not in frame
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in css
    assert "repeat(3, 1fr)" not in css


def test_crop_panel_guides_dropdown_size_in_corner_ruler_row_and_sticky_footer():
    """裁切页：参考线是下拉框（COPY 3.1 的顺序）；尺寸在画面角落；拉直是左标签右数值；底部按钮贴底。"""
    crop = (ASSETS / "crop.js").read_text(encoding="utf-8")
    correct = (ASSETS / "correct.js").read_text(encoding="utf-8")
    stage = (ASSETS / "stage.js").read_text(encoding="utf-8")
    viewer = (ASSETS / "viewer.js").read_text(encoding="utf-8")
    css = (ASSETS / "app.css").read_text(encoding="utf-8")
    labels = re.findall(r"\['(?:off|thirds|grid|golden|spiral|diagonal)', '([^']+)'\]", crop)
    assert labels == ["关", "三等分", "网格", "黄金比例", "黄金螺旋", "对角线"]
    assert "'参考线'" in crop and "▾" in crop
    assert "class: 'small pixels'" not in crop
    assert "'crop-size'" in stage and "pixelsText(view)" in stage
    assert "h('div', { class: 'row' }, h('div', { class: 'lab' }, h('span', {}, '拉直'), out)" in correct
    assert "sticky-foot" in viewer and "position: sticky" in css


def test_import_banner_and_photo_bar_wording():
    """导入完成的提示条只留结果（不写「请先推出存储卡」）；照片条的日期在前、时间在后。"""
    import_js = (ASSETS / "import.js").read_text(encoding="utf-8")
    viewer = (ASSETS / "viewer.js").read_text(encoding="utf-8")
    stage = (ASSETS / "stage.js").read_text(encoding="utf-8")
    assert "请先推出存储卡" not in import_js
    assert "fmtDate(p.date)} ${fmtClock(p.captured_at)}" in viewer
    # 整张画面不做淡入（淡入期间会像蒙了一层灰）
    assert "fade-in" not in stage


def test_reveal_posts_to_the_export_id_with_an_empty_body():
    """在访达中显示：POST /api/exports/{export_id}/reveal，请求体为空，不带路径（后端只打开这次导出的第一张）。"""
    export = (ASSETS / "export.js").read_text(encoding="utf-8")
    body = _function_body(export, "function revealButton(exportId)")
    assert "`/api/exports/${exportId}/reveal`" in body
    assert "{ method: 'POST' }" in body
    assert "path" not in body
    # 单张导出用响应里的 export_id，批量导出用任务的 id
    assert "revealButton(r.export_id)" in export and "revealButton(job.id)" in export


def test_bottom_buttons_are_sticky_in_frame_adjust_and_recipe_panels():
    """边框、调整、配方的底部按钮固定在面板底边（和裁切页一样用 sticky-foot）；配方的顺序按 COPY 3.3。"""
    frame = (ASSETS / "frame_panel.js").read_text(encoding="utf-8")
    adjust = (ASSETS / "adjust.js").read_text(encoding="utf-8")
    recipes = (ASSETS / "recipes.js").read_text(encoding="utf-8")
    for text in (frame, adjust, recipes):
        assert "class: 'sticky-foot'" in text
        assert "class: 'foot'" not in text
    # 配方：固定行是直接挂在面板下面的（不能放在会滚动的 rest 里面，否则固定不住）
    assert "nodes.push(h('div', { class: 'sticky-foot' }" in recipes
    # U8：点了没反应的「存储为我的配方」「写入存储卡」删掉了，固定行里只有 [重置]（靠右）
    assert "'存储为我的配方'" not in recipes and "'写入存储卡'" not in recipes
    assert "nodes.push(h('div', { class: 'sticky-foot' }, reset));" in recipes


def test_original_compare_uses_the_render_endpoint_and_falls_back_to_the_old_preview():
    """V2-05 / PRD 第 11 节第 3 条：按住空格看原图用 render?stage=original&size=preview；接口不存在时退回原来的 preview_url。"""
    preview = (ASSETS / "preview.js").read_text(encoding="utf-8")
    stage = (ASSETS / "stage.js").read_text(encoding="utf-8")
    assert "render?stage=original&size=preview" in preview
    assert "view.photo.preview_url" in _function_body(preview, "export function originalFallbackUrl(view)")
    assert "originalFallbackUrl" in stage and "img.dataset.fallback !== '1'" in stage


def test_restore_writes_explicit_off_values_instead_of_null():
    """V2-08 / PRD 第 11 节第 6 条：复原写 frame.style=none、stamp.enabled=false，不写 null（null 会被当成继承默认值）。"""
    viewer = (ASSETS / "viewer.js").read_text(encoding="utf-8")
    body = _function_body(viewer, "async function resetAll()")
    assert "frame: { style: 'none' }" in body and "stamp: { enabled: false }" in body
    assert "frame: null" not in body and "stamp: null" not in body


def test_correction_releases_request_preview_size_not_full():
    """V2-15：松手后的纠正预览用 preview 尺寸，不用 full。"""
    correct = (ASSETS / "correct.js").read_text(encoding="utf-8")
    assert "full: true" not in correct
    for header in ("export async function rotate90(view)", "export async function flipHorizontal(view)"):
        assert "full: true" not in _function_body(correct, header)


def test_frame_thumbnails_show_the_whole_picture():
    """V2-16：边框样式缩略图完整显示成片（contain），覆盖图库的 cover，底部的标志和参数不被裁掉。"""
    css = (ASSETS / "app.css").read_text(encoding="utf-8")
    assert ".fcard .thumb img, .fcard .thumb canvas { width: 100%; height: 100%; object-fit: contain;" in css
    assert ".fcard .thumb { aspect-ratio: 4 / 3;" in css


def test_errors_keep_the_main_sentence_and_fold_the_detail():
    """COPY 3.13：错误主句照常显示；后端给了 detail 时，下面是默认收起的「详细信息」。所有错误提示都走 toastError。"""
    dom = (ASSETS / "dom.js").read_text(encoding="utf-8")
    body = _function_body(dom, "export function errorBody(err, fallback)")
    assert "err.data.detail" in body and "'详细信息'" in body and "h('details'" in body
    for path in ASSETS.glob("*.js"):
        text = path.read_text(encoding="utf-8")
        # 错误提示一律走 toastError（info 的提示不算）
        assert "toast(e.message, 'error')" not in text and "toast(e.message ||" not in text, path.name


def test_export_always_asks_the_server_to_choose_the_source():
    """R2-01：界面导出一律 source=auto，由后端按照片有没有选配方选底图（单张和批量都一样）。"""
    export = (ASSETS / "export.js").read_text(encoding="utf-8")
    assert "source: 'auto'" in export
    assert "sourceFor" not in export and "'recipe'" not in export


def test_render_labels_are_translated_and_machine_values_never_show():
    """R2-05：X-GXR-Label 是机器值，界面换成中文；没校准（X-GXR-Calibrated=0）的电脑预测再加「未校准」；原图不加小标签。"""
    preview = (ASSETS / "preview.js").read_text(encoding="utf-8")
    body = _function_body(preview, "export function stageLabels(view)")
    assert "'X-GXR-Calibrated'" in preview
    assert "label === 'original'" in body and "label === 'prediction'" in body and "label === 'approx'" in body
    assert "LABEL_WORDS[label]" in body


def test_export_errors_keep_the_backend_sentence_and_each_reason():
    """R2-06：导出出错时原样显示后端的主句；批量每一项的原因都留着；有 detail 时进详细信息。"""
    export = (ASSETS / "export.js").read_text(encoding="utf-8")
    assert "errorBody(e, '导出失败。请重试。')" in export
    assert "errorBody({ message: name ? `${name}：${r.message}` : r.message" in export
    assert "'导出失败。请重试。${names" not in export


def test_undo_remembers_whether_the_decoration_was_inherited():
    """R2-09：撤销回到原来的「继承默认」或「明确设置」；继承的写 null，不把默认值固化成照片设置。"""
    preview = (ASSETS / "preview.js").read_text(encoding="utf-8")
    # 撤销和重做共用 restoreSnapshot（重做 ⇧⌘Z 也要按原来的继承关系写回去）
    assert "return restoreSnapshot(view, s);" in _function_body(preview, "export function undoChange(view)")
    assert "return restoreSnapshot(view, s);" in _function_body(preview, "export function redoChange(view)")
    undo = _function_body(preview, "function restoreSnapshot(view, s)")
    # 这一步改过装饰才按快照还原（tf / ts 是快照或现在的样子）
    assert "frameOwn ? tf.frame : null" in undo and "stampOwn ? ts.stamp : null" in undo
    entries = (ASSETS / "undo_entries.js").read_text(encoding="utf-8")
    assert "frameFrom: ed.frameFrom" in entries and "stampFrom: ed.stampFrom" in entries
    assert "ed.frameFrom = 'photo'" in (ASSETS / "frame_panel.js").read_text(encoding="utf-8")
    assert "ed.stampFrom = 'photo'" in (ASSETS / "stamp_panel.js").read_text(encoding="utf-8")
    assert "ed.frameFrom = 'photo'" in (ASSETS / "viewer.js").read_text(encoding="utf-8")


def test_export_file_names_carry_no_labels():
    """导出文件名统一为「原名_gxr.jpg」：前端不拼「_电脑预测」「_近似」之类的后缀（COPY.md 3.9）。"""
    for path in ASSETS.glob("*.js"):
        text = path.read_text(encoding="utf-8")
        assert "_电脑预测" not in text and "_近似" not in text, path.name


def test_segmented_choices_and_zoom_are_not_squeezed_into_ellipses():
    """B3：分段按钮按字数分宽度（「日期和文字」不被截断）；视图工具栏的「适合 / 100%」不被挤成「10…」。"""
    css = (ASSETS / "app.css").read_text(encoding="utf-8")
    seg_button = css[css.index(".seg button {") : css.index("}", css.index(".seg button {"))]
    assert "flex: 1 1 auto" in seg_button and "flex: 1 1 0" not in seg_button
    assert ".viewbar .seg button { min-width: 44px; flex: none; }" in css
    assert ".viewbar .seg { flex: none; }" in css


def test_frame_preset_cards_load_as_skeletons_not_ellipses():
    """边框卡片还在渲染时是骨架底色（不放「…」），出来后去掉骨架；渲染失败写「没有预览」。"""
    js = (ASSETS / "frame_panel.js").read_text(encoding="utf-8")
    cards = (ASSETS / "finish_cards.js").read_text(encoding="utf-8")
    assert "h('div', { class: 'thumb skel', 'aria-busy': 'true' })" in js
    assert "'…'" not in js and "'…'" not in cards and "'没有预览'" in cards
    # 画出来和失败两条路都去掉骨架和 aria-busy
    assert cards.count("classList.remove('skel')") == 2 and cards.count("removeAttribute('aria-busy')") == 2


def test_export_camdata_note_does_not_promise_recipe_photos_export():
    """AA02/AB01：没导入相机配方文件时，后端保留配方但出不了配方效果。

    RAW+JPG 退回相机 JPG（serve.color_image 捕获 RenderUnavailable），只有 RAW 又套了配方的照片返回 409。
    说明不能说「都能导出」，也不能说「套了配方就要先导入」。
    """
    js = (ASSETS / "export.js").read_text(encoding="utf-8")
    assert "不导入也可以导出" not in js
    assert "套用了配方的照片要先导入相机配方文件" not in js
    assert "导出不会带上相机配方效果" in js
    assert "有相机 JPG 的照片导出相机 JPG" in js
    assert "只有 RAW、又套用了配方的照片可能导不出" in js
