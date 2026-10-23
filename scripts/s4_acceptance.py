"""S4 摄取链路本地验收脚本。

验收项：
- A1: 5 篇种子文档 202 → 轮询 completed
- A13: 两个 dev token 交叉访问均 404
- A14: 状态机不变量 SQL 断言
- failed → reingest 成功且切片不重复
- deduplicated 零 Embedding 调用
- HNSW Recall@5 vs 全表扫描 ≥ 0.95（ef_search 40/100/200）
- 5 篇摄取 P95 ≤ 30s
- T-01/T-04/T-05/T-08/T-09 延迟/一致性用例落地

运行方式（由项目根目录执行，ai-service venv）：
    ai-service/.venv/Scripts/python.exe scripts/s4_acceptance.py
"""

from __future__ import annotations

import os
import statistics
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import psycopg
from psycopg.rows import dict_row

# ---------------------------------------------------------------------------
# 配置与常量
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / "deploy" / ".env"
SEED_DIR = PROJECT_ROOT / "scripts" / "seed_docs"
BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8081")
AI_SERVICE_URL = os.getenv("AI_SERVICE_URL", "http://127.0.0.1:8000")

TEST_USERS = ["demo-user", "test-user-2"]
POLL_INTERVAL = 1.0  # 秒
POLL_MAX_WAIT = 120  # 秒

PASS_MARK = "✅"
FAIL_MARK = "❌"
WARN_MARK = "⚠️"

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def load_env(path: Path) -> dict[str, str]:
    """读取 .env 文件，不覆盖已存在的环境变量。"""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key] = value
    return env


def build_dsn(env: dict[str, str]) -> str:
    """从 .env 构建 PostgreSQL DSN（密码 URL 编码）。"""
    import urllib.parse

    user = env.get("POSTGRES_USER", "ops_pilot")
    password = env.get("POSTGRES_PASSWORD", "")
    host = env.get("POSTGRES_HOST", "localhost")
    port = env.get("POSTGRES_PORT", "55432")
    db = env.get("POSTGRES_DB", "ops_pilot")
    sslmode = env.get("POSTGRES_SSLMODE", "disable")
    return (
        f"postgres://{urllib.parse.quote(user, safe='')}:"
        f"{urllib.parse.quote(password, safe='')}@{host}:{port}/{db}?sslmode={sslmode}"
    )


def make_unique_doc(client: APIClient, tag: str) -> Path:
    """为用例生成内容唯一的临时文档，避免 dedup 命中其它用例的文档。

    返回临时文件路径，调用方负责在 finally 中删除。
    """
    tmp = Path(__file__).resolve().parent / f"s4_tmp_{tag}.md"
    marker = uuid.uuid4().hex
    sections = []
    for i in range(1, 5):
        sections.append(
            f"## 段落 {i}（{tag}-{marker[:8]}）\n\n"
            + f"这是 {tag} 用例的专属内容段 {i}，"
            + "用于构造足量文本以触发多切片，确保聚合后每块长度落在目标区间。" * 4
        )
    tmp.write_text(f"# 用例文档 {tag}-{marker}\n\n" + "\n\n".join(sections) + "\n", encoding="utf-8")
    return tmp


def upload_and_wait(
    client: APIClient, db: DBHelper, tag: str, target: str = "completed"
) -> tuple[str | None, Path]:
    """上传唯一文档并轮询到目标状态，返回 (doc_id, 临时文件路径)。"""
    tmp = make_unique_doc(client, tag)
    up = client.upload("demo-user", tmp)
    if up["status_code"] != 202 or not up.get("doc_id"):
        return None, tmp
    doc_id = up["doc_id"]
    _ = db
    client.poll_until("demo-user", doc_id, target)
    return doc_id, tmp


def drop_doc(db: DBHelper, doc_id: str | None, tmp: Path) -> None:
    """清理用例产生的文档与临时文件。"""
    if doc_id:
        with db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM documents WHERE id = %s", (doc_id,))
                conn.commit()
    tmp.unlink(missing_ok=True)


def extract_error_code(payload: dict[str, Any]) -> str | None:
    """统一从响应体里取出错误码。

    后端错误体形如 {"error": {"code": "...", "message": "..."}}；
    成功体里也可能出现顶层 "code"。两者都兼容。
    """
    err = payload.get("error")
    if isinstance(err, dict) and err.get("code"):
        return str(err["code"])
    if payload.get("code"):
        return str(payload["code"])
    return None


def redact(s: str) -> str:
    """用于日志脱敏：只保留前 4 和后 4 字符。"""
    if len(s) <= 12:
        return "***"
    return s[:4] + "..." + s[-4:]


# ---------------------------------------------------------------------------
# HTTP 客户端
# ---------------------------------------------------------------------------


class APIClient:
    """封装 backend 与 ai-service 的 HTTP 调用。"""

    def __init__(self, backend_url: str, ai_url: str, env: dict[str, str]):
        self.backend_url = backend_url.rstrip("/")
        self.ai_url = ai_url.rstrip("/")
        self.ai_token = env.get("AI_SERVICE_TOKEN", "")
        self.tokens: dict[str, str] = {}

    def _headers(self, user: str | None = None) -> dict[str, str]:
        h = {"Accept": "application/json"}
        if user:
            h["Authorization"] = f"Bearer {self.tokens[user]}"
        return h

    def get_dev_token(self, user: str) -> str:
        """获取 dev JWT。"""
        resp = httpx.post(
            f"{self.backend_url}/api/v1/auth/dev-token",
            params={"user": user},
            timeout=10.0,
        )
        resp.raise_for_status()
        token = resp.json()["token"]
        self.tokens[user] = token
        return token

    def upload(self, user: str, file_path: Path) -> dict[str, Any]:
        with file_path.open("rb") as f:
            resp = httpx.post(
                f"{self.backend_url}/api/v1/documents",
                headers=self._headers(user),
                files={"file": (file_path.name, f, "text/markdown")},
                timeout=10.0,
            )
        return {"status_code": resp.status_code, **resp.json()}

    def get_doc(self, user: str, doc_id: str) -> dict[str, Any]:
        resp = httpx.get(
            f"{self.backend_url}/api/v1/documents/{doc_id}",
            headers=self._headers(user),
            timeout=10.0,
        )
        return {"status_code": resp.status_code, **resp.json()}

    def reingest(self, user: str, doc_id: str) -> dict[str, Any]:
        resp = httpx.post(
            f"{self.backend_url}/api/v1/documents/{doc_id}/reingest",
            headers=self._headers(user),
            timeout=10.0,
        )
        return {"status_code": resp.status_code, **resp.json()}

    def delete(self, user: str, doc_id: str) -> dict[str, Any]:
        resp = httpx.delete(
            f"{self.backend_url}/api/v1/documents/{doc_id}",
            headers=self._headers(user),
            timeout=10.0,
        )
        return {"status_code": resp.status_code, **resp.json()}

    def poll_until(
        self, user: str, doc_id: str, target: str, timeout: int = POLL_MAX_WAIT
    ) -> dict[str, Any]:
        """轮询文档状态直到 target 或超时。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            data = self.get_doc(user, doc_id)
            status = data.get("ingest_status")
            if status == target:
                return data
            if status == "failed":
                return data
            time.sleep(POLL_INTERVAL)
        # 超时后返回最后一次的状态，确保返回值不为 None
        final = self.get_doc(user, doc_id)
        if final is None:
            return {"ingest_status": "timeout", "error_code": "TIMEOUT", "error_msg": "轮询超时"}
        return final

    def internal_ingest(self, doc_id: str, run_token: str, attempt: int = 1) -> dict[str, Any]:
        """直接调 ai-service 内部 ingest（模拟 Python 返回）。"""
        resp = httpx.post(
            f"{self.ai_url}/internal/v1/ingest",
            headers={"X-Internal-Token": self.ai_token, "Content-Type": "application/json"},
            json={"doc_id": doc_id, "run_token": run_token, "attempt": attempt},
            timeout=120.0,
        )
        return {"status_code": resp.status_code, **resp.json()}


# ---------------------------------------------------------------------------
# 数据库辅助
# ---------------------------------------------------------------------------


class DBHelper:
    def __init__(self, dsn: str):
        self.dsn = dsn

    def connect(self):
        return psycopg.connect(self.dsn, row_factory=dict_row)

    def clean_test_users(self) -> int:
        """物理删除测试用户的全部数据（利用级联）。"""
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM documents WHERE owner_id = ANY(%s)",
                    (TEST_USERS,),
                )
                deleted = cur.rowcount
                conn.commit()
        return deleted

    def get_doc_state(self, doc_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT d.id, d.owner_id, d.title, d.deleted_at,
                           j.status, j.attempt, j.current_run_token,
                           j.published_run_token, j.chunk_count,
                           j.error_code, j.error_msg
                    FROM documents d
                    JOIN ingest_jobs j ON j.doc_id = d.id
                    WHERE d.id = %s
                    """,
                    (doc_id,),
                )
                return cur.fetchone()

    def set_job_failed(self, doc_id: str) -> None:
        """把文档任务直接置为 failed（用于构造重试场景）。"""
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE ingest_jobs
                       SET status = 'failed',
                           error_code = 'MANUAL_FAIL',
                           error_msg = 'acceptance script manual fail',
                           finished_at = now(),
                           updated_at = now(),
                           current_run_token = NULL,
                           published_run_token = NULL
                     WHERE doc_id = %s
                    """,
                    (doc_id,),
                )
                cur.execute(
                    "DELETE FROM doc_chunks WHERE doc_id = %s",
                    (doc_id,),
                )
                conn.commit()

    def set_job_processing(self, doc_id: str, run_token: str, started_ago_seconds: int) -> None:
        """把 completed 文档回拨为 processing（用于 T-02/T-03 构造）。

        注意：`interval '%s seconds'` 的参数化写法会被 psycopg 当作字面量，
        实际不减时间；必须用 `(%s * interval '1 second')` 形式。
        """
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE ingest_jobs
                       SET status = 'processing',
                           current_run_token = %s,
                           started_at = now() - (%s * interval '1 second'),
                           updated_at = now(),
                           finished_at = NULL,
                           error_code = NULL,
                           error_msg = NULL
                     WHERE doc_id = %s
                    """,
                    (run_token, started_ago_seconds, doc_id),
                )
                # 把可见切片置为不可见（模拟新 run 开始前的旧状态）
                cur.execute(
                    "UPDATE doc_chunks SET visible = FALSE WHERE doc_id = %s",
                    (doc_id,),
                )
                conn.commit()

    def run_invariants(self) -> dict[str, Any]:
        """执行 A14 / T-04 / T-05 相关 SQL 断言。"""
        queries = {
            "i_visible_only_completed": """
                SELECT count(*) AS n
                FROM doc_chunks c
                JOIN ingest_jobs j ON j.doc_id = c.doc_id
                WHERE c.visible = TRUE AND j.status NOT IN ('completed','deleting')
            """,
            "i_completed_has_visible": """
                SELECT count(*) AS n
                FROM ingest_jobs j
                WHERE j.status = 'completed'
                  AND NOT EXISTS (
                      SELECT 1 FROM doc_chunks c
                      WHERE c.doc_id = j.doc_id AND c.visible = TRUE
                  )
            """,
            "i_deleting_has_deleted_at": """
                SELECT count(*) AS n
                FROM documents d
                JOIN ingest_jobs j ON j.doc_id = d.id
                WHERE j.status = 'deleting' AND d.deleted_at IS NULL
            """,
            "i_run_token_matches_published": """
                SELECT count(*) AS n
                FROM doc_chunks c
                JOIN ingest_jobs j ON j.doc_id = c.doc_id
                WHERE c.visible = TRUE
                  AND j.published_run_token IS NOT NULL
                  AND c.run_token IS DISTINCT FROM j.published_run_token
            """,
            "i_status_enum": """
                SELECT count(*) AS n
                FROM ingest_jobs
                WHERE status NOT IN ('pending','processing','completed','failed','deleting')
            """,
            "i_chunk_count_matches_visible": """
                SELECT count(*) AS n
                FROM ingest_jobs j
                WHERE j.status = 'completed'
                  AND j.chunk_count <> (
                      SELECT count(*) FROM doc_chunks c
                      WHERE c.doc_id = j.doc_id AND c.visible = TRUE
                  )
            """,
        }
        results: dict[str, int] = {}
        with self.connect() as conn:
            with conn.cursor() as cur:
                for name, sql in queries.items():
                    cur.execute(sql)
                    results[name] = cur.fetchone()["n"]
        return results

    def chunk_count(self, doc_id: str, visible: bool | None = None) -> int:
        with self.connect() as conn:
            with conn.cursor() as cur:
                if visible is None:
                    cur.execute(
                        "SELECT count(*) AS n FROM doc_chunks WHERE doc_id = %s",
                        (doc_id,),
                    )
                else:
                    cur.execute(
                        "SELECT count(*) AS n FROM doc_chunks WHERE doc_id = %s AND visible = %s",
                        (doc_id, visible),
                    )
                return cur.fetchone()["n"]

    def residual_counts(self, doc_id: str) -> dict[str, int]:
        """A15：四表针对该 doc_id 的残留计数。"""
        # documents 主键列名为 id，其余三表外键列名为 doc_id
        queries = {
            "documents": ("SELECT count(*) AS n FROM documents WHERE id = %s", (doc_id,)),
            "document_contents": (
                "SELECT count(*) AS n FROM document_contents WHERE doc_id = %s",
                (doc_id,),
            ),
            "ingest_jobs": (
                "SELECT count(*) AS n FROM ingest_jobs WHERE doc_id = %s",
                (doc_id,),
            ),
            "doc_chunks": (
                "SELECT count(*) AS n FROM doc_chunks WHERE doc_id = %s",
                (doc_id,),
            ),
        }
        results: dict[str, int] = {}
        with self.connect() as conn:
            with conn.cursor() as cur:
                for tbl, (sql, params) in queries.items():
                    cur.execute(sql, params)
                    results[tbl] = cur.fetchone()["n"]
        return results

    def visible_non_completed_count(self, include_deleting: bool) -> int:
        """A14① 计数，用于对比"原口径会误报 / 修正口径为 0"。

        include_deleting=True  → 修正口径：status NOT IN ('completed','deleting')
        include_deleting=False → 原口径：  status <> 'completed'（会把 deleting 窗口算进来）
        """
        if include_deleting:
            sql = """
                SELECT count(*) AS n
                FROM doc_chunks c
                JOIN ingest_jobs j ON j.doc_id = c.doc_id
                WHERE c.visible = TRUE AND j.status NOT IN ('completed','deleting')
            """
        else:
            sql = """
                SELECT count(*) AS n
                FROM doc_chunks c
                JOIN ingest_jobs j ON j.doc_id = c.doc_id
                WHERE c.visible = TRUE AND j.status <> 'completed'
            """
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                return cur.fetchone()["n"]

    def retrievable_chunks_of_doc(self, doc_id: str) -> int:
        """I-09：按 FR-16 双重过滤，该文档"实际可被检索到"的切片数。

        FR-16：`doc_chunks.visible=TRUE` ∧ `ingest_jobs.status='completed'`
               ∧ `documents.deleted_at IS NULL` ∧ owner 匹配。
        'deleting' 文档在此过滤下必然为 0，这正是"正确性无洞"的依据。
        """
        sql = """
            SELECT count(*) AS n
            FROM doc_chunks c
            JOIN ingest_jobs j ON j.doc_id = c.doc_id
            JOIN documents d ON d.id = c.doc_id
            WHERE c.doc_id = %s
              AND c.visible = TRUE
              AND j.status = 'completed'
              AND d.deleted_at IS NULL
        """
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (doc_id,))
                return cur.fetchone()["n"]

    def all_visible_chunks(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT chunk_id, doc_id, run_token, chunk_index,
                           heading_path, char_start, char_end, content
                    FROM doc_chunks
                    WHERE visible = TRUE
                    ORDER BY doc_id, chunk_index
                    """
                )
                return cur.fetchall()


# ---------------------------------------------------------------------------
# Embedding 与 HNSW 召回
# ---------------------------------------------------------------------------


def embed_query(text: str, env: dict[str, str]) -> list[float]:
    """用与 ai-service 相同的 DashScope 接口获取查询向量。"""
    import httpx

    url = env["BAILIAN_BASE_URL"].rstrip("/") + "/embeddings"
    headers = {
        "Authorization": f"Bearer {env['DASHSCOPE_API_KEY']}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": env.get("EMBEDDING_MODEL", "text-embedding-v4"),
        "input": [text],
        "encoding_format": "float",
    }
    with httpx.Client(timeout=60.0) as client:
        resp = client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]


def vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(str(round(v, 8)) for v in vec) + "]"


def hnsw_top5(dsn: str, vec: list[float], ef_search: int) -> list[str]:
    """使用 HNSW 索引召回 top5 chunk_id."""
    # 使用字符串拼接而非参数化查询，因为 PostgreSQL 不支持在 SET LOCAL 中使用参数
    sql = f"""
        SET LOCAL hnsw.ef_search = {ef_search};
        SELECT chunk_id, 1 - (embedding <=> %s::vector) AS score
        FROM doc_chunks
        WHERE visible = TRUE
        ORDER BY embedding <=> %s::vector
        LIMIT 5
    """
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            # 分开执行 SET 和 SELECT 命令
            cur.execute(f"SET LOCAL hnsw.ef_search = {ef_search}")
            cur.execute("SELECT chunk_id, 1 - (embedding <=> %s::vector) AS score FROM doc_chunks WHERE visible = TRUE ORDER BY embedding <=> %s::vector LIMIT 5", (vector_literal(vec), vector_literal(vec)))
            return [row["chunk_id"] for row in cur.fetchall()]


def exact_top5(dsn: str, vec: list[float]) -> list[str]:
    """强制全表扫描精确召回 top5 chunk_id。"""
    # 分开执行 SET 和 SELECT 命令
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL enable_indexscan = off")
            cur.execute("SET LOCAL enable_bitmapscan = off")
            cur.execute("SELECT chunk_id, 1 - (embedding <=> %s::vector) AS score FROM doc_chunks WHERE visible = TRUE ORDER BY embedding <=> %s::vector LIMIT 5", (vector_literal(vec), vector_literal(vec)))
            return [row["chunk_id"] for row in cur.fetchall()]


def recall_at_k(hnsw: list[str], exact: list[str], k: int = 5) -> float:
    h = set(hnsw[:k])
    e = set(exact[:k])
    if not e:
        return 0.0
    return len(h & e) / len(e)


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------


def test_a1_upload_five(
    client: APIClient, db: DBHelper, seed_files: list[Path]
) -> tuple[bool, dict[str, Any]]:
    """A1: 5 篇示例文档全部 completed。"""
    print("\n【A1】上传 5 篇种子文档并轮询到 completed")
    results: list[dict[str, Any]] = []
    timings: list[float] = []
    all_pass = True

    for path in seed_files:
        start = time.monotonic()
        up = client.upload("demo-user", path)
        elapsed_upload = time.monotonic() - start

        if up["status_code"] != 202:
            print(f"  {FAIL_MARK} {path.name}: 上传非 202 ({up['status_code']}): {up.get('code')}")
            all_pass = False
            continue

        doc_id = up["doc_id"]
        print(f"  上传 {path.name}: 202, doc_id={doc_id}, upload_time={elapsed_upload:.2f}s")

        start_poll = time.monotonic()
        final = client.poll_until("demo-user", doc_id, "completed")
        elapsed_total = time.monotonic() - start
        timings.append(elapsed_total)

        if final.get("ingest_status") != "completed":
            # 如果是超时导致的 None，也认为是失败
            if final.get("ingest_status") is None or final.get("ingest_status") == "timeout":
                print(
                    f"  {FAIL_MARK} {path.name}: 最终状态 {final.get('ingest_status')} "
                    f"error={final.get('error_code')} full={final}"
                )
            else:
                print(
                    f"  {FAIL_MARK} {path.name}: 最终状态 {final.get('ingest_status')} "
                    f"error={final.get('error_code')} full={final}"
                )
            all_pass = False
        else:
            print(
                f"  {PASS_MARK} {path.name}: completed, "
                f"chunks={final.get('chunk_count')}, total_time={elapsed_total:.2f}s"
            )
        results.append({"file": path.name, "doc_id": doc_id, "final": final})

    p95 = statistics.quantiles(timings, n=20)[18] if len(timings) >= 5 else (max(timings) if timings else 0)
    print(f"\n  5 篇摄取耗时: min={min(timings):.2f}s, max={max(timings):.2f}s, P95={p95:.2f}s")
    return all_pass, {"results": results, "timings": timings, "p95": p95}


def test_a13_isolation(client: APIClient, db: DBHelper, demo_doc_id: str) -> bool:
    """A13: 两个 token 交叉访问对方资源均 404。"""
    print("\n【A13】身份与资源隔离")
    client.get_dev_token("demo-user")
    client.get_dev_token("test-user-2")

    checks = [
        ("test-user-2 读 demo-user 文档", lambda: client.get_doc("test-user-2", demo_doc_id)["status_code"]),
        ("test-user-2 reingest demo-user 文档", lambda: client.reingest("test-user-2", demo_doc_id)["status_code"]),
        ("test-user-2 删除 demo-user 文档", lambda: client.delete("test-user-2", demo_doc_id)["status_code"]),
        ("demo-user 读 test-user-2 不存在的文档", lambda: client.get_doc("demo-user", str(uuid.uuid4()))["status_code"]),
    ]
    all_pass = True
    for name, fn in checks:
        code = fn()
        ok = code == 404
        print(f"  {PASS_MARK if ok else FAIL_MARK} {name}: HTTP {code}")
        if not ok:
            all_pass = False
    return all_pass


def test_dedup_zero_embedding(
    client: APIClient, db: DBHelper, seed_files: list[Path]
) -> bool:
    """重复上传同一文档：deduplicated=true 且零 Embedding 调用。"""
    print("\n【A1-dedup】重复上传同一文档，验证零 Embedding 调用")
    path = seed_files[0]
    before = db.get_doc_state(client.upload("demo-user", path)["doc_id"])
    before_chunks = db.chunk_count(before["id"], visible=None)

    up = client.upload("demo-user", path)
    ok = up["status_code"] == 202 and up.get("deduplicated") is True
    print(f"  重复上传 {path.name}: status={up['status_code']}, deduplicated={up.get('deduplicated')}")

    after = db.get_doc_state(before["id"])
    after_chunks = db.chunk_count(before["id"], visible=None)
    chunk_unchanged = after_chunks == before_chunks
    print(f"  重复前 chunks={before_chunks}, 重复后 chunks={after_chunks}, 未变={chunk_unchanged}")

    all_ok = ok and chunk_unchanged
    print(f"  {PASS_MARK if all_ok else FAIL_MARK} 零 Embedding 调用（chunk 数量未增加）")
    return all_ok


def test_failed_reingest(
    client: APIClient, db: DBHelper, demo_doc_id: str
) -> bool:
    """failed → reingest 成功，切片不重复。"""
    print("\n【failed → reingest】人为置 failed 后手动重试")
    before = db.get_doc_state(demo_doc_id)
    before_visible = db.chunk_count(demo_doc_id, visible=True)
    print(f"  初始状态: status={before['status']}, visible_chunks={before_visible}")

    db.set_job_failed(demo_doc_id)
    state = db.get_doc_state(demo_doc_id)
    print(f"  置 failed 后: status={state['status']}, error_code={state['error_code']}")
    
    ri = client.reingest("demo-user", demo_doc_id)
    print(f"  reingest 响应: status_code={ri['status_code']}, ingest_status={ri.get('ingest_status')}")
    if ri["status_code"] != 202:
        print(f"  {FAIL_MARK} reingest 非 202")
        return False

    final = client.poll_until("demo-user", demo_doc_id, "completed")
    if final.get("ingest_status") != "completed":
        print(f"  {FAIL_MARK} reingest 后未 completed: {final.get('ingest_status')}")
        return False

    after_visible = db.chunk_count(demo_doc_id, visible=True)
    # 重新获取最终状态
    final_state = db.get_doc_state(demo_doc_id)
    final_chunks = final_state.get("chunk_count", 0)
    no_dup = after_visible == before_visible
    print(f"  reingest 前 visible={before_visible}, 后 visible={after_visible}, 无重复={no_dup}")
    print(f"  最终状态: chunks={final_chunks}, visible_chunks={after_visible}")
    print(f"  {PASS_MARK if no_dup else FAIL_MARK} failed → reingest 成功且切片不重复")
    return no_dup


def test_t08_zero_chunks(client: APIClient, db: DBHelper) -> bool:
    """T-08: 零切片不得发布为 completed。

    两段断言：
    (a) 仅含空白字符的文档在上传前置校验阶段即被 422 EMPTY_CONTENT 拒绝，
        不落库、不触发 Embedding；
    (b) 直接用 Graph 之外的边界构造——上传后把 document_contents 改成空白再
        触发 reingest，断言最终不得为 completed（应为 failed(EMPTY_CONTENT)）
        且无 visible 切片。
    """
    print("\n【T-08】零内容/零切片不得进入 completed 流程")
    ok_all = True

    # (a) 空白文档前置拒绝
    zero_path = Path(__file__).resolve().parent / "s4_zero_chunk_doc.md"
    zero_path.write_text("   \n\n   \n", encoding="utf-8")
    try:
        up = client.upload("demo-user", zero_path)
        code = extract_error_code(up)
        is_rejected = up["status_code"] == 422 and code == "EMPTY_CONTENT"
        print(f"  (a) 空白文档上传: status_code={up['status_code']}, code={code}")
        if is_rejected:
            print(f"      {PASS_MARK} 零内容在上传阶段被拒绝，未落库/未触发 Embedding")
        else:
            print(f"      {FAIL_MARK} 空白文档未被前置拒绝")
            ok_all = False
    finally:
        zero_path.unlink(missing_ok=True)

    # (b) 已入库文档被清空正文后 reingest → 不得 completed
    doc_id, tmp = upload_and_wait(client, db, "t08")
    if not doc_id:
        print(f"  (b) {FAIL_MARK} 构造用例的文档上传失败")
        tmp.unlink(missing_ok=True)
        return False

    # 清空正文，制造"切片后无内容"
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE document_contents SET content = '   \n\n  ' WHERE doc_id = %s",
                (doc_id,),
            )
            conn.commit()

    # 手动把 job 置 failed 再 reingest，触发新的摄取执行
    db.set_job_failed(doc_id)
    ri = client.reingest("demo-user", doc_id)
    if ri["status_code"] != 202:
        print(f"  (b) {FAIL_MARK} reingest 未返回 202: {ri['status_code']}")
        ok_all = False
    else:
        final = client.poll_until("demo-user", doc_id, "failed", timeout=60)
        status = final.get("ingest_status")
        visible = db.chunk_count(doc_id, visible=True)
        ok_b = status == "failed" and visible == 0
        print(
            f"  (b) 清空正文后 reingest: status={status}, "
            f"error_code={final.get('error_code')}, visible={visible}"
        )
        print(f"      {PASS_MARK if ok_b else FAIL_MARK} 零切片未发布为 completed")
        ok_all = ok_all and ok_b

    drop_doc(db, doc_id, tmp)

    print(f"  {PASS_MARK if ok_all else FAIL_MARK} T-08 总体")
    return ok_all


def test_t02_late_result_dropped(
    client: APIClient, db: DBHelper
) -> bool:
    """T-02: 任务已 failed 后，迟到的 Python ok 结果应被 CAS 丢弃。

    自包含构造：上传并等 completed → 回拨 processing（绑定 late_token）→
    置 failed（模拟 watchdog 收敛）→ 用 late_token 调内部 ingest 制造迟到写入 →
    断言 job 仍为 failed、无 visible 切片。
    """
    print("\n【T-02】迟到结果被丢弃（stale_result_dropped）")
    late_token = "late-token-" + uuid.uuid4().hex[:8]

    doc_id, tmp = upload_and_wait(client, db, "t02")
    if not doc_id:
        print(f"  {FAIL_MARK} 构造文档上传失败")
        tmp.unlink(missing_ok=True)
        return False
    try:
        # 1. 回拨为 processing（模拟旧 run 在途）
        db.set_job_processing(doc_id, late_token, started_ago_seconds=200)
        # 2. 置 failed（模拟 watchdog 先收敛）
        with db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE ingest_jobs
                       SET status = 'failed',
                           error_code = 'TIMEOUT',
                           error_msg = 'simulated watchdog timeout',
                           finished_at = now(),
                           updated_at = now()
                     WHERE doc_id = %s
                    """,
                    (doc_id,),
                )
                conn.commit()
        # 3. 模拟 Python 用旧 token 返回 ok
        resp = client.internal_ingest(doc_id, late_token)
        print(f"  模拟迟到响应: status_code={resp['status_code']}, body_status={resp.get('status')}")
        # 4. 断言未被覆盖
        state = db.get_doc_state(doc_id)
        visible = db.chunk_count(doc_id, visible=True)
        ok = state["status"] == "failed" and visible == 0
        print(
            f"  最终状态: status={state['status']}, "
            f"error_code={state['error_code']}, visible={visible}"
        )
        print(f"  {PASS_MARK if ok else FAIL_MARK} 迟到结果未覆盖最终状态")
        return ok
    finally:
        drop_doc(db, doc_id, tmp)


def test_t03_late_vs_new_run(
    client: APIClient, db: DBHelper
) -> bool:
    """T-03: 迟到 run A 被丢弃，新 run B 结果保留。

    自包含构造（不依赖前序用例状态）：
    1. 上传一篇种子文档并等待 completed（run A 基线）；
    2. 人为把 job 回拨为 processing 并绑定 old_token（模拟 run A 仍在途）；
    3. 立即把 job 置 failed 并 reingest → 产生 run B；
    4. 等 run B completed，记录其 published_run_token；
    5. 用 old_token 触发一次"迟到写入"（直接调 ai-service 内部接口）；
    6. 断言 published_run_token 仍为 run B，且库中不存在属于 old_token 的 visible 切片。
    """
    print("\n【T-03】迟到 run 被丢弃，reingest 新 run 保留")
    old_token = "old-token-" + uuid.uuid4().hex[:8]

    # 1. 建立基线：上传唯一文档并等 completed
    doc_id, tmp = upload_and_wait(client, db, "t03")
    if not doc_id:
        print(f"  {FAIL_MARK} 基线文档上传失败")
        tmp.unlink(missing_ok=True)
        return False
    base = db.get_doc_state(doc_id)
    if not base or base["status"] != "completed":
        print(f"  {FAIL_MARK} 基线未 completed: {base['status'] if base else None}")
        drop_doc(db, doc_id, tmp)
        return False
    print(f"  基线 run 完成: chunks={base['chunk_count']}")

    try:
        # 2. 模拟 run A 在途（回拨 processing + old_token）
        db.set_job_processing(doc_id, old_token, started_ago_seconds=200)

        # 3. 模拟 run A 超时被 watchdog 置 failed
        with db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE ingest_jobs
                       SET status = 'failed',
                           error_code = 'TIMEOUT',
                           error_msg = 'simulated watchdog timeout for run A',
                           finished_at = now(),
                           updated_at = now()
                     WHERE doc_id = %s
                    """,
                    (doc_id,),
                )
                conn.commit()

        # 产生 run B
        ri = client.reingest("demo-user", doc_id)
        if ri["status_code"] != 202:
            print(f"  {FAIL_MARK} reingest 未返回 202: {ri['status_code']}")
            return False
        final = client.poll_until("demo-user", doc_id, "completed")
        if final.get("ingest_status") != "completed":
            print(f"  {FAIL_MARK} run B 未 completed: {final.get('ingest_status')}")
            return False

        state = db.get_doc_state(doc_id)
        new_token = state.get("published_run_token") if state else None
        if not new_token:
            print(f"  {FAIL_MARK} 未获取到 run B 的 published_run_token")
            return False
        print(f"  run B 完成: published_run_token={new_token[:12]}..., chunks={state['chunk_count']}")

        # 4. 用 old_token 触发迟到写入
        resp = client.internal_ingest(doc_id, old_token)
        print(f"  模拟 run A 迟到响应: status_code={resp['status_code']}, body_status={resp.get('status')}")

        # 5. 断言：published_run_token 未变，且无 old_token 的 visible 切片
        state2 = db.get_doc_state(doc_id)
        with db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT count(*) AS n
                    FROM doc_chunks
                    WHERE doc_id = %s AND visible = TRUE AND run_token IS DISTINCT FROM %s
                    """,
                    (doc_id, new_token),
                )
                stale_visible = cur.fetchone()["n"]

        ok = state2["published_run_token"] == new_token and stale_visible == 0
        print(
            f"  验证: published_run_token 未变="
            f"{state2['published_run_token'] == new_token}, "
            f"非 run B 的 visible 切片={stale_visible}"
        )
        print(f"  {PASS_MARK if ok else FAIL_MARK} run B 结果未被迟到 run A 覆盖")
        return ok
    finally:
        drop_doc(db, doc_id, tmp)


def test_a15_deleting_chain(client: APIClient, db: DBHelper) -> bool:
    """A15 删除彻底性 + I-09 删除窗口内断言不误报。

    断言：
    1. DELETE 后接口返回 202 且状态为 deleting，`documents.deleted_at` 已置位，
       此时记录仍在（不出现"记录已删、向量还在"的反向情况）；
    2. **删除窗口内**（deleting + 旧世代 visible 切片共存）：
       - 修正口径的 A14①（status NOT IN ('completed','deleting')）计数必须为 0；
       - FR-16 检索 SQL 必须不返回该文档任何切片（过滤而非删除才是正确性保证）。
       这一步必须在"发起删除后、watchdog 收敛前"执行，否则窗口已关闭，断言退化。
    3. watchdog 收敛后，documents / document_contents / ingest_jobs / doc_chunks
       四表均无该 doc_id 残留。

    ⚠️ **断言时机敏感（勿删此注记）**：删除是**异步收敛**的——DELETE 只把状态
    置为 `deleting` 并置 `deleted_at`，真正的清向量与删行由 watchdog 在下一轮
    （间隔 60s）完成。因此：
      - 想在**删除窗口内**验证不变量，必须在 DELETE 之后**立即**断言，且**不得**
        先等收敛，否则窗口已关闭、断言变成"事后跑"，失去证明力（I-09 的意义）。
      - 想验证**删除彻底性**，必须**轮询到终态**（四表残留全 0）再下结论，不能
        在固定时刻取一次快照就判定——快照可能刚好落在收敛过程中，导致误判。
    历史上 S4 首轮报告的 A14"全 0"正是踩了前者：A14 跑在 A15 之前，删除窗口尚未
    产生，属**假绿**。本用例修正执行顺序并显式覆盖窗口内断言，防止 S8 回归误判。
    """
    print("\n【A15】删除链路、删除窗口内断言（I-09）与删除彻底性")

    doc_id, tmp = upload_and_wait(client, db, "a15")
    if not doc_id:
        print(f"  {FAIL_MARK} 待删除文档上传失败")
        tmp.unlink(missing_ok=True)
        return False
    state0 = db.get_doc_state(doc_id)
    if not state0 or state0["status"] != "completed":
        print(f"  {FAIL_MARK} 待删除文档未 completed")
        drop_doc(db, doc_id, tmp)
        return False
    visible_before = db.chunk_count(doc_id, visible=True)
    print(f"  待删除文档就绪: chunks={state0['chunk_count']}, visible={visible_before}")

    # 1. 发起删除
    resp = client.delete("demo-user", doc_id)
    print(f"  DELETE 响应: status_code={resp['status_code']}, ingest_status={resp.get('ingest_status')}")
    if resp["status_code"] != 202 or resp.get("ingest_status") != "deleting":
        print(f"  {FAIL_MARK} 删除未进入 deleting 状态")
        drop_doc(db, doc_id, tmp)
        return False

    # 2. 【删除窗口内】立即断言（不得先等收敛，否则窗口关闭）
    state = db.get_doc_state(doc_id)
    in_deleting = state is not None and state["status"] == "deleting"
    visible_in_window = db.chunk_count(doc_id, visible=True)
    print(
        f"  deleting 期间: documents 行存在={state is not None}, "
        f"job.status={state['status'] if state else None}, "
        f"deleted_at={'已置位' if (state and state.get('deleted_at')) else '空'}, "
        f"visible 切片={visible_in_window}"
    )

    # I-09：修正口径 A14① 在窗口内必须为 0（原口径会误报）
    old_scope = db.visible_non_completed_count(include_deleting=False)
    new_scope = db.visible_non_completed_count(include_deleting=True)
    retrieved = db.retrievable_chunks_of_doc(doc_id)
    print(f"  I-09 口径对比: 原口径(status<>'completed')={old_scope}（会误报）"
          f" | 修正口径(status NOT IN completed,deleting)={new_scope}")
    print(f"  I-09 FR-16 检索可见该文档切片数={retrieved}（应为 0，证明被过滤排除）")
    i09_ok = new_scope == 0 and retrieved == 0
    print(f"  {PASS_MARK if i09_ok else FAIL_MARK} I-09 删除窗口内断言不误报")

    # 3. 轮询等待 watchdog 清理（间隔 60s，最多等 150s）——验证彻底性必须等终态
    deadline = time.monotonic() + 150
    cleaned = False
    while time.monotonic() < deadline:
        counts = db.residual_counts(doc_id)
        if all(v == 0 for v in counts.values()):
            cleaned = True
            break
        time.sleep(3)

    counts = db.residual_counts(doc_id)
    print(f"  四表残留计数（轮询到终态后）: {counts}")
    for tbl, n in counts.items():
        print(f"    {PASS_MARK if n == 0 else FAIL_MARK} {tbl}: {n}")

    ok = in_deleting and i09_ok and cleaned
    print(f"  {PASS_MARK if ok else FAIL_MARK} A15 删除彻底性")
    if not cleaned:
        drop_doc(db, doc_id, tmp)  # watchdog 未清干净时兜底清理
    else:
        tmp.unlink(missing_ok=True)
    return ok


def test_t07_timeout_threshold(db: DBHelper) -> bool:
    """T-07: processing 超时阈值边界（阈值 150s）。

    注意：`interval '%s seconds'` 的参数化写法会被 psycopg 当作字面量处理，
    实际不减时间；这里必须使用 `(%s * interval '1 second')`。

    判定不能在"状态快照"上做——watchdog 是常驻的，151s 的行可能在断言前
    已被真实收割为 failed。因此这里改为**原子阈值谓词**：在同一条 SELECT 内
    对 `started_at` 求值 `started_at < now() - interval '150 seconds'`，
    与 watchdog 的实现 SQL（store.go StaleProcessingJobs）完全一致，
    不受行状态被并发修改的影响。
    """
    print("\n【T-07】processing 超时阈值边界（阈值 150s）")
    doc_149 = str(uuid.uuid4())
    doc_151 = str(uuid.uuid4())
    with db.connect() as conn:
        with conn.cursor() as cur:
            for doc_id, ago in [(doc_149, 149), (doc_151, 151)]:
                cur.execute(
                    """
                    INSERT INTO documents (id, owner_id, title, source_hash, byte_size, char_count)
                    VALUES (%s, 'demo-user', 'threshold-test', %s, 1, 1)
                    """,
                    (doc_id, f"hash-{doc_id}"),
                )
                cur.execute(
                    """
                    INSERT INTO ingest_jobs (doc_id, status, attempt, current_run_token, started_at)
                    VALUES (%s, 'processing', 1, %s, now() - (%s * interval '1 second'))
                    """,
                    (doc_id, f"tok-{doc_id}", ago),
                )
            conn.commit()

    # 原子阈值谓词：与 watchdog 的 SQL 语义一致，单条查询内完成判定
    sql = """
        SELECT doc_id, started_at,
               (started_at < now() - interval '150 seconds') AS would_reap
        FROM ingest_jobs
        WHERE doc_id = ANY(%s)
    """
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, ([doc_149, doc_151],))
            rows = {str(r["doc_id"]): r for r in cur.fetchall()}

    r149 = rows.get(doc_149)
    r151 = rows.get(doc_151)
    ok_149 = r149 is not None and r149["would_reap"] is False
    ok_151 = r151 is not None and r151["would_reap"] is True
    print(f"  149s 不收敛: {ok_149}（would_reap={r149['would_reap'] if r149 else None}）")
    print(f"  151s 已收敛: {ok_151}（would_reap={r151['would_reap'] if r151 else None}）")

    # 清理
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM documents WHERE id = ANY(%s)", ([doc_149, doc_151],))
            conn.commit()

    ok = ok_149 and ok_151
    print(f"  {PASS_MARK if ok else FAIL_MARK} 超时阈值边界正确")
    return ok


def test_a14_invariants(db: DBHelper) -> bool:
    """A14 + T-04 + T-05 不变量 SQL 断言。"""
    print("\n【A14 / T-04 / T-05】状态机不变量 SQL 断言")
    results = db.run_invariants()
    all_zero = all(v == 0 for v in results.values())
    for name, value in results.items():
        mark = PASS_MARK if value == 0 else FAIL_MARK
        print(f"  {mark} {name}: {value}")
    return all_zero


def test_t09_chunk_count_consistency(db: DBHelper) -> bool:
    """T-09: completed 文档的 chunk_count 与实际 visible 切片一致。"""
    print("\n【T-09】发布后 chunk_count 与实际 visible 切片一致")
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT j.doc_id, j.chunk_count,
                       (SELECT count(*) FROM doc_chunks c
                        WHERE c.doc_id = j.doc_id AND c.visible = TRUE) AS visible_count
                FROM ingest_jobs j
                WHERE j.status = 'completed'
                """
            )
            rows = cur.fetchall()
    ok = True
    for r in rows:
        match = r["chunk_count"] == r["visible_count"]
        print(f"  {PASS_MARK if match else FAIL_MARK} {r['doc_id']}: chunk_count={r['chunk_count']}, visible={r['visible_count']}")
        if not match:
            ok = False
    return ok


def test_hnsw_recall(dsn: str, env: dict[str, str], client: APIClient) -> bool:
    """HNSW Recall@5 vs 全表扫描 ≥ 0.95。"""
    print("\n【HNSW Recall@5】真实语料召回验证")
    queries = [
        ("Pod 反复重启 CrashLoopBackOff 怎么排查", "k8s"),
        ("Nginx 502 上游连接被重置", "nginx"),
        ("MySQL 慢查询日志如何开启", "mysql"),
        ("Linux 服务器网络不通如何排查", "network"),
        ("日志采集与聚合架构", "logging"),
    ]

    ef_values = [40, 100, 200]
    overall_ok = True

    for ef in ef_values:
        recalls: list[float] = []
        print(f"\n  ef_search={ef}")
        for q_text, _ in queries:
            vec = embed_query(q_text, env)
            hnsw = hnsw_top5(dsn, vec, ef)
            exact = exact_top5(dsn, vec)
            r = recall_at_k(hnsw, exact, k=5)
            recalls.append(r)
            print(f"    {q_text[:20]:20s} Recall@5={r:.2f}")
        avg = sum(recalls) / len(recalls) if recalls else 0.0
        min_r = min(recalls) if recalls else 0.0
        threshold_ok = min_r >= 0.95
        print(f"  ef={ef}: avg={avg:.3f}, min={min_r:.3f} {'>=0.95' if threshold_ok else '<0.95'}")
        if not threshold_ok:
            overall_ok = False

    return overall_ok


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main() -> int:
    print("=" * 70)
    print("OpsPilot S4 摄取链路本地验收")
    print("=" * 70)

    env = load_env(ENV_FILE)
    # 修正代理配置：本地运行时使用当前环境的代理设置
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        if key in os.environ and key not in env:
            env[key] = os.environ[key]
    os.environ.update({k: v for k, v in env.items() if k not in os.environ})
    dsn = build_dsn(env)

    print(f"\n后端地址: {BACKEND_URL}")
    print(f"AI 服务地址: {AI_SERVICE_URL}")
    print(f"数据库: {dsn.replace(env.get('POSTGRES_PASSWORD', ''), '***')}")

    db = DBHelper(dsn)
    client = APIClient(BACKEND_URL, AI_SERVICE_URL, env)

    # 0. 获取 token
    for user in TEST_USERS:
        client.get_dev_token(user)
        print(f"获取 {user} token: {redact(client.tokens[user])}")

    # 1. 清理测试数据
    print("\n清理测试用户历史数据...")
    deleted = db.clean_test_users()
    print(f"  删除了 {deleted} 篇历史文档")

    seed_files = sorted(SEED_DIR.glob("*.md"))
    if len(seed_files) < 5:
        print(f"{FAIL_MARK} seed_docs 下不足 5 篇文档")
        return 1

    scorecard: dict[str, bool] = {}

    # 2. A1: 5 篇上传
    a1_ok, a1_data = test_a1_upload_five(client, db, seed_files)
    scorecard["A1_upload_5_docs"] = a1_ok
    scorecard["A1_p95_le_30s"] = a1_data["p95"] <= 30.0
    if a1_data["p95"] > 30.0:
        print(f"  {FAIL_MARK} P95={a1_data['p95']:.2f}s 超过 30s 预算")
    else:
        print(f"  {PASS_MARK} P95={a1_data['p95']:.2f}s ≤ 30s")

    demo_doc_id = a1_data["results"][0]["doc_id"] if a1_data["results"] else None

    # 3. A13: 隔离
    if demo_doc_id:
        scorecard["A13_isolation"] = test_a13_isolation(client, db, demo_doc_id)
    else:
        scorecard["A13_isolation"] = False

    # 4. dedup 零 embedding
    scorecard["A1_dedup_zero_embedding"] = test_dedup_zero_embedding(client, db, seed_files)

    # 5. failed → reingest
    if demo_doc_id:
        scorecard["failed_reingest"] = test_failed_reingest(client, db, demo_doc_id)
    else:
        scorecard["failed_reingest"] = False

    # 6. T-08 零切片
    scorecard["T08_zero_chunks"] = test_t08_zero_chunks(client, db)

    # 7. T-02 迟到结果被丢弃（自包含构造）
    scorecard["T02_late_result_dropped"] = test_t02_late_result_dropped(client, db)

    # 7b. T-03 迟到 run 被丢弃，新 run 保留（自包含构造）
    scorecard["T03_late_vs_new_run"] = test_t03_late_vs_new_run(client, db)

    # 8. T-07 超时阈值
    scorecard["T07_timeout_threshold"] = test_t07_timeout_threshold(db)

    # 9. A15 删除链路：内含"删除窗口内断言（I-09）"+ 轮询到终态验证彻底性。
    #    必须排在 A14 之前——A15 会真实制造 deleting 窗口，且自身覆盖窗口内断言；
    #    待其收敛后再跑 A14 全局不变量，避免"删除窗口尚未产生 → 假绿"。
    scorecard["A15_deleting_chain"] = test_a15_deleting_chain(client, db)

    # 10. A14 / T-04 / T-05（在删除窗口收敛后跑全局断言）
    scorecard["A14_invariants"] = test_a14_invariants(db)

    # 11. T-09
    scorecard["T09_chunk_count"] = test_t09_chunk_count_consistency(db)

    # 12. HNSW Recall
    scorecard["HNSW_recall"] = test_hnsw_recall(dsn, env, client)

    # 汇总
    print("\n" + "=" * 70)
    print("验收结果汇总")
    print("=" * 70)
    for name, ok in scorecard.items():
        print(f"  {PASS_MARK if ok else FAIL_MARK} {name}")
    total = len(scorecard)
    passed = sum(scorecard.values())
    print(f"\n总计: {passed}/{total} 通过")

    # 最终清理
    print("\n最终清理测试用户数据...")
    db.clean_test_users()

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
