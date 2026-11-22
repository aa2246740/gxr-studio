"""Node 导入 .ts 的版本门槛。类型剥离从 Node 22.18 起可用。"""

from __future__ import annotations

import subprocess

PAIR_WGSL_PROBE = (
    "import{readFileSync as r}from 'node:fs';import{expandPairColorWgsl as e}from './frontend/src/pair_wgsl.ts';"
    "const c=r('frontend/src/shaders/common.wgsl','utf8'),p=r('frontend/src/shaders/pair_color.wgsl','utf8'),o=e(c,p);"
    "if(e(c,o)!==o)throw new Error('not idempotent');process.stdout.write(o);"
)


def node_can_strip_types(node: str) -> bool:
    out = subprocess.run([node, "-p", "process.versions.node"], capture_output=True, text=True, check=True).stdout
    major, minor = (int(part) for part in out.split(".")[:2])
    return (major, minor) >= (22, 18)
