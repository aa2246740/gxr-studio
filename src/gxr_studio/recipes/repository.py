"""长期完整记录、证据、终态和名称的唯一写入者。

SQLite事务在内存中准备，经PrivateDir原子发布整个数据库。这样SQLite不会
绕开私有目录句柄另开journal。短事务由进程锁保护，失败时磁盘保留上次提交。
计算不持锁；输入资产及所有已引用版本均不自动回收。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import osplat
from ..privacy import PrivateDir
from ..render.params import UNITS, RecipeSel
from .environment import RenderEnvironment
from .history import HistoricalQuality, verify_historical
from .record import check_record
from .types import FitProduct, Quality, RecipeError, ResolvedRecipe, canonical, digest, require_digest

_SCHEMA = """
CREATE TABLE objects(kind TEXT NOT NULL, sha TEXT NOT NULL, data BLOB NOT NULL, PRIMARY KEY(kind,sha));
CREATE TABLE versions(id TEXT PRIMARY KEY, unit TEXT NOT NULL, slot INTEGER NOT NULL, document BLOB NOT NULL);
CREATE TABLE runs(id TEXT PRIMARY KEY, context TEXT NOT NULL, state TEXT NOT NULL, version TEXT, detail BLOB NOT NULL,
                  metadata BLOB NOT NULL, reported INTEGER NOT NULL DEFAULT 0);
CREATE TABLE events(run TEXT NOT NULL, ordinal INTEGER NOT NULL, state TEXT NOT NULL, detail BLOB NOT NULL,
                    PRIMARY KEY(run,ordinal));
CREATE TABLE names(id TEXT PRIMARY KEY REFERENCES versions(id), run TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                   initial_name TEXT NOT NULL, revision INTEGER NOT NULL, hidden INTEGER NOT NULL, created TEXT NOT NULL);
CREATE TABLE style_heads(style TEXT NOT NULL, unit TEXT NOT NULL, version TEXT NOT NULL REFERENCES versions(id), PRIMARY KEY(style,unit));
PRAGMA user_version=1;
"""


@dataclass(frozen=True)
class _LoadedVersion:
    identity: str
    document: dict[str, Any]
    record: bytes
    environment: RenderEnvironment
    source: dict[str, Any]
    quality: Quality | HistoricalQuality


def _verify_historical_bundle(
    unit: str,
    slot: int,
    context: str,
    record_sha: str,
    source_doc: dict[str, Any],
    receipt_sha: str,
    receipt_doc: dict[str, Any],
    quality: HistoricalQuality,
) -> None:
    reg = receipt_doc.get("registry_record")
    if (
        quality.unit != unit
        or quality.record_sha256 != record_sha
        or source_doc != {"kind": "historical", "registry_id": quality.registry_id}
        or context != receipt_sha
        or not isinstance(reg, dict)
        or reg.get("id") != quality.registry_id
        or reg.get("record_sha256") != record_sha
        or reg.get("control_binding") != slot - 1
    ):
        raise RecipeError("historical_identity_mismatch", "历史授权记录与来源或证据包不一致。")


def _verify_fit_bundle(
    context: str,
    record_sha: str,
    environment_sha: str,
    source_doc: dict[str, Any],
    receipt_sha: str,
    receipt_doc: dict[str, Any],
    quality: Quality,
) -> None:
    from .admission import _exact, required_checks
    from .fit import SOLVER_CONTRACT

    if source_doc.get("kind") == "historical":
        raise RecipeError("bad_fit_quality", "新拟合不能伪造历史授权来源。")
    if "cube_sha256" in source_doc:
        require_digest(source_doc["cube_sha256"])
        target = source_doc.get("target")
        if isinstance(target, dict) and "cube_sha256" in target and target["cube_sha256"] != source_doc["cube_sha256"]:
            raise RecipeError("binding_mismatch", "完整配方的来源与目标摘要不一致。")
    if "contract" in receipt_doc:
        if (
            receipt_doc.get("contract") != SOLVER_CONTRACT
            or receipt_doc.get("record_sha256") != record_sha
            or not isinstance(receipt_doc.get("plan"), dict)
        ):
            raise RecipeError("binding_mismatch", "求解回执与持久记录或契约不一致。")
        require_digest(receipt_doc.get("training_identity", ""))
    if quality.binding is not None:
        target = source_doc.get("target")
        plan = receipt_doc.get("plan")
        details = {c.key: json.loads(c.detail) for c in quality.checks}
        if (
            quality.binding.record_sha256 != record_sha
            or quality.binding.environment_sha256 != environment_sha
            or quality.binding.context_sha256 != context
            or quality.binding.receipt_sha256 != receipt_sha
            or not isinstance(target, dict)
            or (target.get("kind") == "lut" and source_doc.get("cube_sha256") != target.get("cube_sha256"))
            or quality.binding.target_sha256 != digest(canonical(target))
            or not isinstance(plan, dict)
            or receipt_doc.get("contract") != SOLVER_CONTRACT
            or receipt_doc.get("record_sha256") != record_sha
            or receipt_doc.get("source_sha256") != plan.get("source_sha256")
            or quality.binding.plan_sha256 != digest(canonical(plan))
            or ("record" in details and details["record"].get("sha256") != record_sha)
            or (
                "provenance" in details
                and (details["provenance"].get("context"), details["provenance"].get("target")) != (context, quality.binding.target_sha256)
            )
        ):
            raise RecipeError("binding_mismatch", "质量回执的身份绑定与持久记录、来源或回执不一致。")
    if quality.state == "passed":
        if quality.binding is None:
            raise RecipeError("unbound_proof", "准入通过的完整配方必须具备完整身份绑定。")
        if not set(required_checks(("original",))) <= set(quality.required):
            raise RecipeError("incomplete_proof", "准入通过的质量回执缺少必需检查项。")
        successful = {
            a.get("id")
            for a in receipt_doc.get("attempts", [])
            if isinstance(a, dict) and a.get("success") is True and str(a.get("id", "")).startswith("exact/")
        }
        required_keys = ("objective", "baseline_objective", "winner_axis", "winner_vector")
        if (
            receipt_doc.get("contract") != SOLVER_CONTRACT
            or receipt_doc.get("phase") != "exact"
            or receipt_doc.get("record_sha256") != record_sha
            or _exact(receipt_doc)[0] != "pass"
            or receipt_doc.get("winner_attempt") not in successful
            or any(key not in receipt_doc for key in required_keys)
        ):
            raise RecipeError("bad_proof", "准入通过配方的求解回执不完整或未收敛。")


def _verify_bundle(
    *,
    unit: str,
    slot: int,
    context: str,
    style: str | None,
    replacement: str | None,
    record: bytes,
    environment_bytes: bytes,
    source_bytes: bytes,
    quality: Quality | HistoricalQuality,
    receipt_bytes: bytes,
) -> tuple[RenderEnvironment, dict[str, Any]]:
    check_record(record)
    environment = RenderEnvironment(environment_bytes)
    environment.validate()
    if unit != environment.unit or unit not in UNITS or isinstance(slot, bool) or not isinstance(slot, int) or not 1 <= slot <= 8:
        raise RecipeError("binding_mismatch", "配方与单元或控制绑定不一致。")
    if (record[1] == 1) != (slot == 3):
        raise RecipeError("binding_mismatch", "完整黑白记录必须绑定含Filt的原生03控制。")
    require_digest(context)
    if style is not None:
        require_digest(style)
    if replacement is not None:
        require_digest(replacement)
    record_sha = digest(record)
    # 验证默认解析不会改变被评估记录，且模板控制底子实际存在。
    environment.apply(record, RecipeSel(unit, slot, kind="local", id=record_sha))
    try:
        source_doc = json.loads(source_bytes)
        receipt_doc = json.loads(receipt_bytes)
        canonical(source_doc)
        canonical(receipt_doc)
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise RecipeError("bad_proof", "完整配方的来源或求解回执格式无效。") from exc
    if not isinstance(source_doc, dict) or not isinstance(receipt_doc, dict):
        raise RecipeError("bad_proof", "完整配方的来源或求解回执结构无效。")
    receipt_sha = digest(receipt_bytes)
    if isinstance(quality, HistoricalQuality):
        _verify_historical_bundle(unit, slot, context, record_sha, source_doc, receipt_sha, receipt_doc, quality)
    else:
        _verify_fit_bundle(context, record_sha, environment.identity, source_doc, receipt_sha, receipt_doc, quality)
    return environment, source_doc


_VERSION_KEYS = {"unit", "slot", "context", "style", "replacement", "record", "environment", "source", "quality", "receipt"}


class RecipeRepository:
    def __init__(self, home: Path, forbidden: tuple[Path, ...] = ()) -> None:
        self.home = Path(home)
        self.root = Path(home) / "recipes"
        self.forbidden = forbidden
        self._lock = threading.RLock()

    @contextmanager
    def _transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        with self._lock, PrivateDir(self.home, "完整配方", self.forbidden) as parent, parent.child("recipes") as directory:
            try:
                directory.write("writer.lock", b"1")
            except FileExistsError:
                pass
            fd = osplat.open_file_at(directory.fd, "writer.lock")
            try:
                try:
                    osplat.lock_exclusive_nb(fd)
                except OSError as exc:
                    raise RecipeError("recipe_busy", "完整配方正在保存，请稍后重试。") from exc
                db = sqlite3.connect(":memory:")
                db.row_factory = sqlite3.Row
                try:
                    if directory.exists("recipes.sqlite"):
                        db.deserialize(directory.read("recipes.sqlite"))
                        if db.execute("PRAGMA user_version").fetchone()[0] != 1:
                            raise RecipeError("unsupported_repository", "完整配方数据库版本无法识别。")
                    else:
                        db.executescript(_SCHEMA)
                    db.execute("PRAGMA foreign_keys=ON")
                    db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                    yield db
                    db.commit()
                    if write:
                        directory.replace_file("recipes.sqlite", db.serialize())
                finally:
                    db.close()
            finally:
                osplat.unlock(fd)
                os.close(fd)

    @staticmethod
    def _put(db: sqlite3.Connection, kind: str, data: bytes) -> str:
        identity = digest(data)
        db.execute("INSERT OR IGNORE INTO objects VALUES (?,?,?)", (kind, identity, data))
        return identity

    @staticmethod
    def _get(db: sqlite3.Connection, kind: str, identity: str) -> bytes:
        row = db.execute("SELECT data FROM objects WHERE kind=? AND sha=?", (kind, require_digest(identity))).fetchone()
        if row is None or digest(bytes(row[0])) != identity:
            raise RecipeError("recipe_asset_missing", "这份完整配方的依赖缺失或已损坏。")
        return bytes(row[0])

    @staticmethod
    def _run_id(run: str) -> None:
        if re.fullmatch(r"[0-9a-f]{12}", run) is None:
            raise RecipeError("bad_run", "生成任务无法识别。")

    def start_run(self, run: str, context: str, metadata: dict[str, Any] | None = None) -> None:
        self._run_id(run)
        require_digest(context)
        with self._transaction(write=True) as db:
            old = db.execute("SELECT context FROM runs WHERE id=?", (run,)).fetchone()
            if old is not None and old[0] != context:
                raise RecipeError("run_conflict", "生成任务的输入身份冲突。")
            db.execute(
                "INSERT OR IGNORE INTO runs(id,context,state,detail,metadata) VALUES (?,?,'running',?,?)",
                (run, context, b"{}", canonical(metadata or {})),
            )

    @staticmethod
    def _event(db: sqlite3.Connection, run: str, state: str, detail: bytes) -> None:
        ordinal = db.execute("SELECT count(*) FROM events WHERE run=?", (run,)).fetchone()[0]
        db.execute("INSERT INTO events VALUES (?,?,?,?)", (run, ordinal, state, detail))

    def finish_run(self, run: str, state: str, detail: dict[str, Any]) -> dict[str, Any]:
        if state not in ("cancelled", "error", "interrupted"):
            raise RecipeError("bad_terminal", "生成任务的终态无效。")
        with self._transaction(write=True) as db:
            row = db.execute("SELECT state FROM runs WHERE id=?", (run,)).fetchone()
            if row is None:
                raise RecipeError("run_not_found", "找不到这次完整配方生成。")
            payload = canonical(detail)
            self._event(db, run, state, payload)
            if row[0] == "running":
                db.execute("UPDATE runs SET state=?,detail=? WHERE id=?", (state, payload, run))
        result = self.run(run)
        if result is None:
            raise RecipeError("run_not_found", "找不到这次完整配方生成。")
        return result

    def retain_fit(self, run: str, product: FitProduct) -> dict[str, Any]:
        if not isinstance(product.quality, Quality):
            raise RecipeError("bad_fit_quality", "新拟合不能使用历史例外。")
        return self._retain(run, product, product.quality)

    def _retain(self, run: str, product: FitProduct, quality: Quality | HistoricalQuality) -> dict[str, Any]:
        _verify_bundle(
            unit=product.unit,
            slot=product.slot,
            context=product.context,
            style=product.style,
            replacement=product.replacement,
            record=product.entry,
            environment_bytes=product.environment,
            source_bytes=product.source,
            quality=quality,
            receipt_bytes=product.receipt,
        )
        with self._transaction(write=True) as db:
            row = db.execute("SELECT context,state,version FROM runs WHERE id=?", (run,)).fetchone()
            if row is None or row[0] != product.context:
                raise RecipeError("run_conflict", "生成结果与启动时的输入不一致。")
            document = {
                "unit": product.unit,
                "slot": product.slot,
                "context": product.context,
                "style": product.style,
                "replacement": product.replacement,
                "record": self._put(db, "record", product.entry),
                "environment": self._put(db, "environment", product.environment),
                "source": self._put(db, "source", product.source),
                "quality": self._put(db, "quality", canonical(quality.to_json())),
                "receipt": self._put(db, "receipt", product.receipt),
            }
            payload = canonical(document)
            identity = digest(payload)
            self._event(db, run, "done", canonical({"version": identity}))
            if row[1] == "done" and row[2] != identity:
                raise RecipeError("result_conflict", "这次生成已封存了不同结果。")
            if row[1] in ("running", "done"):
                db.execute("INSERT OR IGNORE INTO versions VALUES (?,?,?,?)", (identity, product.unit, product.slot, payload))
                db.execute("UPDATE runs SET state='done',version=?,detail=? WHERE id=?", (identity, b"{}", run))
        result = self.run(run)
        if result is None:
            raise RecipeError("run_not_found", "找不到这次完整配方生成。")
        return result

    def import_historical(
        self,
        entry: bytes,
        environment: RenderEnvironment,
        position: int,
        registry_id: str,
        *,
        authorization: bytes,
        equivalence: bytes,
        registry: bytes,
        name: str,
    ) -> dict[str, Any]:
        quality = verify_historical(
            entry, environment.unit, position, registry_id, authorization=authorization, equivalence=equivalence, registry=registry
        )
        table = json.loads(registry)
        record = next(r for r in table["records"] if r["id"] == registry_id)
        evidence = canonical({"authorization": json.loads(authorization), "equivalence": json.loads(equivalence), "registry_record": record})
        context = digest(evidence)
        run = digest(canonical({"registry_id": registry_id, "environment": environment.identity, "context": context}))[:12]
        self.start_run(run, context, {"unit": environment.unit, "historical_import": True})
        marker = Quality(("original_admission",), ())
        product = FitProduct(
            environment.unit,
            record["control_binding"] + 1,
            entry,
            environment.data,
            context,
            canonical({"kind": "historical", "registry_id": registry_id}),
            marker,
            evidence,
        )
        self._retain(run, product, quality)
        return self.publish(run, name=name)

    @staticmethod
    def _quality(data: bytes) -> Quality | HistoricalQuality:
        value = json.loads(data)
        return HistoricalQuality.from_json(value) if value.get("schema") == "gxr-studio/historical-quality/1" else Quality.from_json(value)

    def run(self, run: str) -> dict[str, Any] | None:
        with self._transaction() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run,)).fetchone()
            if row is None:
                return None
            result = {
                "run": run,
                "context": row["context"],
                "state": row["state"],
                "detail": json.loads(row["detail"]),
                "metadata": json.loads(row["metadata"]),
            }
            if row["version"] is not None:
                result.update(self._view(db, row["version"]))
            return result

    def claim_failure_report(self, run: str) -> bool:
        with self._transaction(write=True) as db:
            result = db.execute("UPDATE runs SET reported=1 WHERE id=? AND reported=0 AND state IN ('error','interrupted')", (run,))
            return result.rowcount == 1

    def publish(self, run: str, *, name: str) -> dict[str, Any]:
        name = self._name(name)
        with self._transaction(write=True) as db:
            row = db.execute("SELECT state,version FROM runs WHERE id=?", (run,)).fetchone()
            if row is None or row[0] != "done":
                raise RecipeError("fit_not_done", "无法保存配方，这次生成没有完整结果。")
            existing = db.execute("SELECT initial_name FROM names WHERE run=?", (run,)).fetchone()
            if existing is not None and existing[0] != name:
                raise RecipeError("publication_conflict", "这次生成已保存，请用改名修改名称。")
            loaded = self._load_version(db, row[1])
            version = loaded.document
            if existing is None and version["style"] is not None:
                head = db.execute("SELECT version FROM style_heads WHERE style=? AND unit=?", (version["style"], version["unit"])).fetchone()
                if (head[0] if head is not None else None) != version["replacement"]:
                    raise RecipeError("replacement_changed", "同风格配方已更新，请重新拟合；这次结果仍保留。")
                db.execute("INSERT OR REPLACE INTO style_heads VALUES (?,?,?)", (version["style"], version["unit"], loaded.identity))
            db.execute(
                "INSERT OR IGNORE INTO names VALUES (?,?,?,?,1,0,?)",
                (loaded.identity, run, name, name, time.strftime("%Y-%m-%dT%H:%M:%S%z")),
            )
            return self._view(db, loaded.identity, loaded=loaded)

    def reference_for_style(self, style: str, unit: str) -> tuple[str, bytes] | None:
        with self._transaction() as db:
            row = db.execute("SELECT version FROM style_heads WHERE style=? AND unit=?", (require_digest(style), unit)).fetchone()
            if row is None:
                return None
            loaded = self._load_version(db, row[0])
            return loaded.identity, loaded.record

    @staticmethod
    def _name(name: str) -> str:
        name = re.sub(r"\s+", " ", name).strip()
        if not 1 <= len(name) <= 40 or any(ord(c) < 32 or ord(c) == 127 for c in name):
            raise RecipeError("bad_name", "配方名称需为1到40个字符。")
        return name

    @staticmethod
    def _version(db: sqlite3.Connection, identity: str) -> dict[str, Any]:
        row = db.execute("SELECT unit,slot,document FROM versions WHERE id=?", (require_digest(identity),)).fetchone()
        if row is None or digest(bytes(row["document"])) != identity:
            raise RecipeError("recipe_not_found", "找不到这份完整配方，请重新选择。")
        doc: dict[str, Any] = json.loads(row["document"])
        if not isinstance(doc, dict) or set(doc) != _VERSION_KEYS or (doc["unit"], doc["slot"]) != (row["unit"], row["slot"]):
            raise RecipeError("binding_mismatch", "完整配方的持久版本清单不一致。")
        return doc

    def _load_version(self, db: sqlite3.Connection, identity: str) -> _LoadedVersion:
        doc = self._version(db, identity)
        record = self._get(db, "record", doc["record"])
        environment_bytes = self._get(db, "environment", doc["environment"])
        source_bytes = self._get(db, "source", doc["source"])
        quality_bytes = self._get(db, "quality", doc["quality"])
        receipt_bytes = self._get(db, "receipt", doc["receipt"])
        quality = self._quality(quality_bytes)
        if canonical(quality.to_json()) != quality_bytes:
            raise RecipeError("bad_proof", "完整配方的质量回执编码已损坏。")
        environment, source_doc = _verify_bundle(
            unit=doc["unit"],
            slot=doc["slot"],
            context=doc["context"],
            style=doc["style"],
            replacement=doc["replacement"],
            record=record,
            environment_bytes=environment_bytes,
            source_bytes=source_bytes,
            quality=quality,
            receipt_bytes=receipt_bytes,
        )
        return _LoadedVersion(identity, doc, record, environment, source_doc, quality)

    def _view(self, db: sqlite3.Connection, identity: str, *, loaded: _LoadedVersion | None = None) -> dict[str, Any]:
        verified = loaded if loaded is not None else self._load_version(db, identity)
        doc = verified.document
        quality = verified.quality
        alias = db.execute("SELECT * FROM names WHERE id=?", (identity,)).fetchone()
        nominal = next((c for c in quality.checks if c.key == "nominal/original"), None) if isinstance(quality, Quality) else None
        scores = json.loads(nominal.detail).get("groups", {}).get("photo_iso200", {}) if nominal else {}
        return {
            "schema": "gxr-studio/full-recipe/1",
            "id": identity,
            "name": alias["name"] if alias else None,
            "revision": alias["revision"] if alias else 0,
            "hidden": bool(alias["hidden"]) if alias else False,
            "created": alias["created"] if alias else None,
            "recipe": RecipeSel(doc["unit"], doc["slot"], kind="local", id=identity).to_json(),
            "record_sha256": doc["record"],
            "binding_sha256": doc["environment"],
            "quality": quality.summary(),
            "controls": verified.environment.controls(doc["slot"]),
            "source": verified.source,
            "fit": {
                "prediction": True,
                "label": "电脑预测",
                "similarity": scores.get("candidate", {}).get("similarity_percent"),
                "holdout": scores.get("candidate"),
                "baseline_holdout": scores.get("current"),
            },
            "camera_write": {"status": "not_supported_yet", "note": "仅供本机使用，暂不支持写入存储卡"},
        }

    def get(self, identity: str) -> dict[str, Any]:
        with self._transaction() as db:
            return self._view(db, identity)

    def listing(self) -> list[dict[str, Any]]:
        if not self.root.exists() and not self.root.is_symlink():
            return []
        with self._transaction() as db:
            return [self._view(db, r[0]) for r in db.execute("SELECT id FROM names WHERE hidden=0 ORDER BY created,id").fetchall()]

    def rename(self, identity: str, *, name: str, expected_revision: int) -> dict[str, Any]:
        return self._change(identity, expected_revision, name=self._name(name))

    def retire(self, identity: str, *, expected_revision: int) -> None:
        self._change(identity, expected_revision, hidden=True)

    def _change(self, identity: str, revision: int, *, name: str | None = None, hidden: bool | None = None) -> dict[str, Any]:
        with self._transaction(write=True) as db:
            loaded = self._load_version(db, identity)
            result = db.execute(
                "UPDATE names SET name=coalesce(?,name),hidden=coalesce(?,hidden),revision=revision+1 WHERE id=? AND revision=?",
                (name, None if hidden is None else int(hidden), loaded.identity, revision),
            )
            if result.rowcount != 1:
                raise RecipeError("recipe_revision_conflict", "配方已被修改，请刷新后重试。")
            return self._view(db, loaded.identity, loaded=loaded)

    def resolve(self, selection: RecipeSel, *, photo_unit: str | None = None, iso: int = 200) -> ResolvedRecipe:
        with self._transaction() as db:
            loaded = self._load_version(db, selection.id or "")
            doc = loaded.document
            if selection.kind != "local" or (doc["unit"], doc["slot"]) != (selection.unit, selection.slot):
                raise RecipeError("binding_mismatch", "完整配方引用与单元或控制绑定不一致。")
            if photo_unit is not None and photo_unit != doc["unit"]:
                raise RecipeError("unit_mismatch", "请选择同一镜头单元的完整配方。")
            record = loaded.record
            environment = loaded.environment
            quality = loaded.quality
        entry, levels = environment.apply(record, selection)
        model, branch, calibrated = environment.model_for(iso)
        return ResolvedRecipe(
            selection,
            entry,
            doc["record"],
            digest(entry),
            environment.identity,
            levels,
            model,
            branch,
            calibrated and photo_unit == selection.unit,
            quality,
            entry == record,
        )
