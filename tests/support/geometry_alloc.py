"""Compile and run an external OOM-injection probe for Rule 2 allocations in gxr-finish."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from support.core_cli import CLI, REPO

_PROBE_RS = """use std::alloc::{GlobalAlloc, Layout, System};
use std::sync::atomic::{AtomicUsize, Ordering};

use gxr_finish::geometry::bounds::ARRAY_TOO_BIG;
use gxr_finish::geometry::{
    fit_ratio_in_valid, normalized_valid_rect, output_size, parse_geometry, warp, warp_valid,
};

static FAIL_SIZE: AtomicUsize = AtomicUsize::new(0);

struct FailAlloc;

unsafe impl GlobalAlloc for FailAlloc {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        if layout.size() == FAIL_SIZE.load(Ordering::Relaxed) {
            return std::ptr::null_mut();
        }
        unsafe { System.alloc(layout) }
    }

    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        unsafe { System.dealloc(ptr, layout) }
    }

    unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, new_size: usize) -> *mut u8 {
        if new_size == FAIL_SIZE.load(Ordering::Relaxed) {
            return std::ptr::null_mut();
        }
        unsafe { System.realloc(ptr, layout, new_size) }
    }
}

#[global_allocator]
static ALLOC: FailAlloc = FailAlloc;

fn assert_too_big<T: std::fmt::Debug>(res: Result<T, gxr_raw::CoreError>, label: &str) {
    let err = res.expect_err(label);
    assert_eq!(err.kind, "bad_geometry", "{label}: {err:?}");
    assert_eq!(err.message, ARRAY_TOO_BIG, "{label}: {err:?}");
}

fn main() {
    let mask_8x8 = vec![1_u8; 64];
    let mut mask_241 = vec![1_u8; 241];
    mask_241[0] = 0;
    let rgb_8x8 = vec![0_u8; 8 * 8 * 3];
    let geom_id = parse_geometry("{}").unwrap();
    let geom_exp = parse_geometry(r#"{"vertical":40}"#).unwrap();
    let (ow, oh) = output_size(8, 8, &geom_exp).unwrap();

    FAIL_SIZE.store(64, Ordering::Relaxed);
    let r1 = warp_valid(8, 8, &geom_id);
    FAIL_SIZE.store(0, Ordering::Relaxed);
    assert_too_big(r1, "warp_valid");

    FAIL_SIZE.store(9 * 9 * 4, Ordering::Relaxed);
    let r2 = fit_ratio_in_valid(&mask_8x8, 8, 8, [0.0, 0.0, 1.0, 1.0], 1, 1);
    FAIL_SIZE.store(0, Ordering::Relaxed);
    assert_too_big(r2, "valid_integral");

    FAIL_SIZE.store(240 * std::mem::size_of::<usize>(), Ordering::Relaxed);
    let r3 = normalized_valid_rect(&mask_241, 241, 1);
    FAIL_SIZE.store(0, Ordering::Relaxed);
    assert_too_big(r3, "linspace_indices");

    FAIL_SIZE.store(2 * 240, Ordering::Relaxed);
    let r4 = normalized_valid_rect(&mask_241, 241, 1);
    FAIL_SIZE.store(0, Ordering::Relaxed);
    assert_too_big(r4, "subsample");

    FAIL_SIZE.store(ow * oh * 3, Ordering::Relaxed);
    let r5 = warp(&rgb_8x8, 8, 8, &geom_exp);
    FAIL_SIZE.store(0, Ordering::Relaxed);
    assert_too_big(r5, "warp_expand_rgb");
}
"""


def _skip_or_fail(msg: str) -> None:
    if os.environ.get("GXR_REQUIRE_CORE") == "1":
        pytest.fail(msg)
    pytest.skip(msg)


def _find_rustc() -> str:
    toolchain_toml = REPO / "core" / "rust-toolchain.toml"
    channel = "stable"
    if toolchain_toml.is_file():
        m = re.search(r'channel\s*=\s*"([^"]+)"', toolchain_toml.read_text(encoding="utf-8"))
        if m:
            channel = m.group(1)
    if shutil.which("rustup") is not None:
        proc = subprocess.run(
            ["rustup", "which", "--toolchain", channel, "rustc"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    found = shutil.which("rustc")
    if found is None:
        _skip_or_fail("rustc is not available for Rule 2 OOM probe")
    return str(found)


def run_rule2_oom_probe(tmp_path: Path) -> None:
    """Compile and execute the OOM probe against the built release ``libgxr_finish`` rlib."""
    if not CLI.is_file():
        _skip_or_fail(f"need {CLI}; run make core")
    deps = REPO / "core" / "target" / "release" / "deps"
    finish_rlibs = sorted(deps.glob("libgxr_finish-*.rlib"), key=lambda p: p.stat().st_mtime, reverse=True)
    raw_rlibs = sorted(deps.glob("libgxr_raw-*.rlib"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not finish_rlibs or not raw_rlibs:
        _skip_or_fail(f"missing release rlibs under {deps}; run make core")

    rustc = _find_rustc()
    src_path = tmp_path / "rule2_oom_probe.rs"
    bin_path = tmp_path / "rule2_oom_probe"
    src_path.write_text(_PROBE_RS, encoding="utf-8")
    compile_proc = subprocess.run(
        [
            rustc,
            "--edition=2024",
            "-C",
            "opt-level=2",
            "-C",
            "panic=abort",
            "-L",
            f"dependency={deps}",
            "--extern",
            f"gxr_finish={finish_rlibs[0]}",
            "--extern",
            f"gxr_raw={raw_rlibs[0]}",
            str(src_path),
            "-o",
            str(bin_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert compile_proc.returncode == 0, compile_proc.stdout + compile_proc.stderr
    run_proc = subprocess.run([str(bin_path)], check=False, capture_output=True, text=True, timeout=60)
    assert run_proc.returncode == 0, run_proc.stdout + run_proc.stderr
