"""The OOM probes link the rlibs cargo built, not the newest file in ``target/release/deps``.

Regression for the hdr-track merge gate: the target dir held ``libgxr_finish-*.rlib`` from
two rustc builds of the same version, the probes picked the newest by mtime, and rustc
stopped with E0514. Here a newer, deliberately broken decoy sits next to the real rlib.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from support.core_cli import REPO
from support.core_rlibs import compile_and_run_probe, deps_dir_of, release_rlibs, rlibs_from_messages
from support.geometry_alloc import run_rule2_oom_probe

CORE = REPO / "core"

_LINK_PROBE_RS = """fn main() {
    let err = gxr_raw::CoreError { kind: "k", message: "m".to_string() };
    assert_eq!(err.kind, "k");
    let _ = gxr_finish::geometry::bounds::ARRAY_TOO_BIG;
}
"""


def _artifact(name: str, manifest: Path, filenames: list[str], kind: str = "lib") -> str:
    return json.dumps(
        {
            "reason": "compiler-artifact",
            "manifest_path": str(manifest),
            "target": {"name": name, "kind": [kind]},
            "filenames": filenames,
        }
    )


def test_rlibs_from_messages_reads_compiler_artifacts(tmp_path: Path) -> None:
    deps = tmp_path / "deps"
    lines = [
        "   Compiling noise",
        json.dumps({"reason": "build-script-executed", "package_id": "x"}),
        _artifact("gxr_raw", tmp_path / "elsewhere" / "Cargo.toml", [str(deps / "libgxr_raw-0bad.rlib")]),
        _artifact(
            "gxr_raw", CORE / "crates" / "gxr-raw" / "Cargo.toml", [str(deps / "libgxr_raw-aaaa.rlib"), str(deps / "libgxr_raw-aaaa.rmeta")]
        ),
        _artifact("gxr_finish", CORE / "crates" / "gxr-finish" / "Cargo.toml", [str(deps / "gxr_finish-bin")], kind="bin"),
        _artifact("gxr_finish", CORE / "crates" / "gxr-finish" / "Cargo.toml", [str(deps / "libgxr_finish-bbbb.rlib")]),
        json.dumps({"reason": "build-finished", "success": True}),
    ]
    got = rlibs_from_messages(lines, ["gxr-finish", "gxr-raw"], CORE)
    assert got == {"gxr_finish": deps / "libgxr_finish-bbbb.rlib", "gxr_raw": deps / "libgxr_raw-aaaa.rlib"}
    assert deps_dir_of(got) == deps


def test_rlibs_from_messages_rejects_missing_or_ambiguous(tmp_path: Path) -> None:
    raw = _artifact("gxr_raw", CORE / "crates" / "gxr-raw" / "Cargo.toml", [str(tmp_path / "libgxr_raw-a.rlib")])
    with pytest.raises(ValueError, match="gxr_finish"):
        rlibs_from_messages([raw], ["gxr-finish", "gxr-raw"], CORE)
    other = _artifact("gxr_raw", CORE / "crates" / "gxr-raw" / "Cargo.toml", [str(tmp_path / "libgxr_raw-b.rlib")])
    with pytest.raises(ValueError, match="two rlibs"):
        rlibs_from_messages([raw, other], ["gxr-raw"], CORE)
    with pytest.raises(ValueError, match="more than one directory"):
        deps_dir_of({"a": tmp_path / "x" / "liba.rlib", "b": tmp_path / "y" / "libb.rlib"})
    # Root packages are reported as the unhashed copy in target/release; never link that.
    with pytest.raises(ValueError, match="deps directory"):
        deps_dir_of({"gxr_raw": tmp_path / "release" / "libgxr_raw.rlib"})


def test_newer_decoy_rlib_is_not_linked(tmp_path: Path) -> None:
    real = release_rlibs()
    real_finish = real.externs["gxr_finish"]
    decoy = real.deps / f"libgxr_finish-dec0y{os.getpid():08x}.rlib"
    assert not decoy.exists()
    try:
        # Not an rlib at all: rustc fails on it, like an rlib from an incompatible compiler.
        decoy.write_bytes(b"decoy: not an rlib from this toolchain\n")
        future = time.time() + 3600
        os.utime(decoy, (future, future))

        # Negative control: the old newest-by-mtime choice lands on the decoy, and linking it fails.
        newest = max(real.deps.glob("libgxr_finish-*.rlib"), key=lambda p: p.stat().st_mtime)
        assert newest == decoy
        src = tmp_path / "decoy_link.rs"
        src.write_text(_LINK_PROBE_RS, encoding="utf-8")
        bad = subprocess.run(
            [
                real.rustc,
                "--edition=2024",
                "-L",
                f"dependency={real.deps}",
                "--extern",
                f"gxr_finish={decoy}",
                "--extern",
                f"gxr_raw={real.externs['gxr_raw']}",
                str(src),
                "-o",
                str(tmp_path / "decoy_link"),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert bad.returncode != 0, "linking the decoy should fail"

        again = release_rlibs()
        assert again.externs == real.externs
        assert again.externs["gxr_finish"] == real_finish != decoy
        compile_and_run_probe(tmp_path, "decoy_link_ok", _LINK_PROBE_RS)
        run_rule2_oom_probe(tmp_path)
    finally:
        decoy.unlink(missing_ok=True)
    assert not decoy.exists()
