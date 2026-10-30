"""E0 等价测试的共用部分：夹具位置、确定性用例生成。

- 理光字节（DAT）**不在仓库里**：夹具直接从本机已有的固件打包目录现场读取，找不到就跳过相关测试；
- 用例生成只依赖 `final.json`，不需要理光数据，所以黄金清单（只含哈希）可以进仓库。
"""

from __future__ import annotations

import importlib.util
import json
import random
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RESEARCH = REPO / "research"
GOLDEN = Path(__file__).parent / "golden"

# 用户卡上 DAT 的开发机副本（各代一份，来自固件打包目录）
FIXTURE_DATS = {
    127: REPO / "firmware/package_v36_g127/local/SD_ROOT/GXRCE.DAT",
    129: REPO / "firmware/package_v36_g129/local/SD_ROOT/GXRCE.DAT",
    131: REPO / "firmware/package_v36_g131/local/SD_ROOT/GXRCE.DAT",
    135: REPO / "firmware/package_v37_g135/local/SD_ROOT/GXRCE.DAT",
}
FIXTURE_SHA256 = {
    127: "9100616f24dae04eb43138805336b3eebc3eebcc5b86f14b3e0b30969474ead6",
    129: "b58396748f1d5bba5ed480c1e7a81bc881a9df08968db2b778086c7c1a9cbf48",
    131: "5ca22d7b3115c9d7a4a556ee1fcb9105b7768e9fac4250873ea10689f1db3980",
    135: "57743bc105bc3d51b7fa65b44efd90afb022b2356d4e7846d470cc0c641c438a",
}
SCENES = ("desk", "neutral", "apple", "redblue")
KNOBS_SRC = RESEARCH / "knobs_v2_r2/src"
SCENE_DIR = RESEARCH / "knobs_v2_r2/local"


def load_by_path(name: str, path: Path, alias: dict | None = None):
    """按文件路径加载研究模块，用私有名字；`alias` 临时占住它 import 的同伴模块名（例如 knobs）。"""
    saved = {}
    for key, mod in (alias or {}).items():
        saved[key] = sys.modules.get(key)
        sys.modules[key] = mod
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        for key, old in saved.items():
            if old is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = old
    return mod


def research_knobs_and_menu():
    knobs = load_by_path("research_knobs", KNOBS_SRC / "knobs.py")
    menu = load_by_path("research_menu", KNOBS_SRC / "menu.py", alias={"knobs": knobs})
    return knobs, menu


def knob_cases(data: dict) -> list[dict]:
    """确定性用例：所有单旋钮状态 + 随机多旋钮菜单状态 + 随机原始剂量（含 wb/tone 方法）。"""
    out = []
    for r in data["recipes"]:
        items = data["items"][r["first_item"] : r["first_item"] + r["item_count"]]
        defaults = [it["default_level"] for it in items]
        for i, it in enumerate(items):
            for p in it["points"]:
                levels = list(defaults)
                levels[i] = p["level"]
                out.append(
                    {
                        "id": f"s/{r['unit']}/{r['slot']}/{i}/{p['level']}",
                        "kind": "menu",
                        "unit": r["unit"],
                        "slot": r["slot"],
                        "levels": levels,
                    }
                )
    rnd = random.Random(20261030)
    for r in data["recipes"]:
        items = data["items"][r["first_item"] : r["first_item"] + r["item_count"]]
        for n in range(30):
            levels = [rnd.choice(it["points"])["level"] for it in items]
            out.append(
                {
                    "id": f"m/{r['unit']}/{r['slot']}/{n}",
                    "kind": "menu",
                    "unit": r["unit"],
                    "slot": r["slot"],
                    "levels": levels,
                }
            )
    limits = (16384, 16384, 16384, 4096, 16384, 4096, 4096, 4096, 4096, 4096)  # knobs.MAX_DOSES
    for r in data["recipes"]:
        for n in range(20):
            doses = [rnd.randint(-m, m) for m in limits]
            doses[3] = abs(doses[3])
            for j in range(10):  # 一部分维度归零，让单旋钮/少旋钮组合也被覆盖
                if rnd.random() < 0.5:
                    doses[j] = 0
            out.append(
                {
                    "id": f"r/{r['unit']}/{r['slot']}/{n}",
                    "kind": "raw",
                    "unit": r["unit"],
                    "slot": r["slot"],
                    "amount": rnd.randint(0, 150),
                    "doses": doses,
                    "wb_method": rnd.randint(0, 1),
                    "tone_method": rnd.randint(0, 1),
                }
            )
    return out


def run_case(knobs, menu, data, case: dict, stock: bytes, base: bytes):
    """用给定的 knobs/menu 实现算一个用例，返回 ('ok', entry, stats) 或 ('error', message)。"""
    try:
        if case["kind"] == "menu":
            req = menu.request(data, case["unit"], case["slot"], case["levels"])
        else:
            req = knobs.Request(case["amount"], tuple(case["doses"]), case["wb_method"], case["tone_method"])
        entry, stats = knobs.apply(stock, base, req)
    except ValueError as e:
        return ("error", str(e))
    return ("ok", entry, stats)


def outcome_digest(outcome) -> str:
    import hashlib

    if outcome[0] == "error":
        return "error:" + hashlib.sha256(outcome[1].encode()).hexdigest()[:12]
    _, entry, stats = outcome
    return hashlib.sha256(entry + json.dumps(stats, sort_keys=True).encode()).hexdigest()[:20]


def fixture_dat(gen: int) -> bytes | None:
    p = FIXTURE_DATS[gen]
    if not p.is_file():
        return None
    import hashlib

    b = p.read_bytes()
    if hashlib.sha256(b).hexdigest() != FIXTURE_SHA256[gen]:
        return None
    return b
