"""拟合进程退出与结果发布的交错，只用合成 LUT 和任务文件。"""

from __future__ import annotations

import json
import logging
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from conftest import PORT
from gxr_studio.app import create_app
from test_lut_library import cube_bytes, upload


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings), base_url=f"http://127.0.0.1:{PORT}", headers={"X-GXR-Token": settings.token}) as api_client:
        yield api_client


@pytest.fixture
def pending_fit(client):
    from full_recipe_support import plant_pending

    item = upload(client, cube_bytes(lambda x: x, size=2)).json()
    service = client.app.state.ctx.lut
    job_id = "a123456789ab"
    directory = service.jobs_dir / job_id
    plant_pending(service, item["cube_id"], job_id, unit="qlaunch")
    return service, job_id, directory


@pytest.mark.parametrize(
    "terminal",
    [
        {"state": "done", "result": {"candidates": []}},
        {"state": "cancelled"},
        {"state": "error", "code": "needs_photo", "message": "需要照片"},
    ],
    ids=["done", "cancelled", "error"],
)
def test_worker_publishes_its_result_between_the_status_read_and_exit_check(client, pending_fit, terminal):
    service, job_id, directory = pending_fit

    def exit_with_result():
        if terminal["state"] == "done":
            from full_recipe_support import publish_controlled_terminal

            publish_controlled_terminal(service, job_id)
        else:
            service._write_json(directory / "result.json", terminal)
        return 1 if terminal["state"] == "error" else 0

    service._procs[job_id] = SimpleNamespace(poll=exit_with_result)
    response = client.get(f"/api/lut/fit/{job_id}")
    assert response.status_code == 200, response.text
    status = response.json()
    assert status["state"] == terminal["state"], response.text
    assert status["unit"] == "qlaunch"
    if terminal["state"] == "done":
        assert len(status["result"]["candidates"]) == 1
        assert status["result"]["candidates"][0]["recipe"]["kind"] == "local"
        assert status["result"]["candidates"][0]["quality"]["state"] == "needs_evidence"
    if terminal["state"] == "error":
        assert status["error"] == {"code": "needs_photo", "message": "需要照片"}


@pytest.mark.parametrize("exit_code", [0, 1, -9])
def test_a_worker_without_a_result_reports_its_exit_and_alerts_once(client, pending_fit, exit_code, caplog, monkeypatch):
    service, job_id, _ = pending_fit
    service._procs[job_id] = SimpleNamespace(poll=lambda: exit_code)
    logger = logging.getLogger("gxr_studio")
    monkeypatch.setattr(logger, "handlers", [caplog.handler])
    monkeypatch.setattr(logger, "propagate", False)
    with caplog.at_level(logging.ERROR, logger="gxr_studio"):
        response = client.get(f"/api/lut/fit/{job_id}")
        again = client.get(f"/api/lut/fit/{job_id}")
    assert response.status_code == again.status_code == 200
    assert response.json()["state"] == "error"
    assert response.json()["error"] == {
        "code": "worker_died",
        "message": "配方计算进程意外退出，未能取得结果。请查看工作室日志。",
        "exit_code": exit_code,
    }
    assert again.json()["error"] == response.json()["error"]
    alerts = client.app.state.ctx.alerts.recent()
    assert len(alerts) == 1
    assert alerts[0]["handler"] == "on_job_failed" and alerts[0]["job_id"] == job_id
    events = [json.loads(record.message) for record in caplog.records]
    assert len(events) == 1
    assert events[0]["event"] == "alert"
    assert f"exit_code={exit_code}" in events[0]["error"]


def test_failure_status_does_not_wait_for_another_fit_to_start(client, pending_fit):
    service, job_id, _ = pending_fit
    polling, finished = threading.Event(), threading.Event()
    responses = []

    def exited():
        polling.set()
        return -9

    def read_status():
        try:
            responses.append(client.get(f"/api/lut/fit/{job_id}"))
        finally:
            finished.set()

    service._procs[job_id] = SimpleNamespace(poll=exited)
    thread = threading.Thread(target=read_status, daemon=True)
    with service._lock:
        thread.start()
        reached_poll = polling.wait(5)
        finished_while_starting = finished.wait(5)
    thread.join(5)
    assert reached_poll and not thread.is_alive()
    assert finished_while_starting, "读取失败结果不应等待另一个拟合的启动锁，否则保存配方与启动拟合会互相死锁"
    assert responses[0].status_code == 200
    assert responses[0].json()["error"]["exit_code"] == -9
