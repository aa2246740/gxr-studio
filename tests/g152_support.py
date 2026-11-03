"""新代数的本机夹具和相机 C 参考。公开仓库只保存脚本与摘要，不保存 DAT。"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = [(gen, test) for gen in (150, 152, 153) for test in (False, True)]
ALL_PACKAGES = PACKAGES + [(gen, test) for gen in (154, 155) for test in (False, True)]
ENGINE_VERSIONS = {152: 60, 153: 62, 154: 63, 155: 64}
UNITS = ("jlaunch", "qlaunch", "l08firm")
LOOK_NAMES = (
    "M9 STD",
    "HNCS HiCon",
    "Tri-X 400",
    "Portra 400",
    "Velvia 50",
    "Kodachrome25",
    "HNCS LowSat",
    "Vision3 500T",
    "instax mini",
    "Metropolis",
)
CONTROL_NAMES = ("amount", "contrast", "highlight", "shadow", "fade", "saturation", "warmth", "tint", "split", "filter")


def dat_path(gen: int, test: bool = False) -> Path:
    version = 310 if gen == 155 else 39 if gen in (153, 154) else 38
    package = ROOT / f"firmware/package_v{version}_g{gen}{'_test' if test else ''}"
    if gen == 155:
        return package / "GXRCE.DAT"
    direct = package / "SD_ROOT/GXRCE.DAT"
    return direct if direct.is_file() else package / "local/SD_ROOT/GXRCE.DAT"


def need_dat(gen: int, test: bool = False) -> bytes:
    path = dat_path(gen, test)
    if not path.is_file():
        pytest.skip(f"g{gen}{'_test' if test else ''} DAT 不在本机")
    return path.read_bytes()


class CameraReference:
    def __init__(self, gen: int, folder: Path) -> None:
        product = ROOT / f"firmware/engine_v{ENGINE_VERSIONS[gen]}_g{gen}/candidate/containers/product"
        compiler = shutil.which("clang")
        crc_source = product.parent.parent / "engine_services/crc.c"
        if not product.is_dir() or not crc_source.is_file() or compiler is None:
            pytest.skip(f"g{gen} 的相机 C 源码或 clang 不在本机")
        folder.mkdir(parents=True, exist_ok=True)
        crc = crc_source.read_text(encoding="utf-8")
        (folder / "crc.c").write_text(
            '#include "fx_pool.h"\n' + crc[crc.index("uint32_t gxr2_crc_add") : crc.index("uint32_t rv_crc_update")], encoding="utf-8"
        )
        core = product / "src/firmware/card_container_v2"
        sources = [core / n for n in ("gate.c", "semantic.c", "common.c")]
        sources += [product / n for n in ("fx_pool.c", "ui_v2_data.c", "memory.c", "kv2/kv_data.c", "kv2/kv_ui.c", "kv2/kv_math.c")]
        wrapper = Path(__file__).parent / "support/g152_reference.c"
        target = folder / ("reference.dylib" if sys.platform == "darwin" else "reference.so")
        mode = ["-dynamiclib"] if sys.platform == "darwin" else ["-shared", "-fPIC"]
        command = [compiler, "-std=c11", "-O2", "-fno-builtin", *mode, f"-I{product}", str(wrapper), str(folder / "crc.c")]
        command += [str(p) for p in sources] + ["-o", str(target)]
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=120, env={**os.environ, "CLANG_MODULE_CACHE_PATH": str(folder / "clang")}
        )
        assert result.returncode == 0, result.stderr
        self.lib = ctypes.CDLL(str(target))
        self.lib.studio_resolve.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
        self.lib.studio_builtin.argtypes = self.lib.studio_resolve.argtypes
        self.lib.studio_user.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]
        self.lib.studio_validate.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint]

    def validate(self, raw: bytes, unit: int) -> bool:
        return self.lib.studio_validate(ctypes.create_string_buffer(raw), len(raw), unit) == 0

    def builtin(self, raw: bytes, unit: int, slot: int, levels: dict[str, int]) -> bytes:
        values = (ctypes.c_int16 * 10)(*(levels.get(n, 100 if n == "amount" else 0) for n in CONTROL_NAMES))
        out = ctypes.create_string_buffer(552)
        result = self.lib.studio_builtin(ctypes.create_string_buffer(raw), len(raw), unit, slot, values, out)
        assert result == 0, (unit, slot, levels, result)
        return out.raw

    def resolve(self, raw: bytes, unit: int, index: int, levels: dict[str, int]) -> bytes:
        values = (ctypes.c_int16 * 10)(*(levels.get(n, 100 if n == "amount" else 0) for n in CONTROL_NAMES))
        out = ctypes.create_string_buffer(552)
        result = self.lib.studio_resolve(ctypes.create_string_buffer(raw), len(raw), unit, index, values, out)
        assert result == 0, (unit, index, levels, result)
        return out.raw

    def user(self, raw: bytes, unit: int, index: int) -> bytes:
        out = ctypes.create_string_buffer(552)
        result = self.lib.studio_user(ctypes.create_string_buffer(raw), len(raw), unit, index, out)
        assert result == 0, (unit, index, result)
        return out.raw
