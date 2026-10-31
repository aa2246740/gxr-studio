"""A1a 代码复审第 1 轮（research/review_studio_a1a/REVIEW.md）的回归用例：每条复审的复现都写成测试。

修好之前这些用例必须失败，修好之后必须通过。全部用现场生成的合成卡、合成 DNG、合成标定和合成"理光字节"；
不碰真实卡、用户图库或应用数据，也不做任何 git 写操作（P1-06 在子进程层模拟 git 的输出，另用只读的
`git cat-file` 读本仓库已有的对象）。
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import os
import shutil
import struct
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from conftest import PORT
from gxr_studio import audit, privacy
from gxr_studio.app import create_app
from gxr_studio.camdata import calib, store
from gxr_studio.cards.coverage import Identity
from gxr_studio.cards.discovery import find_cards
from gxr_studio.cards.importer import DONE_ALL, ImportOptions, run_import
from gxr_studio.cards.session import SessionRegistry
from gxr_studio.devtools.synthetic_card import tree_snapshot
from gxr_studio.engine import colour
from gxr_studio.library.store import Library
from support.fake_card import FakeProbe, avi_bytes, fake_dat, jpeg_bytes, make_fake_card, stamp
from support.fake_git import FakeGit
from test_api import import_all

STUDIO = Path(__file__).resolve().parents[1]
ASSETS = STUDIO / "web" / "assets"
UUID_A = "AAAAAAAA-1111-2222-3333-444444444444"
OPTS = dict(min_free_bytes=0, make_thumbs=False)


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def library_digests(lib: Library) -> set[str]:
    return {sha(p.read_bytes()) for p in lib.root.rglob("*") if p.is_file() and ".studio" not in p.parts}


def one(volumes: Path):
    (c,) = find_cards([volumes])
    return c


# ---- P1-01：换卡后，旧卡任务的结果不能套到新卡上 ---------------------------------------------

FRONTEND_SCENARIO = r"""
import { pathToFileURL } from 'node:url';
const dir = process.argv[2];
const cardA = { id: 'A'.repeat(26), label: 'CARD-A', uuid_state: 'missing' };
const cardB = { id: 'B'.repeat(26), label: 'CARD-B', uuid_state: 'missing' };
const coverageAll = { complete: true, dcim: 'present', total: 3, covered: 3, uncovered: [], not_importable: [],
  system_files: [], all_imported: true, proof: { content: 3, trusted_seen: 0 } };
const doneA = { id: 'job-A', kind: 'import', running: false, error: null,
  progress: { state: 'done', card_id: cardA.id, imported_shots: 0, completed_shots: 0, imported_videos: 0, seen_shots: 0,
    duplicate_shots: 3, done_shots: 3, total_shots: 3, unreadable: [], verify_failed: [], scan_errors: [], coverage: coverageAll,
    message: '这张存储卡上的照片都已导入' } };
const runningB = { id: 'job-B', kind: 'import', running: true, error: null,
  progress: { state: 'running', card_id: cardB.id, imported_shots: 0, completed_shots: 0, imported_videos: 0, seen_shots: 0,
    duplicate_shots: 0, done_shots: 0, total_shots: 3, unreadable: [], verify_failed: [], scan_errors: [], coverage: null } };
globalThis.reviewState = { card: cardA, cards: [cardA], cardSummary: null, cardAccess: null, cardMsg: null, offline: false,
  dismissedJobId: null, job: { ...doneA, running: true, progress: { ...doneA.progress, state: 'running', coverage: null } } };
const requests = [];
globalThis.reviewApi = async (url, options) => {
  requests.push({ url, method: (options && options.method) || 'GET', body: options && options.body });
  if (url === '/api/cards') return [cardB];
  if (url === `/api/cards/${cardB.id}/summary`) return { ...cardB, shots: 3, new_shots: 3, verified: false };
  if (url === '/api/jobs/job-A') return doneA;
  if (url === '/api/jobs/job-B') return runningB;
  if (url === '/api/jobs') return [doneA];
  if (url === '/api/import') return runningB;
  throw new Error(`unexpected request ${url}`);
};
const elements = new Map();
globalThis.document = { getElementById(id) {
  if (!elements.has(id)) elements.set(id, { textContent: '', children: [], className: '',
    replaceChildren(...n) { this.children = n; }, append(...n) { this.children.push(...n); } });
  return elements.get(id);
} };
const text = (n) => n == null ? '' : typeof n === 'string' ? n : [n.textContent || '', ...(n.children || []).map(text)].join(' ');
const imp = await import(pathToFileURL(`${dir}/import.js`).href);
const snapshots = [];
const snap = (step) => snapshots.push({ step, banner: text(elements.get('banner')).trim(),
  status: elements.get('card-status').textContent, card: globalThis.reviewState.card && globalThis.reviewState.card.label });
// A 在服务端已经导完；页面上次轮询时它还在跑。轮询之前 A 被拔掉、B 插上。
await imp.refreshCards(); snap('refresh-1');
await imp.tickImport(); snap('tick');
await imp.refreshCards(); snap('refresh-2');
await imp.refreshCards(); snap('refresh-3');
const posts = requests.filter((r) => r.method === 'POST' && r.url === '/api/import');
console.log(JSON.stringify({ posts, snapshots }));
"""


def _run_frontend(tmp_path: Path) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("没有 node")
    d = tmp_path / "web"
    d.mkdir()
    for name in ("import.js", "import_summary.js"):
        shutil.copy(ASSETS / name, d / name)
    (d / "package.json").write_text('{"type":"module"}\n')
    (d / "api.js").write_text("export const api = (...a) => globalThis.reviewApi(...a);\n")
    (d / "state.js").write_text("export const state = globalThis.reviewState; export const emit = () => {};\n")
    (d / "dom.js").write_text(
        "export const h = (tag, attrs, ...children) => ({tag, attrs, children});\n"
        "export const toast = () => {};\nexport const toastError = (e) => { throw e; };\n"
    )
    (d / "scenario.mjs").write_text(FRONTEND_SCENARIO)
    out = subprocess.run([node, str(d / "scenario.mjs"), str(d)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_r1_p1_01_old_card_result_is_not_applied_to_new_card(tmp_path):
    """P1-01：A 卡任务已在服务端完成、页面还留着 running=true 时换成 B 卡：B 必须发出自己的导入请求；
    A 的"都已导入"和"可以推出"不能出现在 B 卡上。"""
    r = _run_frontend(tmp_path)
    assert [p["body"]["card_id"] for p in r["posts"]] == ["B" * 26], r
    for s in r["snapshots"]:
        assert s["card"] == "CARD-B"
        assert "都已导入" not in s["banner"] and "可以推出" not in s["status"], s


def test_r1_p1_01_job_progress_carries_its_card_session(settings, volumes):
    """P1-01 服务端：导入任务和它的结果带着来源挂载会话号。"""
    make_fake_card(volumes)
    app = create_app(settings, sessions=SessionRegistry(FakeProbe()))
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as client:
        (c,) = client.get("/api/cards").json()
        started = client.post("/api/import", json={"card_id": "0" * 26})  # 先确认格式对、不存在的号不会建任务
        assert started.status_code == 404
        job = import_all(client)  # 发起导入，并等到任务真正结束（有时间上限，不靠固定次数的轮询）
        assert job["progress"]["card_id"] == c["id"] and job["progress"]["state"] == "done"


# ---- P1-02：导入要带着完整会话；读后变化的文件不能沿用旧证明 --------------------------------------


def test_r1_p1_02_remount_during_import_is_interrupted(volumes, lib):
    """P1-02（复审 mid_import_remount）：第二次导入处理完一张后，同路径换成同长度、同 mtime 的另一份内容，
    statfs 指纹也变了：导入必须中断，不能拿换挂载之前的证明报"都已导入"。"""
    from gxr_studio.api.routes import identity_of

    card = make_fake_card(volumes, pairs=0, jpg_only=1)
    c = one(volumes)
    probe = FakeProbe()
    probe.set_uuid(card.root, UUID_A)
    reg = SessionRegistry(probe)
    (session,) = reg.attach_all([c])
    ident = identity_of(session)
    run_import(lib, c, ImportOptions(**OPTS), ident=ident)
    changed = jpeg_bytes("card-B")

    def remount_after_shot(_key):
        audit.unprotect(card.root)
        path = card.path("DCIM/100RICOH/R0010001.JPG")
        path.write_bytes(changed)
        stamp(path)
        probe.remount(card.root)
        probe.set_uuid(card.root, "BBBBBBBB-1111-2222-3333-444444444444")
        audit.protect(card.root)

    p = run_import(lib, c, ImportOptions(**OPTS, after_shot=remount_after_shot), ident=identity_of(session))
    assert p.message != DONE_ALL and not (p.coverage or {}).get("all_imported"), p
    assert p.state == "interrupted"
    assert sha(changed) not in library_digests(lib)


def test_r1_p1_02_file_rewritten_after_read_is_read_again(volumes, lib):
    """P1-02 后半：同一次挂载里，文件在读完之后被等长改写、mtime 改回原值：结束核对不能沿用读之前的证明。"""
    card = make_fake_card(volumes, pairs=0, jpg_only=1)
    c = one(volumes)
    changed = jpeg_bytes("rewritten")

    def rewrite_after_shot(_key):
        audit.unprotect(card.root)
        path = card.path("DCIM/100RICOH/R0010001.JPG")
        path.write_bytes(changed)
        stamp(path)
        audit.protect(card.root)

    p = run_import(lib, c, ImportOptions(**OPTS, after_shot=rewrite_after_shot), ident=Identity(UUID_A))
    assert p.coverage["all_imported"] is False and p.coverage["uncovered"] == ["DCIM/100RICOH/R0010001.JPG"]
    assert "请勿格式化" in p.message
    p2 = run_import(lib, c, ImportOptions(**OPTS), ident=Identity(UUID_A))
    assert sha(changed) in library_digests(lib) and p2.coverage["all_imported"] is True


# ---- P1-03 / P2-01：图库副本内容不对（同长度替换、截断）：重新保存原件，保留被改的文件 ------------------


@pytest.mark.parametrize("how", ["same_size_replacement", "truncated"])
@pytest.mark.parametrize("media", ["photo", "video"])
def test_r1_p1_03_damaged_library_copy_is_restored(volumes, lib, how, media):
    """P1-03（same_size_library_replacement）和 P2-01（truncated_library_file）：
    图库里那份被等长换成别的内容，或被截断：再导入要真正把卡上原件存回来，被改的文件留着，不能说"都已导入"。"""
    make_fake_card(volumes, pairs=0, jpg_only=1 if media == "photo" else 0, videos=1 if media == "video" else 0)
    c = one(volumes)
    run_import(lib, c, ImportOptions(**OPTS))
    table = "files" if media == "photo" else "videos"
    row = lib.db.execute(f"SELECT relpath, sha256 FROM {table}").fetchone()  # noqa: S608
    dest = lib.root / row["relpath"]
    original = dest.read_bytes()
    damaged = jpeg_bytes("different-content", n=len(original)) if how == "same_size_replacement" else b"x"
    dest.write_bytes(damaged)
    p = run_import(lib, c, ImportOptions(**OPTS))
    assert p.message != DONE_ALL and p.copied_files == 1, p
    assert dest.read_bytes() == original, "原位置要恢复成卡上的原件"
    assert sha(damaged) in library_digests(lib), "被改过的文件不能丢掉"
    assert p.coverage["all_imported"] is True
    assert run_import(lib, c, ImportOptions(**OPTS)).message == DONE_ALL


# ---- P1-04：能识别成照片或视频的文件不能算系统文件 -----------------------------------------------


def _real_jpeg() -> bytes:
    bio = io.BytesIO()
    Image.new("RGB", (13, 11), (13, 97, 201)).save(bio, "JPEG")
    return bio.getvalue()


def test_r1_p1_04_decodable_photos_are_never_system_files(volumes, lib):
    """P1-04（system_rule_real_photos）：放在 `._*`、`.Spotlight-V100/`、`.Trashes/` 下的真 JPEG 不是系统文件。"""
    jpg = _real_jpeg()
    extra = {
        "100RICOH/._R0090001.JPG": jpg,
        "100RICOH/.Spotlight-V100/R0090002.JPG": jpg,
        ".Trashes/501/R0090003.JPG": jpg,
        ".Trashes/501/R0090004.AVI": avi_bytes(4),
        ".fseventsd/R0090005.DAT": jpg,  # 名字不像照片，内容是 JPEG
    }
    make_fake_card(volumes, dcim_extra=extra)
    c = one(volumes)
    run_import(lib, c, ImportOptions(**OPTS))
    p = run_import(lib, c, ImportOptions(**OPTS))
    assert p.coverage["system_files"] == [], p.coverage
    assert sorted(p.coverage["not_importable"]) == sorted(f"DCIM/{r}" for r in extra)
    assert p.message != DONE_ALL and p.coverage["all_imported"] is False


def test_r1_p1_04_appledouble_needs_its_magic(volumes, lib):
    """`._*` 要核对 AppleDouble 的格式：真的 AppleDouble 是系统文件，别的内容不是。"""
    make_fake_card(
        volumes,
        dcim_extra={"100RICOH/._R0010001.JPG": b"\x00\x05\x16\x07" + bytes(60), "100RICOH/._R0010002.JPG": b"not appledouble"},
    )
    p = run_import(lib, one(volumes), ImportOptions(**OPTS))
    assert p.coverage["system_files"] == ["DCIM/100RICOH/._R0010001.JPG"]
    assert p.coverage["not_importable"] == ["DCIM/100RICOH/._R0010002.JPG"]


# ---- P1-05：标定、相机数据写入要相对已核对的目录句柄进行 --------------------------------------------


def _stock_entry() -> bytes:
    entry = bytearray(552)
    for i in range(24):
        entry[115 + 4 * i : 119 + 4 * i] = colour.encode_word(8, 0)
    return bytes(entry)


def _calib_pack(folder: Path, entry: bytes) -> None:
    stock = calib.stock_words_of_entry(entry)
    d0, d1 = np.zeros(24), np.zeros(24)
    files = {}
    isos = {}
    folder.mkdir(parents=True)
    for iso in calib.CALIBRATED_ISOS:
        bio = io.BytesIO()
        np.savez(bio, stock=stock, d0=d0, d1=d1, S_eff=((stock[:, 0] + d0) - 1j * (stock[:, 1] + d1)) / 32)
        npz, ring = f"ctx_{iso}.npz", f"ring_{iso}.json"
        (folder / npz).write_bytes(bio.getvalue())
        (folder / ring).write_text(json.dumps({"schema": "gxr-sector-model/2"}))
        isos[str(iso)] = {"npz": npz, "ring": ring}
        for name in (npz, ring):
            data = (folder / name).read_bytes()
            files[name] = {"sha256": sha(data), "size": len(data)}
    (folder / "calib-pack.json").write_text(json.dumps({"schema": "gxr-studio/calib-pack/1", "isos": isos, "files": files}))


def _card_files(root: Path) -> dict:
    """整棵卡目录：文件内容、大小、mtime，以及每个目录的 mtime（建了又删的临时目录也会留下痕迹）。"""
    return tree_snapshot(root)


def _swap_after_check(monkeypatch, home: Path, target: Path, purpose: str) -> list:
    """`purpose` 那一次真实核对通过之后，立刻把应用数据目录换成指向 `target` 的链接（只替换核对这个接缝，不替换写入）。"""
    real = privacy.check_private_dir
    swapped = []

    def check_then_swap(path, why, forbidden=()):
        out = real(path, why, forbidden)
        if why == purpose and not swapped:
            home.rename(home.with_name(home.name + "-original"))
            home.symlink_to(target, target_is_directory=True)
            swapped.append(why)
        return out

    monkeypatch.setattr(privacy, "check_private_dir", check_then_swap)
    return swapped


def test_r1_p1_05_calibration_import_cannot_follow_a_swapped_alias_into_the_card(settings, volumes, tmp_path, monkeypatch):
    """P1-05（calibration_alias_race）：产品入口 /api/calibration/import。应用数据目录在第一次核对通过之后被换成指向
    受保护假卡的链接：标定写不进卡（零新增、零改动），接口拒绝。"""
    entry = _stock_entry()
    home = tmp_path / "app-data"
    (store.camera_data_dir(home) / "units" / "jlaunch").mkdir(parents=True)
    (store.camera_data_dir(home) / "units" / "jlaunch" / "stock04.entry").write_bytes(entry)
    pack = tmp_path / "pack"
    _calib_pack(pack, entry)
    card = make_fake_card(volumes)
    before = _card_files(card.root)
    app = create_app(settings.with_(home_dir=home))
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as client:
        assert client.get("/api/cards").json()  # 卡已发现、已登记写保护
        swapped = _swap_after_check(monkeypatch, home, card.root, "calibration")
        r = client.post("/api/calibration/import", json={"path": str(pack)})
    assert swapped, "换向确实发生了"
    assert _card_files(card.root) == before, "假卡上零新增、零改动（含目录 mtime）"
    assert r.status_code == 422 and r.json()["error"].startswith("private_path"), r.text


def test_r1_p1_05_calibration_writer_itself_is_fd_guarded(volumes, tmp_path, monkeypatch):
    """P1-05：直接调产品写入函数 calib.import_calibration（不替换任何写入函数）：检查之后换向也写不进卡。"""
    entry = _stock_entry()
    home = tmp_path / "app-data"
    (store.camera_data_dir(home) / "units" / "jlaunch").mkdir(parents=True)
    (store.camera_data_dir(home) / "units" / "jlaunch" / "stock04.entry").write_bytes(entry)
    pack = tmp_path / "pack"
    _calib_pack(pack, entry)
    card = make_fake_card(volumes)
    audit.protect(card.root)
    before = _card_files(card.root)
    sources = {iso: (pack / f"ctx_{iso}.npz", pack / f"ring_{iso}.json") for iso in calib.CALIBRATED_ISOS}
    swapped = _swap_after_check(monkeypatch, home, card.root, "calibration")
    with pytest.raises(privacy.PrivatePathError):
        calib.import_calibration(home, sources)
    assert swapped and _card_files(card.root) == before


def test_r1_p1_05_camera_data_writer_is_fd_guarded(volumes, tmp_path, monkeypatch):
    """P1-05：相机数据写入（store.import_from_dat）同样。解析器换成合成条目（不是写入函数），写入走真实路径。"""
    entries = {(u, s): bytes([u, s]) * 276 for u in range(3) for s in range(1, 9)}
    monkeypatch.setattr(store, "entries_from_dat", lambda raw: (SimpleNamespace(generation=137), entries))
    home = tmp_path / "app-data"
    home.mkdir()
    card = make_fake_card(volumes)
    audit.protect(card.root)
    before = _card_files(card.root)
    swapped = _swap_after_check(monkeypatch, home, card.root, "camera_data")
    with pytest.raises(privacy.PrivatePathError):
        store.import_from_dat(fake_dat(1), home)
    assert swapped and _card_files(card.root) == before


# ---- P1-06：提交前检查读暂存区的 blob，不读工作区 ------------------------------------------------


def _checker():
    spec = importlib.util.spec_from_file_location("private_bytes_r1", STUDIO / "scripts" / "private_bytes.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("worktree", ["safe_copy", "missing"])
def test_r1_p1_06_staged_blob_is_checked_not_the_worktree(tmp_path, monkeypatch, worktree):
    """P1-06：暂存区里是受限内容，工作区的同名文件已改回普通文本，或者已经不在：提交前检查照样拦住。"""
    pb = _checker()
    name = "tests/fixtures/notes-r1.txt"
    work = pb.ROOT / name
    assert not work.exists()
    if worktree == "safe_copy":
        monkeypatch.setattr(pb, "ROOT", tmp_path)
        (tmp_path / "tests/fixtures").mkdir(parents=True)
        (tmp_path / name).write_text("plain text\n")
    fake = FakeGit({name: fake_dat(6, n=4096)})
    fake.install(monkeypatch, pb)
    assert pb.main(["--staged"]) == 1
    assert any(c[:3] == ["git", "cat-file", "--batch"] for c in fake.calls), "必须按对象 id 读暂存区的 blob"


def test_r1_p1_06_cat_file_reads_real_blobs_read_only():
    """真实 git（只读）：按对象 id 读出的字节和 `git show HEAD:<path>` 一致。"""
    pb = _checker()
    oid = subprocess.check_output(["git", "rev-parse", "HEAD:README.md"], cwd=STUDIO).decode().strip()
    want = subprocess.check_output(["git", "cat-file", "blob", oid], cwd=STUDIO)
    assert pb.read_blobs([oid]) == {oid: want}


# ---- P1-07：换行编码和超出归档深度、尺寸的内容不能放行 ----------------------------------------------


def _zip(name: str, data: bytes) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, data)
    return out.getvalue()


def _encoded_cases() -> dict[str, bytes]:
    dat = fake_dat("review-generated", n=2048)
    hexs = dat.hex()
    nested = dat
    for i in range(5):
        nested = _zip(f"level-{i}.bin", nested)
    return {
        "wrapped_b64.txt": base64.encodebytes(dat),  # 每 76 列换行
        "wrapped_hex.txt": "\n".join(hexs[i : i + 64] for i in range(0, len(hexs), 64)).encode(),
        "wrapped_b64.json": json.dumps({"blob": base64.encodebytes(dat).decode()}).encode(),  # JSON 里的 \n 转义
        "nested-5.zip": nested,
    }


@pytest.mark.parametrize("name", list(_encoded_cases()))
def test_r1_p1_07_wrapped_encodings_and_deep_archives_fail_the_check(tmp_path, name):
    """P1-07：76 列 base64、64 列 hex、五层 zip：检查失败（退出 1），不能当作没有命中。"""
    pb = _checker()
    p = tmp_path / name
    p.write_bytes(_encoded_cases()[name])
    assert pb.main([str(p)]) == 1
    assert pb.scan_paths([p]) != []


def test_r1_p1_07_oversized_member_is_reported_unchecked(tmp_path, monkeypatch, capsys):
    pb = _checker()
    monkeypatch.setattr(pb, "MAX_MEMBER_BYTES", 1000)
    p = tmp_path / "big.zip"
    p.write_bytes(_zip("inner/blob.bin", os.urandom(4000)))
    assert pb.main([str(p)]) == 1
    out = capsys.readouterr().out
    assert "big.zip!inner/blob.bin" in out and "unchecked" in out


def test_r1_p1_07_staged_and_dist_paths_use_the_same_rules(tmp_path, monkeypatch):
    pb = _checker()
    cases = _encoded_cases()
    fake = FakeGit({f"r1/{n}": d for n, d in cases.items()})
    fake.install(monkeypatch, pb)
    assert pb.main(["--staged"]) == 1
    hits = {n.split("!")[0] for n, _ in pb.scan_staged()}
    assert hits == {f"staged:r1/{n}" for n in cases}
    wheel = tmp_path / "pkg-0.1-py3-none-any.whl"
    wheel.write_bytes(_zip("pkg/data.txt", cases["wrapped_b64.txt"]))
    monkeypatch.setattr(pb, "dist_files", lambda out: [wheel])
    assert pb.main(["--dist"]) == 1


# ---- P1-08：渲染用的有理数标签遇到 0/0 要明确拒绝 ----------------------------------------------------

CM = (50721, "SRATIONAL", [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0])


@pytest.mark.parametrize("tag", [50723, 50964], ids=["CameraCalibration1", "ForwardMatrix1"])
def test_r1_p1_08_zero_denominator_render_matrix_is_refused(tag):
    """P1-08：CameraCalibration1 / ForwardMatrix1 存在但分母为 0：不能当作"没有"悄悄换成单位矩阵，要拒绝。"""
    from gxr_studio.lut.scene import SceneError, scene_colour
    from gxr_studio.raw import open_dng
    from raw_support import build_dng

    data = build_dng(np.full((8, 8), 100, np.uint16), white=4095, neutral=(0.6, 1.0, 0.5), root_extra=(CM, (tag, "SRATIONAL", [(0, 0)] * 9)))
    with pytest.raises(SceneError) as e:
        scene_colour(open_dng(data))
    assert e.value.code == "bad_matrix"


def test_r1_p1_08_linearization_table_must_be_short_and_finite():
    """P1-08：LinearizationTable 的 TIFF 类型改成 RATIONAL 并填 0/0：打开就拒绝，不能产生 NaN 码值。"""
    from gxr_studio.raw import RawDecodeError, half_res_codes, open_dng
    from gxr_studio.raw.tiff import Tiff
    from raw_support import build_dng

    mosaic = np.full((8, 8), 100, np.uint16)
    raw = bytearray(build_dng(mosaic, linearization=list(range(128)), white=4095, neutral=(0.6, 1.0, 0.5), root_extra=(CM,)))
    t = Tiff(bytes(raw))
    sub_offset = t.ifd(t.root)[330]["value"][0]
    count = struct.unpack_from(">H", raw, sub_offset)[0]
    new_offset = len(raw)
    raw.extend(b"\0" * (128 * 8))
    for i in range(count):
        at = sub_offset + 2 + 12 * i
        if struct.unpack_from(">H", raw, at)[0] == 50712:
            struct.pack_into(">H", raw, at + 2, 5)
            struct.pack_into(">I", raw, at + 8, new_offset)
            break
    else:
        raise AssertionError("合成 DNG 里没有 LinearizationTable")
    with pytest.raises(RawDecodeError):
        half_res_codes(open_dng(bytes(raw)))


def test_r1_p1_08_absent_matrices_and_unknown_lens_info_still_work():
    """对照：标签不存在仍按"没有"处理；LensInfo 的 0/0（手动镜头）仍然容忍。"""
    from gxr_studio.lut.scene import scene_colour
    from gxr_studio.raw import open_dng
    from raw_support import build_dng

    data = build_dng(np.full((8, 8), 100, np.uint16), white=4095, neutral=(0.6, 1.0, 0.5), root_extra=(CM,))
    xyz = scene_colour(open_dng(data)).xyz(np.array([[1000.0, 1000.0, 1000.0]]))
    assert np.isfinite(xyz).all()
    lens = build_dng(
        np.full((8, 8), 100, np.uint16), white=4095, root_extra=(CM,), exif_entries=((42034, "RATIONAL", [(0, 100), (0, 100), (0, 0), (0, 0)]),)
    )
    assert open_dng(lens).exif is not None
