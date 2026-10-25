"""memview —— 直接看记忆库，不经模型。

    python memview.py                  # 活跃条目（active + pinned），按 kind 分组
    python memview.py 12               # 只看 #12，全部字段
    python memview.py --kind short     # 只看某一类
    python memview.py --topic 项目      # 只看某个主题
    python memview.py --grep 记忆       # 关键词过滤（大小写不敏感）
    python memview.py --stale          # 权重快掉没了的，该清理的
    python memview.py --all            # 连 faded / superseded / phantom 一起看
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8")

from core import KINDS, TOPICS, Store  # noqa: E402

DB = str(Path.home() / ".dsh" / "memory" / "memory.db")
STALE_BELOW = 0.30  # 权重低于此值：还没死，但快死了


def die(msg: str, code: int = 2) -> int:
    print(msg)
    return code


def main() -> int:
    store = Store(DB)
    argv = sys.argv[1:]

    if argv and argv[0].isdigit():
        row = store.db.execute("SELECT * FROM memories WHERE id=?", (int(argv[0]),)).fetchone()
        if not row:
            return die(f"没有 #{argv[0]}", 1)
        for k in row.keys():
            print(f"{k:>12}: {row[k]}")
        return 0

    def opt(name: str, default: str = "") -> str:
        if name in argv:
            i = argv.index(name)
            if i + 1 >= len(argv):
                raise SystemExit(f"{name} 后面要跟一个值")
            return argv[i + 1]
        return default

    kind = opt("--kind")
    topic = opt("--topic")
    grep = opt("--grep")
    stale = "--stale" in argv
    show_all = "--all" in argv or "--faded" in argv

    if kind and kind not in KINDS:
        return die(f"--kind 只能是 {KINDS}")
    if topic and topic not in TOPICS:
        return die(f"--topic 只能是 {TOPICS}")

    where, params = [], []
    if not show_all:
        where.append("status IN ('active','pinned')")
    if kind:
        where.append("kind=?")
        params.append(kind)
    if topic:
        where.append("topic=?")
        params.append(topic)
    if grep:
        where.append("(text LIKE ? OR tags LIKE ?)")
        params += [f"%{grep}%", f"%{grep}%"]

    sql = "SELECT * FROM memories"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY kind, id"
    rows = store.db.execute(sql, params).fetchall()

    if stale:
        rows = [r for r in rows if store.weight(r) < STALE_BELOW]
        if not rows:
            print(f"没有权重低于 {STALE_BELOW} 的条目。")
            return 0

    if not rows:
        print("没有匹配的条目。")
        return 0

    # 过滤到只剩一类时就不打分组头，省一行噪音
    group = len({r["kind"] for r in rows}) > 1
    last_kind = None
    for r in rows:
        if group and r["kind"] != last_kind:
            print(f"\n── {r['kind']} " + "─" * 40)
            last_kind = r["kind"]
        w = store.weight(r)
        flags = []
        if r["status"] == "pinned":
            flags.append("钉住")
        elif r["status"] == "faded":
            flags.append("已淡忘")
        elif r["status"] == "superseded":
            flags.append(f"被#{r['superseded_by']}取代")
        elif r["status"] == "phantom":
            flags.append("用户纠正为错误")
        if r["source"] == "inferred":
            flags.append("推断")
        if w < STALE_BELOW and r["status"] in ("active", "pinned"):
            flags.append("快过期")
        flag = f" — {'/'.join(flags)}" if flags else ""
        extra = []
        if r["emotion"]:
            who = {"user": "他当时", "self": "我当时", "both": "当时"}.get(r["view"], "")
            extra.append(f"{who}{r['emotion']}")
        if r["sensory"]:
            extra.append(r["sensory"])
        detail = f"  ｜{'；'.join(extra)}" if extra else ""
        print(f"#{r['id']:<3} w={w:.2f} 用{r['use_count']}次  {r['text']}{detail}{flag}")

    print(f"\n匹配 {len(rows)} 条", end="")
    s = store.stats()
    print(f"（全库 {s['total']} 条）  |  {s['db']}")
    if show_all and not kind:
        for k, m in s["by_kind_status"].items():
            print(f"  {k:9} " + "  ".join(f"{x}={v}" for x, v in m.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
