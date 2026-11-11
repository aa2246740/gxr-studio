"""Custom logo regressions found during the closeout review."""

from __future__ import annotations

import io
import json
import subprocess
import threading
import warnings
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageDraw

from gxr_studio import privacy
from gxr_studio.finish.logo_store import LogoStore, decode_safe_rgba, prepare_uploaded_logo
from gxr_studio.finish.spec import parse_frame
from test_web_frontend import run_node


def _png(size: tuple[int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("L", size, 123).save(buffer, format="PNG")
    return buffer.getvalue()


def test_logo_upload_does_not_change_concurrent_photo_decoding(monkeypatch: pytest.MonkeyPatch) -> None:
    logo = _png((128, 128))
    photo = _png((6000, 5000))
    opened, resume = threading.Event(), threading.Event()
    original_open = Image.open

    def paused_open(*args: Any, **kwargs: Any) -> Image.Image:
        image = original_open(*args, **kwargs)
        opened.set()
        assert resume.wait(timeout=10)
        return image

    monkeypatch.setattr(Image, "open", paused_open)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(decode_safe_rgba, logo)
        try:
            assert opened.wait(timeout=10)
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with original_open(io.BytesIO(photo)) as image:
                    assert image.size == (6000, 5000)
        finally:
            resume.set()
            assert pending.result(timeout=10).size == (128, 128)


@pytest.mark.parametrize(
    ("logo", "show_logo", "expected_logo", "expected_show"),
    [
        ("ricoh", False, "none", False),
        ("ricoh", True, "ricoh", True),
        ("none", True, "none", False),
        ("u_saved", False, "u_saved", True),
    ],
)
def test_browser_and_api_agree_on_legacy_logo_switch(
    tmp_path: Path, logo: str, show_logo: bool, expected_logo: str, expected_show: bool
) -> None:
    raw = {"style": "leica", "logo": logo, "show_logo": show_logo}
    browser = run_node(tmp_path, f"return F.normalizeFrame({json.dumps(raw)});")
    parsed = parse_frame(raw)
    assert parsed is not None
    assert browser["logo"] == parsed.logo == expected_logo
    assert browser["show_logo"] == parsed.show_logo == expected_show


def test_unknown_prototype_names_fall_back_to_ricoh_in_preview() -> None:
    assets = Path(__file__).resolve().parents[1] / "web" / "assets"
    script = (
        f"import {{resolveLogo}} from {json.dumps((assets / 'finish_layout.js').as_uri())};"
        f"import {{useLogos, getLogoMap}} from {json.dumps((assets / 'finish_frame_core.js').as_uri())};"
        "useLogos([]);"
        "const ids=['missing_logo','constructor','__proto__'];"
        "const fallback=ids.map(logo=>[resolveLogo({style:'leica',logo}),resolveLogo({style:'leica',logo},{})]);"
        "const listed=ids.map(id=>Boolean(getLogoMap()[id]));"
        "console.log(JSON.stringify({fallback,listed}));"
    )
    result = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, timeout=20, check=True)
    result_data = json.loads(result.stdout)
    assert result_data["fallback"] == [[["ricoh", None], ["ricoh", None]]] * 3
    assert result_data["listed"] == [False, False, False]


@pytest.mark.parametrize("key_mode", [True, "auto"])
def test_white_keying_preserves_existing_logo_transparency(key_mode: str | bool) -> None:
    mark = Image.new("RGBA", (160, 128), (12, 34, 56, 0))
    ImageDraw.Draw(mark).ellipse((16, 16, 144, 112), fill=(50, 20, 70, 255))
    buffer = io.BytesIO()
    mark.save(buffer, format="PNG")
    png, metadata = prepare_uploaded_logo(buffer.getvalue(), key_mode=key_mode)
    with Image.open(io.BytesIO(png)) as saved:
        assert saved.getchannel("A").getextrema() == (0, 255)
        assert saved.getpixel((0, 0))[3] == 0
        assert saved.getpixel((saved.width // 2, saved.height // 2)) == (50, 20, 70, 255)
    assert metadata["keyed"] is False and metadata["opaque"] is False


@pytest.mark.parametrize("repository", [False, True])
def test_logo_upload_rejects_storage_symlink_into_protected_directory(tmp_path: Path, repository: bool) -> None:
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    if repository:
        (blocked / ".git").mkdir()
    app_data = tmp_path / "app-data"
    app_data.mkdir()
    (app_data / "logos").symlink_to(blocked, target_is_directory=True)
    store = LogoStore(app_data, forbidden=() if repository else (blocked,))
    with pytest.raises(privacy.PrivatePathError):
        store.add_logo(_png((128, 128)), name="local mark")
    assert sorted(path.name for path in blocked.iterdir()) == ([".git"] if repository else [])
