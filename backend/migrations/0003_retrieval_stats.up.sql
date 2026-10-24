-- 0003_add_retrieval_stats
-- 目的：D2 —— 证据链字段缺失。`TurnResult` 里有 `retrieval_top_score` /
--       `rewritten_query`，但**没有落库**，导致 `GET /runs/{id}` 读不到
--       （报告里 28/28 案例 top_score=0.0，而 retrieved_chunk_ids 明明有值）。
--       这两个字段是 demo 轨迹与人工复核（A3b）的证据链，必须可回查。
-- 来源：实测（S8 外部审查 D2 裁定）
-- 影响：仅新增可空列，对既有行无影响。

ALTER TABLE agent_runs
    ADD COLUMN IF NOT EXISTS retrieval_top_score DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS rewritten_query TEXT;

COMMENT ON COLUMN agent_runs.retrieval_top_score IS
    '本次检索最高分（Top-1 score）。D2：报告/轨迹证据链字段，此前只存在于 TurnResult 未落库。';
COMMENT ON COLUMN agent_runs.rewritten_query IS
    'FR-17 指代改写后的查询（未触发为 NULL）。D2：轨迹与人工复核需要。';