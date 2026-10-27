"""主集边界哨兵：q017（跨文档）、q029（阈值卡线）、参数过拟合守卫。

**数据来自真实检索快照**，不是手写。

为什么必须用快照（一次真实教训）：
首版这些测试里我凭记忆手写了 chunk 内容与"预期分数"，结果 q029 写成"应在 1.0~1.5"，
实测却是**恰好 1.00** —— 把"卡在阈值上"这个最关键的事实写丢了。
**哨兵若不用真数据，测的是想象。**

快照生成：`scripts/spike_v11/s7_q017_q029_snapshot.py`
快照文件：`docs/testing/q017_q029_snapshot.json`
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.topic_filter import extract_anchors, filter_by_topic, score_topic_consistency

SNAPSHOT = (
    Path(__file__).resolve().parents[3] / "docs" / "testing" / "q017_q029_snapshot.json"
)


def _load(qid: str) -> dict:
    if not SNAPSHOT.exists():
        pytest.skip(
            f"缺少快照 {SNAPSHOT}；先跑 scripts/spike_v11/s7_q017_q029_snapshot.py"
            "（需真实语料与可用模型）"
        )
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))[qid]


def _as_chunks(rows: list[dict]) -> list[dict]:
    """把快照行还原成 chunk 结构（字段与 RetrievedChunk 对齐）。"""
    return [
        {
            "chunk_id": f"chunk-{i}",
            "doc_id": f"doc-{i}",
            "doc_title": r["doc_title"],
            "score": r["score"],
            "content": r["content"],
            "heading_path": r.get("heading_path", ""),
        }
        for i, r in enumerate(rows)
    ]


class TestQ017CrossDocSentinel:
    """**q017 永久回归哨兵**（外部裁决 4 指定）。

    q017 是**跨文档取证题**：`gold_doc_ids` 含
    `seed:nginx-502-upstream` + `seed:linux-network-troubleshooting` 两篇，
    要求"故意引第二份文档"作答 —— **全路径过滤最容易误伤的正是这类题**。
    主集均值 0.9259 没掉，**不代表这道题没被咬**。
    """

    def test_both_gold_docs_survive(self):
        """**核心断言**：两篇 gold 文档都必须保留 —— 少一篇就是误杀。"""
        snap = _load("q017")
        kept, diag = filter_by_topic(_as_chunks(snap["chunks"]), snap["question"])
        titles = {c["doc_title"] for c in kept}
        assert "nginx-502-upstream" in titles, (
            f"q017 误杀 nginx（跨文档题被咬）；removed={diag['removed']}；"
            f"anchors={diag['anchors']}"
        )
        assert "linux-network-troubleshooting" in titles, (
            f"q017 误杀 linux（跨文档题被咬）；removed={diag['removed']}；"
            f"anchors={diag['anchors']}"
        )

    def test_irrelevant_doc_still_removed(self):
        """反向：真无关文档仍须剔除（不能为保 q017 而放水）。"""
        snap = _load("q017")
        kept, diag = filter_by_topic(_as_chunks(snap["chunks"]), snap["question"])
        titles = {c["doc_title"] for c in kept}
        assert "mysql-slow-query" not in titles, (
            f"无关 mysql 未剔除，过滤失效；anchors={diag['anchors']}"
        )

    def test_anchors_cross_two_domains(self):
        """锚点必须同时命中"超时语义"与"上游/连接"两侧，否则必误杀一篇。"""
        a = extract_anchors(_load("q017")["question"])
        joined = " ".join(a)
        assert "timed out" in joined, f"缺 'timed out' 锚点：{a}"
        assert "upstream" in joined or "上游" in joined, f"缺 upstream 锚点：{a}"

    def test_near_threshold_gold_chunk(self):
        """q017 有一篇 gold 的 topic 分仅 **1.30**，离阈值 1.0 只有 0.3。

        这是本语料最脆弱的边界之一：任何让打分变严的改动都会先杀它。
        """
        snap = _load("q017")
        gold_rows = [r for r in snap["chunks"] if r["is_gold"]]
        near = [r for r in gold_rows if r["topic_score"] < 2.0]
        assert near, "预期存在贴近阈值的 gold chunk（原实测 1.30）"
        sc = score_topic_consistency(
            _as_chunks([near[0]])[0], extract_anchors(snap["question"])
        )
        assert sc["score"] >= 1.0, (
            f"q017 脆弱 gold chunk 分数跌到 {sc['score']}（原实测 {near[0]['topic_score']}），"
            f"已跌破阈值 1.0"
        )


class TestQ029ThresholdBoundary:
    """**q029 阈值边界哨兵**（外部裁决 2 的 FB4）。

    q029（`long_query_time 的单位是毫秒吗？`）的 gold chunk 实测 topic 分
    **恰好 1.00** —— **正卡在阈值上**。
    这是阈值扫描里最敏感的一点：分数算法严一点点，它就掉出去。
    """

    def test_gold_survives_at_1_0(self):
        """阈值 1.0 下必须保留（分数恰好 1.00，靠 `>=` 判过）。"""
        snap = _load("q029")
        gold_rows = [r for r in snap["chunks"] if r["is_gold"]]
        assert gold_rows, "快照里没有 gold chunk"
        kept, diag = filter_by_topic(
            _as_chunks(gold_rows), snap["question"], min_consistency=1.0
        )
        assert len(kept) == len(gold_rows), (
            f"q029 gold 在阈值 1.0 下被误杀；diag={diag}"
        )
        assert diag["reason"] == "ok"

    def test_gold_dies_when_threshold_raised_slightly(self):
        """阈值只要抬到 1.01 就误杀 —— **这就是 1.0 不能动的证明**。"""
        snap = _load("q029")
        gold_rows = [r for r in snap["chunks"] if r["is_gold"]]
        # 取分数最低的那条（恰好卡线的那条）
        weakest = min(
            gold_rows,
            key=lambda r: score_topic_consistency(
                _as_chunks([r])[0], extract_anchors(snap["question"])
            )["score"],
        )
        base = score_topic_consistency(
            _as_chunks([weakest])[0], extract_anchors(snap["question"])
        )["score"]
        _, diag = filter_by_topic(
            _as_chunks([weakest]), snap["question"], min_consistency=base + 0.01
        )
        assert diag["reason"] == "fallback_kept_top", (
            f"阈值 {base + 0.01:.2f} 下该 chunk 仍通过；打分逻辑可能变了；diag={diag}"
        )

    def test_score_pinned_at_boundary(self):
        """把边界钉死：该 gold chunk 分数必须 ≈1.00（原实测值）。

        它漂移 = 阈值 1.0 的适用性变了，必须重扫（s5b_threshold_scan）。
        """
        snap = _load("q029")
        gold_rows = [r for r in snap["chunks"] if r["is_gold"]]
        weakest = min(
            gold_rows,
            key=lambda r: score_topic_consistency(
                _as_chunks([r])[0], extract_anchors(snap["question"])
            )["score"],
        )
        assert abs(weakest["topic_score"] - 1.0) < 0.05, (
            f"q029 卡线 chunk 的快照分数是 {weakest['topic_score']}，不是 ≈1.00；"
            f"快照本身可能过期，请重跑 s7 生成"
        )
        sc = score_topic_consistency(
            _as_chunks([weakest])[0], extract_anchors(snap["question"])
        )
        assert abs(sc["score"] - 1.0) < 0.05, (
            f"q029 卡线 chunk 实时分数变成 {sc['score']}（快照 1.00）。"
            f"它正好卡在阈值上 —— 任何漂移都会让阈值 1.0 失效，须重扫"
        )


class TestOverfitGuard:
    """**参数过拟合的守卫**（ADR-0002 §6.2）—— 确保改参数无法悄悄发生。"""

    def test_default_threshold_is_1_0(self):
        from app.config import get_config

        assert get_config()["topic_filter_min_consistency"] == 1.0, (
            "默认阈值变了！须同步 ADR-0002 §6.2 / delta PRD 的已知限制，"
            "并重跑 s5b_threshold_scan 验证 Recall 未崩"
        )

    def test_filter_off_by_default(self):
        from app.config import get_config

        assert get_config()["topic_filter_enabled"] is False, (
            "默认必须 off —— 阈值过拟合本语料，换语料须重扫后才启用（ADR-0002 §6.2）"
        )

    def test_known_limitation_documented(self):
        """已知限制必须写进 ADR-0002（过拟合声明不能只存在于对话里）。"""
        adr = (
            Path(__file__).resolve().parents[3]
            / "docs" / "architecture" / "adr" / "0002-topic-filter-over-critic.md"
        )
        assert adr.exists(), f"缺 ADR-0002：{adr}"
        text = adr.read_text(encoding="utf-8")
        assert "过拟合" in text and "重扫阈值" in text, (
            "ADR-0002 缺少「参数过拟合本语料」的已知限制声明 —— "
            "这条不写，将来 Recall 崩了没人知道为什么"
        )