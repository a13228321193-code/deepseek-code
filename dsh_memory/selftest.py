"""selftest —— 不需要 DSH，直接对着 server.py 走一遍 MCP 协议。

用法：python selftest.py
它会用一个临时数据库，不碰你 ~/.dsh/memory 里的真实记忆。
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent
OK, BAD = "  [ok]", "  [FAIL]"


def run_session(db: str, requests: list[dict]) -> list[dict]:
    env = dict(os.environ)
    env["DSH_MEMORY_DB"] = db
    env["PYTHONIOENCODING"] = "utf-8"
    payload = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in requests)
    proc = subprocess.run(
        [sys.executable, str(HERE / "server.py")],
        input=payload.encode("utf-8"),
        capture_output=True,
        env=env,
        timeout=60,
    )
    if proc.returncode != 0:
        print(proc.stderr.decode("utf-8", "replace"))
        raise SystemExit("server 异常退出")
    out = proc.stdout.decode("utf-8").strip().splitlines()
    return [json.loads(x) for x in out if x.strip()]


def req(i: int, method: str, params: dict | None = None) -> dict:
    m = {"jsonrpc": "2.0", "id": i, "method": method}
    if params is not None:
        m["params"] = params
    return m


def call(i: int, name: str, args: dict) -> dict:
    return req(i, "tools/call", {"name": name, "arguments": args})


def text_of(resp: dict) -> str:
    return resp["result"]["content"][0]["text"]


def main() -> int:
    tmp = Path(tempfile.mkdtemp()) / "test.db"
    print(f"临时库：{tmp}")

    requests = [
        req(1, "initialize", {"protocolVersion": "2024-11-05",
                              "capabilities": {}, "clientInfo": {"name": "selftest"}}),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        req(2, "tools/list"),
        req(3, "ping"),
        call(10, "remember", {
            "text": "用户希望回答简洁，不要用 emoji 和套话",
            "kind": "profile", "topic": "偏好", "tags": ["语气", "格式"],
            "importance": 0.95, "source": "user_said"}),
        call(11, "remember", {
            "text": "用户的记忆项目放在 D:\\projects\\dsh_memory",
            "kind": "fact", "topic": "项目", "tags": ["路径", "dsh_memory"],
            "importance": 0.8}),
        call(12, "remember", {
            "text": "用户希望回答简洁，不要用 emoji 和套话",
            "kind": "profile", "topic": "偏好"}),
        call(13, "remember", {"text": "我的 API key 是 sk-abcdefghijklmnopqrstuvwxyz123456"}),
        call(14, "remember", {"text": "随便聊聊", "kind": "fact"}),
        call(15, "remember", {
            "text": "决定用 MCP stdio 落地长期记忆，因为独立 CLI 那套接不进会话",
            "kind": "decision", "topic": "项目", "tags": ["MCP", "决策"],
            "importance": 0.85}),
        call(20, "recall", {}),
        call(21, "recall", {"query": "记忆项目 路径"}),
        call(22, "recall", {"query": "记忆项目 路径"}),
        call(30, "forget", {"id": 2, "reason": "用户说路径过期了"}),
        call(31, "recall", {"query": "路径"}),
        call(32, "pin", {"id": 1, "pinned": True}),
    ]

    resps = {r["id"]: r for r in run_session(str(tmp), requests)}
    fails = 0

    def check(cond: bool, label: str) -> None:
        nonlocal fails
        print((OK if cond else BAD) + " " + label)
        if not cond:
            fails += 1

    check(resps[1]["result"]["protocolVersion"] == "2024-11-05",
          f"initialize 回显协议版本 ({resps[1]['result']['protocolVersion']})")
    check(len(resps[2]["result"]["tools"]) == 4, "tools/list 返回 4 个工具")
    check(resps[3]["result"] == {}, "ping 正常")

    t10 = text_of(resps[10])
    check("已记住 #1" in t10 and "钉住" in t10, f"写入 profile 并钉住 -> {t10}")
    check("已记住 #2" in text_of(resps[11]), "写入 fact")
    check("合并" in text_of(resps[12]), f"重复写入被合并 -> {text_of(resps[12])}")
    check("拒绝" in text_of(resps[13]) and resps[13]["result"]["isError"],
          f"敏感信息被拒 -> {text_of(resps[13])[:40]}")
    check("拒绝" in text_of(resps[14]) and "太短" in text_of(resps[14]),
          f"无锚点/太短被拒 -> {text_of(resps[14])[:40]}")

    pack = text_of(resps[20])
    check("【偏好档案】" in pack and "不要用 emoji" in pack, "开场包包含偏好")
    check("【手头在做的事】" in pack and "MCP stdio 落地长期记忆" in pack,
          "开场包包含 top-2 决定")
    hit = text_of(resps[21])
    check("dsh_memory" in hit, f"检索命中路径 -> {hit.splitlines()[0][:60]}")
    check("权重" in text_of(resps[22]), "重复检索会加权")

    check("错误" in text_of(resps[30]), f"forget -> {text_of(resps[30])}")
    check("dsh_memory" not in text_of(resps[31]), "被纠正的记忆不再出现")
    check("钉住" in text_of(resps[32]), f"pin -> {text_of(resps[32])}")

    # ---- 直接开库，验证衰减 / 降级 / 偏好不衰减（不走 MCP 协议）----
    print("\n── 衰减与降级 ──")
    from datetime import datetime, timedelta, timezone

    from core import FADE_BELOW, Store

    st = Store(str(tmp))

    def backdate(mid: int, days: int) -> None:
        old = (datetime.now(timezone.utc).astimezone() - timedelta(days=days)).isoformat(
            timespec="seconds"
        )
        st.db.execute("UPDATE memories SET created_at=? WHERE id=?", (old, mid))
        st.db.commit()

    fid = st.remember("用户有一台二手 ThinkPad X1 当备用机", kind="fact",
                      topic="工具", importance=0.6)["id"]
    backdate(fid, 300)
    st.consolidate()
    row = st.db.execute("SELECT status FROM memories WHERE id=?", (fid,)).fetchone()
    check(row["status"] == "faded", "300 天没被想起来的 fact -> faded")

    pid = st.remember("用户要求所有回复用中文", kind="profile", topic="偏好")["id"]
    backdate(pid, 2000)
    st.consolidate()
    row = st.db.execute("SELECT status FROM memories WHERE id=?", (pid,)).fetchone()
    check(row["status"] in ("active", "pinned"), "2000 天前的 profile 仍存活（偏好不衰减）")

    hits = st.recall("ThinkPad 备用机", limit=8)
    check(any(h["id"] == fid for h in hits), "faded 条目仍能被精确检索翻出来（遗忘≠删除）")

    # 开场包不加权；被顺带捞出的低相关条目也不加权；只有真·定点命中才加权
    aid = st.remember("用户习惯用 Obsidian 记笔记", kind="fact",
                      topic="工具", importance=0.5)["id"]
    bid = st.remember("用户的显示器是 27 寸 4K", kind="fact",
                      topic="工具", importance=0.5)["id"]

    def snap(mid: int) -> tuple[float, int]:
        r = st.db.execute(
            "SELECT importance,use_count FROM memories WHERE id=?", (mid,)
        ).fetchone()
        return (round(r["importance"], 4), r["use_count"])

    a1, b1 = snap(aid), snap(bid)
    st.startup_pack()
    st.startup_pack()
    a2, b2 = snap(aid), snap(bid)
    check(a2 == a1 and b2 == b1, f"开场包不改权重（A {a1}->{a2} / B {b1}->{b2}）")

    st.recall("", limit=5)  # 空 query，rel 全为 0
    a2b, b2b = snap(aid), snap(bid)
    check(a2b == a2 and b2b == b2, "空 query 的 recall 也不加权")

    st.recall("Obsidian 记笔记", limit=5)
    a3, b3 = snap(aid), snap(bid)
    check(a3[0] > a2b[0] and a3[1] > a2b[1], f"相关的才加权（{a2b} -> {a3}）")
    check(b3 == b2b, f"被顺带捞出、但不相关的仍不加权（{b2b} -> {b3}）")

    # ---- 情节三件套 ----
    print("\n── 事件：视角 / 情绪 / 细节 ──")
    eid = st.remember(
        "凌晨两点多，老大给我定了名字叫大肥鱼", kind="event", topic="人物",
        view="both", emotion="意外", sensory="他说这话时系统时间已经 02:17",
    )["id"]
    row = st.db.execute(
        "SELECT view,emotion,sensory FROM memories WHERE id=?", (eid,)
    ).fetchone()
    check(
        row["view"] == "both" and row["emotion"] == "意外" and "02:17" in row["sensory"],
        f"event 存下三件套（{row['view']}/{row['emotion']}/{row['sensory'][:12]}…）",
    )

    zid = st.remember("某件没有视角但带了情绪的事", kind="event",
                      topic="其他", emotion="开心")["id"]
    row = st.db.execute("SELECT emotion FROM memories WHERE id=?", (zid,)).fetchone()
    check(row["emotion"] == "", "没有 view 时情绪被丢弃（不存糊涂账）")

    hits = st.recall("大肥鱼 名字", limit=5)
    check(any(h["id"] == eid and h["view"] == "both" for h in hits),
          "recall 会把视角字段带回来")

    # ---- IDF ----
    print("\n── IDF 加权 ──")
    idf = Store._idf([{"用户": 1, "咖啡": 1}, {"用户": 1}, {"用户": 1}])
    check(
        idf["咖啡"] > idf["用户"],
        f"罕见词 IDF 更高（咖啡 {idf['咖啡']:.2f} > 用户 {idf['用户']:.2f}）",
    )
    common = Store._cos_idf({"用户": 1}, {"用户": 1, "咖啡": 1}, idf, 1.0)
    rare = Store._cos_idf({"咖啡": 1}, {"用户": 1, "咖啡": 1}, idf, 1.0)
    check(rare > common, f"命中罕见词比命中常见词更像（{rare:.3f} > {common:.3f}）")

    # ---- 短期记忆 ----
    print("\n── 短期记忆 ──")
    sid = st.remember("正在给 dsh_memory 加短期记忆和权重晋升", kind="short",
                      topic="项目", importance=0.7)["id"]
    pack = st.startup_pack()
    check(any(x["id"] == sid for x in pack.get("short", [])), "短期记忆会进开场包")

    def wof(mid: int) -> float:
        return st.weight(st.db.execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone())

    w0 = wof(sid)
    w1 = wof(sid)
    check(abs(w1 - w0) < 1e-9, f"同一活跃日内不衰减（{w0:.3f} -> {w1:.3f}）")

    # 寿命：importance 0.7 的短期记忆，第 6 个活跃日还在，第 7 个掉出去
    st._set_meta("active_day", str(st._active_day() + 1))
    check(wof(sid) < w0, f"过 1 个活跃日权重要降（{w0:.3f} -> {wof(sid):.3f}）")
    st._set_meta("active_day", str(st._active_day() + 5))  # 累计第 6 天
    w6 = wof(sid)
    check(w6 >= FADE_BELOW, f"第 6 个活跃日还在（w={w6:.3f} > {FADE_BELOW}）")
    st._set_meta("active_day", str(st._active_day() + 1))  # 第 7 天
    w7 = wof(sid)
    check(w7 < FADE_BELOW, f"第 7 个活跃日掉出（w={w7:.3f} < {FADE_BELOW}）")

    zid = st.remember("三个月前的临时备忘草稿", kind="short", topic="其他",
                      importance=0.7)["id"]
    st.db.execute("UPDATE memories SET created_at=datetime('now','-40 day') WHERE id=?", (zid,))
    st.db.commit()
    check(wof(zid) == 0.0, "墙钟超 30 天的 short 权重归零（僵尸兜底）")
    st.consolidate()
    check(
        st.db.execute("SELECT status FROM memories WHERE id=?", (zid,)).fetchone()["status"]
        == "faded",
        "僵尸 short 被清成 faded",
    )

    # 短期"更新"：换正文，但不计提及次数、不加权（否则每轮更新都会被顶成永久）
    t_old = "记忆项目当前状态：永久层、日期唤醒全部完成；待办是重启 dsh"
    t_new = "记忆项目当前状态：永久层、日期唤醒都已完成；新会话强制 recall 已写进规则"
    from core import tokenize as _tok

    cpair = Store._cos(_tok(t_old), _tok(t_new))
    check(0.40 < cpair < 0.90, f"这对状态的余弦 {cpair:.2f} 落在 short 阈值带里")

    upd = st.remember(t_old, kind="short", topic="项目", importance=0.6)["id"]
    b = st.db.execute("SELECT use_count,importance FROM memories WHERE id=?", (upd,)).fetchone()
    st.remember(t_new, kind="short", topic="项目", importance=0.6)
    a = st.db.execute("SELECT id,text,use_count,importance FROM memories WHERE id=?",
                      (upd,)).fetchone()
    check("强制 recall" in a["text"], "短期状态更新会替换正文")
    check(a["use_count"] == b["use_count"], "状态更新不计入提及次数")
    check(abs(a["importance"] - b["importance"]) < 1e-9, "状态更新不加权")

    pid2 = st.remember("短期记忆要靠反复触及才晋升为长期", kind="short",
                       topic="项目", importance=0.7)["id"]
    st.recall("短期记忆 晋升 权重", limit=3)
    st.recall("短期记忆 晋升 权重", limit=3)
    st.consolidate()
    row = st.db.execute("SELECT use_count,kind FROM memories WHERE id=?", (pid2,)).fetchone()
    check(row["kind"] == "short", f"碰两次还不晋升（use={row['use_count']}）")
    st.recall("短期记忆 晋升 权重", limit=3)
    st.consolidate()
    row2 = st.db.execute("SELECT kind,importance FROM memories WHERE id=?", (pid2,)).fetchone()
    check(row2["kind"] == "event",
          f"碰到第三次才晋升（kind={row2['kind']}, importance={row2['importance']:.2f}）")

    # 被想起的 short 会续命：活跃日龄归零
    renew = st.remember("这条短期记忆要被反复想起从而续命", kind="short",
                        topic="项目", importance=0.7)["id"]
    st.db.execute("UPDATE memories SET born_active=born_active-5 WHERE id=?", (renew,))
    st.db.commit()
    check(wof(renew) < 0.25, f"先让它老 5 个活跃日（w={wof(renew):.3f}）")
    st.recall("要被反复想起从而续命", limit=3)
    check(wof(renew) > 0.6, f"被想起后活跃日龄归零（w={wof(renew):.3f}）")

    # 提及 3 次就能晋升，哪怕当时权重已经掉到 0.45 以下
    p3 = st.remember("分散在好几天里被反复提到的同一个问题", kind="short",
                     topic="项目", importance=0.7)["id"]
    for _ in range(3):
        st.recall("分散 反复提到 同一个问题", limit=3)
    st.db.execute("UPDATE memories SET born_active=born_active-3 WHERE id=?", (p3,))
    st.db.commit()
    w_low = wof(p3)
    check(w_low < 0.45, f"先把它变老，权重掉到 {w_low:.3f}")
    st.consolidate()
    check(
        st.db.execute("SELECT kind FROM memories WHERE id=?", (p3,)).fetchone()["kind"]
        == "event",
        "提及 3 次即使权重不够也晋升",
    )

    # 续命：久远的长期记忆被想起来之后，重新变年轻
    e_old = st.remember("很早以前的一次长谈，聊的是记忆架构", kind="event",
                        topic="项目", importance=0.8, view="both")["id"]
    backdate(e_old, 40)
    w_before = wof(e_old)
    check(w_before < 0.2, f"40 天没被想起，权重掉到 {w_before:.3f}")
    st.recall("记忆架构 长谈", limit=8)
    w_after = wof(e_old)
    check(w_after > 0.7, f"被想起来之后重新变年轻（{w_before:.3f} -> {w_after:.3f}）")

    # ---- 永久记忆 & 钉住 ----
    print("\n── 永久记忆 ──")
    st5 = Store(str(Path(tempfile.mkdtemp()) / "perm.db"))

    def backdate5(mid: int, days: int) -> None:
        old = (datetime.now(timezone.utc).astimezone()
               - timedelta(days=days)).isoformat(timespec="seconds")
        st5.db.execute("UPDATE memories SET created_at=? WHERE id=?", (old, mid))
        st5.db.commit()

    def row5(mid: int):
        return st5.db.execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone()

    birth = st5.remember("老大的儿子生日是 6 月 18 日", kind="permanent",
                         topic="人物", tags=["生日", "儿子"], importance=0.85)["id"]
    pinned = st5.remember("老大有一台 ThinkPad T14 当备用机", kind="fact",
                          topic="工具", importance=0.5)["id"]
    st5.pin(pinned, True)

    backdate5(birth, 800)
    backdate5(pinned, 800)
    check(abs(st5.weight(row5(birth)) - 0.85) < 1e-6,
          f"permanent 800 天后权重不变（{st5.weight(row5(birth)):.3f}）")
    w_pin = st5.weight(row5(pinned))
    imp_pin = row5(pinned)["importance"]
    check(abs(w_pin - imp_pin) < 1e-6,
          f"钉住的 fact 800 天后权重 = importance，没有衰减（{w_pin:.3f}）")

    pack5 = st5.startup_pack()
    in_pack = {x["id"] for k in ("facts", "decisions", "short") for x in pack5.get(k, [])}
    check(birth not in in_pack and pinned not in in_pack, "永久记忆和钉住的都不进开场包")

    hits5 = st5.recall("儿子的生日是哪天", limit=5)
    check(any(h["id"] == birth for h in hits5), "提到时仍然检索得到（不在包里 ≠ 找不到）")

    # ---- 日期唤醒 ----
    print("\n── 日期唤醒 ──")
    from datetime import date as _date
    from datetime import timedelta as _td

    from core import _next_due, _norm_due

    for raw, want in [
        ("6-18", "06-18"),
        ("06-18", "06-18"),
        ("2027年6月18日", "2027-06-18"),
        ("2027/6/18", "2027-06-18"),
        ("乱写一通", ""),
        ("13-40", ""),
    ]:
        check(_norm_due(raw) == want, f"日期解析 {raw!r} -> {want or '拒收'}")

    today = _date.today()
    nd = _next_due("01-01", today)
    check(nd is not None and nd >= today, f"每年重复取下一次（01-01 -> {nd}）")

    st6 = Store(str(Path(tempfile.mkdtemp()) / "due.db"))
    far = st6.remember("二十天后才发生的一件大事", kind="permanent",
                       due=(today + _td(days=20)).isoformat(), lead=7)["id"]
    check(all(x["id"] != far for x in st6.upcoming()), "20 天后的事还没进窗口")

    near = st6.remember("七天后要去办的一件正事", kind="permanent",
                        due=(today + _td(days=7)).isoformat(), lead=7)["id"]
    check(any(x["id"] == near for x in st6.upcoming()), "第 7 天开始提醒（第一档）")
    check(all(x["id"] != near for x in st6.upcoming()), "同一档不重复提醒")

    def shift(mid: int, days: int) -> None:
        st6.db.execute(
            "UPDATE memories SET due_date=? WHERE id=?",
            ((today + _td(days=days)).isoformat(), mid),
        )
        st6.db.commit()

    shift(near, 3)
    check(any(x["id"] == near and x["stage"] == "3" for x in st6.upcoming()),
          "还剩 3 天，第二档提醒")
    shift(near, 1)
    check(any(x["id"] == near and x["stage"] == "1" for x in st6.upcoming()),
          "还剩 1 天，第三档提醒")
    shift(near, 0)
    check(any(x["id"] == near and x["stage"] == "0" for x in st6.upcoming()),
          "当天提醒")

    past = st6.remember("上个月已经过去的一件旧事", kind="permanent",
                        due=(today - _td(days=1)).isoformat())["id"]
    check(all(x["id"] != past for x in st6.upcoming()), "一次性日期过期后不再提醒")

    check(isinstance(st6.startup_pack().get("due"), list), "开场包带 due 字段")

    # ---- 跨 kind 不合并 ----
    print("\n── 跨 kind 隔离 ──")
    st4 = Store(str(Path(tempfile.mkdtemp()) / "kinds.db"))
    same = "短期记忆要按活跃日衰减，不按墙钟走"
    ka = st4.remember(same, kind="short", topic="项目", importance=0.6)["id"]
    kb = st4.remember(same, kind="event", topic="项目", importance=0.6, view="self")["id"]
    rep = st4.consolidate()
    alive = {
        r["id"]
        for r in st4.db.execute("SELECT id FROM memories WHERE status IN ('active','pinned')")
    }
    check(ka in alive and kb in alive, f"同文不同 kind 不会被合并（merged={rep['merged']}）")

    # ---- 候选集只扫 active，faded 兜底 ----
    print("\n── 检索范围 ──")
    st3 = Store(str(Path(tempfile.mkdtemp()) / "scope.db"))
    want = st3.remember("老大喜欢在重庆吃小面", kind="fact", importance=0.8)["id"]
    old = st3.remember("很早以前提过一台 ThinkPad 当备用机", kind="fact", importance=0.5)["id"]
    st3.db.execute(
        "UPDATE memories SET created_at=datetime('now','-400 day') WHERE id=?", (old,)
    )
    st3.db.commit()
    st3.consolidate()
    check(
        st3.db.execute("SELECT status FROM memories WHERE id=?", (old,)).fetchone()["status"]
        == "faded",
        "长期没用的条目已降为 faded",
    )

    strong = st3.recall("重庆 小面", limit=8)
    check(
        any(h["id"] == want for h in strong) and all(h["status"] != "faded" for h in strong),
        "强命中时不扫 faded（候选集只有活跃条目）",
    )
    weak = st3.recall("ThinkPad", limit=8)
    check(any(h["id"] == old for h in weak), "弱命中时兜底翻出 faded")

    # ---- 回归：faded 倒排索引（2026-10-28 换掉 LIKE 粗筛）----
    from core import tokenize as _tok

    calls: list[int] = []
    _orig = st3._faded_candidates

    def _spy(*a, **k):
        calls.append(1)
        return _orig(*a, **k)

    st3._faded_candidates = _spy
    st3.recall("", limit=6)
    check(not calls, "空查询不触发 faded 兜底")
    st3.recall("ThinkPad", limit=8)
    check(bool(calls), "非空弱查询仍会触发兜底")
    st3._faded_candidates = _orig

    _q = "ThinkPad"
    _qt = _tok(_q)
    _full = st3._pass(st3._fetch(("faded",), "", ""), st3._tf(_qt), _qt, 8, "", False)
    _rows = st3._faded_candidates(_qt, "", "")
    _tfs = {r["id"]: st3._tf(_tok(r["text"] + " " + r["tags"])) for r in _rows}
    _pre = st3._pass(
        _rows, st3._tf(_qt), _qt, 8, "", False,
        *st3._faded_corpus_stats(_qt, _tfs, ""), tfs=_tfs,
    )
    # 全扫会把 rel=0 但权重高的 faded 也排进来，索引候选不含它们；只比共同条目的相关度
    _full_rel = {x["id"]: x["relevance"] for x in _full}
    _pre_rel = {x["id"]: x["relevance"] for x in _pre}
    _shared = set(_full_rel) & set(_pre_rel)
    check(
        bool(_shared) and all(_full_rel[i] == _pre_rel[i] for i in _shared),
        f"索引候选与全扫的相关度一致（{len(_shared)} 条共同条目）",
    )

    esc = Store(str(Path(tempfile.mkdtemp()) / "esc.db"))
    esc.remember("dsh_memory 的配置放在 dsh 目录", kind="fact", importance=0.5)
    decoy = esc.remember("dshXmemory 是诱饵条目", kind="fact", importance=0.5)["id"]
    esc.db.execute("UPDATE memories SET status='faded'")
    esc.db.commit()
    esc._verify_token_index()
    _hit_ids = [r["id"] for r in esc._faded_candidates(_tok("dsh_memory"), "", "")]
    check(decoy not in _hit_ids, "token 精确匹配，不误伤 dshXmemory")

    # rel=0 的高权重 faded 不该被兜底带出来（全扫会按权重顺带排出几条噪音）
    noise = Store(str(Path(tempfile.mkdtemp()) / "noise.db"))
    noise.remember("完全无关的高权重条目：周末买菜做饭打扫卫生", kind="fact", importance=0.8)
    noise.remember("zenbookx1 的驱动版本锁定在 5.1.3", kind="fact", importance=0.5)
    noise.db.execute("UPDATE memories SET status='faded'")
    noise.db.commit()
    noise._verify_token_index()
    _noise_ids = [r["id"] for r in noise._faded_candidates(_tok("zenbookx1"), "", "")]
    check(len(_noise_ids) == 1, f"rel=0 的高权重 faded 不进候选（实得 {len(_noise_ids)} 条）")

    # ---- 回归：索引与派生数据的一致性 ----
    print("\n── faded 倒排索引 ──")
    idx = Store(str(Path(tempfile.mkdtemp()) / "idx.db"))
    idx.remember("索引一致性用的长期条目，提到 MCP 与 SQLite", kind="fact", importance=0.5)
    idx_texts = [
        "索引测试一：MCP 协议回环能过",
        "索引测试二：SQLite 开了 WAL 模式",
        "索引测试三：倒排索引要统计 df",
        "索引测试四：余弦相关度与 TF-IDF",
        "索引测试五：老库迁移要补列",
        "索引测试六：短期记忆按条数淘汰",
    ]
    faded_ids = []
    for text in idx_texts:
        mid = idx.remember(text, kind="fact", importance=0.4)["id"]
        faded_ids.append(mid)
    for mid in faded_ids:
        idx.db.execute("UPDATE memories SET status='faded' WHERE id=?", (mid,))
    idx.db.commit()
    idx._verify_token_index()

    brute: dict[str, int] = {}
    for r in idx.db.execute("SELECT id,text,tags FROM memories WHERE status='faded'"):
        for t in set(_tok(r["text"] + " " + r["tags"])):
            brute[t] = brute.get(t, 0) + 1
    got_df = {r["token"]: r["df"] for r in idx.db.execute("SELECT token,df FROM token_df")}
    check(got_df == brute, f"token_df 与暴力重算一致（{len(got_df)} 个 token）")

    tf_bad = []
    for r in idx.db.execute("SELECT id,text,tags FROM memories WHERE status='faded'"):
        want_tf = idx._tf(_tok(r["text"] + " " + r["tags"]))
        have_tf = {
            x["token"]: x["tf"]
            for x in idx.db.execute(
                "SELECT token,tf FROM mem_tokens WHERE memory_id=?", (r["id"],)
            )
        }
        if want_tf != have_tf:
            tf_bad.append(r["id"])
    check(not tf_bad, f"mem_tokens 的 tf 与重新 tokenize 一致（坏 {len(tf_bad)} 条）")

    idx.db.execute("DELETE FROM mem_tokens WHERE memory_id=?", (faded_ids[0],))
    idx.db.commit()
    idx._verify_token_index(full=True)
    healed = idx.db.execute(
        "SELECT COUNT(*) FROM mem_tokens WHERE memory_id=?", (faded_ids[0],)
    ).fetchone()[0]
    check(healed > 0, "索引被破坏后能自愈重建")

    idx.forget(faded_ids[1])
    gone = idx.db.execute(
        "SELECT COUNT(*) FROM mem_tokens WHERE memory_id=?", (faded_ids[1],)
    ).fetchone()[0]
    check(gone == 0, "forget 掉 faded 后索引同步清掉")

    idx.pin(faded_ids[2])
    gone2 = idx.db.execute(
        "SELECT COUNT(*) FROM mem_tokens WHERE memory_id=?", (faded_ids[2],)
    ).fetchone()[0]
    check(gone2 == 0, "pin 回 active 后索引同步清掉")

    idx.db.execute(
        "UPDATE memories SET status='faded', updated_at=datetime('now','-400 day'), "
        "last_used_at=NULL WHERE id=?", (faded_ids[2],)
    )
    idx.db.commit()
    idx._verify_token_index()
    rep_idx = idx.consolidate()
    check(
        idx.db.execute("SELECT COUNT(*) FROM memories WHERE id=?", (faded_ids[2],)).fetchone()[0]
        == 0 and rep_idx["purged"] >= 1,
        "过期 faded 被物理删除",
    )
    gone3 = idx.db.execute(
        "SELECT COUNT(*) FROM mem_tokens WHERE memory_id=?", (faded_ids[2],)
    ).fetchone()[0]
    check(gone3 == 0, "物理删除后索引同步清掉")

    # ---- 回归：长查询不能漏（曾按前 8 个 token 截断，命中词在末段就丢了）----
    lq = Store(str(Path(tempfile.mkdtemp()) / "longq.db"))
    tgt = lq.remember("旧机器的说明书放在哪里，我记得是第三个抽屉", kind="fact", importance=0.6)["id"]
    lq.db.execute("UPDATE memories SET status='faded' WHERE id=?", (tgt,))
    lq.db.commit()
    lq._verify_token_index()
    hit = lq.recall("量子咖啡机检修记录归档放在哪里", limit=8, reinforce=False)
    check(any(h["id"] == tgt for h in hit), "长查询（命中词在末段）仍能召回 faded")

    # ---- 回归：kind 过滤时 idf/unseen 与全扫一致 ----
    kd = Store(str(Path(tempfile.mkdtemp()) / "kinddf.db"))
    kd.remember("alpha 缓存命中率的记录", kind="fact", importance=0.5)
    kd.remember("alpha 缓存淘汰策略", kind="fact", importance=0.5)
    kd.remember("alpha 也是事件但 kind 不同", kind="event", importance=0.5)
    kd.db.execute("UPDATE memories SET status='faded'")
    kd.db.commit()
    kd._verify_token_index()
    kqt = _tok("alpha 缓存")
    krows = kd._faded_candidates(kqt, "fact", "")
    check(all(r["kind"] == "fact" for r in krows), "kind 过滤下候选不含其他 kind")
    ktfs = {r["id"]: kd._tf(_tok(r["text"] + " " + r["tags"])) for r in krows}
    got_idf, got_unseen = kd._faded_corpus_stats(kqt, ktfs, "fact")
    want_idf = kd._idf(list(ktfs.values()))
    check(got_idf == want_idf, "kind 过滤时 idf 与全扫逐字一致")
    check(abs(got_unseen - (math.log(3) + 1.0)) < 1e-12, "kind 过滤时 unseen 按该 kind 的池子算")

    # ---- 回归：兜底保底（强相关 faded 不被无关活跃条目挤掉）----
    rs = Store(str(Path(tempfile.mkdtemp()) / "rescue.db"))
    for i in range(8):
        rs.remember(f"第{i}条完全无关的日常记录，讲的是{i}号琐事", kind="fact", importance=0.85)
    target = rs.remember("zenbookx1 的驱动版本锁定在 5.1.3", kind="fact", importance=0.5)["id"]
    rs.db.execute("UPDATE memories SET status='faded' WHERE id=?", (target,))
    rs.db.commit()
    rs._verify_token_index()
    got = rs.recall("zenbookx1", limit=8, reinforce=False)
    check(any(h["id"] == target for h in got), "强相关的 faded 命中不会被无关活跃条目挤掉")

    # 反面：保底有上限，不会让 faded 把结果淹了
    rs2 = Store(str(Path(tempfile.mkdtemp()) / "rescue2.db"))
    for i in range(8):
        rs2.remember(f"第{i}条无关记录，讲的是{i}号琐事", kind="fact", importance=0.85)
    many = []
    for name in ("zenbookx1 的驱动是 5.1.3", "zenbookx1 装过两次", "zenbookx1 在旧机器上"):
        mid = rs2.remember(name, kind="fact", importance=0.4)["id"]
        many.append(mid)
    for mid in many:
        rs2.db.execute("UPDATE memories SET status='faded' WHERE id=?", (mid,))
    rs2.db.commit()
    rs2._verify_token_index()
    got2 = [h for h in rs2.recall("zenbookx1", limit=8, reinforce=False) if h["id"] in many]
    check(len(got2) == 2, f"保底最多放 2 条 faded（实得 {len(got2)}）")

    # ---- 老库迁移 ----
    print("\n── 老库迁移 ──")
    import sqlite3 as _sq

    old_path = Path(tempfile.mkdtemp()) / "old.db"
    con = _sq.connect(str(old_path))
    con.execute(
        """CREATE TABLE memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL, topic TEXT NOT NULL DEFAULT '其他',
            text TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '[]',
            importance REAL NOT NULL DEFAULT 0.5, confidence REAL NOT NULL DEFAULT 0.8,
            source TEXT NOT NULL DEFAULT 'user_said', status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_used_at TEXT,
            use_count INTEGER NOT NULL DEFAULT 0, superseded_by INTEGER,
            note TEXT NOT NULL DEFAULT '')"""
    )
    con.execute(
        "INSERT INTO memories(uid,kind,text,created_at,updated_at) "
        "VALUES ('old1','fact','迁移前就存在的一条',datetime('now'),datetime('now'))"
    )
    con.commit()
    con.close()

    st_old = Store(str(old_path))
    cols = {r["name"] for r in st_old.db.execute("PRAGMA table_info(memories)")}
    from core import MIGRATIONS as _MIG

    want = {name for name, _ in _MIG}
    check(want <= cols, f"老库自动补齐 {len(want)} 列（缺 {sorted(want - cols)}）")
    kept = st_old.db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    check(kept == 1, "迁移不动老数据")

    print(f"\n{'全部通过' if fails == 0 else f'{fails} 项失败'}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
