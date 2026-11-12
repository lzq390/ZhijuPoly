from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

from fastapi.testclient import TestClient
import httpx
import pytest

from server_monitoring.api import AUTH_URL, PREFIX, MonitoringConfig, create_app


NOW = datetime(2026, 11, 11, 10, 0, 0, tzinfo=timezone.utc)
SESSION_ID = "b4026ec5-52c9-4ed9-81ac-1e415df68a1e"
USERS = [
    {"username": "zhiju000", "user_id": "f7b37500-93aa-4d3d-86c5-a219e53dbcc0"},
    {"username": "zhiju001", "user_id": "f7b37501-93aa-4d3d-86c5-a219e53dbcc1"},
    {"username": "zhiju002", "user_id": "f7b37502-93aa-4d3d-86c5-a219e53dbcc2"},
]
HEADERS = {"Cookie": "nexpoly_dev_session=valid-token; unrelated=not-forwarded",
           "X-Session-Context": SESSION_ID}


def session(user=0):
    identity = USERS[user]
    return {"authenticated": True, "user": {"id": identity["user_id"], "username": identity["username"],
                                               "must_change_password": False}, "session_id": SESSION_ID}


def write_json(path: Path, payload):
    path.write_text(json.dumps(payload))
    path.chmod(0o600)


def envelope(age=0):
    captured = NOW - timedelta(seconds=age)
    return {"schema_version": 1, "node_id": "local-server",
            "collector_id": "92b0eed0-e735-4b58-863b-9f04b3a18015",
            "started_at": (NOW - timedelta(hours=1)).isoformat(), "captured_at": captured.isoformat()}


def snapshot(age=0):
    result = envelope(age)
    component = {"status": "ok", "last_success_at": result["captured_at"], "error_code": None}
    return {**result, "hostname": "test-host", "sequence": 1, "logical_cpu_count": 128,
            "uptime_seconds": 1000.0, "load_average": [1.0, 2.0, 3.0],
            "cpu": {"utilization_percent": 25.0},
            "memory": {"total_bytes": 100, "available_bytes": 60, "used_bytes": 40, "used_percent": 40.0},
            "disks": [], "gpus": [], "components": {key: deepcopy(component) for key in ("cpu", "memory", "disks", "gpus")}}


def history(age=0):
    result = envelope(age)
    return {**result, "interval_seconds": 30,
            "points": [{"timestamp": result["captured_at"], "cpu_percent": None,
                        "memory_percent": 40.0, "gpus": []}]}


@pytest.fixture
def build(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    config_path = tmp_path / "config.json"
    config = {"enabled": True, "state_dir": str(state), "allowed_users": USERS}
    write_json(config_path, config)

    def factory(handler=None, *, config_override=None):
        if config_override is not None:
            write_json(config_path, {**config, **config_override})
        transport = httpx.MockTransport(handler or (lambda _request: httpx.Response(200, json=session())))
        return TestClient(create_app(config_path, auth_transport=transport, clock=lambda: NOW))
    factory.state = state
    factory.config_path = config_path
    return factory


@pytest.mark.parametrize("route", ["access", "snapshot", "history", "gpu-tasks"])
def test_missing_cookie_rejected_without_contacting_auth(build, route):
    def forbidden(_request):
        pytest.fail("missing credentials must not contact authentication")
    response = build(forbidden).get(f"{PREFIX}/{route}")
    assert response.status_code == 401
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["vary"] == "Cookie, X-Session-Context"


@pytest.mark.parametrize("user", [0, 1, 2])
def test_allowed_identity_access_needs_no_monitor_state(build, user):
    calls = []
    def upstream(request):
        calls.append(request)
        assert str(request.url) == AUTH_URL and request.method == "GET"
        assert request.headers["cookie"] == "nexpoly_dev_session=valid-token"
        assert "authorization" not in request.headers
        assert "x-session-context" not in request.headers
        assert "x-forwarded-host" not in request.headers
        return httpx.Response(200, json=session(user))
    response = build(upstream).get(f"{PREFIX}/access", headers={**HEADERS, "Authorization": "Bearer ignored",
                                                              "X-Forwarded-Host": "ignored.example"})
    assert response.status_code == 200
    assert response.json() == {"can_read": True}
    assert len(calls) == 1


def test_each_request_revalidates_revocation(build):
    calls = []
    def upstream(_request):
        calls.append(1)
        return httpx.Response(200, json=session() if len(calls) == 1 else {"authenticated": False, "user": None, "session_id": None})
    client = build(upstream)
    assert client.get(f"{PREFIX}/access", headers=HEADERS).status_code == 200
    assert client.get(f"{PREFIX}/access", headers=HEADERS).status_code == 401
    assert len(calls) == 2


@pytest.mark.parametrize("change", ["username", "id", "password"])
def test_identity_pair_and_password_gate(build, change):
    payload = session()
    if change == "username":
        payload["user"]["username"] = "zhiju001"  # A permitted name with another user's ID.
    elif change == "id":
        payload["user"]["id"] = "eac36f2d-5e08-4a59-a465-2c703a0a2ad8"
    else:
        payload["user"]["must_change_password"] = True
    client = build(lambda _: httpx.Response(200, json=payload))
    assert client.get(f"{PREFIX}/snapshot", headers=HEADERS).status_code == 403


@pytest.mark.parametrize("context", [None, "another-session"])
def test_missing_or_changed_session_context(build, context):
    headers = {"Cookie": HEADERS["Cookie"]}
    if context is not None:
        headers["X-Session-Context"] = context
    response = build().get(f"{PREFIX}/access", headers=headers)
    assert response.status_code == 409
    assert response.json() == {"code": "session_context_mismatch"}


def test_password_change_uses_browser_identity_retirement_code(build):
    payload = session()
    payload["user"]["must_change_password"] = True
    response = build(lambda _: httpx.Response(200, json=payload)).get(f"{PREFIX}/access", headers=HEADERS)
    assert response.status_code == 403
    assert response.json() == {"code": "password_change_required"}


def test_upstream_cookies_do_not_cross_callers_or_reach_browser(build):
    calls = []
    def upstream(request):
        cookie = request.headers["cookie"]
        calls.append(cookie)
        user = 0 if cookie == "nexpoly_dev_session=first" else 1
        return httpx.Response(200, json=session(user), headers={"Set-Cookie": "nexpoly_dev_session=poison; Path=/"})
    client = build(upstream)
    for cookie in ("first", "second"):
        response = client.get(f"{PREFIX}/access", headers={**HEADERS, "Cookie": f"nexpoly_dev_session={cookie}"})
        assert response.status_code == 200
        assert "set-cookie" not in response.headers
    assert calls == ["nexpoly_dev_session=first", "nexpoly_dev_session=second"]


def test_duplicate_session_context_and_cookie_rejected(build):
    client = build()
    duplicate_context = [("Cookie", HEADERS["Cookie"]), ("X-Session-Context", SESSION_ID), ("X-Session-Context", SESSION_ID)]
    assert client.get(f"{PREFIX}/access", headers=duplicate_context).status_code == 409
    assert client.get(f"{PREFIX}/access", headers={**HEADERS, "Cookie": "nexpoly_dev_session=a; nexpoly_dev_session=b"}).status_code == 401


@pytest.mark.parametrize("kind", ["timeout", "redirect", "server_error", "malformed", "invalid_types", "oversize", "duplicate_key", "fake_session"])
def test_auth_source_failure_never_falls_back(build, kind):
    def upstream(request):
        if kind == "timeout":
            raise httpx.ReadTimeout("private error text", request=request)
        if kind == "redirect":
            return httpx.Response(302, headers={"Location": "http://127.0.0.1:5318/api/v1/auth/session"})
        if kind == "server_error":
            return httpx.Response(503)
        if kind == "malformed":
            return httpx.Response(200, content=b"not-json")
        if kind == "oversize":
            return httpx.Response(200, content=b" " * (32 * 1024 + 1))
        if kind == "duplicate_key":
            return httpx.Response(200, content=b'{"authenticated":false,"authenticated":true}')
        payload = session()
        if kind == "invalid_types":
            payload["authenticated"] = "true"
        else:
            payload["session_id"] = "local-preview"
        return httpx.Response(200, json=payload)
    response = build(upstream).get(f"{PREFIX}/access", headers=HEADERS)
    assert response.status_code == 503
    assert response.json() == {"code": "authentication_unavailable"}
    assert "private error" not in response.text


@pytest.mark.parametrize("route,filename,maker", [("snapshot", "latest.json", snapshot), ("history", "history.json", history)])
@pytest.mark.parametrize("age,expected", [(0, "fresh"), (15, "fresh"), (15.01, "stale"), (60, "stale"), (60.01, "unavailable")])
def test_freshness_and_real_schema(build, route, filename, maker, age, expected):
    payload = maker(age)
    write_json(build.state / filename, payload)
    if route == "history":
        write_json(build.state / "latest.json", snapshot(age))
    response = build().get(f"{PREFIX}/{route}", headers=HEADERS)
    assert response.status_code == 200
    expected_payload = {**payload, "age_seconds": age, "freshness": expected}
    if route == "snapshot":
        expected_payload["host_info"] = payload.get("host_info")
    assert response.json() == expected_payload
    assert response.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize("missing_fields", [False, True])
def test_host_information_passes_snapshot_and_history_validation(build, missing_fields):
    metadata = {"cpu_model": "Example CPU", "cpu_socket_count": 2, "cpu_physical_core_count": 64,
                "os_name": "Example Linux 1.0", "kernel_release": "6.8.0-example", "nvidia_driver_version": "535.288.01"}
    if missing_fields:
        metadata = dict.fromkeys(metadata)
    payload = {**snapshot(), "host_info": metadata}
    write_json(build.state / "latest.json", payload)
    write_json(build.state / "history.json", history())
    client = build()
    response = client.get(f"{PREFIX}/snapshot", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["host_info"] == metadata
    assert response.json()["freshness"] == "fresh"
    assert client.get(f"{PREFIX}/history", headers=HEADERS).status_code == 200


@pytest.mark.parametrize("change", [
    {"process_command": "must-not-reach-browser"},
    {"cpu_model": "bad\u0000metadata"},
    {"cpu_socket_count": "2"},
])
def test_invalid_host_information_is_rejected_without_exposing_contents(build, change):
    metadata = dict.fromkeys(("cpu_model", "cpu_socket_count", "cpu_physical_core_count",
                              "os_name", "kernel_release", "nvidia_driver_version"))
    write_json(build.state / "latest.json", {**snapshot(), "host_info": {**metadata, **change}})
    response = build().get(f"{PREFIX}/snapshot", headers=HEADERS)
    assert response.status_code == 503
    assert response.json() == {"code": "monitoring_unavailable"}


@pytest.mark.parametrize("change", ["collector_id", "started_at", "missing_latest"])
def test_history_cannot_reuse_previous_collector_instance(build, change):
    write_json(build.state / "history.json", history())
    latest = snapshot()
    if change == "collector_id":
        latest["collector_id"] = "92b0eed0-e735-4b58-863b-9f04b3a18016"
    elif change == "started_at":
        latest["started_at"] = (NOW - timedelta(minutes=10)).isoformat()
    if change != "missing_latest":
        write_json(build.state / "latest.json", latest)
    response = build().get(f"{PREFIX}/history", headers=HEADERS)
    assert response.status_code == 503
    assert response.json() == {"code": "monitoring_unavailable"}


@pytest.mark.parametrize("offset", [30, -3600.001])
def test_history_rejects_points_outside_capture_window(build, offset):
    payload = history()
    payload["points"][0]["timestamp"] = (NOW + timedelta(seconds=offset)).isoformat()
    write_json(build.state / "history.json", payload)
    write_json(build.state / "latest.json", snapshot())
    response = build().get(f"{PREFIX}/history", headers=HEADERS)
    assert response.status_code == 503


def test_state_changed_during_read_is_rejected(build, monkeypatch):
    write_json(build.state / "latest.json", snapshot())
    original = os.fstat
    count = 0
    def changed(descriptor):
        nonlocal count
        result = original(descriptor)
        count += 1
        if count == 2:
            from types import SimpleNamespace
            return SimpleNamespace(st_dev=result.st_dev, st_ino=result.st_ino, st_size=result.st_size + 1,
                                   st_mtime_ns=result.st_mtime_ns, st_ctime_ns=result.st_ctime_ns)
        return result
    client = build()  # Complete config loading before observing the state read.
    monkeypatch.setattr("server_monitoring.api.os.fstat", changed)
    response = client.get(f"{PREFIX}/snapshot", headers=HEADERS)
    assert response.status_code == 503


@pytest.mark.parametrize("replace_before_stat", [1, 2])
def test_atomic_replacement_during_read_returns_complete_previous_snapshot(build, monkeypatch, replace_before_stat):
    path = build.state / "latest.json"
    previous = snapshot()
    replacement = {**previous, "sequence": 2}
    write_json(path, previous)
    staged = build.state / "replacement.json"
    write_json(staged, replacement)
    client = build()
    original = os.fstat
    observations = []

    def replace_while_descriptor_is_open(descriptor):
        if len(observations) + 1 == replace_before_stat:
            os.replace(staged, path)
        result = original(descriptor)
        observations.append(result)
        return result

    monkeypatch.setattr("server_monitoring.api.os.fstat", replace_while_descriptor_is_open)
    response = client.get(f"{PREFIX}/snapshot", headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == {**previous, "host_info": None, "age_seconds": 0, "freshness": "fresh"}
    assert len(observations) == 2
    assert observations[0].st_ino == observations[1].st_ino
    assert observations[0].st_nlink == (0 if replace_before_stat == 1 else 1)
    assert observations[1].st_nlink == 0
    assert json.loads(path.read_text()) == replacement


@pytest.mark.parametrize("same_size", [False, True])
def test_actual_in_place_rewrite_during_read_is_rejected(build, monkeypatch, same_size):
    path = build.state / "latest.json"
    write_json(path, snapshot())
    client = build()
    original = os.fstat
    observations = []

    def rewrite_before_second_stat(descriptor):
        if observations:
            replacement = json.dumps({**snapshot(), "sequence": 2})
            path.write_text(replacement if same_size else replacement + " ")
            # Force a distinct modification time even on a coarse test filesystem.
            first = observations[0]
            os.utime(path, ns=(first.st_atime_ns, first.st_mtime_ns + 1_000_000_000))
        result = original(descriptor)
        observations.append(result)
        return result

    monkeypatch.setattr("server_monitoring.api.os.fstat", rewrite_before_second_stat)
    response = client.get(f"{PREFIX}/snapshot", headers=HEADERS)
    assert response.status_code == 503
    assert response.json() == {"code": "monitoring_unavailable"}
    assert len(observations) == 2
    assert observations[0].st_ino == observations[1].st_ino
    assert (observations[0].st_size == observations[1].st_size) == same_size


@pytest.mark.parametrize("failure", ["missing", "malformed", "schema", "future", "symlink", "hardlink", "fifo", "permissions", "oversize"])
def test_invalid_state_is_unavailable_without_leaking_path(build, failure, tmp_path):
    path = build.state / "latest.json"
    if failure == "malformed":
        path.write_text("bad")
        path.chmod(0o600)
    elif failure == "schema":
        write_json(path, {**snapshot(), "cpu": {"utilization_percent": 101.0}})
    elif failure == "future":
        write_json(path, snapshot(-0.001))
    elif failure == "symlink":
        target = tmp_path / "other.json"
        write_json(target, snapshot())
        path.symlink_to(target)
    elif failure == "hardlink":
        target = tmp_path / "other.json"
        write_json(target, snapshot())
        os.link(target, path)
    elif failure == "fifo":
        os.mkfifo(path, 0o600)
    elif failure == "permissions":
        write_json(path, snapshot())
        path.chmod(0o644)
    elif failure == "oversize":
        path.write_bytes(b" " * (256 * 1024 + 1))
        path.chmod(0o600)
    response = build().get(f"{PREFIX}/snapshot", headers=HEADERS)
    assert response.status_code == 503
    assert response.json() == {"code": "monitoring_unavailable"}
    assert str(tmp_path) not in response.text


def test_disabled_default_and_no_public_docs(tmp_path, build):
    client = TestClient(create_app(tmp_path / "absent.json"))
    assert client.get(f"{PREFIX}/access", headers=HEADERS).status_code == 503
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404
    assert build(config_override={"enabled": False}).get(f"{PREFIX}/access", headers=HEADERS).status_code == 503


@pytest.mark.parametrize("change", ["extra_user", "duplicate_id", "missing_user", "relative_path", "auth_url"])
def test_configuration_cannot_expand_trust_boundary(tmp_path, change):
    payload = {"enabled": True, "state_dir": str(tmp_path), "allowed_users": deepcopy(USERS)}
    if change == "extra_user":
        payload["allowed_users"][0]["username"] = "admin"
    elif change == "duplicate_id":
        payload["allowed_users"][1]["user_id"] = payload["allowed_users"][0]["user_id"]
    elif change == "missing_user":
        payload["allowed_users"].pop()
    elif change == "relative_path":
        payload["state_dir"] = "../state"
    else:
        payload["auth_url"] = "http://127.0.0.1:5318/api/v1/auth/session"
    with pytest.raises(ValueError):
        MonitoringConfig.model_validate(payload)


def test_monitoring_routes_are_read_only(build):
    client = build()
    for method in ("post", "put", "patch", "delete"):
        response = getattr(client, method)(f"{PREFIX}/snapshot", headers=HEADERS)
        assert response.status_code == 405
        assert response.headers["cache-control"] == "private, no-store"


TASK_GPU = 'GPU-00000000-0000-0000-0000-000000000001'


def gpu_task_history(age=0):
    captured = envelope(age)['captured_at']
    return {**envelope(age), 'coverage': 'retained_verified_gpu_records', 'status': 'ready',
            'history_captured_at': captured,
            'gpu_uuids': [TASK_GPU],
            'sources': [{'source_id': name, 'status': 'ready', 'matched_tasks': int(name == 'development_dft'),
                         'excluded_tasks': 0, 'error_code': None}
                        for name in ('development_dft', 'production_dft', 'development_md', 'production_md',
                                     'development_polymer_md', 'production_polymer_md')],
            'activity': {'captured_at': captured, 'status': 'ready', 'error_code': None,
                         'items': [{'id': 'b' * 20, 'gpu_uuid': TASK_GPU, 'kind': 'service',
                                    'module': 'platform', 'environment': 'development', 'label': '平台后端',
                                    'status': 'resident', 'memory_used_bytes': 10, 'started_at': None}]},
            'tasks': [{'id': 'a' * 20, 'gpu_uuid': TASK_GPU, 'module': 'monomer_dft',
                       'environment': 'development', 'calculation_type': 'optimization', 'status': 'completed',
                       'started_at': (NOW - timedelta(minutes=30)).isoformat(),
                       'finished_at': (NOW - timedelta(minutes=20)).isoformat(), 'duration_seconds': 600.0}]}


def write_gpu_task_state(build, age=0):
    latest = snapshot()
    latest['gpus'] = [{'index': 0, 'uuid': TASK_GPU, 'name': 'Test GPU', 'utilization_percent': 0.0,
                       'memory_total_bytes': 100, 'memory_used_bytes': 0, 'temperature_celsius': 30.0, 'power_watts': 10.0}]
    payload = gpu_task_history(age)
    write_json(build.state / 'latest.json', latest)
    write_json(build.state / 'gpu-tasks.json', payload)
    return payload


@pytest.mark.parametrize('age,freshness', [(0, 'fresh'), (15, 'fresh'), (15.1, 'stale'), (60, 'stale'), (60.1, 'unavailable')])
def test_gpu_activity_cache_tracks_live_five_second_collection(build, age, freshness):
    payload = write_gpu_task_state(build, age)
    response = build().get(f'{PREFIX}/gpu-tasks', headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == {**payload, 'age_seconds': age, 'freshness': freshness}
    assert response.headers['cache-control'] == 'private, no-store'
    assert set(response.json()['tasks'][0]) == {'id', 'gpu_uuid', 'module', 'environment', 'calculation_type',
                                               'status', 'started_at', 'finished_at', 'duration_seconds'}
    assert set(response.json()['activity']['items'][0]) == {'id', 'gpu_uuid', 'kind', 'module', 'environment', 'label',
                                                         'status', 'memory_used_bytes', 'started_at'}


@pytest.mark.parametrize('kind', ['wrong_host_gpu', 'private_field', 'activity_private_field', 'activity_unknown_gpu',
                                'missing_state', 'future_file', 'future_activity', 'future_history'])
def test_gpu_task_read_rejects_untrusted_or_unavailable_cache(build, kind):
    payload = write_gpu_task_state(build)
    if kind == 'wrong_host_gpu':
        write_json(build.state / 'latest.json', snapshot())
    elif kind == 'private_field':
        payload['tasks'][0]['owner_username'] = 'must-not-be-exposed'
        write_json(build.state / 'gpu-tasks.json', payload)
    elif kind == 'activity_private_field':
        payload['activity']['items'][0]['command_line'] = 'must-not-be-exposed'
        write_json(build.state / 'gpu-tasks.json', payload)
    elif kind == 'activity_unknown_gpu':
        payload['activity']['items'][0]['gpu_uuid'] = 'GPU-00000000-0000-0000-0000-000000000999'
        write_json(build.state / 'gpu-tasks.json', payload)
    elif kind == 'future_activity':
        payload['activity']['captured_at'] = (NOW + timedelta(seconds=1)).isoformat()
        write_json(build.state / 'gpu-tasks.json', payload)
    elif kind == 'future_history':
        payload['history_captured_at'] = (NOW + timedelta(seconds=1)).isoformat()
        write_json(build.state / 'gpu-tasks.json', payload)
    elif kind == 'missing_state':
        (build.state / 'gpu-tasks.json').unlink()
    else:
        payload['captured_at'] = (NOW + timedelta(seconds=1)).isoformat()
        write_json(build.state / 'gpu-tasks.json', payload)
    response = build().get(f'{PREFIX}/gpu-tasks', headers=HEADERS)
    assert response.status_code == 503
    assert response.json() == {'code': 'monitoring_unavailable'}


def test_gpu_task_access_rechecks_the_real_identity_on_every_request(build):
    write_gpu_task_state(build)
    calls = []
    def upstream(_request):
        calls.append(1)
        return httpx.Response(200, json=session() if len(calls) == 1 else {'authenticated': False})
    client = build(upstream)
    assert client.get(f'{PREFIX}/gpu-tasks', headers=HEADERS).status_code == 200
    assert client.get(f'{PREFIX}/gpu-tasks', headers=HEADERS).status_code == 401
    assert len(calls) == 2


@pytest.mark.parametrize('kind,expected', [('wrong_context', 409), ('wrong_user', 403), ('auth_down', 503)])
def test_gpu_task_cache_does_not_bypass_authorization(build, kind, expected):
    write_gpu_task_state(build)
    data = session()
    if kind == 'wrong_user':
        data['user']['username'] = 'another-account'
    def upstream(_request):
        return httpx.Response(503) if kind == 'auth_down' else httpx.Response(200, json=data)
    headers = {**HEADERS, 'X-Session-Context': 'wrong'} if kind == 'wrong_context' else HEADERS
    assert build(upstream).get(f'{PREFIX}/gpu-tasks', headers=headers).status_code == expected
