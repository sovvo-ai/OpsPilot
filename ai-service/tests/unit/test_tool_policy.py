"""工具策略单元测试（tool-policy §1/§3/§4/§5/§6）。

覆盖：
- 封闭白名单：非白名单 400 TOOL_NOT_ALLOWED + 审计行
- 硬性禁令 P1：静态检查 app/tools/ 无通用执行器
- 执行限制：超时 5s、输出 4KB 截断、单轮 ≤3 次、入参校验
- P5 同参数去重
- 审计：args_digest 只存摘要（不含原始入参）
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest  # noqa: E402

from app.tools import TOOL_REGISTRY, ToolExecutor, ToolNotAllowedError, get_tool  # noqa: E402
from app.tools.analyze_error_log import MAX_LOG_CHARS, analyze_error_log  # noqa: E402
from app.tools.base import (  # noqa: E402
    MAX_OUTPUT_BYTES,
    MAX_TOOL_CALLS_PER_TURN,
    TOOL_TIMEOUT_SECONDS,
    ToolError,
    args_digest,
    canonical_json,
    truncate_output,
)
from app.tools.check_port_conflict import check_port_conflict  # noqa: E402

TOOLS_DIR = Path(__file__).resolve().parent.parent.parent / "app" / "tools"


# ---------------------------------------------------------------------------
# §1 封闭白名单
# ---------------------------------------------------------------------------


class TestRegistryClosed:
    def test_exactly_three_tools(self):
        assert set(TOOL_REGISTRY) == {
            "analyze_error_log",
            "check_port_conflict",
            "search_knowledge_base",
        }

    def test_registry_is_plain_dict_constant(self):
        """注册表是常量：不允许运行时注册/从配置读工具名。"""
        assert isinstance(TOOL_REGISTRY, dict)
        # 不是动态代理或配置容器
        assert not hasattr(TOOL_REGISTRY, "register")

    def test_non_whitelisted_raises_400(self):
        """非白名单工具 → 400 TOOL_NOT_ALLOWED（§6 用例 6）。"""
        with pytest.raises(ToolNotAllowedError) as exc:
            get_tool("read_log_tail")
        assert exc.value.code == "TOOL_NOT_ALLOWED"
        assert exc.value.http_status == 400

    def test_non_whitelisted_records_audit(self):
        """拒绝也要记审计行（status=rejected），且不抛 500。"""
        ex = ToolExecutor()
        with pytest.raises(ToolNotAllowedError):
            ex.invoke("read_log_tail", {})
        assert len(ex.calls) == 1
        assert ex.calls[0]["status"] == "rejected"
        assert ex.calls[0]["tool_name"] == "read_log_tail"

    def test_invoke_safe_does_not_raise(self):
        """节点内使用的宽松版本：白名单拒绝转为结构化结果。"""
        ex = ToolExecutor()
        out = ex.invoke_safe("read_log_tail", {})
        assert out["status"] == "rejected"
        assert out["degraded"] is True


# ---------------------------------------------------------------------------
# §3 硬性禁令 P1（静态检查）
# ---------------------------------------------------------------------------


class TestStaticForbidden:
    """P1：app/tools/ 下不得出现 exec/subprocess/os.system/eval/compile。

与 CI 检查同一规则；此处作为单测前置防线。
注意：必须用**词边界精确匹配** —— 朴素的子串匹配会把 `re.compile(` 误判为
通用执行器（实测踩过），产生假阳性。
"""

    FORBIDDEN_PATTERNS = (
        r"\bsubprocess\b",
        r"\bos\.system\b",
        r"\bos\.popen\b",
        r"\bos\.exec[lv]",
        r"(?<![\w.])eval\s*\(",
        r"(?<![\w.])exec\s*\(",
        r"(?<![\w.])compile\s*\(",  # 排除 re.compile
    )

    def test_no_executor_in_tools_dir(self):
        offenders = []
        for f in TOOLS_DIR.glob("*.py"):
            text = f.read_text(encoding="utf-8")
            for pat in self.FORBIDDEN_PATTERNS:
                if re.search(pat, text):
                    offenders.append(f"{f.name}: {pat}")
        assert not offenders, f"app/tools/ 出现通用执行器痕迹: {offenders}"

    def test_re_compile_not_flagged(self):
        """`re.compile` 是安全用法，不得被误判。"""
        assert not re.search(r"(?<![\w.])compile\s*\(", "PAT = re.compile(r'x')")

    def test_no_shell_or_path_params(self):
        """P3/P4：无路径类参数、无 shell 拼接。"""
        for f in TOOLS_DIR.glob("*.py"):
            text = f.read_text(encoding="utf-8")
            assert "shell=True" not in text, f"{f.name} 出现 shell=True"


# ---------------------------------------------------------------------------
# §4 执行限制
# ---------------------------------------------------------------------------


class TestExecutionLimits:
    def test_constants_match_policy(self):
        assert TOOL_TIMEOUT_SECONDS == 5.0
        assert MAX_TOOL_CALLS_PER_TURN == 3
        assert MAX_OUTPUT_BYTES == 4096

    def test_output_truncated_to_4kb(self):
        """§4：单次输出 ≤4KB，超限截断并标 truncated=true。"""
        big = {"chunks": [{"content": "x" * 500, "chunk_id": str(i)} for i in range(50)]}
        out = truncate_output(dict(big))
        assert out["truncated"] is True
        assert len(canonical_json(out).encode("utf-8")) <= MAX_OUTPUT_BYTES

    def test_small_output_not_marked_truncated(self):
        out = truncate_output({"chunks": [{"content": "small"}]})
        assert out["truncated"] is False

    def test_fourth_call_skipped_and_degraded(self):
        """§4：第 4 次起直接跳过，degraded=true + tool_skipped_budget 语义。"""
        ex = ToolExecutor()
        for i in range(3):
            r = ex.invoke("analyze_error_log", {"log_text": f"ERROR line {i}"})
            assert r["status"] == "ok"
        r4 = ex.invoke("analyze_error_log", {"log_text": "ERROR line 4"})
        assert r4["status"] == "skipped"
        assert r4["degraded"] is True
        assert r4["result"]["error"]["code"] == "TOOL_BUDGET_EXCEEDED"

    def test_args_validation_rejected(self):
        """入参校验失败 → status=rejected（§5 的 status 枚举）。"""
        ex = ToolExecutor()
        r = ex.invoke("check_port_conflict", {"ports": [{"port": 70000, "protocol": "tcp"}]})
        assert r["status"] == "rejected"
        assert r["result"]["error"]["code"] == "INVALID_TOOL_ARGS"


# ---------------------------------------------------------------------------
# P5 同参数去重
# ---------------------------------------------------------------------------


class TestDedup:
    def test_same_args_reuses_cache(self):
        """P5：同轮内同一工具对同一规范化参数只执行一次。"""
        ex = ToolExecutor()
        args = {"log_text": "2026-10-23 ERROR connection refused"}
        r1 = ex.invoke("analyze_error_log", args)
        r2 = ex.invoke("analyze_error_log", dict(args))
        assert r1["args_digest"] == r2["args_digest"]
        # 只记一次审计（缓存复用不新增执行）
        assert len(ex.calls) == 1

    def test_different_args_not_deduped(self):
        ex = ToolExecutor()
        ex.invoke("analyze_error_log", {"log_text": "ERROR a"})
        ex.invoke("analyze_error_log", {"log_text": "ERROR b"})
        assert len(ex.calls) == 2

    def test_canonical_json_key_order_insensitive(self):
        """规范化 JSON 保证键序不同的同参数得同摘要。"""
        assert args_digest({"a": 1, "b": 2}) == args_digest({"b": 2, "a": 1})


# ---------------------------------------------------------------------------
# §5 审计
# ---------------------------------------------------------------------------


class TestAudit:
    def test_digest_format(self):
        d = args_digest({"log_text": "secret log content"})
        assert d.startswith("sha256:")
        assert len(d) == len("sha256:") + 32

    def test_audit_does_not_contain_raw_args(self):
        """§5：只存摘要，不存原始入参（日志可能含敏感信息）。"""
        secret = "password=SuperSecret123"
        ex = ToolExecutor()
        ex.invoke("analyze_error_log", {"log_text": f"ERROR {secret}"})
        record = ex.calls[0]
        assert "log_text" not in record
        assert secret not in json.dumps(record, ensure_ascii=False)

    def test_status_enum(self):
        """status ∈ {ok, timeout, error, rejected}。"""
        ex = ToolExecutor()
        ex.invoke("analyze_error_log", {"log_text": "ERROR x"})
        assert ex.calls[0]["status"] in ("ok", "timeout", "error", "rejected")


# ---------------------------------------------------------------------------
# 工具本身的行为
# ---------------------------------------------------------------------------


class TestAnalyzeErrorLog:
    def test_detects_connection_refused(self):
        out = analyze_error_log({"log_text": "ERROR dial tcp: connect: connection refused"})
        assert out["error_type"] == "connection_refused"
        assert out["key_lines"]

    def test_empty_text_rejected(self):
        with pytest.raises(ToolError):
            analyze_error_log({"log_text": "   "})

    def test_over_8kb_truncated(self):
        """§6 用例 1：超长日志截断至 8KB，truncated=true，不报错。"""
        out = analyze_error_log({"log_text": "ERROR " + "x" * 20000})
        assert out["truncated"] is True

    def test_invalid_language_rejected(self):
        with pytest.raises(ToolError):
            analyze_error_log({"log_text": "ERROR x", "language": "fr"})

    def test_no_root_cause_conclusion(self):
        """不解释根因、不给结论（结论归 troubleshoot 节点）。"""
        out = analyze_error_log({"log_text": "ERROR connection refused"})
        assert "root_cause" not in out
        assert "conclusion" not in out


class TestCheckPortConflict:
    def test_same_port_same_protocol_is_conflict(self):
        out = check_port_conflict(
            {
                "ports": [
                    {"port": 8080, "protocol": "tcp", "service": "nginx"},
                    {"port": 8080, "protocol": "tcp", "service": "java"},
                ]
            }
        )
        assert len(out["conflicts"]) == 1

    def test_same_port_different_protocol_not_conflict(self):
        """§2.2：同端口不同协议不算冲突。"""
        out = check_port_conflict(
            {
                "ports": [
                    {"port": 53, "protocol": "tcp"},
                    {"port": 53, "protocol": "udp"},
                ]
            }
        )
        assert out["conflicts"] == []

    def test_port_out_of_range_rejected(self):
        with pytest.raises(ToolError):
            check_port_conflict({"ports": [{"port": 70000, "protocol": "tcp"}]})

    def test_too_many_ports_rejected(self):
        with pytest.raises(ToolError):
            check_port_conflict({"ports": [{"port": i, "protocol": "tcp"} for i in range(1, 25)]})

    def test_expected_owner_mismatch_reported(self):
        out = check_port_conflict(
            {
                "ports": [
                    {"port": 8080, "protocol": "tcp", "service": "nginx"},
                    {"port": 8080, "protocol": "tcp", "service": "java"},
                ],
                "expected_owner": "nginx",
            }
        )
        assert out["conflicts"][0]["suspected_owner"] == "java"