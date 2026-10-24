-- 0003 回滚：移除证据链字段

ALTER TABLE agent_runs
    DROP COLUMN IF EXISTS retrieval_top_score,
    DROP COLUMN IF EXISTS rewritten_query;