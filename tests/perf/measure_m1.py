"""M1 验收 10：1000 张合成照片的后端数字（导入、索引查询、缩略图），写入 baseline.json。

不是 pytest 用例（要几十秒）。运行：  uv run python tests/perf/measure_m1.py
全程在临时目录里，不碰真实的卡和默认图库。
"""

from __future__ import annotations

import json
import platform
import random
import shutil
import statistics
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient
from PIL import Image

from gxr_studio import audit
from gxr_studio.app import create_app
from gxr_studio.config import Settings
from gxr_studio.devtools.synthetic_card import _exif

N = 1000
SIZE = (1600, 1067)
OUT = Path(__file__).with_name("baseline.json")


def build_card(volumes: Path) -> Path:
    d = volumes / "PERF" / "DCIM" / "100RICOH"
    d.mkdir(parents=True)
    rng = np.random.default_rng(1)
    base = datetime(2026, 7, 1, 9, 0, 0)
    pool = [Image.fromarray(rng.integers(0, 255, (SIZE[1] // 8, SIZE[0] // 8, 3), dtype=np.uint8)).resize(SIZE, Image.BICUBIC) for _ in range(8)]
    for i in range(N):
        taken = base + timedelta(days=i // 10, minutes=i % 10)
        img = pool[i % 8].copy()
        img.putpixel((i % SIZE[0], (i * 7) % SIZE[1]), (i % 256, 255 - i % 256, 7))  # 让每张的哈希不同
        img.save(d / f"R{8000000 + i:07d}.JPG", "JPEG", quality=88, exif=_exif(taken, "ok", 1))
    return volumes


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="gxr-perf-"))
    try:
        audit.forbid_network(True)
        volumes = build_card(tmp / "volumes")
        settings = Settings(
            library_dir=tmp / "library", home_dir=tmp / "home", export_dir=tmp / "exports",
            volumes_roots=(volumes,), token="perf", port=8765, min_free_bytes=0,
        )
        app = create_app(settings)
        res: dict = {
            "photos": N, "jpg_size_px": SIZE, "note": "合成 JPG（噪声图），导入含缩略图生成；单次实测，不是基准承诺",
            "machine": {"platform": platform.platform(), "python": sys.version.split()[0], "processor": platform.processor()},
        }
        with TestClient(app, base_url="http://127.0.0.1:8765", headers={"X-GXR-Token": "perf"}) as c:
            card_id = c.get("/api/cards").json()[0]["id"]
            t = time.perf_counter()
            job = c.post("/api/import", json={"card_id": card_id}).json()
            while job["running"]:
                time.sleep(0.2)
                job = c.get(f"/api/jobs/{job['id']}").json()
            res["import_seconds"] = round(time.perf_counter() - t, 2)
            res["import_state"] = job["progress"]["state"]
            res["import_seconds_per_photo"] = round(res["import_seconds"] / N, 4)

            def timed(fn, n=20):
                xs = []
                for _ in range(n):
                    t0 = time.perf_counter()
                    fn()
                    xs.append((time.perf_counter() - t0) * 1000)
                return {"p50_ms": round(statistics.median(xs), 2), "max_ms": round(max(xs), 2)}

            dates = c.get("/api/dates").json()
            res["dates"] = len(dates)
            res["api_dates"] = timed(lambda: c.get("/api/dates"))
            res["api_photos_first_page_60"] = timed(lambda: c.get("/api/photos", params={"limit": 60}))
            res["api_photos_all_1000"] = timed(lambda: c.get("/api/photos", params={"limit": 1000}), 5)
            photos = c.get("/api/photos", params={"limit": 1000}).json()
            res["thumb_warm_first_screen_60"] = timed(lambda: [c.get(p["thumb_url"]) for p in photos[:60]], 5)
            # 清掉缩略图缓存，量冷启动
            shutil.rmtree(settings.library_dir / ".studio" / "thumbs")
            (settings.library_dir / ".studio" / "thumbs").mkdir()
            t0 = time.perf_counter()
            for p in photos[:60]:
                c.get(p["thumb_url"])
            res["thumb_cold_first_screen_60_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            t0 = time.perf_counter()
            c.get(photos[0]["preview_url"])
            res["preview_cold_1600_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            t0 = time.perf_counter()
            rep = c.post("/api/library/rebuild").json()
            res["rebuild_index_seconds"] = round(time.perf_counter() - t0, 2)
            res["rebuild_shots"] = rep["shots"]
        OUT.write_text(json.dumps(res, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(res, ensure_ascii=False, indent=2))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
