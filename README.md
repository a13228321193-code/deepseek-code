# dsh_memory —— 接在 DSH 上的长期记忆

两条通路，缺一不可：

| 通路 | 怎么工作 | 解决什么 |
|---|---|---|
| **自动注入**（hook） | `UserPromptSubmit` 钩子把记忆状态塞进上下文 | 新会话**不靠我自觉**就知道你是谁、在忙什么 |
| **按需检索**（MCP 工具） | 我在需要时调 `recall` 去翻深层记忆 | 生日、旧决策、事件细节 |

MCP server（stdio）挂在 `~/.dsh/cordis.patch.yml`，会话里就有四个工具：

```
mcp__memory__recall    检索 / 取开场包
mcp__memory__remember  写入
mcp__memory__forget    纠正（标记为错误，不再参与检索）
mcp__memory__pin       钉住 / 取消钉住
```

数据在 `~/.dsh/memory/memory.db`（明文 SQLite）。**没有任何守护进程**，整合是懒触发的。

---

## 两条钩子（都不依赖我自觉）

`AGENTS.md` 里的记忆规则本质是"让我去做一件事"——实测漏过多次。钩子这条路是
"内容已经在上下文里"，不需要我执行任何动作。

```
@deepseek-ai/dsh-hooks-claude-code  (装在 ~/.dsh/profiles/web)
  ├─ SessionStart → dsh_memory/hook_session.py
  │     新会话一建立，直接 agent.inject() 完整开场包（~1250 token）
  └─ Stop         → dsh_memory/hook_stop.py
        本轮没写短期 → block + reason（附活跃清单），强制我再走一步
```

**没有 `UserPromptSubmit` 钩子。** 曾经有过一个 `hook_inject.py`，每轮注入一行
（称呼 + 当前状态，~90 token）。删掉的理由：

* 我每轮都写新状态 → 内容一变就重注入 → **每条消息都注入 90 token**；
* 注入的正是我上一轮刚写的东西，写入时 `新增 #N：…` 已在上下文里，属于重复；
* 它唯一可能的用途"上下文压缩后重新锚定"**也不成立**——哈希没变，它会输出 `{}`，什么都不注入。

那一行的价值只剩"SessionStart 挂掉时兜底"，不值得每条消息付 90 token。
配置备份在 `.claude/hooks.json.bak-before-remove-inject`。

### 五个坑（全都踩过）

1. **必须用 `insert:` 形式**写在 profile 的 `cordis.patch.yml` 里。
   直接写 `- id: xxx` 是**覆盖**已有条目，新插件不会生效——静默失败，不报错。
2. **插件要装在对应 profile**。装在 web 却把配置写到机器级层（`~/.dsh/cordis.patch.yml`）
   → 其他 profile 启动时报 `ERR_MODULE_NOT_FOUND`。
3. **peer 依赖要单独装** `@deepseek-ai/dsh-hook-protocol`，否则报 `Cannot find package`。
4. **`sandbox-policy` 要放开**。它默认 `mode: workspace-write`，钩子通过 `ctx.shell` 执行命令时受它约束；
   这台 Windows 没有可用的受限 runner，于是钩子被直接拒绝执行、`hook/result` 里记一条
   `SANDBOX_UNAVAILABLE`，**看起来像"钩子没触发"，其实是跑了但被挡住**。
   解法：在 web profile 的 patch 里覆盖 `sandbox-policy.mode: danger-full-access`。
5. **别在 dsh 运行时改 profile 的 patch 文件**。web profile 开着 `patchReload: live`，
   改动会热重载插件树，正在进行的那一轮 prompt 直接报
   `sessionController is unavailable` 中断。要改就关掉 dsh 再改。

### 一个 Windows 专属坑

Python 的 stdout 走管道时默认用系统代码页（GBK），桥接按 UTF-8 读 → 中文全变乱码
（实测看到 `��������Զ�ע��`）。所有钩子脚本都强制
`sys.stdout.reconfigure(encoding="utf-8")`，stdin 也按字节读再解码。

### 成本

| 钩子 | 何时 | 成本 |
|---|---|---|
| `SessionStart` | 每个新会话一次 | ~1250 token（完整开场包） |
| `Stop` | 每轮结束 | ~50 ms；只有点名时才输出 reason（约 190 token） |

`SessionStart` 那份开场包，本来也要靠我调 `recall` 拿——同样的 token，从"靠自觉"变成"必然投递"。

---

## 短期状态怎么更新（钩子点名，模型来写）

`short` 里那行「当前状态」由**我自己写**，钩子负责在每轮结束时**点名催我**。

```
回合结束
 └─ hook_stop.py                        ← ~50ms
     ├─ 上次 Stop 之后写过    → 放行（什么都不做）
     ├─ 刚点过名（120s 内）    → 静默放行，不重复催
     └─ 否则                  → 输出 {"decision":"block","reason":"…"}
                                  └─ 桥接把这句 reason 变成 steering 消息
                                      └─ 我被强制再走一步，跑 set_state.py 写状态

新会话建立
 └─ hook_session.py                     ← ~100ms
     └─ 渲染完整开场包 → agent.inject() 直接进会话
```

### 为什么是"点名"而不是"让钩子自己调 LLM"

试过了，`hook_state.py` 那版会调 `deepseek-chat` 总结。**质量不够**：

* 它只看得到一轮，看不到对话脉络；
* 无状态，每轮从头写，状态会来回横跳（实测 5 轮里 3 轮把做完的事又写成"待做"）；
* 还得把会话日志解压 10 MB、烧 700+ token，只为写一句没有上下文的话。

所以改成：**钩子只负责提醒，写由有完整上下文的我来做**。没有 LLM 兜底——
如果我没写，下一轮它再点一次名。

### 为什么需要 block

`dsh-hooks-claude-code` 的 Stop 映射（源码）：

```js
ctx.on("agent/turn-stopping", async ({ agent, turn, signal }) => {
  const merged = await runPoint("Stop", "", stopPayload(agent), { agent, turn, signal });
  if (merged.decision === "deny") {
    const text = merged.reason ?? "continue: blocked by Stop hook";
    agent.steer(createUserMessage({ content: [{ type: "text", text }] }));
```

`decision: "block"` 会让桥接把这句 reason 塞回给模型，**强制再走一步**。
这是钩子唯一能"调动模型"的通道。

### 两条约束

1. **`set_state.py` 是 short 的唯一写者**（我调它）。钩子不写库，只读一个时间戳。
2. **120 秒冷却**防死循环：点过一次名，两分钟内不再点第二次。

### 代价

| 项 | 数值 | 位置 |
|---|---|---|
| `hook_stop.py` | **~50 ms** | 回合收尾（阻塞） |
| `hook_session.py` | ~100 ms | 新会话建立时（阻塞一次） |
| 我写状态 | 一次 `set_state.py`，~100 ms | 回合内 |
| 额外 LLM 调用 | **0** | — |

### 加事件要重启

`hooks.json` 是**进程启动时读一次**（桥接的既定行为）。所以：

* 新增/删除**事件**（比如这次加 `Stop`）→ **必须重启 dsh**；
* 改命令内容/超时 → 同样要重启；
* 改 `hook_stop.py` / `hook_session.py` / `set_state.py` / `packfmt.py` 的代码 → **不用重启**（每次都是新进程）。

---

## 六个 kind，生命周期不同

| kind | 存什么 | 半衰期 | 在开场包里？ |
|---|---|---|---|
| `profile` | **每轮都要生效**的称呼、偏好、禁忌 | **永不衰减** | **在**（全部） |
| `permanent` | **永远为真、但不是每轮都要想起**的（家人、生日、纪念日） | **永不衰减** | **不在** |
| `fact` | 关于用户的硬事实（路径、机器、项目、约定） | 90 天 | 在（前 6 条） |
| `decision` | 定下来的决定和理由 | 30 天 | 在（前 2 条） |
| `event` | 发生过的事，带视角/情绪/细节 | 14 天 | 不在 |
| `short` | **最近发生的事**（在忙什么、今天干了什么） | **7 个活跃日**（半衰期 2.65；墙钟 30 天兜底） | 在（**最新** 3 条） |

`profile` 和 `permanent` 都不参与时间衰减，**也不会被 300 条上限截断**。
钉住的条目（`status=pinned`）同样三者全免。

`event` 不进开场包 —— 只有被问到、或被检索时才出现。这是刻意的取舍：
新会话不自动带"昨天发生了什么"，省 token，但需要时能翻出来。

`short` **进开场包**，标签叫「最近事件」，最多 3 条。

> 注意：它按 **`updated_at` 取最新**，不按权重。短期记忆的 importance 往往并列
> （都由钩子写成 0.8），按权重排会退化成"数据库返回顺序"——实测抽到过一小时前的旧条目。
> 「最近」就该按时间取。

### 短期怎么退场：四层，取最严的先执行

短期过期**不是**"内容自相矛盾"，而是"被更新的顶出去"——它本质是队列，不是语义。
（试过用相似度判过期：14 条里最高余弦只有 0.357，阈值调到能触发就会误伤最新那条。**这条路死了**。）

| 层 | 规则 | 参数 |
|---|---|---|
| 1. 队列 | 只留最近 N 条，超出立刻 faded | `SHORT_MAX = 25` |
| 2. 活跃日 | 活跃日龄超过 M 直接 faded | `SHORT_MAX_ACTIVE_DAYS = 3` |
| 3. 墙钟 | 创建超过 30 天兜底 | `ZOMBIE_DAYS = 30` |
| 4. 手动 | `set_state.py --done "#27 #28"` 当场退场 | 我判断明确的已完成项 |

层 1 在写入时（`set_state.py` 里调 `trim_short()`）和 consolidate 时都会跑；
层 2/3 在 `consolidate()` 的衰减段里跑。

**为什么是 25**：一天几十轮的话，25 条约等于半天的上下文——正好是"这次任务还在进行中"的范围。

---

## 永久记忆（`permanent`）为什么单独一层

儿子的生日、父母的年龄、结婚纪念日 —— 这些东西**永远为真**，但你不会每次跟儿子说话都想一次。
开场包应该是"这一轮会用到的东西"，不是"我知道的一切"。

所以拆成两层：

| | `profile` | `permanent` |
|---|---|---|
| 例子 | 「叫我老大」「不要用 emoji」 | 「儿子生日 6 月 18 日」 |
| 要不要每轮生效 | **要** | 不要 |
| 开场包 | 全量注入 | **不注入** |
| 怎么被想起 | 天然就在 | 只有 `recall` 命中相关话题时浮现 |

判断标准一句话：**「这句话不说，这一轮就会答错吗？」**
会 → `profile`；不会 → `permanent`。

---

## 日期唤醒：到了那天才想起来

`permanent` 默认不进开场包，但**日期到了它自己会冒出来**——这是唯一进开场包的例外，
而且只在窗口期内，平时一个字都不占。

```
写的时候：due="06-18"（每年）或 "2027-06-18"（一次），lead=7（提前多少天开始提醒）
```

只支持**公历**。认得的写法：`6-18` / `06-18` / `2027-6-18` / `2027年6月18日` / `2027/6/18`。
认不出来直接拒写——写错日期比不写更糟。

### 分档提醒，同一档只响一次

| 档位 | 什么时候 | 该说什么 |
|---|---|---|
| `lead` | 剩 `lead`~4 天 | 「下周一生日，有安排吗」 |
| `3` | 剩 3~2 天 | 「准备得怎么样了」 |
| `1` | 明天 | 「别忘了明天」 |
| `0` | 当天 | 「就是今天」 |

档位存在 `due_stage` 里，格式 `"3@2026"` —— **带年份，所以每年的同一档会重新武装一次**。

`lead` 不是固定的，写记忆时由我按事情的分量给：生日 7~14 天，纪念日 14 天，
一次性的重要事项 30 天，普通提醒 3 天。

开场包里的样子：

```
【临近的日子】
#12 老大的儿子生日是 6 月 18 日 —— 2027-06-18，还有 3 天
```


---

## 短期记忆怎么变成长期记忆

`short` 不是另一种存储，是同一张表上的另一种**生命周期**：

```
写入 short → 按「活跃日」衰减 → 没人再提 → 权重跌到 0.12 以下 → faded（≈ 自然遗忘）
                            ↘ 被提及 3 次 → 晋升为 event（长期，14 天半衰期）
                            ↘ 每次被想起来 → 权重↑ + 活跃日龄归零（续命）
```

**寿命：一条典型短期记忆活 7 个活跃日。** 实测（importance 0.7）：第 6 个活跃日 0.146、第 7 天 0.112。
半衰期是 `2.65` 活跃日——这个数不是拍的，是反推的：`0.75 × 0.5^(7/2.65) < 0.12`。
要改寿命，改半衰期就行：`HALF_LIFE["short"] = 目标天数 / 2.65`。

### 两个动作：提权 + 加时长

每次被 `recall` 命中（`rel ≥ 0.10`）时：

1. **提权**：`importance += 0.15·(1−importance)`
2. **续命**：`short` 的活跃日龄归零（`born_active = 当前活跃日`）

第 2 条很关键。只提权不续命的话，一条被老老实实每天提起、但天生活跃日龄很老的短期记忆，
会在攒够 3 次之前先老死 —— 实测旧逻辑下"每 2 天提一次、提了 4 次"仍然在第 9 天淡忘。
现在的规则是：**只要你还在提，它就不老；没人提，才轮到它死。**

晋升只看**提及次数 ≥ 3**，不看当时权重 —— "被反复提起"本身就是证据。

### 实测：不同提起频率的结局

```
从不提起    -> 第 8 天淡忘
每天提一次  -> 第 3 天晋升为 event（use=3）
每 2 天一次 -> 第 6 天晋升为 event（use=3）
每 3 天一次 -> 第 9 天晋升为 event（use=3）
每 6 天一次 -> 第 18 天晋升为 event（use=3）
每 8 天一次 -> 第 15 天淡忘（use=1）   ← 间隔超过寿命，断了
```

最后一行是边界：**提起的间隔超过记忆本身能活的时间，链条就断**。
7 天寿命就是 7 天的容错窗口。

### 为什么用「活跃日」而不是墙钟

用墙钟会有个荒唐的结果：**你人不在的这几天，它也在一路变老**。
出差一周回来，"正在做的事"全没了。

所以 `short` 的年龄按**活跃日**算：`age = active_day − born_active`。

* `active_day` 存在 `meta` 表里，每次读/写记忆时 tick 一次：**跨本地日才 +1**，一天调 N 次也只加 1。
* 你连着来 7 天 → 短期记忆自然淘汰；隔 10 天不来 → `active_day` 不动，一点都不掉。
* tick 漏跑的后果是"记忆不老"，方向是安全的（不会误删）。

**短期的四层退场**（详见上面「短期怎么退场」）：队列 25 条 → 活跃日 3 天 → 墙钟 30 天 → 手动 `--done`。
三层自动闸取最严的先执行；`SHORT_MAX_ACTIVE_DAYS = 3` 是比"7 个活跃日衰减"更早的那道闸——
一天几十轮时，靠衰减根本退不掉。

长期记忆（fact 90 天 / decision 30 天 / event 14 天）**仍然用墙钟** ——
那些数字的意思是"多久以前发生的事"，时间本身有意义，不能换成活跃日。

不过长期记忆的年龄**从最后一次被想起算起**，不是从写下那天算起。
一条 40 天没被碰的 event 权重可以掉到 0.11，被 `recall` 命中一次就回到 0.83（实测）。
**被想起来的时刻，就是它重新变年轻的时刻** —— 只有真正没人再提的才会老死。

这就是「按短期记忆的权重调整记忆」：

* 一次性的杂事，连着几次活跃日没被碰就自己消失，不占地方；
* 真在推进的事，每次被想起都被加权 + 续命，提满 3 次就自动转成长期记忆，不用人判断；
* 开场包取 `short` 时是 **reinforce=False** 的只读，所以「被开门带上」本身不算「被想起」——
  只有真的被 query 命中才算。这条和前面 `#1` 被顶到 0.877 的教训是同一个。

### 跨 kind 不会互相吃掉

写入去重和事实取代都是**同 kind 内**进行的（`AND kind=?`）。

合并那一步原来漏了这个条件 —— 注释写着"同 kind 近重复合并"，SQL 里却没有 `kind` 过滤，
一条 `fact`（90 天）有可能被并进 `event`（14 天），寿命当场缩水。**2026-10-23 已修**，
现在按 kind 分桶合并，并有一条断言守着：同样的文字分别写成 `short` 和 `event`，整合后两条都活着。

---

## 事件的三件套（`event` 才有）

| 字段 | 值 | 规则 |
|---|---|---|
| `view` | `self` / `user` / `both` | `self`=我这边的；`user`=他告诉我的他的经历；`both`=我们之间 |
| `emotion` | 一个词 | `view=user` 时**只能填他明说或明显表现出的**，编不出来就留空 |
| `sensory` | 一个具体细节 | 时间点 / 画面 / 声音 / 原话，不写"氛围很好" |

两条硬约束，写在代码里不是写在文档里：

* `view` 不在枚举里 → 整个 `view` 清空；
* `view` 为空却带 `emotion` → **`emotion` 直接丢掉**。不知道是谁的情绪，存下来就是糊涂账。

同一件事可以两层各记一条：`profile` 存"称呼规则"，`event` 存"那天他定了称呼"。

---

## 写入规则

| # | 规则 | 实现位置 |
|---|---|---|
| W1 | 三问门槛：一个月后还有用吗 / 换个会话看得懂吗 / 是推断还是明说 | 太短(<8 字)直接拒；`source=inferred` 强制置信度 ≤0.5 |
| W2 | 一条只讲一件事，必须含可检索的锚（专名/路径/数字/结论） | 无锚点直接拒 |
| W3 | 只写四类：偏好、硬事实、决定、事件 | 靠 `kind` 约束 + AGENTS.md 的触发清单 |
| W4 | 冲突不删旧值，标 `superseded_by` | `remember()` 里查同前缀旧条目 |
| W5 | 去重并强化：相似度 >0.90 合并，`importance += 0.10` | `_find_duplicate()` |
| W6 | 写入不在对话流里顺手做，由调用方显式触发 | 由 AGENTS.md 的触发点规定 |
| W7 | 写入前校验，不合格打回 | `_check_sensitive()` + 长度/锚点检查 |
| W8 | 用户说记错了立刻降级 | `forget()` → `status='phantom'`，不辩论 |

**敏感信息在写入层直接拒**，不靠自觉。目前拦：`sk-` 开头的 key、`api_key/password/token=` 这类凭据、
17/18 位身份证、16–19 位银行卡、PEM 私钥。**这些内容一个字符都不落库。**

---

## 遗忘规则

| # | 规则 | 实现 |
|---|---|---|
| F1 | 按类别给不同半衰期（见上表），不搞统一衰减 | `HALF_LIFE` |
| F2 | 重要性 ≥0.9 或用户说"记住" → `pinned`，永不衰减 | `PIN_IMPORTANCE` |
| F3 | 用进废退：**真的想起来**才 `importance += 0.15·(1−importance)`；被反复想起的 `faded` 会自动复活。"真的想起来"= `relevant ≥ 0.1` 且不是开场包整批倾倒 | `recall(reinforce=)` |
| F4 | 遗忘 ≠ 删除：降为 `faded` 仍可被检索（兜底），只有 `faded` 超 180 天且再没被用到才物理删 | `consolidate()` |
| F5 | 合并优先于丢弃：同 kind 近重复合成一条，保留信息量大的 | `consolidate()` |
| F6 | 冲突是 supersede 不是遗忘，两条都留着 | `remember()` |
| F7 | 活跃条目上限 300，超了按权重强制降级 | `MAX_ACTIVE` |
| F8 | 敏感信息永不落库 | `SENSITIVE` |

---

## 检索打分

```
0.50·相关  + 0.20·当前权重 + 0.15·新鲜度 + 0.15·使用频次 − 0.10·最近刚用过
```

**候选集默认只扫 `active` / `pinned`**，`faded` 走兜底。这是 2026-10-23 修的性能缺陷。

之前是 `status IN ('active','pinned','faded')` 全扫，而 `faded` 要攒到 180 天才删 —— 库龄越长越慢。
实测（合成数据）：

| 活跃 | faded | 修之前 | 修之后 |
|---|---|---|---|
| 300 | 0 | 19 ms | 15 ms |
| 300 | 3,000 | ~200 ms | 18 ms |
| 300 | 20,000 | ~1,500 ms | **18 ms** |

规则：主检索的**最好成绩** `relevance < 0.15` 时，才去翻 `faded` 兜底一次。
用"最好成绩"而不是"有没有结果"判断 —— 否则库里只要有几条活跃条目，一条弱匹配就会挡住所有淡忘的记忆。

`faded` 的兜底查询本身走**倒排索引**（2026-10-28 加）：`mem_tokens` 存 faded 条目的
token/tf，`token_df` 存 df，只索引 `faded`（活跃条目有 300 条上限，全扫更便宜）。
两张表都是派生数据，数量对不上会自动整表重建 —— 删掉也不丢任何记忆。

- 好处：df 直接从索引读，IDF 与全扫逐位一致；候选只取命中查询词的条目。
  合成数据实测 20,000 条 faded 时一次弱查询 **6 ms**（全扫 1.0 s）。
- 代价：老库首次要扫一遍 faded 建索引（20,000 条约 7.9 s、115 万行；真实库 45 条可忽略）。
- 取候选时单字不参与（选择性太差）；全是高频词的长查询按 df 从小到大取到预算为止。
  被丢掉的都是「被高频词命中」的条目，IDF 权重本来就低。`relevance = 0` 的条目也不再进候选
  —— 全扫会按权重把它们排进前八，实测能占掉一半名额。

`faded` 的条目总分乘 0.55。`query` 留空 = 返回「开场包」：全部 `profile` + 权重最高的 6 条 `fact`
+ 权重最高的 2 条 `decision` + **最新的 3 条 `short`**（按时间，不按权重）。

**加权有两道闸。** 只有"真的想起来"才 `+0.15·(1−w)`：

1. `reinforce=False` —— 开场包整批倾倒不算。不然常驻开场包的条目会被自己一路顶上 0.9 钉死，
   实测三次开场就把 `#1` 从 0.830 推到 0.877、`#4` 到 0.908（过线了）。
2. `relevance ≥ 0.10` —— 被 `limit` 顺带捞出来但根本不相关的不算。实测一次
   "Python 装了什么包"的查询把 `#1`（工作目录）和 `#4`（记忆系统决策）也一起加了权。

中文匹配用**单字 + 二字组**，不用 SQLite FTS5 —— 实测 `unicode61` 会把整段中文当一个 token，
`trigram` 又要求查询串 ≥3 字（"公园"这种两字词直接搜不到）。

**相关度那一项带 IDF 加权**（2026-10-23 加回）：`idf = log((N+1)/(df+0.5)) + 1`，罕见词重、常见词轻。
之前是裸词频，"了/什么/用户"这类虚词和"大肥鱼"权重一样，会把区分度磨平。

只在 **query→doc** 的检索里用 IDF；**doc→doc** 的去重和合并仍走裸词频 —— 两边都是文档，
公共词会自然抵消，套 IDF 反而容易误判（长文档里一个罕见词就能把相似度顶歪）。

> 诚实备注：N=7 的时候 IDF 几乎看不出差别（实测噪声项 0.037 → 0.039，基本没动），
> 因为就这么几条，高频词的 df 也没高到哪去。它的价值要等库涨到几十上百条才显现。
> 单元测试里机制是验证过的：命中罕见词的相似度 0.868，命中常见词只有 0.497。

---

## 可调参数

都在 `core.py` 顶部：

```python
HALF_LIFE = {
    "profile": None,    # 常驻，永不衰减
    "permanent": None,  # 永久，永不衰减，但不进开场包
    "fact": 90.0,
    "decision": 30.0,
    "event": 14.0,
    "short": 2.65,      # 活跃日；约等于"活 7 个活跃日"
}
FADE_BELOW = 0.12          # 低于这个权重 -> faded
PIN_IMPORTANCE = 0.9       # 到这个重要性 -> 钉住（也不再衰减）
MAX_ACTIVE = 300           # 活跃条目上限
SHORT_MAX = 25             # 短期队列上限（超出淘汰最旧）
SHORT_MAX_ACTIVE_DAYS = 3  # 短期活跃日寿命上限
HARD_DELETE_DAYS = 180     # faded 多久后才物理删除
REINFORCE = 0.15           # 每次命中的加权幅度
REINFORCE_MIN_REL = 0.10   # 低于这个相关度不算"想起来"，不加权
PROMOTE_USE_COUNT = 3      # 短期被提及这么多次 -> 晋升为 event
ZOMBIE_DAYS = 30           # short 的墙钟兜底
FADED_FALLBACK_REL = 0.15  # 主检索最好成绩低于此值才翻 faded
FADED_RESCUE_MIN_REL = 0.15  # 兜底命中相关度到这一档才允许挤掉活跃条目
FADED_RESCUE_MAX = 2         # 一次兜底最多保底几条 faded
FADED_CANDIDATE_BUDGET = 2000  # 兜底候选上限（token 按 df 从小到大取，累计超过就停）
CONSOLIDATE_INTERVAL = 12 * 3600
```

三条生效规则，别搞混：

| 改了什么 | 怎么生效 |
|---|---|
| `core.py` / `server.py` 代码 | **重启 dsh**（MCP 子进程在启动时拉起，不热更） |
| `hook_session.py` / `hook_stop.py` | **下一次触发就生效**（每次都是新进程） |
| profile 的 `cordis.patch.yml` | 会**热重载**，但这正是危险所在——见上面的坑 5 |

---

## 自查

```powershell
cd <本目录>

python selftest.py            # 71 项，走真 MCP 协议 + 衰减/晋升/检索范围，用临时库

python memview.py             # 活跃条目，按 kind 分组，带当前权重
python memview.py 12          # 看 #12 这条的全部字段
python memview.py --kind short    # 只看短期
python memview.py --topic 项目     # 只看某个主题
python memview.py --grep Redis    # 关键词（text 和 tags 都搜）
python memview.py --stale         # 权重 < 0.30、快过期还没死的
python memview.py --all           # 连 faded/superseded/phantom 一起看
```

---

## 已知边界

1. **写入靠调用方自觉。** MCP 是请求-响应，server 没法自己决定"这句话值得记"。
   所以 AGENTS.md 里写了六个硬触发点。这是整套方案最薄的一环 —— 实测过，
   让模型"顺手记"，它不记。
2. **"每轮更新短期"也靠自觉。** 钩子能管**投递**（内容自动进上下文），管不了**执行**
   （我每轮该不该写一条 short）。DSH 没有回合结束事件，这条自动化不了。
3. **没有 embedding。** 词面不撞就召回不到（"高兴/开心"不通）。
4. **单进程、非并发安全。** SQLite 默认串行，多会话同时写可能偶发 `database is locked`。
5. **明文 SQLite。** 没有加密。敏感词在写入层被拒，但其余的都在盘上明文躺着。
6. **没有"记忆重构"**：不会用现在的情绪改写过去。这版只做增、降级、合并。

---

## 卸载

三步，顺序别反：

1. 关掉 dsh（**必须先关**，否则改配置会热重载、把当前对话弄断）。
2. 删掉 `~/.dsh/profiles/web/cordis.patch.yml` 里的 `hooks-claude-code` 和 `sandbox-policy` 两段；
   删掉 `~/.dsh/cordis.patch.yml` 里的 `mcp-memory` 段。
3. 可选：删 `~/.dsh/AGENTS.md` 的「长期记忆」一节、删 `~/.dsh/memory/`、删
   `dsh plugin --profile web remove @deepseek-ai/dsh-hooks-claude-code @deepseek-ai/dsh-hook-protocol`。

备份都在，随时可回滚：`~/.dsh/*.bak-before-*`、`~/.dsh/profiles/*/*.bak-*`。

## 文件

```
server.py       MCP stdio 协议层（initialize / tools/list / tools/call / ping）
core.py         存储、检索、衰减、整合、日期唤醒
hook_session.py SessionStart 钩子：新会话自动注入完整开场包
hook_stop.py    Stop 钩子：检查状态是否本轮更新过，没更新就点名（block + reason）
set_state.py    写「当前状态」那一行——由模型调用，不是 LLM
memview.py      命令行看库
selftest.py     71 项自测（走真 MCP 协议 + 引擎行为，用临时库）
```

外部依赖（都不在本目录）：

```
~/.dsh/cordis.patch.yml                 注册 mcp-memory（机器级，所有 profile 生效）
~/.dsh/profiles/web/cordis.patch.yml    挂 hooks-claude-code + 放开 sandbox-policy
~/.dsh/profiles/web/package.json        两条 npm 依赖
.deepseek_code/.claude/hooks.json       钩子配置（SessionStart → hook_session.py；Stop → hook_stop.py）
```

> `memview.py` 原来叫 `inspect.py` —— **这个名字会把标准库 `inspect` 遮蔽掉**，
> 导致 `server.py` 一启动就崩（`dataclasses` 里 `inspect.signature` 找不到）。
> 同目录下不要放与标准库同名的文件。
