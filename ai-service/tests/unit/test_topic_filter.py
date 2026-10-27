"""主题一致性过滤器的单元测试（V1.1 SPIKE 产物）。

**为什么这些用例值得留**：`topic_filter` 的三版调参过程里，
前两版都出现了「锚点提取错 → 误杀正确 chunk」的缺陷（Recall 0.9259→0.7407）。
这些用例把当时的失败形态固化下来，防止回归。

实证来源：`docs/testing/spike-report-v11-critic-phase2.md` §2.4
"""

from __future__ import annotations

import pytest

from app.topic_filter import (
    extract_anchors,
    filter_by_topic,
    score_topic_consistency,
)


def _chunk(content: str, heading: str = "", score: float = 0.5, title: str = "doc") -> dict:
    return {
        "chunk_id": f"c-{abs(hash(content)) % 10000}",
        "doc_id": "d1",
        "doc_title": title,
        "score": score,
        "content": content,
        "heading_path": heading,
    }


class TestAnchorExtraction:
    """锚点提取：优先级错了会误杀（v1 版本的真实缺陷）。"""

    def test_error_code_extracted(self):
        """错误码/错误短语必须被提取 —— v1 版本漏掉它，导致 q002 等 5 题被误杀。"""
        a = extract_anchors("Exit Code 137 一般代表什么？")
        assert "exit code 137" in a

    def test_error_phrase_extracted(self):
        a = extract_anchors("Connection refused 和 Connection timed out 有什么区别？")
        assert "connection refused" in a
        assert "connection timed out" in a

    def test_interrogative_not_an_anchor(self):
        """疑问短语绝不能当锚点 —— v1 把 `一般代表` / `有什么区` 当锚点，是误杀根因。"""
        for q in [
            "Exit Code 137 一般代表什么？",
            "Connection refused 和 timed out 有什么区别？",
            "这是什么问题？",
            "怎么定位？",
        ]:
            anchors = extract_anchors(q)
            for bad in ("一般代表", "有什么区", "这是什么", "怎么定位"):
                assert bad not in anchors, f"{q} 提取到了疑问短语锚点 {bad}"

    def test_tech_identifier_extracted(self):
        a = extract_anchors("livenessProbe 和 readinessProbe 应该怎么配置？")
        assert any("livenessprobe" in x for x in a)
        assert any("readinessprobe" in x for x in a)

    def test_domain_synonym_expansion(self):
        """"探针" 应展开成同义项集合（含英文 probe 系列）。"""
        a = extract_anchors("Pod 的探针怎么配？")
        assert "探针" in a
        assert any("liveness" in x for x in a)
        assert any("startup" in x for x in a)

    def test_empty_query_returns_empty(self):
        assert extract_anchors("") == []
        assert extract_anchors("   ") == []


class TestTopicScoring:
    """打分：位置权重与强信号豁免。"""

    def test_heading_hit_strongest(self):
        c = _chunk("无关内容", heading="探针配置 livenessProbe")
        sc = score_topic_consistency(c, ["探针"])
        assert sc["score"] >= 1.0
        assert "探针" in sc["heading_hits"]

    def test_strong_signal_exempt_from_position(self):
        """**关键回归**：`Exit Code 137` 命中在正文后段也必须算强相关。

        v2 版本按位置一律给 0.3 权重 → 该 chunk 被剔除 → q002 误杀。
        """
        c = _chunk(
            "容器的内存上限由 resources.limits.memory 决定。"
            "超过该值会被内核 OOM Killer 终止，表现为 Exit Code 137。"
        )
        sc = score_topic_consistency(c, ["exit code 137"])
        assert sc["score"] >= 1.0, f"强信号被位置权重压制：{sc}"
        assert sc["strong"] is True

    def test_no_anchor_match_scores_zero(self):
        c = _chunk("网卡状态用 ip link 查看", heading="Linux 网络")
        sc = score_topic_consistency(c, ["探针"])
        assert sc["score"] == 0.0

    def test_tail_mention_only_weak(self):
        """非强信号锚点只在正文后段出现 → 弱权重（可能只是顺带提及）。"""
        c = _chunk("前面大段无关内容。" * 20 + "另外顺带提一句探针。")
        sc = score_topic_consistency(c, ["探针"])
        assert sc["score"] < 1.0
        assert sc["strong"] is False


class TestFilter:
    """过滤：剔除污染 + 兜底不饿死答案。"""

    def test_removes_off_topic_chunk(self):
        """v3 实测场景：探针 query 下，Linux 网络 chunk 应被剔除。"""
        chunks = [
            _chunk("常见错误是把耗时的启动检查放进 livenessProbe…启动慢的应用应使用 startupProbe 保护。",
                   heading="Kubernetes Pod 排查 > 探针", score=0.78, title="k8s"),
            _chunk("用 ip link 查看网卡状态，state DOWN 说明链路层未就绪。",
                   heading="Linux 网络故障排查", score=0.45, title="linux-net"),
        ]
        kept, diag = filter_by_topic(chunks, "如何配置 Pod 的探针？")
        titles = [c["doc_title"] for c in kept]
        assert "k8s" in titles
        assert "linux-net" not in titles
        assert diag["reason"] == "ok"
        assert len(diag["removed"]) == 1

    def test_fallback_keeps_top_when_all_removed(self):
        """全部被剔除时必须兜底保留最高分 —— 空的检索集会让下游退化成拒答。"""
        chunks = [
            _chunk("完全无关内容 A", score=0.4, title="a"),
            _chunk("完全无关内容 B", score=0.3, title="b"),
        ]
        kept, diag = filter_by_topic(chunks, "如何配置 Pod 的探针？")
        assert len(kept) == 1
        assert kept[0]["doc_title"] == "a"     # 最高分
        assert diag["reason"] == "fallback_kept_top"

    def test_no_anchors_means_no_filtering(self):
        """提不出锚点时不过滤（不过滤比误杀安全）。"""
        chunks = [_chunk("内容A"), _chunk("内容B")]
        kept, diag = filter_by_topic(chunks, "")
        assert len(kept) == len(chunks)
        assert diag["reason"] == "no_anchors"

    def test_empty_chunks(self):
        kept, diag = filter_by_topic([], "探针怎么配")
        assert kept == []
        assert diag["reason"] == "empty_chunks"

    def test_threshold_respected(self):
        """阈值语义：heading 命中得 3.0 分，阈值超过它就应被剔除（走兜底保留）。

        注意 `探针` 的实际分数是 3.3（heading 3.0 + 正文前段 0.3 系加权后取整），
        所以要真正触发剔除，阈值必须 > 3.3。
        """
        chunks = [_chunk("正文提到探针", heading="探针配置", score=0.8, title="t")]
        sc = score_topic_consistency(chunks[0], extract_anchors("探针"))
        assert sc["score"] > 3.0, f"预期 heading+body 命中 > 3.0，实得 {sc['score']}"

        kept_low, diag_low = filter_by_topic(chunks, "探针", min_consistency=1.0)
        assert len(kept_low) == 1
        assert diag_low["reason"] == "ok"

        # 阈值高于实际分数 → 全部剔除 → 兜底
        kept_high, diag_high = filter_by_topic(
            chunks, "探针", min_consistency=sc["score"] + 0.1
        )
        assert len(kept_high) == 1                    # 兜底保留
        assert diag_high["reason"] == "fallback_kept_top"


class TestInvariants:
    """不变量：过滤器绝不能放大检索集。"""

    @pytest.mark.parametrize(
        "query",
        ["探针怎么配", "Exit Code 137", "nginx 502", "", "随便问问"],
    )
    def test_output_never_larger_than_input(self, query):
        chunks = [_chunk(f"内容{i}", score=0.5 - i * 0.01) for i in range(5)]
        kept, _ = filter_by_topic(chunks, query)
        assert len(kept) <= len(chunks)

    def test_original_score_preserved(self):
        """过滤不得篡改原检索分（只附 `_topic_score`）。"""
        chunks = [_chunk("探针配置说明", heading="探针", score=0.777, title="k")]
        kept, _ = filter_by_topic(chunks, "探针")
        assert kept[0]["score"] == 0.777
        assert "_topic_score" in kept[0]


