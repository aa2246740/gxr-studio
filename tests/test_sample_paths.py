"""真实样本、研究代码和项目文件夹只有一种找法（support/samples.py）。不需要样本，随时能跑。"""

from __future__ import annotations

import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest

from support import sample_guard, samples

TESTS = Path(__file__).resolve().parent


def test_parent_defaults_to_the_folder_above_the_checkout(monkeypatch):
    monkeypatch.delenv("GXR_STUDIO_TEST_PARENT", raising=False)
    assert samples.sample_parent() == TESTS.parent.parent
    assert samples.captures() == TESTS.parent.parent / "captures"
    assert samples.a12_50_samples() == TESTS.parent.parent / "captures/a12-50/2026-10-19/stage2c-first-run"


def test_parent_follows_the_environment_variable(monkeypatch, tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    monkeypatch.setenv("GXR_STUDIO_TEST_PARENT", str(tmp_path / "link"))
    assert samples.sample_parent() == (tmp_path / "real").resolve()
    assert samples.a12_50_samples() == (tmp_path / "real").resolve() / "captures/a12-50/2026-10-19/stage2c-first-run"


def test_missing_skips_with_the_same_text_by_default(monkeypatch):
    monkeypatch.delenv("GXR_REQUIRE_SAMPLES", raising=False)
    with pytest.raises(pytest.skip.Exception) as caught:
        samples.missing("captures/a12-50 样本不在本机")
    assert caught.value.msg == "captures/a12-50 样本不在本机"


@pytest.mark.parametrize("value", ["0", "", "true"])
def test_only_the_value_1_makes_samples_required(monkeypatch, value):
    monkeypatch.setenv("GXR_REQUIRE_SAMPLES", value)
    with pytest.raises(pytest.skip.Exception):
        samples.missing("x")


def test_missing_fails_under_require_samples(monkeypatch, tmp_path):
    monkeypatch.setenv("GXR_REQUIRE_SAMPLES", "1")
    monkeypatch.setenv("GXR_STUDIO_TEST_PARENT", str(tmp_path))
    with pytest.raises(pytest.fail.Exception) as caught:
        samples.missing("captures/a12-50 样本不在本机")
    assert caught.value.msg.startswith("captures/a12-50 样本不在本机")
    assert str(tmp_path.resolve()) in caught.value.msg


def test_needs_is_the_old_skipif_by_default(monkeypatch):
    monkeypatch.delenv("GXR_REQUIRE_SAMPLES", raising=False)
    absent = samples.needs(False, "研究代码不在本机")
    assert absent.mark.name == "skipif" and absent.mark.args == (True,) and absent.mark.kwargs == {"reason": "研究代码不在本机"}
    present = samples.needs(True, "研究代码不在本机")
    assert present.mark.name == "skipif" and present.mark.args == (False,)


def test_needs_fails_at_setup_under_require_samples(monkeypatch):
    monkeypatch.setenv("GXR_REQUIRE_SAMPLES", "1")
    assert samples.needs(True, "r").mark.args == (False,), "样本在的时候照常跑"
    mark = samples.needs(False, "captures/street-g137-20261031 样本不在本机").mark
    assert mark.name == samples.REQUIRED_MARK
    item = SimpleNamespace(get_closest_marker=lambda name: mark if name == samples.REQUIRED_MARK else None)
    with pytest.raises(pytest.fail.Exception) as caught:
        samples.fail_if_marked(item)  # type: ignore[arg-type]
    assert caught.value.msg.startswith("captures/street-g137-20261031 样本不在本机")
    unmarked = SimpleNamespace(get_closest_marker=lambda name: None)
    samples.fail_if_marked(unmarked)  # type: ignore[arg-type]


def test_real_params_keep_the_empty_set_by_default_and_fail_it_under_require_samples(monkeypatch):
    monkeypatch.delenv("GXR_REQUIRE_SAMPLES", raising=False)
    assert samples.real_params([], "no DNGs under x") == []
    assert samples.real_params(["a"], "no DNGs under x") == ["a"]
    monkeypatch.setenv("GXR_REQUIRE_SAMPLES", "1")
    assert samples.real_params(["a"], "no DNGs under x") == ["a"]
    (param,) = samples.real_params([], "no DNGs under x")
    assert param.id == "NOTSET" and param.values == (None,)
    (mark,) = param.marks
    assert mark.name == samples.REQUIRED_MARK and mark.kwargs == {"reason": "no DNGs under x"}


def test_need_sample_only_acts_under_require_samples(monkeypatch):
    monkeypatch.delenv("GXR_REQUIRE_SAMPLES", raising=False)
    samples.need_sample(False, "no DNGs in x")  # 默认什么都不做，调用方自己跳过
    monkeypatch.setenv("GXR_REQUIRE_SAMPLES", "1")
    samples.need_sample(True, "no DNGs in x")
    with pytest.raises(pytest.fail.Exception) as caught:
        samples.need_sample(False, "no DNGs in x")
    assert caught.value.msg.startswith("no DNGs in x")


# Sol r1 复审的 8 个用例（空的上层目录 + GXR_REQUIRE_SAMPLES=1 时还在跳过），加上同一轮顺手改的样本/研究源码分支。
# 这些用例在空目录下要么在样本检查前不需要 DAT，要么样本检查就是第一步。
STRICT_CASES = {
    "tests/test_g181.py::test_field_raw_and_jpg_tags_agree_and_unknown_positions_remain_unnamed[181]": "g181 成对实拍文件不在本机",
    "tests/test_g181.py::test_field_raw_and_jpg_tags_agree_and_unknown_positions_remain_unnamed[182]": "g181 成对实拍文件不在本机",
    "tests/test_mobile_shell_browser.py::test_mobile_shell_autotest": "no DNGs in ",
    "tests/test_mobile_ui_browser.py::test_mobile_ui_pick_grid_edit_and_bad_file": "need a DNG+JPG pair in ",
    "tests/test_core_parity_lut.py::test_scene_xyz_real_dngs[NOTSET]": "no DNGs under ",
    "tests/test_core_parity_raw.py::test_real_describe_summary_mosaic_and_half[NOTSET]": "no DNGs under ",
    "tests/test_engine_knobs.py::test_menu_requests_equal_the_research_menu": "研究代码不在本机",
    "tests/test_engine_knobs.py::test_copies_equal_the_research_sources": "研究目录不在本机",
    "tests/test_core_parity_raw.py::test_real_demosaic_strip_and_neutral_match_python[NOTSET]": "no DNGs under ",
    "tests/test_core_parity_hdr.py::test_real_pair_flow[NOTSET]": "no RAW+JPG pairs under ",
    "tests/test_engine_knobs.py::test_ast_segments_equal_the_originals": "研究目录不在本机",
    "tests/test_datcodec.py::test_gxui_matches_the_research_ui_data_on_g127": "研究代码不在本机",
}


def _run_empty_parent(tmp_path: Path, require: bool) -> dict[str, tuple[str, str]]:
    """在空的上层目录下用子进程跑 STRICT_CASES，返回 node id → (结果, 文字)。"""
    parent = tmp_path / "empty-parent"
    parent.mkdir()
    env = {k: v for k, v in os.environ.items() if k not in {"GXR_REQUIRE_CORE", "GXR_REQUIRE_SAMPLES", "GXR_REQUIRE_GPU_TEST"}}
    env["GXR_STUDIO_TEST_PARENT"] = str(parent)
    if require:
        env["GXR_REQUIRE_SAMPLES"] = "1"
    report = tmp_path / "report.xml"
    cmd = [sys.executable, "-I", "-B", "-m", "pytest", "-q", "-p", "no:randomly", f"--basetemp={tmp_path / 'base'}", f"--junitxml={report}"]
    proc = subprocess.run([*cmd, *STRICT_CASES], cwd=TESTS.parent, env=env, capture_output=True, text=True, timeout=300, check=False)
    assert report.is_file(), proc.stdout[-2000:] + proc.stderr[-2000:]
    out = {}
    for case in ET.parse(report).getroot().iter("testcase"):
        module = case.get("classname", "").removeprefix("tests.").replace(".", "/")
        node = f"tests/{module}.py::{case.get('name')}"
        for kind in ("failure", "error", "skipped"):
            element = case.find(kind)
            if element is not None:
                out[node] = (kind, element.get("message") or "")
                break
        else:
            out[node] = ("passed", "")
    return out


@pytest.mark.parametrize("require", [False, True], ids=["default", "require-samples"])
def test_empty_parent_skips_by_default_and_fails_under_require_samples(tmp_path, require):
    results = _run_empty_parent(tmp_path, require)
    assert set(results) == set(STRICT_CASES)
    for node, reason in STRICT_CASES.items():
        kind, message = results[node]
        if not require:
            assert kind == "skipped", (node, kind, message)
            if not node.endswith("[NOTSET]"):  # 空参数集保留 pytest 自己的 NOTSET 跳过文字
                assert reason in message, (node, message)
        else:
            assert kind in ("failure", "error"), (node, kind, message)
            assert reason in message and "GXR_REQUIRE_SAMPLES=1" in message, (node, message)


# Sol r1 的守卫反例（static-and-guard.json），外加几种同义写法。
GUARD_BLOCKED = {
    "old_resolve_style": ("test_probe.py", 'ROOT = Path(__file__).resolve().parents[2]\nSAMPLE = ROOT / "captures/a12-50"\n'),
    "old_unresolved_style": ("test_probe.py", 'ROOT = Path(__file__).parents[2]\nSAMPLE = ROOT / "captures/a12-50"\n'),
    "parent_chain_style": ("test_probe.py", 'ROOT = Path(__file__).resolve().parent.parent.parent\nSAMPLE = ROOT / "captures/a12-50"\n'),
    "repo_then_parent": ("test_probe.py", 'REPO = Path(__file__).resolve().parents[1]\nSAMPLE = REPO.parent / "captures"\n'),
    "mixed_parent_parents": ("test_probe.py", "ROOT = Path(__file__).parent.parents[1]\n"),
    "dirname_chain": ("test_probe.py", "ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n"),
    "support_above_repo": ("support/probe.py", "ROOT = Path(__file__).resolve().parents[3]\n"),
    "direct_environment_lookup": ("test_probe.py", 'ROOT = Path(os.environ.get("GXR_STUDIO_TEST_PARENT", "fallback"))\n'),
    "getenv_lookup": ("test_probe.py", 'ROOT = Path(os.getenv("GXR_STUDIO_TEST_PARENT", "fallback"))\n'),
    "subscript_lookup": ("test_probe.py", 'ROOT = Path(os.environ["GXR_STUDIO_TEST_PARENT"])\n'),
    "membership_lookup": ("test_probe.py", 'ok = "GXR_STUDIO_TEST_PARENT" in os.environ\n'),
    "default_samples_lookup": ("test_probe.py", "SAMPLE = sy.DEFAULT_SAMPLES\n"),
    "default_samples_import": ("test_probe.py", "from gxr_studio.devtools.synthetic_card import DEFAULT_SAMPLES as S\n"),
    "unrelated_samples_filename": ("fixtures/samples.py", 'ROOT = Path(os.environ.get("GXR_STUDIO_TEST_PARENT", "fallback"))\n'),
    "nested_samples_test_name": ("support/test_sample_paths.py", "ROOT = Path(__file__).parents[3]\n"),
    # 作用域和接收者改成精确判断之后仍要拦住的
    "inside_a_function": (
        "test_probe.py",
        'def test_x():\n    root = Path(__file__).resolve().parents[1]\n    return root.parent / "captures"\n',
    ),
    "module_name_read_in_a_function": ("test_probe.py", "REPO = Path(__file__).parents[1]\ndef f():\n    return REPO.parent\n"),
    "closure_reads_enclosing_function": (
        "test_probe.py",
        "def f():\n    repo = Path(__file__).parents[1]\n    def g():\n        return repo.parent\n    return g\n",
    ),
    "reassigned_from_a_path": ("test_probe.py", "ROOT = tmp_path\nROOT = Path(__file__).parents[1]\nX = ROOT.parent\n"),
    "walrus": ("test_probe.py", "if (repo := Path(__file__).parents[1]):\n    X = repo.parent\n"),
    "os_alias": ("test_probe.py", 'import os as o\nX = o.environ.get("GXR_STUDIO_TEST_PARENT")\n'),
    "environ_import": ("test_probe.py", 'from os import environ\nX = environ.get("GXR_STUDIO_TEST_PARENT")\n'),
    "environ_import_alias": ("test_probe.py", 'from os import environ as e\nX = e["GXR_STUDIO_TEST_PARENT"]\n'),
    "getenv_import_alias": ("test_probe.py", 'from os import getenv as g\nX = g("GXR_STUDIO_TEST_PARENT")\n'),
    "environ_assigned_to_a_name": ("test_probe.py", 'env = os.environ\nX = env.setdefault("GXR_STUDIO_TEST_PARENT", "x")\n'),
    "environ_pop": ("test_probe.py", 'X = os.environ.pop("GXR_STUDIO_TEST_PARENT", None)\n'),
    "os_imported_in_a_function": ("test_probe.py", 'def f():\n    import os\n    return os.getenv("GXR_STUDIO_TEST_PARENT")\n'),
}
GUARD_ALLOWED = {
    "legitimate_support_imports": (
        "support/probe.py",
        'sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\nsys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))\n',
    ),
    "repo_relative_web": ("test_probe.py", 'WEB = Path(__file__).resolve().parents[1] / "web"\n'),
    "comment_only": ("test_probe.py", "# Samples are explicit; do not use DEFAULT_SAMPLES.\nfrom support import samples\n"),
    "docstring_only": ("test_probe.py", '"""Set GXR_STUDIO_TEST_PARENT; never Path(__file__).parents[2] or DEFAULT_SAMPLES."""\n'),
    "setting_the_variable": (
        "test_probe.py",
        'env["GXR_STUDIO_TEST_PARENT"] = str(parent)\nmonkeypatch.setenv("GXR_STUDIO_TEST_PARENT", "x")\n',
    ),
    "exempt_helper": ("support/samples.py", 'ROOT = Path(os.environ.get("GXR_STUDIO_TEST_PARENT", "fallback"))\n'),
    # Sol r2（boundaries.json 的 guard_execution）：四条合法写法
    "reassigned_name": ("test_probe.py", "ROOT = Path(__file__).resolve().parents[1]\nROOT = tmp_path\nSAMPLE = ROOT.parent\n"),
    "local_scope_leak": (
        "test_probe.py",
        "def test_repository():\n"
        "    root = Path(__file__).resolve().parents[1]\n"
        '    assert (root / "web").is_dir()\n'
        "def test_temporary(tmp_path):\n"
        '    root = tmp_path / "child"\n'
        "    assert root.parent == tmp_path\n",
    ),
    "parameter_shadow": ("test_probe.py", "root = Path(__file__).resolve().parents[1]\ndef temporary_parent(root):\n    return root.parent\n"),
    "plain_dictionary_get": (
        "test_probe.py",
        'expected = {"GXR_STUDIO_TEST_PARENT": "x"}\nassert expected.get("GXR_STUDIO_TEST_PARENT") == "x"\n',
    ),
    # 同一类的几种变体
    "local_before_assignment": ("test_probe.py", "root = Path(__file__).parents[1]\ndef f(tmp_path):\n    x = root\n    root = tmp_path\n"),
    "class_body_not_seen_by_methods": (
        "test_probe.py",
        "class T:\n    ROOT = Path(__file__).parents[1]\n    def go(self, ROOT=None):\n        return ROOT.parent\n",
    ),
    "comprehension_variable": ("test_probe.py", "root = Path(__file__).parents[1]\nparents = [root.parent for root in tmp_dirs]\n"),
    "tuple_rebinding": ("test_probe.py", "root = Path(__file__).parents[1]\nroot, other = tmp_path, 1\nx = root.parent\n"),
    "for_rebinding": ("test_probe.py", "root = Path(__file__).parents[1]\nfor root in tmp_dirs:\n    x = root.parent\n"),
    "plain_dictionary_pop_and_subscript": (
        "test_probe.py",
        'env = {}\nenv.pop("GXR_STUDIO_TEST_PARENT", None)\nx = expected["GXR_STUDIO_TEST_PARENT"]\n',
    ),
    "os_shadowed": ("test_probe.py", 'def f(os):\n    return os.environ.get("GXR_STUDIO_TEST_PARENT")\n'),
}


def _guard(tmp_path: Path, rel: str, source: str) -> list[str]:
    tests = tmp_path / "repo" / "tests"
    (tests / rel).parent.mkdir(parents=True, exist_ok=True)
    (tests / rel).write_text(source, encoding="utf-8")
    return sample_guard.scan_tests(tests)


@pytest.mark.parametrize("case", sorted(GUARD_BLOCKED))
def test_guard_blocks(tmp_path, case):
    assert _guard(tmp_path, *GUARD_BLOCKED[case]) != []


@pytest.mark.parametrize("case", sorted(GUARD_ALLOWED))
def test_guard_allows(tmp_path, case):
    assert _guard(tmp_path, *GUARD_ALLOWED[case]) == []


def test_no_test_finds_samples_on_its_own():
    """回归：tests/ 里别再按 __file__、`DEFAULT_SAMPLES` 或自己读环境变量找样本（T3 工作树里会悄悄跳过）。"""
    assert sample_guard.scan_tests(TESTS) == []


def test_stable_sample_membership_depends_only_on_each_file(tmp_path):
    """parity-drift-1: adding captures elsewhere never moves a file in or out of a sample, and a
    smaller request is a subset of a larger one (the old spread by sorted index shifted)."""
    root = tmp_path / "captures"
    first = [root / f"a{i // 50:02d}" / f"R{i:07d}.DNG" for i in range(2000)]
    later = [root / "r6-field" / f"R{i:07d}.DNG" for i in range(500)] + [root / "a00" / f"Q{i:07d}.DNG" for i in range(500)]
    for limit in (2, 8, 20, 60):
        before = samples.stable_sample(first, root, limit)
        after = samples.stable_sample(first + later, root, limit)
        assert [p for p in after if p in set(first)] == before
        assert before == sorted(
            (p for p in first if samples.sample_key(p, root) < samples.sample_share(limit)), key=lambda p: samples.sample_key(p, root)
        )
    assert (
        set(samples.stable_sample(first, root, 8)) <= set(samples.stable_sample(first, root, 20)) <= set(samples.stable_sample(first, root, 60))
    )
    assert samples.sample_share(1) == samples.sample_share(samples.SAMPLE_FLOOR)
    # The key is the file's own relative path: the same layout under another parent gives the same keys.
    assert samples.sample_key(first[7], root) == samples.sample_key(tmp_path / "x" / first[7].relative_to(root), tmp_path / "x")


# sample-stable-2: each test-local scanner. Picks and the case derived from a file's own key stay
# put when five files arrive. The old index spread moves; this fails on 3a556bd for every scanner
# that still spreads that way. ``hdr_track._real_pairs_all`` does not spread, so that case stays green.
_SCANNERS = (
    "color",
    "raw",
    "hdr_track_all",
    "hdr_track_spread",
    "hdr_finish",
    "geometry",
    "export_meta",
    "resample",
    "raster",
)


def _case_of(path: Path, root: Path) -> int:
    """Same assignment the parity tests use: the file's own key, not its place in the list."""
    return int(samples.sample_key(path, root) * 1e9) % 6


def _bind_captures(monkeypatch: pytest.MonkeyPatch, root: Path, *modules: object) -> None:
    monkeypatch.setattr(samples, "captures", lambda: root)
    for module in modules:
        monkeypatch.setattr(module, "CAPTURES", root, raising=False)


def _populate(root: Path, count: int = 80) -> None:
    """DNGs in several folders. The first five have no JPG yet, so a pair scanner can gain them later."""
    for index in range(count):
        folder = root / f"u{index % 7:02d}" / f"s{index % 4}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"f{index:04d}.DNG").write_bytes(b"")
        if index >= 5:
            (folder / f"f{index:04d}.JPG").write_bytes(b"")


def _add_unrelated(root: Path, scanner: str) -> None:
    """Five files the scanner's own filter accepts. Pair scanners gain five JPG mates, not new DNGs."""
    if scanner in {"hdr_track_all", "hdr_track_spread", "hdr_finish"}:
        for index in range(5):
            folder = root / f"u{index % 7:02d}" / f"s{index % 4}"
            (folder / f"f{index:04d}.JPG").write_bytes(b"")
        return
    suffixes = {
        "color": (".DNG",) * 5,
        "raw": (".DNG",) * 5,
        "geometry": (".DNG",) * 5,
        "export_meta": (".DNG", ".DNG", ".JPG", ".JPG", ".JPG"),
        "resample": (".DNG", ".DNG", ".JPG", ".JPG", ".JPG"),
        "raster": (".JPG", ".JPG", ".DNG", ".JPG", ".DNG"),
    }[scanner]
    extra = root / "zz-unrelated"
    extra.mkdir()
    for index, suffix in enumerate(suffixes):
        (extra / f"n{index:04d}{suffix}").write_bytes(b"")


def _ids_of(scanner: str, root: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], ...]:
    if scanner == "color":
        import test_core_parity_color as mod

        _bind_captures(monkeypatch, root, mod)
        return ([path.relative_to(root).as_posix() for path in mod._real_dngs()],)
    if scanner == "raw":
        import test_core_parity_raw as mod

        _bind_captures(monkeypatch, root, mod)
        return ([path.relative_to(root).as_posix() for path in mod._real_dngs()],)
    if scanner in {"hdr_track_all", "hdr_track_spread"}:
        import test_core_parity_hdr_track as mod

        _bind_captures(monkeypatch, root, mod)
        pairs = mod._real_pairs_all() if scanner == "hdr_track_all" else mod._spread_pairs(20)
        return ([dng.relative_to(root).as_posix() for _jpg, dng in pairs],)
    if scanner == "hdr_finish":
        import test_core_parity_hdr_track_finish as mod

        _bind_captures(monkeypatch, root, mod)
        return ([dng.relative_to(root).as_posix() for _jpg, dng in mod._spread_pairs(16)],)
    if scanner == "geometry":
        import test_core_parity_geometry as mod

        _bind_captures(monkeypatch, root, mod)

        class _Opened:
            orientation = 1

            @staticmethod
            def crop_window() -> tuple[int, int, int, int]:
                return (0, 0, 6, 10)

        monkeypatch.setattr(mod, "open_dng", lambda _path: _Opened())
        return ([rel for rel, _width, _height in mod._real_dng_sizes()],)
    if scanner == "export_meta":
        import test_core_parity_export_meta as mod

        _bind_captures(monkeypatch, root, mod)
        dngs = [path.relative_to(root).as_posix() for path in mod._real_files((".DNG",))]
        jpgs = [path.relative_to(root).as_posix() for path in mod._real_files((".JPG", ".JPEG"))]
        return dngs, jpgs
    if scanner == "resample":
        import test_core_parity_resample as mod

        _bind_captures(monkeypatch, root, mod)
        jpgs, dngs = mod._collect_real_images(12)
        return (
            [path.relative_to(root).as_posix() for path in jpgs],
            [path.relative_to(root).as_posix() for path in dngs],
        )
    import test_core_parity_raster as mod

    _bind_captures(monkeypatch, root, mod)

    class _Pixels:
        shape = (4, 4, 3)

    monkeypatch.setattr(mod, "open_dng", lambda _path: object())
    monkeypatch.setattr(mod, "render_half", lambda _dng: _Pixels())
    monkeypatch.setattr(mod, "load_jpeg", lambda _path, max_edge=None: _Pixels())
    found = []
    for row in mod._real_capture_inputs(24):
        ident = row[0]
        if isinstance(ident, Path):
            found.append(ident.relative_to(root).as_posix())
        else:
            matches = [path for path in root.rglob("*") if path.is_file() and path.name == ident]
            assert len(matches) == 1
            found.append(matches[0].relative_to(root).as_posix())
    return (found,)


@pytest.mark.parametrize("scanner", _SCANNERS)
def test_local_scanner_picks_and_cases_stay_when_unrelated_files_arrive(scanner: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Adding captures must not drop a file a scanner already picked, nor change the case of its own key."""
    root = tmp_path / "captures"
    _populate(root)
    before = _ids_of(scanner, root, monkeypatch)
    cases = {rel: _case_of(root / rel, root) for group in before for rel in group}
    _add_unrelated(root, scanner)
    after = _ids_of(scanner, root, monkeypatch)
    assert len(before) == len(after)
    for index, (left, right) in enumerate(zip(before, after, strict=True)):
        assert left, f"{scanner} group {index} picked nothing"
        missing = [rel for rel in left if rel not in set(right)]
        assert not missing, f"{scanner} group {index} dropped {len(missing)} original picks"
        for rel in left:
            assert _case_of(root / rel, root) == cases[rel]
