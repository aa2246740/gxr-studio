"""成品比例经过默认值、sidecar、粘贴和前端归一化后仍然一致。"""

from __future__ import annotations

import json

import pytest

from gxr_studio.finish.serve import defaults_path
from gxr_studio.finish.spec import OUTPUT_RATIOS, FrameSpec
from test_api import find
from test_finish_routes import client as client
from test_paste_edits import ok, paste, state
from test_web_frontend import run_node

LEGACY = [{"keep_ratio": False}, {"canvas_ratio": "9:16"}, {"style": "canvas"}, {"keep_ratio": True, "output_ratio": "16:9"}]


@pytest.mark.parametrize("ratio", (*OUTPUT_RATIOS, "original"))
def test_defaults_and_sidecars_round_trip_the_output_ratio(client, ratio):
    frame = {"style": "gallery", "output_ratio": ratio}
    result = client.put("/api/defaults/finish", json={"frame": frame, "stamp": {"enabled": False}})
    assert result.status_code == 200, result.text
    assert client.get("/api/defaults/finish").json()["frame"]["output_ratio"] == ratio
    pid = find(client, "R0000102")["id"]
    assert client.put(f"/api/photos/{pid}/edit", json={"frame": frame}).status_code == 200
    assert client.get(f"/api/photos/{pid}/edit").json()["frame"]["output_ratio"] == ratio
    side = client.app.state.ctx.lib.sidecar_path_for(client.app.state.ctx.lib.get_shot(pid))
    assert json.loads(side.read_text())["border"]["output_ratio"] == ratio


@pytest.mark.parametrize("legacy", LEGACY)
def test_old_defaults_sidecars_and_pastes_reset_to_factory_frame(client, legacy):
    old = {"style": "gallery", "color": "black", **legacy}
    expected = FrameSpec().to_json()
    context = client.app.state.ctx
    context.settings.home_dir.mkdir(parents=True, exist_ok=True)
    defaults_path(context.settings.home_dir).write_text(json.dumps({"frame": old, "stamp": {}}))
    assert client.get("/api/defaults/finish").json()["frame"] == expected
    photo = find(client, "R0000102")
    shot = context.lib.get_shot(photo["id"])
    path = context.lib.sidecar_path_for(shot)
    side = json.loads(path.read_text())
    side["border"] = old
    path.write_text(json.dumps(side))
    assert client.get(f"/api/photos/{photo['id']}/edit").json()["frame"] == expected
    before = state(client, photo)["crop"]
    response = ok(paste(client, ["frame"], [photo], source={"frame": old}))
    assert response["results"][0]["status"] == "applied"
    assert state(client, photo)["edit"]["frame"] == expected
    assert state(client, photo)["crop"] == before
    saved = json.loads(path.read_text())["border"]
    assert saved == expected and "keep_ratio" not in saved and "canvas_ratio" not in saved
    assert client.put("/api/defaults/finish", json={"frame": old, "stamp": {}}).status_code == 200
    assert json.loads(defaults_path(context.settings.home_dir).read_text())["frame"] == expected


def test_browser_defaults_and_frame_payload_use_only_the_new_key(tmp_path):
    out = run_node(
        tmp_path,
        """
      const f = F.normalizeFrame({style:'instant', output_ratio:'9:16'});
      return {body:F.frameBody(f), defaults:F.defaultsPayload(f, {}),
        legacy:[{keep_ratio:false}, {canvas_ratio:'9:16'}, {style:'canvas'}].map((x) => F.normalizeFrame(x))};
    """,
    )
    assert out["body"]["output_ratio"] == out["defaults"]["frame"]["output_ratio"] == "9:16"
    assert "keep_ratio" not in out["body"] and "canvas_ratio" not in out["body"]
    for frame in out["legacy"]:
        assert frame["style"] == "none" and frame["output_ratio"] == "3:4"
        assert "keep_ratio" not in frame and "canvas_ratio" not in frame
