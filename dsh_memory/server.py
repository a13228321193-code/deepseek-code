"""server —— 极简 MCP stdio 服务，把记忆暴露成工具。

协议：MCP over stdio，每条消息是一行 JSON-RPC 2.0。
只实现 initialize / tools/list / tools/call / ping，够 DSH 用。

stdout 只能输出 JSON 消息，任何 debug 一律不许 print（否则客户端解析失败）。
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import KINDS, TOPICS, VIEWS, Rejected, Store  # noqa: E402
from packfmt import fmt, render_pack  # noqa: E402

SERVER_INFO = {"name": "dsh-memory", "version": "0.1.0"}
DEFAULT_PROTOCOL = "2024-11-05"

DB_PATH = os.environ.get(
    "DSH_MEMORY_DB",
    str(Path(os.path.expanduser("~")) / ".dsh" / "memory" / "memory.db"),
)

TOOLS = [
    {
        "name": "recall",
        "description": (
            "检索长期记忆。query 留空 = 返回'开场包'（全部偏好 + 权重最高的事实），"
            "适合会话开始或需要了解用户时调用。查历史细节时给 query。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索用的短句；留空取开场包"},
                "limit": {"type": "integer", "description": "返回条数，默认 8"},
                "kind": {"type": "string", "enum": list(KINDS)},
                "topic": {"type": "string", "enum": list(TOPICS)},
            },
            "required": [],
        },
    },
    {
        "name": "remember",
        "description": (
            "写入一条长期记忆。四类：稳定偏好、关于用户的硬事实、已定下的决定、发生过的事(event)。"
            "一条只讲一件事，必须含可检索的锚（专名/路径/数字/结论）。不要写过程和寒暄。"
            "event 要带 view/emotion/sensory；其余三类不用。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "一句话，含主语和锚点"},
                "kind": {
                    "type": "string",
                    "enum": list(KINDS),
                    "description": (
                        "profile=每轮都要生效的偏好/称呼/禁忌(永不衰减，进开场包) "
                        "permanent=永久记忆(永不衰减，**不进开场包**，只在该被想起时浮现) "
                        "fact=硬事实 decision=决定 event=发生过的事 "
                        "short=最近发生的事(约7个活跃日后淡出，进开场包)"
                    ),
                },
                "topic": {"type": "string", "enum": list(TOPICS)},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "2-4 个关键词"},
                "importance": {"type": "number", "description": "0-1，>=0.9 会被钉住不衰减"},
                "source": {
                    "type": "string",
                    "enum": ["user_said", "inferred", "tool"],
                    "description": "inferred 表示你推断的，会强制降低置信度",
                },
                "view": {
                    "type": "string",
                    "enum": list(VIEWS),
                    "description": "这条经历属于谁：self=我这边的 user=他告诉我的他的经历 both=我们之间",
                },
                "emotion": {
                    "type": "string",
                    "description": "一个词。view=user 时只能填他明说或明显表现出的，编不出来就留空，不要猜",
                },
                "sensory": {
                    "type": "string",
                    "description": "一个具体细节：画面/声音/时间点/原话。不要写'氛围很好'这种形容词",
                },
                "due": {
                    "type": "string",
                    "description": (
                        "日期唤醒。写 '2027-06-18' 表示一次性，写 '06-18' 表示每年重复（只支持公历）。"
                        "到窗口期开场包会主动提一句，不用我记着。"
                    ),
                },
                "lead": {
                    "type": "integer",
                    "description": "提前多少天开始提醒，默认 7。重要的日子可以给 14~30，一般的事给 3~7",
                },
            },
            "required": ["text", "kind"],
        },
    },
    {
        "name": "forget",
        "description": "纠正记忆。用户说'记错了/不对'时调用，把该条标记为错误并停止参与检索。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "reason": {"type": "string"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "pin",
        "description": "钉住/取消钉住一条记忆。钉住的条目不参与时间衰减，也不会被上限截断。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "pinned": {"type": "boolean"},
            },
            "required": ["id"],
        },
    },
]


def call_tool(store: Store, name: str, args: dict) -> str:
    if name == "recall":
        q = (args.get("query") or "").strip()
        kind = args.get("kind") or ""
        topic = args.get("topic") or ""
        if not q and not kind and not topic:
            return render_pack(store.startup_pack())
        hits = store.recall(q, int(args.get("limit") or 8), kind, topic)
        return fmt(hits)

    if name == "remember":
        res = store.remember(
            text=args.get("text", ""),
            kind=args.get("kind", "fact"),
            topic=args.get("topic", "其他"),
            tags=args.get("tags") or [],
            importance=float(args.get("importance", 0.5)),
            source=args.get("source", "user_said"),
            view=args.get("view", ""),
            emotion=args.get("emotion", ""),
            sensory=args.get("sensory", ""),
            due=args.get("due", ""),
            lead=int(args.get("lead") or 7),
        )
        if res.get("merged_into"):
            # 别写死文案：core 会区分"重复加权"和"短期状态更新"两种情况
            note = res.get("note") or "与已有记忆重复，已合并"
            return f"#{res['merged_into']} —— {note}"
        msg = f"已记住 #{res['id']}"
        if res.get("superseded"):
            msg += f"，旧值 #{res['superseded']} 被取代（保留，可追溯）"
        if res.get("pinned"):
            msg += "，已钉住（不再随时间衰减）"
        if res.get("due"):
            kind_cn = "每年" if len(res["due"]) == 5 else "一次性"
            msg += f"，{kind_cn} {res['due']}，提前 {res['lead']} 天开始提醒"
        return msg

    if name == "forget":
        res = store.forget(int(args["id"]), args.get("reason", "用户说记错了"))
        return f"#{res['id']} 已标记为错误，不再参与检索"

    if name == "pin":
        res = store.pin(int(args["id"]), bool(args.get("pinned", True)))
        return f"#{res['id']} " + ("已钉住" if res["pinned"] else "已取消钉住")

    raise Rejected(f"未知工具 {name}")


def handle(store: Store, msg: dict) -> dict | None:
    method = msg.get("method")
    mid = msg.get("id")
    params = msg.get("params") or {}

    if mid is None:  # notification，不回
        return None

    if method == "initialize":
        proto = params.get("protocolVersion") or DEFAULT_PROTOCOL
        return {
            "jsonrpc": "2.0",
            "id": mid,
            "result": {
                "protocolVersion": proto,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}

    if method in ("resources/list", "resources/templates/list"):
        return {"jsonrpc": "2.0", "id": mid, "result": {"resources": [], "resourceTemplates": []}}

    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"prompts": []}}

    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments") or {}
        try:
            text = call_tool(store, name, args)
            return {
                "jsonrpc": "2.0",
                "id": mid,
                "result": {"content": [{"type": "text", "text": text}], "isError": False},
            }
        except Rejected as e:
            return {
                "jsonrpc": "2.0",
                "id": mid,
                "result": {"content": [{"type": "text", "text": f"拒绝：{e}"}], "isError": True},
            }
        except Exception as e:  # 工具内部错误也走 isError，不让整个会话崩
            return {
                "jsonrpc": "2.0",
                "id": mid,
                "result": {
                    "content": [{"type": "text", "text": f"出错：{type(e).__name__}: {e}"}],
                    "isError": True,
                },
            }

    return {
        "jsonrpc": "2.0",
        "id": mid,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def main() -> int:
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    store = Store(DB_PATH)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            resp = handle(store, msg)
        except Exception:
            resp = {
                "jsonrpc": "2.0",
                "id": msg.get("id"),
                "error": {"code": -32603, "message": traceback.format_exc(limit=3)},
            }
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
