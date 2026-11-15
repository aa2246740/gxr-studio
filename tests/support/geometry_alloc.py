"""Compile and run an external OOM-injection probe for Rule 2 allocations in gxr-finish."""

from __future__ import annotations

from pathlib import Path

from support.core_rlibs import compile_and_run_probe

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


def run_rule2_oom_probe(tmp_path: Path) -> None:
    """Compile and execute the OOM probe against the release rlibs cargo reports for gxr-finish and gxr-raw."""
    compile_and_run_probe(tmp_path, "rule2_oom_probe", _PROBE_RS)
