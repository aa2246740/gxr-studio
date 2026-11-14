"""Allocation-rule driver for gxr-hdr (Sol r3 P2-10, UNIT-COMMON "which allocations must be
fallible").

A tiny Rust program links ``gxr-hdr`` with a global allocator that, while one entry point
runs, returns null for every allocation larger than a threshold. The threshold is 8× the
bytes of the entry point's validated inputs, so only rule-2 allocations (sized from
parameters, not bounded by 8× the input) can be refused. A rule-2 path must then return
``value_error`` / ``array is too big; ...``; a path with no rule-2 allocation must succeed
with zero refusals. The program needs ``unsafe`` for the allocator, so it is built outside
the core workspace, in a temporary directory, offline; nothing is written to the repository.
"""

from __future__ import annotations

import os
import pwd
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

CARGO_TOML = """\
[package]
name = "hdr-alloc-rules"
version = "0.0.0"
edition = "2024"
publish = false

[dependencies]
gxr-hdr = {{ path = "{crate}" }}

[workspace]
"""

MAIN_RS = r"""
//! Refuse every allocation above LIMIT bytes inside one gxr-hdr entry point; print the outcome.
use std::alloc::{GlobalAlloc, Layout, System};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};

use gxr_hdr::{Alignment, Params, Plane32};

struct Refuse;
static ACTIVE: AtomicBool = AtomicBool::new(false);
static LIMIT: AtomicUsize = AtomicUsize::new(usize::MAX);
/// 0: count allocations above LIMIT without refusing; k: refuse only the k-th one.
static REFUSE_AT: AtomicUsize = AtomicUsize::new(0);
static ABOVE: AtomicUsize = AtomicUsize::new(0);
static REFUSED: AtomicUsize = AtomicUsize::new(0);

unsafe impl GlobalAlloc for Refuse {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        if ACTIVE.load(Ordering::SeqCst) && layout.size() > LIMIT.load(Ordering::SeqCst) {
            let k = ABOVE.fetch_add(1, Ordering::SeqCst) + 1;
            if k == REFUSE_AT.load(Ordering::SeqCst) {
                REFUSED.fetch_add(1, Ordering::SeqCst);
                return std::ptr::null_mut();
            }
        }
        unsafe { System.alloc(layout) }
    }
    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        unsafe { System.dealloc(ptr, layout) }
    }
}

#[global_allocator]
static ALLOCATOR: Refuse = Refuse;

/// Sol's r3 input: s[i] = 0.02 + 0.90 * i / 1023.
fn sol(n: usize) -> Vec<f32> {
    (0..n).map(|i| 0.02 + 0.90 * i as f32 / 1023.0).collect()
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let op = args[1].as_str();
    // "count": count allocations above 8× the input; k >= 1: refuse only the k-th of them.
    let refuse_at: usize = if args[2] == "count" { 0 } else { args[2].parse().unwrap() };
    // Inputs are built before the allocator is armed; `bytes` is what the entry validates.
    let s = sol(1024);
    let raw2: Vec<f32> = s.iter().map(|v| 2.0 * v).collect();
    let clip32 = vec![false; 1024];
    let clip8 = vec![false; 64];
    let tiny = sol(16);
    let rgb: Vec<u8> = (0..4 * 4 * 3).map(|i| (i * 13 % 256) as u8).collect();
    // Two tight clusters 2000 apart: enough samples per bin to fit, about 16000 edges.
    let xs: Vec<f32> = (0..160)
        .map(|i| if i < 80 { -1000.0 + 0.0005 * i as f32 } else { 1000.0 + 0.0005 * (i - 80) as f32 })
        .collect();
    let ys: Vec<f32> = xs.iter().map(|x| x * 0.5).collect();
    let valid = vec![true; 160];
    let sdr = Plane32::new(s.clone(), 32, 32).unwrap();
    let raw = Plane32::new(s.clone(), 32, 32).unwrap();
    let raw8 = Plane32::new(sol(64), 8, 8).unwrap();
    // Sol r4: one-pixel inputs, so the sigma-sized Gaussian kernel exceeds 8x the input.
    let one = [0.25f32];
    let px = Plane32::new(vec![0.02], 1, 1).unwrap();
    let px2 = Plane32::new(vec![0.02], 1, 1).unwrap();
    let al = Alignment {
        scale: 1.0,
        dy: 0,
        dx: 0,
        correlation: 0.0,
        tiles_moved: 0,
        shift_frac: 0.0,
        tiles_unverified: 0,
    };
    let bytes = match op {
        "align" => 2 * 4 * 1024,
        "compute_gain" => 2 * 4 * 1024 + 1024,
        "register" => 4 * 64 + 64,
        "block_mean" => 4 * 16,
        "luma" => 4 * 4 * 3,
        "fit_curve" => 160 * (4 + 4 + 1),
        "gaussian_256" => 4,
        "align_1x1" => 2 * 4,
        _ => panic!("op"),
    };
    LIMIT.store(8 * bytes, Ordering::SeqCst);
    REFUSE_AT.store(refuse_at, Ordering::SeqCst);
    ACTIVE.store(true, Ordering::SeqCst);
    let result: Result<(), (&'static str, String)> = match op {
        "align" => gxr_hdr::align(&sdr, &raw).map(|_| ()).map_err(|e| (e.kind, e.message)),
        "compute_gain" => gxr_hdr::compute_gain(&s, &raw2, &clip32, 32, 32, &Params::default())
            .map(|_| ())
            .map_err(|e| (e.kind, e.message)),
        "register" => gxr_hdr::register(&raw8, &clip8, (1024, 1024), &al, (8, 8))
            .map(|_| ())
            .map_err(|e| (e.kind, e.message)),
        "block_mean" => gxr_hdr::block_mean(&tiny, 4, 4, 300).map(|_| ()).map_err(|e| (e.kind, e.message)),
        "luma" => gxr_hdr::sdr_luma_grid(&rgb, 4, 4, 300).map(|_| ()).map_err(|e| (e.kind, e.message)),
        "gaussian_256" => gxr_hdr::gaussian_filter(&one, 1, 1, 256.0).map(|_| ()).map_err(|e| (e.kind, e.message)),
        "align_1x1" => gxr_hdr::align(&px, &px2).map(|_| ()).map_err(|e| (e.kind, e.message)),
        _ => gxr_hdr::fit_curve(&xs, &ys, &valid).map(|_| ()).map_err(|e| (e.kind, e.message)),
    };
    ACTIVE.store(false, Ordering::SeqCst);
    let (above, refused) = (ABOVE.load(Ordering::SeqCst), REFUSED.load(Ordering::SeqCst));
    match result {
        Ok(()) => println!("ok above={above} refused={refused}"),
        Err((kind, message)) => println!("error above={above} refused={refused} {kind}: {message}"),
    }
}
"""

# rule 2: these paths size buffers from parameters, so an injected refusal must surface.
RULE2_OPS = ["register", "block_mean", "luma", "fit_curve", "gaussian_256", "align_1x1"]
# no rule-2 allocation is reachable here with Sol's inputs: nothing above 8× the input.
BOUNDED_OPS = ["align", "compute_gain"]


def build(tmp: Path) -> Path | None:
    """Build the driver; ``None`` when cargo is not available."""
    cargo = shutil.which("cargo")
    if cargo is None:
        return None
    src = tmp / "hdr-alloc-rules"
    (src / "src").mkdir(parents=True)
    (src / "Cargo.toml").write_text(CARGO_TOML.format(crate=REPO / "core" / "crates" / "gxr-hdr"))
    (src / "src" / "main.rs").write_text(MAIN_RS)
    cargo_home = os.environ.get("CARGO_HOME") or str(Path(pwd.getpwuid(os.getuid()).pw_dir) / ".cargo")
    env = {**os.environ, "CARGO_TARGET_DIR": str(tmp / "target"), "CARGO_HOME": cargo_home}
    proc = subprocess.run(
        [cargo, "build", "--release", "--offline", "--quiet", "--manifest-path", str(src / "Cargo.toml")],
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if proc.returncode != 0:
        raise AssertionError(f"building the allocation-rule driver failed:\n{proc.stderr}")
    return tmp / "target" / "release" / "hdr-alloc-rules"


def run(binary: Path, op: str, mode: str) -> subprocess.CompletedProcess[str]:
    """``mode`` is ``"count"`` (refuse nothing) or ``"k"``: refuse only the k-th allocation above 8×."""
    return subprocess.run([str(binary), op, mode], capture_output=True, text=True, timeout=120)
