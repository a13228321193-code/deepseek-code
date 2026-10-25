"""SessionStart 钩子：新会话一建立，就把完整开场包塞进去。

**不依赖模型自觉。** 桥接的实现是：

    ctx.on("agent/created", async ({ agent, source, signal }) => {
      const run = runPoint("SessionStart", source, sessionStartPayload(agent, source), {...})
        .then((merged) => {
          const context = contextFrom(merged);
          if (context) agent.inject(context);   // ← 直接注入会话
        });
      detached.track(run);
      await run;
    });

也就是说 SessionStart 的 additionalContext 会由桥接主动 agent.inject()，
比我"记得去调 recall"可靠得多。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

HOME = Path(os.path.expanduser("~"))
DB = Path(os.environ.get("DSH_MEMORY_DB") or (HOME / ".dsh" / "memory" / "memory.db"))


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    except Exception:
        pass
    try:
        sys.stdin.buffer.read()          # 吃掉 payload，避免桥接等我们读
    except Exception:
        pass

    if not DB.exists():
        print("{}")
        return 0

    try:
        from core import Store
        from packfmt import render_pack

        text = render_pack(Store(str(DB)).startup_pack())
    except Exception as e:
        sys.stderr.write(f"hook_session: {type(e).__name__}: {e}\n")
        print("{}")
        return 0

    if not text.strip():
        print("{}")
        return 0

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": text,
        }
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
