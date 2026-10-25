"""写一条短期记忆（由模型调用，不是 LLM）。

滚动缓冲区 + 重复合并：
  * 和最近 12 条里某条的相似度 ≥ 0.6 → 不新增，**把那条更新掉并计数**
  * 计数到 3 → 晋升为 event（长期），从短期池里移出
  * 都不像 → 新增一条

这条路径就是"重复提起"的计票口。之前那套加权/续命/晋升全挂在 recall 上，
而短期的读路径根本不走 recall，所以 use_count 永远是 0，机制是死的。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8")

from core import PROMOTE_USE_COUNT, Store, tokenize  # noqa: E402

import os

DB = Path(os.environ.get("DSH_MEMORY_DB") or (Path.home() / ".dsh" / "memory" / "memory.db"))
MERGE_COS = 0.60          # 相似到这个程度就算"同一件事又在说"
MAX_STATE_CHARS = 80
LOOKBACK = 12             # 只跟最近 12 条比


def retire(ids: list[int]) -> list[int]:
    """把明确做完/被取代的条目退场（status='faded'）。"""
    if not ids:
        return []
    s = Store(str(DB))
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    done = []
    for mid in ids:
        cur = s.db.execute(
            "UPDATE memories SET status='faded', note=?, updated_at=? "
            "WHERE id=? AND status IN ('active','pinned')",
            ("标记完成/已被取代", now, mid),
        )
        if cur.rowcount:
            done.append(mid)
    s.db.commit()
    return done


def parse_ids(args: list[str]) -> list[int]:
    ids = []
    for a in args:
        for part in a.replace(",", " ").split():
            part = part.lstrip("#").strip()
            if part.isdigit():
                ids.append(int(part))
    return ids


def list_active() -> int:
    """列出活跃短期，给模型判断"哪条已经做完"用。"""
    s = Store(str(DB))
    rows = list(s.db.execute(
        "SELECT id,text,updated_at FROM memories WHERE kind='short' AND status='active' "
        "ORDER BY updated_at DESC"
    ))
    print(f"活跃短期 {len(rows)} 条（新的在上）：")
    for r in rows:
        print(f"  #{r['id']}  {r['text'][:44]}")
    return 0


def main() -> int:
    argv = sys.argv[1:]

    if argv[:1] == ["--list"]:
        return list_active()
    if argv[:1] == ["--done"]:
        ids = parse_ids(argv[1:])
        if not ids:
            print('用法: python set_state.py --done "#27 #28"')
            return 2
        done = retire(ids)
        print(f"退场 {len(done)} 条：{done}" if done else "没有匹配的活跃条目")
        return 0

    # 「写一条 + 顺手退几条」：写成一条命令，避免"忘了退"
    done_ids: list[int] = []
    if "--done" in argv:
        k = argv.index("--done")
        done_ids = parse_ids(argv[k + 1:])
        argv = argv[:k]
    text = " ".join(argv).strip()
    if not text:
        print('用法: python set_state.py "这一轮干了什么" [--done "#27 #28"]')
        print('      python set_state.py --done "#27 #28"')
        print('      python set_state.py --list')
        return 2
    retired = retire(done_ids)
    if len(text) > MAX_STATE_CHARS:
        text = text[: MAX_STATE_CHARS - 1] + "…"

    s = Store(str(DB))
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    toks = tokenize(text)

    rows = list(
        s.db.execute(
            "SELECT * FROM memories WHERE kind='short' AND status='active' "
            "ORDER BY id DESC LIMIT ?", (LOOKBACK,)
        )
    )
    best, best_sim = None, 0.0
    for r in rows:
        sim = s._cos(toks, tokenize(r["text"]))
        if sim > best_sim:
            best, best_sim = r, sim

    if best is not None and best_sim >= MERGE_COS:
        use = best["use_count"] + 1
        imp = min(1.0, best["importance"] + 0.15 * (1.0 - best["importance"]))
        if use >= PROMOTE_USE_COUNT:
            # 反复提起 → 转长期
            s.db.execute(
                "UPDATE memories SET kind='event', text=?, importance=?, use_count=?, "
                "updated_at=?, view='both', note=? WHERE id=?",
                (text, max(imp, 0.75), use, now, "由短期记忆晋升（重复提起）", best["id"]),
            )
            action = f"晋升为 event（第 {use} 次提起）"
        else:
            s.db.execute(
                "UPDATE memories SET text=?, importance=?, use_count=?, born_active=?, "
                "updated_at=? WHERE id=?",
                (text, imp, use, s._active_day(), now, best["id"]),
            )
            action = f"合并进 #{best['id']}（第 {use} 次提起，相似度 {best_sim:.2f}）"
        s.db.commit()
        trimmed = s.trim_short()
        tail = f"（淘汰了 {trimmed} 条过旧的）" if trimmed else ""
        if retired:
            tail += f"（退场 {retired}）"
        print(f"{action}：{text}{tail}")
        return 0

    cur = s.db.execute(
        "INSERT INTO memories(uid,kind,topic,text,tags,importance,confidence,source,"
        "status,created_at,updated_at,use_count,view,emotion,sensory,born_active,"
        "due_date,due_lead) VALUES (?,?,?,?,?,?,?,?,?,?,?,1,'','','',?, '',7)",
        (f"st{int(datetime.now().timestamp()*1000)}", "short", "项目", text,
         '["auto","状态"]', 0.8, 0.9, "model", "active", now, now, s._active_day()),
    )
    s.db.commit()
    trimmed = s.trim_short()
    tail = f"（淘汰了 {trimmed} 条过旧的）" if trimmed else ""
    if retired:
        tail += f"（退场 {retired}）"
    print(f"新增 #{cur.lastrowid}：{text}{tail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
