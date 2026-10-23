-- 0001_init: OpsPilot V1 初始迁移
-- 来源: docs/architecture/data-model.md v1.1
-- 规范: 所有 CREATE 带 IF NOT EXISTS（N1-a）

-- 扩展
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- 1. documents
CREATE TABLE IF NOT EXISTS documents (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id     TEXT        NOT NULL,
    title        TEXT        NOT NULL,
    source_hash  CHAR(64)    NOT NULL,
    byte_size    INTEGER     NOT NULL,
    char_count   INTEGER     NOT NULL,
    deleted_at   TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_documents_owner_hash
    ON documents (owner_id, source_hash)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_documents_owner_created
    ON documents (owner_id, created_at DESC);

-- 2. document_contents
CREATE TABLE IF NOT EXISTS document_contents (
    doc_id         UUID PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    content        TEXT        NOT NULL,
    char_count     INTEGER     NOT NULL,
    token_estimate INTEGER     NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 3. ingest_jobs
CREATE TABLE IF NOT EXISTS ingest_jobs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    doc_id              UUID NOT NULL UNIQUE REFERENCES documents(id) ON DELETE CASCADE,
    status              TEXT NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending','processing','completed','failed','deleting')),
    attempt             INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    current_run_token   TEXT,
    published_run_token TEXT,
    chunk_count         INTEGER NOT NULL DEFAULT 0,
    error_code          TEXT,
    error_msg           TEXT,
    started_at          TIMESTAMPTZ,
    finished_at         TIMESTAMPTZ,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ingest_jobs_watchdog
    ON ingest_jobs (status, COALESCE(started_at, updated_at));

-- 4. doc_chunks
CREATE TABLE IF NOT EXISTS doc_chunks (
    chunk_id     UUID PRIMARY KEY,
    doc_id       UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    run_token    TEXT,
    visible      BOOLEAN NOT NULL DEFAULT FALSE,
    chunk_index  INTEGER NOT NULL,
    heading_path TEXT,
    char_start   INTEGER NOT NULL,
    char_end     INTEGER NOT NULL,
    content      TEXT NOT NULL,
    embedding    vector(1024) NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_doc_chunks_embedding
    ON doc_chunks USING hnsw (embedding vector_cosine_ops)
    WHERE visible = TRUE;

CREATE INDEX IF NOT EXISTS idx_doc_chunks_doc_visible
    ON doc_chunks (doc_id, visible);

CREATE INDEX IF NOT EXISTS idx_doc_chunks_run_token
    ON doc_chunks (run_token);

-- 5. sessions
CREATE TABLE IF NOT EXISTS sessions (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id   TEXT        NOT NULL,
    title      TEXT        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sessions_owner_updated
    ON sessions (owner_id, updated_at DESC);

-- 6. messages
CREATE TABLE IF NOT EXISTS messages (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID        NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role       TEXT        NOT NULL CHECK (role IN ('user','assistant','system')),
    content    TEXT        NOT NULL,
    citations  JSONB       DEFAULT '[]'::jsonb,
    degraded   BOOLEAN     NOT NULL DEFAULT FALSE,
    run_id     UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_messages_session_created
    ON messages (session_id, created_at DESC);

-- 7. agent_runs
CREATE TABLE IF NOT EXISTS agent_runs (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id        UUID        NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    message_id        UUID        NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    owner_id          TEXT        NOT NULL,
    route             TEXT        NOT NULL,
    llm_calls         INTEGER     NOT NULL DEFAULT 0,
    tool_calls        INTEGER     NOT NULL DEFAULT 0,
    prompt_tokens     INTEGER     NOT NULL DEFAULT 0,
    completion_tokens INTEGER     NOT NULL DEFAULT 0,
    total_tokens      INTEGER     NOT NULL DEFAULT 0,
    degraded          BOOLEAN     NOT NULL DEFAULT FALSE,
    degrade_reasons   TEXT[]      DEFAULT '{}'::text[],
    terminated_reason TEXT,
    node_path         TEXT[]      DEFAULT '{}'::text[],
    preempted         TEXT[]      DEFAULT '{}'::text[],
    duration_ms       INTEGER,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_session_created
    ON agent_runs (session_id, created_at DESC);

-- 8. run_steps
CREATE TABLE IF NOT EXISTS run_steps (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id         UUID        NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    step_index     INTEGER     NOT NULL,
    node           TEXT        NOT NULL,
    duration_ms    INTEGER,
    input_digest   TEXT,
    output_summary TEXT,
    degraded       BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, step_index)
);

CREATE INDEX IF NOT EXISTS idx_run_steps_run_id
    ON run_steps (run_id, step_index);

-- 9. tool_calls
CREATE TABLE IF NOT EXISTS tool_calls (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id      UUID        NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    step_index  INTEGER     NOT NULL,
    tool_name   TEXT        NOT NULL,
    args_digest TEXT,
    status      TEXT        NOT NULL,
    duration_ms INTEGER,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tool_calls_run_id
    ON tool_calls (run_id, step_index);
