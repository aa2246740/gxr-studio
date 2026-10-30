"""图库：目录布局 + SQLite 索引（只是缓存，可以整个删掉重建）。

真相是文件和 sidecar：

    <图库>/YYYY/YYYY-MM-DD/Rxxxxxxx.DNG|JPG|gxrs.json
    <图库>/未知日期/…                      （undated_policy = unknown_folder 时）
    <图库>/.studio/{index.sqlite, thumbs/, staging/, import-log.jsonl}

`.studio` 里除了 `import-log.jsonl`（追加日志，同时是 seen 的备份）以外都能删。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from .. import meta as metamod
from . import sidecar as sc

SCHEMA_VERSION = 1
UNKNOWN_DATE_DIR = "未知日期"
BACKUP_DIR = "相机备份"
MEDIA_EXT = {".dng": "dng", ".jpg": "jpg"}
SIDECAR_RESET_NOTE = "这张照片的编辑记录坏了或不见了，已经当作没有编辑处理；照片本身没事。再保存一次编辑就会重新建好。"
_DATE_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS shots(
  shot_id TEXT PRIMARY KEY, name TEXT NOT NULL, folder TEXT NOT NULL,
  date TEXT, date_source TEXT, captured_at TEXT,
  unit TEXT, lens TEXT, iso INTEGER, orientation INTEGER, width INTEGER, height INTEGER,
  has_raw INTEGER NOT NULL, has_jpg INTEGER NOT NULL, added_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS files(
  shot_id TEXT NOT NULL, kind TEXT NOT NULL, relpath TEXT NOT NULL,
  sha256 TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
  PRIMARY KEY(shot_id, kind));
CREATE INDEX IF NOT EXISTS files_sha ON files(sha256);
CREATE INDEX IF NOT EXISTS files_rel ON files(relpath);
CREATE TABLE IF NOT EXISTS seen(
  card_key TEXT NOT NULL, relpath TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
  sha256 TEXT NOT NULL, PRIMARY KEY(card_key, relpath));
CREATE TABLE IF NOT EXISTS edits(
  shot_id TEXT PRIMARY KEY, sidecar_rev INTEGER NOT NULL, updated_at TEXT, crop_json TEXT);
CREATE INDEX IF NOT EXISTS shots_date ON shots(date, captured_at, name);
"""


def make_shot_id(dng_sha: str | None, jpg_sha: str | None) -> str:
    return hashlib.sha256(f"{dng_sha or ''}\n{jpg_sha or ''}".encode()).hexdigest()[:16]


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


@dataclass
class FileRecord:
    kind: str
    relpath: str
    sha256: str
    size: int
    mtime_ns: int


@dataclass
class ShotRecord:
    shot_id: str
    name: str
    folder: str
    date: str | None
    date_source: str
    captured_at: str | None
    unit: str | None
    lens: str | None
    iso: int | None
    orientation: int
    width: int | None
    height: int | None
    added_at: str
    files: list[FileRecord] = field(default_factory=list)
    sidecar_rev: int = 0
    crop: dict[Any, Any] | None = None

    @property
    def has_raw(self) -> bool:
        return any(f.kind == "dng" for f in self.files)

    @property
    def has_jpg(self) -> bool:
        return any(f.kind == "jpg" for f in self.files)


@dataclass
class RebuildReport:
    shots: int = 0
    duplicates: list[str] = field(default_factory=list)
    hashed: int = 0
    mismatches: list[str] = field(default_factory=list)
    seen_restored: int = 0
    sidecars_created: int = 0
    corrupt_backups: list[str] = field(default_factory=list)  # 坏掉的 sidecar 改名留下的备份（相对图库）


class Library:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.studio = self.root / ".studio"
        self.thumbs_dir = self.studio / "thumbs"
        self.staging_dir = self.studio / "staging"
        self.tmp_dir = self.studio / "tmp"  # sidecar 的临时文件；每次打开图库时清空
        self.log_path = self.studio / "import-log.jsonl"
        self.index_path = self.studio / "index.sqlite"
        self.lock = threading.RLock()
        self._db: sqlite3.Connection | None = None
        self.query_log: list[str] | None = None

    # ---- 生命周期 ---------------------------------------------------------
    def open(self) -> "Library":
        self.studio.mkdir(parents=True, exist_ok=True)
        self.thumbs_dir.mkdir(exist_ok=True)
        self.staging_dir.mkdir(exist_ok=True)
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        self.tmp_dir.mkdir(exist_ok=True)
        self._connect()
        return self

    def _connect(self) -> None:
        db = sqlite3.connect(self.index_path, check_same_thread=False, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=NORMAL")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, SCHEMA_VERSION):
            db.close()
            self.index_path.unlink()  # 缓存版本不认识：丢掉重建
            db = sqlite3.connect(self.index_path, check_same_thread=False, isolation_level=None)
            db.row_factory = sqlite3.Row
        db.executescript(_SCHEMA)
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self._db = db

    def close(self) -> None:
        with self.lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    @property
    def db(self) -> sqlite3.Connection:
        assert self._db is not None, "Library.open() 还没调用"  # nosec B101  打开图库之前没有连接，这是编程错误不是请求校验
        return self._db

    def trace_queries(self) -> list[str]:
        """测试用：之后每条 SQL 追加进返回的列表。用来抓住按张查询。"""
        log: list[str] = []
        self.query_log = log
        self.db.set_trace_callback(log.append)
        return log

    def clean_staging(self) -> int:
        """删掉上次中断留下的 .part。只在没有导入在跑时调用。"""
        n = 0
        for p in self.staging_dir.glob("*"):
            try:
                p.unlink()
                n += 1
            except OSError:
                pass
        return n

    def abspath(self, relpath: str) -> Path:
        return self.root / relpath

    # ---- 查询 -------------------------------------------------------------
    def sha_present(self, sha: str) -> str | None:
        """图库里已有这份内容（索引里有，磁盘上也在）就返回所属 shot_id。"""
        with self.lock:
            rows = self.db.execute("SELECT shot_id, relpath FROM files WHERE sha256=?", (sha,)).fetchall()
        for r in rows:
            if self.abspath(r["relpath"]).exists():
                return cast(str, r["shot_id"])
        return None

    def file_by_relpath(self, relpath: str) -> sqlite3.Row | None:
        with self.lock:
            return cast(sqlite3.Row | None, self.db.execute("SELECT * FROM files WHERE relpath=?", (relpath,)).fetchone())

    def seen_hit(self, card_key: str, relpath: str, size: int, mtime_ns: int) -> bool:
        with self.lock:
            row = self.db.execute(
                "SELECT 1 FROM seen WHERE card_key=? AND relpath=? AND size=? AND mtime_ns=?",
                (card_key, relpath, size, mtime_ns),
            ).fetchone()
        return row is not None

    def seen_sha(self, card_key: str, relpath: str, size: int, mtime_ns: int) -> str | None:
        with self.lock:
            row = self.db.execute(
                "SELECT sha256 FROM seen WHERE card_key=? AND relpath=? AND size=? AND mtime_ns=?",
                (card_key, relpath, size, mtime_ns),
            ).fetchone()
        return cast(str, row["sha256"]) if row else None

    def count(self) -> int:
        with self.lock:
            return int(self.db.execute("SELECT COUNT(*) FROM shots").fetchone()[0])

    def dates(self) -> list[dict[Any, Any]]:
        with self.lock:
            rows = self.db.execute(
                """WITH grouped AS (
                     SELECT COALESCE(date,'unknown') AS d, COUNT(*) AS n, SUM(has_raw) AS raws,
                            SUM(has_jpg) AS jpgs, MIN(shot_id) AS any_id
                     FROM shots GROUP BY d)
                   SELECT g.d AS d, g.n AS n, g.raws AS raws, g.jpgs AS jpgs, g.any_id AS any_id,
                          COALESCE(
                            (SELECT s.shot_id FROM shots s
                             WHERE COALESCE(s.date,'unknown')=g.d
                             ORDER BY s.captured_at, s.name LIMIT 1),
                            g.any_id) AS cover_id
                   FROM grouped g
                   ORDER BY (g.d='unknown'), g.d DESC"""
            ).fetchall()
        return [
            {
                "date": r["d"],
                "count": r["n"],
                "raw_count": r["raws"],
                "jpg_count": r["jpgs"],
                "cover_id": r["cover_id"],
            }
            for r in rows
        ]

    def _rows_to_shots(self, rows: list[sqlite3.Row]) -> list[dict[Any, Any]]:
        if not rows:
            return []
        ids = [r["shot_id"] for r in rows]
        marks = ",".join("?" * len(ids))
        files_by: dict[str, dict[Any, Any]] = {shot_id: {} for shot_id in ids}
        for f in self.db.execute(
            f"SELECT shot_id, kind, relpath, sha256, size FROM files WHERE shot_id IN ({marks})",  # nosec B608  marks 只是问号占位，值在参数里
            ids,
        ):
            files_by[f["shot_id"]][f["kind"]] = {
                "kind": f["kind"],
                "relpath": f["relpath"],
                "sha256": f["sha256"],
                "size": f["size"],
            }
        edits = {
            e["shot_id"]: e
            for e in self.db.execute(
                f"SELECT shot_id, sidecar_rev, crop_json FROM edits WHERE shot_id IN ({marks})",  # nosec B608  同上，只拼接占位符个数
                ids,
            )
        }
        out = []
        for r in rows:
            d = dict(r)
            d["files"] = files_by.get(r["shot_id"], {})
            edit = edits.get(r["shot_id"])
            d["sidecar_rev"] = edit["sidecar_rev"] if edit else 0
            d["crop"] = json.loads(edit["crop_json"]) if edit and edit["crop_json"] else None
            out.append(d)
        return out

    def list_shots(self, date: str | None = None, limit: int = 500, offset: int = 0) -> list[dict[Any, Any]]:
        sql = "SELECT * FROM shots"
        args: list[object] = []
        if date == "unknown":
            sql += " WHERE date IS NULL"
        elif date:
            sql += " WHERE date=?"
            args.append(date)
        sql += " ORDER BY captured_at, name LIMIT ? OFFSET ?"
        args += [limit, offset]
        with self.lock:
            return self._rows_to_shots(self.db.execute(sql, args).fetchall())

    def get_shot(self, shot_id: str) -> dict[Any, Any] | None:
        with self.lock:
            rows = self.db.execute("SELECT * FROM shots WHERE shot_id=?", (shot_id,)).fetchall()
            return self._rows_to_shots(rows)[0] if rows else None

    # ---- 写索引 -----------------------------------------------------------
    def commit_shot(self, rec: ShotRecord, seen_rows: list[tuple[Any, ...]] | tuple[Any, ...] = ()) -> None:
        """一个事务：shots + files + edits + seen。"""
        with self.lock:
            db = self.db
            db.execute("BEGIN")
            try:
                self._insert_shot(rec)
                db.executemany(
                    "INSERT OR REPLACE INTO seen(card_key, relpath, size, mtime_ns, sha256) VALUES(?,?,?,?,?)",
                    list(seen_rows),
                )
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def replace_shot(self, old_shot_id: str, rec: ShotRecord, seen_rows: list[tuple[Any, ...]] | tuple[Any, ...] = ()) -> None:
        """一个事务：用 `rec` 换掉旧的那张（另一半补上之后照片的 id 会变）。"""
        with self.lock:
            db = self.db
            db.execute("BEGIN")
            try:
                for table in ("shots", "files", "edits"):
                    db.execute(f"DELETE FROM {table} WHERE shot_id=?", (old_shot_id,))  # nosec B608  table 来自上面的固定元组
                self._insert_shot(rec)
                db.executemany(
                    "INSERT OR REPLACE INTO seen(card_key, relpath, size, mtime_ns, sha256) VALUES(?,?,?,?,?)",
                    list(seen_rows),
                )
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def _insert_shot(self, rec: ShotRecord) -> None:
        db = self.db
        db.execute(
            """INSERT OR REPLACE INTO shots(shot_id,name,folder,date,date_source,captured_at,unit,lens,iso,
                 orientation,width,height,has_raw,has_jpg,added_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                rec.shot_id,
                rec.name,
                rec.folder,
                rec.date,
                rec.date_source,
                rec.captured_at,
                rec.unit,
                rec.lens,
                rec.iso,
                rec.orientation,
                rec.width,
                rec.height,
                int(rec.has_raw),
                int(rec.has_jpg),
                rec.added_at,
            ),
        )
        db.execute("DELETE FROM files WHERE shot_id=?", (rec.shot_id,))
        db.executemany(
            "INSERT INTO files(shot_id,kind,relpath,sha256,size,mtime_ns) VALUES(?,?,?,?,?,?)",
            [(rec.shot_id, f.kind, f.relpath, f.sha256, f.size, f.mtime_ns) for f in rec.files],
        )
        db.execute(
            "INSERT OR REPLACE INTO edits(shot_id,sidecar_rev,updated_at,crop_json) VALUES(?,?,?,?)",
            (
                rec.shot_id,
                rec.sidecar_rev,
                datetime.now().isoformat(timespec="seconds"),
                json.dumps(rec.crop) if rec.crop else None,
            ),
        )

    def add_seen(self, rows: list[tuple[Any, ...]]) -> None:
        with self.lock:
            self.db.executemany("INSERT OR REPLACE INTO seen(card_key, relpath, size, mtime_ns, sha256) VALUES(?,?,?,?,?)", rows)

    def set_edit_cache(self, shot_id: str, rev: int, crop: dict[Any, Any] | None) -> None:
        with self.lock:
            self.db.execute(
                "INSERT OR REPLACE INTO edits(shot_id,sidecar_rev,updated_at,crop_json) VALUES(?,?,?,?)",
                (shot_id, rev, datetime.now().isoformat(timespec="seconds"), json.dumps(crop) if crop else None),
            )

    def sidecar_path_for(self, shot: dict[Any, Any]) -> Path:
        first = next(iter(shot["files"].values()))
        return sc.sidecar_path(self.abspath(first["relpath"]))

    # ---- sidecar 坏了/没了：退回"没有编辑" ------------------------------------
    def sidecar_problem(self, shot: dict[Any, Any]) -> str | None:
        """这张照片的编辑记录读不出来（损坏或被删）就返回一句白话，否则 None。不写任何东西。"""
        try:
            sc.read(self.sidecar_path_for(shot))
        except sc.SidecarCorrupt:
            return SIDECAR_RESET_NOTE
        return None

    def fresh_sidecar(self, shot: dict[Any, Any]) -> dict[Any, Any]:
        """按索引里的信息重新造一份"没有编辑"的 sidecar（只在内存里）。"""
        files = shot["files"]
        dng, jpg = files.get("dng"), files.get("jpg")
        return sc.new_sidecar(
            dng=Path(dng["relpath"]).name if dng else None,
            jpg=Path(jpg["relpath"]).name if jpg else None,
            dng_sha256=dng["sha256"] if dng else None,
            jpg_sha256=jpg["sha256"] if jpg else None,
            orientation=shot.get("orientation") or 1,
            width=shot.get("width"),
            height=shot.get("height"),
            imported={
                "shot_id": shot["shot_id"],
                "date": shot.get("date"),
                "date_source": "reset",
                "captured_at": shot.get("captured_at"),
                "added_at": shot.get("added_at"),
                "sizes": {k: v["size"] for k, v in files.items()},
            },
        )

    def read_sidecar_or_default(self, shot: dict[Any, Any]) -> tuple[dict[Any, Any], str | None]:
        """读用：sidecar 读不出来就给"没有编辑"的默认内容和一句白话，不改磁盘。"""
        with self.lock:
            try:
                return sc.read(self.sidecar_path_for(shot)), None
            except sc.SidecarCorrupt:
                return self.fresh_sidecar(shot), SIDECAR_RESET_NOTE

    def read_sidecar_for_write(self, shot: dict[Any, Any]) -> dict[Any, Any]:
        """写用：读不出来就把坏的改名留着（`.corrupt-<时间>`），重新建一份"没有编辑"的再往下写。"""
        path = self.sidecar_path_for(shot)
        with self.lock:
            try:
                return sc.read(path)
            except sc.SidecarCorrupt:
                if path.exists():
                    sc.backup_corrupt(path)
                return sc.write_atomic(path, self.fresh_sidecar(shot), bump=False, tmp_dir=self.tmp_dir)

    # ---- 日志 -------------------------------------------------------------
    def log(self, entry: dict[Any, Any]) -> None:
        entry = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"), **entry}
        line = json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n"
        with self.lock, open(self.log_path, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())

    def read_log(self) -> list[dict[Any, Any]]:
        if not self.log_path.exists():
            return []
        out = []
        with open(self.log_path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue  # 被打断的最后一行
        return out

    # ---- 重建 -------------------------------------------------------------
    def rebuild(self, verify: bool = False) -> RebuildReport:
        """丢掉索引，按目录和 sidecar 重建；seen 从 import-log 恢复。

        默认信任 sidecar 里记录的哈希（大小不符时才重新算）；verify=True 时全部重算并报告不一致。
        """
        report = RebuildReport()
        groups: dict[tuple[str, str], dict[str, Path]] = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            rel_dir = Path(dirpath).relative_to(self.root)
            if rel_dir.parts[:1] in ((".studio",), (BACKUP_DIR,)):
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames if d != ".studio" and d != BACKUP_DIR and not d.startswith(".")]
            for fn in filenames:
                if fn.startswith("."):
                    continue
                kind = MEDIA_EXT.get(os.path.splitext(fn)[1].lower())
                if kind:
                    p = Path(dirpath) / fn
                    groups.setdefault((str(rel_dir), p.stem.lower()), {})[kind] = p
        with self.lock:
            self.db.execute("BEGIN")
            try:
                for table in ("shots", "files", "edits", "seen"):
                    self.db.execute(f"DELETE FROM {table}")  # nosec B608  table 来自上面的固定元组
                for (folder, _), media in sorted(groups.items()):
                    self._rebuild_group(folder, media, verify, report)
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            rows = []
            for e in self.read_log():
                for f in e.get("files", []) if e.get("type") == "shot" else []:
                    if f.get("card_key") and f.get("card_relpath"):
                        rows.append((f["card_key"], f["card_relpath"], f["card_size"], f["card_mtime_ns"], f["sha256"]))
            self.add_seen(rows)
            report.seen_restored = len(rows)
        return report

    def _rebuild_group(self, rel_dir: str, media: dict[str, Path], verify: bool, report: RebuildReport) -> None:
        any_path = next(iter(media.values()))
        sc_path = sc.sidecar_path(any_path)
        side: dict[Any, Any] | None = None
        if sc_path.exists():
            try:
                side = sc.read(sc_path)
            except sc.SidecarCorrupt:
                side = None
                kept = sc.backup_corrupt(sc_path)  # 下面会写一份新的：先把坏的留着，别悄悄覆盖
                report.corrupt_backups.append(kept.relative_to(self.root).as_posix())
        known = (side or {}).get("source", {})
        files: list[FileRecord] = []
        for kind in ("dng", "jpg"):
            p = media.get(kind)
            if p is None:
                continue
            st = p.stat()
            sha = known.get(f"{kind}_sha256")
            trusted = sha and known.get(kind) == p.name and not verify
            recorded_size = ((side or {}).get("import", {}).get("sizes") or {}).get(kind)
            if trusted and recorded_size is not None and recorded_size != st.st_size:
                trusted = False
            if not trusted:
                actual = sha256_file(p)
                report.hashed += 1
                if sha and actual != sha:
                    report.mismatches.append(p.relative_to(self.root).as_posix())
                sha = actual
            files.append(FileRecord(kind, p.relative_to(self.root).as_posix(), sha, st.st_size, st.st_mtime_ns))
        dng_sha = next((f.sha256 for f in files if f.kind == "dng"), None)
        jpg_sha = next((f.sha256 for f in files if f.kind == "jpg"), None)
        shot_id = make_shot_id(dng_sha, jpg_sha)
        if self.db.execute("SELECT 1 FROM shots WHERE shot_id=?", (shot_id,)).fetchone():
            report.duplicates.append(rel_dir + "/" + any_path.stem)
            return
        # 元数据：JPG 优先
        jpg = media.get("jpg")
        dng = media.get("dng")
        pm_dng = metamod.read_dng_meta(dng) if dng else metamod.PhotoMeta()
        pm = metamod.read_jpeg_meta(jpg) if jpg else pm_dng
        if pm.error:
            pm = pm_dng
        imp = (side or {}).get("import", {})
        folder_date = Path(rel_dir).name if _DATE_DIR.match(Path(rel_dir).name) else None
        date = imp.get("date", folder_date)
        if side is None:
            taken = pm.taken or pm_dng.taken
            date = folder_date or (taken.date().isoformat() if taken else None)
            imp = {
                "date": date,
                "date_source": "rebuild",
                "captured_at": taken.isoformat() if taken else None,
                "added_at": datetime.now().isoformat(timespec="seconds"),
                "shot_id": shot_id,
            }
            side = sc.new_sidecar(
                dng=dng.name if dng else None,
                jpg=jpg.name if jpg else None,
                dng_sha256=dng_sha,
                jpg_sha256=jpg_sha,
                orientation=pm.orientation,
                width=pm.width or pm_dng.width,
                height=pm.height or pm_dng.height,
                imported=imp,
            )
            sc.write_atomic(sc_path, side, bump=False, tmp_dir=self.tmp_dir)
            report.sidecars_created += 1
        frame = side.get("frame", {})
        crop = side.get("crop")
        rec = ShotRecord(
            shot_id=shot_id,
            name=any_path.stem,
            folder=Path(rel_dir).as_posix(),
            date=date,
            date_source=imp.get("date_source", "rebuild"),
            captured_at=imp.get("captured_at") or (pm.taken.isoformat() if pm.taken else None),
            unit=metamod.unit_from_lens(pm.lens or pm_dng.lens),
            lens=pm.lens or pm_dng.lens,
            iso=pm.iso or pm_dng.iso,
            orientation=frame.get("orientation", pm.orientation),
            width=frame.get("width"),
            height=frame.get("height"),
            added_at=imp.get("added_at") or datetime.now().isoformat(timespec="seconds"),
            files=files,
            sidecar_rev=int(side.get("rev", 0)),
            crop=crop,
        )
        self._insert_shot(rec)
        report.shots += 1
