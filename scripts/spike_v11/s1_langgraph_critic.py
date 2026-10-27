from typing import TypedDict
from langgraph.graph import END, StateGraph

class S(TypedDict, total=False):
    route: str
    text: str
    seed: str      # ← 关键：必须声明，否则输入就被丢弃
    critic_ran: bool

def mk_final(state):
    return {"text": state.get("seed", "")}

def mk_critic(state):
    return {"text": state.get("text","") + "（已审查）", "critic_ran": True}

def after_final(state):
    t = state.get("text","")
    if state.get("route") == "troubleshooting":
        return "critic"
    if len(t) > 10:
        return "critic"
    return "end"

g = StateGraph(S); g.add_node("final", mk_final); g.add_node("critic", mk_critic)
g.set_entry_point("final")
g.add_conditional_edges("final", after_final, {"critic":"critic","end":END})
g.add_edge("critic", END)
app = g.compile()

for label, st in [
    ("troubleshooting 短",  {"route":"troubleshooting","seed":"短"}),
    ("kb_qa 短",            {"route":"kb_qa","seed":"短"}),
    ("kb_qa 超长(11字)",     {"route":"kb_qa","seed":"一二三四五六七八九十一"}),
    ("kb_qa 恰好10字",       {"route":"kb_qa","seed":"一二三四五六七八九十"}),
]:
    r = app.invoke(st)
    print(f"{label:22s} critic_ran={r.get('critic_ran', False)}  text_len={len(r.get('text',''))}")
