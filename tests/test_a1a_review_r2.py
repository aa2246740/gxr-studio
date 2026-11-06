"""A1a 代码复审第 2 轮（research/review_studio_a1a_r2/REVIEW.md）的回归用例：按类写，不只覆盖复审给出的那几个例子。

- R2-P1-01 图库里任何写入都按真实解析后的位置判断，经链接进入卡根就拒绝；卡上的文件不算图库副本；审计记真实路径；
- R2-P1-02 图库内容证明绑定读字节那一刻的文件状态；读的时候在变就算未知；
- R2-P1-03 私有目录的每次创建、写入、改名、读取、删除都核对最终用的父目录 fd；审计上下文保留 fd；
- R2-P1-04 私有字节检查：任何没有完整检查到的输入一律 R0 并失败；
- R2-P1-05 渲染和换算的计算结果必须有限；
- R2-P2-01 首个导入请求失败后能重试；R2-P2-02 旧的 `_done_message` 名字还能导入。

全部是现场生成的合成卡、合成 DNG、合成"理光字节"；不碰真实卡、用户图库或应用数据，不做 git 写操作。
"""

from __future__ import annotations

import base64
import bz2
import gzip
import importlib.util
import io
import json
import lzma
import os
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

import numpy as np
import pytest

from gxr_studio import audit, osplat, privacy
from gxr_studio.cards.coverage import Identity, photo_coverage
from gxr_studio.cards.discovery import find_cards
from gxr_studio.cards.importer import DONE_ALL, LAYOUT_REFUSED, ImportOptions, run_import
from gxr_studio.devtools.synthetic_card import tree_snapshot
from gxr_studio.library import store as store_mod
from support.fake_card import fake_dat, make_fake_card

STUDIO = Path(__file__).resolve().parents[1]
ASSETS = STUDIO / "web" / "assets"
OPTS = dict(min_free_bytes=0, make_thumbs=False)


def one(volumes: Path):
    (c,) = find_cards([volumes])
    return c


def _move_into_card(lib, rel_dir: str, card_root: Path) -> None:
    """把图库里的一个目录换成指向卡上 `reserve/` 的链接（原有内容搬过去）。"""
    audit.unprotect(card_root)  # 这里是在布置测试场景：发现卡时登记的写保护先放开
    reserve = card_root / "reserve"
    reserve.mkdir(exist_ok=True)
    src = lib.root / rel_dir
    if src.exists():
        for item in src.iterdir():
            shutil.move(str(item), reserve / item.name)
        src.rmdir()
    src.parent.mkdir(parents=True, exist_ok=True)
    src.symlink_to(reserve, target_is_directory=True)


# ---- R2-P1-01：图库路径经过链接指到卡 ---------------------------------------------------------------


@pytest.mark.parametrize("kind", ["photo", "video"])
@pytest.mark.parametrize("where", ["year", "day", "file_link"])
def test_r2_library_writes_resolve_real_location(volumes, lib, kind, where):
    """年目录、日期目录被换成指到卡的链接，或者图库里的文件本身是指到卡上原件的链接：
    新导入和修复都不写卡（整棵卡目录、含目录 mtime 都不变），也不把卡上的文件当成图库副本，不说"都已导入"。"""
    card = make_fake_card(volumes, pairs=0, jpg_only=1 if kind == "photo" else 0, videos=1 if kind == "video" else 0)
    c = one(volumes)
    table = "files" if kind == "photo" else "videos"
    if where == "file_link":
        run_import(lib, c, ImportOptions(**OPTS))
        rel = lib.db.execute(f"SELECT relpath FROM {table}").fetchone()[0]  # noqa: S608
        (lib.root / rel).unlink()
        src_rel = next(iter(card.files))
        (lib.root / rel).symlink_to(card.path(src_rel))  # 图库"副本"其实是卡上的原件
    elif where == "year":
        _move_into_card(lib, "2026", card.root)
    else:
        (lib.root / "2026").mkdir()
        (lib.root / "2026" / "2026-10-20").symlink_to(card.root / "DCIM" / "100RICOH", target_is_directory=True)
    audit.protect(card.root)
    before = tree_snapshot(card.root)
    with audit.AuditRecorder() as rec:
        p = run_import(lib, c, ImportOptions(**OPTS))
    assert tree_snapshot(card.root) == before, "卡上零写入（含 utime、mkdir、改名、编辑记录）"
    assert rec.writes_under(card.root) == [], "没有一次写入落到（或试图落到）卡上"
    assert p.state == "done" and p.coverage["all_imported"] is False and p.message != DONE_ALL
    assert p.write_refused == list(card.files), p
    # 用户要求：明确说有文件没导入、请勿格式化，不能说"可以推出"
    assert p.message == f"有 {len(card.files)} 个文件未能导入。请勿格式化存储卡，重新插入后再试一次。", p.message
    assert "推出" not in p.message


def test_r2_studio_dir_linked_into_card_refuses_whole_run(volumes, lib):
    """`.studio`（暂存区、索引、日志）被换成指到卡的链接：整次导入不写一个字节，明确失败。"""
    card = make_fake_card(volumes, pairs=0, jpg_only=1)
    c = one(volumes)
    lib.close()
    _move_into_card(lib, ".studio", card.root)
    audit.protect(card.root)
    before = tree_snapshot(card.root)
    p = run_import(lib, c, ImportOptions(**OPTS))
    assert p.state == "failed" and p.message == LAYOUT_REFUSED
    assert tree_snapshot(card.root) == before


def test_r2_audit_records_and_blocks_the_real_location(tmp_path, volumes):
    """审计按真实路径判断和记录：经过链接别名往卡里写（打开、建目录、改名、改时间、编辑记录）都被拦下，记的是卡上的位置。"""
    card = make_fake_card(volumes)
    alias = tmp_path / "alias"
    alias.symlink_to(card.root / "DCIM", target_is_directory=True)
    audit.protect(card.root)
    before = tree_snapshot(card.root)
    target = card.root / "DCIM" / "100RICOH" / "R0010001.JPG"
    with audit.AuditRecorder() as rec:
        with pytest.raises(PermissionError):
            open(alias / "new.bin", "wb").close()
        with pytest.raises(PermissionError):
            os.mkdir(alias / "sub")
        with pytest.raises(PermissionError):
            os.utime(alias / "100RICOH" / "R0010001.JPG", ns=(1, 1))
        with pytest.raises(PermissionError):
            os.rename(alias / "100RICOH" / "R0010001.JPG", alias / "x.JPG")
        with pytest.raises(PermissionError):
            shutil.copyfile(card.path("DCIM/100RICOH/R0010001.JPG"), alias / "100RICOH" / "copy.JPG")
    assert tree_snapshot(card.root) == before and target.exists()
    recorded = rec.writes_under(card.root)
    assert recorded and all(w.blocked for w in recorded)
    assert all(w.path.startswith(os.path.realpath(card.root)) for w in recorded), "记的是真实位置，不是别名"


def test_r2_library_copy_must_really_be_local(volumes, lib):
    """`sha_in_library` / `current_sha`：索引指向的文件经链接落在卡上时不算图库副本。"""
    card = make_fake_card(volumes, pairs=0, jpg_only=1)
    c = one(volumes)
    run_import(lib, c, ImportOptions(**OPTS))
    row = lib.db.execute("SELECT relpath, sha256, size FROM files").fetchone()
    day = (lib.root / row["relpath"]).parent
    shutil.rmtree(day)
    # 日期目录换成指到卡上 DCIM/100RICOH 的链接：同名文件本身是普通文件，内容也和原件一样，但它在卡上
    day.symlink_to(card.root / "DCIM" / "100RICOH", target_is_directory=True)
    assert (lib.root / row["relpath"]).is_file() and Path(os.path.realpath(lib.root / row["relpath"])).is_relative_to(card.root.resolve())
    audit.protect(card.root)
    assert lib.current_sha(row["relpath"]) is None
    assert not lib.sha_in_library(row["sha256"], row["size"])
    assert photo_coverage(lib, c.root, Identity()).all_imported is False


# ---- R2-P1-02：内容证明绑定读字节时的状态 --------------------------------------------------------------


def test_r2_hash_that_never_settles_is_unknown(volumes, lib, monkeypatch):
    """每次算哈希的过程中文件都在变：结果是"未知"，不当作证明，覆盖核对不报全部导入。"""
    make_fake_card(volumes, pairs=0, jpg_only=1)
    c = one(volumes)
    run_import(lib, c, ImportOptions(**OPTS))
    rel = lib.db.execute("SELECT relpath FROM files").fetchone()[0]
    dest = lib.root / rel
    real_hash = store_mod.sha256_file
    flips = []

    def always_changing(path):
        value = real_hash(path)
        if Path(path) == dest:
            data = dest.read_bytes()
            dest.write_bytes(bytes([data[0] ^ 1]) + data[1:])  # 每读完一次就改一位
            flips.append(1)
        return value

    lib.db.execute("DELETE FROM filecheck")
    monkeypatch.setattr(store_mod, "sha256_file", always_changing)
    assert lib.current_sha(rel) is None and len(flips) >= 2
    assert photo_coverage(lib, c.root, Identity()).all_imported is False


def test_r2_note_verified_reads_actual_bytes(volumes, lib):
    """`note_verified` 不拿新的 lstat 给旧 SHA 签名：文件已经不是那份内容时返回 False，缓存记的是实际内容。"""
    make_fake_card(volumes, pairs=0, jpg_only=1)
    c = one(volumes)
    run_import(lib, c, ImportOptions(**OPTS))
    row = lib.db.execute("SELECT relpath, sha256 FROM files").fetchone()
    dest = lib.root / row["relpath"]
    st = dest.stat()
    dest.write_bytes(bytes(b ^ 7 for b in dest.read_bytes()))
    os.utime(dest, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert lib.note_verified(row["relpath"], row["sha256"]) is False
    assert lib.current_sha(row["relpath"]) != row["sha256"]
    p = run_import(lib, c, ImportOptions(**OPTS))
    assert p.copied_files == 1 and p.coverage["all_imported"] is True  # 下一次导入照样把原件放回去


# ---- R2-P1-03：私有目录的子目录句柄换向 --------------------------------------------------------------


def _moving_at(monkeypatch, target_factory, when):
    """在 `_at` 交出父目录 fd 之后、真正操作之前，把那个子目录整个移走（只替换 `_at` 这个接缝）。"""
    actual = privacy.PrivateDir._at
    moved = []

    from contextlib import contextmanager

    @contextmanager
    def at(directory, rel, create=False):
        with actual(directory, rel, create) as (dfd, name):
            if when(rel, create) and not moved:
                old = Path(privacy.fd_path(dfd))
                target = target_factory()
                audit_was = [r for r in audit.protected_roots()]
                for r in audit_was:
                    audit.unprotect(r)
                try:
                    old.rename(target / "moved")
                finally:
                    for r in audit_was:
                        audit.protect(r)
                moved.append(str(old))
            yield dfd, name

    monkeypatch.setattr(privacy.PrivateDir, "_at", at)
    return moved


@pytest.mark.parametrize("op", ["write", "replace_file", "remove", "read"])
@pytest.mark.parametrize("destination", ["card", "repository"])
def test_r2_every_operation_checks_its_own_parent_fd(tmp_path, volumes, monkeypatch, op, destination):
    card = make_fake_card(volumes)
    home = tmp_path / "app-data"
    (home / "a" / "b").mkdir(parents=True)
    (home / "a" / "b" / "old.bin").write_bytes(b"old")
    target = card.root if destination == "card" else tmp_path / "repo"
    if destination == "repository":
        (target / ".git").mkdir(parents=True)
    audit.protect(card.root)
    rel = {"write": "a/b/new.bin", "replace_file": "a/b/old.bin", "remove": "a/b/old.bin", "read": "a/b/old.bin"}[op]
    moved = _moving_at(monkeypatch, lambda: target, lambda r, create: r.startswith("a/b/"))
    before = tree_snapshot(target)
    d = privacy.PrivateDir(home, "test", [volumes])
    try:
        with pytest.raises(privacy.PrivatePathError):
            if op == "write":
                d.write(rel, fake_dat(1))
            elif op == "replace_file":
                d.replace_file(rel, fake_dat(1))
            elif op == "remove":
                d.remove(rel)
            else:
                d.read(rel)
    finally:
        d.close()
    assert moved, "子目录确实在拿到 fd 之后被移走了"
    after = tree_snapshot(target)
    new = {k: v for k, v in after.items() if k not in before and not k.startswith("moved")}
    moved_tree = {k: v for k, v in after.items() if k.startswith("moved")}
    assert new == {}, new
    assert sorted(k for k in moved_tree if moved_tree[k][0] == "file") == ["moved/old.bin"], "移走的目录里没多出、也没少掉文件"


@pytest.mark.parametrize("destination", ["card", "repository"])
def test_r2_mkdir_checks_the_intermediate_fd(tmp_path, volumes, monkeypatch, destination):
    """中间一级目录 fd 打开之后被整个移走，接下来要在它里面建子目录：建之前就拒绝，目标位置不多出任何东西。"""
    card = make_fake_card(volumes)
    home = tmp_path / "app-data"
    (home / "a" / "b").mkdir(parents=True)
    target = card.root if destination == "card" else tmp_path / "repo"
    if destination == "repository":
        (target / ".git").mkdir(parents=True)
    audit.protect(card.root)
    before = tree_snapshot(target)
    real_open = osplat.open_dir_at
    moved = []

    def opening(dir_fd, path):  # 平台原语：两个平台都经过它
        fd = real_open(dir_fd, path)
        if path == "b" and not moved:
            audit.unprotect(card.root)
            try:
                Path(privacy.fd_path(fd)).rename(target / "moved")
            finally:
                audit.protect(card.root)
            moved.append(1)
        return fd

    d = privacy.PrivateDir(home, "test", [volumes])
    monkeypatch.setattr(osplat, "open_dir_at", opening)
    try:
        with pytest.raises(privacy.PrivatePathError):
            d.write("a/b/c/new.bin", fake_dat(1))
    finally:
        monkeypatch.undo()
        d.close()
    after = tree_snapshot(target)
    assert moved and [k for k in after if k not in before] == ["moved"], after


def test_r2_audit_context_keeps_the_fd(tmp_path):
    """审计的相对路径上下文保留 fd：目录在打开之后被移走，记录的是它事件发生时真正所在的位置。"""
    a = tmp_path / "a"
    a.mkdir()
    fd = osplat.open_dir(a)
    try:
        a.rename(tmp_path / "b")
        with audit.AuditRecorder() as rec, audit.relative_to(fd):
            os.close(osplat.open_file_at(fd, "x.txt", create=True))
        assert [w.path for w in rec.writes if w.path.endswith("x.txt")] == [os.path.realpath(tmp_path / "b" / "x.txt")]
    finally:
        os.close(fd)


# ---- R2-P1-04：没有完整检查到的输入一律 R0 ---------------------------------------------------------------


def _checker():
    spec = importlib.util.spec_from_file_location("private_bytes_r2", STUDIO / "scripts" / "private_bytes.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _wrap(text: str, width: int, sep: str) -> str:
    return sep.join(text[i : i + width] for i in range(0, len(text), width))


def _content_cases() -> dict[str, tuple[bytes, str]]:
    """名字 -> (字节, 期望规则前缀)。R3 = 解出来命中；R0 = 没法完整检查。"""
    dat = fake_dat("r2", n=2048)
    b64, hexs = base64.b64encode(dat).decode(), dat.hex()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        info = tarfile.TarInfo("inner.bin")
        info.size = len(dat)
        t.addfile(info, io.BytesIO(dat))
    tar_truncated = buf.getvalue()[:700]
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w", compression=zipfile.ZIP_STORED) as z:
        z.writestr("a.bin", fake_dat("zip", n=300))
    bad_crc = bytearray(zbuf.getvalue())
    bad_crc[bad_crc.index(b"GXRCE001") + 20] ^= 0xFF  # 改成员内容，CRC 对不上
    return {
        "json_crlf_b64": (json.dumps({"x": _wrap(b64, 76, "\r\n")}).encode(), "R3"),
        "json_crlf_hex": (json.dumps({"x": _wrap(hexs, 64, "\r\n")}).encode(), "R3"),
        "crlf_text_b64": (_wrap(b64, 76, "\r\n").encode(), "R3"),
        "cr_only_b64": (_wrap(b64, 76, "\r").encode(), "R3"),
        "gzip_two_members": (gzip.compress(b"notes\n", mtime=0) + gzip.compress(dat, mtime=0), "R"),
        "gzip_unfinished": (gzip.compress(dat, mtime=0)[:20], "R0"),
        "bzip2_two_streams": (bz2.compress(b"notes") + bz2.compress(dat), "R"),
        "bzip2_truncated": (bz2.compress(dat)[:30], "R0"),
        "xz_truncated": (lzma.compress(dat)[:40], "R0"),
        "xz_payload": (lzma.compress(dat), "R"),
        "seven_zip_header": (b"7z\xbc\xaf\x27\x1c" + bytes(64), "R0"),
        "rar_header": (b"Rar!\x1a\x07\x00" + bytes(64), "R0"),
        "base64_of_gzip": (base64.b64encode(gzip.compress(dat, mtime=0)), "R"),
        "hex_of_zip": (zbuf.getvalue().hex().encode(), "R"),
        "tar_truncated_member": (tar_truncated, "R0"),
        "zip_bad_crc": (bytes(bad_crc), "R"),
        "encoded_over_limit": (base64.b64encode(b"\0" * (5 << 20)), "R0"),
    }


@pytest.mark.parametrize("name", list(_content_cases()))
def test_r2_content_check_never_passes_unchecked_input(tmp_path, name):
    pb = _checker()
    data, rule = _content_cases()[name]
    p = tmp_path / name
    p.write_bytes(data)
    hits = pb.scan_paths([p])
    assert hits and any(r.startswith(rule) for _, r in hits), hits
    assert pb.main([str(p)]) == 1


def test_r2_complete_compressed_input_without_payload_passes(tmp_path):
    """对照：能完整解开、内容也干净的压缩文件照常通过（不是一律报错）。"""
    pb = _checker()
    for name, data in {
        "ok.gz": gzip.compress(b"plain text\n" * 100, mtime=0) * 2,
        "ok.bz2": bz2.compress(b"plain text\n" * 100),
        "ok.xz": lzma.compress(b"plain text\n" * 100),
    }.items():
        p = tmp_path / name
        p.write_bytes(data)
        assert pb.scan_paths([p]) == [], name


# ---- R2-P1-05：计算结果必须有限 ----------------------------------------------------------------------

CM = (50721, "SRATIONAL", [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0])


@pytest.mark.parametrize(
    "case",
    ["zero_forward", "zero_colour_matrix", "equal_levels", "black_above_white", "delta_pushes_black_over_white"],
)
def test_r2_computed_values_are_finite_or_refused(case):
    from gxr_studio.lut.scene import SceneError, scene_colour
    from gxr_studio.raw import RawDecodeError, half_res_codes, open_dng, render_half
    from raw_support import build_dng

    mosaic = np.full((8, 8), 100, np.uint16)
    kw: dict = dict(white=4095, neutral=(0.6, 1.0, 0.5), root_extra=(CM,))
    if case == "zero_forward":
        kw["root_extra"] = (CM, (50964, "SRATIONAL", [(0, 1)] * 9))
    elif case == "zero_colour_matrix":
        kw["root_extra"] = ((50721, "SRATIONAL", [(0, 1)] * 9),)
    elif case == "equal_levels":
        kw.update(black=(64,), white=64)
    elif case == "black_above_white":
        kw.update(black=(500,), white=400)
    else:
        kw.update(black=(100,), white=200, black_delta_h=[150.0] * 8)
    try:
        dng = open_dng(build_dng(mosaic, **kw))
        codes = half_res_codes(dng)
        assert np.isfinite(codes).all()
        image = render_half(dng)
        assert image.dtype == np.uint8
        xyz = scene_colour(dng).xyz(codes.reshape(-1, 3))
        assert np.isfinite(xyz).all(), "能算出来的结果必须全部有限"
        refused = False
    except (RawDecodeError, SceneError):
        refused = True
    assert refused, "这些输入应当被明确拒绝"


def test_r2_lens_info_zero_over_zero_is_still_tolerated():
    from gxr_studio.lut.scene import scene_colour
    from gxr_studio.raw import half_res_codes, open_dng
    from raw_support import build_dng

    data = build_dng(
        np.full((8, 8), 100, np.uint16), white=4095, neutral=(0.6, 1.0, 0.5),
        root_extra=(CM, (50736, "RATIONAL", [(0, 100), (0, 100), (0, 0), (0, 0)])),
        exif_entries=((42034, "RATIONAL", [(0, 100), (0, 100), (0, 0), (0, 0)]), (33437, "RATIONAL", [(0, 0)])),
    )  # fmt: skip
    dng = open_dng(data)
    codes = half_res_codes(dng)
    assert np.isfinite(scene_colour(dng).xyz(codes.reshape(-1, 3))).all() and dng.exif.get("f") is None


# ---- R2-P2-01：首个导入请求失败后能重试 ----------------------------------------------------------------

REQUEST_ERROR = r"""
import { pathToFileURL } from 'node:url';
const dir = process.argv[2];
const card = { id: 'A'.repeat(26), label: 'CARD-A', uuid_state: 'missing' };
globalThis.reviewState = { card: null, cards: [], cardSummary: null, cardAccess: null, cardMsg: null, offline: false,
  dismissedJobId: null, job: null };
const posts = [];
globalThis.reviewApi = async (url, options) => {
  if (url === '/api/cards') return [card];
  if (url === `/api/cards/${card.id}/summary`) return { ...card, shots: 1, new_shots: 1, verified: false };
  if (url === '/api/import') {
    posts.push(options.body.card_id);
    if (posts.length === 1) { const e = new Error('请求失败'); e.code = 'offline'; throw e; }
    return { id: 'job-A', kind: 'import', running: true, error: null,
      progress: { state: 'running', card_id: card.id, done_shots: 0, total_shots: 1 } };
  }
  throw new Error(`unexpected ${url}`);
};
const elements = new Map();
globalThis.document = { getElementById(id) {
  if (!elements.has(id)) elements.set(id, { textContent: '', children: [], className: '',
    replaceChildren(...n) { this.children = n; }, append(...n) { this.children.push(...n); } });
  return elements.get(id);
} };
const text = (n) => n == null ? '' : typeof n === 'string' ? n : [n.textContent || '', ...(n.children || []).map(text)].join(' ');
const buttons = (n, out = []) => {
  if (n && typeof n === 'object') { if (n.tag === 'button') out.push(n); (n.children || []).forEach((c) => buttons(c, out)); }
  return out;
};
const imp = await import(pathToFileURL(`${dir}/import.js`).href);
await imp.refreshCards();
await imp.refreshCards();
const banner = elements.get('banner');
const retry = buttons(banner).find((b) => text(b).includes('导入'));
const before = posts.length;
if (retry) await retry.attrs.onclick();
console.log(JSON.stringify({ before, after: posts.length, banner: text(banner) }));
"""


def test_r2_first_import_request_failure_can_be_retried(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("没有 node")
    d = tmp_path / "web"
    d.mkdir()
    for name in ("import.js", "import_summary.js"):
        shutil.copy(ASSETS / name, d / name)
    (d / "package.json").write_text('{"type":"module"}\n', encoding="utf-8")
    (d / "api.js").write_text("export const api = (...a) => globalThis.reviewApi(...a);\n", encoding="utf-8")
    (d / "state.js").write_text("export const state = globalThis.reviewState; export const emit = () => {};\n", encoding="utf-8")
    (d / "dom.js").write_text(
        "export const h = (tag, attrs, ...children) => ({tag, attrs, children});\n"
        "export const toast = () => {};\nexport const toastError = () => {};\n"
        "export const setFraction = (bar) => bar;\nexport const platform = () => 'mac';\n",
        encoding="utf-8",
    )
    (d / "scenario.mjs").write_text(REQUEST_ERROR, encoding="utf-8")
    out = subprocess.run([node, str(d / "scenario.mjs"), str(d)], capture_output=True, text=True, timeout=60, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    r = json.loads(out.stdout)
    assert r["before"] == 1, "失败之后不自动连续重发"
    assert r["after"] == 2, f"横幅里要有能用的「导入」按钮：{r['banner']}"


WRITE_REFUSED_SUMMARY = r"""
import { pathToFileURL } from 'node:url';
const S = await import(pathToFileURL(process.argv[2]).href);
const p = { state: 'done', imported_shots: 0, completed_shots: 0, imported_videos: 0, seen_shots: 0, duplicate_shots: 0,
  failed_shots: 1, unreadable: [], verify_failed: [], scan_errors: [], write_refused: ['DCIM/100RICOH/R0010001.JPG'],
  coverage: { complete: true, dcim: 'present', total: 0, covered: 0, uncovered: [], not_importable: [], all_imported: true } };
console.log(JSON.stringify({ summary: S.summarizeImport(p), eject: S.canEject(p), status: S.doneStatusText(p) }));
"""


def test_r2_refused_library_writes_show_as_not_imported(tmp_path):
    """界面：图库位置被拒绝写入的文件算"未能导入"，横幅叫人别格式化，状态行不说"可以推出"（只看这一项，核对结果故意给干净的）。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("没有 node")
    script = tmp_path / "s.mjs"
    script.write_text(WRITE_REFUSED_SUMMARY, encoding="utf-8")
    out = subprocess.run([node, str(script), str(ASSETS / "import_summary.js")], capture_output=True, text=True, timeout=60, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    r = json.loads(out.stdout)
    assert r["eject"] is False and r["status"] == "存储卡：有未导入的照片"
    assert r["summary"]["kind"] == "bad" and r["summary"]["text"] == "有 1 个文件未能导入。请勿格式化存储卡，重新插入后再试一次。"
    assert "R0010001.JPG" in r["summary"]["detail"]


# ---- R2-P2-02：从 importer 导入的公开名字 ------------------------------------------------------------------


def test_r2_public_import_names():
    """importer 转出的公开名字都能用；HEAD 里的私有名字 `_done_message` 不再提供（报告里已改正之前"旧路径都能用"的说法）。"""
    from gxr_studio.cards import importer, messages

    for name in ("CHECK_FAILED", "DONE_ALL", "done_message", "run_import", "stage_file", "ImportOptions", "ImportProgress"):
        assert name in importer.__all__ and hasattr(importer, name), name
    assert importer.done_message is messages.done_message
