"""只监听本机、令牌、Host/Origin 校验、审计钩子、离线。"""

from __future__ import annotations

import os
import socket

import pytest
from fastapi.testclient import TestClient

from conftest import PORT
from gxr_studio import audit, cli
from gxr_studio.app import create_app


@pytest.fixture
def app(settings):
    return create_app(settings)


def mk(app, host=f"127.0.0.1:{PORT}", **kw):
    return TestClient(app, base_url=f"http://{host}", **kw)


def test_no_token_is_401(app):
    with mk(app) as c:
        assert c.get("/api/health").status_code == 401
        assert c.get("/").status_code == 401
        assert c.get("/api/photos").json()["error"] == "token_required"
        assert c.get("/api/health", headers={"X-GXR-Token": "wrong"}).status_code == 401


def test_token_via_query_becomes_httponly_cookie(app, settings):
    with mk(app) as c:
        r = c.get(f"/?token={settings.token}", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/"
        cookie = r.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and settings.token in cookie
        assert c.get("/", follow_redirects=True).status_code == 200  # cookie 已记住
        assert c.get("/api/health").status_code == 200
    with mk(app) as fresh:
        assert fresh.get("/?token=bad", follow_redirects=False).status_code == 401


def test_bearer_header_works(app, settings):
    with mk(app) as c:
        assert c.get("/api/health", headers={"Authorization": f"Bearer {settings.token}"}).status_code == 200


@pytest.mark.parametrize(
    "host",
    ["evil.example.com", "127.0.0.1", "127.0.0.1:9999", "localhost:1", "0.0.0.0:8765", "127.0.0.1.evil.com:8765"],
)
def test_bad_host_is_403(app, settings, host):
    with mk(app, host=host, headers={"X-GXR-Token": settings.token}) as c:
        r = c.get("/api/health")
        assert r.status_code == 403 and r.json()["error"] == "host_not_allowed"


def test_localhost_name_is_fine(app, settings):
    with mk(app, host=f"localhost:{PORT}", headers={"X-GXR-Token": settings.token}) as c:
        assert c.get("/api/health").status_code == 200


def test_foreign_origin_is_403_even_with_token(app, settings):
    with mk(app, headers={"X-GXR-Token": settings.token}) as c:
        for origin in ("http://evil.example.com", "http://127.0.0.1:9999", "null", "https://127.0.0.1:8765"):
            r = c.get("/api/health", headers={"Origin": origin})
            assert r.status_code == 403 and r.json()["error"] == "origin_not_allowed", origin
        assert c.get("/api/health", headers={"Origin": f"http://127.0.0.1:{PORT}"}).status_code == 200
        assert c.get("/api/health", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_cookie_alone_cannot_do_writes_without_origin(app, settings):
    """跨站表单提交会带 cookie 但不一定有可控的头；没有 Origin 的写操作必须用头部令牌。"""
    with mk(app) as c:
        c.get(f"/?token={settings.token}", follow_redirects=True)  # 拿到 cookie
        r = c.post("/api/library/rebuild")
        assert r.status_code == 403 and r.json()["error"] == "origin_required"
        ok = c.post("/api/library/rebuild", headers={"Origin": f"http://127.0.0.1:{PORT}"})
        assert ok.status_code == 200
        ok2 = c.post("/api/library/rebuild", headers={"X-GXR-Token": settings.token})
        assert ok2.status_code == 200


def test_error_responses_are_not_cacheable_and_have_nosniff(app, settings):
    with mk(app, headers={"X-GXR-Token": settings.token}) as c:
        r = c.get("/api/health")
        assert r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"
        assert "access-control-allow-origin" not in r.headers


def test_cli_binds_only_loopback(monkeypatch, tmp_path, settings):
    bound = {}

    class FakeServer:
        def __init__(self, config):
            bound["app"] = config.app

        def run(self, sockets):
            bound["sockets"] = sockets

    monkeypatch.setattr(cli.uvicorn, "Server", FakeServer)
    monkeypatch.setenv("GXR_STUDIO_LIBRARY", str(tmp_path / "lib"))
    monkeypatch.setenv("GXR_STUDIO_EXPORT", str(tmp_path / "exp"))
    monkeypatch.setenv("GXR_STUDIO_VOLUMES", str(tmp_path))
    cli.main(["--no-browser"])
    (sock,) = bound["sockets"]
    try:
        host, port = sock.getsockname()
        assert host == "127.0.0.1" and port > 0 and sock.family == socket.AF_INET
    finally:
        sock.close()


def test_default_settings_would_use_pictures_dir_but_tests_never_touch_it(monkeypatch, tmp_path):
    from gxr_studio.config import Settings

    s = Settings.from_env()
    assert s.library_dir == tmp_path / "home" / "Pictures" / "GXR Studio"  # HOME 已被换成临时目录
    real = os.path.expanduser("~")
    assert str(tmp_path) in real


# ---- 审计钩子本身 -----------------------------------------------------------


def test_audit_records_writes_and_blocks_protected(tmp_path):
    root = tmp_path / "watched"
    root.mkdir()
    with audit.AuditRecorder(watch=(str(root),), record_reads=True) as rec:
        (root / "a.txt").write_text("x")
        (root / "a.txt").read_text()
        os.rename(root / "a.txt", root / "b.txt")
        os.remove(root / "b.txt")
    ops = [(w.op, os.path.basename(w.path)) for w in rec.writes]
    assert ("open", "a.txt") in ops and ("os.rename", "b.txt") in ops and ("os.remove", "b.txt") in ops
    assert any(r.endswith("a.txt") for r in rec.reads)
    audit.protect(root)
    try:
        with audit.AuditRecorder(watch=(str(root),)) as rec2:
            with pytest.raises(PermissionError):
                (root / "c.txt").write_text("x")
        assert rec2.writes and rec2.writes[0].blocked and not (root / "c.txt").exists()
    finally:
        audit.unprotect(root)


def test_real_volumes_are_off_limits_in_tests():
    for target in ("/Volumes", "/Volumes/RICOHDCX", "/Volumes/RICOHDCX/DCIM"):
        with pytest.raises(PermissionError):
            os.listdir(target)
        with pytest.raises(PermissionError):
            open(target + "/x", "rb")
    if not os.path.isdir("/Volumes/Workspace"):
        pytest.skip("工作盘 /Volumes/Workspace 不在本机，跳过 stat 不拦截的检查")
    assert os.path.isdir("/Volumes/Workspace")  # 工作盘本身仍然可用（stat 不触发拦截）


def test_default_library_and_export_dirs_are_protected():
    for sub in ("Pictures/GXR Studio", "Pictures/GXR 导出", "Library/Application Support/GXR Studio"):
        from conftest import REAL_HOME

        with pytest.raises(PermissionError):
            os.makedirs(REAL_HOME / sub / "x", exist_ok=True)


def test_network_is_blocked():
    s = socket.socket()
    try:
        with pytest.raises(PermissionError):
            s.connect(("127.0.0.1", 9))
    finally:
        s.close()
    audit._network_log.clear()  # 这条是故意触发的
