"""Allocation-failure driver for the HDR FFT (Sol r2 P2-08, after ``alloc_failure.rs``).

A tiny Rust program links ``gxr-hdr`` with a global allocator that returns null for the
n-th allocation (any size) made while an FFT entry point runs. The core must turn that into
its ``value_error`` / ``array is too big; ...`` instead of aborting. The program cannot live
in the core workspace, which forbids ``unsafe``; it is built here, in a temporary directory,
offline, against the checked-out crate. Nothing is written into the repository.
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
name = "hdr-fft-alloc"
version = "0.0.0"
edition = "2024"
publish = false

[dependencies]
gxr-hdr = {{ path = "{crate}" }}

[workspace]
"""

MAIN_RS = r"""
//! Fail the n-th allocation made inside one FFT entry point; print the outcome.
use std::alloc::{GlobalAlloc, Layout, System};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};

use gxr_hdr::Plane32;

struct FailNth;
static ACTIVE: AtomicBool = AtomicBool::new(false);
static SEEN: AtomicUsize = AtomicUsize::new(0);
static FAIL_AT: AtomicUsize = AtomicUsize::new(0);

unsafe impl GlobalAlloc for FailNth {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        if ACTIVE.load(Ordering::SeqCst) {
            let nth = SEEN.fetch_add(1, Ordering::SeqCst) + 1;
            if nth == FAIL_AT.load(Ordering::SeqCst) {
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
static ALLOCATOR: FailNth = FailNth;

fn pairs(h: usize, n: usize) -> Plane32 {
    // Sol's 13-point input generalised: re/im are consecutive (i*0.125 - 1).
    let v: Vec<f32> = (0..h * n * 2).map(|i| i as f32 * 0.125 - 1.0).collect();
    Plane32::new(v, h as u64, (n * 2) as u64).unwrap()
}

fn real(h: usize, w: usize, k: f32) -> Plane32 {
    let v: Vec<f32> = (0..h * w).map(|i| ((i * 7 + 3) % 17) as f32 * k).collect();
    Plane32::new(v, h as u64, w as u64).unwrap()
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let op = args[1].as_str();
    let nth: usize = args[2].parse().unwrap();
    let (a, b) = match op {
        "fft13" | "ifft13" => (pairs(1, 13), None),
        "fft89" | "ifft89" | "fft2d89" => (pairs(4, 89), None),
        "plane" => (real(8, 89, 0.25), Some(real(8, 89, -0.5))),
        "twiddles" => (pairs(1, 1), None),
        _ => panic!("op"),
    };
    FAIL_AT.store(nth, Ordering::SeqCst);
    ACTIVE.store(true, Ordering::SeqCst);
    let result = match op {
        "fft13" | "fft89" => a.fft_complex(false, false, None).map(|_| ()),
        "ifft13" | "ifft89" => a.fft_complex(true, false, None).map(|_| ()),
        "fft2d89" => a.fft_complex(false, true, None).map(|_| ()),
        "plane" => a.phase_plane(b.as_ref().unwrap(), None).map(|_| ()),
        _ => Plane32::fft_twiddles(13, 2, None).map(|_| ()),
    };
    ACTIVE.store(false, Ordering::SeqCst);
    match result {
        Ok(()) => println!("ok {}", SEEN.load(Ordering::SeqCst)),
        Err(e) => println!("error {}: {}", e.kind, e.message),
    }
}
"""

OPS = ["fft13", "ifft13", "fft89", "ifft89", "fft2d89", "plane", "twiddles"]


def build(tmp: Path) -> Path | None:
    """Build the driver; ``None`` when cargo is not available."""
    cargo = shutil.which("cargo")
    if cargo is None:
        return None
    src = tmp / "hdr-fft-alloc"
    (src / "src").mkdir(parents=True)
    (src / "Cargo.toml").write_text(CARGO_TOML.format(crate=REPO / "core" / "crates" / "gxr-hdr"))
    (src / "src" / "main.rs").write_text(MAIN_RS)
    # The test session points HOME at a temporary directory; the offline registry is the user's.
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
        raise AssertionError(f"building the allocation driver failed:\n{proc.stderr}")
    return tmp / "target" / "release" / "hdr-fft-alloc"


def run(binary: Path, op: str, nth: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(binary), op, str(nth)], capture_output=True, text=True, timeout=60)
