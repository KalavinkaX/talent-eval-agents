# 第 9 课课堂笔记：可信人才检索、事实合并与证据包

> 复习用快照｜2026-09-25｜代码依据：分支 `lesson-9_note`，HEAD `bc72761` 加当时未提交的注释；这份笔记说明的是**当前实现**，不是接口永远不变的承诺。适合先读此文建立地图，再读同目录《lesson9-可信人才检索与证据输出-完整学习与课后作业指导.md》看逐行与作业细节。文档放在仓库外，**不会自动进入当前 Git 提交**。

## 一、全局架构先看一张图

```text
POST /api/talent-search + include_evidence_pack=true
  ├─ compile_query_plan(LLM) → QueryPlan：过滤条件、语义要求 S1/S2…
  ├─ select_candidate_ids(PostgreSQL) → 候选人 ID 列表
  ├─ 每项要求 hybrid_search_evidence_service(Milvus + 精排) → 检索命中 Chunk
  │    └─ requirement_ids_by_chunk：同一 Chunk 可以服务多个要求
  ├─ load_pack_sources(PostgreSQL)：重新取原文/重验租户与权限/排旧版本
  │    └─ 可扩展同候选人、同版本、有权限的父 Chunk
  ├─ 对每位候选人 × 每项要求 build_evidence_packs
  │    ├─ model_extractor：原文 → EvidenceExtraction（候选事实、来源声明、缺口）
  │    ├─ _validate_fact_sources：引用 ID 属于本次来源且 quote 是连续原文
  │    ├─ _merge_duplicate_facts：同一事实合并，保留所有验证过的来源
  │    ├─ _find_conflicts：对合并后所有事实按同范围 yes/no 成对检查
  │    └─ _evidence_status：冲突 > 充分 > 部分 > 无相关事实
  └─ JSON：query_plan/candidate_ids/searches/chunks/evidence_packs/evidence_status
```

**设计主线**：检索负责“找可能相关的材料”，LLM 负责“将原文提出为可审查的 yes/no 事实”，程序负责“准入、精确去重、显式冲突、降级与状态”；程序没有证明材料真实、结论正确，也没有为候选人最终打分。可信的准确表述是“可追溯、受权限约束、可识别部分失败的证据整理”，而不是“全自动事实核实”。

## 二、为什么要多加一层证据包

直接把 Chunk 交给后续评估会出现四种混淆：① 两份材料重复说一件事，会被误认为两个独立事实；② 面试记录说 no，复盘材料说 yes，排序在非相邻位置也应发现冲突；③ 引用可能捏造或指向不属于本次候选人/要求的 Chunk；④ 模型超时或权限不足不能当作“候选人没有该经历”。证据包按 **candidate_id → requirement_id → facts/sources/conflicts/status** 组织，使下游能区分“没检索到”“检索到了但无关”“抽取失败”“同范围冲突”“有限支持”。

入口对应 `backend/app/api.py:565–677`。默认 `include_evidence_pack=False` 时沿用原有检索响应；开关为 true 且候选集为空时返回 `no_candidates`，不会走检索和抽取；有候选人时按查询计划逐项检索。注意 `/api/talent-search` 请求头带 `X-Tenant-ID`、`X-Permission-Scopes`，代码用于过滤/回库校验，**不能凭这一段代码证明头部已由可信认证层签发**。

## 三、对象协议：每个字段在解决什么问题

`backend/app/evidence_pack.py:12–33` 的 Pydantic 模型用 `extra='forbid'` 拒绝多余字段：

```python
FactSource(chunk_id: str, quote: str)  # 模型提出的来源声明；quote 必须是来源正文中的连续片段
ExtractedFact(event, period, claim, answer, sources)
# event='星河项目'；period='2025' 或 '2025-01/2025-06' 或 'unknown'
# claim='担任项目总负责人'；answer='yes' / 'no'；sources 至少一个
EvidenceExtraction(facts=[...], fully_supported=False, missing_information=[])
```

- `event/period/claim` 是**冲突比较的范围**；`answer` 是立场。`period` 正则只检验字符串形状，不检查月份实际有效、两个范围是否重叠；`unknown` 是可接受值，但冲突检测不会用未知范围造冲突。
- `sources` 是**模型声称引用的** `chunk_id + quote`，随后必须对照已获准的原文。事实没有来源则 Schema 校验失败。
- `fully_supported` 是**模型自报**是否覆盖查询要求全部要素，不是程序二次证明。`missing_information` 是模型报告的缺口，必须是非空字符串列表；无缺口用 `[]`，不是 `['']`。
- 结构化事实经 `_validate_fact_sources` 后，其 `sources` 每项会增加从可信 row 得到的 `citation_id`；字段并非模型凭空指定。最终 `CandidateEvidencePack` 是返回用的嵌套字典，`EvidenceExtraction` 只是中途的模型输出协议。

## 四、从接口到最终包：亲手跟一条数据

### 4.1 回库来源与引用边界

索引命中初形状：

```python
{'chunk_id': 'a', 'candidate_id': 'C901', 'content': '索引缓存文本',
 'requirement_ids': ['S1'], 'score': 0.5, 'metadata': {...}}
```

`api.py:659–670` 调 `load_pack_sources(db, chunks, tenant_id, permission_scopes)`：`resolve_citation` 顺着 `DocumentChunk → DocumentVersion → Document → KnowledgeBase` 核对租户、状态、候选人、文档与 Chunk/知识库作用域，取**数据库原文**、版本和定位信息。`load_pack_sources` 跳过旧版本/撤权命中；如存在合格的父 Chunk，可将其作为额外来源并合并 `requirement_ids`。返回来源像：

```python
{'chunk_id': 'a', 'candidate_id': 'C901', 'citation_id': 'a',
 'content': 'C901 担任星河项目总负责人。', 'document_version_id': 'v1',
 'requirement_ids': ['S1'], 'markdown_start': 10, ...}
```

`response['chunks']` 被筛为回库成功的**原检索命中**并替换正文；父 Chunk 可能加入证据包引用，但不会因为扩展就必然出现在 `response['chunks']`。原 `metadata` 等其他索引字段并非此处逐个回库重建，不应一概称为已验证。直接 GET `/api/evidence/citations/{chunk_id}` 可解析授权历史引用；用于**新构包**的 `load_pack_sources` 则只接受当前版本，两者不矛盾。

### 4.2 模型抽取与来源校验（顺序不可反）

`model_extractor(model)` 返回闭包 `extract(requirement, rows)`；只把每条获准来源的 `chunk_id/content` 和本项 `requirement['query']` 放入消息，`with_structured_output(EvidenceExtraction).invoke(...)` 返回结构化对象。例如模型声称：

```python
{'facts': [{'event':'星河项目', 'period':'2025', 'claim':'担任项目总负责人',
            'answer':'yes', 'sources':[{'chunk_id':'a','quote':'担任星河项目总负责人'}]}],
 'fully_supported':True, 'missing_information':[]}
```

`build_evidence_packs` 先 `EvidenceExtraction.model_validate(raw)`（如 raw 还不是该模型），再执行 **`_validate_fact_sources(data.facts, rows)` → `_merge_duplicate_facts(...)`**。验证器把 `rows` 建为 `by_id`；对模型每一个 `ref` 必须找到本次来源 `row`，且 `ref.quote.strip()` 非空、**整个 quote 连续出现在 row['content']**；否则抛 `ValueError('unverifiable_source')`，外层把该要求标 `partial/extraction_failed`，清空未经确认的事实而保留 `citations=rows`。验证通过后返回普通字典列表，来源映射为 `{'citation_id':row['citation_id'],'chunk_id':ref.chunk_id,'quote':ref.quote}`，同一来源映射只保留一次。

**关键辨析**：`data.facts` 的来源和正文都基于同一批 `rows` 让模型抽取，为什么还校验？因为“喂给模型”≠“模型输出准确复述”，模型仍可能引用错 ID、杜撰 quote、跨要求引用。这个检验只证明字符串和指定 Chunk 对得上；它**不能**证明 “quote 语义确实支持 yes/no”、来源事实可信、模型没有忽略否定词，也不保证召回完整。

### 4.3 合并、冲突、状态

去重键严格为 `(event,period,claim,answer)`。同四元组的两个事实合并为一条，经过验证的多个来源收进一个 `sources` 数组；不是“同一个 Chunk 才算重复”，也不是语义相近就自动合并。冲突键严格为 `(event,period,claim)` 且三个字符串都不等于 `unknown`，同时答案集合恰为 `{yes,no}`。双层循环遍历**合并后所有不同事实**，故非相邻事实也能形成 `[[0,2]]`。项目不同、时间文本不同或 claim 文本不同，都不会被判为同范围；部分重叠年份和别名尚无语义归一算法。

`_evidence_status` 只处理**成功抽取后的事实集合**：

| 优先级 | 条件 | 结果 | 小心误读 |
|---|---|---|---|
| 1 | `conflicts` 非空 | `conflicting` | 不裁定 yes/no 谁真 |
| 2 | 有至少一个 yes，且模型报 `fully_supported=True`、`missing_information=[]` | `sufficient` | 非客观证明完全满足 |
| 3 | 有 `facts`，但以上条件不满足 | `partial` | 此处**不代表抽取失败**；可以只有 no |
| 4 | `facts=[]` | `missing` | 也可能已有 Chunk，只是未提取相关事实 |

`build_evidence_packs` 外层另有初始项 `missing/no_accessible_hits/not_run`（该人该要求无获准来源）及异常分支 `partial/extraction_failed/failed`（模型、Schema、来源校验或程序异常），别把这些原因硬塞进 `_evidence_status`。成功分支的 `reason='evidence_review' if facts else 'no_relevant_evidence'`；`status`、`reason`、`extraction_status` 共同解释结果，单看 `status` 容易误解。

## 五、五 Chunk 课堂实验：手算再验证

| 原始序号 | 来源 | `(event,period,claim,answer)` | 合并去向 |
|---:|---|---|---|
| 0 | x0 | `(星河,2025,总负责人,yes)` | 事实 0，来源 x0 |
| 1 | x1 | `(远航,2025,技术负责人,yes)` | 事实 1，来源 x1 |
| 2 | x2 | `(星河,2025,总负责人,no)` | 事实 2，来源 x2 |
| 3 | x3 | `(远航,2024,技术负责人,no)` | 事实 3，来源 x3 |
| 4 | x4 | `(星河,2025,总负责人,yes)` | 并入事实 0，来源 x0+x4 |

输出应为 **4 事实、事实 0 有两个来源、`conflicts=[[0,2]]`、`status='conflicting'`**。x1/x3 年份不同不冲突。`backend/tests/lesson09_multiscope.py` 当前用五条**手工固定的结构化事实**注入 `extract`，测的是确定性后处理，不是实际 LLM 生成行为；要提交“结合任意一个 LLM 生成五条事实 Chunk”的完整作业，还应单独保存合成 Chunk 生成/模型原始输出记录，避免把固定桩伪装成模型输出。

## 六、可信边界、剩余风险与如何改善

1. **原文 ≠ 真相**：数据库重新取到的是受权限约束的已存材料，不是第三方核实。可加入独立来源、人工复核、版本/作者签名和跨材料一致性检查。
2. **引用包含 ≠ 支持结论**：`quote in content` 不解析否定、时间和职责范围。可在保留原文的前提下加约束性校验/自然语言推理复核，仍保留人工审查。
3. **模型自报完整 ≠ 完整**：`fully_supported` 可能判断错；建议把查询拆成必须覆盖的槽位并逐项核验，保留缺口。
4. **精确字符串比较的边界**：同义词/简称、`2025` 与 `2025-01/2025-06`、重叠时间范围不会自然合并或冲突；可引入有证据可追溯的实体和时间归一化，不能盲目把所有相似文本合并。
5. **召回/权限/注入**：没命中不等于不存在；文档正文可能包含“忽略指令”的提示注入；请求头如果未由上游可信认证层保护，权限链可能被绕过。分别需要召回质量评估、数据与指令隔离、服务端认证授权。
6. **异常归因**：`except Exception` 让用户安全降级，但 `extraction_failed` 不专指 LLM 超时，也可能是代码异常；日志只记异常类型，运维要另配安全的监控与追踪。

## 七、复习与提交前检查（就这次分支状态）

- 运行 `pytest tests/test_evidence_pack.py tests/test_evidence_citations.py tests/test_talent_search_api.py -q`：本次 **23 passed**，覆盖本课主要接口、权限和整理逻辑。
- 五 Chunk 文件应从 `backend` 目录运行 `python -m tests.lesson09_multiscope`，本次输出 `[[0,2]] / conflicting`；直接 `python tests/lesson09_multiscope.py` 在本机没有将 `backend` 加到 `sys.path`，报 `ModuleNotFoundError: app`。该文件不是 `test_*.py` 且无 `def test_...`，**不会被普通 pytest 自动发现**。
- 本次全量 pytest：**104 passed、2 skipped、11 failed、2 errors**；失败集中在缺少 `sample-data/generated` 测试材料、临时目录权限设置；本次无法声称“整个仓库测试全绿”，但不能据此判定第 9 课整理逻辑失败。提交前若项目要求全绿，应补齐测试夹具/环境后复测。
- 当前 `backend/tests/lesson09_multiscope.py` **暂存区为空文件，40 行内容仍在工作区**（`git status` 显示 `AM`）。若要把实验内容一起提交，必须明确将该文件的最新内容加入暂存区，并用 `git diff --cached` 检查，不要以为 `AM` 等于内容已暂存。
- `.idea/`、`logs/`、临时探测脚本及未跟踪的 `docs/` 与第 9 课注释不应被盲目 `git add -A`。本课笔记位于仓库外的 `D:\Code\K_Course\talent-eval-agents_docs\homework_docs`；如果“将来回到这个 Git 提交就能看到笔记”是硬要求，还需在仓库内另存一份并明确纳入提交。**此文档所在目录的变更不会跟着当前分支提交。**
- 代码注释中两处建议提交前手动修正：`_evidence_status` 的 `partial/missing` 注释；`if ref.quote ...:` 行末孤立的 `#`。具体建议见原学习文档新增的“七、注释审阅与提交门槛”。

**三问自测**：给出两个相同四元组、不同来源，是否合并？（是，保留两个来源。）给出同三元组、相反答案，是否覆盖哪个为真？（否，只标冲突。）模型超时而已有 Chunk 是否返回 missing？（否，`partial/failed` 且保留原始 citations。）