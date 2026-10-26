from fastapi.testclient import TestClient

from workers.polymer_md_worker.app.main import create_app
from polymer_md.contracts import StageRun
from test_manager import request, supervisor


def test_private_http_idempotency_cancel_fence_and_cleanup(tmp_path):
    manager, calls = supervisor(tmp_path)
    manager.draining = True
    app = create_app(manager=manager)
    payload = request()
    with TestClient(app) as client:
        health = client.get("/health").json()
        assert health["protocol_version"] == 1 and health["gpu_count"] == 0
        assert health["accepting_jobs"] is False
        # An unknown cancellation can fence a lost submit while draining.
        path = f"/jobs/{payload.job_id}"
        response = client.post(path + "/cancel", json=payload.model_dump(mode="json"))
        assert response.status_code == 200
        assert response.json()["status"] == "cancelled"
        response = client.post("/jobs", json=payload.model_dump(mode="json"))
        assert response.status_code == 200 and response.json()["cleanup_confirmed"]
        changed = payload.model_dump(mode="json")
        changed["attempt_token"] = "b" * 64
        assert client.post("/jobs", json=changed).status_code == 409
        assert client.get(path + "/artifacts/unknown", params={"attempt_token": "b" * 64}).status_code == 409
        stage = StageRun(stage_run_id="eq1-1", stage="eq1", status="cancelled").model_dump(mode="json")
        manager.records[payload.job_id]["snapshot"]["stage_runs"].append(stage)
        log = manager._directory(payload) / "stages/eq1-1/runner.log"
        log.parent.mkdir(parents=True)
        log.write_text("old line\nprivate " + str(log) + "\nlast measured step 123\n")
        log_path = path + "/stages/eq1-1/logs"
        assert client.get(log_path, params={"attempt_token": "b" * 64}).status_code == 409
        response = client.get(log_path, params={"attempt_token": payload.attempt_token, "tail_lines": 2})
        assert response.status_code == 200
        assert response.json()["lines"] == ["private [private]", "last measured step 123"]
        assert response.json()["truncated"] is True
        assert client.get(log_path, params={"attempt_token": payload.attempt_token, "tail_lines": 1001}).status_code == 422
        assert not manager.readers
        assert client.request("DELETE", path, json=payload.model_dump(mode="json")).status_code == 200
        assert client.post("/jobs", json=payload.model_dump(mode="json")).status_code == 409
    assert not calls
