"""成品比例经过默认值、sidecar、粘贴和前端归一化后仍然一致。"""

from __future__ import annotations

import json

import pytest

from gxr_studio.finish.serve import defaults_path
from gxr_studio.finish.spec import OUTPUT_RATIOS, FrameSpec
from test_api import find
from test_finish_routes import client as client
from test_paste_edits import paste, state
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
def test_old_stored_settings_reset_and_new_invalid_writes_fail(client, legacy):
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
    assert paste(client, ["frame"], [photo], source={"frame": old}).status_code == 422
    assert state(client, photo)["crop"] == before
    assert client.put("/api/defaults/finish", json={"frame": old, "stamp": {}}).status_code == 422
    # Saving the factory result carries no retired keys.
    assert client.put(f"/api/photos/{photo['id']}/edit", json={"frame": expected}).status_code == 200
    assert json.loads(path.read_text())["border"] == expected


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


@pytest.mark.parametrize(
    ("style", "fmt", "table"),
    [
        ("film", "135", "light"),
        ("film", "120", "black"),
        ("instant", "polaroid", "light"),
        ("instant", "instax_square", "light"),
        ("instant", "instax_wide", "light"),
        ("instant", "instax_mini", "light"),
    ],
)
def test_format_options_survive_defaults_paste_and_undo(client, style, fmt, table):
    from gxr_studio.finish.spec import parse_frame
    from test_paste_edits import ok

    source, target = find(client, "R0000102"), find(client, "R0000103")
    frame = {"style": style, "fmt": fmt, "table": table, "text": "Harbour", "show_date": True, "output_ratio": "4:5"}
    parsed = parse_frame(frame)
    assert parsed is not None
    expected = parsed.to_json()
    before = state(client, target)
    assert client.put(f"/api/photos/{source['id']}/edit", json={"frame": frame}).status_code == 200
    assert client.put("/api/defaults/finish", json={"frame": frame, "stamp": {}}).status_code == 200
    assert client.get("/api/defaults/finish").json()["frame"] == expected
    result = ok(paste(client, ["frame"], [target], source_id=source["id"]))
    assert state(client, target)["edit"]["frame"] == expected
    assert client.post("/api/edits/paste/undo", json={"batch_id": result["batch_id"]}).status_code == 200
    assert state(client, target) == before


@pytest.mark.parametrize(
    "raw",
    [
        {"style": "gallery"},
        {"version": 1, "style": "film"},
        {"version": 2, "style": "film", "fmt": "instax_wide"},
        {"version": 2, "style": "instant", "fmt": "120"},
        {"version": 2, "style": "gallery", "unknown": True},
        {"version": 2, "style": ["film"]},
        {"version": 2, "style": "gallery", "show_date": "yes"},
        ["bad"],
        42,
    ],
)
def test_bad_stored_frames_load_as_factory_without_crashing(client, raw):
    context = client.app.state.ctx
    context.settings.home_dir.mkdir(parents=True, exist_ok=True)
    defaults_path(context.settings.home_dir).write_text(json.dumps({"frame": raw, "stamp": {}}))
    assert client.get("/api/defaults/finish").json()["frame"] == FrameSpec().to_json()
    photo = find(client, "R0000102")
    path = context.lib.sidecar_path_for(context.lib.get_shot(photo["id"]))
    side = json.loads(path.read_text())
    side["border"] = raw
    path.write_text(json.dumps(side))
    response = client.get(f"/api/photos/{photo['id']}/edit")
    assert response.status_code == 200, response.text
    assert response.json()["frame"] == FrameSpec().to_json()


@pytest.mark.parametrize("change", [{"adjust": {"exposure_ev": 0.5}}, {"crop": {"preset": "1:1"}}])
def test_writing_another_edit_does_not_save_retired_border_keys(client, change):
    photo = find(client, "R0000102")
    lib = client.app.state.ctx.lib
    path = lib.sidecar_path_for(lib.get_shot(photo["id"]))
    side = json.loads(path.read_text())
    side["border"] = {"style": "film", "keep_ratio": False, "garbage": "old"}
    path.write_text(json.dumps(side))
    if "crop" in change:
        response = client.put(f"/api/photos/{photo['id']}/crop", json=change["crop"])
    else:
        response = client.put(f"/api/photos/{photo['id']}/edit", json=change)
    assert response.status_code == 200, response.text
    assert json.loads(path.read_text())["border"] == FrameSpec().to_json()


def test_browser_rejects_malformed_stored_frames_and_keeps_new_format_keys(tmp_path):
    out = run_node(
        tmp_path,
        """
      const bad=[{version:2,style:null,color:'black'}, {version:2,style:[],color:'black'},
        {version:2,style:'film',fmt:'instax_mini'}, {version:2,style:'instant',fmt:'120'},
        {version:2,style:'gallery',old:true}];
      const source=F.normalizeFrame({style:'film',fmt:'120',table:'black',show_date:true});
      return {bad:bad.map(frame=>F.resolveFinish({frame},{}).frame), body:F.frameBody(source),
        instant:F.selectFrame(source,'instant'),leica:F.selectFrame(source,'leica')};
    """,
    )
    assert all(value == FrameSpec().to_json() for value in out["bad"])
    assert out["body"]["fmt"] == "120" and out["body"]["table"] == "black" and out["body"]["version"] == 2
    assert out["instant"]["fmt"] == "polaroid" and out["instant"]["show_date"] is False
    assert out["leica"]["show_date"] is True


def test_frame_constructors_use_the_approved_date_defaults():
    assert FrameSpec(style="leica").show_date is True
    assert FrameSpec(style="hasselblad").show_date is True
    assert FrameSpec(style="instant").show_date is False
    assert FrameSpec(style="leica", show_date=False).show_date is False
