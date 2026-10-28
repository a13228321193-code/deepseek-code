"""core —— 长期记忆的存储、检索、衰减。

规则见 README。四个 kind 的生命周期不一样，这是整套设计的根：

    profile   偏好/称呼/禁忌     永不衰减，只能被新值取代或被用户撤销
    fact      关于用户的硬事实    半衰期 90 天
    decision  定下来的决定        半衰期 30 天
    event     发生过的事          半衰期 14 天

写入前有三问门槛 + 敏感信息拦截；遗忘是分级降级而不是删除。
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

# 各类型的半衰期（天）。None = 永不衰减。
# short 的单位是「活跃日」，不是墙钟天。
# 2.65 这个数是反推的：importance≈0.75 时 0.75×0.5^(n/2.65) 在第 7 个活跃日跌破 0.12，
# 也就是「一条典型的短期记忆活 7 个活跃日」。改寿命就改这个数：半衰期 = 天数 / 2.65。
HALF_LIFE: dict[str, float | None] = {
    "profile": None,      # 常驻：每轮都注入，必须一直在
    "permanent": None,    # 永久：不衰减，但**不进开场包**，只在被问到/命中时才浮现
    "fact": 90.0,
    "decision": 30.0,
    "event": 14.0,
    "short": 2.65,
}

KINDS = tuple(HALF_LIFE)
TOPICS = ("偏好", "项目", "工具", "人物", "待办", "其他")

FADE_BELOW = 0.12          # 权重低于此值 -> faded
PIN_IMPORTANCE = 0.9       # 重要性到此 -> 钉住，不自动衰减
MAX_ACTIVE = 300           # 活跃条目上限
SHORT_MAX = 25             # 短期缓冲区的条数上限（超出淘汰最旧）
SHORT_MAX_ACTIVE_DAYS = 3  # 短期条目的活跃日寿命上限（超过直接 faded）
SHORT_IN_PACK = 8          # 开场包里给全文的短期条数；更早的只汇总成一行
PROMOTE_USE_COUNT = 3      # 被提及这么多次就晋升，不看当时权重
FADED_FALLBACK_REL = 0.15  # 主检索的最好成绩低于此值，才去翻 faded 兜底
FADED_CANDIDATE_BUDGET = 2000  # 兜底候选上限：按 df 从小到大取 token，累计命中数超此值就停
FADED_RESCUE_MIN_REL = 0.15  # 兜底命中相关度到这一档，才允许挤掉活跃条目
FADED_RESCUE_MAX = 2         # 一次兜底最多保底放进几条 faded
HARD_DELETE_DAYS = 180     # faded 且从未再被用到，超过此天数才物理删除
REINFORCE = 0.15           # 每次命中加权的幅度
REINFORCE_MIN_REL = 0.10   # 相关度低于此值：只是被"顺带捞出来"，不算想起来，不加权
CONSOLIDATE_INTERVAL = 12 * 3600
ZOMBIE_DAYS = 30           # short 的墙钟兜底：再怎么说，30 天前的"正在忙"也该过期了

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    uid           TEXT NOT NULL UNIQUE,
    kind          TEXT NOT NULL,
    topic         TEXT NOT NULL DEFAULT '其他',
    text          TEXT NOT NULL,
    tags          TEXT NOT NULL DEFAULT '[]',
    importance    REAL NOT NULL DEFAULT 0.5,
    confidence    REAL NOT NULL DEFAULT 0.8,
    source        TEXT NOT NULL DEFAULT 'user_said',
    status        TEXT NOT NULL DEFAULT 'active',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    last_used_at  TEXT,
    use_count     INTEGER NOT NULL DEFAULT 0,
    superseded_by INTEGER,
    note          TEXT NOT NULL DEFAULT '',
    -- 情节三件套：只对 kind=event 有意义
    view          TEXT NOT NULL DEFAULT '',
    emotion       TEXT NOT NULL DEFAULT '',
    sensory       TEXT NOT NULL DEFAULT '',
    -- short 专用的"出生活跃日"，用来按活跃日而不是墙钟算年龄
    born_active   INTEGER NOT NULL DEFAULT 0,
    -- 日期唤醒：due_date 形如 "2027-06-18"（一次）或 "06-18"（每年）
    due_date      TEXT NOT NULL DEFAULT '',
    due_lead      INTEGER NOT NULL DEFAULT 7,   -- 提前多少天开始提醒
    due_stage     TEXT NOT NULL DEFAULT ''      -- 已经提醒过哪一档，防重复
);
CREATE INDEX IF NOT EXISTS idx_mem_kind   ON memories(kind);
CREATE INDEX IF NOT EXISTS idx_mem_status ON memories(status);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
-- faded 兜底的倒排索引。只索引 status='faded' 的条目：活跃条目 ≤ MAX_ACTIVE 条，
-- 直接全扫比维护索引便宜。索引是派生数据，任何时候都能从 memories 重建。
CREATE TABLE IF NOT EXISTS mem_tokens (
    memory_id INTEGER NOT NULL,
    token     TEXT NOT NULL,
    tf        INTEGER NOT NULL,
    PRIMARY KEY (memory_id, token)
);
CREATE INDEX IF NOT EXISTS idx_tok_token ON mem_tokens(token);
CREATE TABLE IF NOT EXISTS token_df (
    token TEXT PRIMARY KEY,
    df    INTEGER NOT NULL
);
"""

# 依赖后加列的索引，必须等 _migrate() 补完列再建 ——
# 否则老库上 CREATE INDEX ... (due_date) 会先崩。
POST_MIGRATE_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_mem_due ON memories(due_date)",
]

# 老库迁移：SQLite 只支持 ADD COLUMN，够用
MIGRATIONS = [
    ("view", "ALTER TABLE memories ADD COLUMN view TEXT NOT NULL DEFAULT ''"),
    ("emotion", "ALTER TABLE memories ADD COLUMN emotion TEXT NOT NULL DEFAULT ''"),
    ("sensory", "ALTER TABLE memories ADD COLUMN sensory TEXT NOT NULL DEFAULT ''"),
    ("born_active", "ALTER TABLE memories ADD COLUMN born_active INTEGER NOT NULL DEFAULT 0"),
    ("due_date", "ALTER TABLE memories ADD COLUMN due_date TEXT NOT NULL DEFAULT ''"),
    ("due_lead", "ALTER TABLE memories ADD COLUMN due_lead INTEGER NOT NULL DEFAULT 7"),
    ("due_stage", "ALTER TABLE memories ADD COLUMN due_stage TEXT NOT NULL DEFAULT ''"),
]

VIEWS = ("self", "user", "both")

# 敏感信息：写入层直接拒，不依赖自觉
SENSITIVE = [
    (re.compile(r"sk-[A-Za-z0-9_\-]{16,}"), "疑似 API key"),
    (re.compile(r"(?i)\b(api[_-]?key|secret|passwd|password|token)\b\s*[:=]\s*\S+"), "疑似凭据"),
    (re.compile(r"\b\d{17}[\dXx]\b"), "疑似身份证号"),
    (re.compile(r"\b(?:\d[ -]?){16,19}\b"), "疑似银行卡号"),
    (re.compile(r"(?i)-----BEGIN [A-Z ]*PRIVATE KEY-----"), "私钥"),
]


class Rejected(Exception):
    pass


def tokenize(text: str) -> list[str]:
    """中文按单字 + 二字组，英文按词。不用 FTS5：它对中文短词的切分不可靠。"""
    text = text.lower()
    out = re.findall(r"[a-z0-9_]+", text)
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        out.extend(run)
        out.extend(run[i : i + 2] for i in range(len(run) - 1))
    return out


def _now() -> float:
    return time.time()


def _iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or _now(), timezone.utc).astimezone().isoformat(timespec="seconds")


def _today() -> str:
    """本地日期。活跃日按本地日划分，不用 UTC —— 用的人是本地的。"""
    return datetime.now().strftime("%Y-%m-%d")


# ---------------- 日期唤醒 ----------------

def _norm_due(s: str) -> str:
    """规范日期。返回 'YYYY-MM-DD'（一次性）或 'MM-DD'（每年），认不出来返回 ''。

    接受 6-18 / 06-18 / 2027-6-18 / 2027年6月18日 / 2027/6/18 这些写法。
    """
    if not s:
        return ""
    t = str(s).strip()
    for ch in ("年", "月", "日", "/", "."):
        t = t.replace(ch, "-")
    t = t.strip("-")
    parts = [p.strip() for p in t.split("-")]
    if any(p == "" for p in parts):
        return ""
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return ""
    if len(nums) == 2:
        m, d = nums
        if 1 <= m <= 12 and 1 <= d <= 31:
            return f"{m:02d}-{d:02d}"
    elif len(nums) == 3:
        y, m, d = nums
        if 1900 <= y <= 2200 and 1 <= m <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{m:02d}-{d:02d}"
    return ""


def _next_due(due: str, today: date) -> date | None:
    """下一次发生的日期。一次性的过期了返回 None，每年重复的取今年或明年。"""
    if not due:
        return None
    if len(due) == 10:
        try:
            d0 = date.fromisoformat(due)
        except ValueError:
            return None
        return d0 if d0 >= today else None
    try:
        m, d = int(due[:2]), int(due[3:])
    except ValueError:
        return None
    for year in (today.year, today.year + 1):
        try:
            d0 = date(year, m, d)
        except ValueError:  # 2 月 29 日落在平年
            continue
        if d0 >= today:
            return d0
    return None


def _stage_of(left: int) -> str:
    """把"还剩几天"压成几档，同一档只提醒一次。"""
    if left <= 0:
        return "0"
    if left == 1:
        return "1"
    if left <= 3:
        return "3"
    return "lead"


def _age_days(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return 0.0
    return max(0.0, (_now() - dt.timestamp()) / 86400.0)


class Store:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), timeout=10)
        self.db.row_factory = sqlite3.Row
        # WAL：多个 dsh 实例（web + headless）同时挂这个库时读写不互相堵塞。
        # 这是数据库文件级设置，设一次永久生效。
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()

    def _migrate(self) -> None:
        """给老库补列。CREATE TABLE IF NOT EXISTS 不会改已存在的表。"""
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(memories)")}
        added: set[str] = set()
        for name, ddl in MIGRATIONS:
            if name not in cols:
                self.db.execute(ddl)
                added.add(name)
        # 第一次加 born_active 时把活跃日时钟对齐到今天：
        # 老数据一律算"今天出生"，不能因为迁移就凭空老掉一天。
        if "born_active" in added:
            self._set_meta("active_day", "0")
            self._set_meta("last_active_date", _today())
        # 列补齐之后才能建依赖它们的索引
        for sql in POST_MIGRATE_SQL:
            self.db.execute(sql)
        # 倒排索引是派生数据：对不上就整表重建。老库第一次用新代码也走这里。
        self._verify_token_index()

    # ---------------- 基础 ----------------

    def _meta(self, k: str, default: str = "") -> str:
        row = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row["v"] if row else default

    def _set_meta(self, k: str, v: str) -> None:
        self.db.execute(
            "INSERT INTO meta(k,v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (k, v),
        )
        self.db.commit()

    def _decay(self, kind: str, age_days: float) -> float:
        hl = HALF_LIFE.get(kind, 14.0)
        if hl is None:
            return 1.0
        return 0.5 ** (age_days / hl)

    # ---------------- 活跃日 ----------------
    # short 的年龄按「活跃日」算，不按墙钟：你不在的那些天，它不该变老。

    def _active_day(self) -> int:
        return int(self._meta("active_day", "0") or 0)

    def _tick(self) -> None:
        """每次读写记忆时调一次。跨本地日 -> active_day +1。

        同一进程里一天调 N 次也只加 1。漏调的结果是"记忆不老"，方向是安全的。
        """
        today = _today()
        if self._meta("last_active_date", "") != today:
            self._set_meta("active_day", str(self._active_day() + 1))
            self._set_meta("last_active_date", today)

    def _age_of(self, row: sqlite3.Row) -> float:
        """衰减用的年龄。

        short  -> 活跃日（你不在的日子里它不变老）
        其余   -> 墙钟天数，但**从最后一次被想起算起**，不是从写下那天算起。

        后一条是「续命」：一条长期记忆每次被想起来，就重新变年轻一次。
        反复被想起来的主题因此不会过期，只有真正没人再提的才会老死。
        """
        if row["kind"] == "short":
            return max(0.0, float(self._active_day() - (row["born_active"] or 0)))
        return _age_days(row["last_used_at"] or row["created_at"])

    def weight(self, row: sqlite3.Row) -> float:
        """当前权重 = 重要度 × 衰减。

        三个"不衰减"的口径，原来只有一个是真的：
          * kind=profile / kind=permanent —— 半衰期为 None，真不衰减
          * status=pinned —— **修了个 bug**：以前只做到了"不删除、不被上限截断"，
            权重照样往下掉，和注释写的"钉住不自动衰减"对不上。现在真的豁免衰减。

        short 额外加一道墙钟兜底：再怎么说，30 天前的"正在忙"也该过期了。
        钉住的条目不受这条限制 —— 你明确要留住的东西，不该被兜底清掉。
        """
        if row["status"] == "pinned" or HALF_LIFE.get(row["kind"]) is None:
            return row["importance"]
        if row["kind"] == "short" and _age_days(row["created_at"]) > ZOMBIE_DAYS:
            return 0.0
        return row["importance"] * self._decay(row["kind"], self._age_of(row))

    # ---------------- 写入 ----------------

    def _check_sensitive(self, text: str) -> None:
        for pat, why in SENSITIVE:
            if pat.search(text):
                raise Rejected(f"拒绝写入：{why}。这类内容不落库，请只记'有过某个凭据'，不要记值。")

    def remember(
        self,
        text: str,
        kind: str = "fact",
        topic: str = "其他",
        tags: list[str] | None = None,
        importance: float = 0.5,
        confidence: float = 0.8,
        source: str = "user_said",
        view: str = "",
        emotion: str = "",
        sensory: str = "",
        due: str = "",
        lead: int = 7,
    ) -> dict:
        text = (text or "").strip()
        if not text:
            raise Rejected("记忆内容为空。")
        if kind not in KINDS:
            raise Rejected(f"kind 必须是 {KINDS} 之一。")
        if topic not in TOPICS:
            topic = "其他"
        self._check_sensitive(text)
        self._tick()

        # W1-1/W2：太短或没有任何可检索锚点的，不写
        if len(text) < 8:
            raise Rejected("太短，一个月后看不懂。补全主语和细节再写。")
        if not re.search(r"[A-Za-z0-9_\\/:.#\-]{2,}|[\u4e00-\u9fff]{4,}", text):
            raise Rejected("没有可检索的锚点（专名/路径/数字/结论）。")

        tags = [str(t).strip() for t in (tags or []) if str(t).strip()][:6]
        importance = max(0.0, min(1.0, float(importance)))
        confidence = max(0.0, min(1.0, float(confidence)))
        if source == "inferred":
            confidence = min(confidence, 0.5)  # 推断的不能当事实讲

        # 情节三件套：只收短字段，超长截断，别让它变成第二段正文
        view = (view or "").strip().lower()
        if view not in VIEWS:
            view = ""
        emotion = (emotion or "").strip()[:12]
        sensory = (sensory or "").strip()[:80]
        # 视角不明却写了情绪，等于不知道是谁的情绪 —— 丢掉，别存糊涂账
        if not view:
            emotion = ""

        # 日期唤醒：写错日期比不写更糟，直接拒
        due_norm = ""
        if due:
            due_norm = _norm_due(due)
            if not due_norm:
                raise Rejected(f"日期认不出来：{due}。用 '2027-06-18'（一次）或 '06-18'（每年）。")
        try:
            lead_days = max(1, min(90, int(lead)))
        except (TypeError, ValueError):
            lead_days = 7

        # W5：近重复合并 + 强化
        dup = self._find_duplicate(text, kind)
        if dup:
            merged_tags = json.dumps(
                sorted(set(json.loads(dup["tags"])) | set(tags)), ensure_ascii=False
            )
            if kind == "short":
                # 短期的"重复"不是又提了一次，是**状态更新**：直接换掉正文。
                # 关键是别动 use_count / 不要 +0.10 —— 否则每轮更新都会把它
                # 顶到 3 次触发晋升，一夜之间所有短期记忆都变成永久。
                self.db.execute(
                    "UPDATE memories SET text=?, updated_at=?, born_active=?, tags=?, "
                    "importance=MAX(importance, ?) WHERE id=?",
                    (text, _iso(), self._active_day(), merged_tags, importance, dup["id"]),
                )
                self.db.commit()
                return {"id": None, "merged_into": dup["id"], "note": "短期记忆状态已更新"}
            self.db.execute(
                "UPDATE memories SET text=?, importance=MIN(1.0, importance+0.10), "
                "updated_at=?, use_count=use_count+1, tags=? WHERE id=?",
                (max(dup["text"], text, key=len), _iso(), merged_tags, dup["id"]),
            )
            self.db.commit()
            return {"id": None, "merged_into": dup["id"], "note": "与已有记忆重复，已合并并加权"}

        # W4：同 key 冲突 -> 旧的标 superseded，不删
        superseded = None
        if kind in ("profile", "fact"):
            key = self._key_of(text)
            if key:
                old = self.db.execute(
                    "SELECT id,text FROM memories WHERE status='active' AND kind=? AND text LIKE ? "
                    "AND id<>0 ORDER BY updated_at DESC LIMIT 1",
                    (kind, key + "%"),
                ).fetchone()
                if old and self._cos(tokenize(old["text"]), tokenize(text)) > 0.5:
                    superseded = old["id"]

        uid = uuid.uuid4().hex[:12]
        ts = _iso()
        cur = self.db.execute(
            "INSERT INTO memories(uid,kind,topic,text,tags,importance,confidence,source,"
            "status,created_at,updated_at,use_count,view,emotion,sensory,born_active,"
            "due_date,due_lead) VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?,?)",
            (uid, kind, topic, text, json.dumps(tags, ensure_ascii=False),
             importance, confidence, source,
             "pinned" if importance >= PIN_IMPORTANCE else "active", ts, ts,
             view, emotion, sensory, self._active_day(), due_norm, lead_days),
        )
        new_id = cur.lastrowid
        if superseded:
            self.db.execute(
                "UPDATE memories SET status='superseded', superseded_by=? WHERE id=?",
                (new_id, superseded),
            )
        self.db.commit()
        self._maybe_consolidate()
        return {
            "id": new_id,
            "superseded": superseded,
            "pinned": importance >= PIN_IMPORTANCE,
            "due": due_norm,
            "lead": lead_days,
        }

    @staticmethod
    def _key_of(text: str) -> str:
        m = re.match(r"^([^，。；,;:：]{2,14})", text)
        return m.group(1).strip() if m else ""

    def _find_duplicate(self, text: str, kind: str) -> sqlite3.Row | None:
        # short 的阈值单独定，0.40。实测同一项目的两条"当前状态"余弦在 0.47~0.69，
        # 不同话题的两条只有 0.08~0.24 —— 中间有明确间隔，0.40 正好落在缝里。
        # 0.90 会把"状态变了"判成两条新记忆，旧的永远盖不掉；0.75 也不够，真实两条就落在 0.69。
        threshold = 0.40 if kind == "short" else 0.90
        toks = tokenize(text)
        for r in self.db.execute(
            "SELECT * FROM memories WHERE status IN ('active','pinned') AND kind=?", (kind,)
        ):
            if self._cos(toks, tokenize(r["text"])) > threshold:
                return r
        return None

    # ---------------- 检索 ----------------

    @staticmethod
    def _cos(a: list[str], b: list[str]) -> float:
        if not a or not b:
            return 0.0
        da, db_ = {}, {}
        for t in a:
            da[t] = da.get(t, 0) + 1
        for t in b:
            db_[t] = db_.get(t, 0) + 1
        dot = sum(v * db_.get(k, 0) for k, v in da.items())
        if not dot:
            return 0.0
        na = math.sqrt(sum(v * v for v in da.values()))
        nb = math.sqrt(sum(v * v for v in db_.values()))
        return dot / (na * nb) if na and nb else 0.0

    # --- IDF 加权 ---
    # 纯词频的问题是「用户」「了」「什么」这类高频词和「大肥鱼」「重庆」权重一样，
    # 查询里的虚词会把区分度磨平。IDF 让罕见词变重、常见词变轻。
    # 只在 query→doc 的检索里用；doc→doc 的去重/合并仍走裸词频（两边都是文档，公
    # 共词会自然抵消，套 IDF 反而容易误判）。

    @staticmethod
    def _tf(tokens: list[str]) -> dict[str, int]:
        d: dict[str, int] = {}
        for t in tokens:
            d[t] = d.get(t, 0) + 1
        return d

    @staticmethod
    def _idf(tfs: list[dict[str, int]]) -> dict[str, float]:
        n = max(1, len(tfs))
        df: dict[str, int] = {}
        for tf in tfs:
            for t in tf:
                df[t] = df.get(t, 0) + 1
        return {t: math.log((n + 1) / (c + 0.5)) + 1.0 for t, c in df.items()}

    @staticmethod
    def _cos_idf(
        qt: dict[str, int], dt: dict[str, int], idf: dict[str, float], unseen: float
    ) -> float:
        qv = {t: c * idf.get(t, unseen) for t, c in qt.items()}
        dv = {t: c * idf.get(t, unseen) for t, c in dt.items()}
        if not qv or not dv:
            return 0.0
        if len(qv) > len(dv):
            qv, dv = dv, qv
        dot = sum(v * dv.get(k, 0.0) for k, v in qv.items())
        if not dot:
            return 0.0
        na = math.sqrt(sum(v * v for v in qv.values()))
        nb = math.sqrt(sum(v * v for v in dv.values()))
        return dot / (na * nb) if na and nb else 0.0

    # ---------------- faded 倒排索引 ----------------
    # 兜底检索需要「整个 faded 池」的 df/idf，但不需要每次 tokenize 全池。所以把
    # faded 条目的 token 与 tf 存成索引，df 另存一张计数表。只索引 faded：
    # 活跃条目有 MAX_ACTIVE 上限，全扫本来就便宜。
    # 索引是派生数据 —— 任何不一致都能从 memories 整表重建，不会丢东西。

    def _index_add(self, memory_id: int, text: str, tags: str) -> None:
        tfs = self._tf(tokenize(text + " " + tags))
        if not tfs:
            return
        self.db.executemany(
            "INSERT OR REPLACE INTO mem_tokens(memory_id,token,tf) VALUES (?,?,?)",
            [(memory_id, t, c) for t, c in tfs.items()],
        )
        self.db.executemany(
            "INSERT INTO token_df(token,df) VALUES (?,1) "
            "ON CONFLICT(token) DO UPDATE SET df=df+1",
            [(t,) for t in tfs],
        )

    def _index_remove(self, memory_id: int) -> None:
        toks = [
            r["token"]
            for r in self.db.execute(
                "SELECT token FROM mem_tokens WHERE memory_id=?", (memory_id,)
            )
        ]
        if not toks:
            return
        self.db.execute("DELETE FROM mem_tokens WHERE memory_id=?", (memory_id,))
        self.db.executemany(
            "UPDATE token_df SET df=df-1 WHERE token=?", [(t,) for t in toks]
        )

    def _prune_token_df(self) -> None:
        """清掉 df 归零的 token。不影响正确性（查询都按 df>0 过滤），只是别让词表无限攒。"""
        self.db.execute("DELETE FROM token_df WHERE df<=0")

    def _reindex(self, memory_id: int) -> None:
        """改过 text/tags 或 status 之后调一次。索引只收 faded，离开 faded 等于只删不加。"""
        had = self.db.execute(
            "SELECT 1 FROM mem_tokens WHERE memory_id=? LIMIT 1", (memory_id,)
        ).fetchone()
        self._index_remove(memory_id)
        row = self.db.execute(
            "SELECT id,text,tags,status FROM memories WHERE id=?", (memory_id,)
        ).fetchone()
        now = bool(row and row["status"] == "faded")
        if now:
            self._index_add(row["id"], row["text"], row["tags"])
        delta = int(now) - int(bool(had))
        if delta:
            self._bump_index_docs(delta)

    def _bump_index_docs(self, delta: int) -> None:
        """按差值维护「索引收了多少个 faded 文档」，避免每次重新 COUNT。"""
        cur = int(self._meta("token_index_docs", "0") or 0)
        n = max(0, cur + delta)
        self.db.execute(
            "INSERT INTO meta(k,v) VALUES ('token_index_docs',?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (str(n),),
        )

    def _sync_index_meta(self) -> None:
        """记下「索引收了多少个 faded 文档」，_verify_token_index 拿它做廉价体检。"""
        n = self.db.execute(
            "SELECT COUNT(*) FROM memories WHERE status='faded'"
        ).fetchone()[0]
        self.db.execute(
            "INSERT INTO meta(k,v) VALUES ('token_index_docs',?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (str(n),),
        )

    def _rebuild_token_index(self) -> None:
        self.db.execute("DELETE FROM mem_tokens")
        self.db.execute("DELETE FROM token_df")
        for r in self.db.execute("SELECT id,text,tags FROM memories WHERE status='faded'"):
            self._index_add(r["id"], r["text"], r["tags"])
        self._sync_index_meta()

    def _verify_token_index(self, full: bool = False) -> None:
        """体检：文档数对不上必重建；full=True 时再核行数（consolidate 里用）。"""
        want = self.db.execute(
            "SELECT COUNT(*) FROM memories WHERE status='faded'"
        ).fetchone()[0]
        have = int(self._meta("token_index_docs", "-1") or -1)
        bad = have != want
        if not bad and full:
            rows = self.db.execute("SELECT COUNT(*) FROM mem_tokens").fetchone()[0]
            df_sum = self.db.execute(
                "SELECT COALESCE(SUM(df),0) FROM token_df"
            ).fetchone()[0]
            bad = rows != df_sum
        if bad:
            self._rebuild_token_index()

    # ---------------- faded 兜底检索 ----------------

    def _faded_corpus_stats(
        self, qt: list[str], tfs: dict[int, dict[str, int]], kind: str
    ) -> tuple[dict[str, float], float]:
        """全 faded 池的 df/idf，df 从索引直接读，不再扫表。

        这样算出的 idf 与「全扫」逐位相同 —— 候选可以只算几条，分数不变。
        带 kind 时 df 必须同样收窄到该 kind，所以走一次命中索引的 join 统计。
        """
        tokens = list(dict.fromkeys(qt + [t for tf in tfs.values() for t in tf]))
        if not tokens:
            return {}, 1.0
        ph = ",".join("?" * len(tokens))
        if kind:
            dfs = {
                r["token"]: r["c"]
                for r in self.db.execute(
                    f"SELECT t.token, COUNT(*) c FROM mem_tokens t "
                    f"JOIN memories m ON m.id=t.memory_id "
                    f"WHERE t.token IN ({ph}) AND m.status='faded' AND m.kind=? "
                    f"GROUP BY t.token",
                    tokens + [kind],
                )
            }
            n = self.db.execute(
                "SELECT COUNT(*) FROM memories WHERE status='faded' AND kind=?", (kind,)
            ).fetchone()[0]
        else:
            dfs = {
                r["token"]: r["df"]
                for r in self.db.execute(
                    f"SELECT token, df FROM token_df WHERE token IN ({ph})", tokens
                )
            }
            n = self.db.execute(
                "SELECT COUNT(*) FROM memories WHERE status='faded'"
            ).fetchone()[0]
        if n == 0:
            return {}, 1.0
        unseen = math.log(n + 1) + 1.0
        # df 为 0 的 token 不放进 idf：_cos_idf 会用 unseen 兜底，和全扫时的行为一致。
        idf = {
            t: math.log((n + 1) / (dfs[t] + 0.5)) + 1.0
            for t in tokens
            if dfs.get(t, 0) > 0
        }
        return idf, unseen

    def _faded_candidates(self, qt: list[str], kind: str, topic: str) -> list[sqlite3.Row]:
        """兜底候选：走倒排索引取「命中任一 token」的 faded 条目。

        与全扫只差三处，都是有意的取舍：
          * relevance=0 的条目不进候选 —— 全扫会按权重把它们排进前八，实测能占掉
            一半名额，把真正命中的挤出去；
          * 有二字组/英文词时单字不参与选候选 —— 选择性太差（一个「的」能捞大半
            个库），单字命中的条目相关度本来就低；整条查询都是单字时才退回单字；
          * 查询里全是高频词、累计命中超过 FADED_CANDIDATE_BUDGET 时，按 df 从
            小到大只留一部分 token，丢掉的都是被高频词命中的条目。
        留下的候选用全池 IDF 打分，相关度与全扫逐位相同。
        """
        where = ["m.status='faded'"]
        args: list = []
        if kind:
            where.append("m.kind=?")
            args.append(kind)
        if topic:
            # 带 topic 时 _pass 会给 rel==0 的条目标 0.12 底分，索引取候选会改掉
            # 这个行为；topic 本身已经把候选集缩小了，这条路保持全扫。
            where.append("m.topic=?")
            args.append(topic)
            return self.db.execute(
                f"SELECT m.* FROM memories m WHERE {' AND '.join(where)}", args
            ).fetchall()

        toks = [t for t in dict.fromkeys(qt) if len(t) >= 2] or list(dict.fromkeys(qt))
        if not toks:
            return []
        ph = ",".join("?" * len(toks))
        dfs = {
            r["token"]: r["df"]
            for r in self.db.execute(
                f"SELECT token, df FROM token_df WHERE token IN ({ph})", toks
            )
        }
        toks = [t for t in toks if dfs.get(t, 0) > 0]
        if not toks:
            return []
        toks.sort(key=lambda t: dfs[t])  # 罕见的优先；预算不够时先丢高频词
        chosen: list[str] = []
        budget = FADED_CANDIDATE_BUDGET
        for t in toks:
            if chosen and dfs[t] > budget:
                break
            chosen.append(t)
            budget -= dfs[t]
        sub = (
            "SELECT DISTINCT memory_id FROM mem_tokens "
            f"WHERE token IN ({','.join('?' * len(chosen))})"
        )
        where.append(f"m.id IN ({sub})")
        args.extend(chosen)
        return self.db.execute(
            f"SELECT m.* FROM memories m WHERE {' AND '.join(where)}", args
        ).fetchall()

    def _fetch(self, statuses: tuple[str, ...], kind: str, topic: str) -> list[sqlite3.Row]:
        where = [f"status IN ({','.join('?' * len(statuses))})"]
        args: list = list(statuses)
        if kind:
            where.append("kind=?")
            args.append(kind)
        if topic:
            where.append("topic=?")
            args.append(topic)
        return self.db.execute(
            f"SELECT * FROM memories WHERE {' AND '.join(where)}", args
        ).fetchall()

    def _item(self, r: sqlite3.Row) -> dict:
        """把一行包成和 _pass() 同形状的 dict（不打分，只算权重）。开场包按时间取时用。"""
        w = self.weight(r)
        return {
            "id": r["id"],
            "kind": r["kind"],
            "topic": r["topic"],
            "text": r["text"],
            "tags": json.loads(r["tags"]),
            "weight": round(w, 3),
            "relevance": 0.0,
            "status": r["status"],
            "confidence": r["confidence"],
            "source": r["source"],
            "age_days": round(_age_days(r["created_at"]), 1),
            "use_count": r["use_count"],
            "created_at": r["created_at"],
            "view": r["view"],
            "emotion": r["emotion"],
            "sensory": r["sensory"],
            "score": round(w, 4),
        }

    def _pass(
        self,
        rows: list[sqlite3.Row],
        qt_tf: dict[str, int],
        qt: list[str],
        limit: int,
        topic: str,
        reinforce: bool,
        idf: dict[str, float] | None = None,
        unseen: float | None = None,
        tfs: dict[int, dict[str, int]] | None = None,
    ) -> list[dict]:
        """对一批候选打分。候选集大小直接决定耗时，所以调用方要控制它。

        idf / unseen 传了就按传入的算（faded 索引用全池统计，避免缩小候选集后
        IDF 退化、把长文档的相关度稀释掉）；不传则维持原来的「候选集内算 IDF」。
        tfs 传了就复用调用方算好的词频 —— 兜底路径要用它同时算 df，别算两遍。
        """
        if not rows:
            return []
        if tfs is None:
            tfs = {r["id"]: self._tf(tokenize(r["text"] + " " + r["tags"])) for r in rows}
        if idf is None:
            idf = self._idf(list(tfs.values()))
        if unseen is None:
            unseen = math.log(len(rows) + 1) + 1.0

        scored = []
        for r in rows:
            w = self.weight(r)
            fresh = self._decay(r["kind"], self._age_of(r))
            used = min(1.0, math.log1p(r["use_count"]) / math.log1p(12))
            recent = 1.0 if r["last_used_at"] and _age_days(r["last_used_at"]) < 1.0 else 0.0
            rel = self._cos_idf(qt_tf, tfs[r["id"]], idf, unseen) if qt else 0.0

            if qt:
                score = 0.50 * rel + 0.20 * w + 0.15 * fresh + 0.15 * used - 0.10 * recent
                # 字面没撞上但属于同 topic 的，给一点底分
                if rel == 0 and topic and r["topic"] == topic:
                    score += 0.12
            else:
                score = w

            if r["status"] == "faded":
                score *= 0.55
            scored.append((score, r, rel, w))

        scored.sort(key=lambda x: x[0], reverse=True)
        out = []
        for score, r, rel, w in scored[: max(1, min(limit, 30))]:
            if score <= 0.02:
                continue
            if reinforce and rel >= REINFORCE_MIN_REL:
                # 只有"真的想起来"才算强化：
                #   - 开场包那种整批倾倒不算（reinforce=False）
                #   - 被 limit 顺带捞出来、但相关度很低的也不算
                # 否则常驻的条目会被自己一路顶到 0.9 钉死，用进废退在顶部就失效了。
                new_imp = min(1.0, r["importance"] + REINFORCE * (1.0 - r["importance"]))
                self.db.execute(
                    "UPDATE memories SET last_used_at=?, use_count=use_count+1, "
                    "importance=? WHERE id=?",
                    (_iso(), new_imp, r["id"]),
                )
                # short 的「加时长」：每次被想起来，活跃日龄归零。
                # 一直有人提的东西不该因为"年纪"死掉，它只该因为没人提而死。
                if r["kind"] == "short":
                    self.db.execute(
                        "UPDATE memories SET born_active=? WHERE id=?",
                        (self._active_day(), r["id"]),
                    )
                # F3 的另一半：被反复想起的淡忘条目应该自己回来（用进废退是双向的）
                if r["status"] == "faded" and rel >= 0.25:
                    revived_w = new_imp * self._decay(r["kind"], self._age_of(r))
                    if revived_w >= FADE_BELOW * 1.5:
                        self.db.execute("UPDATE memories SET status='active' WHERE id=?", (r["id"],))
                        self._reindex(r["id"])  # 离开 faded，索引里要撤掉
            out.append(
                {
                    "id": r["id"],
                    "kind": r["kind"],
                    "topic": r["topic"],
                    "text": r["text"],
                    "tags": json.loads(r["tags"]),
                    "weight": round(w, 3),
                    "relevance": round(rel, 3),
                    "status": r["status"],
                    "confidence": r["confidence"],
                    "source": r["source"],
                    "age_days": round(_age_days(r["created_at"]), 1),
                    "use_count": r["use_count"],
                    "created_at": r["created_at"],
                    "view": r["view"],
                    "emotion": r["emotion"],
                    "sensory": r["sensory"],
                    "score": round(score, 4),
                }
            )
        return out

    def recall(
        self,
        query: str = "",
        limit: int = 8,
        kind: str = "",
        topic: str = "",
        reinforce: bool = True,
        include_faded: bool = False,
        include_pinned: bool = True,
    ) -> list[dict]:
        """默认只扫 active/pinned —— 候选集因此恒定在 MAX_ACTIVE 以内，
        不会因为 faded 越攒越多而越来越慢（实测 2 万条时全扫要 1.5s）。

        faded 只在主检索一无所获时兜底查一次：平时不付这个成本，
        "你还记得吗"那种问法仍然翻得出来。

        include_pinned=False 时只扫 active —— 开场包用这个，把"永久记忆"
        （钉住的、kind=permanent 的）挡在外面：它们不衰减，但也不该每轮都出现。
        """
        qt = tokenize((query or "").strip())
        qt_tf = self._tf(qt)
        self._tick()

        base: tuple[str, ...]
        if include_faded:
            base = ("active", "pinned", "faded")
        elif include_pinned:
            base = ("active", "pinned")
        else:
            base = ("active",)
        out = self._pass(self._fetch(base, kind, topic), qt_tf, qt, limit, topic, reinforce)

        # 兜底：主检索没打中（不是"没有结果"，是"结果都很弱"），才去翻 faded。
        # 用"最好成绩"判断而不是"有没有结果"，否则只要库里有几条活跃条目，
        # 一条弱匹配就会挡住所有已经淡忘的记忆。
        #
        # 空查询不兜底：开场包那种 qt 为空的调用，相关度恒为 0，必然触发一次全库
        # faded 扫描（实测 2 万条 faded 要 344ms，每个新会话都付）。而且开场包本来
        # 就不该带 faded 条目 —— 那是"已经淡忘的东西"。
        if not include_faded and qt:
            best = max((x["relevance"] for x in out), default=0.0)
            if best < FADED_FALLBACK_REL:
                faded_rows = self._faded_candidates(qt, kind, topic)
                extra: list[dict] = []
                if faded_rows:
                    # 词频只算一遍：df/idf 要用，打分也要用。
                    tfs = {
                        r["id"]: self._tf(tokenize(r["text"] + " " + r["tags"]))
                        for r in faded_rows
                    }
                    idf = unseen = None
                    if not topic:
                        # 索引取候选人，IDF 仍按整个 faded 池算，分数与全扫一致。
                        idf, unseen = self._faded_corpus_stats(qt, tfs, kind)
                    extra = self._pass(
                        faded_rows, qt_tf, qt, limit, topic, reinforce,
                        idf=idf, unseen=unseen, tfs=tfs,
                    )
                if extra:
                    seen = {x["id"] for x in out}
                    added = [e for e in extra if e["id"] not in seen]
                    out += added
                    out.sort(key=lambda x: x["score"], reverse=True)
                    cap = max(1, min(limit, 30))
                    out = out[:cap]

                    # 保底：faded 条目分数要乘 0.55 的淡忘系数，而"无关但权重高"的
                    # 活跃条目动辄 0.27 分。只按总分截断的话，一条 relevance 0.25 的
                    # 老记忆会被八条 relevance 0 的活跃条目整批挤掉 —— 兜底等于白做。
                    # 规则：命中足够相关（≥ FADED_RESCUE_MIN_REL）且比被替换的那条
                    # 更相关时，才占一个名额；这样不会把真正相关度更高的活跃条目挤走。
                    kept = {x["id"] for x in out}
                    dropped = sorted(
                        (
                            e
                            for e in added
                            if e["id"] not in kept and e["relevance"] >= FADED_RESCUE_MIN_REL
                        ),
                        key=lambda x: x["relevance"],
                        reverse=True,
                    )[: max(1, min(FADED_RESCUE_MAX, cap // 4))]
                    for e in dropped:
                        victim = min(out, key=lambda x: x["relevance"])
                        if victim["relevance"] < e["relevance"]:
                            out[out.index(victim)] = e
                    out.sort(key=lambda x: x["score"], reverse=True)
                    # 兜底里可能把 faded 复活成 active（见 _pass），索引的文档数要跟上。
                    self._sync_index_meta()

        self.db.commit()
        return out

    def upcoming(self) -> list[dict]:
        """日期唤醒：今天该提醒的条目。

        同一档只提醒一次 —— 「还剩 7 天」问计划，「还剩 3 天」问准备，
        「明天」再点一下。档位存在 due_stage 里，带上年份，所以每年会重新武装。
        没到窗口的一个都不返回，开场包也就一个字都不多。
        """
        today = date.today()
        out: list[dict] = []
        rows = self.db.execute(
            "SELECT * FROM memories WHERE due_date<>'' AND status IN ('active','pinned')"
        ).fetchall()
        for r in rows:
            nd = _next_due(r["due_date"], today)
            if nd is None:
                continue
            left = (nd - today).days
            lead = max(1, int(r["due_lead"] or 7))
            if left > lead:
                continue
            stage = _stage_of(left)
            armed = f"{stage}@{nd.year}"
            if r["due_stage"] == armed:
                continue  # 这一档已经提醒过了
            self.db.execute("UPDATE memories SET due_stage=? WHERE id=?", (armed, r["id"]))
            out.append(
                {
                    "id": r["id"],
                    "kind": r["kind"],
                    "text": r["text"],
                    "date": nd.isoformat(),
                    "left": left,
                    "stage": stage,
                    "lead": lead,
                }
            )
        self.db.commit()
        out.sort(key=lambda x: x["left"])
        return out

    def startup_pack(self) -> dict:
        """会话开场用：常驻 profile + 权重最高的几条事实 + 近期决定 + 短期。

        **永久记忆不进这里。** kind=permanent 和钉住的条目都不参与开场包 ——
        它们不衰减，但也不该每轮都占用上下文。儿子的生日你不会每天想起，
        只有到了那天、或者话题碰到的时候才该浮现。
        """
        profile = [
            dict(r)
            for r in self.db.execute(
                "SELECT id,kind,topic,text,tags FROM memories "
                "WHERE kind='profile' AND status IN ('active','pinned') ORDER BY id"
            )
        ]
        top = self.recall("", limit=6, kind="fact", reinforce=False, include_pinned=False)
        decisions = self.recall("", limit=2, kind="decision", reinforce=False, include_pinned=False)
        # 短期按**时间**取，不按权重。短期记忆的定义就是"最近"，
        # 而它们的 importance 往往并列，按权重排会退化成随机——实测挑到过一小时前的旧条目。
        #
        # 两级注入：最近 SHORT_IN_PACK 条给全文，更早的只留一句汇总。
        # 全量倒进去实测过：50 条约 2500 token，且大半已过期——花的钱买的是噪音。
        rows = list(self.db.execute(
            "SELECT * FROM memories WHERE kind='short' AND status IN ('active','pinned') "
            "ORDER BY updated_at DESC, id DESC"
        ))
        short = [self._item(r) for r in rows[:SHORT_IN_PACK]]
        older = rows[SHORT_IN_PACK:]
        short_older = None
        if older:
            ids = [r["id"] for r in older]
            short_older = {
                "count": len(older),
                "id_from": min(ids),
                "id_to": max(ids),
            }
        # 日期唤醒是唯一的例外：只有真到窗口里才出现，平时一个字都不占。
        return {
            "profile": profile,
            "facts": top,
            "decisions": decisions,
            "short": short,
            "short_older": short_older,
            "due": self.upcoming(),
        }

    # ---------------- 纠正 / 遗忘 ----------------

    def forget(self, memory_id: int, reason: str = "用户说记错了") -> dict:
        row = self.db.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        if not row:
            raise Rejected(f"没有 #{memory_id} 这条记忆。")
        self.db.execute(
            "UPDATE memories SET status='phantom', confidence=0.0, note=?, updated_at=? WHERE id=?",
            (reason[:200], _iso(), memory_id),
        )
        if row["status"] == "faded":
            self._reindex(memory_id)
            self._sync_index_meta()
        self.db.commit()
        return {"id": memory_id, "status": "phantom", "note": "已标记为错误，不再参与检索"}

    def pin(self, memory_id: int, pinned: bool = True) -> dict:
        row = self.db.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        if not row:
            raise Rejected(f"没有 #{memory_id} 这条记忆。")
        self.db.execute(
            "UPDATE memories SET status=?, importance=MAX(importance, ?) WHERE id=?",
            ("pinned" if pinned else "active", PIN_IMPORTANCE if pinned else 0.0, memory_id),
        )
        if row["status"] == "faded":
            self._reindex(memory_id)
            self._sync_index_meta()
        self.db.commit()
        return {"id": memory_id, "pinned": pinned}

    # ---------------- 整合 ----------------

    def trim_short(self) -> int:
        """短期是滚动缓冲区：只留最近 SHORT_MAX 条，更早的直接 faded。

        只靠活跃日衰减太慢——一天几十轮的话，短期池会先被撑爆，开场包
        【最近事件】也会挑到旧的。容量上限比时间衰减更直接。
        """
        rows = self.db.execute(
            "SELECT id FROM memories WHERE kind='short' AND status IN ('active','pinned') "
            "ORDER BY updated_at DESC, id DESC"
        ).fetchall()
        if len(rows) <= SHORT_MAX:
            return 0
        drop = [r["id"] for r in rows[SHORT_MAX:]]
        self.db.executemany(
            "UPDATE memories SET status='faded' WHERE id=?", [(i,) for i in drop]
        )
        for i in drop:
            self._reindex(i)
        self._sync_index_meta()
        self.db.commit()
        return len(drop)

    def _maybe_consolidate(self) -> None:
        last = float(self._meta("last_consolidate", "0") or 0)
        if _now() - last < CONSOLIDATE_INTERVAL:
            return
        self.consolidate()

    def consolidate(self) -> dict:
        """合并近重复 -> 降级 -> 上限截断 -> 物理清理。无守护进程，懒触发。"""
        # 索引只是缓存：这里做一次完整对账，任何漏掉的增删都在下一次整理时自愈。
        self._verify_token_index(full=True)
        report = {"merged": 0, "faded": 0, "capped": 0, "purged": 0, "promoted": 0,
                  "short_trimmed": 0}

        # F5：同 kind 近重复合并。
        # 必须按 kind 分桶 —— 不分组的话，一条 fact（90 天）可能被并进 event（14 天），
        # 寿命当场缩水，而且 kind 语义全丢。这是 2026-10-23 修的一个真漏洞。
        rows = self.db.execute(
            "SELECT * FROM memories WHERE status IN ('active','pinned') ORDER BY id"
        ).fetchall()
        seen_by_kind: dict[str, list[sqlite3.Row]] = {}
        for r in rows:
            bucket = seen_by_kind.setdefault(r["kind"], [])
            dup = next(
                (s for s in bucket if self._cos(tokenize(r["text"]), tokenize(s["text"])) > 0.90),
                None,
            )
            if dup is None:
                bucket.append(r)
                continue
            keep = r if len(r["text"]) > len(dup["text"]) else dup
            drop = dup if keep is r else r
            self.db.execute(
                "UPDATE memories SET importance=MIN(1.0, importance+?), "
                "use_count=use_count+?, updated_at=? WHERE id=?",
                (drop["importance"] * 0.5, drop["use_count"], _iso(), keep["id"]),
            )
            self.db.execute(
                "UPDATE memories SET status='superseded', superseded_by=? WHERE id=?",
                (keep["id"], drop["id"]),
            )
            if drop in bucket:
                bucket.remove(drop)
            report["merged"] += 1

        # 短期 -> 长期：被提及 3 次就晋升，不看当时权重。
        # 「被反复提起」本身就是证据，不能因为它恰好老了就否掉。
        # 注意 short 每次被想起来都会把活跃日龄归零（见 _pass），所以真正在被用的东西不会先老死。
        for r in self.db.execute(
            "SELECT * FROM memories WHERE kind='short' AND status IN ('active','pinned')"
        ):
            if r["use_count"] >= PROMOTE_USE_COUNT:
                self.db.execute(
                    "UPDATE memories SET kind='event', importance=?, updated_at=?, "
                    "note=? WHERE id=?",
                    (max(r["importance"], 0.7), _iso(), "由短期记忆晋升（反复提及）", r["id"]),
                )
                report["promoted"] += 1

        # F1/F3：按类别衰减，低于阈值降级为 faded
        for r in self.db.execute("SELECT * FROM memories WHERE status='active'"):
            if r["importance"] >= PIN_IMPORTANCE:
                self.db.execute("UPDATE memories SET status='pinned' WHERE id=?", (r["id"],))
                continue
            # profile / permanent 永不降级
            if HALF_LIFE.get(r["kind"]) is None:
                continue
            # 短期有两道时间闸：
            #   1) 活跃日龄超过 SHORT_MAX_ACTIVE_DAYS —— 短期是"这阵子"，不是"这周"
            #   2) 墙钟超过 ZOMBIE_DAYS —— 兜底，防止活跃日一直不涨时永远不过期
            if r["kind"] == "short":
                age_active = self._active_day() - (r["born_active"] or 0)
                if age_active > SHORT_MAX_ACTIVE_DAYS:
                    self.db.execute("UPDATE memories SET status='faded' WHERE id=?", (r["id"],))
                    self._reindex(r["id"])
                    report["faded"] += 1
                    continue
                if _age_days(r["created_at"]) > ZOMBIE_DAYS:
                    self.db.execute("UPDATE memories SET status='faded' WHERE id=?", (r["id"],))
                    self._reindex(r["id"])
                    report["faded"] += 1
                    continue
            if self.weight(r) < FADE_BELOW:
                self.db.execute("UPDATE memories SET status='faded' WHERE id=?", (r["id"],))
                self._reindex(r["id"])
                report["faded"] += 1

        # F7：活跃上限。钉住的、profile、permanent 都不参与截断 —— 永久就是永久。
        active = self.db.execute(
            "SELECT * FROM memories WHERE status IN ('active','pinned')"
        ).fetchall()
        if len(active) > MAX_ACTIVE:
            order = sorted(active, key=self.weight)
            for r in order[: len(active) - MAX_ACTIVE]:
                if r["status"] == "pinned" or HALF_LIFE.get(r["kind"]) is None:
                    continue
                self.db.execute("UPDATE memories SET status='faded' WHERE id=?", (r["id"],))
                self._reindex(r["id"])
                report["capped"] += 1

        # F8：短期是滚动缓冲区，超出 SHORT_MAX 条就淘汰最旧的
        report["short_trimmed"] = self.trim_short()

        # F4：faded 且长期没再用到，才物理删除
        for r in self.db.execute("SELECT * FROM memories WHERE status='faded'"):
            ref = r["last_used_at"] or r["updated_at"]
            if _age_days(ref) > HARD_DELETE_DAYS:
                self._index_remove(r["id"])
                self.db.execute("DELETE FROM memories WHERE id=?", (r["id"],))
                report["purged"] += 1

        self._prune_token_df()
        self._sync_index_meta()
        self.db.commit()
        self._set_meta("last_consolidate", str(_now()))
        return report

    def stats(self) -> dict:
        rows = self.db.execute(
            "SELECT kind, status, COUNT(*) n FROM memories GROUP BY kind, status"
        ).fetchall()
        by: dict[str, dict[str, int]] = {}
        for r in rows:
            by.setdefault(r["kind"], {})[r["status"]] = r["n"]
        total = self.db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        return {"total": total, "by_kind_status": by, "db": str(self.path)}
