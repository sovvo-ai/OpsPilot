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


def test_health_ready_matches_dependency_state():
    """ready 的返回码必须与依赖聚合状态一致（不能自相矛盾）。

    ready 的 healthy = 数据库 ok ∧ 迁移 ok ∧ LLM 已配置 ∧ embedding 已配置。
    - 全部就绪 → 200
    - 任一不就绪 → 503

    原用例硬编码 503 只在"无 DB 且无 key"的纯 CI 环境成立；
    本机/容器配齐依赖时会伪失败。改为条件断言后两种环境都成立，
    同时仍能抓住"依赖挂了却报 200"这类真实回归。
    """
    r = client.get("/health/ready")
    data = r.json()
    assert "checks" in data and "healthy" in data

    if data["healthy"]:
        assert r.status_code == 200, "依赖全部就绪时 ready 应为 200"
        assert data["checks"]["database"]["status"] == "ok"
    else:
        assert r.status_code == 503, "存在未就绪依赖时 ready 必须为 503"
        # 至少有一项非 ok
        assert any(
            c.get("status") != "ok" for c in data["checks"].values()
        ), "healthy=false 时必存在非 ok 的检查项"


def test_health_deps_no_auth():
    """deps 免鉴权。"""
    r = client.get("/health/deps")
    assert r.status_code == 200
    assert "checks" in r.json()


def test_internal_requires_token():
    r = client.post("/internal/v1/ingest", json={})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "INTERNAL_AUTH_FAILED"


def test_internal_with_token_missing_doc_id():
    """ingest 已实现，空请求应报 400 缺少 doc_id/run_token。"""
    r = client.post("/internal/v1/ingest", json={}, headers=TOKEN)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_REQUEST"


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
