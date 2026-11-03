"""Bounded, separate runtime checks for Windows CI diagnosis."""

from __future__ import annotations

import argparse
import asyncio
import faulthandler
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", choices=("io", "loop", "lut"))
    args = parser.parse_args()
    print(json.dumps({"case": args.case, "python": sys.version, "platform": sys.platform}), flush=True)
    faulthandler.dump_traceback_later(120, exit=True)
    try:
        if args.case == "io":
            from gxr_studio.privacy import PrivateDir

            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "app-data"
                content = bytes(range(256)) * 16
                with PrivateDir(root, "runtime-check") as directory:
                    one, two = root.stat(), os.fstat(directory.fd)
                    assert (one.st_dev, one.st_ino) == (two.st_dev, two.st_ino)
                    directory.write("nested/first.bin", content)
                    assert directory.read("nested/first.bin") == content
                    assert directory.names("nested") == ["first.bin"]
                    directory.replace_file("nested/first.bin", content[::-1])
                    assert directory.read("nested/first.bin") == content[::-1]
                    directory.remove("nested")
                    assert not directory.exists("nested")
        elif args.case == "loop":
            from gxr_studio import audit

            audit.forbid_network(True)
            try:
                loop = asyncio.new_event_loop()
                loop.close()
            finally:
                print(json.dumps({"network_attempts": audit.network_attempts()}), flush=True)
                audit.forbid_network(False)
        else:
            import numpy as np

            from gxr_studio.gpu.tables import lut_bytes
            from gxr_studio.lut.cube import Cube
            from gxr_studio.lut.spaces import Target

            np.show_config()
            v = np.linspace(0, 1, 17)
            nodes = np.clip(np.array([[r, g, b] for b in v for g in v for r in v]) * [0.86, 0.97, 1.05], 0, 1)
            text = "LUT_1D_SIZE 3\nLUT_3D_SIZE 17\n0 0 0\n0.6 0.55 0.5\n1 1 1\n"
            text += "\n".join(" ".join(f"{c:.17g}" for c in row) for row in nodes) + "\n"
            data, meta, reason = lut_bytes(Target(Cube.parse(text), "srgb", "vlog"))
            assert data is not None and len(data) == 67108864 and reason is None
            print(json.dumps({"bytes": len(data), "meta": meta, "sha256": hashlib.sha256(data).hexdigest()}), flush=True)
    finally:
        faulthandler.cancel_dump_traceback_later()
    print("runtime check passed", flush=True)


if __name__ == "__main__":
    main()
