"""A pytest plugin for the privacy regression (Sol r1 P1): the real ``test_output_sizes_match_desktop``
runs on a synthetic JPG whose EXIF LensModel is a canary, and one ``meta.lens`` that
``export-run-sizes`` returns is changed, so the comparison must fail. Loaded with
``-p support.export_pipeline_canary`` in a child pytest; the parent then checks that the canary
appears in none of stdout, stderr or the JUnit file. No real photo is read."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

CANARY = "EXPORT_PIPELINE_EXIF_REDACTION_CANARY"


def pytest_collection_modifyitems(items: list[Any]) -> None:
    folder = Path(os.environ["EXPORT_PIPELINE_CANARY_DIR"])
    folder.mkdir(parents=True, exist_ok=True)
    jpg, dng = folder / "synthetic.JPG", folder / "synthetic.DNG"
    exif = Image.Exif()
    exif[271] = "SYNTHETIC"
    exif[272] = "SYNTHETIC"
    exif.get_ifd(0x8769)[42036] = CANARY
    Image.fromarray(np.full((80, 120, 3), 128, np.uint8)).save(jpg, "JPEG", exif=exif)
    dng.write_bytes(b"SYNTHETIC FILE, NOT READ AS RAW BY THIS TEST")
    for item in items:
        module = item.module
        module.real_pairs = lambda _n: [(jpg, dng)] * 6
        real_run = module.run_json

        def corrupt(args: list[str], real_run: Any = real_run, **kwargs: Any) -> Any:
            code, payload = real_run(args, **kwargs)
            if code == 0 and args[0] == "export-run-sizes":
                payload["meta"]["lens"] = "INTENTIONALLY_DIFFERENT"
            return code, payload

        module.run_json = corrupt
