"""开场包的渲染，供 MCP server 和 SessionStart 钩子共用。

两处一定要走同一个函数，否则界面上看到的和钩子注入的会慢慢跑偏。
"""

from __future__ import annotations


def fmt(items: list[dict]) -> str:
    if not items:
        return "（没有相关记忆）"
    lines = []
    for it in items:
        tag = "".join(f"#{t} " for t in it.get("tags", [])[:3])
        flags = []
        if it.get("status") == "faded":
            flags.append("已淡忘")
        if it.get("source") == "inferred":
            flags.append("推断")
        if it.get("confidence", 1) < 0.6:
            flags.append("待确认")
        flag = f" [{'/'.join(flags)}]" if flags else ""
        extra = []
        emo = it.get("emotion") or ""
        if emo:
            who = {"user": "他当时", "self": "我当时", "both": "当时"}.get(it.get("view"), "")
            extra.append(f"{who}{emo}")
        if it.get("sensory"):
            extra.append(str(it["sensory"]))
        detail = f" ｜{'；'.join(extra)}" if extra else ""
        lines.append(
            f"#{it['id']} [{it['kind']}/{it['topic']}] {it['text']}"
            f"{detail}{flag} {tag}(权重{it['weight']}, {it['age_days']}天前)"
        )
    return "\n".join(lines)


def render_pack(pack: dict) -> str:
    """把 startup_pack() 的返回值渲染成给模型看的文本。"""
    out = []
    if pack.get("due"):
        out.append("【临近的日子】")
        for d in pack["due"]:
            when = (
                "就是今天" if d["left"] == 0
                else "明天" if d["left"] == 1
                else f"还有 {d['left']} 天"
            )
            out.append(f"#{d['id']} {d['text']} —— {d['date']}，{when}")
        out.append("")
    out.append("【偏好档案】（永不衰减）")
    out.append("\n".join(f"#{p['id']} {p['text']}" for p in pack["profile"]) or "（空）")
    out.append("\n【记得的事实】（按权重）")
    out.append(fmt(pack["facts"]))
    out.append("\n【手头在做的事】（最近的决定）")
    out.append(fmt(pack["decisions"]))
    if pack.get("short"):
        out.append("\n【最近事件】（短期记忆，最近 8 条给全文）")
        out.append(fmt(pack["short"]))
        older = pack.get("short_older")
        if older:
            out.append(
                f"…还有 {older['count']} 条更早的短期记忆"
                f"（#{older['id_from']}–#{older['id_to']}），要细节用 recall 查"
            )
    return "\n".join(out)
