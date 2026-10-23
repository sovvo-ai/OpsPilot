"""`check_port_conflict`：分析用户提供的端口清单（tool-policy §2.2 / PRD FR-24）。

纯数据校验与比对。**不调用 `netstat`/`ss`/`lsof`**，不触达主机。
"""

from __future__ import annotations

from typing import Any

from app.tools.base import ToolError

MAX_PORTS = 20
VALID_PROTOCOLS = ("tcp", "udp")


def check_port_conflict(args: dict[str, Any]) -> dict[str, Any]:
    """入参：`ports`(1–20 项) + 可选 `expected_owner`。"""
    ports = args.get("ports")
    if not isinstance(ports, list) or not ports:
        raise ToolError("INVALID_TOOL_ARGS", "ports 必须为非空数组，1–20 项")
    if len(ports) > MAX_PORTS:
        raise ToolError("INVALID_TOOL_ARGS", f"ports 最多 {MAX_PORTS} 项")

    expected_owner = args.get("expected_owner")

    # 规范化 + 校验（P2：模型输出的字符串不得作为命令/SQL/路径使用，全部经枚举/范围校验）
    normalized: list[dict[str, Any]] = []
    for item in ports:
        if not isinstance(item, dict):
            raise ToolError("INVALID_TOOL_ARGS", "ports 每项必须是对象")
        port = item.get("port")
        if not isinstance(port, int) or isinstance(port, bool):
            raise ToolError("INVALID_TOOL_ARGS", "port 必须是整数")
        if not (1 <= port <= 65535):
            raise ToolError("INVALID_TOOL_ARGS", f"port 越界: {port}（须在 1–65535）")
        protocol = str(item.get("protocol", "tcp")).lower()
        if protocol not in VALID_PROTOCOLS:
            raise ToolError("INVALID_TOOL_ARGS", f"protocol 必须是 {VALID_PROTOCOLS} 之一")
        pid = item.get("pid")
        if pid is not None and (not isinstance(pid, int) or isinstance(pid, bool)):
            raise ToolError("INVALID_TOOL_ARGS", "pid 必须是整数")
        service = item.get("service")
        if service is not None and not isinstance(service, str):
            raise ToolError("INVALID_TOOL_ARGS", "service 必须是字符串")
        normalized.append(
            {"port": port, "protocol": protocol, "service": service, "pid": pid}
        )

    # 冲突判定：**同端口 + 同协议**才算冲突（§2.2 明确"同端口不同协议不算冲突"）
    buckets: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for item in normalized:
        buckets.setdefault((item["port"], item["protocol"]), []).append(item)

    conflicts: list[dict[str, Any]] = []
    for (port, protocol), items in sorted(buckets.items()):
        if len(items) < 2:
            continue
        owners = [i["service"] or (f"pid:{i['pid']}" if i["pid"] else "unknown") for i in items]
        reasons = "同一端口与协议上出现了多个占用者"
        suspected = None
        if expected_owner:
            unexpected = [o for o in owners if o != expected_owner]
            if unexpected:
                suspected = unexpected[0]
                reasons = f"预期占用者为 {expected_owner}，实际存在 {unexpected[0]}"
        conflicts.append(
            {
                "port": port,
                "protocol": protocol,
                "conflict": True,
                "suspected_owner": suspected or (owners[0] if owners else None),
                "reason": reasons,
            }
        )

    summary = (
        f"检查 {len(normalized)} 项端口，发现 {len(conflicts)} 处冲突"
        if conflicts
        else f"检查 {len(normalized)} 项端口，未发现冲突"
    )

    return {"conflicts": conflicts, "summary": summary, "checked": len(normalized)}