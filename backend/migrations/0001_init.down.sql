-- 0001_init 回滚
-- 规范 N1-b：先清 visible=false 的孤儿切片，再删列/表

-- 1. 先删孤儿切片：回滚后 visible 列将消失，未发布切片会被视为正常切片参与检索
DELETE FROM doc_chunks WHERE visible = FALSE;

-- 2. 按依赖逆序删表（IF EXISTS 幂等）
DROP TABLE IF EXISTS tool_calls;
DROP TABLE IF EXISTS run_steps;
DROP TABLE IF EXISTS agent_runs;
DROP TABLE IF EXISTS messages;
DROP TABLE IF EXISTS sessions;
DROP TABLE IF EXISTS doc_chunks;
DROP TABLE IF EXISTS ingest_jobs;
DROP TABLE IF EXISTS document_contents;
DROP TABLE IF EXISTS documents;

-- 3. 扩展可选清理（通常保留，但 down 脚本保持完整）
-- DROP EXTENSION IF EXISTS pgcrypto;
-- DROP EXTENSION IF EXISTS vector;
