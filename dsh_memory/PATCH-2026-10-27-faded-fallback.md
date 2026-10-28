# 补丁 2026-10-27：faded 兜底提速 + 保底

> **已被 2026-10-28 的倒排索引取代**（见 `PATCH-2026-10-28-token-index.md`）：
> 本文的 LIKE 粗筛、`_fetch_faded`、`_faded_tokens` 已删除；空查询不兜底与保底逻辑保留。
> 保留本文的原因：它记录了当初的实测与两个坑，正是这次换结构的依据。

> 改动文件：`core.py`（`recall` / `_pass` / 新增 `_fetch_faded` `_faded_corpus_stats` `_faded_tokens`）、
> `selftest.py`（新增 6 项回归）。
> 备份：`core.py.bak-before-faded-fix`（本补丁第一版前）、`core.py.bak-before-faded-rescue`（保底前）。

---

## 一、背景：两个问题

### 1. 开场包随 faded 增长越来越慢

`recall()` 在「主检索最好成绩 < 0.15」时会去翻 faded 兜底。开场包用的是**空查询**，
空查询的相关度恒为 0 → 必然 < 0.15 → 必然触发兜底 → 把全库 faded 捞出来逐条 tokenize + 算余弦。

实测（合成数据，本机）：

| 活跃 / faded | 开场包 |
|---|---|
| 300 / 0 | 7.8 ms |
| 300 / 3,000 | 56 ms |
| 300 / 20,000 | 384 ms |
| 10,000 / 100,000 | 1,917 ms |

每个新会话付一次，纯浪费：开场包本来就不该带「已经淡忘的东西」。

### 2. 兜底就算跑了也没用

兜底结果与主检索结果合并后**只按 `score` 截断**到 `limit`，而 faded 还要乘 0.55 淡忘系数。
一条真正相关的旧记忆（relevance 0.249、总分 0.211）会被 8 条 relevance 0 但权重高的
活跃条目（各 0.27）整批挤掉 —— 兜底等于白做。

---

## 二、思路

1. **空查询不该兜底**：没有查询词就没有「相关」可言，兜底只会把整个 faded 池拖进来。
2. **兜底候选先粗筛**：全扫的成本不在 SQL，而在 Python 侧逐条 `tokenize` + 余弦。
   用 SQL `LIKE` 在 C 里先过一遍，命中集通常只有几条。
3. **粗筛不能改变分数**：`_pass` 的 IDF 是「候选集内」算的。候选集从 2 万缩到 1 条，
   IDF 会退化成全相等、长文档余弦被稀释 —— 实测正确的条目从第 1 名直接掉出前八。
   所以粗筛之后，IDF 必须仍按**整个 faded 池**统计。
4. **保底名额**：合并后截断时，给「相关度足够高」的兜底命中留位置，
   但不能把更相关的活跃条目挤掉，也不能一次放太多。

---

## 三、改动步骤

### 步骤 1：空查询不兜底

`recall()` 的兜底条件由 `if not include_faded:` 改为 `if not include_faded and qt:`。

效果：开场包从 384 ms 降到 ~10 ms（2 万 faded），且开场包不再混入 faded 条目，
与其「只带 profile / 事实 / 决定 / 最近事件」的定义一致。

### 步骤 2：兜底候选粗筛（`_fetch_faded`）

```python
where = ["status='faded'"]
# kind / topic 过滤保留
clause = " OR ".join(["(text || ' ' || tags) LIKE ? ESCAPE '\\'"] * len(toks))
```

- 匹配 `text || ' ' || tags`：`_pass` 的打分本来就把 tags 拼进文本，粗筛必须同口径，
  否则「只在标签里出现」的条目会被漏掉。
- `ESCAPE '\'` + `_like_escape()`：英文 token 里的 `_`（如 `dsh_memory`）是 LIKE 通配符，
  不转义会匹配到 `dshXmemory` 之类的诱饵。
- 带 `topic` 时保持全扫：`_pass` 会给 `rel == 0` 且 topic 相同的条目标 0.12 底分，
  粗筛会改掉这个行为，而 topic 本身已经缩小了候选集。

### 步骤 3：IDF 按全池统计（`_faded_corpus_stats`）

一条 SQL 在全 faded 池上数文档频率：

```sql
SELECT COUNT(*),
       SUM(CASE WHEN (text || ' ' || tags) LIKE ? ESCAPE '\' THEN 1 ELSE 0 END), ...
FROM memories WHERE status='faded'
```

`idf = log((N+1)/(df+0.5)) + 1`，`unseen = log(N+1) + 1` —— 与原来「全扫时把 2 万条
faded 当候选集」算出来的数完全一致。`df == 0` 的 token 不放进 idf，交给 `_cos_idf`
用 `unseen` 兜底（保持与全扫一致）。

`_pass()` 相应加了两个可选参数 `idf` / `unseen`；不传时仍走「候选集内算 IDF」，
主检索路径完全不受影响。

### 步骤 4：兜底保底（`recall()` 合并段）

```python
dropped = sorted((e for e in added
                  if e["id"] not in kept and e["relevance"] >= FADED_RESCUE_MIN_REL),
                 key=lambda x: x["relevance"], reverse=True
                 )[: max(1, min(FADED_RESCUE_MAX, cap // 4))]
for e in dropped:
    victim = min(out, key=lambda x: x["relevance"])
    if victim["relevance"] < e["relevance"]:
        out[out.index(victim)] = e
```

- 门槛 `FADED_RESCUE_MIN_REL = 0.15`（与兜底触发阈值同档）；
- 上限 `FADED_RESCUE_MAX = 2`，且不超过 `limit // 4`，防止小 limit 下 faded 淹结果；
- 只有当**结果里相关度最低那条比它更低**时才替换 → 不会挤掉更相关的活跃条目；
- 替换后重新按 score 排序，保底条目仍排在下游，但保证在结果里。

### 参数

```python
FADED_PREFILTER_TOKENS = 8   # 粗筛最多用几个 token（每个 token 一遍全表扫描）
FADED_RESCUE_MIN_REL = 0.15  # 保底门槛
FADED_RESCUE_MAX = 2         # 一次兜底最多保底几条
```

---

## 四、踩过的坑（都已修）

### 坑 1：粗筛让 IDF 退化，正确条目掉出结果

第一版只做粗筛，查询 `zenbookx1` 时正确条目从第 1 名**消失**。
原因：候选集从 2 万变 1 条，IDF 全相等 → 长文档余弦被稀释。
解法是步骤 3 的全池统计。

### 坑 2：测试方法本身会污染结论

在同一个库上连跑三次 `recall`，命中会 reinforce，甚至把 faded **复活成 active**
（`_pass` 的强化逻辑），后面的结果被前面的调用改掉。
A/B 必须**每条查询用库副本、只调一次**。

### 坑 3：LIKE 通配符

`dsh_memory` 里的 `_` 会匹配任意单字符。必须 `ESCAPE` 并转义 `%` `_` `\`。

---

## 五、验证

### 自检（`python selftest.py`）

77 项全过（原 71 + 新增 6）：

```
[ok] 空查询不触发 faded 兜底
[ok] 非空弱查询仍会触发兜底
[ok] 粗筛与全扫的排序结果一致
[ok] LIKE 里的下划线被转义（不误伤 dshXmemory）
[ok] 强相关的 faded 命中不会被无关活跃条目挤掉
[ok] 保底最多放 2 条 faded（实得 2）
```

### A/B（`/tmp/ab2.py`：同一份库的副本、每条查询只调一次，新旧代码对比）

| 查询 | 旧版 | 新版 |
|---|---|---|
| `zenbookx1` | 目标**不出现** | 出现（relevance 0.249） |
| `qnx77` | 目标**不出现** | 出现 2 条（含只在 tags 里命中的） |
| `量子咖啡机检修` | 出现 | 出现（不变） |
| `项目配置路径` / `完全无关` / 空查询 | — | 结果不变 |

### 边界用例（11 项全过）

`%` / `_` 转义、单字查询、含单引号查询、kind 过滤、topic 过滤、`include_faded=True`、
超长查询（token 远超上限）、空库、无 faded 库、MCP 协议回环。

### 性能（合成数据，本机；同一负载下背靠背测）

| 活跃 / faded | 开场包 旧→新 | 弱查询 旧→新 |
|---|---|---|
| 300 / 3,000 | 56 → 9 ms | 190 → 37 ms |
| 300 / 20,000 | 384 → 12 ms | 1,155 → 86 ms |
| 3,000 / 50,000 | 923 → 77 ms | 3,050 → 421 ms |
| 10,000 / 100,000 | 1,917 → 253 ms | 6,409 → 1,155 ms |

同负载背靠背（20k faded）：弱查询 1,707 → 117 ms，开场包 516 → 14.5 ms，强查询持平。
强命中查询本来就不碰 faded，只有活跃条目数会影响它。

---

## 六、取舍与遗留

- **faded 的 0.55 折扣没动**：它保证「活跃优先」，保底只解决「一条都出不来」的极端情况，
  不改变常态排序。
- **粗筛是「任一 token 命中」的超集**，不会漏掉 relevance > 0 的条目；但 relevance 恰好为 0
  的 faded 条目不再被兜底带出来（原来会按权重顺带排出几条）—— 这些本来就是噪音。
- **长查询成本仍随 token 数增长**：每个 token 一遍全表扫描（20k 行约 8 ms），
  所以上限压到 8 个 token。
- `consolidate()` 的合并仍是同 kind 内两两比对（O(n²)），3,000 条活跃时约 39 秒；
  正常被 `MAX_ACTIVE = 300` 挡住，但一次性灌库会卡一次，这次没动。
