"""S6 LangGraph 全图的单元测试（mock LLM，不依赖网络/DB）。

覆盖：
- 图结构与条件边分流（smalltalk / refuse / kb_qa / troubleshooting）
- router 规则层命中 → LLM 调用数 0（A5 硬要求）
- FR-17 rewrite 三重条件 + **首轮不复写**
- evidence 规则层裁剪无效引用
- final 直通条件（supported + 非降级 → 不调模型）
- 预算：reserved_for_final 保证 final 恒被调用（A6）；preempted 不含 final
- 轨迹：每节点一行（R-1）
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from app.graph.budget import MAX_LLM_CALLS, Budget  # noqa: E402
from app.graph.runner import run_turn  # noqa: E402

CFG = {
    "retrieval_top_k": 5,
    "retrieval_min_score": 0.35,
    "llm_model_router": "qwen-flash",
    "llm_model_qa": "qwen-plus",
    "llm_model_troubleshoot": "qwen-plus",
    "llm_model_checker": "qwen-plus",
    "llm_model_final": "qwen-plus",
    "answer_max_tokens": 256,
    "llm_timeout_seconds": 5,
    "bailian_base_url": "https://example.invalid",
    "dashscope_api_key": "sk-test",
    "embedding_model": "text-embedding-v4",
    "hnsw_ef_search": 100,
}

CHUNKS = [
    {
        "chunk_id": "11111111-1111-1111-1111-111111111111",
        "doc_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "doc_title": "k8s-pod-troubleshooting.md",
        "score": 0.83,
        "content": "CrashLoopBackOff 表示容器启动后反复退出。",
        "heading_path": "1. Pod 启动失败",
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


class CountingLLM:
    """记录调用次数与角色的假 LLM。"""

    def __init__(self, answers: dict[str, str] | None = None):
        self.calls: list[tuple[str, str]] = []
        self.answers = answers or {}

    def __call__(self, role: str, prompt: str) -> str:
        self.calls.append((role, prompt))
        if role in self.answers:
            return self.answers[role]
        if role == "final":
            return "CrashLoopBackOff 需先看 describe[1]。"
        if role == "troubleshoot":
            return "从日志看是连接被拒，建议先确认监听地址[1]。"
        if role == "router":
            return "kb_qa"
        if role == "checker":
            return "supported"
        if role == "rewrite":
            return "Kubernetes 的 livenessProbe 和 readinessProbe 应该怎么配置？"
        return "回答[1]"

    def roles(self) -> list[str]:
        return [r for r, _ in self.calls]


def patch_cfg(monkeypatch):
    monkeypatch.setattr("app.graph.runner.get_config", lambda: CFG)


def run(q: str, **kw):
    payload = {
        "question": q,
        "owner_id": "demo-user",
        "session_id": "s-1",
        "turn_index": kw.pop("turn_index", 0),
        "history": kw.pop("history", []),
    }
    if "attach_log" in kw:
        payload["attach_log"] = kw.pop("attach_log")
    return run_turn(payload, **kw)


# ---------------------------------------------------------------------------
# 图结构 / 条件边
# ---------------------------------------------------------------------------


class TestGraphRouting:
    def test_smalltalk_zero_retrieval_zero_llm(self, monkeypatch):
        """寒暄：零检索、零生成（PRD §7.1 第 2 档）。"""
        patch_cfg(monkeypatch)
        llm = CountingLLM()

        def no_retrieve(owner, query, cfg):
            raise AssertionError("smalltalk 不应触发检索")

        r = run("你好", llm_call=llm, retrieve_fn=no_retrieve)
        assert r["route"] == "smalltalk"
        assert r["answer_type"] == "smalltalk"
        assert r["usage"]["llm_calls"] == 0
        assert r["node_path"] == ["router", "smalltalk"]

    def test_out_of_scope_zero_retrieval(self, monkeypatch):
        patch_cfg(monkeypatch)
        llm = CountingLLM()

        def no_retrieve(owner, query, cfg):
            raise AssertionError("out_of_scope 不应触发检索")

        r = run("帮我写一段 Python 爬虫代码", llm_call=llm, retrieve_fn=no_retrieve)
        assert r["route"] == "out_of_scope"
        assert r["answer_type"] == "out_of_scope"
        assert r["usage"]["llm_calls"] == 0

    def test_kb_qa_path(self, monkeypatch):
        patch_cfg(monkeypatch)
        llm = CountingLLM()
        r = run(
            "Pod 一直 CrashLoopBackOff 应该怎么排查？",
            llm_call=llm,
            retrieve_fn=lambda o, q, c: CHUNKS,
        )
        assert r["route"] == "kb_qa"
        assert "retrieval" in r["node_path"]
        assert "final" in r["node_path"]
        assert "refuse" not in r["node_path"]

    def test_refuse_path_when_no_chunks(self, monkeypatch):
        """检索无结果 → refuse 节点，不调生成（FR-15）。"""
        patch_cfg(monkeypatch)
        llm = CountingLLM()
        r = run("Redis 集群脑裂怎么处理？", llm_call=llm, retrieve_fn=lambda o, q, c: [])
        assert r["answer_type"] == "refusal"
        assert r["node_path"] == ["router", "retrieval", "refuse"]
        assert r["usage"]["llm_calls"] == 0

    def test_refuse_path_when_low_score(self, monkeypatch):
        patch_cfg(monkeypatch)
        llm = CountingLLM()
        low = [dict(CHUNKS[0], score=0.10)]
        r = run("Redis 怎么配？", llm_call=llm, retrieve_fn=lambda o, q, c: low)
        assert r["answer_type"] == "refusal"
        assert "最高分片段" in r["final_text"]
        assert r["usage"]["llm_calls"] == 0

    def test_troubleshooting_path_with_log(self, monkeypatch):
        patch_cfg(monkeypatch)
        llm = CountingLLM()
        r = run(
            "日志报 error dial tcp 10.0.0.1:3306 connection refused，怎么处理？",
            llm_call=llm,
            retrieve_fn=lambda o, q, c: CHUNKS,
        )
        assert r["route"] == "troubleshooting"
        assert "troubleshoot" in r["node_path"]


# ---------------------------------------------------------------------------
# A5：规则层零调用
# ---------------------------------------------------------------------------


class TestRouterRulesZeroLLM:
    @pytest.mark.parametrize(
        "q,expected",
        [
            ("你好", "smalltalk"),
            ("谢谢", "smalltalk"),
            ("你是谁", "smalltalk"),
            # B3 小项：复合闲聊必须走 smalltalk（前缀匹配），不得漏到 kb_qa
            ("你好，你是谁？", "smalltalk"),
            ("你好，你能做什么？", "smalltalk"),
            ("在吗？我想问下 Nginx", "smalltalk"),
            ("帮我翻译这段话", "out_of_scope"),
            ("讲个笑话", "out_of_scope"),
            ("Pod CrashLoopBackOff 怎么排查？", "kb_qa"),
            ("Nginx 502 是什么原因", "kb_qa"),
            ("日志里出现 exception 怎么定位", "troubleshooting"),
            ("服务报 error 起不来", "troubleshooting"),
            ("MySQL explain 怎么看", "kb_qa"),
        ],
    )
    def test_rule_hit_no_llm_call(self, monkeypatch, q, expected):
        """规则命中 → route 正确且 LLM 调用数 0。"""
        patch_cfg(monkeypatch)
        llm = CountingLLM()
        r = run(q, llm_call=llm, retrieve_fn=lambda o, qq, c: CHUNKS)
        assert r["route"] == expected, f"{q!r} 期望 {expected}，实际 {r['route']}"
        # smalltalk/out_of_scope 完全不应调模型；其余也应规则命中
        assert "router" not in llm.roles(), f"{q!r} 不该调 router 模型"


# ---------------------------------------------------------------------------
# FR-17：rewrite 三重条件 + 首轮不复写
# ---------------------------------------------------------------------------


class TestQueryRewrite:
    def test_first_turn_never_rewrites(self, monkeypatch):
        """首轮不复写：即使命中指代词也不触发（FR-17 条件 1）。"""
        patch_cfg(monkeypatch)
        llm = CountingLLM()
        r = run(
            "那它怎么配？",
            llm_call=llm,
            retrieve_fn=lambda o, q, c: CHUNKS,
            turn_index=0,
            history=[{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}],
        )
        assert r["rewritten_query"] is None
        assert "rewrite" not in llm.roles()

    def test_rewrite_requires_anaphora(self, monkeypatch):
        """非首轮但无指代词 → 不触发（条件 2）。"""
        patch_cfg(monkeypatch)
        llm = CountingLLM()
        r = run(
            "livenessProbe 怎么配？",
            llm_call=llm,
            retrieve_fn=lambda o, q, c: CHUNKS,
            turn_index=1,
            history=[{"role": "assistant", "content": "先说探针"}],
        )
        assert r["rewritten_query"] is None
        assert "rewrite" not in llm.roles()

    def test_rewrite_requires_assistant_history(self, monkeypatch):
        """非首轮 + 有指代词 但无 assistant 消息 → 不触发（条件 3）。"""
        patch_cfg(monkeypatch)
        llm = CountingLLM()
        r = run(
            "那它怎么配？",
            llm_call=llm,
            retrieve_fn=lambda o, q, c: CHUNKS,
            turn_index=2,
            history=[{"role": "user", "content": "只有用户消息"}],
        )
        assert r["rewritten_query"] is None
        assert "rewrite" not in llm.roles()

    def test_rewrite_triggers_when_all_three(self, monkeypatch):
        """三重条件全满足 → 触发改写。"""
        patch_cfg(monkeypatch)
        llm = CountingLLM()
        r = run(
            "那它怎么配？",
            llm_call=llm,
            retrieve_fn=lambda o, q, c: CHUNKS,
            turn_index=1,
            history=[{"role": "assistant", "content": "先说探针配置"}],
        )
        assert r["rewritten_query"] is not None
        assert "rewrite" in llm.roles()


# ---------------------------------------------------------------------------
# evidence 规则层裁剪
# ---------------------------------------------------------------------------


class TestEvidenceRuleLayer:
    def test_invalid_citations_removed(self, monkeypatch):
        """引用不属于本次检索集 → 被裁剪（FR-22 / A3 代码层保证）。"""
        patch_cfg(monkeypatch)
        llm = CountingLLM(answers={"final": "结论甲[1]，结论乙[9]。"})
        r = run("CrashLoopBackOff 怎么排查？", llm_call=llm, retrieve_fn=lambda o, q, c: CHUNKS)
        ids = {c["chunk_id"] for c in r["citations"]}
        assert ids <= {CHUNKS[0]["chunk_id"], CHUNKS[1]["chunk_id"]}
        assert "deadbeef" not in str(ids)


class TestRefusalClearsCitations:
    """FR-18③：明确拒答时禁止伪造引用（拒答却挂引用会误导用户）。

    真实案例 m002/m005：答案说"依据不足"但挂了 [1][2] 这类"引用式否定"，
    早期实现会原样落库。
    """

    def test_refusal_drops_all_citations(self, monkeypatch):
        patch_cfg(monkeypatch)
        llm = CountingLLM(answers={"final": "知识库未提供该数值[1]。依据不足，无法回答。"})
        r = run("那默认值是多少？", llm_call=llm, retrieve_fn=lambda o, q, c: CHUNKS)
        assert r["answer_type"] == "refusal"
        assert r["citations"] == [], "拒答不得携带引用（FR-18③）"


class TestRefusalNoTroubleshootSteps:
    """拒答不得输出具体排障步骤（PRD §7.4 / FR-20）。"""

    def test_low_score_refusal_has_no_steps(self, monkeypatch):
        patch_cfg(monkeypatch)
        llm = CountingLLM()
        low = [dict(CHUNKS[0], score=0.08)]
        r = run("公司差旅报销流程是什么？", llm_call=llm, retrieve_fn=lambda o, q, c: low)
        assert r["answer_type"] == "refusal"
        text = r["final_text"]
        for marker in ("kubectl", "第一步", "排查顺序", "```"):
            assert marker not in text, f"拒答文案出现了排障步骤特征：{marker}"


class TestEvidenceSkipSemantics:
    """B2 回归：kb_qa 主路径无 draft → evidence 必须输出 skip/None，**不得输出 False**。

    根因：早期 evidence 在无 draft 时输出 `supported=False`，这个假阴性流进 final 的
    prompt 后模型会据此拒答 —— 即"检索到好证据却拒答"（演示事故级）。
    """

    def test_no_draft_yields_skip_not_false(self):
        from app.graph.evidence import evidence_node

        out = evidence_node({"chunks": CHUNKS, "draft_answer": "", "citations": []}, budget=Budget())
        check = out["check_result"]
        assert check["method"] == "skip", f"无 draft 应为 skip，实际 {check['method']}"
        assert check["supported"] is None, "无 draft 时 supported 必须是 None，不能是 False"

    def test_no_draft_does_not_call_checker(self):
        calls = []

        def llm(role, prompt):
            calls.append(role)
            return "supported"

        from app.graph.evidence import evidence_node

        evidence_node({"chunks": CHUNKS, "draft_answer": "", "citations": []},
                      budget=Budget(), llm_call=llm)
        assert calls == [], "无 draft 时不应调 checker"

    def test_with_draft_keeps_rule_semantics(self):
        from app.graph.evidence import evidence_node

        cit = [dict(CHUNKS[0], snippet="x", source_type="kb", status="live")]
        cit[0]["chunk_id"] = CHUNKS[0]["chunk_id"]
        out = evidence_node({"chunks": CHUNKS, "draft_answer": "看这里[1]", "citations": cit},
                            budget=Budget())
        assert out["check_result"]["method"] in ("rule", "llm")
        assert out["check_result"]["supported"] is True


class TestAnsweredStability:
    """B2 回归：有检索片段时，kb_qa 主路径必须稳定 answered（5 篇语料各一问）。

    且 answered 必须**必然带引用** —— 模型漏标 `[n]` 时由 final 兜底归因到最高分片段。
    """

    # 覆盖 5 篇语料
    CASES = [
        ("Pod CrashLoopBackOff 应该怎么排查？", "11111111-1111-1111-1111-111111111111"),
        ("Nginx 502 是什么原因？", "22222222-2222-2222-2222-222222222222"),
        ("MySQL 慢查询怎么定位？", "33333333-3333-3333-3333-333333333333"),
        ("Linux 网络不通怎么排查？", "44444444-4444-4444-4444-444444444444"),
        ("日志级别应该怎么用？", "55555555-5555-5555-5555-555555555555"),
    ]

    @pytest.mark.parametrize("question,chunk_id", CASES)
    def test_answered_when_chunks_present(self, monkeypatch, question, chunk_id):
        patch_cfg(monkeypatch)
        # 模型给出带引用的正常回答
        llm = CountingLLM(answers={"final": "根据文档，应按顺序排查[1]。"})
        chunks = [
            {
                "chunk_id": chunk_id,
                "doc_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "doc_title": "doc.md",
                "score": 0.72,
                "content": "相关排查内容。",
                "heading_path": "1",
            }
        ]
        r = run(question, llm_call=llm, retrieve_fn=lambda o, q, c: chunks)
        assert r["answer_type"] == "answered", (
            f"{question!r} 有片段却判 {r['answer_type']}（citations={len(r['citations'])}）"
        )
        assert r["citations"], "answered 必须带引用"

    def test_anaphora_question_with_good_chunks_is_answered(self, monkeypatch):
        """B2 的真实形态：含指代 + 有片段 + rewrite 成功 → 必须 answered。"""
        patch_cfg(monkeypatch)
        llm = CountingLLM(answers={"final": "探针应这样配置：livenessProbe 检查存活[1]。"})
        r = run(
            "那它怎么配？",
            llm_call=llm,
            retrieve_fn=lambda o, q, c: CHUNKS,
            turn_index=1,
            history=[{"role": "assistant", "content": "先看 describe"}],
        )
        assert r["rewritten_query"] is not None
        assert r["answer_type"] == "answered", f"实际 {r['answer_type']}"
        assert r["citations"]

    @pytest.mark.parametrize(
        "answer",
        [
            "模型这次忘了标注引用，但内容是有依据的。",   # 漏标 [n]
            "答案里引用了不存在的编号[9]。",              # 编造编号
        ],
    )
    def test_answered_always_has_citations(self, monkeypatch, answer):
        """B2 兜底归因：answered 时引用必须非空（否则前端无法展开、A3 失败）。"""
        patch_cfg(monkeypatch)
        llm = CountingLLM(answers={"final": answer})
        r = run("Pod 怎么排查？", llm_call=llm, retrieve_fn=lambda o, q, c: CHUNKS)
        assert r["answer_type"] == "answered"
        assert r["citations"], f"answered 却无引用：{answer!r}"
        # 归因对象必须是本次检索集内的切片（A3 硬断言）
        assert r["citations"][0]["chunk_id"] in {c["chunk_id"] for c in CHUNKS}


class TestPromptHardConstraint:
    """final 的 prompt 只应消费 invalid_citations 硬约束，不消费 supported 布尔。"""

    def test_prompt_does_not_leak_supported_false(self, monkeypatch):
        patch_cfg(monkeypatch)
        seen = {}

        def llm(role, prompt):
            if role == "final":
                seen["prompt"] = prompt
            return "答案[1]"

        run("Pod 怎么排查？", llm_call=llm, retrieve_fn=lambda o, q, c: CHUNKS)
        prompt = seen.get("prompt", "")
        assert "supported=False" not in prompt
        assert "kept=0" not in prompt
        # 正向口径必须在
        assert "优先依据" in prompt or "基于它作答" in prompt


# ---------------------------------------------------------------------------
# S7：工具接线
# ---------------------------------------------------------------------------


class TestToolWiring:
    """S7：troubleshoot 接真工具，且工具证据送达生成环节。"""

    def test_tools_actually_invoked_for_log_question(self, monkeypatch):
        """带日志提问 → analyze_error_log 真的被调用（tool_executor 接上）。"""
        patch_cfg(monkeypatch)
        from app.tools import ToolExecutor

        ex = ToolExecutor()
        llm = CountingLLM()
        r = run(
            "服务起不来，日志如下，帮我分析",
            llm_call=llm,
            tool_executor=ex,
            retrieve_fn=lambda o, q, c: CHUNKS,
            attach_log="2026-10-23 10:00:01 ERROR dial tcp: connection refused",
        )
        names = [c["tool_name"] for c in ex.calls]
        assert "analyze_error_log" in names, f"工具未调用: {names}"
        assert r["route"] == "troubleshooting"

    def test_tool_evidence_reaches_prompt(self, monkeypatch):
        """工具证据必须出现在 troubleshoot 的 prompt 里（否则分析被丢弃）。"""
        patch_cfg(monkeypatch)
        seen: dict[str, str] = {}

        def llm(role, prompt):
            if role == "troubleshoot":
                seen["prompt"] = prompt
            return "诊断结论[1]"

        run(
            "服务起不来，日志如下，帮我分析",
            llm_call=llm,
            retrieve_fn=lambda o, q, c: CHUNKS,
            attach_log="2026-10-23 ERROR connection refused",
        )
        prompt = seen.get("prompt", "")
        assert "工具证据" in prompt, "工具证据未注入 prompt"
        assert "analyze_error_log" in prompt

    def test_tool_records_returned_for_persistence(self, monkeypatch):
        """工具审计行随返回值传出（Go 落 tool_calls 表）。"""
        patch_cfg(monkeypatch)
        r = run(
            "服务起不来，日志如下",
            llm_call=CountingLLM(),
            retrieve_fn=lambda o, q, c: CHUNKS,
            attach_log="2026-10-23 ERROR connection refused",
        )
        records = r.get("tool_call_records") or []
        assert records, "未返回工具审计行"
        rec = records[0]
        # 只含摘要，不含原始入参（NFR-S5）
        assert "log_text" not in rec
        assert rec["args_digest"].startswith("sha256:")

    def test_non_whitelisted_tool_does_not_crash_turn(self, monkeypatch):
        """非白名单工具被拒时不中断本轮（走 invoke_safe 语义）。"""
        patch_cfg(monkeypatch)
        from app.tools import ToolExecutor

        ex = ToolExecutor()
        out = ex.invoke_safe("read_log_tail", {"path": "/var/log/syslog"})
        assert out["status"] == "rejected"
        assert out["degraded"] is True


# ---------------------------------------------------------------------------
# 预算（A6）
# ---------------------------------------------------------------------------


class TestBudget:
    def test_reserved_for_final_keeps_final_reachable(self, monkeypatch):
        """A6：额度被前序节点耗尽后，final 仍能获取额度（reserved_for_final）。"""
        patch_cfg(monkeypatch)
        llm = CountingLLM()
        # 人为把 LLM 额度用掉，仅剩预留份额
        b = Budget()
        for _ in range(MAX_LLM_CALLS - 1):
            b.consume_llm()
        assert b.available_llm() == 0
        # final 仍可获取
        assert b.try_acquire("final", is_final=True) is True

    def test_preempted_excludes_final(self, monkeypatch):
        patch_cfg(monkeypatch)
        llm = CountingLLM()
        r = run("Pod 重启怎么排查？", llm_call=llm, retrieve_fn=lambda o, q, c: CHUNKS)
        assert "final" not in r["preempted"]

    def test_budget_exhausted_marks_preempted(self):
        b = Budget()
        for _ in range(MAX_LLM_CALLS - 1):
            b.consume_llm()
        ok = b.try_acquire("checker")
        assert ok is False
        assert "checker" in b.preempted


# ---------------------------------------------------------------------------
# 轨迹（A8 / R-1）
# ---------------------------------------------------------------------------


class TestTrace:
    def test_every_node_one_step_row(self, monkeypatch):
        patch_cfg(monkeypatch)
        llm = CountingLLM()
        r = run("Pod CrashLoopBackOff 怎么排查？", llm_call=llm, retrieve_fn=lambda o, q, c: CHUNKS)
        nodes = [s["node"] for s in r["steps"]]
        assert nodes == r["node_path"]
        for s in r["steps"]:
            assert "duration_ms" in s
            assert "input_digest" in s
            assert "output_summary" in s

    def test_node_path_reconstructable(self, monkeypatch):
        """A8：可从 node_path + steps 还原节点顺序与耗时。"""
        patch_cfg(monkeypatch)
        llm = CountingLLM()
        r = run("Pod CrashLoopBackOff 怎么排查？", llm_call=llm, retrieve_fn=lambda o, q, c: CHUNKS)
        assert r["node_path"][0] == "router"
        assert r["node_path"][-1] == "final"
        total = sum(s["duration_ms"] for s in r["steps"])
        assert total <= r["duration_ms"] + 50  # 步骤耗时之和不超过总耗时（含余量）


# ---------------------------------------------------------------------------
# 事件回调（流式共用入口）
# ---------------------------------------------------------------------------


class TestEmit:
    def test_emit_receives_meta_citation_done(self, monkeypatch):
        patch_cfg(monkeypatch)
        llm = CountingLLM()
        events = []
        run(
            "Pod CrashLoopBackOff 怎么排查？",
            llm_call=llm,
            retrieve_fn=lambda o, q, c: CHUNKS,
            emit=events.append,
        )
        types = [e["type"] for e in events]
        assert types[0] == "meta"
        assert "citation" in types
        assert types[-1] == "done"