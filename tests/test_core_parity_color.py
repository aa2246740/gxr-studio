"""Parity: color-knobs-apply against engine.knobs and engine.menu.

Bytes and the stats dict are exact. Error kind is ``value`` and the message
matches ``ValueError``. ``GXR_REQUIRE_GPU_TEST`` does not make the CLI required.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from gxr_studio.engine import knobs, menu
from support.core_cli import error_of, require_cli, run_json

MENU = Path(__file__).resolve().parents[1] / "src/gxr_studio/engine/data/final.json"


def _knots(buf: bytearray, at: int) -> None:
    for i in range(31):
        struct.pack_into(">H", buf, at + 2 * i, (i + 1) * 128)


def _curve(buf: bytearray, at: int, tilt: int) -> None:
    for i in range(33):
        buf[at + i] = min(255, i * 7 + tilt)


def _entry(flags: int = 0x17, kind: int = 4) -> bytes:
    buf = bytearray(552)
    buf[0] = kind
    buf[1] = flags
    for at in (20, 284, 456):
        _knots(buf, at)
    _curve(buf, 82, 0)
    _curve(buf, 346, 1)
    _curve(buf, 379, 2)
    _curve(buf, 412, 3)
    _curve(buf, 518, 4)
    for sector in range(24):
        a = 8 + (sector % 5)
        c = 3 + (sector % 3)
        buf[115 + 4 * sector : 115 + 4 * sector + 4] = bytes((a, c, c ^ 0x80, a))
    words: list[int] = []
    for _half in range(2):
        words.extend([400, 312, 312, 0, 0, 0, 0, 0, 0, 0, 128, 128, 0, 0])
    struct.pack_into(">28H", buf, 216, *words)
    return bytes(buf)


def _apply(tmp: Path, stock: bytes, base: bytes, params: dict, name: str) -> tuple[int, dict, bytes]:
    entry = tmp / f"{name}-base.bin"
    stock_path = tmp / f"{name}-stock.bin"
    params_path = tmp / f"{name}.json"
    out = tmp / f"{name}-out.bin"
    entry.write_bytes(base)
    stock_path.write_bytes(stock)
    params_path.write_text(json.dumps(params), encoding="utf-8")
    code, payload = run_json(
        [
            "color-knobs-apply",
            "--entry",
            str(entry),
            "--stock",
            str(stock_path),
            "--menu",
            str(MENU),
            "--params",
            str(params_path),
            "--out",
            str(out),
        ]
    )
    written = out.read_bytes() if out.is_file() else b""
    return code, payload, written


def _doses(**overrides: int) -> list[int]:
    doses = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
    names = ("contrast", "highlight", "shadow", "fade", "saturation", "warmth", "tint", "split", "filter", "tone")
    for name, value in overrides.items():
        doses[names.index(name)] = value
    return doses


def _expect_ok(tmp: Path, stock: bytes, base: bytes, params: dict, name: str) -> None:
    request = knobs.Request(
        params.get("amount", 100),
        tuple(params.get("doses", [0] * 10)),
        params.get("wb_method", 0),
        params.get("tone_method", 0),
    )
    entry, stats = knobs.apply(stock, base, request)
    code, payload, written = _apply(tmp, stock, base, params, name)
    assert error_of(code, payload) is None
    assert written == entry
    assert payload["stats"] == stats
    assert payload["length"] == 552


def _expect_error(tmp: Path, stock: bytes, base: bytes, params: dict, name: str) -> None:
    request = knobs.Request(
        params.get("amount", 100),
        tuple(params.get("doses", [0] * 10)),
        params.get("wb_method", 0),
        params.get("tone_method", 0),
    )
    with pytest.raises(ValueError) as caught:
        knobs.apply(stock, base, request)
    code, payload, _written = _apply(tmp, stock, base, params, name)
    err = error_of(code, payload)
    assert err is not None
    assert err.kind == "value"
    assert err.message == str(caught.value)


def test_knobs_match_on_synthetic_entries(tmp_path: Path) -> None:
    require_cli()
    stock = _entry()
    base = _entry(flags=0x17)
    other = _entry(flags=0x01)
    _expect_ok(tmp_path, stock, base, {"amount": 100, "doses": _doses()}, "identity")
    _expect_ok(tmp_path, stock, other, {"amount": 0, "doses": _doses()}, "amount-zero")
    _expect_ok(
        tmp_path,
        stock,
        other,
        {"amount": 80, "doses": _doses(contrast=400, highlight=-300, shadow=100, fade=200, saturation=500)},
        "blend",
    )
    _expect_ok(
        tmp_path,
        stock,
        other,
        {
            "amount": 40,
            "doses": _doses(warmth=-100, tint=50, split=80, filter=-200, tone=300),
            "wb_method": 0,
            "tone_method": 0,
        },
        "matrix",
    )
    _expect_ok(
        tmp_path,
        stock,
        other,
        {"amount": 60, "doses": _doses(warmth=80, tint=-40, split=-20, tone=-100), "wb_method": 1, "tone_method": 1},
        "curves",
    )
    _expect_ok(tmp_path, stock, _entry(kind=3), {"amount": 70, "doses": _doses(saturation=1000, filter=400)}, "bw")
    _expect_error(tmp_path, stock, base, {"amount": 151, "doses": _doses()}, "amount")
    _expect_error(tmp_path, stock, base, {"amount": 100, "doses": _doses(fade=-1)}, "fade")
    _expect_error(tmp_path, stock, base, {"amount": 100, "doses": _doses(contrast=16385)}, "doses")
    _expect_error(tmp_path, stock, base, {"amount": 100, "doses": _doses(), "wb_method": 2}, "method")
    _expect_error(tmp_path, b"\x00" * 10, base, {"amount": 100, "doses": _doses()}, "short")


def test_menu_levels_match_final_json(tmp_path: Path) -> None:
    require_cli()
    data = menu.load()
    recipe = data["recipes"][0]
    items = data["items"][recipe["first_item"] : recipe["first_item"] + recipe["item_count"]]
    levels = [item["points"][0]["level"] for item in items]
    params = {"unit": 0, "slot": 1, "levels": levels}
    code, payload, written = _apply(tmp_path, _entry(), _entry(flags=0x01), params, "menu")
    req = menu.request(data, 0, 1, levels)
    entry, stats = knobs.apply(_entry(), _entry(flags=0x01), req)
    assert error_of(code, payload) is None
    assert written == entry
    assert payload["stats"] == stats
    bad = dict(params)
    bad["levels"] = levels[:-1]
    code, payload, _written = _apply(tmp_path, _entry(), _entry(), bad, "menu-count")
    with pytest.raises(ValueError) as caught:
        menu.request(data, 0, 1, levels[:-1])
    err = error_of(code, payload)
    assert err is not None and err.kind == "value" and err.message == str(caught.value)
    worse = dict(params)
    worse["unit"] = 3
    code, payload, _written = _apply(tmp_path, _entry(), _entry(), worse, "menu-unit")
    with pytest.raises(ValueError) as caught:
        menu.request(data, 3, 1, levels)
    err = error_of(code, payload)
    assert err is not None and err.message == str(caught.value)
    floated = dict(params)
    floated["levels"] = [1.0, *levels[1:]]
    code, payload, _written = _apply(tmp_path, _entry(), _entry(), floated, "menu-float")
    err = error_of(code, payload)
    assert err is not None and err.message == "integer UI level required"
