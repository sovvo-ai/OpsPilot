-- 0002 回滚：移除评测用的检索集列

DROP INDEX IF EXISTS idx_agent_runs_retrieved;
ALTER TABLE agent_runs DROP COLUMN IF EXISTS retrieved_chunk_ids;