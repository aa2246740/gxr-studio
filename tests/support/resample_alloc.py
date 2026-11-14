"""Compile and run an external OOM-injection probe for Rule 2 allocations in gxr-finish resample."""

from __future__ import annotations

from pathlib import Path

from support.core_cli import CLI, REPO
from support.geometry_alloc import _find_rustc, _skip_or_fail

_PROBE_RS = """use std::alloc::{GlobalAlloc, Layout, System};
use std::sync::atomic::{AtomicUsize, Ordering};

use gxr_finish::geometry::bounds::ARRAY_TOO_BIG;
use gxr_finish::resample::{Filter, Mode, precompute_coeffs, resample_f32, resample_u8};

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
    let rgb = vec![128_u8; 20 * 20 * 3];
    let rgba = vec![128_u8; 20 * 20 * 4];
    let f32_img = vec![0.5_f32; 20 * 20];

    let c = precompute_coeffs(20, 0.0, 20.0, 10, Filter::Lanczos).unwrap();
    let kk_bytes = c.kk.len() * std::mem::size_of::<f64>();
    let bounds_bytes = c.bounds.len() * std::mem::size_of::<u32>();

    // 1. Identity copy allocation failure
    FAIL_SIZE.store(rgb.len(), Ordering::Relaxed);
    let r_id = resample_u8(&rgb, 20, 20, Mode::Rgb, Filter::Lanczos, 20, 20);
    FAIL_SIZE.store(0, Ordering::Relaxed);
    assert_too_big(r_id, "identity_u8");

    // 2. Coefficient table allocation failure
    FAIL_SIZE.store(kk_bytes, Ordering::Relaxed);
    let r_kk = resample_u8(&rgb, 20, 20, Mode::Rgb, Filter::Lanczos, 10, 10);
    FAIL_SIZE.store(0, Ordering::Relaxed);
    assert_too_big(r_kk, "coeffs_kk");

    // 3. Coefficient bounds allocation failure
    FAIL_SIZE.store(bounds_bytes, Ordering::Relaxed);
    let r_bd = resample_u8(&rgb, 20, 20, Mode::Rgb, Filter::Lanczos, 10, 10);
    FAIL_SIZE.store(0, Ordering::Relaxed);
    assert_too_big(r_bd, "coeffs_bounds");

    // 4. RGBA premultiply buffer allocation failure
    FAIL_SIZE.store(rgba.len(), Ordering::Relaxed);
    let r_pre = resample_u8(&rgba, 20, 20, Mode::Rgba, Filter::Lanczos, 10, 10);
    FAIL_SIZE.store(0, Ordering::Relaxed);
    assert_too_big(r_pre, "rgba_premul");

    // 5. Final output buffer allocation failure (10x10x3 = 300)
    FAIL_SIZE.store(10 * 10 * 3, Ordering::Relaxed);
    let r_out = resample_u8(&rgb, 20, 20, Mode::Rgb, Filter::Lanczos, 10, 10);
    FAIL_SIZE.store(0, Ordering::Relaxed);
    assert_too_big(r_out, "u8_out");

    // 6. Float32 output buffer allocation failure (10x10x4 = 400)
    FAIL_SIZE.store(10 * 10 * 4, Ordering::Relaxed);
    let r_f32 = resample_f32(&f32_img, 20, 20, Filter::Box, 10, 10);
    FAIL_SIZE.store(0, Ordering::Relaxed);
    assert_too_big(r_f32, "f32_out");
}
"""


def run_resample_oom_probe(tmp_path: Path) -> None:
    """Compile and execute the OOM probe against the built release ``libgxr_finish`` rlib."""
    import subprocess

    if not CLI.is_file():
        _skip_or_fail(f"need {CLI}; run make core")
    deps = REPO / "core" / "target" / "release" / "deps"
    finish_rlibs = sorted(deps.glob("libgxr_finish-*.rlib"), key=lambda p: p.stat().st_mtime, reverse=True)
    raw_rlibs = sorted(deps.glob("libgxr_raw-*.rlib"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not finish_rlibs or not raw_rlibs:
        _skip_or_fail(f"missing release rlibs under {deps}; run make core")

    rustc = _find_rustc()
    src_path = tmp_path / "resample_oom_probe.rs"
    bin_path = tmp_path / "resample_oom_probe"
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
