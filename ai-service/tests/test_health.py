"""AI Service 健康检查与中间件测试（S3 骨架）。"""

import os

os.environ.setdefault("AI_SERVICE_TOKEN", "test-token")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)
TOKEN = {"X-Internal-Token": "test-token"}


def test_health_live_200():
    r = client.get("/health/live")
    assert r.status_code == 200
    assert r.json()["service"] == "ops-pilot-ai"


def test_health_ready_not_ok_503():
    """数据库不可用时 ready 返回 503。若未配置 DATABASE_URL 则状态为 unknown，
    若已配置但连不上则状态为 error。"""
    r = client.get("/health/ready")
    assert r.status_code == 503
    data = r.json()
    db_status = data["checks"]["database"]["status"]
    assert db_status in ("unknown", "error")


def test_health_deps_no_auth():
    """deps 免鉴权。"""
    r = client.get("/health/deps")
    assert r.status_code == 200
    assert "checks" in r.json()


def test_internal_requires_token():
    r = client.post("/internal/v1/ingest", json={})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "INTERNAL_AUTH_FAILED"


def test_internal_with_token_not_implemented():
    r = client.post("/internal/v1/ingest", json={}, headers=TOKEN)
    assert r.status_code == 501
    assert r.json()["error"]["code"] == "NOT_IMPLEMENTED"


def test_bad_token_401():
    r = client.post(
        "/internal/v1/ingest",
        json={},
        headers={"X-Internal-Token": "wrong"},
    )
    assert r.status_code == 401


def test_health_is_exempt_from_token():
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/deps").status_code == 200


def test_request_id_passthrough():
    r = client.get("/health/live", headers={"X-Request-Id": "req-123"})
    assert r.headers["X-Request-Id"] == "req-123"
