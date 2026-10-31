#!/usr/bin/env python3
"""按内容拦下理光字节（设计 §5.6 第三道）：提交前检查暂存区，`make check` 检查仓库和分发包。

不只看文件名：改名、去掉后缀、塞进 zip/tar（含 zip 套 zip）、编码成长 hex/base64 都能拦住。规则：

- R1 `dat-head`：文件开头 8 字节是 `GXRCE001`；
- R2 `ricoh-magic`：二进制文件里含 `GXUI124\\0`、`GXUI140\\0`、`GXRRV201`，或 `GXDP` 后面紧跟版本字段
  （源码里的短字面量不算：文本文件只看开头和长编码串）；
- R3 `encoded-blob`：≥ 512 个字符的连续 hex 或 base64，解码后命中 R1/R2；
- R4 `registered-sha`：文件 SHA-256 等于已登记的理光文件（设计 §3.2；有 `src/gxr_studio/kit/releases.json` 时也读它）；
- R0 `unchecked`：没有完整检查到的内容，一律算失败，并写明哪个成员没查：归档或编码套得太深、成员太大、展开或解码
  总量超限、归档或压缩流读不出来或没有正常结束（gzip/bzip2/xz/zstd 每一段都要解完）、认得出但解不开的格式（7z、rar）。

R3 也认按行换行的编码（76 列 base64、64 列 hex，CRLF，含字符串里的 `\r\n`/`\n` 转义），解出来的内容再整个递归检查。
提交前检查读的是暂存区里的 blob（`git diff --cached --raw` 拿对象 id，`git cat-file --batch` 读字节），不读工作区的文件。

命中只打印路径和规则编号，不回显内容。用法::

    python scripts/private_bytes.py --staged      # 提交前：暂存区里新增或修改的 blob
    python scripts/private_bytes.py --tracked     # 仓库里已跟踪和未忽略的文件
    python scripts/private_bytes.py --dist        # uv build 出 sdist/wheel，逐个成员检查
    python scripts/private_bytes.py PATH...       # 指定文件
"""

from __future__ import annotations

import argparse
import base64
import binascii
import bz2
import hashlib
import io
import json
import lzma
import re
import subprocess
import sys
import tarfile
import tempfile
import zipfile
import zlib
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
RELEASES = ROOT / "src" / "gxr_studio" / "kit" / "releases.json"

# 设计 §3.2 登记过的理光文件（完整 SHA-256）。只有哈希，没有字节。
KNOWN_SHA256 = frozenset(
    {
        "78dcbb9a2735cd84e55385683ce0296b97818ddf90f8b492d9003512dbeff3f8",  # g137 发布 DAT
        "aab6c4f8d525dfda94fd33861c0a632b16f4d0df5481c7d9423e2bf820399e21",  # g139 发布候选
        "2378a88c5f963ff58bb27e099d5e8668d721f043ce77d3f958b02d8e47c80dd5",  # g139 当前测试包
        "40bdfe76482f0765f5f8f6be381d3f81862396010bb77b8fc3e09f975d16140a",  # 旧向量（内存生成）
        "9e03675d6e8a26a214816c773949b9eb5cf0c055c73821c91223cb0ac2ea69ce",  # diag147a
    }
)
MAGIC_HEAD = b"GXRCE001"
MAGIC_ANY = re.compile(rb"GXUI124\x00|GXUI140\x00|GXRRV201|GXDP\x00[\x01-\x0f]\x00\x40")
HEX_RUN = re.compile(rb"[0-9A-Fa-f]{512,}")
B64_RUN = re.compile(rb"[A-Za-z0-9+/_-]{512,}={0,2}")
# 换行编码：一行里只有一段 base64/hex 字符（可以带引号、逗号），连起来够长就当成一整块解码
SEGMENT = re.compile(rb"[A-Za-z0-9+/_=-]{8,}")
LINE_NOISE = b" \t\"',\\"
MIN_ENCODED = 512
MAX_DEPTH = 4
MAX_MEMBER_BYTES = 256 << 20
MAX_EXPANDED = 1 << 30  # 一个文件里所有归档展开、编码解码的总量
MAX_DECODED = 4 << 20  # 一段编码串解码后的上限；超过就报"未检查"，不截掉尾部
# 认得出、但这里解不开的容器：一律报"未检查"
UNSUPPORTED = ((b"7z\xbc\xaf\x27\x1c", "7z archive"), (b"Rar!\x1a\x07", "rar archive"))


class Unchecked(Exception):
    """这部分内容没能完整检查（读不完、解不完、超过上限）。调用方报 R0 并让检查失败。"""


class Budget:
    """一个顶层文件的展开总量；超过就停下，并把没查的部分报成"未检查"。"""

    def __init__(self, limit: int) -> None:
        self.left = limit

    def take(self, n: int) -> bool:
        self.left -= n
        return self.left >= 0


def known_shas() -> frozenset[str]:
    out = set(KNOWN_SHA256)
    try:
        data = json.loads(RELEASES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset(out)
    for e in data.get("entries", []) if isinstance(data, dict) else []:
        for k in ("file_sha256", "template_sha256"):
            v = e.get(k) if isinstance(e, dict) else None
            if isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v):
                out.add(v)
    return frozenset(out)


def _is_binary(data: bytes) -> bool:
    if b"\0" in data[:8192]:
        return True
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def _magic(data: bytes, binary: bool) -> str | None:
    if data[:8] == MAGIC_HEAD:
        return "R1 dat-head"
    if binary and MAGIC_ANY.search(data):
        return "R2 ricoh-magic"
    return None


def _decode_candidates(run: bytes) -> Iterator[bytes]:
    """一段编码串的全部可能解码（hex、两种 base64 字母表）。解码后超过 MAX_DECODED：抛 Unchecked，不只看前缀。"""
    if re.fullmatch(rb"[0-9A-Fa-f]+", run):
        if len(run) // 2 > MAX_DECODED:
            raise Unchecked("encoded block too large")
        try:
            yield binascii.unhexlify(run[: len(run) // 2 * 2])
        except binascii.Error:
            pass
    b64 = run.replace(b"=", b"")
    if len(b64) * 3 // 4 > MAX_DECODED:
        raise Unchecked("encoded block too large")
    b64 += b"=" * (-len(b64) % 4)
    for alt in (b"+/", b"-_"):
        try:
            yield base64.b64decode(b64, altchars=alt, validate=False)
        except (binascii.Error, ValueError):
            continue


def _normalize_newlines(data: bytes) -> bytes:
    """CRLF、单独的 CR，以及字符串里的 `\r\n`、`\n`、`\r` 转义，都算一个换行。"""
    for old in (b"\\r\\n", b"\\n", b"\\r", b"\r\n", b"\r"):
        data = data.replace(old, b"\n")
    return data


def _wrapped_blocks(data: bytes) -> Iterator[bytes]:
    """按行换行的 base64/hex（76 列 base64、64 列 hex，含 JSON/源码字符串里的换行转义）连成一整块。
    一行里只有编码字符的算块的中间；块的第一行可以带前缀（`"blob": "`），最后一行可以带后缀。"""
    block: list[bytes] = []

    def flush() -> Iterator[bytes]:
        joined = b"".join(block)
        block.clear()
        if len(joined) >= MIN_ENCODED:
            yield joined

    for line in _normalize_newlines(data).split(b"\n"):
        core = line.strip(LINE_NOISE)
        if core and SEGMENT.fullmatch(core):
            block.append(core)
            continue
        lead = SEGMENT.match(line.lstrip(LINE_NOISE))
        if lead and block:
            block.append(lead.group(0))
        yield from flush()
        tail = [m for m in SEGMENT.finditer(line) if not line[m.end() :].strip(LINE_NOISE + b"}]);")]
        if tail:
            block.append(tail[-1].group(0))
    yield from flush()


def _encoded_runs(data: bytes) -> Iterator[bytes]:
    for m in HEX_RUN.finditer(data):
        yield m.group(0)
    for m in B64_RUN.finditer(data):
        yield m.group(0).rstrip(b"=")
    yield from _wrapped_blocks(data)


Member = tuple[str, bytes | None, str]  # (成员名, 字节或 None, 没查的原因)


def _zip_members(data: bytes, budget: Budget) -> Iterator[Member]:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        infos = zf.infolist()
    except (zipfile.BadZipFile, OSError, ValueError, EOFError, NotImplementedError):
        yield "<zip>", None, "unreadable archive"
        return
    for i in infos:
        if i.is_dir():
            continue
        if i.file_size > MAX_MEMBER_BYTES:
            yield i.filename, None, "member too large"
            continue
        if not budget.take(i.file_size):
            yield i.filename, None, "archive expands too much"
            continue
        try:
            body = zf.read(i)
        except (zipfile.BadZipFile, RuntimeError, OSError, ValueError, NotImplementedError, EOFError):
            yield i.filename, None, "unreadable member"
            continue
        yield (i.filename, body, "") if len(body) == i.file_size else (i.filename, None, "incomplete member")
    yield from _zip_unlisted(data, zf, infos)


def _zip_unlisted(data: bytes, zf: zipfile.ZipFile, infos: list[zipfile.ZipInfo]) -> Iterator[Member]:
    """zip 里没有被任何成员（本地头 + 数据 + 数据描述符）、中央目录或结束记录占用的字节：在第一个成员前、成员之间、
    结束记录之后夹带的内容，解压成员时看不到。这些区间单独当成员检查；算不清布局就报未检查（复审 A1a R3-P1-02）。"""
    used: list[tuple[int, int]] = []
    for i in infos:
        at = i.header_offset
        if data[at : at + 4] != b"PK\x03\x04" or len(data) < at + 30:
            yield f"<zip@{at}>", None, "unreadable member layout"
            return
        end = at + 30 + int.from_bytes(data[at + 26 : at + 28], "little") + int.from_bytes(data[at + 28 : at + 30], "little")
        end += i.compress_size
        if i.flag_bits & 0x08:  # 数据描述符：可选签名 + CRC + 两个长度
            end += 16 if data[end : end + 4] == b"PK\x07\x08" else 12
        used.append((at, end))
    eocd = data.rfind(b"PK\x05\x06")
    start_dir = getattr(zf, "start_dir", None)
    if eocd < 0 or not isinstance(start_dir, int) or len(data) < eocd + 22:
        yield "<zip>", None, "unreadable archive layout"
        return
    used.append((start_dir, eocd + 22 + int.from_bytes(data[eocd + 20 : eocd + 22], "little")))
    pos = 0
    for start, end in sorted(used):
        if start > pos and data[pos:start].strip(b"\0"):
            yield f"<zip-unlisted@{pos}>", data[pos:start], ""
        pos = max(pos, end)
    if data[pos:].strip(b"\0"):
        yield f"<zip-unlisted@{pos}>", data[pos:], ""


def _decompressor(kind: str) -> Any:
    if kind == "gzip":
        return zlib.decompressobj(16 + zlib.MAX_WBITS)
    if kind == "bzip2":
        return bz2.BZ2Decompressor()
    if kind == "xz":
        return lzma.LZMADecompressor(format=lzma.FORMAT_XZ)
    from compression import zstd  # Python 3.14 起自带

    return zstd.ZstdDecompressor()


STREAMS = ((b"\x1f\x8b", "gzip"), (b"BZh", "bzip2"), (b"\xfd7zXZ\x00", "xz"), (b"\x28\xb5\x2f\xfd", "zstd"))


def _decompress_all(data: bytes, kind: str, budget: Budget) -> list[bytes]:
    """把连接在一起的每一段都解完（gzip 多段、bzip2/xz/zstd 多流），每段都必须正常结束；返回每一段解出的内容。
    读不完、段没有结束、解压结果超过上限，都抛 Unchecked。"""
    parts: list[bytes] = []
    total = 0
    rest = data
    while True:
        d = _decompressor(kind)
        try:
            part = d.decompress(rest, max_length=MAX_MEMBER_BYTES + 1 - total)
        except (zlib.error, OSError, EOFError, ValueError, lzma.LZMAError, ImportError) as exc:
            raise Unchecked(f"unreadable {kind} stream") from exc
        total += len(part)
        if total > MAX_MEMBER_BYTES:
            raise Unchecked(f"{kind} content too large")
        if not d.eof:
            raise Unchecked(f"unfinished {kind} stream")
        parts.append(part)
        rest = d.unused_data
        if not rest.strip(b"\0"):  # 段尾只允许补零
            break
    if not budget.take(total):
        raise Unchecked("archive expands too much")
    return parts


def _tar_members(data: bytes, budget: Budget) -> Iterator[Member]:
    try:
        tf = tarfile.open(fileobj=io.BytesIO(data))
        members = tf.getmembers()
    except (tarfile.TarError, OSError, EOFError, ValueError):
        yield "<tar>", None, "unreadable archive"
        return
    for m in members:
        if not m.isfile():
            continue
        if m.size > MAX_MEMBER_BYTES:
            yield m.name, None, "member too large"
            continue
        if not budget.take(m.size):
            yield m.name, None, "archive expands too much"
            continue
        try:
            fh = tf.extractfile(m)
            body = fh.read() if fh is not None else None
        except (tarfile.TarError, OSError, EOFError, ValueError):
            body = None
        yield (m.name, body, "") if body is not None and len(body) == m.size else (m.name, None, "incomplete member")
    # tar 结束标记之后（或读到无效头停下之后）还有非零字节：例如 `cat notes.tar camera.dat`、两个 tar 首尾相接。
    # 解 tar 时看不到它们，单独当成员检查（复审 A1a R3-P1-02）
    rest = data[tf.offset :].lstrip(b"\0")
    if rest:
        yield f"<after-tar@{len(data) - len(rest)}>", rest, ""


def _archive_members(data: bytes, budget: Budget) -> Iterator[Member] | None:
    """zip / tar / gzip / bzip2 / xz / zstd 就逐个给出成员（只在内存里读，不解到磁盘）；不是归档返回 None。
    压缩流的每一段、tar 结束之后的字节、zip 里没有成员占用的字节，都作为成员给出，交给调用方完整检查。
    读不出来、读不完、成员太大、展开总量超限、解不开的格式，给出 (名字, None, 原因)，调用方报成"未检查"。"""
    if data[:4] == b"PK\x03\x04":
        return _zip_members(data, budget)
    for magic, kind in STREAMS:
        if data.startswith(magic):
            try:
                parts = _decompress_all(data, kind, budget)
            except Unchecked as exc:
                return iter([(f"<{kind}>", None, str(exc))])
            if len(parts) == 1:
                return iter([(f"<{kind}>", parts[0], "")])
            # 多段：每一段都单独完整检查（首段是 tar/zip 也不提前返回，后面的段照查），再查一次连起来的内容
            members: list[Member] = [(f"<{kind}:{i + 1}>", part, "") for i, part in enumerate(parts)]
            members.append((f"<{kind}:joined>", b"".join(parts), ""))
            return iter(members)
    for magic, kind in UNSUPPORTED:
        if data.startswith(magic):
            return iter([(f"<{kind}>", None, "unsupported archive format")])
    if len(data) > 262 and data[257:262] == b"ustar":
        return _tar_members(data, budget)
    if _prefixed_zip(data):
        # 前面带着别的字节的 zip（例如自解压脚本）：照样按 zip 检查成员，前面那段作为"没有成员占用的字节"一起检查
        return _zip_members(data, budget)
    return None


def _prefixed_zip(data: bytes) -> bool:
    """开头不是本地头、但末尾有 zip 结束记录，而且 zipfile 真的能打开、有成员：说明是前面带了别的字节的 zip。"""
    if b"PK\x05\x06" not in data[-(0xFFFF + 22) :] or b"PK\x03\x04" not in data:
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            return bool(zf.infolist())
    except (zipfile.BadZipFile, OSError, ValueError, EOFError, NotImplementedError):
        return False


def scan_bytes(
    name: str, data: bytes, shas: frozenset[str] | None = None, depth: int = 0, budget: Budget | None = None
) -> list[tuple[str, str]]:
    """-> [(路径, 规则)]。归档里的成员写成 `归档!成员`，编码串解出来的内容写成 `名字!<encoded>`。
    任何没有完整检查到的内容都报 `R0 unchecked`，不当作没有命中。"""
    shas = known_shas() if shas is None else shas
    budget = budget or Budget(MAX_EXPANDED)
    hits: list[tuple[str, str]] = []
    if hashlib.sha256(data).hexdigest() in shas:
        hits.append((name, "R4 registered-sha"))
    members = _archive_members(data, budget)
    if members is None:
        binary = _is_binary(data)
        rule = _magic(data, binary)
        if rule:
            hits.append((name, rule))
        else:
            hits += _scan_encoded(name, data, shas, depth, budget)
        return hits
    # 归档/压缩容器：内容检查做在成员上（加上压缩流的每一段、tar 结束之后、zip 里没有成员占用的字节），
    # 这些合起来覆盖容器里的全部内容；容器自身的原始字节（压缩数据、tar 头）不按"二进制里含魔数"去匹配
    if depth >= MAX_DEPTH:
        hits.append((name, "R0 unchecked: archive nested too deep"))
        return hits
    for member, body, why in members:
        if body is None:
            hits.append((f"{name}!{member}", f"R0 unchecked: {why}"))
        else:
            hits += scan_bytes(f"{name}!{member}", body, shas, depth + 1, budget)
    return hits


def _scan_encoded(name: str, data: bytes, shas: frozenset[str], depth: int, budget: Budget) -> list[tuple[str, str]]:
    """每段编码串解码后：直接核对魔数和登记 SHA，再整个递归检查（编码里套归档、套编码）。"""
    seen: set[str] = set()
    try:
        for run in _encoded_runs(data):
            for decoded in _decode_candidates(run):
                digest = hashlib.sha256(decoded).hexdigest()
                if digest in seen:
                    continue
                seen.add(digest)
                if _magic(decoded, True) or digest in shas:
                    return [(name, "R3 encoded-blob")]
                if not budget.take(len(decoded)):
                    return [(name, "R0 unchecked: encoded content expands too much")]
                if depth >= MAX_DEPTH:
                    if _archive_members(decoded, Budget(0)) is not None or next(_encoded_runs(decoded), None) is not None:
                        return [(name, "R0 unchecked: encoded content nested too deep")]
                    continue
                inner = scan_bytes(f"{name}!<encoded>", decoded, shas, depth + 1, budget)
                if inner:
                    return inner
    except Unchecked as exc:
        return [(name, f"R0 unchecked: {exc}")]
    return []


def scan_paths(paths: list[Path], shas: frozenset[str] | None = None) -> list[tuple[str, str]]:
    shas = known_shas() if shas is None else shas
    hits: list[tuple[str, str]] = []
    for p in paths:
        try:
            data = p.read_bytes()
        except (IsADirectoryError, FileNotFoundError):
            continue
        hits += scan_bytes(_display(p), data, shas)
    return hits


def _display(p: Path) -> str:
    try:
        return p.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(p)


def _git(*args: str) -> list[Path]:
    out = subprocess.check_output(["git", *args], cwd=ROOT)  # nosec B603 B607  固定的只读 git 子命令
    return [ROOT / part.decode() for part in out.split(b"\0") if part]


def staged_entries() -> list[tuple[str, str]]:
    """暂存区里新增或修改的文件：(路径, 暂存 blob 的对象 id)。提交的是这个 blob，不是工作区里的文件。"""
    out = subprocess.check_output(  # nosec B603 B607  固定的只读 git 子命令
        ["git", "diff", "--cached", "--raw", "-z", "--no-abbrev", "--diff-filter=ACMR"], cwd=ROOT
    )
    parts = out.split(b"\0")
    entries: list[tuple[str, str]] = []
    i = 0
    while i < len(parts):
        meta = parts[i]
        if not meta.startswith(b":"):
            i += 1
            continue
        _old_mode, new_mode, _old_id, new_id, status = meta[1:].decode().split()[:5]
        step = 3 if status[:1] in ("R", "C") else 2
        path = parts[i + step - 1].decode()
        i += step
        if new_mode != "160000":  # 子模块没有 blob
            entries.append((path, new_id))
    return entries


def read_blobs(oids: list[str]) -> dict[str, bytes]:
    """用只读的 `git cat-file --batch` 按对象 id 读 blob 的字节。"""
    unique = list(dict.fromkeys(oids))
    if not unique:
        return {}
    res = subprocess.run(  # nosec B603 B607  固定的只读 git 子命令，对象 id 来自 git 自己的输出
        ["git", "cat-file", "--batch"], input="".join(f"{o}\n" for o in unique).encode(), capture_output=True, check=True, cwd=ROOT, timeout=600
    )
    out, at, blobs = res.stdout, 0, {}
    for oid in unique:
        nl = out.index(b"\n", at)
        header = out[at:nl].decode().split()
        if len(header) != 3 or header[0] != oid or header[1] != "blob":
            raise RuntimeError(f"git cat-file 没有返回 blob {oid}")
        size = int(header[2])
        blobs[oid] = out[nl + 1 : nl + 1 + size]
        at = nl + 1 + size + 1
    return blobs


def scan_staged(shas: frozenset[str] | None = None) -> list[tuple[str, str]]:
    shas = known_shas() if shas is None else shas
    entries = staged_entries()
    blobs = read_blobs([oid for _, oid in entries])
    hits: list[tuple[str, str]] = []
    for path, oid in entries:
        hits += scan_bytes(f"staged:{path}", blobs[oid], shas)
    return hits


def repository_files() -> list[Path]:
    return [p for p in _git("ls-files", "-z", "--cached", "--others", "--exclude-standard") if p.is_file()]


def dist_files(out_dir: Path) -> list[Path]:
    """uv build 出 sdist 和 wheel（离线，用本机缓存的构建后端）。"""
    subprocess.run(  # nosec B603 B607  固定命令，输出目录是临时目录
        ["uv", "build", "--offline", "--quiet", "--out-dir", str(out_dir)], cwd=ROOT, check=True, timeout=600
    )
    return sorted(p for p in out_dir.iterdir() if p.is_file())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="按内容拦下理光字节")
    ap.add_argument("--staged", action="store_true")
    ap.add_argument("--tracked", action="store_true")
    ap.add_argument("--dist", action="store_true")
    ap.add_argument("paths", nargs="*", type=Path)
    args = ap.parse_args(argv)
    hits: list[tuple[str, str]] = []
    count = 0
    if args.staged:
        count += len(staged_entries())
        hits += scan_staged()
    if args.tracked:
        files = repository_files()
        count += len(files)
        hits += scan_paths(files)
    if args.paths:
        count += len(args.paths)
        hits += scan_paths(list(args.paths))
    if args.dist:
        with tempfile.TemporaryDirectory(prefix="gxr-dist-") as tmp:
            files = dist_files(Path(tmp))
            count += len(files)
            shas = known_shas()
            for f in files:
                hits += scan_bytes(f"dist/{f.name}", f.read_bytes(), shas)
    for name, rule in hits:
        print(f"{name}: {rule}")
    if hits:
        print("发现疑似理光字节。请把这些文件移出仓库（真实文件只放在本机外部路径）。", file=sys.stderr)
        return 1
    print(f"private bytes: checked {count} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
