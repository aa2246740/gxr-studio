"""质量包固定字节schema、导入、冻结与覆盖检查。包内只有数据。"""

from __future__ import annotations

import io
import json
import os
import uuid
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np

from .. import osplat
from ..lut.cube import Cube
from ..lut.spaces import SPACES, Target
from ..privacy import PrivateDir
from ..recipes import RecipeError, RenderEnvironment, check_record
from ..recipes.types import canonical, digest, require_digest
from ..render.params import UNITS
from .types import GROUPS, MAX_ARRAY_BYTES, FrozenContext, SampleGroup, TrainingInput

SCHEMA = "gxr-studio/quality-pack/1"
CONTEXT_SCHEMA = "gxr-studio/fit-context/1"
_MAX_PACK = 256 << 20


def _file(name: str) -> str:
    if not isinstance(name, str) or not name or Path(name).name != name or name in (".", "..") or "\\" in name:
        raise RecipeError("bad_quality_path", "质量包文件名无效。")
    return name


def _read_data_package(folder: Path, filename: str, schema: str) -> tuple[dict[str, Any], dict[str, bytes]]:
    fd = osplat.open_dir(folder)
    try:

        def read(name: str, maximum: int) -> bytes:
            file_fd = osplat.open_file_at(fd, _file(name))
            with os.fdopen(file_fd, "rb") as stream:
                if os.fstat(stream.fileno()).st_size > maximum:
                    raise RecipeError("quality_pack_too_large", "质量包超过大小上限。")
                result = stream.read(maximum + 1)
                if len(result) > maximum:
                    raise RecipeError("quality_pack_too_large", "质量包超过大小上限。")
                return result

        manifest = json.loads(read(filename, 1 << 20))
        if not isinstance(manifest, dict) or manifest.get("schema") != schema:
            raise RecipeError("bad_quality_schema", "质量包版本无法识别。")
        files = manifest["files"]
        if not isinstance(files, dict) or not 1 <= len(files) <= 64 or sum(f["size"] for f in files.values()) > _MAX_PACK:
            raise RecipeError("bad_quality_manifest", "质量包文件清单无效。")
        blobs = {}
        for name, metadata in files.items():
            data = read(name, MAX_ARRAY_BYTES)
            if len(data) != metadata["size"] or digest(data) != metadata["sha256"]:
                raise RecipeError("quality_source_changed", "质量包的来源摘要不符。")
            blobs[name] = data
        return manifest, blobs
    finally:
        os.close(fd)


def _read_pack(folder: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    return _read_data_package(folder, "quality-pack.json", SCHEMA)


def _photos(photos: Any) -> list[dict[str, Any]]:
    if not isinstance(photos, list) or any(not isinstance(p, dict) or p.get("id") != i for i, p in enumerate(photos)):
        raise RecipeError("bad_quality_photos", "质量包的照片索引不完整。")
    for photo in photos:
        for name in ("raw_sha256", "jpg_sha256", "family_sha256", "sampling_sha256"):
            require_digest(photo[name])
        if not isinstance(photo["iso"], int) or isinstance(photo["iso"], bool) or photo["iso"] <= 0:
            raise RecipeError("bad_quality_photos", "质量包缺少照片ISO。")
    return [dict(p) for p in photos]


_GAMUTS = ("rec709", "rec2020", "vgamut", "sgamut3", "sgamut3cine")


def _validate_lut_target_spec(target: dict[str, Any]) -> None:
    require_digest(target.get("cube_sha256", ""))
    spaces = target.get("spaces")
    if not isinstance(spaces, dict):
        raise RecipeError("quality_target_mismatch", "LUT目标缺少色彩空间解释。")
    if spaces.get("input_space") not in SPACES or spaces.get("output_space") not in SPACES:
        raise RecipeError("quality_target_mismatch", "LUT目标的色彩空间无效。")
    if spaces.get("input_gamut") not in _GAMUTS or spaces.get("output_gamut") not in _GAMUTS:
        raise RecipeError("quality_target_mismatch", "LUT目标的色域无效。")
    if spaces.get("input_range") not in ("full", "legal") or spaces.get("output_range") not in ("full", "legal"):
        raise RecipeError("quality_target_mismatch", "LUT目标的范围无效。")


def _authoritative_lut_target(target: dict[str, Any], target_asset: bytes) -> Target:
    _validate_lut_target_spec(target)
    cube_sha = target["cube_sha256"]
    if digest(target_asset) != cube_sha:
        raise RecipeError("quality_target_mismatch", "LUT目标原字节与摘要不符。")
    try:
        cube = Cube.parse(target_asset.decode("utf-8-sig"), cube_sha)
    except (UnicodeDecodeError, ValueError) as exc:
        raise RecipeError("quality_target_mismatch", "LUT目标文件无法解析。") from exc
    spaces = target["spaces"]
    renderer = Target(
        cube,
        spaces["input_space"],
        spaces["output_space"],
        spaces["input_gamut"],
        spaces["output_gamut"],
        spaces["input_range"],
        spaces["output_range"],
    )
    if target != {"kind": "lut", "cube_sha256": cube_sha, "spaces": renderer.description()}:
        raise RecipeError("quality_target_mismatch", "LUT目标声明与规范空间解释不一致。")
    return renderer


def _verify_lut_wants(groups: dict[str, SampleGroup], renderer: Target) -> None:
    for values in groups.values():
        try:
            expected = np.asarray(renderer.render(values.base_new, values.xyz), dtype="<f8")
        except ValueError as exc:
            raise RecipeError("quality_target_mismatch", "LUT目标无法在当前采样上求值。") from exc
        if not np.array_equal(values.want, expected):
            raise RecipeError("quality_target_mismatch", "质量包内冻结的want与实际LUT及空间解释不一致。")


def _render_group_wants(
    manifest: dict[str, Any],
    groups: dict[str, SampleGroup],
    renderer: Target,
    blobs: dict[str, bytes],
    files: dict[str, Any],
) -> None:
    for name, values in groups.items():
        try:
            want = np.asarray(renderer.render(values.base_new, values.xyz), dtype="<f8")
        except ValueError as exc:
            raise RecipeError("quality_target_mismatch", "LUT目标无法在当前采样上求值。") from exc
        data = replace(values, want=want).to_bytes()
        SampleGroup.from_bytes(data)
        filename = manifest["groups"][name]
        blobs[filename] = data
        files[filename] = {"size": len(data), "sha256": digest(data)}


def _validate(manifest: dict[str, Any], blobs: dict[str, bytes]) -> tuple[RenderEnvironment, dict[str, SampleGroup], list[str]]:
    if manifest.get("unit") not in UNITS or manifest.get("family") not in ("color", "bw", "native"):
        raise RecipeError("bad_quality_manifest", "质量包的单元或记录类型无法识别。")
    environment = RenderEnvironment(blobs[manifest["environment"]])
    environment.validate()
    if environment.unit != manifest["unit"]:
        raise RecipeError("quality_model_mismatch", "质量包与模型的镜头单元不同。")
    slot = manifest.get("slot")
    if isinstance(slot, bool) or not isinstance(slot, int) or not 1 <= slot <= 8:
        raise RecipeError("bad_control_binding", "质量包的控制底子无效。")
    if (manifest["family"] == "bw") != (slot == 3) and manifest["family"] != "native":
        raise RecipeError("bad_control_binding", "完整黑白配方必须绑定含Filt的原生03控制，彩色配方不能使用该控制。")
    environment.controls(slot)
    for role, name in manifest["references"].items():
        if role not in ("template", "original", "factory04", "replacement", "additional"):
            raise RecipeError("bad_quality_reference", "质量包含未知参照角色。")
        check_record(blobs[name], full=False)
    if not {"template", "original", "factory04"} <= set(manifest["references"]):
        raise RecipeError("quality_training_missing", "完整拟合缺少模板、原优化参照或原厂04。")
    if blobs[manifest["references"]["factory04"]] != environment.stock:
        raise RecipeError("quality_model_mismatch", "质量包的04参照与模型绑定不同。")
    if manifest["family"] == "color" and blobs[manifest["references"]["template"]][0] == 3:
        raise RecipeError("bad_quality_reference", "彩色完整配方不能使用原生黑白路由模板。")
    photos = _photos(manifest["photos"])
    groups = {}
    used: dict[str, set[str]] = {}
    missing = [key for key in ("photo_iso200", "photo_other_iso") if key not in manifest["groups"]]
    for name, filename in manifest["groups"].items():
        if name not in GROUPS:
            raise RecipeError("bad_quality_group", "质量包的分组无法识别。")
        values = SampleGroup.from_bytes(blobs[filename])
        values.verify_model(environment)
        identities: set[str] = set()
        for pid in np.unique(values.photo_id):
            if pid < 0 and name in ("train", "synthetic"):
                continue
            if pid < 0 or pid >= len(photos):
                raise RecipeError("bad_quality_photos", "留出样本缺少真实来源索引。")
            photo = photos[int(pid)]
            if np.any(values.iso[values.photo_id == pid] != photo["iso"]):
                raise RecipeError("bad_quality_photos", "样本ISO与照片来源不同。")
            identities.update((photo["family_sha256"], photo["raw_sha256"]))
            if name not in ("train", "synthetic") and not photo.get("scene"):
                missing.append(f"{name}/scene/{int(pid)}")
        if name == "photo_iso200" and np.any(values.iso != 200):
            raise RecipeError("bad_quality_iso", "主留出必须全部为ISO200。")
        if name == "photo_other_iso" and np.any(values.iso == 200):
            raise RecipeError("bad_quality_iso", "其他ISO留出不能用ISO200代替。")
        used[name], groups[name] = identities, values
    if "train" not in groups:
        raise RecipeError("quality_training_missing", "没有训练输入，不能启动完整拟合。")
    if any(used["train"] & value for key, value in used.items() if key not in ("train", "synthetic")):
        raise RecipeError("quality_split_leak", "训练和留出包含同一照片或配对家族。")
    _validate_provenance_and_target(manifest, blobs, groups)
    return environment, groups, sorted(set(missing))


def _validate_provenance_and_target(manifest: dict[str, Any], blobs: dict[str, bytes], groups: dict[str, SampleGroup]) -> None:
    for seed in manifest.get("seeds", []):
        check_record(blobs[seed], full=False)
    if not isinstance(manifest.get("target"), dict) or not isinstance(manifest.get("sources"), list) or not manifest["sources"]:
        raise RecipeError("quality_provenance_missing", "质量包缺少冻结目标或来源。")
    for source in manifest["sources"]:
        require_digest(source["sha256"])
        if not source.get("kind") or not source.get("description"):
            raise RecipeError("quality_provenance_missing", "质量包缺少采样来源说明。")
    if manifest["target"].get("kind") == "lut":
        _validate_lut_target_spec(manifest["target"])
        if manifest.get("schema") == CONTEXT_SCHEMA and "target.cube" not in blobs:
            raise RecipeError("quality_target_missing", "冻结LUT上下文缺少原始.cube字节。")
        if "target.cube" in blobs:
            _verify_lut_wants(groups, _authoritative_lut_target(manifest["target"], blobs["target.cube"]))


def build_quality_pack(
    folder: Path,
    *,
    environment: RenderEnvironment,
    groups: dict[str, SampleGroup],
    photos: list[dict[str, Any]],
    references: dict[str, bytes],
    target: dict[str, Any],
    sources: list[dict[str, Any]],
    slot: int,
    family: str = "color",
    seeds: tuple[bytes, ...] = (),
    forbidden: tuple[Path, ...] = (),
) -> dict[str, Any]:
    """由明确来源的封存数值采样构建包；不搜索照片，不执行输入包脚本。"""
    if isinstance(slot, bool) or not isinstance(slot, int) or not 1 <= slot <= 8:
        raise RecipeError("bad_control_binding", "质量包的控制底子无效。")
    blobs = {"environment.json": environment.data}
    blobs.update({f"{name}.npz": group.to_bytes() for name, group in groups.items()})
    blobs.update({f"{role}.record": entry for role, entry in references.items()})
    blobs.update({f"seed-{i}.record": entry for i, entry in enumerate(seeds)})
    manifest = {
        "schema": SCHEMA,
        "unit": environment.unit,
        "slot": slot,
        "family": family,
        "environment": "environment.json",
        "groups": {name: f"{name}.npz" for name in groups},
        "references": {role: f"{role}.record" for role in references},
        "seeds": [f"seed-{i}.record" for i in range(len(seeds))],
        "photos": photos,
        "target": target,
        "sources": sources,
        "files": {name: {"size": len(data), "sha256": digest(data)} for name, data in sorted(blobs.items())},
    }
    _validate(manifest, blobs)
    # 最后发布manifest；不覆盖一个已有包，失败也不会出现可导入的半包。
    with PrivateDir(folder, "私有质量包", forbidden) as directory:
        if directory.exists("quality-pack.json"):
            raise RecipeError("quality_pack_exists", "请为新的质量包选择空目录。")
        for name, data in blobs.items():
            directory.write(_file(name), data)
        directory.replace_file("quality-pack.json", canonical(manifest))
    return manifest


def build_sample_source(source: Path, destination: Path) -> dict[str, Any]:
    """封存raw_wb/XYZ采样来源包转为可复用质量语料，不需要执行采样脚本。"""
    manifest, blobs = _read_data_package(source, "source.json", "gxr-studio/sample-source/1")
    environment = RenderEnvironment(blobs[manifest["environment"]])
    environment.validate()
    groups = {}
    names = {"raw_wb", "xyz", "sw", "photo_id", "iso"}
    for name, filename in manifest["groups"].items():
        blob = blobs[filename]
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            if len(archive.infolist()) != 5 or sum(i.file_size for i in archive.infolist()) > MAX_ARRAY_BYTES:
                raise RecipeError("bad_quality_arrays", "封存采样的成员或大小无效。")
        with np.load(io.BytesIO(blob), allow_pickle=False) as data:
            if set(data.files) != names:
                raise RecipeError("bad_quality_arrays", "封存采样缺少原白平衡码值、XYZ或来源索引。")
            arrays = {key: data[key] for key in names}
            if any(a.dtype.str != ("<i8" if k in ("photo_id", "iso") else "<f8") for k, a in arrays.items()):
                raise RecipeError("bad_quality_arrays", "封存采样需要固定的小端数组。")
            groups[name] = SampleGroup.from_samples(environment, **arrays)
    references = {role: blobs[filename] for role, filename in manifest["references"].items()}
    return build_quality_pack(
        destination,
        environment=environment,
        groups=groups,
        photos=manifest["photos"],
        references=references,
        target={"kind": "sample-corpus"},
        sources=[
            *manifest["sources"],
            {
                "kind": "archived-sample-source",
                "sha256": digest(canonical(manifest)),
                "description": "source.json with verified numerical members",
            },
        ],
        slot=manifest["slot"],
        family=manifest["family"],
        seeds=tuple(blobs[filename] for filename in manifest.get("seeds", [])),
    )


class FitContexts:
    def __init__(self, home: Path, forbidden: tuple[Path, ...] = ()) -> None:
        self.home = Path(home)
        self.root = Path(home) / "fit_contexts"
        self.forbidden = forbidden

    def _publish(self, manifest: dict[str, Any], blobs: dict[str, bytes]) -> str:
        data = canonical(manifest)
        identity = digest(data)
        staging = f".staging-{uuid.uuid4().hex}"
        with PrivateDir(self.home, "完整拟合输入", self.forbidden) as parent, parent.child("fit_contexts") as directory:
            if directory.exists(identity):
                if directory.read(f"{identity}/manifest.json") != data:
                    raise RecipeError("context_conflict", "冻结输入的内容身份冲突。")
                return identity
            try:
                for name, value in blobs.items():
                    directory.write(f"{staging}/{_file(name)}", value)
                directory.write(f"{staging}/manifest.json", data)
                directory.rename(staging, identity)
                osplat.sync_dir(directory.fd)
            finally:
                directory.remove(staging)
        return identity

    def import_pack(self, folder: Path) -> dict[str, Any]:
        try:
            manifest, blobs = _read_pack(folder)
            _environment, groups, missing = _validate(manifest, blobs)
        except RecipeError:
            raise
        except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as exc:
            raise RecipeError("quality_pack_unreadable", "私有质量包的结构或来源无法核验。") from exc
        coverage = {k: len(v.x) for k, v in groups.items()}
        identity = self._publish({**manifest, "coverage": coverage, "missing": missing}, blobs)
        return {"id": identity, "unit": manifest["unit"], "coverage": coverage, "missing": missing}

    def listing(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        if not self.root.exists():
            return result
        with PrivateDir(self.home, "完整拟合输入", self.forbidden) as parent, parent.child("fit_contexts") as directory:
            for name in sorted(osplat.listdir_fd(directory.fd)):
                if len(name) != 64 or any(c not in "0123456789abcdef" for c in name):
                    continue
                data = directory.read(f"{name}/manifest.json")
                if digest(data) != name:
                    raise RecipeError("context_corrupt", "质量输入清单已损坏。")
                manifest = json.loads(data)
                if manifest["schema"] == SCHEMA:
                    result.append({"id": name, "unit": manifest["unit"], "coverage": manifest["coverage"], "missing": manifest["missing"]})
        return result

    def _open(self, identity: str, *, training_only: bool = False) -> tuple[dict[str, Any], dict[str, bytes]]:
        require_digest(identity)
        with PrivateDir(self.home, "完整拟合输入", self.forbidden) as parent, parent.child("fit_contexts") as directory:
            data = directory.read(f"{identity}/manifest.json")
            if digest(data) != identity:
                raise RecipeError("context_corrupt", "冻结输入清单已损坏。")
            manifest = json.loads(data)
            names = set(manifest["files"])
            if training_only:
                names = {manifest["environment"], manifest["groups"]["train"], *manifest["references"].values(), *manifest["seeds"]}
                if "target.cube" in manifest["files"]:
                    names.add("target.cube")
            blobs = {name: directory.read(f"{identity}/{_file(name)}") for name in names}
        if any(len(data) != manifest["files"][n]["size"] or digest(data) != manifest["files"][n]["sha256"] for n, data in blobs.items()):
            raise RecipeError("context_corrupt", "冻结输入成员已损坏。")
        return manifest, blobs

    def _bind_lut_freeze(
        self,
        manifest: dict[str, Any],
        groups: dict[str, SampleGroup],
        target: dict[str, Any],
        target_renderer: Target | None,
        target_asset: bytes | None,
        blobs: dict[str, bytes],
        files: dict[str, Any],
    ) -> None:
        raw_cube = target_asset if target_asset is not None else blobs.get("target.cube")
        if raw_cube is None:
            raise RecipeError("quality_target_missing", "LUT目标需要提供原始.cube字节。")
        if "target.cube" in blobs and blobs["target.cube"] != raw_cube:
            raise RecipeError("quality_target_mismatch", "质量包内封存的LUT字节与当前目标不符。")
        authoritative = _authoritative_lut_target(target, raw_cube)
        if target_renderer is not None:
            probe = groups["train"]
            if target_renderer.description() != authoritative.description() or not np.array_equal(
                np.asarray(target_renderer.render(probe.base_new, probe.xyz), dtype="<f8"),
                np.asarray(authoritative.render(probe.base_new, probe.xyz), dtype="<f8"),
            ):
                raise RecipeError("quality_target_mismatch", "传入的LUT渲染器与实际.cube字节不一致。")
        if manifest["target"].get("kind") == "sample-corpus":
            _render_group_wants(manifest, groups, authoritative, blobs, files)
        else:
            if manifest["target"] != target:
                raise RecipeError("quality_target_mismatch", "质量包的冻结目标与本次LUT及空间解释不同。")
            _verify_lut_wants(groups, authoritative)
        blobs["target.cube"] = raw_cube
        files["target.cube"] = {"size": len(raw_cube), "sha256": digest(raw_cube)}

    def freeze(
        self,
        pack: str,
        *,
        target: dict[str, Any],
        plan: dict[str, Any],
        environment: RenderEnvironment,
        style: str | None = None,
        replacement: tuple[str, bytes] | None = None,
        target_renderer: Target | None = None,
        target_asset: bytes | None = None,
    ) -> FrozenContext:
        manifest, blobs = self._open(pack)
        bound, _groups, missing = _validate(manifest, blobs)
        if bound.data != environment.data:
            raise RecipeError("quality_model_mismatch", "质量包绑定的模型或控制数据已变化，请重新建立质量包。")
        files = dict(manifest["files"])
        if target.get("kind") == "lut":
            self._bind_lut_freeze(manifest, _groups, target, target_renderer, target_asset, blobs, files)
        else:
            if manifest["target"].get("kind") == "sample-corpus":
                if target_renderer is None:
                    raise RecipeError("quality_target_missing", "采样语料需要明确的LUT目标。")
                _render_group_wants(manifest, _groups, target_renderer, blobs, files)
            elif manifest["target"] != target:
                raise RecipeError("quality_target_mismatch", "质量包的冻结目标与本次LUT及空间解释不同。")
            if target_asset is not None:
                if digest(target_asset) != target.get("cube_sha256"):
                    raise RecipeError("quality_target_mismatch", "LUT目标原字节与摘要不符。")
                blobs["target.cube"] = target_asset
                files["target.cube"] = {"size": len(target_asset), "sha256": digest(target_asset)}
        if style is not None:
            require_digest(style)
        references = dict(manifest["references"])
        if replacement is not None:
            version, entry = replacement
            require_digest(version)
            check_record(entry)
            if "replacement" in references and blobs[references["replacement"]] != entry:
                raise RecipeError("replacement_changed", "质量包的替换参照与现行同风格配方不同。")
            references["replacement"] = "replacement.record"
            blobs["replacement.record"] = entry
            files["replacement.record"] = {"size": len(entry), "sha256": digest(entry)}
        context = {
            **manifest,
            "schema": CONTEXT_SCHEMA,
            "quality_pack": pack,
            "plan": plan,
            "missing": missing,
            "target": target,
            "style": style,
            "replacement_version": replacement[0] if replacement else None,
            "references": references,
            "files": files,
        }
        identity = self._publish(context, blobs)
        return self.open(identity)

    def open(self, identity: str) -> FrozenContext:
        manifest, blobs = self._open(identity, training_only=True)
        if manifest.get("schema") != CONTEXT_SCHEMA:
            raise RecipeError("bad_context_schema", "这份输入尚未冻结为拟合上下文。")
        # freeze已核全部输入；worker在胜者封存前只重读训练及静态依赖。
        train_manifest = {**manifest, "groups": {"train": manifest["groups"]["train"]}}
        environment, groups, _missing = _validate(train_manifest, blobs)
        refs = manifest["references"]
        train = TrainingInput(
            manifest["unit"],
            manifest["slot"],
            manifest["family"],
            groups["train"],
            environment,
            blobs[refs["template"]],
            blobs[refs["original"]],
            tuple(blobs[n] for n in manifest["seeds"]),
            canonical(manifest["plan"]),
        )
        return FrozenContext(identity, canonical(manifest), train)

    def evaluation(self, identity: str) -> tuple[dict[str, SampleGroup], dict[str, bytes]]:
        manifest, blobs = self._open(identity)
        if manifest.get("schema") != CONTEXT_SCHEMA:
            raise RecipeError("bad_context_schema", "这份输入尚未冻结为拟合上下文。")
        _environment, groups, _missing = _validate(manifest, blobs)
        return {k: v for k, v in groups.items() if k not in ("train", "synthetic")}, {k: blobs[n] for k, n in manifest["references"].items()}
