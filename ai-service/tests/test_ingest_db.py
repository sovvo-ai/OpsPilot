"""摄取写入的集成测试（需要真实 PostgreSQL + pgvector）。

无数据库时自动 skip。运行方式：

    POSTGRES_HOST=localhost POSTGRES_PORT=55432 \\
    POSTGRES_USER=ops_pilot POSTGRES_PASSWORD=... POSTGRES_DB=ops_pilot \\
    pytest tests/test_ingest_db.py -v

覆盖重点：两阶段发布协议中"Python 阶段一清全世代 + 写不可见切片"
（docs/architecture/ingest-consistency.md §4.1）——这是重摄取路径下
避免确定性 chunk_id（uuidv5(doc_id+chunk_index)）主键冲突的关键。
"""

import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402
import pytest  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from app.db import build_dsn  # noqa: E402
from app.ingest import chunk_text  # noqa: E402

# ---------------------------------------------------------------------------
# 连接与夹具
# ---------------------------------------------------------------------------


def _can_connect() -> bool:
    try:
        with psycopg.connect(build_dsn(), connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(
    not _can_connect(), reason="需要可用的 PostgreSQL（含 pgvector）"
)

OWNER = "ingest-int-test-user"


@pytest.fixture()
def doc_with_content():
    """建一个临时文档 + 正文，测试结束后级联清理。"""
    doc_id = str(uuid.uuid4())
    dsn = build_dsn()
    content = (
        "# 集成测试文档\n\n"
        "## 第一节\n\n"
        + "这是一段用于验证切片与世代清理的正文内容。" * 30
        + "\n\n## 第二节\n\n"
        + "第二段同样需要足够长度以形成多个切片。" * 30
        + "\n"
    )
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO documents (id, owner_id, title, source_hash, byte_size, char_count)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (doc_id, OWNER, "int-test", f"hash-{doc_id}", len(content), len(content)),
            )
            cur.execute(
                """INSERT INTO document_contents (doc_id, content, char_count, token_estimate)
                   VALUES (%s, %s, %s, %s)""",
                (doc_id, content, len(content), len(content) // 2),
            )
            cur.execute(
                "INSERT INTO ingest_jobs (doc_id, status, attempt) VALUES (%s, 'pending', 0)",
                (doc_id,),
            )
            conn.commit()
    yield doc_id, content
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM documents WHERE id = %s", (doc_id,))
            conn.commit()


def _write_generation(doc_id: str, run_token: str, content: str) -> int:
    """复刻 ingest_document 的库内写入部分（清全世代 + 写不可见切片）。"""
    chunks = chunk_text(content)
    doc_uuid = uuid.UUID(doc_id)
    dsn = build_dsn()
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM doc_chunks WHERE doc_id = %s", (doc_id,))
            for c in chunks:
                chunk_id = uuid.uuid5(doc_uuid, str(c["chunk_index"]))
                cur.execute(
                    """INSERT INTO doc_chunks
                       (chunk_id, doc_id, run_token, visible, chunk_index,
                        heading_path, char_start, char_end, content, embedding)
                       VALUES (%s, %s, %s, FALSE, %s, %s, %s, %s, %s, %s::vector)""",
                    (
                        str(chunk_id),
                        doc_id,
                        run_token,
                        c["chunk_index"],
                        c["heading_path"],
                        c["char_start"],
                        c["char_end"],
                        c["content"],
                        "[" + ",".join(["0.0"] * 1024) + "]",
                    ),
                )
            conn.commit()
    return len(chunks)


def _counts(doc_id: str) -> dict[str, int]:
    dsn = build_dsn()
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT count(*) AS total,
                          count(*) FILTER (WHERE visible) AS visible,
                          count(*) FILTER (WHERE NOT visible) AS invisible
                     FROM doc_chunks WHERE doc_id = %s""",
                (doc_id,),
            )
            return cur.fetchone()


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


def test_first_generation_all_invisible(doc_with_content):
    """首次写入：全部切片 visible=false（发布前检索不可见）。"""
    doc_id, content = doc_with_content
    n = _write_generation(doc_id, "run-a", content)
    assert n > 0
    c = _counts(doc_id)
    assert c["total"] == n
    assert c["visible"] == 0
    assert c["invisible"] == n


def test_retry_clears_old_generation_no_pk_conflict(doc_with_content):
    """重摄取：清全世代后重写，不因确定性 chunk_id 主键冲突而失败。

    这是 S4 验收暴露的真实缺陷回归点：run A 写入 chunk_id=uuid5(doc+idx)，
    run B 若不清旧世代，INSERT 必然主键冲突 → 摄取失败。
    """
    doc_id, content = doc_with_content
    n1 = _write_generation(doc_id, "run-a", content)

    # run B 重写：不得抛异常（主键冲突会被 psycopg 抛 UniqueViolation）
    n2 = _write_generation(doc_id, "run-b", content)

    c = _counts(doc_id)
    assert c["total"] == n2, "重摄取后应只剩新世代切片"
    assert c["visible"] == 0, "新世代发布前不可见"
    assert n1 == n2, "相同内容两次切片数量应一致"


def test_publish_flips_visible_and_drops_old(doc_with_content):
    """发布：本世代翻 visible=true，旧世代物理删除（对齐 §4.2）。"""
    doc_id, content = doc_with_content
    _write_generation(doc_id, "run-a", content)
    n = _write_generation(doc_id, "run-b", content)

    dsn = build_dsn()
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE doc_chunks SET visible = TRUE WHERE doc_id = %s AND run_token = %s",
                (doc_id, "run-b"),
            )
            cur.execute(
                "DELETE FROM doc_chunks WHERE doc_id = %s AND run_token IS DISTINCT FROM %s",
                (doc_id, "run-b"),
            )
            conn.commit()

    c = _counts(doc_id)
    assert c["total"] == n
    assert c["visible"] == n
    assert c["invisible"] == 0

    # 复核：可见切片全部属于 run-b
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM doc_chunks WHERE doc_id = %s AND run_token <> %s",
                (doc_id, "run-b"),
            )
            assert cur.fetchone()["n"] == 0


def test_chunk_id_is_deterministic(doc_with_content):
    """chunk_id = uuidv5(doc_id, chunk_index)，确定性可复现。"""
    doc_id, content = doc_with_content
    doc_uuid = uuid.UUID(doc_id)
    chunks = chunk_text(content)
    expected = [str(uuid.uuid5(doc_uuid, str(c["chunk_index"]))) for c in chunks]
    assert expected[0] == str(uuid.uuid5(doc_uuid, "0"))
    assert len(set(expected)) == len(expected)