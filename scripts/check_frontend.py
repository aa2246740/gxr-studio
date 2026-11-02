#!/usr/bin/env python3
"""前端构建产物与源码一致（frontend/ → web/assets/gpu/）。

产物提交进仓库，双击启动工作室不需要 Node。这里重新构建到临时目录，逐字节比较；再跑一遍 TypeScript 类型检查。
没有 node/pnpm 或没装 frontend 依赖时跳过并说明；`--require`（或环境变量 GXR_REQUIRE_FRONTEND=1，CI 用）时这些情况算失败。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
OUT = ROOT / "web" / "assets" / "gpu"


def _run(cmd: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=FRONTEND, capture_output=True, text=True, env=env, timeout=600)  # noqa: S603


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--require", action="store_true")
    args = ap.parse_args()
    require = args.require or os.environ.get("GXR_REQUIRE_FRONTEND") == "1"
    pnpm = shutil.which("pnpm")
    if pnpm is None or shutil.which("node") is None or not (FRONTEND / "node_modules").is_dir():
        msg = "跳过前端构建核对：需要 node、pnpm 和 `pnpm -C frontend install --frozen-lockfile`"
        print(msg)
        return 1 if require else 0
    typecheck = _run([pnpm, "run", "-s", "typecheck"])
    if typecheck.returncode != 0:
        print(typecheck.stdout + typecheck.stderr)
        print("TypeScript 类型检查没有通过")
        return 1
    with tempfile.TemporaryDirectory() as tmp:
        build = _run([pnpm, "run", "-s", "build"], env={**os.environ, "GXR_GPU_OUT": tmp})
        if build.returncode != 0:
            print(build.stdout + build.stderr)
            return 1
        fresh = {p.relative_to(tmp): p.read_bytes() for p in Path(tmp).rglob("*") if p.is_file()}
    committed = {p.relative_to(OUT): p.read_bytes() for p in OUT.rglob("*") if p.is_file()} if OUT.is_dir() else {}
    if fresh != committed:
        changed = sorted(str(k) for k in set(fresh) | set(committed) if fresh.get(k) != committed.get(k))
        print("web/assets/gpu/ 和 frontend/ 的源码对不上，请运行 `make web` 后提交：" + ", ".join(changed))
        return 1
    print(f"前端构建产物与源码一致（{len(fresh)} 个文件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
