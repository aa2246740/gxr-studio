"""可选研究文件与 g178 实际 C 源码的宿主对照。仅现场读取，不内嵌数据。"""

from __future__ import annotations

import ctypes
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from e0_support import REPO
from g152_support import CONTROL_NAMES
from gxr_studio.camdata import r5

PACKAGES = {
    176: REPO / "research/body_r5_freeze/local/packages/r5-NOT_FOR_INSTALLATION/SD_ROOT",
    178: REPO / "research/build_g178_r1/local/model",
}
DAT_SHA256 = {
    176: "852f72f6b7a48c592b0658ca65c6571b23075a6037cbb800c6145cef987a475f",
    178: "2cb4d5817c9bcad91194f9c2d02b6c135a83524bfcfa5cdc349afb81bb91fae6",
}


def need_bundle(generation):
    folder = PACKAGES[generation]
    if not all((folder / name).is_file() for name in ("GXRCE.DAT", "GXRFX.BIN")):
        pytest.skip(f"g{generation} 的本地双文件副本不在本机")
    raw, library = (folder / "GXRCE.DAT").read_bytes(), (folder / "GXRFX.BIN").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == DAT_SHA256[generation], "受审 DAT 已变化，停止对照"
    assert hashlib.sha256(library).hexdigest() == r5.LIBRARY_SHA256[generation], "受审库已变化，停止对照"
    return raw, library


class FullRecordReference:
    def __init__(self, folder):
        product = REPO / "research/build_g178_r1/local/engine-004/candidate/containers/product"
        crc_source = product.parent.parent / "engine_services/crc.c"
        compiler = shutil.which("clang")
        if not product.is_dir() or not crc_source.is_file() or compiler is None:
            pytest.skip("g178 的真实 C 源码或 clang 不在本机")
        folder.mkdir(parents=True, exist_ok=True)
        crc = crc_source.read_text()
        (folder / "crc.c").write_text('#include "fx_pool.h"\n' + crc[crc.index("uint32_t gxr2_crc_add") : crc.index("uint32_t rv_crc_update")])
        core = product / "src/firmware/card_container_v2"
        sources = [core / name for name in ("gate.c", "semantic.c", "common.c")]
        sources += [product / name for name in ("fx_pool.c", "ui_v2_data.c", "memory.c", "kv2/kv_data.c", "kv2/kv_ui.c", "kv2/kv_math.c")]
        wrapper = Path(__file__).parent / "support/g178_reference.c"
        target = folder / ("reference.dylib" if sys.platform == "darwin" else "reference.so")
        mode = ["-dynamiclib"] if sys.platform == "darwin" else ["-shared", "-fPIC"]
        command = [compiler, "-std=c11", "-O2", "-fno-builtin", "-DRV_HOST", *mode, f"-I{product}", str(wrapper), str(folder / "crc.c")]
        command += [str(p) for p in sources] + ["-o", str(target)]
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=120, env={**os.environ, "CLANG_MODULE_CACHE_PATH": str(folder / "clang")}
        )
        assert result.returncode == 0, result.stderr
        self.lib = ctypes.CDLL(str(target))
        self.lib.studio_full_record.argtypes = [
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        self.lib.studio_full_record.restype = ctypes.c_int

    def resolve(self, catalogue, unit, position, levels):
        values = (ctypes.c_int16 * 10)(*(levels.get(n, 100 if n == "amount" else 0) for n in CONTROL_NAMES))
        out = ctypes.create_string_buffer(552)
        result = self.lib.studio_full_record(ctypes.create_string_buffer(catalogue), len(catalogue), unit, position, values, out)
        assert result == 0, (unit, position, levels, result)
        return out.raw
