"""Python 引擎与 C 镜像（`knobs.c`，相机端算法的同源实现）对拍。

ARCHITECTURE §9.1：`knobs.c` 与 Python 逐字节一致，工作室不调用它，但 CI 里有编译器时应对拍。
这里把研究目录里的 `knobs.c` 编译到临时目录（不写仓库），对整张用例清单逐字节比较条目和统计。
没有 clang 或研究目录不在本机就跳过。
"""

from __future__ import annotations

import ctypes as ct
import shutil
import subprocess

import pytest

import e0_support as t
from gxr_studio.camdata import datcodec as dc
from gxr_studio.engine import knobs, menu

STATS = ("clamps", "node_repairs", "monotone_repairs", "slope_repairs", "colour_cap", "matrix_cap", "rowsum_cap")


class CRequest(ct.Structure):
    _fields_ = [
        ("amount", ct.c_int32),
        ("dose", ct.c_int32 * 10),
        ("wb_method", ct.c_int32),
        ("tone_method", ct.c_int32),
    ]


class CStats(ct.Structure):
    _fields_ = [(n, ct.c_int32) for n in STATS]


@pytest.fixture(scope="module")
def lib(tmp_path_factory):
    src = t.KNOBS_SRC / "knobs.c"
    clang = shutil.which("clang") or "/usr/bin/clang"
    if not src.is_file() or not shutil.which(clang):
        pytest.skip("knobs.c 或 clang 不在本机")
    out = tmp_path_factory.mktemp("c-parity") / "knobs.dylib"
    cp = subprocess.run(
        [clang, "-std=c99", "-O2", "-Wall", "-Wextra", "-Werror", "-pedantic", "-dynamiclib", str(src), "-o", str(out)],
        capture_output=True,
        text=True,
    )
    if cp.returncode:
        pytest.skip("knobs.c 编译失败：" + cp.stderr[-300:])
    lib = ct.CDLL(str(out))
    lib.kv_apply.argtypes = [ct.c_void_p, ct.c_void_p, ct.POINTER(CRequest), ct.c_void_p, ct.POINTER(CStats)]
    lib.kv_apply.restype = ct.c_int
    return lib


def c_apply(lib, stock, base, req):
    rq = CRequest(req.amount, (ct.c_int32 * 10)(*req.doses), req.wb_method, req.tone_method)
    out, stats = (ct.c_uint8 * 552)(), CStats()
    rc = lib.kv_apply(stock, base, ct.byref(rq), out, ct.byref(stats))
    if rc:
        raise ValueError(f"C apply returned {rc}")
    return bytes(out), {n: getattr(stats, n) for n in STATS}


def test_python_engine_equals_the_c_mirror_on_every_case(lib):
    raw = t.fixture_dat(127)
    if raw is None:
        pytest.skip("g127 夹具 DAT 不在本机")
    dat = dc.parse(raw)
    data = menu.load()
    n = 0
    for c in t.knob_cases(data):
        stock, base = dat.entry_of(c["unit"], 4), dat.entry_of(c["unit"], c["slot"])
        req = (
            menu.request(data, c["unit"], c["slot"], c["levels"])
            if c["kind"] == "menu"
            else knobs.Request(c["amount"], tuple(c["doses"]), c["wb_method"], c["tone_method"])
        )
        assert c_apply(lib, stock, base, req) == knobs.apply(stock, base, req), c["id"]
        n += 1
    assert n >= 2000
