"""本机 LUT 库。原始字节按 SHA 保存，所有写入都经过 PrivateDir。"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from collections import OrderedDict
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any, cast

from ..privacy import PrivateDir
from .cube import MAX_BYTES, Cube
from .errors import LutError
from .identify import IDENTIFICATION_VERSION, identify
from .metadata import FIELDS, export_cube, read_header
from .spaces import SPACES, Target

SCHEMA = "gxr-studio/lut-library/1"
NOT_FOUND = "找不到这个 LUT。请重新导入。"


def durable_write(directory: PrivateDir, name: str, doc: dict[str, Any]) -> None:
    """文件同步后原子改名，再同步父目录；失败向调用者报告。"""
    directory.replace_file(name, (json.dumps(doc, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode())


def label(value: Any, field: str = "LUT 名称") -> str:
    if not isinstance(value, str):
        raise LutError(422, "bad_name", "无法保存。名称无效。")
    value = re.sub(r"\s+", " ", value).strip()
    if not 1 <= len(value) <= 255:
        raise LutError(422, "bad_name", f"{field}需为 1–255 个字符。")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise LutError(422, "bad_name", "无法保存。名称无效。")
    return cast(str, value)


def file_parts(filename: str) -> tuple[str, str | None, bool]:
    # 这是显示数据和分组依据，绝不作为磁盘路径。
    parts = PurePosixPath(filename.replace("\\", "/")).parts
    skip = "__MACOSX" in parts or any(p.startswith("._") for p in parts)
    name = parts[-1] if parts else ""
    group = parts[0] if len(parts) > 1 and parts[0] not in ("/", ".", "..") else None
    return name, group, skip


# 决定 LUT 出图结果的字段和缺省值（与 `LutLibrary.target` 一致）
_RENDER_SPEC = (
    ("input_space", None),
    ("output_space", None),
    ("input_gamut", None),
    ("output_gamut", None),
    ("input_range", "full"),
    ("output_range", "full"),
)


def _token(cube_id: str, item: dict[str, Any]) -> str:
    spec = {key: item.get(key, default) for key, default in _RENDER_SPEC}
    doc = json.dumps({"cube": cube_id, "spec": spec}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(doc.encode()).hexdigest()[:16]


class LutLibrary:
    def __init__(self, home: Path, forbidden: Iterable[Path] = ()) -> None:
        self.home = Path(home)
        self.forbidden = tuple(forbidden)
        self.lock = threading.RLock()
        self._cubes: OrderedDict[str, Cube] = OrderedDict()

    def private(self) -> PrivateDir:
        return PrivateDir(self.home, "LUT", self.forbidden)

    def _read(self, directory: PrivateDir, *, persist: bool = True) -> dict[str, Any]:
        """persist=False：旧格式的升级只在内存里做，不写盘（显卡预览的只读接口用）。正常启动时 `migrate()` 已写好。"""
        if not directory.exists("luts/library.json"):
            return self._legacy(directory, persist=persist)
        try:
            doc: dict[str, Any] = json.loads(directory.read("luts/library.json"))
            if doc["schema"] != SCHEMA or not isinstance(doc["items"], dict) or not isinstance(doc["groups"], list):
                raise ValueError("library schema")
            if isinstance(doc["revision"], bool) or not isinstance(doc["revision"], int) or doc["revision"] < 0:
                raise ValueError("library revision")
            for group in doc["groups"]:
                if not isinstance(group, dict) or not isinstance(group.get("id"), str) or not isinstance(group.get("name"), str):
                    raise ValueError("library group")
            for key, item in doc["items"].items():
                if not re.fullmatch(r"[0-9a-f]{64}", key) or not isinstance(item, dict) or item.get("cube_id") != key:
                    raise ValueError("library item")
                if item["input_space"] not in (*SPACES, None) or item["output_space"] not in SPACES:
                    raise ValueError("library space")
                if (
                    isinstance(item["version"], bool)
                    or not isinstance(item["version"], int)
                    or item["version"] < 1
                    or not isinstance(item["starred"], bool)
                ):
                    raise ValueError("library version")
                if any(not isinstance(item[field], str) for field in ("name", "filename", "title", *FIELDS)):
                    raise ValueError("library metadata")
        except (KeyError, ValueError, TypeError) as exc:
            raise LutError(422, "lut_library_unreadable", "无法读取 LUT 库。请检查应用数据。") from exc
        pending = doc.get("pending_deletes", [])
        if not isinstance(pending, list) or any(not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item) for item in pending):
            raise LutError(422, "lut_library_unreadable", "无法读取 LUT 库。请检查应用数据。")
        upgraded = self._upgrade_attribution(directory, doc)
        upgraded = self._upgrade_identification(directory, doc) or upgraded
        if not persist:
            return doc
        if pending:
            for cube_id in pending:
                self._finish_deleted(directory, cube_id)
            doc["pending_deletes"] = []
        if pending or upgraded:
            self._save(directory, doc)
        return doc

    def _upgrade_attribution(self, directory: PrivateDir, doc: dict[str, Any]) -> bool:
        changed = False
        for cube_id, item in doc["items"].items():
            if item.get("attribution_version") == 2:
                continue
            text = self._raw(directory, cube_id).decode("utf-8-sig")
            old_fields, _ = read_header(text, header_only=True)
            fields, comments = read_header(text)
            for key in FIELDS:
                if item[key] == old_fields[key]:
                    item[key] = fields[key]
            item["comments"] = comments
            item["attribution_version"] = 2
            changed = True
        return changed

    def _upgrade_identification(self, directory: PrivateDir, doc: dict[str, Any]) -> bool:
        changed = False
        for cube_id, item in doc["items"].items():
            # Any manual colour setting froze the whole space specification in
            # previous versions, including output-only and gamut/range edits.
            if item.get("input_space_manual") or item.get("identification_version") == IDENTIFICATION_VERSION:
                continue
            raw = self._raw(directory, cube_id)
            result = identify(Cube.parse(raw.decode("utf-8-sig"), cube_id), item["filename"])
            if any(item[key] != result[key] for key in ("input_space", "output_space")):
                self._invalidate_space(item)
            item.update(result)
            changed = True
        return changed

    @staticmethod
    def _invalidate_space(item: dict[str, Any]) -> None:
        item["fit"] = item["recipe"] = item["camera_lut"] = None
        item["version"] += 1

    def _legacy(self, directory: PrivateDir, *, persist: bool = True) -> dict[str, Any]:
        """旧版仅保存 SHA.cube 和 SHA.filename，首次打开时登记，保留原文件。"""
        doc: dict[str, Any] = {"schema": SCHEMA, "revision": 0, "groups": [], "items": {}}
        if directory.exists("luts"):
            for name in sorted(directory.names("luts")):
                if not re.fullmatch(r"[0-9a-f]{64}\.cube", name):
                    continue
                raw = directory.read(f"luts/{name}")
                filename = name.removesuffix(".cube") + ".filename"
                original_name = directory.read(f"luts/{filename}").decode().strip() if directory.exists(f"luts/{filename}") else name
                record = self._record(raw, original_name)
                if record["cube_id"] != name.removesuffix(".cube"):
                    raise LutError(409, "cube_changed", "这个 LUT 已改变。请重新导入。")
                doc["items"][record["cube_id"]] = record
        if doc["items"] and persist:
            self._save(directory, doc)
        return doc

    def migrate(self) -> None:
        """启动时做一次：旧版登记、署名和识别的升级、未完成的删除都写好，之后的读取不再写盘。"""
        with self.lock, self.private() as directory:
            self._read(directory)

    def _save(self, directory: PrivateDir, doc: dict[str, Any]) -> None:
        doc["revision"] += 1
        durable_write(directory, "luts/library.json", doc)

    @staticmethod
    def _item(doc: dict[str, Any], cube_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-f]{64}", cube_id):
            raise LutError(404, "cube_not_found", NOT_FOUND)
        item = doc["items"].get(cube_id)
        if item is None:
            raise LutError(404, "cube_not_found", NOT_FOUND)
        return cast(dict[str, Any], item)

    def listing(self) -> dict[str, Any]:
        with self.lock, self.private() as directory:
            doc = self._read(directory)
            return {"luts": list(doc["items"].values()), "groups": doc["groups"], "revision": doc["revision"]}

    def get(self, cube_id: str, *, persist: bool = True) -> dict[str, Any]:
        with self.lock, self.private() as directory:
            return self._item(self._read(directory, persist=persist), cube_id)

    def render_token(self, cube_id: str, *, persist: bool = True) -> str:
        """照片缓存、显卡 LUT 表和图片 URL 共用的出图标识：cube 内容（cube_id 就是它的 SHA-256）加完整的色彩规范
        （`target()` 用到的全部空间字段）的哈希。同一个规范一定得到同一张表；规范一变标识就变。
        不用记录里的 version：删除后重新导入，version 会从头再数，旧标识会被复用（复审 r2 P1-02）。删除得到单独的标识。"""
        try:
            item = self.get(cube_id, persist=persist)
        except LutError as exc:
            if exc.code != "cube_not_found":
                raise
            return hashlib.sha256(f"{cube_id}:deleted".encode()).hexdigest()[:16]
        return _token(cube_id, item)

    def add(self, raw: bytes, filename: str, group: str | None = None) -> dict[str, Any]:
        name, folder, skip = file_parts(filename)
        if skip:
            return {"skipped": True, "filename": name}
        if Path(name).suffix.lower() != ".cube":
            raise LutError(422, "cube_unsupported", "不支持这种 LUT 格式")
        record = self._record(raw, name)
        digest = record["cube_id"]
        with self.lock, self.private() as directory:
            doc = self._read(directory)
            relative = f"luts/{digest}.cube"
            if digest in doc["items"]:
                if not directory.exists(relative) or hashlib.sha256(directory.read(relative)).hexdigest() != digest:
                    directory.replace_file(relative, raw)
                return {**doc["items"][digest], "duplicate": True}
            group_name = group if group is not None else folder
            if group_name:
                record["group"] = self._group(doc, label(group_name, "分组名称"))["id"]
            directory.replace_file(relative, raw)
            doc["items"][digest] = record
            self._save(directory, doc)
        return {**record, "duplicate": False}

    @staticmethod
    def _record(raw: bytes, name: str) -> dict[str, Any]:
        if len(raw) > MAX_BYTES:
            raise LutError(422, "cube_too_big", "无法读取 LUT。文件过大。")
        digest = hashlib.sha256(raw).hexdigest()
        try:
            text = raw.decode("utf-8-sig")
            cube = Cube.parse(text, digest)
        except (UnicodeDecodeError, ValueError, IndexError) as exc:
            raise LutError(422, "cube_unreadable", "无法读取 LUT。请改用 .cube 文件。") from exc
        metadata, comments = read_header(text)
        return {
            "cube_id": digest,
            "name": label((cube.title or Path(name).stem)[:255]),
            "filename": name,
            "file": f"{digest}.cube",
            "group": None,
            "starred": False,
            "output_space": "srgb",
            "input_gamut": None,
            "output_gamut": None,
            "input_range": "full",
            "output_range": "full",
            "version": 1,
            "fit": None,
            "recipe": None,
            "camera_lut": None,
            "comments": comments,
            "attribution_version": 2,
            **metadata,
            **identify(cube, name),
            "title": cube.title,
            "kind": "+".join(k for k, t in (("1D", cube.table1), ("3D", cube.table3)) if t is not None),
            "size_1d": 0 if cube.table1 is None else len(cube.table1),
            "size_3d": 0 if cube.table3 is None else len(cube.table3),
        }

    def load(self, cube_id: str, *, persist: bool = True) -> Cube:
        with self.lock, self.private() as directory:
            self._item(self._read(directory, persist=persist), cube_id)
            raw = self._raw(directory, cube_id)
            cached = self._cubes.get(cube_id)
            if cached is None:
                cached = Cube.parse(raw.decode("utf-8-sig"), cube_id)
                self._cubes[cube_id] = cached
            self._cubes.move_to_end(cube_id)
            while len(self._cubes) > 4:
                self._cubes.popitem(last=False)
            return cached

    @staticmethod
    def _raw(directory: PrivateDir, cube_id: str) -> bytes:
        try:
            raw = directory.read(f"luts/{cube_id}.cube")
        except FileNotFoundError as exc:
            raise LutError(404, "cube_not_found", NOT_FOUND) from exc
        if hashlib.sha256(raw).hexdigest() != cube_id:
            raise LutError(409, "cube_changed", "这个 LUT 已改变。请重新导入。")
        return raw

    def snapshot(self, cube_id: str) -> tuple[bytes, dict[str, Any]]:
        with self.lock, self.private() as directory:
            item = self._item(self._read(directory), cube_id)
            return self._raw(directory, cube_id), dict(item)

    @staticmethod
    def _group(doc: dict[str, Any], name: str) -> dict[str, Any]:
        found = next((g for g in doc["groups"] if g["name"] == name), None)
        if found is not None:
            return cast(dict[str, Any], found)
        group = {"id": uuid.uuid4().hex[:12], "name": name}
        doc["groups"].append(group)
        return group

    def add_group(self, name: str) -> dict[str, Any]:
        with self.lock, self.private() as directory:
            doc = self._read(directory)
            group = self._group(doc, label(name, "分组名称"))
            self._save(directory, doc)
            return group

    def change_group(self, group_id: str, name: str | None) -> None:
        with self.lock, self.private() as directory:
            doc = self._read(directory)
            group = next((g for g in doc["groups"] if g["id"] == group_id), None)
            if group is None:
                raise LutError(404, "lut_group_not_found", "找不到这个分组。请刷新页面后重试。")
            if name is None:
                doc["groups"].remove(group)
                for item in doc["items"].values():
                    if item["group"] == group_id:
                        item["group"] = None
            else:
                cleaned = label(name, "分组名称")
                if any(g["name"] == cleaned and g["id"] != group_id for g in doc["groups"]):
                    raise LutError(409, "lut_group_exists", "已有同名分组")
                group["name"] = cleaned
            self._save(directory, doc)

    def patch(self, cube_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        if set(changes) - {
            "name",
            "group",
            "starred",
            "input_space",
            "output_space",
            "input_gamut",
            "output_gamut",
            "input_range",
            "output_range",
            *FIELDS,
        }:
            raise LutError(422, "bad_lut_change", "请求无效。请刷新页面后重试。")
        with self.lock, self.private() as directory:
            doc = self._read(directory)
            item = self._item(doc, cube_id)
            for key, value in changes.items():
                self._validate_change(doc, key, value)
                item[key] = label(value) if key == "name" else value
            if {"input_space", "output_space", "input_gamut", "output_gamut", "input_range", "output_range"} & changes.keys():
                item["input_space_manual"] = True
                self._invalidate_space(item)
            self._save(directory, doc)
            return item

    @staticmethod
    def _validate_change(doc: dict[str, Any], key: str, value: Any) -> None:
        bad = LutError(422, "bad_lut_change", "请求无效。请刷新页面后重试。")
        if key == "starred" and not isinstance(value, bool):
            raise bad
        if key in ("input_space", "output_space") and value not in SPACES:
            raise LutError(422, "bad_space", "请选择输入色彩空间")
        if key in ("input_gamut", "output_gamut") and value not in (None, "rec709", "rec2020", "vgamut", "sgamut3", "sgamut3cine"):
            raise bad
        if key in ("input_range", "output_range") and value not in ("full", "legal"):
            raise bad
        if key == "group" and value is not None and value not in [g["id"] for g in doc["groups"]]:
            raise bad
        if key in FIELDS and (not isinstance(value, str) or len(value) > 8192 or "\r" in value or "\x00" in value):
            raise bad

    def delete(self, cube_id: str) -> None:
        with self.lock, self.private() as directory:
            doc = self._read(directory)
            self._item(doc, cube_id)
            del doc["items"][cube_id]
            doc["pending_deletes"] = [cube_id]
            self._save(directory, doc)
            self._finish_deleted(directory, cube_id)
            doc["pending_deletes"] = []
            self._save(directory, doc)
            self._cubes.pop(cube_id, None)

    def _finish_deleted(self, directory: PrivateDir, cube_id: str) -> None:
        # 删除先在库中提交。中途失败时留下待完成记录，下次打开继续，只动本机应用数据。
        directory.remove(f"luts/{cube_id}.cube")
        directory.remove(f"luts/{cube_id}.filename")

    def target(self, cube_id: str, *, persist: bool = True) -> tuple[Target, dict[str, Any]]:
        target, item, _token_value = self.target_and_token(cube_id, persist=persist)
        return target, item

    def target_and_token(self, cube_id: str, *, persist: bool = True) -> tuple[Target, dict[str, Any], str]:
        """出图目标和它的标识来自同一次读取（同一个快照）：表和标识不会对不上。"""
        with self.lock:
            item = self.get(cube_id, persist=persist)
            space = item["input_space"]
            if space is None:
                raise LutError(422, "lut_space_required", "请选择输入色彩空间")
            return (
                Target(
                    self.load(cube_id, persist=persist),
                    space,
                    item["output_space"],
                    item.get("input_gamut"),
                    item.get("output_gamut"),
                    item.get("input_range", "full"),
                    item.get("output_range", "full"),
                ),
                item,
                _token(cube_id, item),
            )

    def export(self, cube_id: str) -> dict[str, Any]:
        with self.lock, self.private() as directory:
            record = self.get(cube_id)
            data = export_cube(self.load(cube_id), record, original=self._raw(directory, cube_id))
            # 固定在应用数据目录，用户提供的名称不参与落盘路径。
            relative = f"luts/exports/{cube_id}.cube"
            directory.replace_file(relative, data)
            return {"cube_id": cube_id, "path": str(directory.real / relative), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}

    def export_group(self, group_id: str | None) -> list[dict[str, Any]]:
        with self.lock:
            listing = self.listing()
            if group_id is not None and group_id not in [g["id"] for g in listing["groups"]]:
                raise LutError(404, "lut_group_not_found", "找不到这个分组。请刷新页面后重试。")
            return [self.export(item["cube_id"]) for item in listing["luts"] if item["group"] == group_id]
