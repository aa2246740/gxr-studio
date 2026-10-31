"""在子进程层模拟 git 的输出，测试提交前检查（scripts/private_bytes.py --staged）。

只回答检查会发出的两类只读命令：`git diff --cached`（暂存区里的路径和 blob 对象 id）和 `git cat-file --batch`
（按对象 id 读字节）。不碰任何真实的 git 索引，也不做 git 写操作。
"""

from __future__ import annotations

import hashlib
import subprocess
from types import SimpleNamespace


class FakeGit:
    """只模拟 git 的输出：暂存区里 `staged` 的内容和工作区不同（或工作区已经没有这个文件）。不碰任何真实的 git 索引。"""

    def __init__(self, staged: dict[str, bytes]):
        self.staged = {name: (hashlib.sha1(b"blob %d\0" % len(d) + d).hexdigest(), d) for name, d in staged.items()}  # noqa: S324
        self.calls: list[list[str]] = []

    def install(self, monkeypatch, pb) -> None:
        fake_mod = SimpleNamespace(check_output=self.check_output, run=self.run, CalledProcessError=subprocess.CalledProcessError)
        monkeypatch.setattr(pb, "subprocess", fake_mod)

    def check_output(self, args, cwd=None, **kw):
        self.calls.append(list(args))
        if args[:3] == ["git", "diff", "--cached"]:
            if "--name-only" in args:
                return b"".join(n.encode() + b"\0" for n in self.staged)
            out = b""
            for name, (oid, _) in self.staged.items():
                out += f":100644 100644 {'0' * 40} {oid} M".encode() + b"\0" + name.encode() + b"\0"
            return out
        raise AssertionError(f"unexpected git call {args}")

    def run(self, args, input=None, capture_output=False, check=False, cwd=None, timeout=None, **kw):  # noqa: A002
        self.calls.append(list(args))
        if args[:3] == ["git", "cat-file", "--batch"]:
            by_id = {oid: d for oid, d in self.staged.values()}
            out = b""
            for line in input.decode().split():
                d = by_id[line]
                out += f"{line} blob {len(d)}\n".encode() + d + b"\n"
            return subprocess.CompletedProcess(args, 0, out, b"")
        return subprocess.run(args, input=input, capture_output=capture_output, check=check, cwd=cwd, timeout=timeout, **kw)
