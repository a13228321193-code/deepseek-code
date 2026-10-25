"""Stop 钩子：到点了就"点名"让模型自己写状态。没有 LLM 兜底。

协议依据（dsh-hooks-claude-code 源码）：
    agent/turn-stopping 里 merged.decision === "deny" 时，
    agent.steer(createUserMessage(reason)) —— 把 reason 作为 steering 消息塞回给模型，
    强制它再走一步。这就是"钩子调动模型"的机制。

三道闸，防死循环：
  1. 状态是新鲜的（15 分钟内更新过）  → 不吭声，放行
  2. 刚点过名（120 秒内）            → 不再点名，静静放行
  3. 否则                            → deny + reason，让模型自己写

为什么不加 LLM 兜底：没上下文的模型写不出"当前状态"，试过，质量不够。
宁可等下一轮再点一次名，让有上下文的模型来写。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path(os.path.expanduser("~"))
MEM = HOME / ".dsh" / "memory"
DB = MEM / "memory.db"
STOP_STATE = MEM / "stop_state.json"

BLOCK_COOLDOWN = 120          # 点名的冷却，防止死循环


def auto_row_updated_ts() -> float:
    """那行 auto 状态的 updated_at（epoch 秒），没有就 0。"""
    if not DB.exists():
        return 0.0
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=3)
        row = con.execute(
            "SELECT updated_at FROM memories WHERE tags LIKE '%\"auto\"%' "
            "AND status!='phantom' ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        con.close()
        if not row or not row[0]:
            return 0.0
        return datetime.fromisoformat(row[0]).timestamp()
    except Exception:
        return 0.0


def load_state() -> dict:
    try:
        return json.loads(STOP_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(d: dict) -> None:
    try:
        STOP_STATE.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def active_list(limit: int = 12) -> str:
    """把活跃的短期条目列出来，让模型在写新状态时顺手判断哪些该退场。

    不给清单的话，模型只能看见注入里那一条，没法判断 "哪几条已经做完了"。
    """
    if not DB.exists():
        return "（读不到库）"
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=3)
        rows = list(con.execute(
            "SELECT id,text FROM memories WHERE kind='short' AND status='active' "
            "ORDER BY updated_at DESC LIMIT ?", (limit,)
        ))
        con.close()
    except Exception:
        return "（读库失败）"
    if not rows:
        return "（没有）"
    return "\n".join(f"#{i} {t[:20]}" for i, t in rows)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    except Exception:
        pass

    try:
        raw = sys.stdin.buffer.read()
        payload = json.loads(raw.decode("utf-8")) if raw.strip() else {}
    except Exception:
        payload = {}
    sid = str(payload.get("session_id") or os.environ.get("DSH_SESSION_ID") or "")

    now, updated = time.time(), auto_row_updated_ts()
    state = load_state()
    last_stop = float(state.get("last_stop", 0) or 0)

    # 判据是「**上一次 Stop 之后**有没有写过」，不是「15 分钟内有没有写过」。
    # 用固定窗口会漏：一次写入之后连着半小时的每一轮都被当成"刚写过"。
    wrote_this_turn = updated > last_stop
    state["last_stop"] = now

    if wrote_this_turn:
        state["last_ok"] = now
        save_state(state)
        return 0

    # 冷却期内不重复点名（也没有 LLM 兜底——没上下文的总结没有价值）
    if now - float(state.get("last_block", 0) or 0) < BLOCK_COOLDOWN:
        save_state(state)
        return 0

    # 点名，让模型自己写
    state["last_block"] = now
    save_state(state)
    # 路径从 __file__ 现算，别写死 —— 写死的话换台机器就失效，而且会把用户名带进代码里
    setter = HERE / "set_state.py"
    cmd = f'"{sys.executable}" "{setter}" "<一句话>" --done "#27 #28"'
    print(json.dumps({
        "decision": "block",
        "reason": (
            "写短期记忆。有做完的就一起退。\n"
            f"{cmd}\n"
            "写完只回一行 `已写 #<id>`（有退场就 `已写 #<id>，退 #a #b`）。"
            "禁止解释、总结、表格、复述清单。\n"
            "当前活跃：\n" + active_list()
        ),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
