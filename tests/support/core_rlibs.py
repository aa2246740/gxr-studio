"""Link external Rust probes against the exact release rlibs cargo built.

The OOM probes compile a small program with ``rustc`` and ``--extern`` it against
``gxr-finish`` and ``gxr-raw``. Picking ``libgxr_finish-*.rlib`` by newest mtime breaks
when one target dir holds rlibs from two compilers (for example Homebrew rustc and rustup
rustc of the same version): the newest file can come from the other compiler, and rustc
stops with E0514. So this helper runs ``cargo build --message-format=json`` with the same
toolchain as the ``rustc`` that links the probe, and takes the rlib paths from cargo's
``compiler-artifact`` messages.

It builds ``-p gxr-cli``, the same unit ``make core`` builds, and reads the artifacts of
its dependencies ``gxr-finish`` and ``gxr-raw``. Two reasons not to ask for
``-p gxr-finish -p gxr-raw`` directly: building the libraries as root packages produces
another set of rlibs (a different profile fingerprint) instead of reusing ``make core``'s
dependency artifacts; and for root packages
cargo reports the unhashed copy ``target/release/libgxr_raw.rlib``, which every root
build overwrites, instead of the hashed file in ``deps/``. When ``make core`` is up to
date, this cargo run does nothing, and the probe links the very rlibs ``gxr-cli`` linked.

A test skips when the core or the toolchain is missing, unless ``GXR_REQUIRE_CORE=1``,
in which case it fails.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

import pytest

from support.core_cli import CLI, REPO

CORE_MANIFEST = REPO / "core" / "Cargo.toml"
PROBE_PACKAGES = ("gxr-finish", "gxr-raw")
BUILD_PACKAGE = "gxr-cli"
CARGO_TIMEOUT_S = 900


def skip_or_fail(msg: str) -> NoReturn:
    """Skip, or fail under ``GXR_REQUIRE_CORE=1``."""
    if os.environ.get("GXR_REQUIRE_CORE") == "1":
        pytest.fail(msg)
    pytest.skip(msg)


def _real_home() -> Path:
    if os.name == "posix":
        import pwd

        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    return Path(os.environ.get("USERPROFILE") or Path.home())


def find_rustc() -> str:
    """The rustc of the channel in ``core/rust-toolchain.toml``, else the one on PATH."""
    toolchain_toml = REPO / "core" / "rust-toolchain.toml"
    channel = "stable"
    if toolchain_toml.is_file():
        m = re.search(r'channel\s*=\s*"([^"]+)"', toolchain_toml.read_text(encoding="utf-8"))
        if m:
            channel = m.group(1)
    exe = "rustc.exe" if os.name == "nt" else "rustc"
    rustup_home = Path(os.environ.get("RUSTUP_HOME") or (_real_home() / ".rustup"))
    toolchains_dir = rustup_home / "toolchains"
    if toolchains_dir.is_dir():
        for tc in sorted(toolchains_dir.iterdir()):
            if tc.name == channel or tc.name.startswith(f"{channel}-"):
                cand = tc / "bin" / exe
                if cand.is_file():
                    return str(cand)
    found = shutil.which("rustc")
    if found is not None:
        return str(found)
    skip_or_fail("rustc is not available for the OOM probe")


def find_cargo(rustc: str) -> str | None:
    """The cargo next to ``rustc`` (same toolchain), else the one on PATH."""
    sibling = Path(rustc).with_name("cargo.exe" if os.name == "nt" else "cargo")
    if sibling.is_file():
        return str(sibling)
    return shutil.which("cargo")


@dataclass(frozen=True)
class CoreRlibs:
    """Release rlibs of the probe packages, and the ``-L dependency=`` dir they live in."""

    rustc: str
    deps: Path
    externs: dict[str, Path]

    def link_args(self) -> list[str]:
        args = ["-L", f"dependency={self.deps}"]
        for crate, rlib in sorted(self.externs.items()):
            args += ["--extern", f"{crate}={rlib}"]
        return args


def rlibs_from_messages(lines: Iterable[str], packages: Sequence[str], core_dir: Path) -> dict[str, Path]:
    """Map crate name to rlib from cargo's JSON ``compiler-artifact`` lines.

    Only lib targets whose manifest is inside ``core_dir`` count, so a dependency that
    happens to share a name cannot stand in. Raises ``ValueError`` when a package is
    missing, has no rlib, or is reported twice with different rlibs.
    """
    wanted = {pkg.replace("-", "_") for pkg in packages}
    core_dir = core_dir.resolve()
    found: dict[str, Path] = {}
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        msg = json.loads(line)
        if msg.get("reason") != "compiler-artifact":
            continue
        target = msg.get("target") or {}
        crate = target.get("name")
        if crate not in wanted or "lib" not in (target.get("kind") or []):
            continue
        manifest = Path(msg.get("manifest_path") or "").resolve()
        if not manifest.is_relative_to(core_dir):
            continue
        rlibs = [Path(f) for f in msg.get("filenames") or [] if str(f).endswith(".rlib")]
        if len(rlibs) != 1:
            raise ValueError(f"cargo reported {len(rlibs)} rlibs for {crate}: {msg.get('filenames')}")
        if crate in found and found[crate] != rlibs[0]:
            raise ValueError(f"cargo reported two rlibs for {crate}: {found[crate]} and {rlibs[0]}")
        found[crate] = rlibs[0]
    missing = sorted(wanted - found.keys())
    if missing:
        raise ValueError(f"cargo reported no rlib for {', '.join(missing)}")
    return found


def deps_dir_of(externs: dict[str, Path]) -> Path:
    """The one ``deps`` directory all rlibs sit in; it becomes ``-L dependency=``."""
    parents = {rlib.parent for rlib in externs.values()}
    if len(parents) != 1:
        raise ValueError(f"rlibs are in more than one directory: {sorted(map(str, parents))}")
    deps = parents.pop()
    if deps.name != "deps":
        raise ValueError(f"rlibs are not in a cargo deps directory (uplifted copies?): {deps}")
    return deps


def release_rlibs(packages: Sequence[str] = PROBE_PACKAGES, build: str = BUILD_PACKAGE) -> CoreRlibs:
    """Build ``build`` (a no-op after ``make core``) and return the release rlibs of its dependencies ``packages``."""
    if not CLI.is_file():
        skip_or_fail(f"need {CLI}; run make core")
    rustc = find_rustc()
    cargo = find_cargo(rustc)
    if cargo is None:
        skip_or_fail("cargo is not available for the OOM probe")
    cmd = [cargo, "build", "--release", "--offline", "--locked", "--message-format=json", "--manifest-path", str(CORE_MANIFEST)]
    cmd += ["-p", build]
    cargo_home = os.environ.get("CARGO_HOME") or str(_real_home() / ".cargo")
    # RUSTC pins cargo to the compiler that will link the probe, whatever is first on PATH.
    env = {**os.environ, "RUSTC": rustc, "CARGO_HOME": cargo_home}
    proc = subprocess.run(cmd, env=env, check=False, capture_output=True, text=True, timeout=CARGO_TIMEOUT_S)
    if proc.returncode != 0:
        skip_or_fail(f"cargo build -p {build} failed (exit {proc.returncode}):\n{proc.stderr[-4000:]}")
    try:
        externs = rlibs_from_messages(proc.stdout.splitlines(), packages, REPO / "core")
        deps = deps_dir_of(externs)
    except ValueError as exc:
        skip_or_fail(f"cannot locate release rlibs from cargo output: {exc}")
    for rlib in externs.values():
        if not rlib.is_file():
            skip_or_fail(f"cargo reported {rlib}, but it does not exist")
    return CoreRlibs(rustc=rustc, deps=deps, externs=externs)


def compile_and_run_probe(tmp_path: Path, name: str, source: str) -> None:
    """Compile ``source`` against the release rlibs and run it; both steps must exit 0."""
    rlibs = release_rlibs()
    src_path = tmp_path / f"{name}.rs"
    bin_path = tmp_path / name
    src_path.write_text(source, encoding="utf-8")
    compile_proc = subprocess.run(
        [
            rlibs.rustc,
            "--edition=2024",
            "-C",
            "opt-level=2",
            "-C",
            "panic=abort",
            *rlibs.link_args(),
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
