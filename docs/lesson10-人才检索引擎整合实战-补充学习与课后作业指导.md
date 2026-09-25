# 第 10 课：人才检索引擎整合实战——第 9 课补充篇

> 对照代码：当前工作区中的第 9 课基础，以及本地 Git 对象 `07531abd6da62c6168726e2ec8e357bc390b3ed1`（“证据补充高亮”）。下一提交是**阅读依据**，不是说你当前未提交的工作区已切换到它。本文只讲第 10 课增量；事实抽取、去重、冲突算法的详细推演参阅同目录第 9 课两份笔记。

## 1. 这节课到底补了什么

第 8 课解决“自然语言要求如何计划、筛人、检索”，第 9 课解决“命中的 Chunk 如何变成可追溯事实”。第 10 课解决的是**把结果交给真实使用者时，能不能安全地打开、定位并重新核查原文**。

```text
用户输入
  └─ POST /api/talent-search（X-Tenant-ID / X-Permission-Scopes）
       ├─ QueryPlan → 结构化候选人 ID → 每项语义要求的混合检索
       ├─ 检索命中（索引中的 ID/文本/元数据；不直接信任）
       ├─ PostgreSQL 重读 Chunk、版本、文档、知识库并复核权限
       ├─ LLM 抽取事实 + 程序验证 quote/合并/冲突/状态
       └─ CandidateEvidencePack → 前端候选人/要求/事实/冲突/缺失信息
                                             └─ 点击某条来源
                                                GET /api/evidence/citations/{chunk_id}
                                                → 当下重新鉴权 → 原文/版本/坐标
                                                → 抽屉高亮 quote；失败不清空其他 pack
```

**两个时间点、两次不同的授权**：构包时过滤索引命中；点击引用时重新判断当前状态。第一次有权看到材料，不意味着以后还能打开。事实来源中的 `quote` 只是可定位的原文片段，不等于事实命题已被独立核实。

## 2. 从请求入口读后端：先看框架，再沿真实对象往下走

代码入口：`backend/app/api.py` 的 `TalentSearchInput`、`search_talent`；具体权限实现：`backend/app/evidence_citations.py`；构包：`backend/app/evidence_pack.py`。

### 2.1 请求与响应边界

```http
POST /api/talent-search
X-Tenant-ID: t1
X-Permission-Scopes: hr_private
Content-Type: application/json

{"query":"2025年上半年担任星河项目总负责人","include_evidence_pack":true,"retrieval_mode":"standard","limit":8}
```

`query` 必填；`include_evidence_pack` 默认为 `false`，前端明确传 `true`；`retrieval_mode` 还可为 `auto_optimize`；`limit` 默认为 10（1～100），另外可选 `ef`、`rrf_k`、`rerank_top_n`。租户和权限范围来自请求头，不由 LLM 推断。没有非空 scope → 403；缺模型、Embedding、Rerank → 503；计划不可执行 → 422 `clarification_required`；没有候选人 → 空 `candidate_ids/searches/chunks/evidence_packs`，状态 `no_candidates`。

响应主干（值是**结构示意**，并非某次真实模型输出）：

```json
{
  "query_plan": {"...": "计划对象"},
  "candidate_ids": ["C901"],
  "searches": [{"requirement_id": "R1", "strategy": null, "queries": ["星河项目总负责人"], "chunk_count": 2}],
  "chunks": [{"chunk_id": "UUID", "candidate_id": "C901", "content": "从 PG 复核的内容", "requirement_ids": ["R1"], "score": 0.8, "metadata": {}}],
  "evidence_packs": [{
    "schema_version": "2.0", "candidate_id": "C901",
    "requirements": [{
      "requirement_id": "R1", "query": "2025年上半年担任星河项目总负责人",
      "status": "partial", "reason": "evidence_review", "extraction_status": "succeeded",
      "facts": [{"event": "星河项目", "period": "2025-01/2025-06", "claim": "担任总负责人", "answer": "yes",
                 "sources": [{"citation_id": "UUID", "chunk_id": "UUID", "quote": "担任星河项目总负责人", "quote_start": 0, "quote_end": 10}]}],
      "conflicts": [], "missing_information": [], "citations": [{"chunk_id": "UUID", "content": "从 PG 复核的内容", "document_title": "项目复盘"}]
    }]
  }], "evidence_status": "reviewed"
}
```

上面的数字和字段示意不能作为固定测试预期；尤其 `partial/sufficient` 由事实、`fully_supported` 与缺失情况决定。顶层 `evidence_status='reviewed'` 说明**已走证据整理分支**，不表示每项事实充分。`requirements[].status` 才是各要求的 `missing/partial/sufficient/conflicting`。

### 2.2 `search_talent` 的实际调用顺序与信任边界

| 步骤 | 真实调用/片段 | 输入 → 输出 | 为什么重要 |
|---|---|---|---|
| 1 | `permission_scopes = ...split(',')`；`get_chat_model/get_embedding_model/get_reranker` | 请求头和模型配置 → 可用组件；缺少时立即失败 | 不让 LLM 决定权限。 |
| 2 | `compile_query_plan(payload.query, model)` → `select_candidate_ids(db, plan, tenant_id=...)` | 自然语言 → 可执行计划 → 当前租户的候选人 ID | 先限定人，再查证据；无候选人直接返回。 |
| 3 | 遍历 `plan.semantic_requirements`，`hybrid_search_evidence_service(query=..., filters=EvidenceFilter(tenant_id, permission_scopes, candidate_ids), ...)`；可选 `search_with_optimization` | 每项要求 → 排序命中 `results` | 索引检索只是召回；优化模式可能扩展查询，不等于扩权。 |
| 4 | `requirement_ids_by_chunk` + `merge_query_results(result_sets)` | 多次命中 → 同一 Chunk 关联多个 requirement ID 的 `chunks` | 确保后续材料仍能按要求分组。 |
| 5 | `load_pack_sources(db, chunks, tenant_id, permission_scopes)` | 检索命中的 ID → 从 PG 重读的受控行 `sources` | 不拿索引文本作最终可信证据；丢弃不可访问、候选人不符、非当前版本的命中；可附加同版本、同候选人且受控的父 Chunk。 |
| 6 | `source_by_id` 覆写 `response['chunks']`，仅保留在 `sources` 中的 hit，正文改为 PG 正文 | 不可信的 `chunks` → 可返回的 `chunks` | 避免与已验证 pack 同时返回陈旧/越权索引原文。**该替换位于 `include_evidence_pack=true` 分支；不要误说所有模式均如此。** |
| 7 | `build_evidence_packs(candidate_ids, requirements, sources, extract=model_extractor(model))` | 当前可访问原文 → 每人每要求的 pack | `model_extractor` 向 LLM 传 `{requirement, sources:[{chunk_id,content}]}`；程序验证来源、去重、计算冲突和状态；模型只提供待验证抽取，不决定最终权限。 |

`build_evidence_packs` 的最小骨架：每个 `candidate_id` × 每个 `requirement` 选择对应 `rows`，有证据时运行 `extract` → `EvidenceExtraction` → `_validate_fact_sources` → `_merge_duplicate_facts` → `_find_conflicts` → `_evidence_status`。`rows` 为空保持 `missing/no_accessible_hits`；抽取异常保持 `partial/extraction_failed`，不把异常中的模型输入或原文抛给客户端。本课需认出这条链路即可，第 9 课已有函数内部详解。

### 2.3 第 10 课的新坐标：谁计算，代表什么

`_validate_fact_sources(data.facts, rows)` 的 `data.facts[].sources[]` 由 LLM 生成，只携带 `{chunk_id, quote}`；`rows` 是服务端 PG 重读的可信输入。程序按 `chunk_id` 找 `row`，计算：

```python
quote_start = row['content'].find(ref.quote)
if row is None or not ref.quote.strip() or quote_start < 0:
    raise ValueError('unverifiable_source')
source = {'citation_id': row['citation_id'], 'chunk_id': ref.chunk_id,
          'quote': ref.quote, 'quote_start': quote_start,
          'quote_end': quote_start + len(ref.quote)}
```

例如 `content='负责星河项目。'`，`quote='负责星河项目'`，得到 `[0,6)`，即 `content[0:6]`。这是 **Chunk 内容内** Unicode 字符位置的左闭右开区间；`markdown_start/end` 是 Chunk 在解析后的 Markdown 文本中的坐标，两套坐标不能混用。重复子串时 `str.find` 只取首次；存在原文子串仅证明字面可追溯，不证明 `answer=yes` 的语义必然正确。若 `quote` 是改写或不连续拼接，验证会失败，该要求的抽取在 `build_evidence_packs` 中转为 `extraction_failed`，不是直接把 `unverifiable_source` 发给前端。

## 3. 点击原文：另一条独立的后端请求链

入口：`backend/app/api.py` 的 `GET /api/evidence/citations/{chunk_id}`：路径必须是 UUID，两个请求头仍必需；可选成对的整数 query `quote_start`、`quote_end`。路由将请求头切为 scopes，调用 `resolve_citation(db, chunk_id, tenant_id=..., permission_scopes=..., quote_start=..., quote_end=...)`。

`resolve_citation` 不是读取上次保存的 pack，也不是相信前端传来的标题或 quote，而是：

1. `db.get(DocumentChunk, chunk_id)` → `DocumentVersion` → `Document`，需要时继续取 `KnowledgeBase`；任何缺失或当前 `doc.status != 'active'` 均用相同的 404 文案“引用不可用或无访问权限”。
2. 验证 `doc.tenant_id`、文档/Chunk/知识库权限范围、`chunk.candidate_id == doc.candidate_id`、知识库租户与启用状态；**先授权，后返回原文和标题**。
3. 再校验 quote 参数必须同时出现，且 `0 <= start < end <= len(chunk.content)`；位置不合法 → 422“引用位置无效”。授权失败 → 404，不能让 422 反向泄露引用是否存在。
4. 返回绑定原始 `document_version_id/version_no` 的 `content`、`document_title`、`heading_path`、`markdown_start/end`、页码/时间戳、`quote_start/end` 等。即使版本已不是 current，只要**现在**仍有权访问，引用可显示 `version_state='historical'`；而**新构包**时 `load_pack_sources` 会排除非当前版本。历史引用可追溯 ≠ 新检索把旧资料当最新证据。

```http
GET /api/evidence/citations/{chunk_id}?quote_start=0&quote_end=6
X-Tenant-ID: t1
X-Permission-Scopes: hr_private
```

**时序示例**：`t0` active、有权 → 200 和正文；`t1` 文档停用 → 同一个 URL、同一凭据 → 404，响应不带标题与正文。此时旧 pack 可能仍在用户浏览器内：服务端能保证“再次请求不再泄露”，不能从远端收回此前已经发送给浏览器的字节；前端必须避免拿旧 drawer/缓存原文冒充新授权结果，必要时刷新检索并标注旧 pack 的时效性。更严格的即时撤权要求另行设计（例如减少 pack 初次返回的原文、失效后局部标记/清除缓存）。

## 4. 前端现状与本课新 UI 边界

目标提交的 `frontend/src/EvidencePackPanel.tsx`：`search()` 向 `/talent-search` 发送 `include_evidence_pack:true`，以 `packs` 状态呈现每人每要求的状态、`yes/no` 事实、`sources` 按钮、冲突索引（界面加 1）、缺失信息、原始 citations。`openCitation()` 调 GET，再以 `citation` 展示抽屉；原文与版本由这次 GET 返回。引用按钮带事实 `quote_start/end` 时高亮；原始 citations 不带 offsets，只展示整块。

```tsx
const chars = Array.from(citation.content);
// 目标提交的 JSX 等价处理：前段 + <mark>chars.slice(start,end)</mark> + 后段
```

使用 `Array.from` 而不是直接 `content.slice`：Python `len/str.find` 基于 Unicode 码点，而 JS 字符串 `.slice` 基于 UTF-16 码元；包含部分表情等字符时会错位。仍需注意更复杂的 Unicode 规范化或多次重复 quote 问题，不能把偏移当永久不变的内容锚点。

**当前缺口**：`openCitation` 的 `catch` 写入通用 `error`；没有单独的“引用失效”提示。它目前不会 `setPacks([])`，但会 `setCitation(null)`。因此作业二主要是**区分引用错误和检索错误**、明确提示及保证一位候选人引用失效时其他候选人仍可核查，并非重新发明整块 pack 展示。

## 5. 作业一：材料状态变化测试（先写失败测试，再验证现有实现）

**目标**：测同一 HTTP 引用端点的“随时间重新授权”，而不是只测直接调用 `resolve_citation`。现有 `backend/tests/test_evidence_citations.py::test_citation_endpoint_rechecks_access` 改的是文档 `permission_scope`；现有参数化直接函数测试含 `deleted`，但还缺题目要求的“同一个引用第一次 200，停用后第二次 404，并且标题和 Chunk 原文都不泄漏”的完整端点时序。

1. 定位 `fixture()`：`MemoryRows` 假 DB 包含 `db, kb, doc, version, chunk`；其中 `doc.title == '项目复盘'`、`chunk.content == '负责星河项目。'`，并覆盖 `app.database.get_db`。不新建第二份文档/客户端；**复用同一对象**变更状态。
2. 在 `backend/tests/test_evidence_citations.py` 添加以下测试，先跑单测，确认其断言能抓住若删去 `doc.status` 判定的回归，再保留真实实现。

```python
def test_citation_endpoint_rechecks_document_status_without_leak():
    from app.main import create_app
    from app.database import get_db
    from fastapi.testclient import TestClient

    db, kb, doc, version, chunk = fixture()
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    headers = {'X-Tenant-ID': 't1', 'X-Permission-Scopes': 'hr_private'}
    url = f'/api/evidence/citations/{chunk.id}'

    first = client.get(url, headers=headers)
    assert first.status_code == 200
    assert first.json()['document_title'] == doc.title
    assert first.json()['content'] == chunk.content

    doc.status = 'inactive'  # 本测试的 SimpleNamespace 假对象可直接变更
    second = client.get(url, headers=headers)
    assert second.status_code == 404
    assert second.json()['detail'] == '引用不可用或无访问权限'
    assert doc.title not in second.text
    assert chunk.content not in second.text
```

3. 单测命令：在 `backend` 目录执行 `uv run --no-sync pytest -q tests/test_evidence_citations.py::test_citation_endpoint_rechecks_document_status_without_leak`；再运行整份 `tests/test_evidence_citations.py`。**成功标准**：第一次确实返回可见标题/原文，第二次是 404、统一文案、响应体不含这两段敏感文本；若只写 `assert 404`，无法防止错误响应仍携带原文。可再覆盖知识库停用、文档删除及有 quote offsets 的同一路径，但别把“文档权限改了”冒充“材料状态停用”。

> 为什么不是 403？统一 404 让无权限者不能从状态码区分“有这个引用但你无权访问”与“引用不存在”。接口仍需结合鉴权、日志与缓存策略；404 本身不构成完整安全方案。若改成真实 DB 集成测试，须确认该工程 `Document.status` 可用的状态值，并提交事务/刷新会话；上述假 DB 测试无需迁移或提交事务。

## 6. 作业二：引用失效提示，但不牵连其他候选人

**目标场景**：一次搜索展示候选人 A、B；A 的材料随后停用。点击 A 的引用得到 404，显示“引用已失效或当前无访问权限，请重新检索”；A 的旧事实只作为旧搜索结果保留并标记待刷新，B 的 pack 和 B 的引用不受影响。不得把 A 的旧正文再次放进原文抽屉。

1. 在 `frontend/src/EvidencePackPanel.tsx` 新增 `citationError`（或 `{id,message}`）独立于检索 `error`。可加 `invalidCitationIds` 集合标记特定引用/候选人的旧结果“引用待刷新”；不要用一个全局失败状态把整个 `packs` 清空。
2. `read()` 目前 `throw new Error(detail)` 会丢掉 HTTP 状态码。引入带 `status` 的异常以**基于状态码**区分 404、422、网络失败，而不是解析中文错误文案。例如：

```tsx
class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}
// 在 read() 的 !response.ok 分支：
throw new ApiError(response.status,
  typeof data.detail === 'string' ? data.detail : '请求失败');
```

3. `openCitation` 点击前 `setCitation(null)`，清除这次的 `citationError` 并置 `citationBusy=true`；请求仍带当前租户/scope 及可选两段 quote offsets；404 时只更新 `citationError` 和对应 `invalidCitationIds`，**不调用 `setPacks([])`**；422 应提示位置无效/重新检索，不要一律说文档停用。成功时设 `citation` 并清除相应错误标记；`finally` 恢复 busy。可参考下面只展示关键分支的伪补丁：

```tsx
// 新 state：const [citationError, setCitationError] = useState('');
// 新 state：const [invalidCitationIds, setInvalidCitationIds] = useState<string[]>([]);
// openCitation 中：
setCitation(null); setCitationError(''); setCitationBusy(true);
try {
  const fresh = await read(`/evidence/citations/${encodeURIComponent(id)}${quoteQuery}`);
  setCitation(fresh);
  setInvalidCitationIds(ids => ids.filter(item => item !== id));
} catch (e) {
  if (e instanceof ApiError && e.status === 404) {
    setCitationError('引用已失效或当前无访问权限，请重新检索。');
    setInvalidCitationIds(ids => ids.includes(id) ? ids : [...ids, id]);
  } else {
    setCitationError(e instanceof Error ? e.message : '引用核查失败');
  }
} finally { setCitationBusy(false); }
// JSX：{citationError && <p role="alert">{citationError}</p>}
```

4. 将失效消息放在引用面板附近，并在对应来源按钮/候选要求旁显示“该引用需重新核查”；旧 `packs` 仍可用于观察候选/冲突概况，**不可把失效引用的旧 `citation.content` 重新展示**。在新一次 `search()` 时清空引用错误/失效 ID 后重建 packs。当前 UI 的 `facts[].sources[].quote` 及 `citations[].document_title/content` 已随初次 pack 到达前端；如果产品要求撤权后也不展示这些历史缓存内容，需要另行修改 pack 载荷和前端清理策略，本作业的 404/抽屉清空不能实现“收回客户端已收到的数据”。
5. 手工或组件测试验收：初次两名候选人 A/B 都显示 → 让 A 的引用返回 404 → 警示可见，抽屉无 A 的旧正文，B 的 pack 仍在 → 点击 B 的引用返回 200、B 原文正常且本次提示清除 → 再检索时状态按新权限重建。增加 422 与网络错误检查，避免误报为停用。若要写组件自动化测试，现有 `frontend/package.json` 仅定义 `dev`/`build`，应先建立测试设施，不能把“完成 build”说成“通过前端行为测试”。

### 完整回归与构建（作业完成后实际执行）

```powershell
# PowerShell；在仓库根目录
Set-Location backend
uv run --no-sync pytest -q
Set-Location ..\frontend
npm run build
```

`npm run build` 对应 `tsc -b && vite build`；回归须跑**完整后端测试集**，不能仅跑下面五个相关文件后称为完整回归：`test_query_plan.py`、`test_hybrid_search_service.py`、`test_talent_search_api.py`、`test_evidence_pack.py`、`test_evidence_citations.py`。依赖没装或外部样本缺失导致环境失败时要报告实际命令/失败原因，不要宣称通过。目标提交 README 的历史测试数字不是本次运行结论。

开发联调按 README：`docker compose -p talent-eval-agents-course up -d` 启动中间件；本地 `backend` 分别运行 `uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 18080` 和 `uv run python -m app.worker`；前端 `npm run dev`。材料导入/索引与模型配置要事先准备；单元测试使用替身不能代替真实检索、文档停用再打开的端到端验收。仅做此作业的假 DB 测试不要求启动整套依赖。

## 7. 易错点、老师想考的能力与自测

| 容易出现的错误 | 正确设计/排查方向 |
|---|---|
| 用索引中的 `content` 直接生成 pack 或返回给用户 | `include_evidence_pack` 路径先 PG 重读当前文档/版本/权限，返回的 `chunks` 也由可信 source 替换并过滤。 |
| 把旧 pack 的有权访问当作永久授权 | `GET` 每次重新查文档/知识库状态和当前权限；停用后同一 URL 404。 |
| 404 错误体包含标题、Chunk 内容、异常字符串 | 统一不可用文案；测试同时断言状态码和两类内容不泄漏。 |
| 将 `markdown_start` 当成 quote 的局部位置 | quote 用 Chunk 内 `[quote_start,quote_end)`；解析全文坐标另用 markdown offsets。 |
| `quote` 存在就判事实充分 | quote 校验解决“可定位”，冲突/覆盖/缺失仍由事实整理规则处理。 |
| A 的引用打不开就清空 A/B 所有 `packs` | 将引用错误与搜索错误分离；保留 B，A 的失效来源单独标注；失效引用不复用旧抽屉。 |
| 定向 43 个相关测试当“完整回归” | 完整跑 `backend` 全套 `pytest -q`；再跑 `frontend` 的 `npm run build`；分别记结果。 |

**老师希望你能回答**：

1. 为什么召回的 Chunk 还要“PG 重读 + 当前权限复核”？因为索引可能滞后或权限/状态变化。
2. 为什么打开引用需要新 HTTP 请求，而不是直接展开搜索时的结果？为了以**点击时**的权限作准、绑定原始材料版本，并从服务端取可核查正文。
3. 为什么位置高亮由程序计算，且同时有 Chunk 局部坐标和 Markdown 全文坐标？前者校验 quote 原样可追溯并用于高亮，后者定位文档，不能互换。
4. 为什么第二次 404 仍要断言“不包含标题和原文”？安全性看响应载荷，不仅看状态码。
5. 为什么引用失效只处理这条引用，却不能宣称浏览器历史内容被真正撤回？组件局部容错与服务器未来拒绝访问不同于撤销已经传输的数据。

完成标准：你能拿着本文从路由逐层指出每次输入、输出和权限检查位置；能自己补上作业一的端点时序测试；能解释作业二的 404/422/其他错误分支和双候选人 UI；能独立执行并解释完整回归、生产构建和联调结果。

