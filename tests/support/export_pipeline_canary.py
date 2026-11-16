"""A pytest plugin for the privacy regressions (Sol r1 P1, r2 P1). A child pytest loads it with
``-p support.export_pipeline_canary``; ``EXPORT_PIPELINE_CANARY_TARGET`` picks what to break:

- ``sizes``: ``test_output_sizes_match_desktop`` on a synthetic JPG whose LensModel is the canary;
  one returned ``meta.lens`` is changed;
- ``bytes``: ``test_e2e_camera_jpg_shots_match_export_finish`` on a synthetic JPG whose Make and
  LensModel are the canary; the canary in Rust's finished file is overwritten (file bytes);
- ``raw``: ``test_raw_branches_real_pairs_match_export_finish`` (the ``run_case`` RAW and LUT share)
  on a synthetic DNG and that JPG; the canary in Rust's finished file is overwritten.

The parent then checks that the canary reaches none of stdout, stderr or the JUnit file. No real
photo is read. The HDR Gate A/B and HDR-state paths are in ``export_pipeline_canary_probe.py``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

CANARY = "EXPORT_PIPELINE_EXIF_REDACTION_CANARY"


def _synthetic(folder: Path, size: tuple[int, int] = (120, 80)) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    jpg = folder / "R0001.JPG"
    exif = Image.Exif()
    exif[271] = CANARY
    exif[272] = "SYNTHETIC"
    exif.get_ifd(0x8769)[42036] = CANARY
    Image.fromarray(np.full((size[1], size[0], 3), 128, np.uint8)).save(jpg, "JPEG", exif=exif)
    return jpg


def _scrub_out(args: list[str]) -> None:
    """Overwrite the canary in the file ``export-run-complete`` wrote (same length)."""
    if args and args[0] == "export-run-complete" and "--out" in args:
        out = Path(args[args.index("--out") + 1])
        if out.is_file():
            data = out.read_bytes()
            out.write_bytes(data.replace(CANARY.encode(), b"X" * len(CANARY)))


def pytest_collection_modifyitems(items: list[Any]) -> None:
    folder = Path(os.environ["EXPORT_PIPELINE_CANARY_DIR"])
    target = os.environ.get("EXPORT_PIPELINE_CANARY_TARGET", "sizes")
    if target == "raw":
        from support import export_source_cases as xs
        from support.dng_writer import write_dng
        from test_core_parity_export_source import _mosaic

        jpg = _synthetic(folder, (64, 48))
        dng = folder / "R0001.DNG"
        dng.write_bytes(write_dng(_mosaic(48, 64, 1), white=4095))
        xs.real_pairs = lambda _n: [(dng, jpg)] * 60
    else:
        jpg = _synthetic(folder)
        dng = folder / "R0001.DNG"
        dng.write_bytes(b"SYNTHETIC FILE, NOT READ AS RAW BY THIS TEST")
    for item in items:
        module = item.module
        if target == "sizes":
            module.real_pairs = lambda _n: [(jpg, dng)] * 6
        elif target == "bytes":
            module.real_pairs = lambda _n: [(jpg, dng)] * 18
        real_run = module.run_json

        def run(args: list[str], real_run: Any = real_run, **kwargs: Any) -> Any:
            code, payload = real_run(args, **kwargs)
            if code == 0 and target == "sizes" and args[0] == "export-run-sizes":
                payload["meta"]["lens"] = "INTENTIONALLY_DIFFERENT"
            if code == 0 and target in ("bytes", "raw"):
                _scrub_out(args)
            return code, payload

        module.run_json = run
