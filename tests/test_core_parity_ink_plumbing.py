"""Parity: gxr-cli ink-ops & ink-reduce against Pillow 12.2.0 and NumPy 2.4.3 (Item 1)."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image

from support.core_cli import run_json, run_npy
from support.export_pipeline_cases import anon_ids, check, same_pixels


def _synth_img(w: int, h: int, mode: str, seed: int) -> Image.Image:
    rng = np.random.default_rng(seed)
    ch = {"L": 1, "RGB": 3, "RGBA": 4}[mode]
    if ch == 1:
        arr = rng.integers(0, 256, size=(h, w), dtype=np.uint8)
        return Image.fromarray(arr, mode="L")
    arr = rng.integers(0, 256, size=(h, w, ch), dtype=np.uint8)
    if mode == "RGBA" and w * h >= 4:
        flat = arr.reshape(-1, 4)
        flat[0, 3] = 0
        flat[1, 3] = 255
        if len(flat) > 2:
            flat[2, 3] = 128
    return Image.fromarray(arr, mode=mode)


def _cli_ops(
    canvas: Image.Image,
    ops: list[dict[str, Any]],
    tmp_path: Path,
    tag: str,
    logos: dict[str, Image.Image] | None = None,
) -> np.ndarray:
    in_npy = tmp_path / f"{tag}_in.npy"
    ops_path = tmp_path / f"{tag}_ops.json"
    out_npy = tmp_path / f"{tag}_out.npy"
    np.save(in_npy, np.asarray(canvas, dtype=np.uint8))
    ops_path.write_text(json.dumps(ops), encoding="utf-8")
    args = ["ink-ops", "--canvas", str(in_npy), "--mode", canvas.mode, "--ops", str(ops_path)]
    if logos:
        for lid, lim in logos.items():
            lp = tmp_path / f"{tag}_logo_{lid}.npy"
            np.save(lp, np.asarray(lim, dtype=np.uint8))
            args.extend(["--logo", f"{lid}={lp}"])
    return run_npy(args, out_npy)


@pytest.mark.parametrize(
    ("mode", "w", "h", "color"),
    [
        ("L", 1, 1, 0),
        ("L", 17, 13, 211),
        ("RGB", 1, 1, [12, 34, 56]),
        ("RGB", 23, 19, [255, 0, 128]),
        ("RGBA", 1, 1, [10, 20, 30, 0]),
        ("RGBA", 15, 11, [10, 20, 30, 180]),
    ],
    ids=anon_ids(6),
)
def test_new_fill(mode: str, w: int, h: int, color: Any, tmp_path: Path) -> None:
    seed_img = Image.new(mode, (1, 1), 0)
    pil = Image.new(mode, (w, h), tuple(color) if isinstance(color, list) else color)
    got = _cli_ops(seed_img, [{"op": "new", "mode": mode, "w": w, "h": h, "color": color}], tmp_path, "new")
    same_pixels(got, np.asarray(pil, dtype=np.uint8), "ink new fill")


@pytest.mark.parametrize(
    ("dst_mode", "src_mode", "mask_mode", "box"),
    [
        ("L", "L", None, [2, 3]),
        ("L", "L", "L", [-3, -2]),
        ("L", "L", "RGBA", [10, 8]),
        ("RGB", "RGB", None, [0, 0]),
        ("RGB", "RGB", "L", [-4, 5]),
        ("RGB", "RGBA", "RGBA", [7, -3]),
        ("RGB", "RGBA", "L", [3, 4]),
        ("RGB", "L", "L", [1, 2]),
        ("RGBA", "RGBA", None, [-2, -2]),
        ("RGBA", "RGBA", "L", [4, 6]),
        ("RGBA", "RGBA", "RGBA", [2, 1]),
        ("RGB", "RGB", "L", [50, 50]),
        ("RGB", "RGB", "L", [-20, -20]),
        ("RGB", "RGB", None, [-2, -3, 7, 4]),
    ],
    ids=anon_ids(14),
)
def test_paste_image(
    dst_mode: str,
    src_mode: str,
    mask_mode: str | None,
    box: list[int],
    tmp_path: Path,
) -> None:
    dst = _synth_img(19, 15, dst_mode, 11)
    src = _synth_img(9, 7, src_mode, 22)
    mask = _synth_img(9, 7, mask_mode, 33) if mask_mode else None

    pil = dst.copy()
    pil.paste(src, tuple(box), mask)

    logos: dict[str, Image.Image] = {"src_in": src.convert("RGBA")}
    ops = [
        {"op": "load_reg", "reg": "logo:src_in", "save_as": "src_rgba"},
        {"op": "convert", "target": "src_rgba", "mode": src_mode, "save_as": "src"},
    ]
    paste_op: dict[str, Any] = {"op": "paste_image", "src_reg": "src", "box": box}
    if mask is not None:
        logos["mask_in"] = mask.convert("RGBA")
        ops.append({"op": "load_reg", "reg": "logo:mask_in", "save_as": "mask_rgba"})
        ops.append({"op": "convert", "target": "mask_rgba", "mode": mask_mode, "save_as": "m"})
        paste_op["mask_reg"] = "m"
    # reset main canvas by saving before load_reg
    ops_full: list[dict[str, Any]] = [
        {"op": "save_reg", "save_as": "orig"},
        *ops,
        {"op": "load_reg", "reg": "orig"},
        paste_op,
    ]
    got = _cli_ops(dst, ops_full, tmp_path, "pimg", logos=logos)
    same_pixels(got, np.asarray(pil, dtype=np.uint8), "ink paste image")


@pytest.mark.parametrize(
    ("dst_mode", "mask_mode", "color", "box"),
    [
        ("L", None, 177, [-2, 3, 7, 10]),
        ("L", "L", 255, [4, -1]),
        ("L", "RGBA", 42, [0, 0]),
        ("RGB", None, [20, 40, 60], [1, 2, 10, 9]),
        ("RGB", "L", [220, 110, 55], [-3, 5]),
        ("RGB", "RGBA", [12, 34, 56], [5, -2]),
        ("RGBA", "L", [12, 34, 56, 200], [2, 2]),
        ("RGBA", "RGBA", [99, 88, 77, 150], [-1, 3]),
        ("RGB", "L", [1, 2, 3], [100, 100]),
    ],
    ids=anon_ids(9),
)
def test_paste_color(
    dst_mode: str,
    mask_mode: str | None,
    color: Any,
    box: list[int],
    tmp_path: Path,
) -> None:
    dst = _synth_img(17, 13, dst_mode, 44)
    mask = _synth_img(8, 6, mask_mode, 55) if mask_mode else None
    pil = dst.copy()
    c_val = tuple(color) if isinstance(color, list) else color
    pil.paste(c_val, tuple(box), mask)

    ops: list[dict[str, Any]] = [{"op": "save_reg", "save_as": "orig"}]
    logos: dict[str, Image.Image] = {}
    paste_op: dict[str, Any] = {"op": "paste_color", "color": color, "box": box}
    if mask is not None:
        logos["m_in"] = mask.convert("RGBA")
        ops.append({"op": "load_reg", "reg": "logo:m_in", "save_as": "m_rgba"})
        ops.append({"op": "convert", "target": "m_rgba", "mode": mask_mode, "save_as": "m"})
        paste_op["mask_reg"] = "m"
    ops.extend([{"op": "load_reg", "reg": "orig"}, paste_op])
    got = _cli_ops(dst, ops, tmp_path, "pcol", logos=logos)
    same_pixels(got, np.asarray(pil, dtype=np.uint8), "ink paste color")


@pytest.mark.parametrize("scale", [0.0, 0.3, 0.35, 1.0, 1.4, -0.2], ids=anon_ids(6))
def test_point_mul(scale: float, tmp_path: Path) -> None:
    img = _synth_img(17, 13, "L", 66)
    pil = img.point(lambda v: int(v * scale))
    got = _cli_ops(img, [{"op": "point_mul", "scale": scale}], tmp_path, "pt")
    same_pixels(got, np.asarray(pil, dtype=np.uint8), "ink point_mul")


@pytest.mark.parametrize(
    ("src_mode", "dst_mode"),
    [
        ("RGB", "L"),
        ("RGBA", "L"),
        ("RGBA", "RGB"),
        ("RGB", "RGBA"),
        ("L", "RGB"),
        ("L", "RGBA"),
    ],
    ids=anon_ids(6),
)
def test_convert(src_mode: str, dst_mode: str, tmp_path: Path) -> None:
    img = _synth_img(21, 17, src_mode, 77)
    pil = img.convert(dst_mode)
    got = _cli_ops(img, [{"op": "convert", "mode": dst_mode}], tmp_path, "conv")
    same_pixels(got, np.asarray(pil, dtype=np.uint8), "ink convert")


def test_alpha_channel_and_putalpha(tmp_path: Path) -> None:
    rgba = _synth_img(19, 15, "RGBA", 88)
    pil_a = rgba.getchannel("A")
    got_a = _cli_ops(rgba, [{"op": "get_alpha"}], tmp_path, "ga")
    same_pixels(got_a, np.asarray(pil_a, dtype=np.uint8), "ink get_alpha")

    rgb = _synth_img(19, 15, "RGB", 89)
    pil_put_img = rgb.copy()
    pil_put_img.putalpha(pil_a)
    got_put_img = _cli_ops(
        rgb,
        [
            {"op": "save_reg", "save_as": "orig"},
            {"op": "load_reg", "reg": "logo:src", "save_as": "s"},
            {"op": "get_alpha", "target": "s", "save_as": "a"},
            {"op": "load_reg", "reg": "orig"},
            {"op": "put_alpha_reg", "reg": "a"},
        ],
        tmp_path,
        "pai",
        logos={"src": rgba},
    )
    same_pixels(got_put_img, np.asarray(pil_put_img, dtype=np.uint8), "ink putalpha image")

    pil_put_c = rgb.copy()
    pil_put_c.putalpha(123)
    got_put_c = _cli_ops(rgb, [{"op": "put_alpha_const", "alpha": 123}], tmp_path, "pac")
    same_pixels(got_put_c, np.asarray(pil_put_c, dtype=np.uint8), "ink putalpha const")


def test_alpha_composite(tmp_path: Path) -> None:
    dst = _synth_img(23, 19, "RGBA", 91)
    src = _synth_img(23, 19, "RGBA", 92)
    pil = Image.alpha_composite(dst, src)
    got = _cli_ops(
        dst,
        [{"op": "alpha_composite_reg", "reg": "logo:over"}],
        tmp_path,
        "ac",
        logos={"over": src},
    )
    same_pixels(got, np.asarray(pil, dtype=np.uint8), "ink alpha_composite")


@pytest.mark.parametrize("mode", ["L", "RGB", "RGBA"], ids=anon_ids(3))
def test_rotate_90(mode: str, tmp_path: Path) -> None:
    img = _synth_img(23, 13, mode, 93)
    pil = img.rotate(90, expand=True)
    got = _cli_ops(img, [{"op": "rotate90"}], tmp_path, "rot")
    same_pixels(got, np.asarray(pil, dtype=np.uint8), "ink rotate90")


@pytest.mark.parametrize(
    ("mode", "box"),
    [
        ("RGB", [2, 3, 10, 9]),
        ("RGB", [-3, -2, 8, 7]),
        ("RGB", [5, 4, 25, 20]),
        ("RGB", [-5, -5, 25, 25]),
        ("RGB", [30, 30, 35, 35]),
        ("L", [-2, 1, 7, 14]),
        ("RGBA", [2.5, 3.5, 10.5, 9.5]),
    ],
    ids=anon_ids(7),
)
def test_crop(mode: str, box: list[float], tmp_path: Path) -> None:
    img = _synth_img(17, 13, mode, 94)
    pil = img.crop(tuple(box))
    got = _cli_ops(img, [{"op": "crop", "box": box}], tmp_path, "crop")
    same_pixels(got, np.asarray(pil, dtype=np.uint8), "ink crop")


@pytest.mark.parametrize("n", [0, 1, 7, 8, 9, 127, 128, 129, 255, 256, 257, 100003], ids=anon_ids(12))
@pytest.mark.parametrize("op", ["sum", "mean"], ids=["sum", "mean"])
def test_numpy_pairwise_reductions(n: int, op: str, tmp_path: Path) -> None:
    rng = np.random.default_rng(101 + n)
    arr = (rng.random(n) * 20.0 - 10.0).astype(np.float32) if n else np.array([], dtype=np.float32)
    p = tmp_path / "arr.npy"
    np.save(p, arr)
    code, payload = run_json(["ink-reduce", "--in", str(p), "--op", op])
    check(code == 0, "ink-reduce exit")
    ref = arr.sum() if op == "sum" else arr.mean()
    ref_bits = int.from_bytes(np.float32(ref).tobytes(), "little")
    got_bits = int(payload["bits"])
    if math.isnan(float(ref)):
        check(math.isnan(float(np.frombuffer(got_bits.to_bytes(4, "little"), dtype=np.float32)[0])), "reduce nan")
    else:
        check(got_bits == ref_bits, "reduce exact f32 bits")


def test_numpy_reductions_special_values(tmp_path: Path) -> None:
    # Negative zero, injected NaN, and uint8->float32 mean (used by text_ink & paint_clogo)
    neg0 = np.array([-0.0], dtype=np.float32)
    p0 = tmp_path / "neg0.npy"
    np.save(p0, neg0)
    code, res0 = run_json(["ink-reduce", "--in", str(p0), "--op", "sum"])
    check(code == 0, "neg0 sum exit")
    check(int(res0["bits"]) == int.from_bytes(neg0.sum().tobytes(), "little"), "neg0 sum bits")

    with_nan = np.array([1.0, np.nan, 2.0], dtype=np.float32)
    pn = tmp_path / "nan.npy"
    np.save(pn, with_nan)
    code, resn = run_json(["ink-reduce", "--in", str(pn), "--op", "mean"])
    check(code == 0, "nan mean exit")
    got_nan = float(np.frombuffer(int(resn["bits"]).to_bytes(4, "little"), dtype=np.float32)[0])
    check(math.isnan(got_nan), "nan propagates")

    u8_arr = np.random.default_rng(202).integers(0, 256, size=(37, 41), dtype=np.uint8)
    pu = tmp_path / "u8.npy"
    np.save(pu, u8_arr)
    code, resu = run_json(["ink-reduce", "--in", str(pu), "--op", "mean"])
    check(code == 0, "u8 mean exit")
    ref_u8 = np.asarray(u8_arr, dtype=np.float32).mean()
    check(int(resu["bits"]) == int.from_bytes(np.float32(ref_u8).tobytes(), "little"), "u8 mean bits")
