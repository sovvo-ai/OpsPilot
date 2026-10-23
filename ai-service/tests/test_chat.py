"""chat 模块的纯工具函数单测（不依赖 DB/网络）。

覆盖：
- 引用提取：只保留属于本次检索集的 `[n]`，编造编号被剔除（FR-22）
- 引用校验：chunk_id 不在检索集 → 剔除
- prompt 组装：片段编号与历史拼接
- 拒答判定：无有效引用 / 呈"依据不足"特征 → refusal

编排链路（图结构、条件边、预算、rewrite）见 tests/test_graph.py。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.chat import (  # noqa: E402
    build_prompt,
    decide_answer_type,
    extract_citations,
    validate_citations,
)

CHUNKS = [
    {
        "chunk_id": "11111111-1111-1111-1111-111111111111",
        "doc_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "doc_title": "k8s-pod-troubleshooting.md",
        "score": 0.83,
        "content": "CrashLoopBackOff 表示容器启动后反复退出。",
        "heading_path": "1. Pod 启动失败 > CrashLoopBackOff",
    },
    {
        "chunk_id": "22222222-2222-2222-2222-222222222222",
        "doc_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "doc_title": "k8s-pod-troubleshooting.md",
        "score": 0.71,
        "content": "Exit Code 137 通常是 OOMKilled。",
        "heading_path": "2. 内存与探针配置",
    },
]


class TestExtractCitations:
    def test_extract_valid_indexes(self):
        answer = "反复退出说明 CrashLoopBackOff[1]，137 多为 OOM[2]。"
        cits = extract_citations(answer, CHUNKS)
        assert len(cits) == 2
        assert cits[0]["chunk_id"] == CHUNKS[0]["chunk_id"]
        assert cits[1]["chunk_id"] == CHUNKS[1]["chunk_id"]

    def test_fabricated_index_removed(self):
        """文本里出现 [9]（检索集只有 2 条）→ 必须剔除（FR-22）。"""
        answer = "结论甲[1]，结论乙[9]，结论丙[99]。"
        cits = extract_citations(answer, CHUNKS)
        assert len(cits) == 1
        assert cits[0]["chunk_id"] == CHUNKS[0]["chunk_id"]

    def test_no_citations(self):
        assert extract_citations("没有任何引用的回答。", CHUNKS) == []

    def test_dedup_same_index(self):
        answer = "先说一次[1]，再说一次[1]。"
        cits = extract_citations(answer, CHUNKS)
        assert len(cits) == 1

    def test_snippet_truncated_to_200(self):
        long_chunk = dict(CHUNKS[0], content="字" * 500)
        cits = extract_citations("看这里[1]", [long_chunk])
        assert len(cits[0]["snippet"]) <= 200

    def test_snippet_no_newline(self):
        chunk = dict(CHUNKS[0], content="第一行\n第二行")
        cits = extract_citations("[1]", [chunk])
        assert "\n" not in cits[0]["snippet"]

    def test_citation_fields(self):
        cits = extract_citations("[1]", CHUNKS)
        c = cits[0]
        assert c["source_type"] == "kb"
        assert c["status"] == "live"
        assert c["doc_title"] == "k8s-pod-troubleshooting.md"
        assert c["score"] == 0.83


class TestValidateCitations:
    def test_all_valid(self):
        cits = extract_citations("[1][2]", CHUNKS)
        kept, invalid = validate_citations(cits, CHUNKS)
        assert len(kept) == 2
        assert invalid == []

    def test_foreign_chunk_removed(self):
        """chunk_id 不属于本次检索集 → 剔除并记录（A3 代码层保证）。"""
        foreign = [
            {
                "chunk_id": "deadbeef-dead-dead-dead-deaddeadbeef",
                "doc_id": "x",
                "doc_title": "t",
                "snippet": "s",
                "score": 0.9,
                "source_type": "kb",
                "status": "live",
            }
        ]
        kept, invalid = validate_citations(foreign, CHUNKS)
        assert kept == []
        assert invalid == ["deadbeef-dead-dead-dead-deaddeadbeef"]


class TestBuildPrompt:
    def test_contains_chunks_and_question(self):
        prompt = build_prompt("怎么排查 CrashLoopBackOff？", CHUNKS, [])
        assert "[1]" in prompt and "[2]" in prompt
        assert "CrashLoopBackOff" in prompt
        assert "怎么排查" in prompt

    def test_contains_history(self):
        history = [
            {"role": "user", "content": "Pod 一直重启"},
            {"role": "assistant", "content": "先看 describe"},
        ]
        prompt = build_prompt("那它怎么配？", CHUNKS, history)
        assert "Pod 一直重启" in prompt
        assert "先看 describe" in prompt

    def test_no_history_placeholder(self):
        prompt = build_prompt("问题", CHUNKS, [])
        assert "（无历史）" in prompt


class TestDecideAnswerType:
    """答案层拒答判定（真实标定后引入的第二道闸门）。"""

    def test_no_chunks_is_refusal(self):
        assert decide_answer_type("任何文本", [], has_chunks=False) == "refusal"

    def test_no_citations_is_refusal(self):
        """有检索片段但答案无有效引用 → 拒答。

        真实案例：Redis 问题检索到 0.47 的噪声片段，模型说"依据不足"，
        早期实现标 answered + 0 引用，A4 判分失败且误导用户。
        """
        assert decide_answer_type("这是回答。", [], has_chunks=True) == "refusal"

    def test_insufficient_evidence_marker_is_refusal(self):
        answer = "依据提供的知识库片段，未包含关于 Redis 集群脑裂的描述。因此，无法依据知识库片段回答该问题。依据不足。"
        assert decide_answer_type(answer, [], has_chunks=True) == "refusal"

    def test_marker_with_citations_still_refusal(self):
        """即使模型挂了引用，只要明说依据不足仍判拒答。"""
        answer = "片段未包含该内容[1]，依据不足。"
        assert decide_answer_type(answer, [{"chunk_id": "x"}], has_chunks=True) == "refusal"

    def test_answered_when_citations_present(self):
        assert decide_answer_type("CrashLoopBackOff 先看 describe[1]。", [{"chunk_id": "x"}], has_chunks=True) == "answered"


# NOTE(S6)：编排类测试（引用清空 / 拒答分支）已迁移至 tests/test_graph.py，
# 因为编排统一走 app.graph.run_turn（LangGraph 全图）。本文件只测被节点复用的纯工具函数。
