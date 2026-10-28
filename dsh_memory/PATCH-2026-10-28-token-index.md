# 补丁 2026-10-28：faded 兜底改成倒排索引

> 改动文件：`core.py`（新增 `mem_tokens` / `token_df` 两张派生表、索引维护与自愈、
> `_faded_candidates` / 重写 `_faded_corpus_stats` / `_pass` 加 `tfs` 入参）、
> `selftest.py`（新增 12 项回归）、`README.md`（兜底一节）。
> 取代 2026-10-27 的 LIKE 粗筛（`PATCH-2026-10-27-faded-fallback.md`）。

---

## 一、为什么换掉 LIKE 粗筛

2026-10-27 那版把兜底候选从「全池」缩到「LIKE 命中的几条」，但缩完后有两个问题
无法在**不建索引**的前提下同时解决：

| 问题 | 实测 |
|---|---|
| `_faded_corpus_stats` 只统计**查询词**的 df，文档侧 token 全落到 `unseen`（`log(N+1)+1`），而全扫里它们各自有 df | 400 条池子：目标 rel 全扫 0.174 → 新版 0.081；2 万条：0.118 → 0.082 |
| `_faded_tokens` 只取前 8 个 token，命中词排在后面就整条漏掉 | 「量子咖啡机检修记录归档放在哪里」：旧版返回目标 rel 0.221，补丁版返回空（命中词 `放在/在哪/哪里` 排第 11–13 位） |
| `kind=` 过滤时干脆不传 idf，只有候选集内 IDF | 同一目标 rel 0.174 → 0.086 |

要「分数与全扫一致」，就得把候选文档里**每个词**都在全池上数 df。实测多 CASE 的
单条 SQL 在 2 万条池子上要 845 ms（全扫 1.0 s），提速就没了 —— 所以必须换结构。

## 二、表结构

只索引 `status='faded'` 的条目。活跃条目有 `MAX_ACTIVE = 300` 条上限，直接全扫更便宜。

```sql
CREATE TABLE mem_tokens (          -- 一条 faded 条目的 token 与词频
    memory_id INTEGER NOT NULL,
    token     TEXT NOT NULL,
    tf        INTEGER NOT NULL,
    PRIMARY KEY (memory_id, token)
);
CREATE INDEX idx_tok_token ON mem_tokens(token);

CREATE TABLE token_df (            -- 每个 token 在 faded 池里出现在多少条文档中
    token TEXT PRIMARY KEY,
    df    INTEGER NOT NULL
);
```

两张表都是**派生数据**，任何时候都能从 `memories` 整表重建（`_rebuild_token_index`）。

## 三、维护点

索引跟着「进入/离开 faded」走，文本更新不需要额外处理（`_find_duplicate` 只会改
`active/pinned` 的正文，它们不在索引里）：

| 位置 | 动作 |
|---|---|
| `consolidate()` 降级 / 上限截断 / `trim_short()` | fade → `_reindex` |
| `_pass()` 里 faded 复活 | 离开 faded → `_reindex` |
| `forget()` / `pin()` | 若原状态是 faded → `_reindex` |
| 物理删除（180 天） | `_index_remove` |
| `Store.__init__` / `consolidate()` 开头 | `_verify_token_index()` 对账，不一致就重建 |

`token_index_docs` 存在 `meta` 里，`__init__` 用「索引文档数 vs 实际 faded 数」做廉价体检；
`consolidate()` 再用「`mem_tokens` 行数 vs `sum(df)`」做一次完整对账，漏掉的增删最迟
12 小时自愈一次。

## 四、检索路径

```
recall(q) → 主检索（active/pinned，不变）
         → 最好成绩 < 0.15 且 q 非空
         → _faded_candidates：按 token 精确取候选
         → _faded_corpus_stats：df 从 token_df 直接读，算全池 idf
         → _pass(idf=…, tfs=…)：打分与全扫逐位相同
         → 合并 + 保底（逻辑不变）
```

`_faded_candidates` 与全扫的差别只有三处，都是有意的：

1. **rel=0 的条目不进候选**。全扫会按权重把「相关度 0 但 importance 高」的 faded 也排进前八，
   实测真实库里它们能占掉 4–6 个名额，把真正命中的挤出去。2026-10-27 那版已经这么取舍过。
2. **单字不参与选候选**。选择性太差（一个「的」能捞大半个库），且单字命中的条目相关度本来就低。
3. **查询里全是高频词时按预算截断**：token 按 df 从小到大取，累计超过
   `FADED_CANDIDATE_BUDGET = 2000` 就停，被丢掉的都是高频词命中的条目。

带 `topic` 时保持全扫（`_pass` 会给 rel=0 且同 topic 的条目 0.12 底分，索引取候选会改掉这个行为）。

## 五、实测

### 性能（合成数据，本机）

| faded 池 | 建索引（一次性） | 索引行数 | 弱查询 `zenbookx1` | 全扫 |
|---|---|---|---|---|
| 5,000 | 1.7 s | 286,572 | **5 ms** | 261 ms |
| 20,000 | 7.9 s | 1,145,800 | **6 ms** | 1,010–1,298 ms |

对比 2026-10-27 那版（LIKE 粗筛）：20k 时 21 ms —— 但相关度是偏低的（见第一节）。
新方案在**分数正确**的前提下更快。

**病态长查询**：`量子咖啡机检修记录归档放在哪里` 在「两万条全都含『记录』」的合成池上，
候选会退化成两万条，要 **2.1 s**（5k 池子 390 ms）。耗时在 Python 侧 tokenize 两万条文档，
不是 SQL；这种查询本来也不该靠兜底解决。真实库规模下无感。

### 正确性

- 自检新增 12 项：索引与暴力重算一致、tf 一致、破坏后自愈、forget/pin/删除同步、
  长查询召回、kind 过滤的 idf/unseen 与全扫逐字一致、rel=0 噪音不进候选。
- 真实库（45 条 faded）逐条对比：同一候选的相关度与全扫**完全相同**
  （如 #27 0.496、#45 0.181、#56 0.214），差异只在候选集合 —— 也就是被排除的 rel=0 噪音。
- 真实库三版对比（旧全扫 / 2026-10-27 补丁 / 本版）：本版与补丁捞出的条目基本一致，
  相关度更接近全扫；`SessionStart 注入` 这类查询里补丁的近似 IDF 会把 #58 抬进保底，
  本版按全扫分数选的是 #56/#48。

## 六、取舍与遗留

- **索引占空间**：20k faded 约 115 万行。只在 faded 上建，活跃条目零成本。
- **首次迁移**：老库第一次跑新代码要扫一遍 faded 建索引（20k 约 7.9 s，5k 约 1.7 s）。
  当前真实库 45 条，可忽略。
- **单字与预算两条取舍**会漏掉「只被单字/极高频词命中」的 faded 条目，它们本来就是噪音。
- 索引是派生数据，删除 `mem_tokens` / `token_df` 不会丢任何记忆；下次打开自动重建。
