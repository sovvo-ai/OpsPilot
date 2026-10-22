"""S1 SPIKE：只验证 health 与内部鉴权三态，不涉及业务。"""

import os

os.environ.setdefault("AI_SERVICE_TOKEN", "dummy-internal-auth-value")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)
TOKEN = {"X-Internal-Token": "dummy-internal-auth-value"}


def test_health_live_200():
    r = client.get("/health/live")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_echo_requires_token():
    r = client.post("/internal/v1/echo", json={"text": "中文"})
    assert r.status_code == 401


def test_echo_with_token_roundtrips_chinese():
    r = client.post("/internal/v1/echo", json={"text": "网络不通如何排查"}, headers=TOKEN)
    assert r.status_code == 200
    assert r.json()["echo_text"] == "网络不通如何排查"
    assert r.json()["text_len"] == 8


def test_bad_token_401():
    r = client.post("/internal/v1/echo", json={"text": "x"}, headers={"X-Internal-Token": "wrong"})
    assert r.status_code == 401


def test_health_is_exempt_from_token():
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/deps").status_code == 200
