"""Rule-1 order check for the hdr CLI's `.npy` parser (Sol r4 P2-10).

A temporary Rust program mounts the repository's own `common.rs`, `npy.rs` and an unmodified
copy of `cmd_hdr.rs`, with one appended hook that calls the private `parse_npy`. A tracing
allocator counts allocations exactly the size of the payload while the parser runs: a header
that fails validation (u64 overflow, payload too short, payload too long) must be refused
with no payload-sized copy, and a valid header is still accepted. The program needs `unsafe`
for the allocator, so it is built outside the workspace in a temporary directory; nothing is
written into the repository.
"""

from __future__ import annotations

import os
import pwd
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CLI_SRC = REPO / "core" / "crates" / "gxr-cli" / "src"

CARGO_TOML = """\
[package]
name = "hdr-cli-npy-order"
version = "0.0.0"
edition = "2024"
publish = false

[dependencies]
gxr-hdr = {{ path = "{core}/crates/gxr-hdr" }}
gxr-raw = {{ path = "{core}/crates/gxr-raw" }}

[workspace]
"""

HOOK = """

/// Test hook appended to a temporary copy; the repository file is unchanged.
pub fn test_parse(raw: &[u8]) -> bool {
    parse_npy(raw).is_some()
}
"""

MAIN_RS = r"""
#![allow(dead_code, unused_imports)]
use std::alloc::{GlobalAlloc, Layout, System};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};

#[path = "{cli}/npy.rs"]
mod npy;
#[path = "{cli}/common.rs"]
mod common;
#[path = "cmd_hdr_copy.rs"]
mod cmd_hdr;

const PAYLOAD: usize = 4096;
struct Trace;
static ACTIVE: AtomicBool = AtomicBool::new(false);
static COPIES: AtomicUsize = AtomicUsize::new(0);

unsafe impl GlobalAlloc for Trace {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        if ACTIVE.load(Ordering::SeqCst) && layout.size() == PAYLOAD {
            COPIES.fetch_add(1, Ordering::SeqCst);
        }
        unsafe { System.alloc(layout) }
    }
    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        unsafe { System.dealloc(ptr, layout) }
    }
}

#[global_allocator]
static ALLOCATOR: Trace = Trace;

fn main() {
    let case = std::env::args().nth(1).unwrap();
    // Sol's four headers over a 4096-byte payload of float32 zeros.
    let shape = match case.as_str() {
        "overflow" => "18446744073709551615, 2",
        "short" => "64, 32",
        "long" => "16, 32",
        "valid" => "32, 32",
        _ => panic!("case"),
    };
    let header = format!("{{'descr': '<f4', 'fortran_order': False, 'shape': ({shape}), }}\n");
    let mut raw = b"\x93NUMPY\x01\x00".to_vec();
    raw.extend_from_slice(&(header.len() as u16).to_le_bytes());
    raw.extend_from_slice(header.as_bytes());
    raw.extend_from_slice(&[0u8; PAYLOAD]);
    ACTIVE.store(true, Ordering::SeqCst);
    let accepted = cmd_hdr::test_parse(&raw);
    ACTIVE.store(false, Ordering::SeqCst);
    println!("{case} accepted={accepted} payload_copies={}", COPIES.load(Ordering::SeqCst));
}
"""

CASES = {"overflow": False, "short": False, "long": False, "valid": True}


def build(tmp: Path) -> Path | None:
    """Build the driver from the current sources; ``None`` when cargo is not available."""
    cargo = shutil.which("cargo")
    if cargo is None:
        return None
    src = tmp / "hdr-cli-npy-order"
    (src / "src").mkdir(parents=True)
    (src / "Cargo.toml").write_text(CARGO_TOML.format(core=REPO / "core"))
    (src / "src" / "cmd_hdr_copy.rs").write_text((CLI_SRC / "cmd_hdr.rs").read_text(encoding="utf-8") + HOOK, encoding="utf-8")
    (src / "src" / "main.rs").write_text(MAIN_RS.replace("{cli}", str(CLI_SRC)), encoding="utf-8")
    cargo_home = os.environ.get("CARGO_HOME") or str(Path(pwd.getpwuid(os.getuid()).pw_dir) / ".cargo")
    env = {**os.environ, "CARGO_TARGET_DIR": str(tmp / "target"), "CARGO_HOME": cargo_home}
    proc = subprocess.run(
        [cargo, "build", "--release", "--offline", "--quiet", "--manifest-path", str(src / "Cargo.toml")],
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )
    if proc.returncode != 0:
        raise AssertionError(f"building the CLI order driver failed:\n{proc.stderr}")
    return tmp / "target" / "release" / "hdr-cli-npy-order"


def run(binary: Path, case: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(binary), case], capture_output=True, text=True, timeout=60)
