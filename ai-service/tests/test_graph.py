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


class TestStateSchemaCompleteness:
    """**State 必须声明节点会返回的每一个键**（否则被 LangGraph 静默丢弃）。

    S9 实测教训：`retrieval_node` return 了 `probe_chunk`，但当时只把它写进了
    `RetrievedChunk`，**没写进 `State`** —— TypedDict 按 schema 过滤节点返回值，
    于是 refuse 永远拿到 `None`，PRD §7.4 的"列出最高分片段"退化成"未找到"。

    这是"字段在写路径存在、传输路径丢失"的**第三次**（前两次：`retrieval_top_score`
    未落库、`Citation.Index` 未声明）。所以立一条**机制级**回归。
    """

    def test_every_returned_key_is_declared_in_state(self):
        import inspect

        from app.graph import state as st

        declared = set(st.State.__annotations__)
        # 节点允许返回的"内部键"白名单（以 _ 开头的是内部标记，已在 State 声明）
        required = {
            "rewritten_query", "chunks", "probe_chunk",      # retrieval
            "tool_calls", "draft_answer",                    # troubleshoot
            "citations", "final_text", "answer_type",        # final / refuse
            "route", "route_conf",                           # router
            "degraded", "degrade_reasons",                   # 通用
        }
        missing = required - declared
        assert not missing, f"这些键节点会 return 但 State 未声明（会被静默丢弃）: {sorted(missing)}"

    def test_retrieval_node_return_keys_are_declared(self):
        """直接检查 retrieval 节点的返回键集合 ⊆ State。"""
        from app.graph import state as st
        from app.graph.retrieval import retrieval_node
        from app.graph.budget import Budget

        out = retrieval_node(
            {"question": "q", "owner_id": "u", "route": "kb_qa", "turn_index": 0},
            cfg={"retrieval_top_k": 5, "retrieval_min_score": 0.35, "hnsw_ef_search": 100},
            budget=Budget(),
            retrieve_fn=lambda o, q, c: [],
        )
        undeclared = set(out) - set(st.State.__annotations__)
        assert not undeclared, f"retrieval_node 返回了未声明键: {sorted(undeclared)}"


class TestRefuseListsProbeChunk:
    """PRD §7.4 硬要求：拒答必须列出"**最高分片段及分数**"。

    ⚠️ 为什么需要这组用例（S9 实测教训）：
    `test_refuse_path_when_low_score` 传的是**已经过阈值过滤的低分 chunks**
    （`retrieve_fn` 直接给 `[dict(CHUNK, score=0.10)]`）—— 它验证的是 refuse 节点
    的**渲染逻辑**，却绕过了真实装配：真实 `retrieve()` 在低分时返回**空列表**，
    refuse 于是走 `not chunks` 分支、拿不到任何片段。

    实测：`公司差旅报销流程是什么？` 走 `chunks=0` → 输出干巴巴的"未找到"，
    §7.4 要求的"最高分片段"根本没出现。**函数对、装配错**（与前几轮同型）。
    """

    def test_real_retrieve_probe_reaches_refuse(self, monkeypatch):
        """装配级：retrieve 走 **probe** 路径（低分样本被过滤，但探针带回）。"""
        patch_cfg(monkeypatch)
        from app.graph.final import refuse_node

        low = dict(CHUNKS[0], score=0.10)   # 低于 min_score

        def fake_retrieve(owner, query, cfg, *, probe=False):
            # 模拟真实 retrieve：过阈值的为空；探针带回阈值下的最高分
            return ([], low) if probe else ([],)

        r = refuse_node(
            {"question": "公司差旅报销流程是什么？", "chunks": [],
             "probe_chunk": low, "history": []},
        )
        assert r["answer_type"] == "refusal"
        assert "最高分片段" in r["final_text"], r["final_text"]
        assert "0.1" in r["final_text"], "应显示探针分数"
        assert low["doc_title"] in r["final_text"], "应显示来源文档"

    def test_probe_absent_falls_back_to_plain_refusal(self):
        """探针为 None（知识库真的空）→ 回落到无片段的拒答文案，不报错。"""
        from app.graph.final import refuse_node

        r = refuse_node({"question": "公司差旅报销流程是什么？", "chunks": [],
                         "probe_chunk": None, "history": []})
        assert r["answer_type"] == "refusal"
        assert "未在知识库中找到" in r["final_text"]

    def test_retrieve_probe_returns_highest_below_threshold(self, monkeypatch):
        """纯函数：`retrieve(probe=True)` 带回阈值下的最高分（降序第一条）。"""
        import app.retrieval as ret

        rows = [
            {"chunk_id": "a", "doc_id": "d", "doc_title": "t", "content": "c",
             "heading_path": "", "score": 0.30},   # 低于阈值 0.35 → 探针
            {"chunk_id": "b", "doc_id": "d", "doc_title": "t", "content": "c",
             "heading_path": "", "score": 0.10},
        ]
        monkeypatch.setattr(ret, "embed_query", lambda q, cfg: [0.0] * 4)

        class _Cur:
            def execute(self, *a, **k): pass
            def fetchall(self): return rows
            def __enter__(self): return self
            def __exit__(self, *a): return False

        class _Conn:
            def cursor(self): return _Cur()
            def __enter__(self): return self
            def __exit__(self, *a): return False

        monkeypatch.setattr(ret, "_build_conn", lambda: _Conn(), raising=False)
        monkeypatch.setattr("psycopg.connect", lambda *a, **k: _Conn(), raising=False)

        chunks, probe = ret.retrieve("u", "q", {"retrieval_top_k": 5,
                                                "retrieval_min_score": 0.35,
                                                "hnsw_ef_search": 100,
                                                "embedding_model": "m",
                                                "bailian_base_url": "http://x",
                                                "dashscope_api_key": "k"},
                                     probe=True)
        assert chunks == [], "过阈值的为空"
        assert probe is not None and probe["score"] == 0.3, f"探针应为最高分 0.30，实际 {probe}"


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
# D1：故障排查路由的证据门槛 + 零证据闸门
# ---------------------------------------------------------------------------


class TestD1EvidenceGate:
    """D1 回归（外部审查裁定）：无证据不得进排障、零证据不得自由生成。

    真实缺陷：`它起不来了怎么办？` 因裸短语"起不来"命中 troubleshooting 规则，
    节点在无 attach_log、无引用的情况下自由生成 → `answered` 且 0 引用，
    绕过了证据闸门。PRD §7.3 的触发条件是"日志/错误特征 或 显式诊断请求"，
    裸症状描述不在内；§7.4 第三行要求这种情况给 **clarify**（2~3 个候选、不猜）。
    """

    @pytest.mark.parametrize(
        "question,reason",
        [
            ("它起不来了怎么办？", "裸指代 + 弱症状，无任何证据"),
            ("这个报错怎么解决？", "裸指代，无证据"),
            ("服务起不来了", "裸症状，无证据"),
            ("页面打不开", "裸症状，无证据"),
        ],
    )
    def test_no_evidence_symptom_not_routed_to_ts(self, question, reason):
        """裸症状 + 无证据 → **不得**由规则层判为 troubleshooting。"""
        from app.graph.router import rule_route

        assert rule_route(question) != "troubleshooting", f"{question!r}（{reason}）应落到默认档"

    @pytest.mark.parametrize(
        "question",
        [
            "8080 起不来了怎么办？",     # PRD §7.3 原文：端口号 + 起不来
            "服务报 error 起不来",       # 强特征 + 弱特征
            "日志报 exception",         # 强特征
            "ERROR 1040 起不来",        # 错误码 + 弱特征
        ],
    )
    def test_evidence_still_routes_to_ts(self, question):
        """有证据时仍必须进排障（D1 修复不得误伤）。"""
        from app.graph.router import rule_route

        assert rule_route(question) == "troubleshooting", f"{question!r} 有证据应进排障"

    @pytest.mark.parametrize(
        "question",
        [
            "Pod CrashLoopBackOff 怎么排查？",   # 纯知识问答（A5 既有用例）
            "Nginx 502 是什么原因",
            "MySQL 索引失效的原因",
            "怎么配置 livenessProbe",
        ],
    )
    def test_pure_knowledge_not_misrouted_to_ts(self, question):
        """纯知识问答不得被误判为排障（"排查"二字不是排障证据）。"""
        from app.graph.router import rule_route

        assert rule_route(question) == "kb_qa", f"{question!r} 是知识问答"

    def test_zero_evidence_node_outputs_clarify_not_answered(self, monkeypatch):
        """**节点级**：零证据 → clarify，且**不调模型**（防自由生成）。"""
        patch_cfg(monkeypatch)
        llm = CountingLLM()
        r = run(
            "它起不来了怎么办？",
            llm_call=llm,
            retrieve_fn=lambda o, q, c: [],   # 检索无结果
        )
        assert r["answer_type"] == "clarify", f"零证据应 clarify，实际 {r['answer_type']}"
        assert r["citations"] == [], "澄清不得携带引用"
        assert "tool_calls" not in llm.roles(), f"零证据不该调模型：{llm.roles()}"

    def test_zero_evidence_ts_route_also_clarifies(self, monkeypatch):
        """即使被路由到 troubleshoot（如显式诊断请求但没给材料）→ 仍 clarify。"""
        patch_cfg(monkeypatch)
        from app.tools import ToolExecutor

        ex = ToolExecutor()
        r = run(
            "帮我看看这段日志",         # 显式诊断请求，但 attach_log 为空
            llm_call=CountingLLM(),
            tool_executor=ex,
            retrieve_fn=lambda o, q, c: [],
        )
        assert r["answer_type"] == "clarify", f"实际 {r['answer_type']}"
        assert r["citations"] == []

    def test_clarify_text_gives_candidates(self, monkeypatch):
        """§7.4 要求给 2~3 个候选（不是干巴巴的"无法回答"）。"""
        patch_cfg(monkeypatch)
        r = run("它起不来了怎么办？", llm_call=CountingLLM(), retrieve_fn=lambda o, q, c: [])
        text = r["final_text"]
        # 至少给出 3 条编号候选
        assert text.count("\n1.") >= 1 and "2." in text and "3." in text, (
            f"澄清文案应含候选清单，实际：{text[:200]}"
        )


# ---------------------------------------------------------------------------
# S7-A：router 消费 attach_log（招牌场景）
# ---------------------------------------------------------------------------


class TestRouterConsumesAttachLog:
    """S7-A 回归：附日志提问必须走 troubleshooting。

    真实缺陷：PRD §2.2-S2 的招牌场景"粘一段日志问『这是什么问题』"，问句本身
    不含错误特征（"帮我看看这段日志"），早期 router 只吃 question → 判 kb_qa
    → 最终拒答。**真实链路上这个场景是坏的。**
    """

    ERROR_LOG = (
        "2026-10-23 10:00:01 ERROR dial tcp 10.0.0.1:3306: connect: connection refused\n"
        "2026-10-23 10:00:02 ERROR failed to connect to mysql: connection refused"
    )

    @pytest.mark.parametrize(
        "question",
        ["帮我看看这段日志", "这段日志是什么问题", "日志如下，帮我分析", "这是什么问题"],
    )
    def test_attach_log_routes_to_troubleshooting(self, monkeypatch, question):
        patch_cfg(monkeypatch)
        r = run(question, llm_call=CountingLLM(), retrieve_fn=lambda o, q, c: CHUNKS,
                attach_log=self.ERROR_LOG)
        assert r["route"] == "troubleshooting", (
            f"{question!r} + 错误日志应为 troubleshooting，实际 {r['route']}"
        )
        assert "troubleshoot" in r["node_path"]

    def test_direct_rule_layer_consumes_attach_log(self):
        """规则层直测（不经过图）：attach_log 参与判定。"""
        from app.graph.router import rule_route

        # 附错误日志 → 排障
        assert rule_route("帮我看看这段日志", self.ERROR_LOG) == "troubleshooting"
        # 无 attach_log 但显式要求诊断 → 进排障节点（由节点闸门决定澄清/作答）
        # D1 修正：早期此处期望 kb_qa，但"说要看日志却没给日志"属**缺输入**，
        # 应进排障节点后由零证据闸门输出 clarify（PRD §7.4 第三行）。
        assert rule_route("帮我看看这段日志", None) == "troubleshooting"

    def test_normal_info_log_not_misrouted(self):
        """正常 INFO 日志不得被误判为故障排查。"""
        from app.graph.router import rule_route

        normal = "2026-10-23 10:00:01 INFO request completed in 12ms\n2026-10-23 10:00:02 INFO ok"
        assert rule_route("帮我看看这段日志", normal) == "kb_qa"

    @pytest.mark.parametrize(
        "log_text,expected",
        [
            ("Traceback (most recent call last):", "troubleshooting"),
            ("2026-01-01 10:00:00 FATAL cannot start", "troubleshooting"),
            ("nginx: [error] upstream closed", "troubleshooting"),
            ("数据库连接错误，请检查", "troubleshooting"),
            ("dial tcp: ECONNREFUSED", "troubleshooting"),
            ("service started successfully", "kb_qa"),
            ("所有测试通过，无异常", "kb_qa"),
            ("正常完成，没有失败", "kb_qa"),
            ("服务未报错", "kb_qa"),
        ],
    )
    def test_error_signal_detection(self, log_text, expected):
        """错误特征判定（含否定语境、errno 大小写）。"""
        from app.graph.router import rule_route

        assert rule_route("帮我看看", log_text) == expected

    def test_attach_log_scenario_end_to_end_answered(self, monkeypatch):
        """招牌场景端到端：附日志 + 模糊问句 → answered（不再拒答）。"""
        patch_cfg(monkeypatch)
        r = run("帮我看看这段日志", llm_call=CountingLLM(), retrieve_fn=lambda o, q, c: CHUNKS,
                attach_log=self.ERROR_LOG)
        assert r["answer_type"] == "answered", f"招牌场景被判 {r['answer_type']}"
        assert r["citations"]


# ---------------------------------------------------------------------------
# S7-B：端口提取保留同端口多条
# ---------------------------------------------------------------------------


class TestPortExtraction:
    """S7-B 回归：同端口多行必须保留多条（冲突的定义来源）。

    真实缺陷：早期 `_extract_ports` 用 `seen: set[int]` 去重，把"8080 nginx"与
    "8080 java"压成一条 → 工具判"无重复端口"→ **自信回答"未发现占用冲突"**。
    这不是简化，是给出错误结论（比不调用更糟）。
    """

    def test_two_lines_same_port_reports_conflict(self):
        from app.graph.troubleshoot import _extract_ports
        from app.tools.check_port_conflict import check_port_conflict

        text = "8080 nginx 正在监听\n8080 java 也在用"
        ports = _extract_ports(text)
        assert len(ports) == 2, f"同端口两行应保留 2 条，实际 {ports}"
        assert {p["service"] for p in ports} == {"nginx", "java"}
        out = check_port_conflict({"ports": ports})
        assert len(out["conflicts"]) == 1, f"应报冲突，实际 {out['summary']}"

    def test_single_line_two_ports_reports_conflict(self):
        from app.graph.troubleshoot import _extract_ports
        from app.tools.check_port_conflict import check_port_conflict

        ports = _extract_ports("nginx 监听 8080，java 也占用 8080")
        out = check_port_conflict({"ports": ports})
        assert len(out["conflicts"]) == 1

    def test_same_port_different_protocol_no_conflict(self):
        from app.graph.troubleshoot import _extract_ports
        from app.tools.check_port_conflict import check_port_conflict

        ports = _extract_ports("53/udp dns\n53/tcp dns")
        assert [p["protocol"] for p in ports] == ["udp", "tcp"]
        out = check_port_conflict({"ports": ports})
        assert out["conflicts"] == [], "同端口不同协议不应算冲突"

    def test_single_port_no_conflict(self):
        from app.graph.troubleshoot import _extract_ports
        from app.tools.check_port_conflict import check_port_conflict

        ports = _extract_ports("8080 nginx")
        out = check_port_conflict({"ports": ports})
        assert out["conflicts"] == []

    def test_pid_extracted(self):
        from app.graph.troubleshoot import _extract_ports

        ports = _extract_ports("8080 java pid=1234")
        assert ports[0].get("pid") == 1234

    def test_pid_number_not_treated_as_port(self):
        """`pid=1234` 的 1234 不得被当成端口。"""
        from app.graph.troubleshoot import _extract_ports

        ports = _extract_ports("8080 java pid=1234")
        assert [p["port"] for p in ports] == [8080]

    def test_no_port_falls_back_to_placeholder(self):
        from app.graph.troubleshoot import _extract_ports

        assert _extract_ports("服务起不来") == [{"port": 8080, "protocol": "tcp"}]

    @pytest.mark.parametrize(
        "text,note",
        [
            ("8080 nginx 正在监听\n8080 java 也在用", "真换行"),
            ("8080 nginx 正在监听\\n8080 java 也在用", "字面 \\n（从 JSON 复制）"),
            ("8080/tcp nginx\\r\\n8080/tcp java", "字面 \\r\\n"),
        ],
    )
    def test_same_port_two_entries_across_escapes(self, text, note):
        """S8：字面转义序列也必须能识别为两行同端口（实测真实链路踩到）。

        真实缺陷：用户从 JSON/日志复制时带**字面 `\\n`**，整行变成
        `8080 nginx\\n8080 java` —— `\\b` 词边界在 `n8080` 处不成立，
        只提取到 1 个端口 → 又输出"未发现冲突"（错误结论）。
        """
        from app.graph.troubleshoot import _extract_ports
        from app.tools.check_port_conflict import check_port_conflict

        ports = _extract_ports(text)
        assert len(ports) == 2, f"{note}: 应提取 2 条，实际 {ports}"
        out = check_port_conflict({"ports": ports})
        assert len(out["conflicts"]) == 1, f"{note}: 应报冲突，实际 {out['summary']}"

    def test_windows_path_not_mangled(self):
        """转义还原只处理分隔类，不误伤 Windows 路径。"""
        from app.graph.troubleshoot import _normalize_escapes

        # 单个反斜杠的路径（如日志里出现 C:\Users\app）不应被改动
        assert _normalize_escapes(r"C:\Users\app\config") == r"C:\Users\app\config"

    def test_troubleshoot_node_reports_real_conflict(self, monkeypatch):
        """节点级：端口冲突问句 → 工具证据里出现真实冲突（非"未发现"）。"""
        patch_cfg(monkeypatch)
        from app.tools import ToolExecutor

        ex = ToolExecutor()
        seen: dict[str, str] = {}

        def llm(role, prompt):
            if role == "troubleshoot":
                seen["prompt"] = prompt
            return "端口被占用[1]"

        run(
            "8080 nginx 正在监听\n8080 java 也在用，是不是端口冲突？",
            llm_call=llm,
            tool_executor=ex,
            retrieve_fn=lambda o, q, c: CHUNKS,
        )
        names = [c["tool_name"] for c in ex.calls]
        assert "check_port_conflict" in names, f"未调用端口工具：{names}"
        assert "发现 1 处冲突" in seen.get("prompt", ""), "工具证据里没有报出冲突"


class TestPortExtractionSourceWiring:
    """S7-C 回归：端口的**输入源**必须包含 attach_log。

    真实缺陷（装配错误，非纯函数错误）：`_extract_ports` 本身正确，
    但调用点只喂 `question`。而"端口清单"这种多行数据用户就是**贴在附件里**的 ——
    问题只说"起不来"时，提取到的只是兜底占位，最终输出"未发现冲突"。

    **教训**：上一轮的 `TestPortExtraction` 全是纯函数直测，没测调用点喂了什么文本
    —— 这正是本循环反复出现的模式："函数对、装配错，单测盲区"。
    本用例**故意走 run_turn 全链路**，纯函数测试不得再充当它的替身。
    """

    PORT_LIST = "8080 nginx 正在监听\n8080 java 也在用"

    def test_attach_log_is_used_as_port_source(self, monkeypatch):
        """清单放 attach_log + 问题只说"起不来" → 必须报出冲突（走 run_turn）。

        用例形式按 S8 任务书指定：question = "8080 起不来，冲突吗？"，
        端口清单在 attach_log —— 断言 conflicts 非空。
        """
        patch_cfg(monkeypatch)
        from app.tools import ToolExecutor

        ex = ToolExecutor()
        seen: dict[str, str] = {}

        def llm(role, prompt):
            if role == "troubleshoot":
                seen["prompt"] = prompt
            return "端口冲突[1]"

        r = run(
            "8080 起不来，冲突吗？",
            llm_call=llm,
            tool_executor=ex,
            retrieve_fn=lambda o, q, c: CHUNKS,
            attach_log=self.PORT_LIST,
        )
        names = [c["tool_name"] for c in ex.calls]
        assert "check_port_conflict" in names, (
            f"清单在 attach_log 时未调用端口工具：{names}"
        )
        assert "发现 1 处冲突" in seen.get("prompt", ""), (
            "工具证据未报出冲突（说明提取的输入源不含 attach_log）"
        )
        assert r["answer_type"] == "answered"

    def test_node_level_conflicts_non_empty(self):
        """节点级直调（任务书指定形式）：断言 conflicts 结构非空。

        与上一条互补：上条走 run_turn 全链路，本条直接调 troubleshoot 节点、
        直接检查工具返回值中的 `conflicts`，避免"证据文案变了但判据没变"的隐性失效。
        """
        from app.tools import ToolExecutor

        from app.graph.budget import Budget
        from app.graph.troubleshoot import troubleshoot_node

        ex = ToolExecutor()
        results: list[dict] = []

        class RecordingExecutor(ToolExecutor):
            def invoke_safe(self, name, args):
                out = super().invoke_safe(name, args)
                if name == "check_port_conflict":
                    results.append(out)
                return out

        rex = RecordingExecutor()
        troubleshoot_node(
            {
                "question": "8080 起不来，冲突吗？",
                "attach_log": self.PORT_LIST,
                "route": "troubleshooting",
                "chunks": [],
            },
            cfg={}, budget=Budget(), llm_call=lambda r, p: "x", tool_executor=rex,
        )
        assert results, "端口工具未被调用"
        conflicts = results[0]["result"].get("conflicts")
        assert conflicts, f"conflicts 为空：{results[0]['result']}"
        assert conflicts[0]["port"] == 8080
        assert conflicts[0]["conflict"] is True

    def test_source_merges_attach_log_and_question(self):
        """输入源 = attach_log + question（两边都有线索时都不丢）。"""
        from app.graph.troubleshoot import _extract_ports

        # 端口清单在附件，问题里另提一个端口
        merged = f"{self.PORT_LIST}\n3306 mysql"
        ports = _extract_ports(merged)
        assert [p["port"] for p in ports] == [8080, 8080, 3306]

    def test_question_only_still_works(self):
        """清单直接写在问题里时也能工作（不因修复而回归）。"""
        from app.tools import ToolExecutor

        from app.graph.budget import Budget
        from app.graph.troubleshoot import troubleshoot_node

        ex = ToolExecutor()
        seen: dict[str, str] = {}

        def llm(role, prompt):
            seen["prompt"] = prompt
            return "x"

        troubleshoot_node(
            {"question": "8080 nginx 和 8080 java 端口冲突吗？", "route": "troubleshooting",
             "chunks": []},
            cfg={}, budget=Budget(), llm_call=llm, tool_executor=ex,
        )
        assert "check_port_conflict" in [c["tool_name"] for c in ex.calls]
        assert "发现 1 处冲突" in seen["prompt"]

    def test_normal_question_without_port_does_not_call_tool(self):
        """无端口线索时不调用端口工具（避免噪声）。"""
        from app.tools import ToolExecutor

        from app.graph.budget import Budget
        from app.graph.troubleshoot import troubleshoot_node

        ex = ToolExecutor()
        troubleshoot_node(
            {"question": "Pod 一直重启怎么排查？", "route": "troubleshooting", "chunks": []},
            cfg={}, budget=Budget(), llm_call=lambda r, p: "x", tool_executor=ex,
        )
        assert "check_port_conflict" not in [c["tool_name"] for c in ex.calls]


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