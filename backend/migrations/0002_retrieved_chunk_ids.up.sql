-- 0002_add_retrieved_chunk_ids
-- 目的：评测（S8）计算 Recall@5 / MRR@5 需要**本次检索的完整 Top-K 集合**，
--       而 agent_runs 此前只存了被引用的切片（citations 在 messages 里）。
--       没有完整集合，Recall 只能按 citations 近似 —— 那会系统性低估召回
--       （S6 验收时已踩过该口径错误）。
-- 来源：实测（S8 评测真跑前置）
-- 影响：仅新增可空列，对既有行无影响。

ALTER TABLE agent_runs
    ADD COLUMN IF NOT EXISTS retrieved_chunk_ids uuid[] DEFAULT '{}'::uuid[];

COMMENT ON COLUMN agent_runs.retrieved_chunk_ids IS
    '本次检索的完整 Top-K chunk_id 列表（按分数降序）。评测算 Recall@5/MRR@5 用，不含原始文本。';

-- 便于按 run 回查检索集
CREATE INDEX IF NOT EXISTS idx_agent_runs_retrieved
    ON agent_runs USING GIN (retrieved_chunk_ids);