"""g250 半格一对的图库集成：批量查询伙伴、单张补算、导入/重建/回填。"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import halfframe as hf
from . import halfframe_pairs as hfp


def _shot_jpg_sha(shot: dict[Any, Any]) -> str | None:
    all_files = shot.get("all_files") or shot.get("files") or {}
    jpg = all_files.get("jpg")
    if isinstance(jpg, dict):
        sha = jpg.get("sha256")
        return sha if isinstance(sha, str) and sha else None
    return None


def _source_summary(shot: dict[Any, Any]) -> dict[str, Any]:
    all_files = shot.get("all_files") or shot.get("files") or {}
    return {
        "shot_id": shot["shot_id"],
        "name": shot["name"],
        "orientation": int(shot.get("orientation") or 1),
        "unit": shot.get("unit"),
        "lens": shot.get("lens"),
        "iso": shot.get("iso"),
        "width": shot.get("width"),
        "height": shot.get("height"),
        "date": shot.get("date"),
        "captured_at": shot.get("captured_at"),
        "copyright": shot.get("copyright"),
        "recipe_tag": shot.get("recipe_tag"),
        "all_files": {k: dict(v) for k, v in all_files.items()},
    }


def _fetch_shots_by_jpg_shas(db: sqlite3.Connection, lock: Any, shas: set[str]) -> dict[str, dict[str, Any]]:
    """按 JPG SHA 批量查出配对伙伴的基本元数据与全部文件（dng/jpg）。"""
    if not shas:
        return {}
    from .camdata.recipe_tags import RECIPE_TAG_VERSION, parse_recipe_tag

    by_sha: dict[str, dict[str, Any]] = {}
    sha_list = sorted(shas)
    with lock:
        for start in range(0, len(sha_list), 400):
            chunk = sha_list[start : start + 400]
            marks = ",".join("?" * len(chunk))
            rows = db.execute(
                f"""SELECT s.shot_id, s.name, s.orientation, s.unit, s.lens, s.iso, s.width, s.height,
                           s.date, s.captured_at, s.copyright, s.recipe_tag, f.sha256 AS jpg_sha
                    FROM files f
                    JOIN shots s ON s.shot_id = f.shot_id
                    WHERE f.kind = 'jpg' AND f.sha256 IN ({marks})""",  # nosec B608
                chunk,
            ).fetchall()
            if not rows:
                continue
            sids = [r["shot_id"] for r in rows]
            smarks = ",".join("?" * len(sids))
            files_by: dict[str, dict[str, dict[str, Any]]] = {sid: {} for sid in sids}
            for f in db.execute(
                f"SELECT shot_id, kind, relpath, sha256, size FROM files WHERE shot_id IN ({smarks})",  # nosec B608
                sids,
            ).fetchall():
                files_by[f["shot_id"]][f["kind"]] = {
                    "kind": f["kind"],
                    "relpath": f["relpath"],
                    "sha256": f["sha256"],
                    "size": f["size"],
                }
            for r in rows:
                tag = json.loads(r["recipe_tag"]) if r["recipe_tag"] else None
                if tag is None or tag.get("version") != RECIPE_TAG_VERSION:
                    tag = parse_recipe_tag(r["copyright"])
                by_sha[r["jpg_sha"]] = {
                    "shot_id": r["shot_id"],
                    "name": r["name"],
                    "orientation": int(r["orientation"] or 1),
                    "unit": r["unit"],
                    "lens": r["lens"],
                    "iso": r["iso"],
                    "width": r["width"],
                    "height": r["height"],
                    "date": r["date"],
                    "captured_at": r["captured_at"],
                    "copyright": r["copyright"],
                    "recipe_tag": tag,
                    "all_files": files_by.get(r["shot_id"], {}),
                }
    return by_sha


def _query_target_jpg_rows(db: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = db.execute(
        """SELECT s.shot_id, s.name, s.orientation, s.width, s.height, f.relpath, f.sha256
           FROM files f
           JOIN shots s ON s.shot_id = f.shot_id
           WHERE f.kind = 'jpg'
             AND (s.width IS NULL OR s.height IS NULL
                  OR (s.width = 4288 AND s.height = 2848)
                  OR (s.width = 2848 AND s.height = 4288))
           ORDER BY s.date DESC, s.captured_at DESC, s.name, s.shot_id"""
    ).fetchall()
    return [
        {
            "shot_id": r["shot_id"],
            "name": r["name"],
            "number": hfp.extract_number(r["name"]),
            "orientation": int(r["orientation"] or 1),
            "width": r["width"],
            "height": r["height"],
            "relpath": r["relpath"],
            "sha256": r["sha256"],
        }
        for r in rows
        if r["sha256"]
    ]


def _fetch_target_jpg_candidates(db: sqlite3.Connection, lock: Any) -> list[dict[str, Any]]:
    with lock:
        return _query_target_jpg_rows(db)


def _fetch_target_jpg_by_names(db: sqlite3.Connection, lock: Any, names: list[str]) -> list[dict[str, Any]]:
    """按相邻文件名（含导入冲突改名后缀 `_<sha8>`）查 4288×2848 候选 JPG，避免全表扫描。"""
    if not names:
        return []
    marks = ",".join("?" * len(names))
    like_clauses = ["s.name LIKE ? ESCAPE '\\'"] * len(names)
    params: list[str] = [*names]
    for name in names:
        esc = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params.append(f"{esc}\\_%")
    where = f"s.name IN ({marks}) OR " + " OR ".join(like_clauses)
    with lock:
        rows = db.execute(
            f"""SELECT s.shot_id, s.name, s.orientation, s.width, s.height, f.relpath, f.sha256
                FROM shots s
                JOIN files f ON f.shot_id = s.shot_id
                WHERE f.kind = 'jpg' AND ({where})""",  # nosec B608
            params,
        ).fetchall()
    return [
        {
            "shot_id": r["shot_id"],
            "name": r["name"],
            "number": hfp.extract_number(r["name"]),
            "orientation": int(r["orientation"] or 1),
            "width": r["width"],
            "height": r["height"],
            "relpath": r["relpath"],
            "sha256": r["sha256"],
        }
        for r in rows
        if r["sha256"] and hf.is_target_dims(r["width"], r["height"])
    ]


def _index_by_number(items: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    by_num: dict[int, list[dict[str, Any]]] = {}
    for item in items:
        num = item.get("number")
        if num is not None:
            by_num.setdefault(int(num), []).append(item)
    return by_num


def _ensure_first_like(
    relpath: str,
    sha: str,
    store: hfp.PairStore,
    abspath: Callable[[str], Path],
    *,
    coalesce: bool,
) -> bool | None:
    known = store.get_first_like(sha)
    if known is not None:
        return known
    detected = hfp.detect_first_like(abspath(relpath))
    if detected is not None:
        store.put_first_like(sha, detected, save=not coalesce, coalesce=coalesce, strict=False)
    return detected


def _match_second_candidate(
    sec_item: dict[str, Any],
    prev_list: list[dict[str, Any]],
    is_first_like_fn: Callable[[dict[str, Any]], bool],
    abspath: Callable[[str], Path],
    *,
    pair_store: hfp.PairStore | None = None,
    rejected_out: set[tuple[str, str]] | None = None,
    coalesce: bool = True,
) -> dict[str, Any] | None:
    """在编号为 `N-1` 的候选中找出唯一通过 g250 1/8 指纹的第 1 张。"""
    sec_sha = str(sec_item["sha256"])
    sec_ori = int(sec_item.get("orientation") or 1)
    matches: list[tuple[str, hfp.PairDecision]] = []
    for cand in prev_list:
        a_sha = cand.get("sha256")
        if not a_sha or a_sha == sec_sha or int(cand.get("orientation") or 1) != sec_ori:
            continue
        if pair_store is not None and pair_store.is_rejected(str(a_sha), sec_sha):
            continue
        if not is_first_like_fn(cand):
            continue
        dec, stable = hfp.evaluate_candidate_checked(
            abspath(cand["relpath"]),
            abspath(sec_item["relpath"]),
            orientation_a=sec_ori,
            orientation_b=sec_ori,
            a_first_like=True,
            adjacent=True,
        )
        if dec is not None:
            matches.append((str(a_sha), dec))
        elif not stable:
            continue  # 读文件出错：不记成“不是一对”，下次（重启、回填、再读）重试
        elif rejected_out is not None:
            rejected_out.add((str(a_sha), sec_sha))
        elif pair_store is not None:
            pair_store.mark_rejected(str(a_sha), sec_sha, save=not coalesce, coalesce=coalesce, strict=False)
    if len(matches) == 1:
        return hfp.make_pair_record(matches[0][0], sec_sha, matches[0][1])
    return None


def _try_pair_for_second(
    sec_item: dict[str, Any],
    by_num: dict[int, list[dict[str, Any]]],
    pair_store: hfp.PairStore,
    abspath: Callable[[str], Path],
    *,
    coalesce: bool,
) -> dict[str, Any] | None:
    sec_sha = sec_item.get("sha256")
    sec_num = sec_item.get("number")
    sec_ori = int(sec_item.get("orientation") or 1)
    if not sec_sha or sec_num is None or sec_ori not in hfp.VALID_ORIENTATIONS:
        return None
    prev_list = by_num.get(int(sec_num) - 1) or []
    if not prev_list:
        return None
    existing = pair_store.pair_for_second(str(sec_sha))
    if existing is not None and any(c["sha256"] == existing["first_sha"] and int(c["orientation"] or 1) == sec_ori for c in prev_list):
        return existing
    rec = _match_second_candidate(
        sec_item,
        prev_list,
        lambda c: bool(_ensure_first_like(str(c["relpath"]), str(c["sha256"]), pair_store, abspath, coalesce=coalesce)),
        abspath,
        pair_store=pair_store,
        coalesce=coalesce,
    )
    if rec is not None:
        pair_store.put_pair(rec, save=not coalesce, coalesce=coalesce, strict=False)
    return rec


def _compute_missing_for_shot(
    shot: dict[Any, Any],
    db: sqlite3.Connection,
    lock: Any,
    verdict_store: hf.VerdictStore,
    pair_store: hfp.PairStore,
    abspath: Callable[[str], Path],
    *,
    coalesce: bool,
    use_memo: bool = False,
) -> dict[str, dict[str, Any]]:
    """在 `lib.lock` 外为单张 4288×2848 照片补算缺失的 `first_like` 与相邻配对；已配对时走一次 `files_sha` 索引短路。

    `use_memo`（只给单张读取）：本次运行里已经查过 ±1 邻居、没配上的照片不再按名字查一遍
    （`shots.name` 没有索引，大图库里每次要扫表）。导入和补全走 `ensure_pair_for_shot`，不看也不写这份记录；
    重建索引时清空。读文件出错的候选不会被记成“不是一对”，下次启动的回填会再试。
    """
    all_files = shot.get("all_files") or shot.get("files") or {}
    jpg = all_files.get("jpg")
    if not isinstance(jpg, dict) or not jpg.get("relpath") or not jpg.get("sha256"):
        return {}
    if not hf.is_target_dims(shot.get("width"), shot.get("height")):
        return {}
    sha = str(jpg["sha256"])
    ori = int(shot.get("orientation") or 1)
    num = hfp.extract_number(shot.get("name"))
    diptych = bool(shot.get("diptych"))

    if diptych:
        if not pair_store.has_first_like(sha):
            pair_store.put_first_like(sha, False, save=not coalesce, coalesce=coalesce, strict=False)
        if num is None or ori not in hfp.VALID_ORIENTATIONS:
            return {}
        existing = pair_store.pair_for_second(sha)
        if existing is not None:
            prefetched = _fetch_shots_by_jpg_shas(db, lock, {existing["first_sha"]})
            first = prefetched.get(existing["first_sha"])
            if first is not None and int(first.get("orientation") or 1) == ori:
                return prefetched
        prev_names = hfp.neighbor_names(shot.get("name"), -1)
        if not prev_names:
            return {}
        if use_memo:
            if pair_store.neighbors_checked(sha):
                return {}
            pair_store.mark_neighbors_checked(sha)
        prev_list = [c for c in _fetch_target_jpg_by_names(db, lock, prev_names) if c["number"] == num - 1]
        if not prev_list:
            return {}
        sec_item = {
            "shot_id": shot["shot_id"],
            "name": shot["name"],
            "number": num,
            "orientation": ori,
            "relpath": jpg["relpath"],
            "sha256": sha,
        }
        _try_pair_for_second(sec_item, {num - 1: prev_list}, pair_store, abspath, coalesce=coalesce)
        return {}

    fl = _ensure_first_like(str(jpg["relpath"]), sha, pair_store, abspath, coalesce=coalesce)
    if not fl or num is None or ori not in hfp.VALID_ORIENTATIONS:
        return {}
    existing = pair_store.pair_for_first(sha)
    if existing is not None:
        prefetched = _fetch_shots_by_jpg_shas(db, lock, {existing["second_sha"]})
        second = prefetched.get(existing["second_sha"])
        if second is not None and int(second.get("orientation") or 1) == ori:
            return prefetched
    next_names = hfp.neighbor_names(shot.get("name"), 1)
    if not next_names:
        return {}
    if use_memo:
        if pair_store.neighbors_checked(sha):
            return {}
        pair_store.mark_neighbors_checked(sha)
    next_list = [
        c
        for c in _fetch_target_jpg_by_names(db, lock, next_names)
        if c["number"] == num + 1 and int(c["orientation"] or 1) == ori and c["sha256"] != sha
    ]
    if not next_list:
        return {}
    self_entry = {
        "shot_id": shot["shot_id"],
        "name": shot["name"],
        "number": num,
        "orientation": ori,
        "relpath": jpg["relpath"],
        "sha256": sha,
    }
    by_num = {num: [self_entry]}
    for sec in next_list:
        if verdict_store.get(sec["sha256"]) is False:
            continue
        _try_pair_for_second(sec, by_num, pair_store, abspath, coalesce=coalesce)
    return {}


def attach_pairs_to_shots(
    shots: list[dict[Any, Any]],
    db: sqlite3.Connection,
    lock: Any,
    verdict_store: hf.VerdictStore,
    pair_store: hfp.PairStore,
    abspath: Callable[[str], Path],
    *,
    allow_decode: bool,
) -> list[dict[Any, Any]]:
    """给一批 shot 写入 `pair` / `pair_sources` / `half_first` 与配对后的 `has_raw`。

    列表路径（`allow_decode=False`）绝不解码文件；当图库里没有任何已存配对时零额外 SQL。
    """
    if not shots:
        return shots
    prefetched: dict[str, dict[str, Any]] = {}
    if allow_decode and len(shots) == 1:
        prefetched = _compute_missing_for_shot(shots[0], db, lock, verdict_store, pair_store, abspath, coalesce=True, use_memo=True)

    needed_shas: set[str] = set()
    page_by_sha: dict[str, dict[Any, Any]] = {}
    matched: list[tuple[dict[Any, Any], str, dict[str, Any]]] = []
    has_any_pairs = pair_store.has_pairs()
    for s in shots:
        s.pop("pair", None)
        s.pop("pair_sources", None)
        s.pop("half_first", None)
        sha = _shot_jpg_sha(s)
        if not sha:
            continue
        page_by_sha[sha] = s
        if not has_any_pairs:
            continue
        if s.get("diptych"):
            rec = pair_store.pair_for_second(sha)
            if rec is not None:
                matched.append((s, "second", rec))
                needed_shas.add(rec["first_sha"])
        else:
            rec = pair_store.pair_for_first(sha)
            if rec is not None:
                matched.append((s, "first", rec))
                needed_shas.add(rec["second_sha"])

    partners_by_sha: dict[str, dict[str, Any]] = dict(prefetched)
    if needed_shas:
        missing = needed_shas - set(page_by_sha) - set(partners_by_sha)
        for sha in needed_shas & set(page_by_sha):
            partners_by_sha[sha] = _source_summary(page_by_sha[sha])
        if missing:
            partners_by_sha.update(_fetch_shots_by_jpg_shas(db, lock, missing))

    paired_ids: set[str] = set()
    for s, role, rec in matched:
        partner_sha = rec["first_sha"] if role == "second" else rec["second_sha"]
        partner = partners_by_sha.get(partner_sha)
        if partner is None:
            continue
        s_ori = int(s.get("orientation") or 1)
        p_ori = int(partner.get("orientation") or 1)
        if s_ori != p_ori or s_ori not in hfp.VALID_ORIENTATIONS:
            continue
        s["pair"] = {
            "partner_id": partner["shot_id"],
            "role": role,
            "first_half": rec["first_half"],
            "flat": bool(rec["flat"]),
            "kind": hf.SEAM_G250,
        }
        paired_ids.add(s["shot_id"])
        if role == "second":
            s_summary = _source_summary(s)
            s["pair_sources"] = {"first": partner, "second": s_summary}
            s["has_raw"] = 1 if ("dng" in partner["all_files"] and "dng" in s_summary["all_files"]) else 0

    for s in shots:
        if s["shot_id"] in paired_ids or s.get("diptych"):
            continue
        sha = _shot_jpg_sha(s)
        if sha and pair_store.get_first_like(sha) is True:
            s["half_first"] = True
    return shots


def resolve_shot_pair(
    shot: dict[Any, Any],
    db: sqlite3.Connection,
    lock: Any,
    verdict_store: hf.VerdictStore,
    pair_store: hfp.PairStore,
    abspath: Callable[[str], Path],
) -> dict[Any, Any]:
    """单张路径补算双联与配对状态（在 `lib.lock` 外执行）。"""
    resolved = hf.resolve_shot(shot, verdict_store, abspath)
    updated = dict(resolved)
    attach_pairs_to_shots([updated], db, lock, verdict_store, pair_store, abspath, allow_decode=True)
    return updated


def paired_raw_second_shas(db: sqlite3.Connection, pair_store: hfp.PairStore) -> set[str]:
    """返回在图库里配对成立且两半都有 DNG 的第 2 张 JPG SHA，供 `Library.dates` 保留其 `raw_count`。"""
    pairs = pair_store.all_pairs()
    if not pairs:
        return set()
    needed = set(pairs.keys()) | {rec["first_sha"] for rec in pairs.values()}
    sha_list = sorted(needed)
    info: dict[str, tuple[int, int]] = {}
    for start in range(0, len(sha_list), 400):
        chunk = sha_list[start : start + 400]
        marks = ",".join("?" * len(chunk))
        rows = db.execute(
            f"SELECT f.sha256, s.has_raw, s.orientation FROM files f JOIN shots s ON s.shot_id = f.shot_id "  # nosec B608
            f"WHERE f.kind = 'jpg' AND f.sha256 IN ({marks})",
            chunk,
        ).fetchall()
        for r in rows:
            info[r["sha256"]] = (int(r["has_raw"]), int(r["orientation"] or 1))
    out: set[str] = set()
    for sec_sha, rec in pairs.items():
        s_info = info.get(sec_sha)
        f_info = info.get(rec["first_sha"])
        if s_info is None or f_info is None:
            continue
        if s_info[0] == 1 and f_info[0] == 1 and s_info[1] == f_info[1] and s_info[1] in hfp.VALID_ORIENTATIONS:
            out.add(sec_sha)
    return out


def ensure_pair_for_shot(
    shot: dict[Any, Any],
    db: sqlite3.Connection,
    lock: Any,
    verdict_store: hf.VerdictStore,
    pair_store: hfp.PairStore,
    abspath: Callable[[str], Path],
) -> None:
    """导入或补全完成后，为新照片计算 `first_like` 并检查它在整个图库里的相邻编号伙伴。"""
    _compute_missing_for_shot(shot, db, lock, verdict_store, pair_store, abspath, coalesce=False)


def rebuild_pairs(
    items: list[dict[str, Any]],
    verdict_store: hf.VerdictStore,
    pair_store: hfp.PairStore,
    abspath: Callable[[str], Path],
) -> None:
    """`Library.rebuild` 在释放 `lib.lock` 后重建配对缓存；已存的 `first_like` 与配对直接复用。"""
    valid = [it for it in items if it.get("sha256") and hf.is_target_dims(it.get("width"), it.get("height"))]
    by_num = _index_by_number(valid)
    fl_updates: dict[str, bool] = {}
    pair_updates: dict[str, dict[str, Any]] = {}
    rejected_updates: set[tuple[str, str]] = set()

    for item in valid:
        sha = str(item["sha256"])
        known_fl = pair_store.get_first_like(sha)
        if known_fl is not None:
            fl_updates[sha] = known_fl
        elif verdict_store.get(sha) is True:
            fl_updates[sha] = False
        else:
            detected = hfp.detect_first_like(abspath(item["relpath"]))
            if detected is not None:
                fl_updates[sha] = detected

    for item in valid:
        sec_sha = str(item["sha256"])
        sec_num = item.get("number")
        sec_ori = int(item.get("orientation") or 1)
        if sec_num is None or sec_ori not in hfp.VALID_ORIENTATIONS or verdict_store.get(sec_sha) is not True:
            continue
        prev_list = by_num.get(int(sec_num) - 1) or []
        if not prev_list:
            continue
        stored = pair_store.pair_for_second(sec_sha)
        if stored is not None and any(
            c["sha256"] == stored["first_sha"] and int(c["orientation"] or 1) == sec_ori and fl_updates.get(str(c["sha256"])) is True
            for c in prev_list
        ):
            pair_updates[sec_sha] = stored
            continue
        rec = _match_second_candidate(
            item,
            prev_list,
            lambda c: fl_updates.get(str(c["sha256"])) is True,
            abspath,
            pair_store=pair_store,
            rejected_out=rejected_updates,
        )
        if rec is not None:
            pair_updates[sec_sha] = rec

    pair_store.put_batch(fl_updates, pair_updates, rejected=rejected_updates, save=False)
    pair_store.flush_quietly()


def rebuild_pairs_from_db(
    db: sqlite3.Connection,
    lock: Any,
    verdict_store: hf.VerdictStore,
    pair_store: hfp.PairStore,
    abspath: Callable[[str], Path],
) -> None:
    """从刚重建好的索引取出候选 JPG，在 `lib.lock` 外重建配对缓存。"""
    pair_store.forget_neighbors_checked()
    rebuild_pairs(_fetch_target_jpg_candidates(db, lock), verdict_store, pair_store, abspath)


def pending_pairs(db: sqlite3.Connection, verdict_store: hf.VerdictStore, pair_store: hfp.PairStore) -> list[dict[str, Any]]:
    """查出还需要后台补算 `first_like` 或候选配对的 4288×2848 JPG。"""
    items = _query_target_jpg_rows(db)
    by_num = _index_by_number(items)
    todo: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        sha = str(item["sha256"])
        if sha in seen:
            continue
        needs_fl = not pair_store.has_first_like(sha)
        needs_pair = False
        if verdict_store.get(sha) is True and pair_store.pair_for_second(sha) is None:
            num = item["number"]
            ori = item["orientation"]
            if num is not None and ori in hfp.VALID_ORIENTATIONS:
                prev_list = by_num.get(int(num) - 1) or []
                needs_pair = any(
                    c["sha256"] != sha
                    and int(c["orientation"]) == ori
                    and pair_store.get_first_like(c["sha256"]) is not False
                    and not pair_store.is_rejected(str(c["sha256"]), sha)
                    for c in prev_list
                )
        if needs_fl or needs_pair:
            seen.add(sha)
            todo.append(item)
    return todo


def backfill_pairs(
    lib: Any,
    todo: list[dict[str, Any]],
    progress: hf.HalfframeProgress,
    cancel: threading.Event,
    *,
    batch_size: int = 32,
) -> None:
    """在 `lib.lock` 外分批补算 `first_like` 与 g250 配对，按 `strict=True` 写回 `.studio/halfframe-pairs-v1.json`。"""
    fl_batch: dict[str, bool] = {}
    pair_batch: dict[str, dict[str, Any]] = {}
    rejected_batch: set[tuple[str, str]] = set()

    for item in todo:
        if cancel.is_set():
            break
        sha = str(item["sha256"])
        if not lib._pairs.has_first_like(sha):
            if lib._halfframe.get(sha) is True:
                fl_batch[sha] = False
            else:
                detected = hfp.detect_first_like(lib.abspath(item["relpath"]))
                if detected is not None:
                    fl_batch[sha] = detected
        progress.advance(False)
        if len(fl_batch) >= batch_size:
            lib.record_halfframe_pairs_batch(fl_batch, {})
            fl_batch.clear()
    if fl_batch:
        lib.record_halfframe_pairs_batch(fl_batch, {})
        fl_batch.clear()
    if cancel.is_set():
        return

    all_items = _fetch_target_jpg_candidates(lib.db, lib.lock)
    by_num = _index_by_number(all_items)
    for item in all_items:
        if cancel.is_set():
            break
        sec_sha = str(item["sha256"])
        sec_num = item.get("number")
        sec_ori = int(item.get("orientation") or 1)
        if sec_num is None or sec_ori not in hfp.VALID_ORIENTATIONS:
            continue
        if lib._halfframe.get(sec_sha) is not True or lib._pairs.pair_for_second(sec_sha) is not None:
            continue
        prev_list = by_num.get(int(sec_num) - 1) or []
        rec = _match_second_candidate(
            item,
            prev_list,
            lambda c: lib._pairs.get_first_like(c["sha256"]) is True,
            lib.abspath,
            pair_store=lib._pairs,
            rejected_out=rejected_batch,
        )
        if rec is not None:
            pair_batch[sec_sha] = rec
        if len(pair_batch) + len(rejected_batch) >= batch_size:
            lib.record_halfframe_pairs_batch({}, pair_batch, rejected_batch)
            pair_batch.clear()
            rejected_batch.clear()
    if pair_batch or rejected_batch:
        lib.record_halfframe_pairs_batch({}, pair_batch, rejected_batch)
