"""Regressions for Sol's hdr r1 review (`65d277b`): P2-02 empty planes, P2-03 non-finite
reports, P2-04 host `log2` inputs, P2-05 Unicode numbers, P3-06 full case folding.

The original counterexamples come from
`round-6/sol-review-mobile-hdr-r1/evidence/` and are written out here literally.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import unicodedata
from decimal import Decimal, localcontext
from pathlib import Path

import numpy as np
import pytest
from scipy import ndimage

import hdr_support as hs
from gxr_studio.hdr import capture
from gxr_studio.hdr import gain as g
from support import hdr_parity as hp
from support.core_cli import require_cli
from support.wasm_check import run_area

NON_FINITE = ("value_error", "结果含 NaN 或无穷大")
TABLES = Path(__file__).resolve().parents[1] / "core" / "crates" / "gxr-hdr" / "src" / "unicode_tables.rs"


@pytest.fixture(autouse=True)
def _cli() -> None:
    require_cli()


# ---------------------------------------------------------------- P2-02 empty planes


@pytest.mark.parametrize("shape", [(0, 1), (1, 0), (0, 8), (8, 0)])
@pytest.mark.parametrize("dtype", [np.float32, np.float64])
def test_empty_planes_come_back_empty(tmp_path: Path, shape: tuple[int, int], dtype: type) -> None:
    a = np.empty(shape, dtype)
    src = hp.save(tmp_path / "a.npy", a)
    cases = [
        ("uniform", ["--size", "3"], ndimage.uniform_filter(a, 3, mode="reflect")),
        ("uniform", ["--size", "16"], ndimage.uniform_filter(a, 16, mode="reflect")),
        ("gaussian", ["--sigma", "0.6"], ndimage.gaussian_filter(a, 0.6)),
        ("gaussian", ["--sigma", "8"], ndimage.gaussian_filter(a, 8)),
        ("minimum", ["--size", "3"], ndimage.minimum_filter(a, 3)),
    ]
    for op, extra, python in cases:
        out = tmp_path / f"{op}.npy"
        hp.ok(["hdr-filter", "--op", op, "--in", src, *extra, "--out", str(out)])
        rust = np.load(out)
        assert (rust.dtype, rust.shape) == (python.dtype, python.shape), f"{op} {extra}"
    with pytest.raises(OverflowError) as caught:
        ndimage.gaussian_filter(a, float("inf"))
    _, err = hp.call(["hdr-filter", "--op", "gaussian", "--in", src, "--sigma", "inf", "--out", str(tmp_path / "x.npy")])
    hp.same_error(caught.value, err)


# ---------------------------------------------------------------- P2-03 non-finite reports


def _registered() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    from gxr_studio.raw import open_dng

    jpeg, dng = hs.pair(1)
    raw = g.raw_luma(open_dng(dng))
    sdr = g.sdr_luma_grid(g.decode_rgb(jpeg), 4)
    al = g.align(sdr, raw.y)
    y, clip = g.register(raw, sdr.shape, al, sdr.shape)
    return sdr, y, clip


@pytest.mark.parametrize("fill", [np.nan, np.inf])
def test_non_finite_alignment_is_refused(tmp_path: Path, fill: float) -> None:
    plane = np.full((8, 9), fill, np.float32)
    # Python's own result on these inputs is not finite, so no JSON can carry it.
    assert math.isnan(g.align(plane, plane).correlation)
    p = hp.save(tmp_path / "p.npy", plane)
    _, err = hp.call(["hdr-align", "--sdr", p, "--raw", p])
    assert err is not None and (err.kind, err.message) == NON_FINITE


def test_non_finite_gain_meta_is_refused_and_raw_nan_still_works(tmp_path: Path) -> None:
    sdr, y, clip = _registered()
    bad = sdr.copy()
    bad[40, 70] = np.nan  # a NaN SDR pixel inside the gate makes gmax NaN in Python
    assert math.isnan(g.compute_gain(bad, y, clip, g.Params()).meta.gain_max)
    args = ["hdr-gain", "--sdr", hp.save(tmp_path / "s.npy", bad), "--raw", hp.save(tmp_path / "r.npy", y)]
    _, err = hp.call([*args, "--clip", hp.save(tmp_path / "c.npy", clip), "--out", str(tmp_path / "g.npy")])
    assert err is not None and (err.kind, err.message) == NON_FINITE
    # RAW NaN still marks non-photo pixels and the report stays finite.
    stamped = y.copy()
    stamped[10:20, 30:70] = np.nan
    assert hp.compare_gain(tmp_path, sdr, stamped, clip, None, "stamp").startswith("ok")


def test_non_finite_wasm_matches_native(tmp_path: Path) -> None:
    sdr, y, clip = _registered()
    folders = []
    for name, fill in (("nan", np.nan), ("inf", np.inf)):
        d = tmp_path / name
        d.mkdir()
        hp.save(d / "sdr.npy", np.full((8, 9), fill, np.float32))
        hp.save(d / "raw.npy", np.full((8, 9), fill, np.float32))
        folders.append(str(d))
    d = tmp_path / "nangain"
    d.mkdir()
    bad = sdr.copy()
    bad[40, 70] = np.nan
    hp.save(d / "sdr.npy", bad)
    hp.save(d / "reg.npy", y)
    hp.save(d / "clip.npy", clip)
    folders.append(str(d))
    proc = run_area("hdr", folders, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr


# ---------------------------------------------------------------- P2-04 host log2 inputs

# The seven inputs Sol found where the host float32 log2 is not correctly rounded
# (`log2-attribution-results.json`): input bits, host output bits.
LOG2_CASES = [
    (0x3F8014F2, 0x3A71AA13),
    (0x3E98EC00, 0xBFDF260F),
    (0x3F800100, 0x3838A982),
    (0x3E0CA40E, 0xC0374DE0),
    (0x3DE485EB, 0xC04A7BC3),
    (0x3F39A39B, 0xBEED62A8),
    (0x3F1A36C4, 0xBF3B30A1),
]


def _log2_oracle(x: np.float32) -> np.float32:
    with localcontext() as ctx:
        ctx.prec = 80
        d = Decimal(float(x))
        return hp._f32_of(d.ln() / Decimal(2).ln())


def test_host_log2_inputs_follow_the_correctly_rounded_value(tmp_path: Path) -> None:
    """PORT-WAVE-1 platform-libm rule, one assertion set per input."""
    xs = np.array([b for b, _ in LOG2_CASES], np.uint32).view(np.float32)
    hp.ok(["hdr-log2", "--in", hp.save(tmp_path / "x.npy", xs), "--out", str(tmp_path / "y.npy")])
    rust = np.load(tmp_path / "y.npy")
    # 1. The inputs reach both sides bit for bit.
    assert np.load(tmp_path / "x.npy").view(np.uint32).tolist() == [b for b, _ in LOG2_CASES]
    for i, (bits, host_bits) in enumerate(LOG2_CASES):
        want = _log2_oracle(xs[i])
        # 2. Rust equals the correctly rounded float32.
        assert int(rust[i].view(np.uint32)) == int(want.view(np.uint32)), f"0x{bits:08x}"
        if sys.platform == "darwin":
            # 3. The host's log2 differs from it (and is the value Sol recorded).
            host = np.log2(xs[i : i + 1])[0]
            assert int(host.view(np.uint32)) == host_bits != int(want.view(np.uint32)), f"0x{bits:08x}"


def test_host_log2_inputs_keep_gain_codes_and_meta_exact(tmp_path: Path) -> None:
    """4. Downstream: planes holding these inputs give exact uint8 codes and metadata."""
    sdr, y, clip = _registered()
    xs = np.array([b for b, _ in LOG2_CASES], np.uint32).view(np.float32)
    raw_in, sdr_in = y.copy(), sdr.copy()
    for i, x in enumerate(xs):
        raw_in[5 + 3 * i, 10 : 10 + 40] = x  # x = log2(max(raw, 1e-6)) sees x itself
        sdr_in[60 + 3 * i, 20 : 20 + 40] = x  # y = log2(max(sdr, 1e-5)) sees x itself
    for tag, s_plane, r_plane in (("raw", sdr, raw_in), ("sdr", sdr_in, y), ("both", sdr_in, raw_in)):
        assert hp.compare_gain(tmp_path, s_plane, r_plane, clip, None, f"log2-{tag}").startswith("ok")


# ---------------------------------------------------------------- P2-05 / P3-06 capture text

# Sol's four capture counterexamples (`independent-capture.json`), then extra cases.
CAPTURE_CASES = [
    ({272: "ǰ"}, {272: "ǰ"}),
    ({33434: "١.٢"}, {33434: 1.2}),
    ({42036: "ΐ"}, {42036: "ΐ"}),
    ({33434: "١.٢"}, {33434: 2.0}),
    ({33437: "  2.8　", 34855: "２００", 37386: "\x1c50\x1f"}, {33437: 2.8, 34855: 200, 37386: 50.0}),
    ({33437: "١_٢", 34855: "٣e٢", 33434: "1_0.0_5"}, {33437: 12.0, 34855: 300.0, 33434: 10.05}),
    ({33437: "1.2.3", 34855: "Ⅻ", 33434: "1 2", 37386: "_1"}, {33437: 1.0, 34855: 12, 33434: 1.0, 37386: 1.0}),
    ({33434: "-١", 33437: "+inf", 34855: "nan", 37386: "๓.๕"}, {33434: 1.0, 33437: 2.0, 34855: 3.0, 37386: 3.5}),
    ({33434: "\x7f1", 33437: "1\x0b", 34855: "\x85 3", 37386: "\xa05"}, {33434: 1.0, 33437: 1.0, 34855: 3.0, 37386: 5.0}),
    ({271: "STRASSE ẞ ß", 272: "ΣΑΣ ﬃ", 42036: "İ ǅ Ⅷ ㎒"}, {271: "strasse ss ss", 272: "σασ ffi", 42036: "i̇ ǆ ⅷ ㎒"}),
    ({42016: "ＡＢＣ​", 50708: "Ꭰ ꭰ 𐐀"}, {42016: "ａｂｃ​", 50708: "Ꭰ Ꭰ 𐐨"}),
]


@pytest.mark.parametrize("case", range(len(CAPTURE_CASES)))
def test_capture_unicode_numbers_and_case_folding(tmp_path: Path, case: int) -> None:
    raw_tags, jpg_tags = CAPTURE_CASES[case]
    (tmp_path / "r.json").write_text(hp.tag_json(raw_tags))
    (tmp_path / "j.json").write_text(hp.tag_json(jpg_tags))
    got = hp.ok(["hdr-capture", "--raw", str(tmp_path / "r.json"), "--jpeg", str(tmp_path / "j.json")])
    raw_cap, jpg_cap = capture._capture(raw_tags), capture._capture(jpg_tags)
    expect = {"raw": raw_cap, "jpeg": jpg_cap, "differs": capture.differs(raw_cap, jpg_cap)}
    assert json.loads(json.dumps(expect)) == got
    assert list(got["raw"]) == list(raw_cap) and list(got["jpeg"]) == list(jpg_cap)


def _table(name: str) -> list[int]:
    text = TABLES.read_text(encoding="utf-8")
    m = re.search(rf"pub const {name}: \[u32; (\d+)\] = \[(.*?)\];", text, re.S)
    assert m, name
    values = [int(v, 16) for v in re.findall(r"0x([0-9a-f]+)", m.group(2))]
    assert len(values) == int(m.group(1))
    return values


def test_unicode_tables_match_the_reference_python() -> None:
    """The committed tables equal a fresh enumeration through this interpreter."""
    space, zeros, k1, v1, kn, vn = [], [], [], [], [], []
    for cp in range(sys.maxunicode + 1):
        if 0xD800 <= cp <= 0xDFFF:
            continue
        c = chr(cp)
        if c.isspace():
            space.append(cp)
        if unicodedata.decimal(c, -1) == 0:
            zeros.append(cp)
        f = c.casefold()
        if f != c:
            if len(f) == 1:
                k1.append(cp)
                v1.append(ord(f))
            else:
                kn.append(cp)
                vn.extend([ord(x) for x in f] + [0] * (3 - len(f)))
    assert _table("PY_SPACE") == space
    assert _table("DECIMAL_ZERO") == zeros
    assert (_table("FOLD1_KEY"), _table("FOLD1_VAL")) == (k1, v1)
    assert (_table("FOLDN_KEY"), _table("FOLDN_VAL")) == (kn, vn)


# ---------------------------------------------------------------- P2-10 allocation rules (Sol r3)

ARRAY_TOO_BIG = "array is too big; `arr.size * arr.dtype.itemsize` is larger than the maximum possible size."


def _sol_r3_inputs(d: Path) -> None:
    """Sol's r3 inputs (`alloc_entrypoints.rs`): s[i] = 0.02 + 0.90*i/1023 on 32×32, RAW = 2s,
    clip all false; FFT: 13 complex values whose 26 float32 parts are i*0.125 - 1."""
    s = (np.float32(0.02) + np.float32(0.90) * np.arange(1024, dtype=np.float32) / np.float32(1023.0)).reshape(32, 32)
    hp.save(d / "r1-s.npy", s)
    hp.save(d / "r1-raw.npy", (np.float32(2.0) * s).astype(np.float32))
    hp.save(d / "r1-clip.npy", np.zeros(1024, bool))
    hp.save(d / "r1-clip-short.npy", np.zeros(1000, bool))
    hp.save(d / "r1-fft.npy", (np.arange(26, dtype=np.float32) * np.float32(0.125) - np.float32(1.0)).reshape(13, 2))


def test_rule1_entry_points_check_before_copying(tmp_path: Path) -> None:
    """Rule 1 at `hdr_align`, `hdr_gain` (dimensions and clip length), `hdr_fft` and
    `hdr_phase_plane`: right and same-size mismatched dimensions give the same success bytes
    or the same core error from native and WASM (cases in `wasm_cases/hdr.mjs`)."""
    d = tmp_path / "rule1"
    d.mkdir()
    _sol_r3_inputs(d)
    proc = run_area("hdr", [str(d)], tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # The CLI half of the same cases: the mismatches are core errors, not usage errors.
    for args in (
        ["hdr-align", "--sdr", str(d / "r1-s.npy"), "--raw", str(d / "r1-s.npy"), "--sdr-shape", "31", "32"],
        ["hdr-fft", "--in", str(d / "r1-fft.npy"), "--inverse", "0", "--in-shape", "1", "12", "--out", str(tmp_path / "f.npy")],
        ["hdr-gain", "--sdr", str(d / "r1-s.npy"), "--raw", str(d / "r1-raw.npy"), "--clip", str(d / "r1-clip-short.npy")]
        + ["--sdr-shape", "32", "32", "--raw-shape", "32", "32", "--out", str(tmp_path / "g.npy")],
    ):
        _, err = hp.call(args)
        assert err is not None and (err.kind, err.message) == ("bad_input", "数组长度和形状对不上"), args[0]


def test_rule2_allocations_fail_as_core_errors(tmp_path: Path) -> None:
    """Rule 2: every allocation above 8× the validated input bytes is refused while one entry
    point runs. Paths that size buffers from parameters (`register`'s output shape,
    `block_mean` / `sdr_luma_grid` padding, `fit_curve`'s edge range) must return the size
    error; `align` and `compute_gain` on Sol's inputs have no such allocation and succeed."""
    from support import hdr_alloc_rules as ar

    binary = ar.build(tmp_path)
    if binary is None:
        if os.environ.get("GXR_REQUIRE_CORE") == "1":
            pytest.fail("cargo is required to build the allocation-rule driver")
        pytest.skip("cargo is not on PATH")
    seen = {}
    for op in ar.RULE2_OPS + ar.BOUNDED_OPS:
        base = ar.run(binary, op, "count")
        assert base.returncode == 0 and base.stdout.startswith("ok above="), (op, base.stdout, base.stderr)
        above = int(base.stdout.split()[1].split("=")[1])
        if op in ar.BOUNDED_OPS:
            assert above == 0, f"{op}: {above} allocations above 8x the input"
        else:
            assert above > 0, f"{op}: expected a rule-2 allocation"
        # Refuse each rule-2 allocation in turn: every one must come back as the size error.
        for k in range(1, above + 1):
            got = ar.run(binary, op, str(k))
            assert got.returncode == 0, f"{op} allocation {k}: exit {got.returncode} {got.stderr.strip()}"
            assert got.stdout.strip().endswith(f"refused=1 value_error: {ARRAY_TOO_BIG}"), (op, k, got.stdout)
        seen[op] = above
    print("allocation rules (allocations above 8x the input, each refused once):", seen)


def test_cli_npy_parser_validates_before_copying(tmp_path: Path) -> None:
    """Sol r4 rule 1: the CLI's `.npy` parser checks the header's element and byte counts
    against the borrowed payload before copying it. Invalid headers make no payload-sized
    copy; a valid one is still accepted (and copied once)."""
    from support import hdr_cli_npy_order as no

    binary = no.build(tmp_path)
    if binary is None:
        if os.environ.get("GXR_REQUIRE_CORE") == "1":
            pytest.fail("cargo is required to build the CLI order driver")
        pytest.skip("cargo is not on PATH")
    for case, accepted in no.CASES.items():
        got = no.run(binary, case)
        assert got.returncode == 0, (case, got.stderr)
        copies = 1 if accepted else 0
        assert got.stdout.strip() == f"{case} accepted={str(accepted).lower()} payload_copies={copies}", got.stdout
