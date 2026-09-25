# 第 9 课：可信人才检索与证据输出——完整学习笔记与课后作业

> 代码依据：当前工作区 `D:\Code\K_Course\talent-eval-agents_learning`，核对日期：2026-09-25。本笔记讲的是**该分支实际实现**，不把设想当成已有功能。C901/C902、项目名称及引文均为课程合成材料，不能用于真实人事决策。

## 一、先用一句话理解这节课

第 8 课解决“去哪里找、找到了哪些 Chunk”；第 9 课解决“这些 Chunk 究竟提出了哪些**带出处的可核对陈述**，哪些陈述相同、相反或缺失”。关键不是让 LLM 直接裁判谁适合，而是把不可信的材料和模型输出限制在可验证协议内：**检索 → 回库鉴权 → 模型抽取 → 来源校验 → 确定性去重与冲突 → CandidateEvidencePack**。证据包是供下游评估参考的材料组织结果，不是真相认证，也不是录用建议。

### 1.1 老师要你建立的五个区分

1. **命中 ≠ 满足要求**：`负责排期` 不能自动推导为 `总负责人`；`未担任总负责人` 不能因为语义接近就当作支持。
2. **可追溯 ≠ 真实**：quote 是原文子串，只证明“这个 Chunk 写过这句话”，不证明材料内容真实，也不证明模型的 `answer` 解释正确。
3. **去重 ≠ 丢来源**：相同命题、同一时期、同一答案可以合并事实，但必须保留所有独立引用。
4. **冲突 ≠ 否定**：同一范围出现 yes/no 时标记待核查，既不多数投票，也不直接判 no；不同项目/时期/命题不得误报冲突。
5. **检索无命中 ≠ 抽取失败**：空材料可以是 `missing`，有材料而模型/校验失败应是 `partial + extraction_failed`，不能伪装成“没有经历”。

### 1.2 核心数据结构（简化展示）

```json
{
  "schema_version": "2.0",
  "candidate_id": "C901",
  "requirements": [{
    "requirement_id": "S1",
    "query": "2025年上半年担任星河项目总负责人",
    "status": "conflicting",
    "reason": "evidence_review",
    "extraction_status": "succeeded",
    "facts": [{
      "event": "星河项目", "period": "2025-01/2025-06",
      "claim": "担任项目总负责人", "answer": "yes",
      "sources": [{"citation_id": "…", "chunk_id": "…", "quote": "原文连续片段"}]
    }],
    "conflicts": [[0, 1]],
    "missing_information": [],
    "citations": []
  }]
}
```

这是**形状示例**，不是本次实验的真实 UUID/完整响应。顶层响应还含 `query_plan`、`candidate_ids`、`searches`、`chunks`、`evidence_packs`、`evidence_status`；`evidence_status=reviewed` 只表示走过证据包分支，不等于每个 requirement 都可信或 sufficient。

`ExtractedFact.period` 实际只接受 `unknown`、`YYYY` 或 `YYYY-MM/YYYY-MM`；`answer` 只接受 `yes/no`，事实至少一个来源。完整协议和提示词见 `D:\Code\K_Course\talent-eval-agents_learning\backend\app\evidence_pack.py:12-57`。

## 二、按“入口 → 输出”读真实后端链路

### 2.1 大框架：外层请求与第 9 课新增层

```text
POST /api/talent-search
  → FastAPI 解析 TalentSearchInput / 请求头
  → LLM 编译 QueryPlan → 校验 clarification
  → PostgreSQL 筛出候选编号
  → 对每个 semantic_requirement 做混合检索 / 可选优化
  → 合并命中 Chunk，并记住每个 chunk 的 requirement_ids
  → include_evidence_pack=false：按第 8 课原样返回
  → include_evidence_pack=true：PostgreSQL 重新读取/鉴权当前版本 Chunk
  → 按 candidate_id × requirement_id 分组
  → LLM 输出 EvidenceExtraction
  → 校验 quote/chunk_id → 合并事实 → 全量成对比较 yes/no
  → 计算状态，返回 CandidateEvidencePack
```

入口注册：`D:\Code\K_Course\talent-eval-agents_learning\backend\app\main.py:18-58` 创建 FastAPI、挂载 `router`；`D:\Code\K_Course\talent-eval-agents_learning\backend\app\api.py:44` 的 router 前缀为 `/api`。实际调用是 `api.py:565-675` 的 `search_talent()`。不要误以为 `query_plan.py:153` 的 `execute_composite_search()` 就是该 HTTP 接口的调用链：它是可复用函数，**当前这个入口实际在 `search_talent` 内循环执行**。

请求示例（租户和权限头由调用环境提供；演示值仅用于隔离案例）：

```http
POST /api/talent-search
X-Tenant-ID: course-demo
X-Permission-Scopes: hr_private
Content-Type: application/json

{"query":"在深圳且在2025年上半年担任星河项目总负责人的候选人","include_evidence_pack":true,"retrieval_mode":"standard","limit":8}
```

`api.py:93-100` 定义请求模型：`include_evidence_pack` 默认 `false`，所以旧调用不触发第 9 课加工；`retrieval_mode` 可为 `standard`/`auto_optimize`。`api.py:567-584` 读必需请求头，解析逗号分隔的权限范围，并确认查询模型、Embedding、Rerank 服务就绪。**请求头并非身份认证本身**：安全部署还需要可信认证网关/主体到租户、权限的绑定，不应让任意客户端自报有效权限。

### 2.2 查询计划与候选集：先缩小搜索空间

1. `api.py:586-595` 调 `compile_query_plan(payload.query, model)`；`D:\Code\K_Course\talent-eval-agents_learning\backend\app\query_plan.py:250-262` 使用 `with_structured_output(QueryPlan)`，将硬条件、语义要求、偏好、待澄清项分开。含待澄清项时接口返回 422，不私自补阈值。
2. `api.py:596-599` 调 `select_candidate_ids(db, plan, tenant_id=x_tenant_id)`。`query_plan.py:127-150` 用字段注册表构建参数化 SQL，并加 `tenant_id`、`employment_status=active`；例如“深圳”走结构化过滤，“担任星河总负责人”走语义要求。无候选直接返回空 `evidence_packs` 与 `evidence_status=no_candidates`，**不会拿空 ID 列表去做全库召回**。
3. 可单独学习的接口：`POST /api/talent-search/plan` 位于 `api.py:538-547`，只编译计划；`POST /api/talent-search/candidates` 位于 `api.py:550-562`，接收 `QueryPlan` 和租户头，返回候选编号。它们是分步调试接口，完整证据包仍由 `POST /api/talent-search` 产生。

### 2.3 检索、要求关联、回库重验

`api.py:603-650` 为每条语义要求构造 `EvidenceFilter(tenant_id, permission_scopes, candidate_ids)`，调用 `D:\Code\K_Course\talent-eval-agents_learning\backend\app\hybrid_search_service.py:49-71`：向量化查询 → store 稠密/BM25 融合召回 → rerank → 截取 limit。`auto_optimize` 在首轮无有效命中时走 `query_plan.py:224-247` 的优化查询；标准模式只查一次。`merge_query_results()` 按 `chunk_id` 保留第一次命中；`requirement_ids_by_chunk` 在合并前记下命中归属，所以同一 Chunk 可属于多个要求。`searches` 保存策略、查询和命中数。

第 9 课不能直接信任索引中的文本。`api.py:658-669` 调 `load_pack_sources()`，见 `D:\Code\K_Course\talent-eval-agents_learning\backend\app\evidence_citations.py:7-70`：

- 由 `chunk_id` 回 PostgreSQL 查 Chunk、版本、文档、知识库；检查租户、文档活动状态、权限范围、候选归属；历史版本不进入证据包。
- 有父 Chunk 时仅在**同候选、同版本、可访问、长度不超过 8000**等条件下补上下文；聚合该来源对应的 `requirement_ids`。
- 失效或无权限命中跳过，并用重新读出的文本替换响应 `chunks` 中的索引文本，避免把旧索引内容跟可信引用同时发给前端。

注意：这里验证的是**该请求携带的访问上下文对数据库记录是否可用**，并非证明材料独立真实；授权头来源需要上游保证。被跳过的 Chunk 不进入抽取，可能形成 `missing`。

### 2.4 模型负责抽取，程序负责裁决形式上的可靠性

`api.py:665-668` 将所有候选编号、计划中每项语义要求、经鉴权来源交给 `build_evidence_packs()`，模型抽取器由 `model_extractor(model)` 生成。`evidence_pack.py:36-56` 的 system prompt 要求：只处理相关项目/时期/职责，未知不补；将 `claim` 写成可回答命题；无明确支持/否定的背景不生成事实；引用输入 `chunk_id` 和连续原文 `quote`；不评分不推荐。

`evidence_pack.py:117-145` 的**每候选人 × 每要求**循环是本课核心：

1. 按 `candidate_id` 和 `requirement_id` 筛 `sources`，按 `chunk_id` 去重；初态 `missing / no_accessible_hits / not_run`，保留原始 `citations`。
2. 有材料时 `extract(requirement, rows)`，再用 `EvidenceExtraction.model_validate` 约束结构；此时 `fully_supported`/`missing_information` 仍是模型报告值。
3. `evidence_pack.py:60-74` 对每一事实的每一引用校验：`chunk_id` 必须存在于该组输入，`quote` 去空白后非空，且**精确作为该 Chunk 的连续子串**；真正返回的 `citation_id` 从数据库来源映射，不能让模型自报。
4. `evidence_pack.py:77-89` 以 `(event, period, claim, answer)` 为完整去重键，保留首次出现次序，并并入所有不重复的校验来源；不同 answer **不合并**。
5. `evidence_pack.py:92-103` 对合并后事实两两比较，范围键 `(event, period, claim)` 三者完全一致且均不为 `unknown`、答案一 yes 一 no 才记录索引对。比较全部组合，不是仅比较相邻项。
6. `evidence_pack.py:106-114` 状态优先级：**有冲突 → conflicting**；否则**有 yes 且 fully_supported=true 且 missing_information 为空 → sufficient**；否则**有事实 → partial**；完全无事实 → missing。没有事实但有 Chunk 的 `reason=no_relevant_evidence`；冲突也是 `reason=evidence_review`。
7. 模型调用、格式校验或引文校验任一异常：`partial + extraction_failed`、`facts=[]`，仍保留 `citations`；日志只记异常类型，响应不泄露模型错误详情。注意目前按**整个要求**降级，不是逐条丢弃坏事实。

`status=sufficient` 只表示本轮材料与**模型声称的覆盖程度**满足上述程序条件，**不等于外部核实**；`unknown` 范围不参与冲突；近义项目名、不同时间写法、局部重叠区间目前也不会自动识别为同一范围。

### 2.5 用已有隔离演示串起来

`D:\Code\K_Course\talent-eval-agents_learning\backend\scripts\verify_evidence_pack.py:23-135` 构建内存 SQLite 与 C901/C902、三个虚构材料 Chunk；用固定 QueryPlan、固定检索替身；默认固定抽取结果，`--live-model` 才向项目配置模型发送**这三份合成材料**。完整路由仍真实执行。示例响应文件 `D:\Code\K_Course\talent-eval-agents_learning\backend\artifacts\lesson09-fixture-model.json`：C901 为 `conflicting`、两条合并后事实；C902 无可访问命中为 `missing`。这个现有演示是三条材料，**不是下文的五 Chunk 作业实验**。

## 二·补充：把 `search_talent` 当作程序亲手单步执行（先读这一章，再做作业）

> 阅读路线：先看 §A 的“对象在谁手里”，再从 §B 的入口开始顺着 §C～§I 跑一遍 C901/C902，最后用 §J 的边界测试检查自己是否真正懂了。以下摘取的代码是当前分支的关键语句，省略与主题无关的部分；括号中的 `文件:行号` 可回到真实代码核对。**这一章位于作业之前**，上文 2.1～2.5 可视为总览，本章是逐语句展开。

### A. 新增了什么对象？先辨清“模型输出对象”与“接口输出字典”

```text
请求 TalentSearchInput                       FastAPI/Pydantic 对象
  include_evidence_pack: bool = False          新开关；默认保持旧版输出
  query: str; retrieval_mode; limit; ...

QueryPlan                                    LLM 编译后的 Pydantic 对象（第 8 课）
  filters → PostgreSQL                          如 region=深圳
  semantic_requirements → 检索与分组            [{requirement_id:'S1', query:'...'}]

chunks                                      接口内中间 list[dict]
  [{chunk_id, candidate_id, content, score, metadata, requirement_ids}]
       ↓ load_pack_sources 再读数据库
sources                                     list[dict]，已鉴权且为当前版本
  [{chunk_id, citation_id, candidate_id, document_version_id,
    content, permission_scope, requirement_ids, ...来源定位元数据}]
       ↓ model_extractor(model) 给模型的只有 requirement/query 与 chunk_id/content
EvidenceExtraction                         LLM 输出的 Pydantic 对象（新增）
  facts: list[ExtractedFact]                每条含 event/period/claim/answer/sources
  fully_supported: bool
  missing_information: list[str]
       ↓ 后端验证、去重、比较
Candidate Evidence Pack                    返回值是字典，并非名为 CandidateEvidencePack 的类
  {schema_version:'2.0', candidate_id, requirements:[{..., facts,
    citations, conflicts, status, reason, extraction_status, missing_information}]}
```

**为什么分两类？** `EvidenceExtraction` 是“模型建议的结构化陈述”，尚不可直接用于评估；`sources` 是数据库重读得到的来源；最终 pack 是“引用已机械核验、重复事实已合并、冲突已按规则标出”的接口产物。当前代码没有一个 `class CandidateEvidencePack`；只是返回相应形状的 dict。不要将“模型调用成功”与“证据可信”混为一谈。

对象的代码定义（`backend/app/evidence_pack.py:12-33`）：

```python
class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')  # 模型多吐无关字段，直接拒绝

class FactSource(StrictModel):
    chunk_id: str
    quote: str = Field(min_length=1, max_length=2000)

class ExtractedFact(StrictModel):
    event: str = Field(min_length=1, max_length=200)
    period: str = Field(pattern=r'^(?:unknown|\d{4}|\d{4}-\d{2}/\d{4}-\d{2})$')
    claim: str = Field(min_length=1, max_length=200)
    answer: Literal['yes', 'no']
    sources: list[FactSource] = Field(min_length=1, max_length=24)

class EvidenceExtraction(StrictModel):
    facts: list[ExtractedFact] = Field(default_factory=list, max_length=30)
    fully_supported: bool = False
    missing_information: list[str] = Field(default_factory=list, max_length=10)
```

最后一行是**教学式简写**：实际 `missing_information` 的元素还通过 `Annotated[str, Field(min_length=1, max_length=200)]` 限制长度，见原文件 32–33 行。正则只约束外形，不会检验月份 13 是否合理、起止先后或“2025”与全年区间语义相等。`Literal['yes','no']` 防止模型生成“可能是”这种额外状态，却也要求不确定时不造事实。`fully_supported` 是模型报告的覆盖程度，**并非程序独立计算的真值**。

### B. 请求入口的每一步：哪些变量是新层的原料？

定位：`backend/app/api.py:565-675`，`@router.post('/talent-search')` 加上 router 的 `/api` 前缀就是 `POST /api/talent-search`。开关在 `api.py:93-100` 的 `TalentSearchInput` 中，不是另开一个“证据包 API”。

```python
# api.py:565-599，摘取关键语句
permission_scopes = [v.strip() for v in x_permission_scopes.split(',') if v.strip()]
model = get_chat_model(temperature=0)
embedder = get_embedding_model()
reranker = get_reranker()
plan = compile_query_plan(payload.query, model)
if not plan.executable: raise HTTPException(422, ...)
candidate_ids = select_candidate_ids(db, plan, tenant_id=x_tenant_id)
if not candidate_ids:
    return {..., 'evidence_packs': [], 'evidence_status': 'no_candidates'}  # 仅开关为 true 时带后两字段
```

* `payload` 由 FastAPI 校验；`x_tenant_id` 和 `x_permission_scopes` 从 HTTP 头取得；`db` 来自 `Depends(get_db)`。缺权限范围 403，模型/Embedding/Rerank 未配置 503，待澄清 422。`temperature=0` 降低随机性，但**不保证相同输出**。
* `compile_query_plan()`（`query_plan.py:258-262`）给 LLM 输出模式 `QueryPlan`；例如查询“在深圳且 2025 上半年担任星河项目总负责人”：硬条件是 `region=深圳`，语义要求 `S1` 是负责人经历。这里尚无事实和 quote。
* `select_candidate_ids()`（`query_plan.py:127-150`）以租户、在职状态、Filter DSL 构造 SQL 取员工编号。可能得到 `['C901','C902']`。**这仅是候选集，并非二人已满足项目要求。**如果为空，证据整理不运行，也不能把该路径解释成抽取失败。

`api.py:601-650` 再按每项 requirement 检索：

```python
def run_hybrid_search(query):
    return hybrid_search_evidence_service(
        query=query,
        filters=EvidenceFilter(tenant_id=x_tenant_id,
            permission_scopes=permission_scopes, candidate_ids=candidate_ids),
        store=store, embedder=embedder, reranker=reranker, ...)

for requirement in plan.semantic_requirements:
    outcome = (search_with_optimization(requirement, search_query=run_hybrid_search, model=model)
               if payload.retrieval_mode == 'auto_optimize'
               else {'strategy': None, 'queries': [requirement.query],
                     'results': run_hybrid_search(requirement.query)})
    result_sets.append(outcome['results'])
    for item in outcome['results']:
        requirement_ids_by_chunk.setdefault(item.chunk_id, []).append(requirement.requirement_id)
    searches.append({'requirement_id': requirement.requirement_id, ...})

chunks = [{'chunk_id': item.chunk_id, 'candidate_id': item.candidate_id,
           'content': item.content, 'score': item.rerank_score,
           'metadata': item.metadata,
           'requirement_ids': requirement_ids_by_chunk[item.chunk_id]}
          for item in merge_query_results(result_sets)]
```

`hybrid_search_evidence_service()`（`hybrid_search_service.py:49-71`）依次调用 embedder、store.hybrid_search、reranker，再取 `limit` 条；`search_with_optimization()`（`query_plan.py:224-247`）首轮不足时追加重写/多查询并去重。这里新增证据层关心的桥梁是 `requirement_ids_by_chunk`：假设 Chunk A 同时命中 S1 和 S2，最终只保留一次 Chunk A，但 `requirement_ids=['S1','S2']`，因此后面两项要求都能拿它作为候选材料。`chunks` 中此时的 `content` 来源于**索引命中**，还没回库重读，不能当最终证据原文。假设本例 S1 命中 C901 的 A、B、C 三个 Chunk，C902 没有命中，那么 `candidate_ids` 仍有两个人，而 `chunks` 只有 C901 的三条。

### C. `include_evidence_pack` 的新分支：调用顺序与数据形状[重点 NEW]

```python
# api.py:652-670；下列语句与当前实现对应
response = {'query_plan': plan, 'candidate_ids': candidate_ids,
            'searches': searches, 'chunks': chunks}
if payload.include_evidence_pack:
    sources = load_pack_sources(db, chunks, tenant_id=x_tenant_id,
                                permission_scopes=permission_scopes)
    source_by_id = {row['chunk_id']: row for row in sources}
    response['chunks'] = [dict(hit, content=source_by_id[hit['chunk_id']]['content'])
                          for hit in chunks if hit['chunk_id'] in source_by_id]
    response['evidence_packs'] = build_evidence_packs(
        candidate_ids=candidate_ids,
        requirements=[r.model_dump() for r in plan.semantic_requirements],
        sources=sources,
        extract=model_extractor(model))
    response['evidence_status'] = ('reviewed' if plan.semantic_requirements
                                   else 'not_requested')
return response
```

**逐句解释**：第一句先生成兼容第 8 课的响应。开关为 false，到此直接返回，不新增证据字段。为 true，先从 `chunks` 的 ID 再读数据库 `sources`；随后把有权限的命中 Chunk 文本替换成数据库版本，无法回库核验的命中从响应的 `chunks` 中过滤掉；然后按候选人和要求生成 pack；最后标注是否存在待处理的语义要求。`reviewed` 只表示已进入整理分支，**不是全部 requirement 的 `status=sufficient`**。`model_extractor(model)` 返回一个供 `build_evidence_packs()` 反复调用的函数，不是在这一行就批量抽取完所有人。

**方法之间的责任分工**：`load_pack_sources` = “先确定允许模型看到什么、引用哪里”；`model_extractor` = “把语言材料转成候选事实”；`build_evidence_packs` = “逐人逐要求验证并组织”；内部 `_validate_fact_sources` = “检验引用原样出现”；`_merge_duplicate_facts` = “重复事实多来源”；`_find_conflicts` = “找同范围反向陈述”；`_evidence_status` = “汇总状态”。外层 `search_talent` 不自己判断事实 yes/no，模型也不负责最终冲突计算。

### D. 方法一：`load_pack_sources` 和 `resolve_citation`，为什么索引命中还不够？

位置：`backend/app/evidence_citations.py:7-70`。输入是 DB session、上一步的 `chunks` 和请求的 tenant/scopes；返回**经验证的来源列表**。核心循环如下：

```python
for hit in chunks:
    try:
        row = resolve_citation(db, UUID(hit['chunk_id']),
                               tenant_id=tenant_id, permission_scopes=permission_scopes)
    except (ValueError, HTTPException):
        continue
    if row['candidate_id'] != hit['candidate_id'] or row['version_state'] != 'current':
        continue
    expanded = [row]
    if row['parent_chunk_id']:
        # 父 Chunk 也 resolve_citation；同候选、同版本、content <= 8000 才加入
        ...
    for entry in expanded:
        existing = sources.setdefault(entry['chunk_id'],
                                      {**entry, 'requirement_ids': []})
        existing['requirement_ids'] = sorted(
            set(existing['requirement_ids'] + hit['requirement_ids']))
return list(sources.values())
```

`resolve_citation` 先通过 UUID 查 `DocumentChunk → DocumentVersion → Document`，检查 `doc.tenant_id`、`doc.status=active`、文档权限、Chunk 权限、`chunk.candidate_id == doc.candidate_id`；如果关联知识库还检查知识库租户、活动状态、权限；读解析任务来补 `parser_version` 等定位信息。返回 `citation_id`（当前实现等于 Chunk UUID）、`document_title`、`document_version_id`、`version_state`、页码/时间戳/标题路径等，**这些字段由数据库填充，不让模型生成**。`load_pack_sources` 再拒绝和检索命中的候选人不一致以及历史版本，必要时带上同版本、同人的父 Chunk。父 Chunk 可以让“只截到职责一半”的片段拥有上下文，但也必须经过权限检查。

**具体例子**：Milvus 命中 A 的 `content='担任总负责人'`，但 PostgreSQL 当前版本实际 Chunk A 已改为 `content='未担任总负责人'`：本分支不会把索引里的肯定句发给抽取器，而是从数据库获取当前有权读取的内容；若 A 对应的是历史版本则整条跳过。`response['chunks']` 也被替换/过滤。这个例子只说明**文本一致性和访问控制**，不说明数据库内容必然真实。由于代码只过滤无效项而不是自动重新检索，部分反证可能仍未被召回。

### E. 方法二：`model_extractor(model)`，模型到底看到了什么、吐出什么？

位置：`backend/app/evidence_pack.py:36-57`。这是**高阶函数/闭包**：先捕获 `model`，返回签名 `extract(requirement, sources)` 的函数；`build_evidence_packs` 对每个有材料的 `(candidate, requirement)` 调一次 `extract`。核心代码：

```python
def model_extractor(model):
    def extract(requirement, sources):
        payload = {'requirement': requirement['query'], 'sources': [
            {'chunk_id': row['chunk_id'], 'content': row['content']}
            for row in sources]}
        return model.with_structured_output(EvidenceExtraction).invoke([
            ('system', EXTRACTION_PROMPT),
            ('user', json.dumps(payload, ensure_ascii=False)),
        ])
    return extract
```

假设 S1 = “2025 年上半年担任星河项目总负责人”，A = “担任星河项目总负责人”，B = “未担任星河项目总负责人”。模型 user payload 只含要求文本及允许的 `chunk_id/content`，**不含授权信息也不含它可自造的 `citation_id`**。系统提示要求统一范围如 `2025-01/2025-06`、`claim='担任项目总负责人'`，明确肯定 yes，明确否定 no；纯背景不产生事实。理想输出示意：

```json
{"facts":[
  {"event":"星河项目","period":"2025-01/2025-06","claim":"担任项目总负责人",
   "answer":"yes","sources":[{"chunk_id":"A","quote":"担任星河项目总负责人"}]},
  {"event":"星河项目","period":"2025-01/2025-06","claim":"担任项目总负责人",
   "answer":"no","sources":[{"chunk_id":"B","quote":"未担任星河项目总负责人"}]}
],"fully_supported":false,"missing_information":["需要核查项目职责记录"]}
```

这只是**示意结构**；真实 `period` 不能只凭片段就补年份，需要原文/上下文明确年份。模型可能误读否定词、改写 quote、合并事实或不稳定排序；Pydantic 负责格式门槛，后续函数负责来源与冲突，但不会自动证明语义蕴含。

### F. 方法三：`build_evidence_packs`，逐人逐要求的“控制器”

位置：`backend/app/evidence_pack.py:117-145`。输入类型可心算为 `candidate_ids: list[str]`、`requirements: list[dict]`、`sources: list[dict]`、`extract: callable(requirement, rows)`；输出为 `list[dict]`，每位候选人一包。对齐核心语句：

```python
packs = []
for candidate_id in dict.fromkeys(candidate_ids):    # 去掉重复候选人，保留输入顺序
    items = []
    for requirement in requirements:
        rows = list({row['chunk_id']: row for row in sources
             if row['candidate_id'] == candidate_id
             and requirement['requirement_id'] in row['requirement_ids']}.values())
        item = {**requirement, 'status': 'missing',
                'reason': 'no_accessible_hits', 'extraction_status': 'not_run',
                'facts': [], 'conflicts': [], 'missing_information': [], 'citations': rows}
        if rows:
            try:
                raw = extract(requirement, rows)
                data = raw if isinstance(raw, EvidenceExtraction) else EvidenceExtraction.model_validate(raw)
                facts = _merge_duplicate_facts(_validate_fact_sources(data.facts, rows))
                conflicts = _find_conflicts(facts)
                status = _evidence_status(facts=facts, conflicts=conflicts,
                    fully_supported=data.fully_supported,
                    missing_information=data.missing_information)
                item.update(status=status,
                    reason='evidence_review' if facts else 'no_relevant_evidence',
                    extraction_status='succeeded', facts=facts, conflicts=conflicts,
                    missing_information=data.missing_information)
            except Exception as exc:
                logger.warning('evidence_extraction_failed type=%s', type(exc).__name__)
                item.update(status='partial', reason='extraction_failed',
                            extraction_status='failed')
        items.append(item)
    packs.append({'schema_version': '2.0',
                  'candidate_id': candidate_id, 'requirements': items})
return packs
```

**看清两个去重层次**：`rows` 用 `chunk_id` 去重是“同一 Chunk 多次召回只抽取一次”；后面的 `_merge_duplicate_facts` 以事实四元组去重是“不同 Chunk 陈述同一件事，事实一条、来源多条”。两者绝不可混同。`citations=rows` 在任何抽取之前已经设置，模型失败也保留经过鉴权的原始材料。`data = ... model_validate(raw)` 容纳真实模型返回对象或测试替身返回 dict。某个 `(人,要求)` 失败会降级该 item，不阻止同请求的其他组继续处理；该 try/except 范围内任何异常均按抽取失败处理，日志只暴露异常类型。

**例子逐步走**：候选集 `[C901,C902]`，要求 `[S1]`，经重验 `sources` 有 C901 的 A/B/C 共三条，C902 零条。C901 的 `rows=[A,B,C]`，进入 LLM，验证后事实 `yes(A,C)` 与 `no(B)`，冲突 `[0,1]`。C902 的 `rows=[]`，**不调用模型**，直接保留 `missing / no_accessible_hits / not_run`，但仍输出 C902 的 pack。这解释为何 SQL 候选人不会因为没有项目证据就在响应里悄悄消失。

### G. 方法四与五：`_validate_fact_sources` 和 `_merge_duplicate_facts`，每行数据如何变化？

来源验证位置 `evidence_pack.py:60-74`：

```python
by_id = {row['chunk_id']: row for row in sources}
for fact in facts:
    refs = []
    for ref in fact.sources:
        row = by_id.get(ref.chunk_id)
        if row is None or not ref.quote.strip() or ref.quote not in row['content']:
            raise ValueError('unverifiable_source')
        mapped = {'citation_id': row['citation_id'],
                  'chunk_id': ref.chunk_id, 'quote': ref.quote}
        if mapped not in refs: refs.append(mapped)
    validated.append({**fact.model_dump(exclude={'sources'}), 'sources': refs})
```

假设模型引用 `chunk_id='A'`、`quote='担任总负责人'`，且数据库 A 的 `content` 含这个**连续原文子串**，后端把该来源扩展为 `{citation_id:数据库A的ID, chunk_id:'A', quote:'担任总负责人'}`。若模型伪造 `chunk_id='Z'`，或把 quote 改写为原文没有的“主导全集团”，则 raise；外层捕获后该组变为 `partial/extraction_failed`，不把其余“看似正确”的事实部分返回。这是一种**全组失败即降级**的保守策略。这里没有检查 quote 的语义是否支持 `answer`，也没有确认引用独立性。

事实合并位置 `evidence_pack.py:77-89`：

```python
key = (fact['event'], fact['period'], fact['claim'], fact['answer'])
if key not in index_by_key:
    index_by_key[key] = len(merged)
    merged.append({**fact, 'sources': []})
for ref in fact['sources']:
    if ref not in merged[index_by_key[key]]['sources']:
        merged[index_by_key[key]]['sources'].append(ref)
```

输入 `[yes(A), no(B), yes(C)]`（三条都指向**同 event/period/claim**）：处理 A 时建 `merged[0]={answer:yes,sources:[A]}`；处理 B 时因为 answer 不同建 `merged[1]={answer:no,sources:[B]}`；处理 C 时命中 yes 的四元键，把 C 添到 `merged[0].sources`。输出只剩 2 个事实，来源 `[A,C]` 与 `[B]`，保留最初出现次序。如果 C 和 A 的 quote 虽同义但 `claim` 字符串不同，当前程序不会合并；模型“尽量规范化措辞”只是提示词，不是确定性别名表。

### H. 方法六与七：`_find_conflicts` 和 `_evidence_status`，怎样从事实走到状态？

`evidence_pack.py:92-114` 的比较和状态逻辑可缩写为：

```python
for i, left in enumerate(facts):
    for j in range(i + 1, len(facts)):
        right = facts[j]
        same_scope = all(left[k] == right[k] and left[k] != 'unknown'
                         for k in ('event', 'period', 'claim'))
        opposite = {left['answer'], right['answer']} == {'yes', 'no'}
        if same_scope and opposite: conflicts.append([i, j])

if conflicts: return 'conflicting'             # 优先级最高
if (any(f['answer'] == 'yes' for f in facts) and fully_supported
        and not missing_information): return 'sufficient'
if facts: return 'partial'
return 'missing'
```

**手算非相邻**：已合并 facts 下标 `[0 星河/2025/总负责人/yes, 1 远航/2025/技术负责人/yes, 2 星河/2025/总负责人/no, 3 远航/2024/技术负责人/no]`。外层 i=0，内层 j=1 不同项目跳过；j=2 三个范围字段一致且 yes/no 相反，记 `[0,2]`；i=1、j=3 虽同远航、技术负责人，却是 2025 vs 2024，跳过。**对所有组合比较**是这道作业专门测的点；只比较邻接项会漏 `[0,2]`。最多 30 个抽取事实，合并后不超过 30，嵌套两层最多比较 435 对，逻辑简单且可审计。

状态不是根据“yes 比 no 多”算的。`conflicts` 非空立即 `conflicting`；否则必须存在 yes、模型报告 fully_supported 为 true、并且缺失信息为空，才 `sufficient`；只有 no 的有效事实是 `partial`，不会武断地写作“候选人没有经历”。完全没有事实时 `missing`，但仍需借 `reason`/`extraction_status` 区分“没命中”和“命中却不相关”。这里的确定性只涵盖**给定模型字段之后的比较规则**；`fully_supported` 仍来自模型，故最终 `sufficient` 不能理解成已独立核实。

#### H.1 统一的“逐帧输入”：先把数据固定，后面每个方法都用它

下面是**教学用的同一组数据**，不是对外接口直接接收的 JSON，也不是真实员工档案。为读懂 `load_pack_sources()` 之前的 UUID 转换，这里先用合法 UUID 表示检索命中；在后续事实处理的快照中将它们分别简写为 A/B/C。三条文本都包含完整的主体、日期、项目和肯定/否定词：

```python
A = '00000000-0000-0000-0000-0000000000a1'
B = '00000000-0000-0000-0000-0000000000b2'
C = '00000000-0000-0000-0000-0000000000c3'
texts = {
    A: 'C901 在 2025 年 1 月至 6 月担任星河项目总负责人。',
    B: 'C901 在 2025 年 1 月至 6 月未担任星河项目总负责人。',
    C: '复盘确认：C901 在 2025 年 1 月至 6 月担任星河项目总负责人。',
}
# 一条 S1 要求；另有候选人 C902，但没有命中它的 Chunk。
requirements = [{'requirement_id': 'S1',
                 'query': '2025年上半年担任星河项目总负责人', 'required': True}]
candidate_ids = ['C901', 'C902']
```

假设数据库里 A/B/C 都归 C901、属于同一租户、权限允许且为当前版本，亦无父 Chunk。`search_talent` 中的 `chunks`（检索阶段的输出）是：

```python
[
  {'chunk_id': A, 'candidate_id': 'C901', 'content': texts[A],
   'score': 0.90, 'metadata': {}, 'requirement_ids': ['S1']},
  {'chunk_id': B, 'candidate_id': 'C901', 'content': texts[B],
   'score': 0.80, 'metadata': {}, 'requirement_ids': ['S1']},
  {'chunk_id': C, 'candidate_id': 'C901', 'content': texts[C],
   'score': 0.70, 'metadata': {}, 'requirement_ids': ['S1']},
]
```

其中 `score`、`metadata` 是检索字段；后面 `load_pack_sources` 会把**真实数据库**信息与文本读出来，不沿用这里的分数来判断事实。以下写 `A` 时指上面的完整 UUID 字符串，不指字母字符。`citation_id` 在当前实现中取同一个数据库 Chunk ID，但**角色不同**：`chunk_id` 用于匹配模型回指，`citation_id` 是服务端最终输出的引用定位符。

#### H.2 第一帧：`resolve_citation()` / `load_pack_sources()` 把检索命中变成授权来源

**调用入口**：`api.py:658-660`；实现 `evidence_citations.py:7-70`。实际入参不仅是一个 ID：

```python
sources = load_pack_sources(
    db,                       # SQLAlchemy Session；能按 UUID 查 PostgreSQL 模型
    chunks,                   # 上面 list[dict]，还只是索引的检索命中
    tenant_id='course-demo',  # 来自请求上下文
    permission_scopes=['hr_private'],
)
```

方法内部创建 `sources = {}`。第一轮 `hit=chunks[0]`：

1. `UUID(hit['chunk_id'])` 把字符串 A 变 UUID；`resolve_citation(db, UUID(A), ...)` 用 `db.get(DocumentChunk, A)` 读 Chunk，再通过 `document_version_id` 查版本、文档；检查文档与 Chunk 的候选人归属、租户、活动状态及权限，有知识库时还检查知识库。出错/无权限/UUID 不合法，`load_pack_sources` **跳过该 hit**。
2. 假设通过，`resolve_citation` **返回 dict**，主要是：

   ```python
   row = {'chunk_id': A, 'citation_id': A, 'candidate_id': 'C901',
          'document_id': '...', 'document_title': '复盘材料',
          'document_version_id': '...', 'version_no': 1,
          'version_state': 'current', 'permission_scope': 'hr_private',
          'content': texts[A], 'parent_chunk_id': None,
          'heading_path': ['工作分工'], 'page_start': None, ...}
   ```

   这里只展示形状；`...` 为演示省略的 DB 元数据，不是可以传给程序的实际值。`resolve_citation` **本身不做 current 过滤**，它返回 `version_state`；`load_pack_sources` 才用 `row['version_state'] != 'current'` 拦历史版本，并核对 `row['candidate_id'] == hit['candidate_id']`。
3. `expanded=[row]`。若 `parent_chunk_id` 存在，会**再次**调 `resolve_citation`，只有父 Chunk 同候选、同版本、长度 `<=8000` 才加入 `expanded`。本例无父 Chunk。
4. `setdefault(A, {**row,'requirement_ids':[]})` 给 `sources` 增加 A；再把检索命中的 `['S1']` 加进去并 `sorted(set(...))`。此时局部字典为 `{A: {**rowA, 'requirement_ids':['S1']}}`。第二、三轮分别加入 B、C。若相同 Chunk 再次被 S2 检索命中，键仍是 A，只会变成 `requirement_ids=['S1','S2']`，不会生成另一条来源。
5. `return list(sources.values())`，输出类型是 `list[dict]`，本例为 `[sourceA, sourceB, sourceC]`。每个 `sourceX` 的字段来自**数据库**并额外加上 `requirement_ids`，不是模型构造的。

**注意两种筛除产生的变化**：B 若属于旧版本或其他候选，则 `sources=[sourceA,sourceC]`，后续模型完全看不到 B，冲突也可能检测不到。这个方法解决“索引可能陈旧/无权限”，却不能保证反向材料都被召回；更不能证明 DB 文本的陈述是真的。

#### H.3 第二帧：`model_extractor(model)` 的入参与返回值究竟是哪一层对象

`model_extractor(model)` 是工厂函数：**入参为 LLM 对象，返回一个 `extract(requirement, sources)` 可调用对象**，不是立即返回事实集合。后面 `build_evidence_packs()` 才调用：

```python
extract = model_extractor(model)
raw = extract(requirements[0], [sourceA, sourceB, sourceC])
```

内部（`evidence_pack.py:49-57`）把来源压缩为模型可见的 `payload`，即：

```python
{
  'requirement': '2025年上半年担任星河项目总负责人',
  'sources': [
    {'chunk_id': A, 'content': texts[A]},
    {'chunk_id': B, 'content': texts[B]},
    {'chunk_id': C, 'content': texts[C]},
  ],
}
```

注意 `sourceA` 中的 `citation_id`、文档版本、权限、候选人编号等元数据**没有作为结构化字段传给模型**；模型只能报告输入中的 ID 和逐字引文。`with_structured_output(EvidenceExtraction).invoke([('system', EXTRACTION_PROMPT), ('user', json.dumps(payload, ensure_ascii=False))])` 把 `payload` 序列化为 user 消息，期待返回 `EvidenceExtraction`。下面是为了走读而固定的一次**可能输出**，不是保证模型每次都会这样回答：

```python
raw.facts = [
  ExtractedFact(event='星河项目', period='2025-01/2025-06',
      claim='担任项目总负责人', answer='yes',
      sources=[FactSource(chunk_id=A, quote=texts[A])]),
  ExtractedFact(event='星河项目', period='2025-01/2025-06',
      claim='担任项目总负责人', answer='no',
      sources=[FactSource(chunk_id=B, quote=texts[B])]),
  ExtractedFact(event='星河项目', period='2025-01/2025-06',
      claim='担任项目总负责人', answer='yes',
      sources=[FactSource(chunk_id=C, quote=texts[C])]),
]
raw.fully_supported = False
raw.missing_information = ['需要核查职责冲突']
```

这里的 `raw.facts` 是 **`list[ExtractedFact]`，每条 `sources` 是 `list[FactSource]`**；其中尚**没有** `citation_id`。如果模型把 A 和 C 先合在一条事实的两个来源里，后面仍能验证，但不能再声称本次“模型输出三条事实”。入参 `requirement` 是按 `SemanticRequirement.model_dump()` 得到的字典；其 `query` 是模型抽取的目标，`required` 在这里原样带到包中，但**目前没有单独参与 `_evidence_status` 计算**。

#### H.4 第三帧：`_validate_fact_sources(facts, sources)` 每轮处理了什么？

此方法是**模型对象 → 后端字典**的关键边界。输入的两个参数分别是：

```python
facts   = raw.facts                         # list[ExtractedFact]：上面三条
sources = [sourceA, sourceB, sourceC]       # list[dict]：从数据库重读并鉴权
```

程序首先建立 `by_id = {row['chunk_id']: row for row in sources}`，本例是 `{A:sourceA, B:sourceB, C:sourceC}`，查找一次引用不必每次全表扫描。接着对每个 `fact` 的每个 `ref` 执行：

```python
row = by_id.get(ref.chunk_id)
if row is None or not ref.quote.strip() or ref.quote not in row['content']:
    raise ValueError('unverifiable_source')
mapped = {'citation_id': row['citation_id'],
          'chunk_id': ref.chunk_id, 'quote': ref.quote}
if mapped not in refs:
    refs.append(mapped)
validated.append({**fact.model_dump(exclude={'sources'}), 'sources': refs})
```

逐轮变量快照（这里 `quoteA` 就是 `texts[A]`，B/C 同理）：

| 当前事实 | `ref`（模型给的） | `by_id.get()` | 通过检查后的 `mapped` | 本轮完成的 `validated` 长度 |
|---|---|---|---|---:|
| 0 yes | `{chunk_id:A, quote:quoteA}` | `sourceA` | `{citation_id:A, chunk_id:A, quote:quoteA}` | 1 |
| 1 no | `{chunk_id:B, quote:quoteB}` | `sourceB` | `{citation_id:B, chunk_id:B, quote:quoteB}` | 2 |
| 2 yes | `{chunk_id:C, quote:quoteC}` | `sourceC` | `{citation_id:C, chunk_id:C, quote:quoteC}` | 3 |

具体说，处理事实 0 时 `refs=[] → [mappedA]`，`fact.model_dump(exclude={'sources'})` 把 Pydantic 对象转成 `{event,period,claim,answer}` 的 Python dict，再添加服务端生成的 `sources=[mappedA]`。事实 1、2 重复同样步骤。**输出集合是 `list[dict]`，仍为三条，不做事实去重：**

```python
validated = [
  {'event':'星河项目','period':'2025-01/2025-06','claim':'担任项目总负责人',
   'answer':'yes','sources':[{'citation_id':A,'chunk_id':A,'quote':texts[A]}]},
  {'event':'星河项目','period':'2025-01/2025-06','claim':'担任项目总负责人',
   'answer':'no','sources':[{'citation_id':B,'chunk_id':B,'quote':texts[B]}]},
  {'event':'星河项目','period':'2025-01/2025-06','claim':'担任项目总负责人',
   'answer':'yes','sources':[{'citation_id':C,'chunk_id':C,'quote':texts[C]}]},
]
```

若一条事实把完全相同的引用重复两次，`mapped not in refs` 会在**该事实内部**只保留一次；不同事实的相同陈述仍留待 `_merge_duplicate_facts`。若 `quote='C901 主导集团项目'` 不在对应 `row['content']`，或 ID 为组外 Chunk，就 raise；`build_evidence_packs` 的外层 try 捕获后**整个 C901/S1 组**变 `partial/extraction_failed`、`facts=[]`、`citations` 仍在，不会返回前两条已经局部校验过的事实。这里验的是字符串回指，不验语义支持或材料真伪。

#### H.5 第四帧：`_merge_duplicate_facts(facts)` 输入三条，输出两条，字典如何变化？

**入参不是原始 `ExtractedFact` 对象**，而是 H.4 的 `validated: list[dict]`；每个 `dict` 已有 `{event,period,claim,answer,sources:[{citation_id,chunk_id,quote}]}`。返回类型同样是 `list[dict]`。实际内部是 `merged=[]; index_by_key={}`；下面跟踪这两份可变状态：

| 循环轮次 | 当前 `fact` | `key=(event,period,claim,answer)` | `index_by_key` 变化 | `merged` 变化 |
|---:|---|---|---|---|
| 0 | `validated[0]`，yes/A | `(星河项目,2025-01/2025-06,担任项目总负责人,yes)` | 新键 → 0 | 先造 `{...,answer:yes,sources:[]}`，再追加 `mappedA`，现为 `[yes{A}]` |
| 1 | `validated[1]`，no/B | `(星河项目,2025-01/2025-06,担任项目总负责人,no)` | 新键 → 1 | 新建第二项，再追加 `mappedB`，现为 `[yes{A},no{B}]` |
| 2 | `validated[2]`，yes/C | 与第 0 轮**完全相同** | 不增新键；取索引 0 | 不建第三项；把 `mappedC` 追加 `merged[0].sources`，现为 `[yes{A,C},no{B}]` |

对应实现（`evidence_pack.py:77-89`）：

```python
key = (fact['event'], fact['period'], fact['claim'], fact['answer'])
if key not in index_by_key:
    index_by_key[key] = len(merged)          # 首次出现的位置
    merged.append({**fact, 'sources': []}) # 复制其余字段，来源容器从空开始
index = index_by_key[key]
for ref in fact['sources']:
    if ref not in merged[index]['sources']:
        merged[index]['sources'].append(ref)
return merged
```

因此输出精确形状为：

```python
merged = [
  {'event':'星河项目','period':'2025-01/2025-06','claim':'担任项目总负责人',
   'answer':'yes','sources':[{'citation_id':A,'chunk_id':A,'quote':texts[A]},
                             {'citation_id':C,'chunk_id':C,'quote':texts[C]}]},
  {'event':'星河项目','period':'2025-01/2025-06','claim':'担任项目总负责人',
   'answer':'no','sources':[{'citation_id':B,'chunk_id':B,'quote':texts[B]}]},
]
```

为什么 key **包含** `answer`？若不包含，正反两条会被错误合并成一个事实，冲突证据就消失；若把 `chunk_id` 放进 key，相同事实跨两个 Chunk 又永远不能合并。`if ref not in ...` 对完整引用 dict 判相等；A 和 C 的 `chunk_id` 不同，所以都保留，即使它们说的是同一事实。方法仅做**精确字符串**键比较，不会认为“星河”自动等于“星河项目”，也不会认为 `2025` 等于 `2025-01/2025-12`。输入顺序决定合并后索引，不能把这些索引当稳定的数据库 ID。

#### H.6 第五帧：`_find_conflicts(facts)` 输入两条，返回的是哪种集合？

入参就是上一帧的 `merged: list[dict]`，**不是原始三条**。每条至少要有 `event/period/claim/answer`；该方法不再读取 `sources` 的 quote，只比较事实范围与 yes/no。它先 `conflicts=[]`，再用两层循环遍历 `i<j`：

```python
same_scope = all(left[k] == right[k] and left[k] != 'unknown'
                 for k in ('event', 'period', 'claim'))
opposite_answers = {left['answer'], right['answer']} == {'yes', 'no'}
if same_scope and opposite_answers:
    conflicts.append([left_index, right_index])
```

本例 `left=merged[0]=yes{A,C}`，`right=merged[1]=no{B}`，三项范围完全相同，两个答案的集合正好是 `{'yes','no'}`，故 `conflicts=[] → [[0,1]]`。返回值是 `list[list[int]]`，不是 quote 列表，也不是 `{'A','B'}` 这种 Chunk 对。数字 **0/1 指向合并后 `facts` 的数组下标**；读取冲突时应先看 `facts[0]` 和 `facts[1]`，再通过各自 `sources` 找 A/C 与 B。若远航 2024/2025 混在这个组里，只要 event 或 period 有一项不等，就跳过。`unknown` 不与 `unknown` 构成冲突，避免未知范围被错误判作相同范围。

五 Chunk 作业时合并顺序是 `[星河 yes, 远航 yes, 星河 no, 远航 2024 no]`，因此同一逻辑给 `[[0,2]]`；算法没有特殊“非相邻”分支，只是遍历了所有 `i<j`。

#### H.7 第六帧：`_evidence_status(...)` 输入是什么？输出为什么是 `conflicting`？

此方法采用**仅关键字参数**，调用点 `evidence_pack.py:134-135`：

```python
status = _evidence_status(
    facts=merged,                   # list[dict]，本例两条事实
    conflicts=[[0, 1]],           # list[list[int]]，由上一方法返回
    fully_supported=False,         # bool，来自模型 EvidenceExtraction
    missing_information=['需要核查职责冲突'], # list[str]，来自模型
)
```

执行顺序是 `if conflicts` → `if any(yes) and fully_supported and not missing_information` → `if facts` → `missing`。当前 `conflicts` 非空，**第一句就返回字符串 `'conflicting'`**；后面的 yes、覆盖程度和缺失清单不会再影响本组状态。若同样的 facts 无冲突且 `fully_supported=True`、`missing_information=[]`，则返回 `'sufficient'`；有事实但未满足充分条件返回 `'partial'`；没有事实则 `'missing'`。它只返回**一个状态字符串**，不是整个 requirement dict。`reason` 与 `extraction_status` 在调用它的 `build_evidence_packs()` 中设置。

#### H.8 最后一帧：`build_evidence_packs(...)` 怎样把这些输出装回二维集合？

最外层输入是四个**关键字参数**（`evidence_pack.py:117`）：

```python
build_evidence_packs(
    candidate_ids=['C901','C902'],
    requirements=[{'requirement_id':'S1','query':'...', 'required':True}],
    sources=[sourceA,sourceB,sourceC],
    extract=model_extractor(model),  # 函数：接收 requirement/rows，返回 EvidenceExtraction
)
```

**分组阶段**：外层 `for candidate_id in dict.fromkeys(candidate_ids)`，每位候选人建一个 `items=[]`；内层 `for requirement in requirements`，按 `(candidate_id, requirement_id)` 从 `sources` 过滤 `rows`，并用 `{row['chunk_id']:row ...}.values()` 去除重复 Chunk。此时 C901/S1 的 `rows=[sourceA,sourceB,sourceC]`；C902/S1 的 `rows=[]`。这个循环形成“每人一个 pack、每项要求一个 item”的二维结构，而不是把所有人混在一个事实池里比较。

**初态阶段**：

```python
item = {**requirement,
        'status':'missing', 'reason':'no_accessible_hits',
        'extraction_status':'not_run', 'facts':[], 'conflicts':[],
        'missing_information':[], 'citations':rows}
```

`citations` 是**这一组所有被准入的原始来源 list[dict]**，区别于 `facts[i]['sources']` 的**每一事实实际引用的来源 list[dict]**；即使一个 Chunk 被判不相关，它仍可能留在 `citations` 供审阅。C902/S1 由于 `rows=[]`，不进 `if rows`，保留初态。C901/S1 进入 `try`：`raw=extract(...)` → `EvidenceExtraction.model_validate(raw)`（若 raw 已经是该模型对象则直接用）→ H.4 的三条已校验字典 → H.5 的两条合并事实 → H.6 的 `[[0,1]]` → H.7 的 `'conflicting'`，然后 `item.update(...)`。

**结束阶段的精简输出**：

```python
[
  {'schema_version':'2.0', 'candidate_id':'C901',
   'requirements':[{'requirement_id':'S1', 'query':'...', 'required':True,
     'status':'conflicting', 'reason':'evidence_review',
     'extraction_status':'succeeded',
     'facts':[merged[0],merged[1]], 'conflicts':[[0,1]],
     'missing_information':['需要核查职责冲突'],
     'citations':[sourceA,sourceB,sourceC]}]},
  {'schema_version':'2.0', 'candidate_id':'C902',
   'requirements':[{'requirement_id':'S1', 'query':'...', 'required':True,
     'status':'missing', 'reason':'no_accessible_hits',
     'extraction_status':'not_run', 'facts':[], 'conflicts':[],
     'missing_information':[], 'citations':[]}]},
]
```

其中 `'query':'...'` 是为了缩短排版，实际是原 `requirement['query']`，`sourceA` 等是前文已鉴权 dict，并非 JSON 可直接提交的变量名。最后 `api.py:665-669` 把这个 list 赋值给 `response['evidence_packs']`；`response['evidence_status']='reviewed'` 是顶层分支标记。**异常分支**：只要 C901/S1 的模型调用、Pydantic 校验、来源校验或其余 try 内步骤抛异常，该 item 保留初始 `citations`，改为 `status='partial' / reason='extraction_failed' / extraction_status='failed'`，`facts=[] / conflicts=[]`；C902 不受影响。这个分组隔离和安全降级是老师最希望你能亲手推演出的设计思想。

#### H.9 自己动手跟一遍，不再只背术语

照着本节的 A/B/C：在纸上画四列 `当前入参`、`局部状态`、`返回值`、`上一层保存在哪里`。先写 `_validate_fact_sources` 的 `by_id` 和三次 `refs/validated`；再写 `_merge_duplicate_facts` 的 `index_by_key`/`merged` 三轮；最后写 `_find_conflicts` 的索引对和 `build_evidence_packs` 的两个 candidate item。改变 B 的 `quote` 为不存在的字符串，确认**不是**“只去掉 B 然后变 sufficient”，而是 C901/S1 **整组失败降级**；改变 C 的 `claim` 为“负责项目排期”，确认它不再合并到 A，也不能和 B 构成同一命题冲突。然后再做下文五 Chunk 作业。

### I. 对照真实隔离响应，一次追踪到最后（不是五 Chunk 作业的结果）

`backend/scripts/verify_evidence_pack.py` 的固定演示从 `backend/samples/lesson09/` 的三份合成材料造数据库 Chunk、用固定模型与检索替身调用真实路由。已存的 `backend/artifacts/lesson09-fixture-model.json` 可直接看真实返回，不必猜想：

```text
candidate_ids = [C901, C902]
S1 = 在 2025 年 1 月至 6 月担任星河项目总负责人
C901: citations = 3；validated/merged facts = 2
  facts[0] = (星河项目, 2025-01/2025-06, 担任项目总负责人, yes), sources=2
  facts[1] = (星河项目, 2025-01/2025-06, 担任项目总负责人, no),  sources=1
  conflicts = [[0,1]]
  extraction_status=succeeded; reason=evidence_review; status=conflicting
  missing_information=[需要核查项目职责记录]
C902: citations = 0; facts=[]; conflicts=[]
  extraction_status=not_run; reason=no_accessible_hits; status=missing
```

调用栈可手写成：

```text
FastAPI request
  search_talent(payload, headers, db)
    ├─ compile_query_plan → QueryPlan / S1
    ├─ select_candidate_ids → [C901,C902]
    ├─ run_hybrid_search(S1.query) → 三个命中 Chunk
    ├─ load_pack_sources → resolve_citation(每个 ID) → 有权限、当前版本 sources
    └─ build_evidence_packs(..., extract=model_extractor(model))
         ├─ C901/S1: extract → EvidenceExtraction
         │    → model_validate → _validate_fact_sources → _merge_duplicate_facts
         │    → _find_conflicts → _evidence_status → conflicting
         └─ C902/S1: rows=[] → missing（不调模型）
    → response['evidence_packs'] / response['evidence_status']
```

想在调试器里重现：先看 `api.py:596` 的 `candidate_ids`，再看 `api.py:641-650` 的 `chunks` 与 `requirement_ids_by_chunk`，之后看 `api.py:659` 的 `sources`（是否旧版本、缺权限、父 Chunk 扩展），最后看 `evidence_pack.py:122` 的 `rows`、`:131` 的 `data`、`:132` 的 `facts`、`:133` 的 `conflicts`、`:134-138` 的状态。**不要打印真实员工原文到非授权日志**；本例只使用虚构材料。

### J. 老师真正考什么？通过“问题—代码选择—剩余限制”来记忆

| 现象/问题 | 代码里的具体设计 | 你必须解释的因果与仍存在的边界 |
|---|---|---|
| Chunk A 说 yes，B 说 no，C 重复 yes | 先 `_merge_duplicate_facts`，再 `_find_conflicts` | 合并 A/C 的**来源**而非 yes/no；B 保留，冲突待核查，不投票。 |
| 同一句可能是“另一个人/旧材料/无权材料”的 | `resolve_citation`、`load_pack_sources` 重新鉴权、核对候选与当前版本 | 检索库不是来源授权真相；回库能守住一致性，但不能证明内容真实。 |
| 模型编造引用或改变原话 | Pydantic + `quote in row['content']`，失败整组降级 | 机械回指拦截伪造出处；合法原句也可能被**误解语义**。 |
| 不同项目/不同年有相反回答 | 冲突范围键 `event/period/claim` 必须逐字段相等 | 避免错误冲突；字符串别名、交叠时间还需额外规范化/复核。 |
| 有材料但模型宕机或格式错 | `except` → `partial/extraction_failed`，保留 citations | 失败不等于“候选人没有经历”；下游可复核原文。 |
| 没有命中的候选仍进入评估输入 | `for candidate_id in dict.fromkeys(candidate_ids)` 总为每人建包 | 保留 SQL 候选人与信息缺口，不拿“没有命中”证明“不具备能力”。 |

**三句背诵版**：① LLM 善于从自然语言里提出结构化陈述，但不能当唯一裁判；② 程序应该控制来源、范围、去重、冲突和失败降级，让每一步可回放；③ 证据包的“可信”是相对原始 Chunk 的**可追溯与边界明确**，不是对现实真实性、模型语义判断、检索召回率的绝对保证。带着这三句和上面的变量追踪，再去完成后面的作业一、作业二。

---

## 三、作业一：拓展思考——按题写出“失效原因 → 验证 → 优化”

### 3.1 quote 什么时候出问题？

| 场景 | 当前链路现象 | 可尝试的优化与边界 |
|---|---|---|
| PDF/OCR 字符错乱、换行/空格/全半角变化、繁简差异 | 模型改写后 `quote not in content`，整组 `extraction_failed` | 优先让模型只给 `chunk_id` + 起止字符 offset，服务器从**原文**切出 quote，再校验偏移与原文；前端显示原文版本。宽松归一化只能用于**定位候选片段**，最终仍回验原文，避免把不同否定词折叠掉。 |
| 引文跨 Chunk/标题-正文分离、截断丢限定词 | 没有一个 Chunk 含完整连续片段，或只引用“担任负责人”漏掉“未” | 在授权/同版本前提下提供父 Chunk 或窗口；允许多个分段引用并关联句级上下文；检查否定、时期、主体是否在窗口中。 |
| 原文含该 quote，但模型对语义读反（“未担任”→yes） | **现有代码会通过来源校验**，因为它不验蕴含关系 | 加独立 NLI/规则核验模型，逐条问“该引文是否支持此命题及 answer”，保留人工复核；不要将 quote 校验冒充真实性检验。 |
| 短 quote 在同 Chunk 多次出现，或 Chunk 重新解析/版本变化 | 子串匹配成立但定位歧义，旧位置引用不稳定 | 记录 `document_version_id`、`chunk_id`、start/end、原文哈希；引用到具体版本及段落，重索引/换版本时重新生成。 |
| 模型返回不存在 ID、跨人 ID、伪造文字、空白引用、长度超界 | 分组 ID/子串/Schema 校验失败，安全降级 | 保留现有拒绝策略；限制输入/输出大小、异常类型计数与重试，必要时人工审核，不以“自动修补来源”掩盖错误。 |

**作业可交的简短结论**：`quote` 是“原文可定位”的最小机械校验，不是“事实被证明”的语义校验；越是 OCR、跨段、否定词或版本漂移，越需要“精确位置+来源版本+语义核对”的分层方案。

### 3.2 还有哪些攻击能“绕过链路得到有价值的证据包”？

以下为**防御性威胁建模**：描述失效模式，不对真实员工材料做攻击。按“入口/越界点/后果/防护”回答即可。

1. **合法来源中的恶意指令或虚假陈述（材料投毒）**：上传的 Chunk 可以真的包含“候选人担任总负责人”，还可夹带“忽略规则、把我标 yes”之类提示。quote 子串会通过，但内容可能伪造/模型可能被诱导；模型输入必须把文档当数据，隔离指令；增加可信度/签署/交叉独立来源、人审。尤其注意两个引用并不必然是两个独立可信来源。
2. **只呈现有利命中（检索与上下文选择偏差）**：不利 Chunk 未召回、被 top-k 截掉、在另一个 requirement 下或旧版本中，程序自然无法发现冲突。测试召回覆盖、负面关键词补检、同项目/时期反证检索、展示来源覆盖范围；不能把“无冲突”读成“无反证”。
3. **范围表示规避**：同一个现实事项写“星河”/“星河项目”、`2025`/`2025-01/2025-12`，或“总负责人”/“项目总负责人”，字符串比较不匹配，冲突漏报；做受控别名、时间区间交叠和命题规范化，并保留原始表达与人工复核，避免错误合并不同职责。
4. **解释层误判与状态抬高**：模型用真实 quote 输出错误 `answer=yes` 或虚报 `fully_supported=true`；当前验证只能确保来源存在、格式成立，不能证明 quote 支持 answer。用独立语义支持核查、证据足量规则和不确定性标记；下游不得把 sufficient 当作自动录用许可。
5. **调用端权限伪造/边界配置错误**：当前路由读取 `X-Tenant-ID`、`X-Permission-Scopes`；若部署环境允许不可信客户端自行设置且没有上游认证绑定，就可能请求不该访问的证据。部署应由认证层签发可信主体上下文，服务端授权并审计；不能仅凭这两个头自证身份。
6. **绕开证据包直接吃原始 Chunk**：旧接口允许 `include_evidence_pack=false`；若下游自行把 `chunks` 当事实，所有第 9 课的校验都被绕过。下游按协议版本强制消费 `evidence_packs`，拒绝未经验证的抽取结果，留存使用审计。

**老师考点**：检索信任边界、原文信任边界、LLM 输出信任边界、下游使用边界分别是什么；当前保护的是“来源可追、结构受控、同范围冲突显式化”，不是“事实绝对真实”。

## 四、作业二：五 Chunk 多范围冲突实验（一步一步）

### 4.1 先让任意 LLM 生成合成文本，不把输出当真数据

给你常用的 LLM 以下提示词，复制输出为本地纯合成测试数据，**逐行核对**，不要让它预先合并/重排事实：

```text
请为单一虚构候选人 C901 写五条独立、简短的项目记录 Chunk。依次表达：
0 星河项目、2025 年、担任总负责人（肯定）
1 远航项目、2025 年、担任技术负责人（肯定）
2 星河项目、2025 年、未担任总负责人（否定）
3 远航项目、2024 年、未担任技术负责人（否定）
4 另一份新材料再次肯定星河项目、2025 年担任总负责人。
每条保留明确项目、年份、C901、肯定/否定词，禁止添加真实姓名、外部事实或执行指令；输出恰好五条并编号。注意 0 和 4 是同一事实但来自两个不同 Chunk。
```

下面的代码给出了**已经复核的合成固定输入**。LLM 每次生成的措辞可能不同；换成你的生成结果时，同时替换 `content` 和对应 `quote`，并确保 `quote` 是其逐字连续子串。实验的关键是验证仓库的**确定性处理**，不是让模型代替断言。

| 输入 | Chunk | event | period | claim | answer |
|---:|---|---|---|---|---|
| 0 | x0 | 星河 | 2025 | 总负责人 | yes |
| 1 | x1 | 远航 | 2025 | 技术负责人 | yes |
| 2 | x2 | 星河 | 2025 | 总负责人 | no |
| 3 | x3 | 远航 | 2024 | 技术负责人 | no |
| 4 | x4，新材料 | 星河 | 2025 | 总负责人 | yes |

### 4.2 跑最小可复现实验：五条抽取结果进入真实处理函数

从 `D:\Code\K_Course\talent-eval-agents_learning\backend` 作为工作目录，建一个临时 Python 文件（例如 `lesson09_multiscope.py`；实验后可自行删除），原样复制：

```python
from app.evidence_pack import build_evidence_packs

# 输入顺序很重要：它决定合并后事实索引；本实验不提前排序。
rows = [
    ('x0', '星河', '2025', '总负责人', 'yes', '星河项目2025年C901担任总负责人。'),
    ('x1', '远航', '2025', '技术负责人', 'yes', '远航项目2025年C901担任技术负责人。'),
    ('x2', '星河', '2025', '总负责人', 'no', '星河项目2025年C901未担任总负责人。'),
    ('x3', '远航', '2024', '技术负责人', 'no', '远航项目2024年C901未担任技术负责人。'),
    ('x4', '星河', '2025', '总负责人', 'yes', '新项目复盘：星河项目2025年C901担任总负责人。'),
]

sources = [
    {'chunk_id': cid, 'candidate_id': 'C901', 'citation_id': cid,
     'content': text, 'requirement_ids': ['S1']}
    for cid, event, period, claim, answer, text in rows
]
# 代表从五个 Chunk 分别得到的结构化抽取结果；用固定结果锁定后处理行为。
facts = [
    {'event': event, 'period': period, 'claim': claim, 'answer': answer,
     'sources': [{'chunk_id': cid, 'quote': text}]}
    for cid, event, period, claim, answer, text in rows
]
pack = build_evidence_packs(
    candidate_ids=['C901'],
    requirements=[{'requirement_id': 'S1', 'query': '核对项目负责人记录'}],
    sources=sources,
    extract=lambda requirement, input_chunks: {
        'facts': facts, 'fully_supported': False, 'missing_information': []
    },
)[0]['requirements'][0]

for i, f in enumerate(pack['facts']):
    print(i, f['event'], f['period'], f['claim'], f['answer'],
          [src['chunk_id'] for src in f['sources']])
print('conflicts=', pack['conflicts'], 'status=', pack['status'])
assert pack['extraction_status'] == 'succeeded'
assert len(pack['facts']) == 4
assert [s['chunk_id'] for s in pack['facts'][0]['sources']] == ['x0', 'x4']
assert pack['conflicts'] == [[0, 2]]
assert pack['status'] == 'conflicting'
```

运行：

```powershell
cd D:\Code\K_Course\talent-eval-agents_learning\backend
uv run --no-sync python .\lesson09_multiscope.py
```

本机当前 `.venv` 指向已不存在的 Python 路径；如你遇到相同报错，先修复/重建项目虚拟环境，或使用**与 Python 3.12 相容且装有项目依赖**的解释器。本次验证用运行时 Python 3.12 + 本项目 `.venv\Lib\site-packages` 进行了实际执行。`quote` 若被改写会导致 `extraction_failed`，不是“发现另一组冲突”。

### 4.3 逐步手算为什么是四条、且只有一对非相邻冲突

```text
原始索引 0: (星河,2025,总负责人,yes) → 合并索引 0，来源 [x0]
原始索引 1: (远航,2025,技术负责人,yes) → 合并索引 1，来源 [x1]
原始索引 2: (星河,2025,总负责人,no)  → 合并索引 2，来源 [x2]
原始索引 3: (远航,2024,技术负责人,no) → 合并索引 3，来源 [x3]
原始索引 4: (星河,2025,总负责人,yes) → 合并到索引 0，来源 [x0,x4]
```

去重键含 `answer`，因此 0 与 2 **不合并**；冲突键不含 `answer`，因此 0 与 2 同一范围且答案相反，记录 `[[0,2]]`。远航记录 1 与 3 的 `period` 不同，不是冲突。状态计算优先检查冲突，所以哪怕部分事实为 yes，最终仍是 `conflicting`。不要用原始五行的索引去解释返回 `conflicts`：它引用的是**合并后 facts 的索引**。

### 4.4 LLM 实际参与时为何还要固定后处理实验

用该项目配置的模型对这五条合成 Chunk 做了一次结构化抽取尝试：模型**直接将 x0 与 x4 合成一个事实**，并改变其输出顺序，得到四条事实、一个冲突、最终 `conflicting`；由于输出顺序变化，那次冲突索引是 `[[0,1]]`，**不能伪称模型本身稳定输出了预期的 `[[0,2]]`**。因此作业建议交两份观察：① 模型原始结构化输出（核对项目/时期/否定及引用），② 用五条按指定顺序的结构化事实运行上述断言，单独证明去重与全量比较同时工作。模型可能已预合并、重排或规范化年份；若要测每条抽取，可逐 Chunk 调模型并按输入顺序组装，再交给真实后处理函数，最后断言，不要假设一次调用总会输出五条。

### 4.5 补充反例与回归测试

- 将 x3 的 `period` 从 `2024` 改为 `2025`：远航两条范围相同、答案相反，预期新增 `[1,3]` 冲突（在原有 0/2 冲突之外）。
- 将 x2 的 `quote` 改成不存在的文字，或 `chunk_id` 改成其他人的 ID：预期 `partial / extraction_failed`、`facts=[]`，但保留原始 `citations`；不应继续给出未经核验的冲突。
- 把 x4 的 `answer` 改为 no：它不再并入事实 0，而并入事实 2；总数仍四条，但来源归属变化。
- 把 `period` 写成 `2025 年`：Schema 不接受，进入失败降级。正式方案需先规范化；不要让冲突检测自行猜测。

已有自动测试：`D:\Code\K_Course\talent-eval-agents_learning\backend\tests\test_evidence_pack.py` 覆盖重复 Chunk、来源合并、伪造来源、同/异时期冲突、错误时间格式、超时降级；`D:\Code\K_Course\talent-eval-agents_learning\backend\tests\test_talent_search_api.py` 覆盖 HTTP 主链。验证命令：

```powershell
cd D:\Code\K_Course\talent-eval-agents_learning\backend
uv run --no-sync python -m pytest tests/test_evidence_pack.py tests/test_talent_search_api.py -q
uv run --no-sync python -m scripts.verify_evidence_pack
# 有已配置模型且只处理虚构材料时才运行：
uv run --no-sync python -m scripts.verify_evidence_pack --live-model
```

本次实测前两个测试文件共 **13 passed**（本机采用可用 Python 3.12 解释器加载该项目虚拟环境依赖）；固定五条的处理断言也通过：四条事实，事实 0 来源 `[x0,x4]`，冲突 `[[0,2]]`，状态 `conflicting`。上面的现有演示脚本不是五条测试，不应混淆。

## 五、学习检查清单：能脱离笔记说清楚才算掌握

- [ ] 能画出请求入口、SQL 候选约束、每项要求检索、回库校验、事实抽取和返回之间的数据流。
- [ ] 能区分 `candidate_ids=[]`、`no_accessible_hits`、`no_relevant_evidence`、`extraction_failed`、`conflicting` 的业务含义。
- [ ] 能解释 `quote in content` 只验字符串，为什么不足以判定 `answer` 正确或证据真实。
- [ ] 能口算“去重键四元组、冲突范围三元组”，并说明非相邻全量比较与不同年份不冲突。
- [ ] 能说明冲突优先于充分、模型超时不等于没材料、模型原始抽取输出顺序不等于题设输入顺序。
- [ ] 能提出至少三个真实边界：提示注入/材料投毒、召回遗漏、别名与时间范围、权限头信任、下游绕过。

**最终一句话**：这一课不是“让模型给候选人打分”，而是把不确定的检索内容变成**按人按要求组织、可追溯、可降级、显式标冲突**的中间证据协议，再诚实说明协议仍无法替代来源真实性和人工核查。
## 六、逐个 `def` 读懂第 9 课的测试：输入、断言和通过标准

> 阅读约定：下文的“通过”是**对应断言没有抛错**，不表示模型语义判断一定正确，也不表示所有安全威胁都被覆盖。代码位置均以本章编写时的工作区为准。两份 pytest 文件是自动化断言；`verify_evidence_pack.py` 是从 HTTP 入口走一遍的演示/有限断言脚本。当前实测前两份共 **14 个 `test_` 函数、20 个参数化后测试用例，20 passed**；固定演示在 UTF-8 模式下退出码为 0。以下逐个解释 `def`，包括只负责构造输入的辅助函数。

### 6.1 怎样看一次 pytest 的“合格”

单条测试可按四步读：① **Arrange** 改造输入（例如把引用 ID 改为不存在）；② **Act** 调用 `build` / `resolve` / HTTP；③ **Assert** 比对返回的精确字段，或要求抛出指定异常；④ 思考**没断言什么**。有多个 `assert` 时必须全部满足才通过。`pytest.mark.parametrize` 会把一个函数展开为多次独立执行；只通过其中一次不算整组通过。辅助函数的 `def` 本身并不是 pytest 自动发现的用例。

### 6.2 `test_evidence_pack.py`：整理层的单元测试

文件：`D:\Code\K_Course\talent-eval-agents_learning\backend\tests\test_evidence_pack.py`。先锁定公共输入：`source()` 返回候选人 C001、要求 S1 的一个来源字典：`chunk_id='a'`、`citation_id='a'`、`content='负责星河项目。'`、版本 `v1`；`extraction()` 返回模型**模拟结果**：一条 `(event='星河项目', period='2025', claim='担任项目负责人', answer='yes')`，来源 `a`，连续原文引用 `负责星河项目`，`fully_supported=True`，缺口 `[]`。并没有真实 LLM 调用。

**辅助 `def module()`（6–9 行）**：`find_spec` 检查模块能否被找到，再导入 `app.evidence_pack` 返回模块。模块不存在时其断言失败；单独调用它不会检验整理算法。

**辅助 `def source(cid='a', candidate='C001')`（12–14 行）**：构造字典，供测试变换 Chunk ID 与候选人。它只造内存输入，不从数据库查证。`source('b')` 内容仍是 `负责星河项目。`，如测试需要其他内容，则用 `dict(source('b'), content='...')` 覆写。

**辅助 `def extraction()`（17–21 行）**：制造一份符合形状的提取结果。注意 `facts` 是“模拟模型抽取出的结构化事实”，`sources` 里是模型声称引用的 `chunk_id + quote`，与 `source()` 的原文是两份独立数据；后续验证要比较两者。

**辅助 `def build(data=None, sources=None, candidates=None)`（24–29 行）**：给 `build_evidence_packs` 固定要求 `[{'requirement_id':'S1','query':'负责过星河项目'}]`，用 `extract=lambda requirement, rows: ...` 注入模拟提取；`data=None` 时采用 `extraction()`，`sources=None` 时采用 `[source()]`。返回形状是 `packs=[{'schema_version', 'candidate_id', 'requirements': [item]}]`，多数测试取 `[0]['requirements'][0]`。小细节：`candidates or ['C001']` 意味着传空列表也会变为 C001；它不是对空候选列表的测试。

1. **`test_duplicate_chunk_is_not_counted_twice_and_missing_candidate_is_preserved()`（32–37 行）**：给 `sources=[a,a]`、候选人 `[C001,C002]`；调用 `build`。断言 C001 包版本 `2.0`、其 S1 `citations` 长度为 **1**（重复 Chunk 不重复计数）、状态为 `sufficient`，同时 C002 仍有包且其 S1 为 `missing`。四条都成立才通过。它未断言两包内部每个字段的完整内容，也未检验数据库去重。

2. **`test_same_fact_merges_sources_without_dropping_provenance()`（40–47 行）**：把原 yes 事实深拷贝一份，仅将第二份的来源改为 `b`；提供来源 `[a,b]`。调用后要求 `facts` **1 条**而该事实 `sources` **2 条**。这同时检查四元组相同的事实被合并、来源不丢失；未精确断言两条来源的排序或引用文本。

3. **`test_unverifiable_model_output_falls_back_to_raw_citations(bad)`（50–59 行）**：`@parametrize` 生成 **2 次**：`unknown_id` 把模型声称的来源 ID 改为 `other-person`（不在本次来源集合）；`fabricated_quote` 保持 ID `a`，但把 quote 改为不在 `content` 里的 `主持千人团队`。任一分支都要求 `extraction_status='failed'`、`facts=[]`、原始 `citations` 仍为 1、整体 `status='partial'`。两个分支均通过才算这组通过；说明失败降级**保留可核验的原始材料，却不认可伪造事实**。这里只检查文本连续包含与 ID 对应，不证明引用的语义或材料真实性。

4. **`test_yes_and_no_answers_in_same_scope_form_conflict()`（62–72 行）**：保留 `a` 上的 yes；增添同一 `(星河项目, 2025, 担任项目负责人)` 的 no，引用 `b` 的 `仅参与星河项目`，并提供该真实来源。断言 `status='conflicting'`、`reason='evidence_review'`、`conflicts=[[0,1]]`。三个精确值都满足才通过；冲突索引指**整理后** `facts[0]` 和 `facts[1]`。并没有判断哪一方“真”。

5. **`test_yes_and_no_answers_in_different_periods_are_not_a_conflict()`（75–85 行）**：no 的 `period` 改为 `2024`，yes 仍是 `2025`，并把 `fully_supported=False`。断言 `conflicts=[]`、`status='partial'`；表示跨时期的相反答案不算**同范围**冲突，且当前材料不足以称 `sufficient`。本例没有覆盖不同项目、不同命题或时间范围部分重叠的情形。

6. **`test_noncanonical_period_falls_back_instead_of_entering_conflict_check()`（88–93 行）**：把 `period` 改成 `2025 年 1 月至 2025 年 6 月`，不符合 Schema 要求的 `YYYY` / `YYYY-MM/YYYY-MM` / `unknown`。断言 `status='partial'`、`extraction_status='failed'`；成功条件是非法抽取走失败降级，不让该事实参加冲突判定。未断言失败后 `facts` 与 `citations` 的具体长度。

7. **`test_model_failure_is_not_material_absence()`（96–104 行）**：内部 `def fail(*args)` 直接抛 `TimeoutError('private details')`，并作为 `extract` 注入；它是**故障桩**，不是被 pytest 单独执行的测试。结果断言 `status='partial'`、`extraction_status='failed'`，且整个返回字符串不包含 `private details`。全部满足代表模型超时不被误写成“确无材料”，异常详情也未暴露；并未测试日志本身是否脱敏。

8. **`test_irrelevant_evidence_is_distinguished_from_no_hits()`（107–112 行）**：仍使用默认非空 `[source()]`，但模拟提取 `facts=[]`、`fully_supported=False`、`missing_information=['没有项目职责信息']`。断言 `status='missing'`、`reason='no_relevant_evidence'`。这与根本没有命中 Chunk 的 `no_accessible_hits` 不同：**有材料，但无相关事实**。未单独断言 `extraction_status`，也未测试空来源分支的具体 `reason`。

9. **`test_blank_missing_information_is_rejected()`（114–119 行）**：把有效 `extraction()` 的缺口改成 `['']`；虽然有一条 yes 事实，Schema 对缺口字符串规定 `min_length=1`。断言 `status='partial'` 与 `extraction_status='failed'`；通过即表示无效结构化输出不被当成成功抽取。要表达无缺口应使用 `[]`，不是 `['']`。此测试不检查错误消息。

### 6.3 `test_evidence_citations.py`：回库权限与引用接口

文件：`D:\Code\K_Course\talent-eval-agents_learning\backend\tests\test_evidence_citations.py`。这组是**内存数据库替身 + 一次真实 FastAPI TestClient**，不是连接实际 PostgreSQL 的集成测试。

**辅助类 `MemoryRows` 的 `def __init__()`、`def add(cls, **kwargs)`、`def get(cls, key)`（9–17 行）**：初始化 `rows={}`；`add` 为假记录加随机 UUID、以 `(类, id)` 为键存入；`get` 按同样键取出，找不到返回 `None`。它用来模拟 `db.get`。三个方法不是独立测试，也不验证真实 ORM/SQL 权限。

**辅助 `def fixture()`（20–33 行）**：造出完整引用链 `KnowledgeBase → Document → DocumentVersion → DocumentChunk`，以及 `ParseJob/ChunkingRun`；租户 `t1`、候选人 `C001`、作用域 `hr_private`、原文 `负责星河项目。`、Markdown 起点 10。返回 `(db,kb,doc,version,chunk)`，每次调用重新造一套，可直接突变对象模拟权限变化。

**辅助 `def resolve(db, chunk, tenant='t1', scopes=None)`（36–40 行）**：先检查模块存在，再把 `chunk.id` 与租户、权限传入 `resolve_citation`；`scopes=None` 使用默认 `['hr_private']`，而显式传入 `scopes=[]` 会保留**空权限**，两者不一样。

1. **`test_citation_binds_original_version_and_markdown_offsets()`（43–50 行）**：将 `version.is_current=False`，调用 `resolve`；断言 `version_state='historical'`、`document_version_id==str(version.id)`、`markdown_start==10`、`content==chunk.content`。说明单独解析引用仍能定位授权的历史版本及原文偏移，不会偷偷替换成当前版本；**检索构包**时历史版本由 `load_pack_sources` 过滤（见第 5 条），不要误读为“所有接口都拒绝历史引用”。未验证 `markdown_end`、页面/时间戳等字段。

2. **`test_citation_rechecks_current_access_without_leaking_existence(change)`（53–65 行）**：参数化 **6 次**，每次先建全新 fixture，再依次仅改一个因素：文档租户变 `other`、文档权限变 `restricted`、知识库权限变 `restricted`、文档状态变 `deleted`、从内存库删除文档、Chunk 候选人变 `C002`。每种都要求 `resolve` 抛 `HTTPException`，并精确断言 `status_code==404`、`detail=='引用不可用或无访问权限'`。六次全通过意味着越权/失效与不存在都统一呈现为不可用，不通过错误详情透露资源是否存在；未覆盖所有组合与真实数据库并发更新。

3. **`test_empty_scopes_cannot_open_citation()`（68–71 行）**：显式 `resolve(..., scopes=[])`，要求抛出 `HTTPException`。成功条件仅是**拒绝访问**；本测试没要求特定 404 或文案。若需要固定错误协议，应另加断言。

4. **`test_citation_endpoint_rechecks_access()`（74–88 行）**：创建应用，用 `get_db` 依赖覆盖为内存对象，HTTP 请求带 `X-Tenant-ID:t1` 与 `X-Permission-Scopes:hr_private`：第一次 GET `/api/evidence/citations/{chunk.id}` 应 `200`；把 `doc.permission_scope` 改为 `restricted` 后再次 GET 同一 URL 应 `404`，且响应文本不包含原文 `chunk.content`。三个断言都满足才通过，展示引用**访问时再次鉴权**，而非第一次获准后永久可见。测试替换了 DB，并未实际认证请求头来源是否可信。

5. **`test_pack_sources_reload_text_and_skip_stale_or_revoked_hits()`（91–102 行）**：索引命中 `hit.content='不可信索引文本'`，而数据库 Chunk 内容是 `负责星河项目。`。第一次 `load_pack_sources` 必须得到数据库原文；标记版本不再 current 后必须返回 `[]`；恢复 current 但撤销文档权限后也必须返回 `[]`。三段断言证明**索引文本不可直接采信、旧版本或已撤权命中不可入包**。本测试只比较第一个来源的 `content`，没有遍历所有元数据或父 Chunk 扩展逻辑。

### 6.4 `verify_evidence_pack.py`：`def` 逐个读，不把演示输出当作 pytest

文件：`D:\Code\K_Course\talent-eval-agents_learning\backend\scripts\verify_evidence_pack.py`。运行 `python -m scripts.verify_evidence_pack` 才执行底部 `if __name__ == '__main__': main()`；pytest 不会从这个文件自动收集前述测试。其目的更像**隔离环境下的接口冒烟和结果观察**。

**`def make_demo(live_model=False)`（27–94 行）**：创建内存 SQLite 及数据库会话；造深圳的 C901/C902，并从 `samples/lesson09/review.md`、`report.md`、`interview.md` 建立 C901 的三条文档-版本-Chunk 链；UUID 用 `uuid5` 固定，使引用可复现。默认 `live_model=False` 不调用真实 LLM：内部 `DemoModel.with_structured_output(schema)`（59–82 行）对 `QueryPlan` 返回一条深圳过滤 + S1 要求，对 `EvidenceExtraction` 返回两条事实——yes 引用前两块、no 引用第三块，并报告缺口。`live_model=True` 时仅抽取走配置模型，`VisibleSyntheticExtraction.invoke(messages)`（68–71 行）调用它并打印结构化输出；未配置密钥会在准备阶段抛错。`DemoStore.hybrid_search(*args, **kwargs)`（85–87 行）固定返回三条命中；将模型、向量、重排器、存储等依赖打补丁后，返回 `(app, db, chunks, patches, engine)`，注意补丁**此时尚未启动**。这些内部 `def` 只是演示桩及包装器，不是自动断言：若要判断造数是否合格，应检查返回值、三个样本文件和随后主流程。

**`def main()`（97–132 行）**：解析两个选项，先 `make_demo`、启动补丁。默认模式用 `TestClient` 携租户/权限头向 `/api/talent-search` POST `{'query': QUERY, 'include_evidence_pack': True, 'limit': 8}`；`raise_for_status()` 拒绝非 2xx；把**完整 HTTP 响应**写至 `backend/artifacts/lesson09-fixture-model.json`；控制台打印候选人、Chunk 数、每个要求的状态/抽取状态/事实数/引用数。只有默认模式还断言 `extraction_states` 不含 `failed`（并排除了 `not_run`）；最后 `finally` 停止补丁、关闭会话和引擎。若实际输出 `C901: conflicting/succeeded, facts=2, citations=3`、`C902: missing/not_run, facts=0, citations=0`，则演示与当前造数预期吻合；**但脚本本身没有 assert 这几个具体数字或 `conflicts`**，只看退出码 0 不足以证明它们。完整包的冲突索引、来源数还要打开保存的 JSON 检查。

**模式区别与通过条件**：`--serve` 分支启动 `127.0.0.1:18089` 服务供人工请求，在 `return` 后走 `finally`，它**没有运行自动 POST/断言**；`--live-model` 仍只发送合成材料，但模型输出可能变动，默认模式的“不得 failed”断言**不会执行**，出现 `model_result='safe_fallback'` 也可能正常退出，须人工对照 JSON 和控制台 `synthetic_model_output`。`model_result` 是按抽取状态计算的显示标签，不是独立质量评分。这个三 Chunk 演示与作业二的**五 Chunk、四事实、非相邻 `[0,2]`**是不同实验，不能用演示退出码替代五条事实断言。

### 6.5 自己运行与判断（Windows）

```powershell
cd D:\Code\K_Course\talent-eval-agents_learning\backend
$env:PYTHONPATH=(Resolve-Path '.\.venv\Lib\site-packages').Path
$env:PYTHONDONTWRITEBYTECODE='1'
& 'C:\Users\Kalav\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest -p no:cacheprovider tests/test_evidence_pack.py tests/test_evidence_citations.py -q
# 本机这次是 20 passed, 3 warnings。warnings 是依赖库弃用提示，不等于测试失败。
$env:PYTHONUTF8='1'
& 'C:\Users\Kalav\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m scripts.verify_evidence_pack
# 观察打印摘要；再检查 .\artifacts\lesson09-fixture-model.json 内的 evidence_packs、facts、sources、conflicts。
```

这里的 `PYTHONPATH`/解释器绝对路径是**本机可运行方式**，你的机器也可用项目自己的 `uv run --no-sync python -m pytest ...`。本机脚本读取中文样本使用 `Path.read_text()` 而未指定 encoding，系统默认 GBK 时曾抛 `UnicodeDecodeError`；设置 `PYTHONUTF8=1` 后重跑退出 0。这是**文件解码环境问题**，不是事实冲突算法失败；更稳健的长期改法是样本读取显式 `encoding='utf-8'`，此处只记录现象，不改业务/测试代码。

**验收边界一句话**：20 个 pytest 用例证明被断言的去重、来源校验、降级、冲突、权限等行为在这些输入下符合预期；三 Chunk 演示证明一条隔离 HTTP 路径能返回包；五 Chunk 作业仍需单独断言 `4` 条、`facts[0].sources` 两个来源、`conflicts=[[0,2]]` 与最终 `conflicting`。

## 七、本次分支注释审阅：哪里已理解、哪里需要改口

> 审阅快照：2026-09-25，分支 `lesson-9_note`，HEAD `bc72761` 加未提交工作区修改。本节不是重新讲一遍所有方法，而是**对你当前新增的中文注释逐条校对**，留给你下一次复习时先看。标记：✅ 当前理解基本准确；⚠️ 表达容易让以后误解；❗建议提交前改注释或改提交方式。原代码位置：`D:\Code\K_Course\talent-eval-agents_learning\backend\app\evidence_pack.py`、`backend\app\api.py`、`backend\tests\lesson09_multiscope.py`。

### 7.1 先给结论：能不能提交？

**可作为“学习阶段快照”有条件提交，但现在不建议原样一把 `git add -A && git commit`。**三个第 9 课相关测试文件本次 **23 passed**；五 Chunk 确定性实验通过（四事实、来源 `[x0,x4]`、冲突 `[[0,2]]`、`conflicting`），说明新增注释并未打断已测的主链。不过：① 有两处注释把状态的归因写错；② 五 Chunk 新文件暂存的还是空文件；③ 全量测试没有全绿；④ `.idea/`、`logs/` 等不相干未跟踪文件很多；⑤ 本目录**在 Git 仓库外**，此处笔记不会随分支提交。若你的“提交”意味着生产就绪或全量 CI 通过，应先处理全量测试环境，不能拿 23 个相关测试代替全量。

### 7.2 逐处核对代码注释

| 位置/你写的意思 | 判定 | 当前代码实际上怎样、建议怎样记 |
|---|---|---|
| `api.py:658–662`：开启证据包、Milvus 命中回 PG 取 Chunk 和父 Chunk | ✅ 有边界 | `load_pack_sources` 还会按租户/权限/候选人/当前版本**筛掉**无效命中；父 Chunk 是满足条件才扩展，并非每个命中都必带父块。`response['chunks']` 中只有可回库的原命中，父块可进入 pack sources，但不一定出现在 `chunks`。建议注释补“回库重验权限与版本”。 |
| `evidence_pack.py:22–26`：`event/period/claim/answer/sources` 分工 | ✅ 有边界 | 这五项的概念你抓住了；但“缺失时会合并错误”是**为什么设计字段**，并非当前 Schema 在缺字段时会继续错误合并——必填字段缺失会校验失败。`period` 允许 `unknown`，格式正则不懂日历和时间交集；`claim` 同义表达未归一时仍是不同字符串。 |
| `evidence_pack.py:31–33`：`fully_supported` 和 `missing_information` | ✅ 但别过信模型 | `fully_supported` 是**模型给的布尔判断**，不是校验器独立证明的充分性；缺口 `['']` 无效，`[]` 才是无缺口。状态判断中冲突优先，因此即使有缺口、存在冲突仍先返回 `conflicting`。 |
| `evidence_pack.py:49–58`：`model_extractor` 的入参与结果 | ✅，术语可改 | 闭包 `extract(requirement,sources)` 的 sources 是**回库并按候选人/要求筛出的 rows**，模型只看到 `chunk_id/content`，不会直接看到整个 PG row；`with_structured_output` 限制输出形状，不意味着 LLM 对事实和来源必然正确。 |
| `evidence_pack.py:64–77`：`data.facts` 是 LLM 提取，rows 是可信来源，验证引用 | ✅，但只证明一小件事 | 验证器确认 `ref.chunk_id` 属于本次 rows、quote 非空并为 row.content **原样连续子串**，映射 DB 给出的 `citation_id`。并不验证 quote **支持** yes/no、不验证材料真伪，也不保证没有漏引。测试时 rows 是内存伪造而非 PG；生产链路由 `load_pack_sources` 回库。第 72 行末尾的孤立 `#` 没有意义，建议删掉。 |
| `evidence_pack.py:85–94`：四元组作合并 key，不放 Chunk | ✅，加精确条件 | `(event,period,claim,answer)` **逐字符串精确相等**才合并；来源列表按完整映射字典去重。用多个 Chunk 的同事实合并是目的，但它不会自动识别“星河”与“星河项目”别名。 |
| `evidence_pack.py:104–108, 141–142`：双层循环查同范围相反答案 | ✅，别遗漏 `unknown` | 必须三个范围字段逐个相等且都不为字符串 `unknown`，答案集合恰为 `{yes,no}`。索引是**合并后 facts** 下标；循环不局限相邻项。不同时间串、重叠但不完全一致的范围，当前不会判冲突。 |
| `evidence_pack.py:112–120`：状态四级 | ❗需要纠正 | `conflicting` 的确优先；`sufficient` 需至少一个 yes + 模型 `fully_supported=True` + 无缺口且无冲突。但第 119 行 `partial` 注释里的“或者事实抽取失败”**不是这个函数的路径**：失败由 `build_evidence_packs` 的 `except` 分支设 `partial/extraction_failed/failed`。第 120 行 `missing` 不一定“完全没有事实材料”：也可能 `rows`/citations 非空，但模型抽出的相关 facts 为空。 |
| `evidence_pack.py:128–140`：每个要求筛 rows，“先合并事实” | ⚠️ 顺序要重写 | 外层按人、按要求筛来源，默认去重 Chunk；实际表达式 `_merge_duplicate_facts(_validate_fact_sources(data.facts, rows))` 的**执行次序是先校验再合并**。如果以后复习只读第 139 行的“先合并事实”，容易忘记安全门槛。 |
| `evidence_pack.py:149–155`：`item.update` 和降级 | ⚠️ 补全概念 | `item.update` 是把初始 `missing/no_accessible_hits/not_run` 字段覆盖为成功抽取的 `status/reason/facts/conflicts` 等；`except Exception` 只把抽取状态标 failed、整体 partial，保留初始化时的 `citations=rows`。失败可能来自模型、结构校验、来源校验**乃至代码自身异常**，不是模型失败的同义词。 |
| `tests/lesson09_multiscope.py:3–40`：输入顺序、模拟事实、断言 | ✅ 但不是 pytest/LLM | 桩事实准确地验证四事实与非相邻 `[0,2]` 冲突；其 `rows` 是内存自建，`extract=lambda ...` 不会调用真实模型。它文件名不是 `test_*.py`，也没有 `def test_...`，`pytest` 不会自动运行。想证明“任意 LLM 生成五条 Chunk”还需保留单独的实际模型生成过程与输出。 |

**建议直接替换的三条注释（只提供文字，本轮没有擅自改你的源文件）：**

```python
# _evidence_status 的第三分支：
return 'partial'  # 已成功抽取到相关事实，但不满足“充分”且无同范围冲突；抽取失败由外层 except 处理
# _evidence_status 的第四分支：
return 'missing'  # 成功抽取后 facts 为空；可能没有获准 Chunk，也可能有 Chunk 但无相关事实
# build_evidence_packs 的嵌套调用前：
# 先校验模型引用是否对应本次可用 Chunk 的连续原文，再合并同四元组事实并汇集来源
facts = _merge_duplicate_facts(_validate_fact_sources(data.facts, rows))
```

`api.py:660` 的注释也可以精确写为“对索引命中回 PG 重验租户、权限、候选人和当前版本，重新读取可信原文；符合条件时扩展父 Chunk”。建议将 `if ref.quote ...:` 后的空 `#` 删除。

### 7.3 你目前没写在注释里、复习时最应补的五个边界

1. **并非客观验真**：`quote in content` 仅验证字面引用；否定句原文被模型说成 yes 仍可能通过。数据库也可能存入有误/恶意材料。
2. **状态是多字段组合**：`missing/no_accessible_hits/not_run`、`missing/no_relevant_evidence/succeeded`、`partial/extraction_failed/failed` 不能混为一谈。搜索没召回、权限过滤后无来源，也不能推断候选人没有经历。
3. **精确匹配的脆弱性**：合并和冲突都靠模型输出的同一命名和时间字面值；`unknown` 会抑制冲突，别名、重叠区间、年月格式不同也可能漏判。正则保证形状，不保证日历/逻辑真值。
4. **安全范围**：权限头是否可信要看 API 前面的身份鉴权层；文档正文可带提示注入；模型看到的仅是当前获准来源，但仍要考虑发往外部模型的隐私与审计。构包完成不代表下游评估不能绕过证据包直接消费原始 `chunks`。
5. **测试层级**：`test_evidence_pack.py` 是整理算法，`test_evidence_citations.py` 是内存 DB 与一次 HTTP 权限重验，`test_talent_search_api.py` 是 HTTP 拼装，`verify_evidence_pack.py` 是三 Chunk 隔离演示，`lesson09_multiscope.py` 是五 Chunk 固定后处理实验。它们各自的通过不能互相替代；特别是 pytest 不会自动收集最后一个文件。

### 7.4 此刻的证据和提交前操作清单

- [x] 第 9 课相关 pytest：`test_evidence_pack.py + test_evidence_citations.py + test_talent_search_api.py` → **23 passed，3 个依赖弃用警告**。
- [x] 五 Chunk 固定输入：从 `backend` 运行 `python -m tests.lesson09_multiscope`，得到四事实、`[x0,x4]` 与 `[[0,2]] / conflicting`。
- [ ] 全量 pytest **未全绿**：本次 `104 passed, 2 skipped, 11 failed, 2 errors`。失败集中在 `sample-data/generated` 未提供和 pytest 临时目录权限错误，暂不可宣称全库测试通过；若团队要求全量通过，先补夹具或环境，重新跑完。
- [ ] `git status` 显示 `AM backend/tests/lesson09_multiscope.py`：暂存文件为空，40 行在工作区；先决定是否把这个脚本作为学习附件提交。若要提交，重新将**该文件最新内容**加入暂存区并检查 `git diff --cached --stat` 和 `git diff --cached -- <路径>`。
- [ ] 不要 `git add -A`；明确挑选 `backend/app/api.py`、`backend/app/evidence_pack.py`、选定的实验脚本。`.idea/`、`logs/`、临时 probe、`docs/` 等未跟踪文件应逐一辨认再决定。`backend/artifacts/lesson09-fixture-model.json` 在 status 显示 M，但本次 `git diff` 无内容差异；先检查差异，别把生成的随机 ID 混入学习注释提交。
- [ ] 修正上面两处状态注释、嵌套调用的顺序注释及空 `#`；再运行 `git diff --check` 和相关测试。
- [ ] **重要：这个作业文档和新课堂笔记位于 `D:\Code\K_Course\talent-eval-agents_docs\homework_docs`，在当前 Git 工作树外。** 如果希望“回到这次提交就能找到笔记”，需另行复制进仓库并纳入提交，或在提交说明中写出稳定的外部存储位置；不能只依赖这两个外部文件。

**复习自测**：给一条 `no` 事实、无冲突、`fully_supported=True`，返回哪种状态？→ `partial`，因为 sufficient 要至少一个 yes。给 `facts=[]` 但 `citations` 有一个来源？→ `missing/no_relevant_evidence/succeeded`，不是 `no_accessible_hits`。让模型引用正确原文但颠倒肯定/否定？→ 来源校验可能通过，仍需语义核查。