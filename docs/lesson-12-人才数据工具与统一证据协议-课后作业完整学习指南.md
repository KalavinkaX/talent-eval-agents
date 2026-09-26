# 第 12 课：人才数据工具与统一证据协议——课后作业完整学习与实现指南

> 适用代码：`D:\Code\K_Course\talent-eval-agents_learning\backend`。本讲义依据本仓库的实际实现解释“模型 → 工具 → 执行器 → Service → 数据 / 证据 → 主图”的链路；第五节起保留原有四道作业的逐题指南。文中明确区分**当前代码已经做到的事**与**生产环境建议**。

## 一、课程定位、学习目标与阅读路线

### 1.1 为什么第 12 课不是“再造一个搜索接口”

第 8 课已有 `QueryPlan`、`FilterCondition` 和安全 SQL Builder；第 9 课已有 Candidate Evidence Pack 2.0 的事实抽取、合并、冲突检测和引用验证；第 11 课主图已有接收请求、获取候选人、分支和报告的状态位置。本课补上 Agent **安全且可治理地调用这些已有能力**所需的适配层：四个只读工具、统一结果协议、通用可靠性执行器、岗位 JD / 工具审计表，以及把结构化筛选接到主图的 Provider。

一句话：**模型决定“想查什么”；受信任的运行环境决定“代表谁、能查谁”；业务代码决定“如何查”；证据模块决定“事实可否引用”；执行器决定“失败如何治理”。**

| 层 | 本仓库入口 | 自己负责 | 不负责 |
|---|---|---|---|
| Agent/Tool | `build_talent_tools()`、`@tool` | 公开业务参数 Schema、接收 `ToolRuntime` | 让模型设置租户、直接写 SQL |
| 运行时身份 | `TalentToolContext` / `DecisionContext` | 可信租户、权限、调用身份、Run 关联 | 替模型提取岗位条件 |
| 执行治理 | `ToolExecutor`、`ToolPolicy`、`CircuitBreaker` | 超时、重试、熔断、结果封装、审计 | 判定候选人是否适合岗位 |
| 业务查询 | `TalentToolService` | JD、SQL 候选人、档案、证据 Provider 调用 | 对每个工具重新实现可靠性策略 |
| 事实/证据 | SQLAlchemy、混合检索、`evidence_pack.py` | 数据筛选、证据加工与引用校验 | 决定 Agent 代表哪个租户 |
| 主图 | `build_talent_decision_graph()` | 状态转移、候选人范围、报告占位 | 在节点里写死候选人列表 |

读代码建议按顺序：`backend/app/talent_tools.py` → `backend/app/query_plan.py` → `backend/app/api.py` / `evidence_citations.py` / `evidence_pack.py` → `backend/app/talent_decision_graph.py` → `backend/tests/test_talent_tools.py` / `test_talent_decision_graph.py`。第五节以后再按四道作业动手。

### 1.2 先建立两个入口的心智模型

**入口 A，Agent Tool 调用：**模型发出包含业务参数的 tool call → `@tool`/Pydantic 校验 → LangChain `ToolRuntime[TalentToolContext]` 注入上下文 → Tool 函数把 Service 调用包在 `ToolExecutor.execute()` 中 → Service 使用 Session / evidence_provider → `_finish()` 返回统一 `{ok,data,error,meta}` 并审计。此入口的租户、权限不是模型参数。

**入口 B，已有 HTTP 证据检索：**`backend/app/main.py` 注册 `backend/app/api.py` 的 Router；`POST /api/talent-search` 接收 `TalentSearchInput` → `compile_query_plan` → `select_candidate_ids` → 带租户、权限、候选人集合的 `EvidenceFilter` → 混合检索 → `load_pack_sources` 对 PostgreSQL 现存版本及权限重新核验 → `build_evidence_packs` → 可选 `evidence_packs`。HTTP 入口与 Agent Tool 入口是两种调用界面，不能把二者的可信身份来源混为一谈。现有 HTTP 示例从请求 Header 接收租户和权限；**真实生产身份需要网关 / 认证层核验并生成可信上下文，不能因它来自 Header 就自动当作可信。**

---

## 二、工具协议与权限边界

### 2.1 当前调用缺口：能力存在 ≠ Agent 可安全调用

已有 SQL/检索函数是业务能力，但模型调用还缺少：输入范围约束；身份、租户、权限的隔离；失败后的可预期返回；跨四个工具一致的超时、重试、熔断和审计。若让模型自行传 `tenant_id='tenant_B'`，即使函数“可以查数据库”，也不符合多租户安全边界。若让每个工具分别 `try/except`，又容易出现 A 工具超时可重试、B 工具没有审计、C 工具返回另一种错误格式。第 12 课的重点是**能力包装与治理**，不是重新发明 SQL 或证据算法。

### 2.2 模型参数与运行上下文

`backend/app/talent_tools.py` 里的 `build_talent_tools(service, executor)` 注册四个 `@tool`。例如档案工具的实质结构：

```python
@tool
def get_candidate_profiles(
    candidate_ids: Annotated[list[str], Field(min_length=1, max_length=100)],
    runtime: ToolRuntime[TalentToolContext],
):
    return executor.execute(
        "get_candidate_profiles",
        lambda: service.get_candidate_profiles(candidate_ids, context=runtime.context),
        context=runtime.context,
        arguments={"candidate_ids": candidate_ids},
    )
```

`candidate_ids` 是模型可提交的业务参数，`Field` 限定最多 100 个；`runtime` 是框架注入而非模型可填写字段。其他模型可提交字段包括岗位 `query/limit`、结构化 `filters`、证据语义 `query/candidate_ids`。`TalentToolContext(tenant_id, permission_scopes, actor_id, run_id)` 则由可信运行环境创建。验证边界的方法不是只读函数签名，而是检查 `tool.tool_call_schema.model_json_schema()`：其中可见 `candidate_ids`，不应包含 `runtime` / `tenant_id` / `permission_scopes`。

从模型到数据的安全链条：模型提议 `candidate_ids=['E001','E999']`；Service 的 `_check_context()` 先确认租户非空且具有权限范围；档案 SQL 再以 `EmployeeProfile.tenant_id == context.tenant_id`、编号集合、`employment_status == 'active'` 求交集。因此模型给出其他租户编号也不能从该查询得到对应档案。注意“有权限范围”与“每一种资料都做了细粒度权限过滤”不同：档案查询现状主要是租户与在职约束；证据链才将 `permission_scopes` 传给下游并在来源核验时使用。生产扩展敏感字段时还需增加字段/文档级授权。

**两种 Context 不要混淆：**Agent Tool 使用 `ToolRuntime[TalentToolContext]`；第 11 课主图节点使用 `Runtime[DecisionContext]`。`build_candidate_provider()` 把后者转换为前者；主图并非通过模型生成一次 `ToolNode` 调用。`actor_id` / `run_id` 用于归因审计，不应让自然语言覆盖。

### 2.3 统一返回协议、失败语义及边界

四个工具经 `ToolExecutor._finish()` 返回相同**外层**结构；成功时 `error=null`、`ok=true`，失败时 `data=null`、`ok=false`，但字段仍齐全：

```json
{
  "ok": false,
  "data": null,
  "error": {"code": "dependency_unavailable", "message": "依赖服务调用超时", "retryable": true},
  "meta": {"tool_name": "search_candidate_evidence", "call_id": "...", "attempts": 2, "degraded": true}
}
```

成功的 `data` 是各业务数据：JD 匹配列表、候选人编号列表、档案列表或证据 Provider 的结果。**统一的是外层，不是把每类 `data` 强行变成相同内容。**证据 `data.evidence_packs[*].schema_version='2.0'` 属于第 9 课的内层证据协议，不能与 `_finish()` 外层协议混淆。`meta.call_id` 和审计记录关联同一次调用；当前 `meta` 不包含总耗时，总耗时在审计记录的 `duration_ms` 中。

| 发生位置/错误 | 当前返回编码 | 自动重试？ | 典型处理 |
|---|---|---|---|
| Service 内 `ValueError`（如空岗位名称、证据版本不支持） | `invalid_argument` | 否 | 改输入或查协议版本；不能盲重试 |
| Service 内 `PermissionError`（缺租户或权限范围） | `permission_denied` | 否 | 阻断请求、修正可信身份配置 |
| `TransientToolError` 或单次 `FutureTimeoutError` 且用尽尝试 | `dependency_unavailable`，`degraded=true` | 会先有限次重试 | 可提示用户稍后重试，并排查依赖 |
| 断路器已打开 | `circuit_open`，`degraded=true`，`attempts=0` | 本次不访问依赖 | 等恢复窗口并观测下游 |
| `@tool` / Pydantic Schema 拒绝参数，例如 101 个 ID | 框架校验失败（不会调用 `_finish()`） | 否 | 修正调用参数；不会执行 SQL 或写入执行器审计 |

**重要细节：**表中前四行是执行器拿到控制权之后的返回；第五行发生在进入 Tool 函数之前。当前实现不能声称“所有 Schema 校验失败也产生 `_finish()` 审计”。当前执行器也没有兜底捕获任意 `Exception`；未知依赖异常不一定转换为 `dependency_unavailable`，需要在依赖适配器处转成 `TransientToolError` 或扩展经过审核的异常映射，避免误把业务编程错误当作可重试网络故障。

### 2.4 证据协议复用：怎样准确接上第 9 课

**先分清“检索命中”与“可信证据”：**向量/关键词检索给出可能相关的 Chunk；引用还要核验原始文档是否仍存在、是否为当前版本、是否属于当前租户与候选人、权限是否允许读取；事实还要有原文定位、重复归并、冲突标记。第 9 课 Evidence Pack 2.0 把这些工作标准化，工具层只调用它，不重新写一套证据提取。

逐步追踪已有 HTTP 能力（`backend/app/api.py`）：

1. `TalentSearchInput.include_evidence_pack=True` 表示除了检索结果还要生成 Evidence Pack；默认是 `False`，因此集成适配器必须显式开启。
2. `compile_query_plan()` 和 `select_candidate_ids()` 先确定结构化候选人范围（第 8 课）；`EvidenceFilter(tenant_id, permission_scopes, candidate_ids)` 限制混合检索的检索空间。**不能仅在拿到向量命中后才用 Python 删除越权候选人**。
3. `backend/app/evidence_citations.py` 的 `load_pack_sources` / `resolve_citation` 再回 PostgreSQL 复核 Document、Chunk、知识库、当前版本、租户、权限及候选人一致性；必要时拼同版本的父级上下文。向量索引中的旧命中不能自动变成当前可引用证据。
4. `backend/app/evidence_pack.py` 的 `build_evidence_packs()` 结合事实提取器，把来源组织为按候选人 / 要求排列的 Pack；`_validate_fact_sources()` 检查引用的原句确实出现在当前 Chunk，并计算片段偏移和 citation 标识；`_merge_duplicate_facts()` 归并重复事实；`_find_conflicts()` 检测同事件、同期、同主张的冲突；`_evidence_status()` 标识 sufficient / partial / conflicting / missing。每个 Pack 带 `schema_version='2.0'`，避免下游把不兼容结构误认为已验证事实。
5. 第 12 课 `TalentToolService.search_candidate_evidence(query, candidate_ids, context=...)` **只负责边界与协议**：先检查 Context，再向注入的 `evidence_provider` 传 `query`、限定后的 `candidate_ids`、`tenant_id=context.tenant_id`、`permission_scopes=list(context.permission_scopes)` 和 `include_evidence_pack=True`；遍历返回的 `evidence_packs` 检查版本 `2.0`。未配置 Provider 时抛 `TransientToolError`；版本不对抛 `ValueError`（执行器映射为 `invalid_argument`）。它**没有重做**抽取、冲突检测或引用校验。

```text
第 8 课：模型意图 → FilterCondition/QueryPlan → SQL 得到可信候选人范围
                                       ↓
第 9 课：受限范围的检索 → 来源复核 → 事实抽取/合并/冲突/引用 → Pack 2.0
                                       ↓
第 12 课：Tool 注入权限、限制范围、调用 Provider、校验 Pack 版本、统一错误/审计
                                       ↓
第 11 课：主图保存 candidate_ids / evidence_refs，之后评估时引用证据
```

**代码现状和接线方案要分开：**`TalentToolService` 通过构造参数**注入** `evidence_provider`，仓库中的工具测试使用测试替身；不能说现有 `POST /api/talent-search` 已自动成为这个 Provider。若要真正贯通，应写一个适配器，在服务端以已认证的 Context 复用已有检索 Service（或经过可信认证的 HTTP 客户端），把上述四个边界参数原样传下去，返回与 Tool Service 预期相符的 `evidence_packs` 字典，并测试跨租户、陈旧版本、权限不足及版本错误。不能让模型拼 HTTP Header 代替认证。图中的 `evidence_refs` 是主图预留状态；当前主图的 `_evaluate` 仍是占位实现，不应宣称整条推荐证据链已自动落地。

---

## 三、四类人才数据工具

### 3.1 工具清单：对照真实函数看“入口—输出—数据层”

以下签名均在 `backend/app/talent_tools.py` 的 `build_talent_tools(service, executor)` 中定义。注意：`runtime: ToolRuntime[TalentToolContext]` 在 Python 函数签名中，却不属于模型可见 Schema；`Field(...)` 定义的是模型的输入契约。

| Tool 函数 | 模型提交的参数 | `executor.execute()` 中延迟执行的 Service 方法 | `data` 与来源 |
|---|---|---|---|
| `lookup_job_descriptions` | `query` 1～200 字、`limit` 1～10（默认 5） | `service.lookup_job_descriptions(query, limit=limit, context=runtime.context)` | JD 列表，`JobDescription` 表 |
| `filter_candidates` | `filters: list[FilterCondition]` 最多 20 项 | `service.filter_candidates(filters, context=runtime.context)` | 排序后的候选人编号，`EmployeeProfile` 表 + 第 8 课 Query Plan / SQL Builder |
| `search_candidate_evidence` | `query` 1～500 字、`candidate_ids` 1～100 项 | `service.search_candidate_evidence(query, candidate_ids, context=runtime.context)` | Provider 返回的证据字典及内层 Pack 2.0，注入的检索依赖 |
| `get_candidate_profiles` | `candidate_ids` 1～100 项 | `service.get_candidate_profiles(candidate_ids, context=runtime.context)` | 基础档案列表，`EmployeeProfile` 表 |

理解每个 Tool 的通用读法：① `@tool` + `Annotated[..., Field(...)]` 限制模型输入；② `runtime.context` 获取可信身份；③ `lambda: service...` 只是创建“待执行函数”，这一步**没有**查库；④ `executor.execute(name, lambda, context=..., arguments=...)` 负责治理；⑤ Service 实际读库或调用 Provider；⑥ `_finish()` 封装、审计并返回。失败时的 `data` 为 `None`，而不是上述业务类型。

验证 Schema 的实际办法（参看 `backend/tests/test_talent_tools.py::test_tool_schema_hides_runtime_context`）：

```python
tools = build_talent_tools(service, ToolExecutor())
schemas = {t.name: t.tool_call_schema.model_json_schema() for t in tools}
assert set(schemas) == {
    "lookup_job_descriptions", "filter_candidates",
    "search_candidate_evidence", "get_candidate_profiles",
}
assert "runtime" not in schemas["lookup_job_descriptions"]["properties"]
```

下一节按**一个真实请求从 Tool 到返回**的方式逐个拆开，而不是只记工具名。

### 3.2 岗位 JD 名称检索：先安全过滤，再相似度匹配

**入口与具体代码**（`backend/app/talent_tools.py`）：

```python
@tool
def lookup_job_descriptions(
    query: Annotated[str, Field(min_length=1, max_length=200)],
    runtime: ToolRuntime[TalentToolContext],
    limit: Annotated[int, Field(ge=1, le=10)] = 5,
):
    return executor.execute(
        "lookup_job_descriptions",
        lambda: service.lookup_job_descriptions(query, limit=limit, context=runtime.context),
        context=runtime.context, arguments={"query": query, "limit": limit},
    )
```

逐步跟踪假设请求“查高级 AI 应用工程师”：模型只能提交 `query/limit` → Schema 拦掉空字符串、超长输入、越界 `limit` → Runtime 注入租户 → 执行器放行后才执行上面的 lambda → `TalentToolService.lookup_job_descriptions` 调用 `_check_context`（租户不得空、权限范围不得空）→ `normalized_query = "".join(query.lower().split())` 消除大小写和空白差异；全空白字符串在此处抛 `ValueError` → `session_factory()` 打开 SQLAlchemy Session：

```python
rows = db.scalars(select(JobDescription).where(
    JobDescription.tenant_id == context.tenant_id,
    JobDescription.status == "active",
)).all()
```

只有本租户、有效 JD 被加载后，才按 `normalized_name` 与 `normalized_query` 比较：包含关系打 1.0，否则 `SequenceMatcher(...).ratio()`；低于 0.3 不返回；最后 `sorted(matches, key=lambda x: (-x["match_score"], x["job_code"]))[:limit]`。输出每条含 `job_code/name/match_score/version/content`，Tool 外层是 `{ok: true, data: [...], error: null, meta: ...}`。**`version` 不参与当前排序**，同名不同版本都可出现；若业务要求“只查最新有效版本”，要另定义版本策略，而不是误读当前代码。多个匹配仅供后续澄清，模型不能编造不存在的 JD。

**为什么这样分层？**若先对全库做相似度、最后再过滤租户，敏感数据先进入内存匹配池；若只返回 `name` 而不返回 `job_code/version`，同名不同版本无法追溯。`backend/app/models.py::JobDescription` 显式保存 `job_code`（全局唯一）、`version` 与 `status`；本题测试应通过这三个字段证明边界。

### 3.3 候选人结构化筛选：逐行连接第 8 课 SQL 与第 9 课证据

**Tool 入参：**`filters: Annotated[list[FilterCondition], Field(max_length=20)]`。`backend/app/query_plan.py::FilterCondition` 将 `field` 限为 `FilterField` 枚举、`operator` 限为 `eq/in/lt/lte/gt/gte`；`model_validator` 要求范围运算只用于年龄/工作年限，`in` 的值必须为列表。因此模型说“上海、L3、五年以上”时，能提交业务 DSL，不能直接提交 SQL 或租户。示例：

```python
filters = [
    FilterCondition(field="region", operator="eq", value="上海"),
    FilterCondition(field="job_level", operator="eq", value="L3"),
    FilterCondition(field="years_of_experience", operator="gte", value=5),
]
```

**Service 代码的每一步：**

```python
self._check_context(context)
plan = QueryPlan(task_type=TaskType.FIND_TALENT, filters=filters)
with self.session_factory() as db:
    return select_candidate_ids(db, plan, tenant_id=context.tenant_id)
```

`select_candidate_ids` 不是重新拼 SQL，它调用 `build_candidate_statement(plan, tenant_id=...)`：首先拒绝空租户或带 `clarifications` 的不可执行计划；随后**固定**追加 `EmployeeProfile.tenant_id == tenant_id`、`employment_status == "active"`；依次查 `FILTER_FIELD_REGISTRY[item.field]`，检验该字段支持该操作符，调用 `spec.build(item, current_day)` 生成 SQLAlchemy 表达式；最终 `select(EmployeeProfile.employee_no).where(and_(*clauses)).order_by(EmployeeProfile.employee_no)`，`db.scalars(...).all()` 返回编号列表。即使模型提交 `employment_status=inactive`，与服务端强制 active 条件求交也不会读出停用人。年龄边界还由 `_age_spec` 将年龄比较换算为出生日期条件。

**跨课链路与“为什么不是一回事”：**第 8 课只证明这些人符合**可入库的结构化条件**，产生 `candidate_ids`；第 9 课接受此范围中的材料命中、做来源核验与事实抽取，才回答“做过企业知识库吗”。例如 SQL 返回 `['C001']` 只意味着“本租户的上海 L3 在职候选人符合年限要求”，**绝不**等同于 C001 的项目经验已获证据支持。第 12 课的筛选 Tool 把这个列表交给后续证据 Tool 或主图，使检索范围不会从全库开始。第 9 课没有替代第 8 课 SQL，第 8 课也没有替代第 9 课材料证据。

**边界注意：**Schema 对模型调用最多 20 条，但直接调用 Service 的其他入口须自己保证合法列表；`FilterCondition` 的字段枚举与 Registry 双重限制，比提示模型“不要拼 SQL”更可靠。

### 3.4 候选人证据检索：Tool 实际传了什么，Pack 实际谁生成

**Tool 入口**（`backend/app/talent_tools.py`）：

```python
@tool
def search_candidate_evidence(
    query: Annotated[str, Field(min_length=1, max_length=500)],
    candidate_ids: Annotated[list[str], Field(min_length=1, max_length=100)],
    runtime: ToolRuntime[TalentToolContext],
):
    return executor.execute(
        "search_candidate_evidence",
        lambda: service.search_candidate_evidence(query, candidate_ids, context=runtime.context),
        context=runtime.context,
        arguments={"query": query, "candidate_ids": candidate_ids},
    )
```

请求“核查 C001 是否参与知识库项目”时，模型提交语义 `query` 与既有 `candidate_ids`；执行器调用 Service，`_check_context` 防止缺失可信租户/权限；之后 Service 原样调用注入的 Provider：

```python
if self.evidence_provider is None:
    raise TransientToolError("evidence_provider_not_configured")
result = self.evidence_provider(
    query=query,
    candidate_ids=candidate_ids,
    tenant_id=context.tenant_id,
    permission_scopes=list(context.permission_scopes),
    include_evidence_pack=True,
)
for pack in result.get("evidence_packs", []):
    if pack.get("schema_version") != "2.0":
        raise ValueError("不支持的证据协议版本")
return result
```

`None` Provider → 最终（用尽尝试后）`dependency_unavailable`；错版本 → `invalid_argument` 且不重试。代码只检查**存在的 Pack**的版本，不校验“必须至少有一个 Pack”或“Provider 回来的每个 ID 必须在输入范围内”；严格生产契约可以再补充结果集合一致性校验。测试 `test_evidence_search_preserves_evidence_pack_v2` 用 stub Provider 断言 `tenant_id/permission_scopes` 并返回 Pack 2.0，这只能证明参数透传与版本处理，不等于真实 Milvus 链路已接通。

要把第 9 课能力接起来：写服务端可信 Provider 适配器，复用现有 `backend/app/api.py::search_talent` 所用的检索 Service / `EvidenceFilter(tenant_id, permission_scopes, candidate_ids)`、`backend/app/evidence_citations.py::load_pack_sources` 和 `backend/app/evidence_pack.py::build_evidence_packs`；若采用 HTTP 调用，也要确保请求身份来自认证层而非模型；适配器须把 `include_evidence_pack=True` 映射到实际调用，再返回期望的字典。Pack 中的原文引用核验、事实合并与冲突仍由第 9 课模块负责。**此处说明的是接线设计，不是当前仓库已经自动完成了 HTTP Provider 接线。**

### 3.5 候选人基础信息获取：批量上限与 SQL 求交

**Tool 入口：**`candidate_ids: Annotated[list[str], Field(min_length=1, max_length=100)]` 加 `runtime: ToolRuntime[TalentToolContext]`；与证据 Tool 使用相同的 100 项上限。Tool 将 `lambda: service.get_candidate_profiles(candidate_ids, context=runtime.context)` 交给执行器。**101 个 ID 在这个 Tool 的 Schema 校验阶段失败**，不会运行 lambda；`_finish` 审计只覆盖进入执行器的调用，Schema 提前拒绝不在此列。

Service 的关键分支与 SQL：

```python
self._check_context(context)
if not candidate_ids:
    return []
with self.session_factory() as db:
    rows = db.scalars(select(EmployeeProfile).where(
        EmployeeProfile.tenant_id == context.tenant_id,
        EmployeeProfile.employee_no.in_(candidate_ids),
        EmployeeProfile.employment_status == "active",
    ).order_by(EmployeeProfile.employee_no)).all()
```

之后每条仅投影 `candidate_id/name/region/current_position/job_level/years_of_experience/department`，不返回整张 ORM 行。若输入 `['C001','C002']`，C002 属于其他租户，只能得到 C001；若 ID 不存在返回空列表。**模型侧**禁止空列表，**直接 Service 调用**允许空列表并立刻返回，二者是不同入口的契约；目前 Service 不自行重新检查 100 项上限。档案字段是结构化事实，不是材料引用；需要证明项目经历时必须继续走第 9 课证据链。

### 3.6 主图适配：Provider 接口、闭包与运行时安全

第 11 课主图定义 `CandidateProvider = Callable[[TalentRequest, DecisionContext], list[str]]`：它**只约定输入、输出**，不关心候选人来自固定测试数组还是数据库。`_candidate_node(candidate_provider)` 在 `retrieve_candidates` 执行时读取 `state['request']` 与 `runtime.context`，调用 Provider，并写入 `candidate_ids`；空结果走 `no_candidates`，非空走 `evaluate`（当前评估仍为 placeholder）。第 12 课的 `build_candidate_provider(service, *, compile_filters)` 就是把已有筛选 Service 变成这个接口的适配器。

**先理解闭包（没有见过也能读懂）：**普通函数的局部变量在返回后通常不再由调用者直接访问；如果内部函数仍引用外层函数的变量，返回该内部函数后，它会连同所需的外层环境一起保留下来，这种“函数 + 捕获的环境”就是闭包。可以想象成先把“数据库筛选器 service”和“把自然语言编译为 filters 的函数 compile_filters”装入一个专用工具箱；以后每次主图运行只需递交本次 `request` 与 `decision_context`。闭包**不冻结每次请求的租户**：租户每次运行时从 `decision_context` 中重新读取。

```python
# 构图时注入长期依赖（定义阶段）
provider = build_candidate_provider(service, compile_filters=compile_filters)
graph = build_talent_decision_graph(provider)

# 调用主图时输入本次请求和受信任的运行上下文（执行阶段）
result = graph.invoke(
    {"request_text": "找上海 L3 的候选人", "messages": []},
    context=DecisionContext(tenant_id="tenant_A", permission_scopes=("hr_private",)),
)
# result['candidate_ids'] 应来自 tenant_A 的实际 SQL 筛选结果
```

内部逻辑近似（请到 `backend/app/talent_tools.py` 对照源文件）：

```python
def build_candidate_provider(service, *, compile_filters):
    def provider(request, decision_context):
        context = TalentToolContext(
            tenant_id=decision_context.tenant_id,
            permission_scopes=tuple(decision_context.permission_scopes),
            actor_id="langgraph",
        )
        filters = compile_filters(request["original_text"])
        return service.filter_candidates(filters, context=context)
    return provider
```

分两次看变量从哪里来：**构造时** `service/compile_filters` 由外层捕获并保留；**调用时** `request/decision_context` 从当前主图运行传入；**函数内**从自然语言编译 filters、转换 Context、执行 SQL；**返回时**列表落到主图 `candidate_ids`。`compile_filters` 可以是测试中的确定性编译器或生产中受控的条件解析器，但必须输出合法 `FilterCondition`，不得把自然语言拼进 SQL。当前适配器创建 `actor_id='langgraph'`，`run_id` 未显式传入（使用默认值）；更关键的是**它直接调用 Service，不经过四个 `@tool` 的 `ToolExecutor`**，所以并不自动继承 Tool 的重试/熔断/审计。如果要求主图内部也纳入统一治理，需要显式包装该调用并保持 Provider 返回 `list[str]` 的类型契约，同时把真实 Run ID 传播进去，不能未经设计就假称“已经审计”。

---

## 四、可靠性与验证

### 4.1 通用执行器：`ToolExecutor.execute()` 从入口到退出逐句读懂

> 源码：`backend/app/talent_tools.py::ToolExecutor`；下面的代码保留真实控制流，只去掉课件性质的行内注释。四个 `@tool` 都复用这一执行器。**它执行“如何安全地调用”，不决定“筛出谁、证据内容是什么”。**

#### 4.1.1 先找调用方：什么在调用 `execute`？

以证据工具为例，`build_talent_tools` 中模型只提交 `query/candidate_ids`，框架在调用时注入 `runtime`：

```python
@tool
def search_candidate_evidence(query, candidate_ids,
                              runtime: ToolRuntime[TalentToolContext]):
    return executor.execute(
        "search_candidate_evidence",
        lambda: service.search_candidate_evidence(
            query, candidate_ids, context=runtime.context,
        ),
        context=runtime.context,
        arguments={"query": query, "candidate_ids": candidate_ids},
    )
```

上面是**便于观察调用关系的省略 Schema 阅读版**；真实签名的 `query/candidate_ids` 有 Pydantic `Annotated[..., Field(...)]` 约束，详见第 3 节。逐项看 `execute(name, operation, *, context, arguments)`：

| 形参 | 实际由谁给 | 在执行器里起什么作用 | 不能误解为 |
|---|---|---|---|
| `name: str` | Tool 包装层写死的工具名 | 查专属策略、作为断路器键、写返回 `meta.tool_name` 和审计 | 不是模型可随意指定的租户或依赖名 |
| `operation: Callable[[], Any]` | Tool 层的 `lambda: service...` | 一次 attempt 要执行的业务闭包；`pool.submit(operation)` 时才真正开始读取 Service/数据库/检索依赖 | 不是已经执行完的结果；**重试会再次调用同一个闭包** |
| `context: TalentToolContext` | `runtime.context`，由受信调用环境注入 | 传给 Service 作租户和权限约束；`_finish` 从中提取租户/身份/Run 审计字段 | 不是 `execute` 自身重新认证用户；Service 的 `_check_context` 和 SQL 仍要强制权限边界 |
| `arguments: dict` | Tool 包装层给出的业务参数字典 | `_finish` **只取 `sorted(arguments)` 记录参数键名** | 不是转交数据库的参数，也不应把参数值写进审计 |

`lambda` 的价值是**延迟执行**：如果断路器已经打开，`allow(name)` 会直接拒绝，闭包从未交给线程池；若先写 `operation = service.search_candidate_evidence(...)`，业务查询早就在熔断检查前发生了。四个 Tool 使用相同模式，使可靠性机制与业务逻辑解耦。该闭包依赖每次调用的 `runtime.context`，不应跨租户缓存重用。

#### 4.1.2 构造器把“策略和基础设施”注入，而非写进 Service

```python
executor = ToolExecutor(
    policy=ToolPolicy(max_attempts=2, timeout_seconds=3.0, backoff_seconds=0.05),
    tool_policies={"search_candidate_evidence": ToolPolicy(2, 5.0, 0.1)},
    circuit_breaker=CircuitBreaker(failure_threshold=3, recovery_seconds=30),
    audit_sink=SqlAuditSink(session_factory),
)
```

真实构造逻辑：`self.policy = policy or ToolPolicy()`；`self.tool_policies = tool_policies or {}`；`self.circuit_breaker = circuit_breaker or CircuitBreaker()`；`self.audit_sink = audit_sink or InMemoryAuditSink()`。`tool_policies` 是按**完全一致的工具名称**覆盖默认 `ToolPolicy`：证据链长可给更大单次预算，本地档案数据库查询可给更小预算。`ToolPolicy` 是不可变数据类（`frozen=True`），但现有代码**没有主动校验** `max_attempts >= 1`、超时是否为正或总 Run 预算；配置治理是生产补强点。`SqlAuditSink` 与 `InMemoryAuditSink` 只是不同的写审计实现，执行器只依赖 `write(record)` 方法。

#### 4.1.3 对照完整的 `execute()`：控制流不遗漏最后一次失败

```python
def execute(self, name: str, operation: Callable[[], Any], *,
            context: TalentToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    call_id = str(uuid4())
    started = time.monotonic()
    policy = self.tool_policies.get(name, self.policy)
    if not self.circuit_breaker.allow(name):
        return self._finish(
            name, call_id, context, arguments, started, 0, "blocked",
            error={"code": "circuit_open", "message": "依赖服务暂时不可用", "retryable": True},
            degraded=True,
        )

    last_error: Exception | None = None
    for attempt in range(1, policy.max_attempts + 1):
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(operation)
            data = future.result(timeout=policy.timeout_seconds)
            self.circuit_breaker.succeed(name)
            return self._finish(name, call_id, context, arguments, started,
                                attempt, "succeeded", data=data)
        except (TransientToolError, FutureTimeoutError) as exc:
            last_error = exc
            if attempt < policy.max_attempts and policy.backoff_seconds:
                time.sleep(policy.backoff_seconds * (2 ** (attempt - 1)))
        except (ValueError, PermissionError) as exc:
            code = "permission_denied" if isinstance(exc, PermissionError) else "invalid_argument"
            return self._finish(
                name, call_id, context, arguments, started, attempt, "failed",
                error={"code": code, "message": str(exc), "retryable": False},
            )
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    self.circuit_breaker.fail(name)
    message = ("依赖服务调用超时" if isinstance(last_error, FutureTimeoutError)
               else "依赖服务暂时不可用")
    return self._finish(
        name, call_id, context, arguments, started, policy.max_attempts, "failed",
        error={"code": "dependency_unavailable", "message": message, "retryable": True},
        degraded=True,
    )
```

**按执行顺序把每一段读透：**

1. `call_id = str(uuid4())`：**一次 Tool 调用一个编号**；这次调用内部重试 1～N 次不会产生 N 个 call ID。`started = time.monotonic()` 在熔断检查前取值，供最终审计计算整个调用的耗时；`policy = ...get(name, self.policy)` 选择工具专属策略，没有则回退默认策略。
2. `circuit_breaker.allow(name)`：与 4.3 节的 `allow` 相连。未放行时马上 `_finish(..., attempts=0, status="blocked", error.code="circuit_open", degraded=True)`。此时**不创建线程池、不会调用 Service、没有数据库查询**，但依然有 `call_id`、返回协议和一条审计。
3. `last_error=None`：保存最后一次**瞬时依赖失败**；循环 `range(1, max_attempts + 1)` 包含首次尝试。默认 `max_attempts=2` 的 `attempt` 是 1、2；不是“先试 1 次，再重试 2 次”。
4. `ThreadPoolExecutor(max_workers=1)` 与 `pool.submit(operation)`：为**每次 attempt** 新建单线程池、提交业务闭包，返回 `Future`。此处真正执行 Service。线程池提供等待超时能力，不自动给数据库、网络或模型调用提供强制取消能力。
5. `future.result(timeout=policy.timeout_seconds)`：当前线程最多等这一轮给定秒数；正常返回的 `data` 可以是列表、字典、空列表等，空列表也是**成功得到的业务结果**。执行过程中抛出的异常会在 `result()` 处重新向调用者抛出。成功后调用 `breaker.succeed(name)` 清零该工具连续失败，再 `_finish(..., status="succeeded", attempts=attempt, data=data)`；首次成功计 1，第二次成功计 2。
6. `except (TransientToolError, FutureTimeoutError)`：只有代码显式标记的短暂故障和等待超时会进入重试。记录 `last_error`，尚有次数才按 `backoff_seconds * 2 ** (attempt - 1)` 退避；第一轮后的等待是基数，下一轮后的等待翻倍。没有次数时**先退出循环**，之后才调用一次 `breaker.fail(name)`。最后一次是超时，消息“依赖服务调用超时”；最后一次是 `TransientToolError`，消息“依赖服务暂时不可用”，但对外编码相同，都是 `dependency_unavailable`。
7. `except (ValueError, PermissionError)`：分别归一为 `invalid_argument` 和 `permission_denied`，`retryable=False`、`degraded=False`（默认）；这次调用直接以 `failed` 结束，**不继续循环、不调用 `breaker.fail`**。例如 Service 缺少可信租户抛 `PermissionError`；证据 Pack 版本不为 2.0 抛 `ValueError`。Schema 的 Pydantic 输入校验发生在 Tool 调用 `execute` **之前**，其异常不是这里的 `ValueError` 分支。
8. `finally: pool.shutdown(wait=False, cancel_futures=True)`：无论前面成功、失败还是 `return`，都释放这一轮线程池的提交入口；`wait=False` 不等待已运行任务结束，`cancel_futures=True` 只能取消尚未运行的任务，**不会杀死正在执行的慢查询**。所以调用方虽收到超时，旧任务仍可能在后台继续执行；重试可能与旧任务重叠。不能把这当成底层依赖的硬超时。
9. 如果每轮都发生瞬时故障，循环后 `breaker.fail(name)` **仅一次**，再 `_finish(..., attempts=max_attempts, status="failed", error.code="dependency_unavailable", degraded=True)`。断路器阈值计算的是这种**最终失败的 Tool 调用**，不是每轮 attempt。与步骤 2、5、7 一起，构成全部**预期**终态。

**异常归类为什么放在执行器？**如果把一个网络闪断误映射为 `ValueError`，会被当作参数错误直接结束；若把真正的权限拒绝误映射为 `TransientToolError`，就会重复访问无权限资源并影响熔断计数。Service/Provider 的边界应把“值得重试的下游错误”显式转成 `TransientToolError`；本执行器没有 `except Exception` 兜底，因此未知异常并不会自动得到稳定的 `{ok,data,error,meta}` 或本层审计，不能在讲义中声称“任何异常都被治理”。

**可靠性预算如何想？**忽略线程创建与调度，最坏等待量级约为 `max_attempts × timeout_seconds + Σ(backoff_seconds × 2 ** (attempt-1))`（只对非末次累加退避）；另加审计写入耗时。默认两次尝试、每次 3 秒、退避 0.05 秒，理论等待约 6.05 秒，不是 3 秒。这个上限只描述**调用方等待**，不覆盖超时线程的后台占用；要小于上层 Run 的总超时预算，并根据依赖 SLA 和工具类型分别配置。第 4.2 节继续解释策略参数，第 4.3 节继续解释断路器实现。

**现有测试如何对照阅读？**`backend/tests/test_talent_tools.py::test_executor_retries_transient_error_and_records_audit` 验证两次瞬时失败、第三次成功与一次审计；`test_each_tool_can_override_default_policy` 验证同一执行器不同工具的尝试次数；`test_timeout_returns_without_waiting_for_slow_operation` 验证调用方可及时拿到超时结果；第 7.7 节给出连续失败直至熔断的跨调用测试。

**为什么已有 LangChain 仍需要这一层？**LangChain/LangGraph 提供 Tool Schema、ToolNode、Runtime 注入与执行编排，但不会自动决定企业的租户约束、不同工具的 SLA、故障分类、熔断状态和隐私审计字段。这里用一个执行器让四个 Tool 共享可靠性与返回协议，避免各写一份 `try/except` 漂移；上层若再无预算地重试整个 Tool，可能把与本层重试叠加放大。

### 4.2 超时与重试：带具体代码、数字和预算

真实定义在 `backend/app/talent_tools.py::ToolPolicy`：

```python
@dataclass(frozen=True)
class ToolPolicy:
    max_attempts: int = 2       # 包含首次：最多 1 次重试
    timeout_seconds: float = 3.0  # 每次 future.result 的等待上限
    backoff_seconds: float = 0.05  # 两次尝试之间等待基数
```

不设置超时，慢依赖可能长时间阻塞请求工作者；不允许有限重试，一次网络闪断就让整轮任务失败。执行器按 `for attempt in range(1, policy.max_attempts + 1)` 运行，每次 `ThreadPoolExecutor(max_workers=1)` 提交 operation；`future.result(timeout=...)` 抛 `FutureTimeoutError` 时与 `TransientToolError` 一样进入下一轮。第 `attempt` 次失败后（且还可以重试），等待 `backoff_seconds * 2 ** (attempt - 1)`：尝试 1 后 0.05 秒，尝试 2 后 0.1 秒，以此类推；**默认两次总共只等待一次 0.05 秒**。成功调用 `_finish(..., status='succeeded', attempts=attempt, data=data)`；失败用尽调用 `breaker.fail(name)`、`_finish(..., status='failed', attempts=policy.max_attempts, error.code='dependency_unavailable', degraded=True)`。`ValueError`（不合法业务参数 / 不兼容证据版本）和 `PermissionError`（缺可信租户或权限）走直接失败分支，不能靠重试修复。

两条可自己动手画出的时间线：

```text
瞬时故障：尝试 1 抛 TransientToolError → 退避 0.05s → 尝试 2 成功 → ok=true / attempts=2 / 一条 succeeded 审计
持续慢调用：尝试 1 等待超时 → 退避 0.05s → 尝试 2 等待超时 → dependency_unavailable / degraded=true / 一条 failed 审计
```

**如何设值？**粗略“等待上界”估算 `N × T + Σ(backoff × 2^(i−1)) + 线程调度/网络/审计等开销`，默认 `2 × 3 + 0.05 = 6.05 秒 + 开销`。证据检索依赖 PostgreSQL、向量库、嵌入/模型服务，可能需要更大 `T`；本地档案 SQL 应更短。选择值时先看每个依赖的 SLA、95/99 分位延迟，再保证总预算小于上层 Run 的剩余 deadline，且考虑同一 Run 可能**串行调用多个工具**。示意而非当前默认值：

```python
tool_policies = {
    "search_candidate_evidence": ToolPolicy(max_attempts=2, timeout_seconds=5, backoff_seconds=0.1),
    "get_candidate_profiles": ToolPolicy(max_attempts=2, timeout_seconds=1, backoff_seconds=0.05),
}
```

此处的上界仅是**调用者等待时长**的近似，不是依赖真正停止执行的上界：`pool.shutdown(wait=False, cancel_futures=True)` 无法终止已运行任务，首轮超时后第二轮可能与仍在运行的首轮重叠。生产环境须叠加数据库 `statement_timeout`、HTTP/SDK 读超时、取消机制、连接池/线程容量、全链路 deadline 和幂等性控制；只读工具相对适合重试，写入 Tool 不能直接沿用这个 Policy。测试见 `backend/tests/test_talent_tools.py::test_executor_retries_transient_error_and_records_audit`、`test_each_tool_can_override_default_policy`、`test_timeout_returns_without_waiting_for_slow_operation`。

### 4.3 熔断与降级：把 `CircuitBreaker` 当成“跨调用的故障闸门”读懂

**它解决的是另一种问题。**重试只处理**同一次工具调用内部**偶发超时；如果证据检索依赖持续故障，每个新请求都重试两次，就会把本来已拥塞的 Milvus/模型服务打得更满。熔断器记住**前几次工具调用的最终结果**，一段时间内直接拒绝后续调用，避免故障放大。它不替代 ToolPolicy 的单次超时，也不负责实际查询或写审计。

#### 4.3.1 源码中的类：构造、状态和锁

下面是 `backend/app/talent_tools.py` 中的完整逻辑（省略源码的说明性注释，保留所有会影响行为的语句）：

```python
class CircuitBreaker:
    def __init__(self, *, failure_threshold: int = 3,
                 recovery_seconds: float = 30) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self._failures: dict[str, int] = {}
        self._opened_at: dict[str, float] = {}
        self._lock = Lock()

    def allow(self, name: str) -> bool:
        with self._lock:
            opened_at = self._opened_at.get(name)
            if opened_at is None:
                return True
            if time.monotonic() - opened_at >= self.recovery_seconds:
                self._failures[name] = 0
                self._opened_at.pop(name, None)
                return True
            return False

    def succeed(self, name: str) -> None:
        with self._lock:
            self._failures[name] = 0
            self._opened_at.pop(name, None)

    def fail(self, name: str) -> None:
        with self._lock:
            failures = self._failures.get(name, 0) + 1
            self._failures[name] = failures
            if failures >= self.failure_threshold:
                self._opened_at[name] = time.monotonic()
```

- `failure_threshold=3`：**连续失败的工具调用次数**阈值，不是线程数、候选人数，也不是单次调用内部的 attempt 数。`ToolExecutor` 只在所有瞬时错误重试耗尽后调用一次 `fail(name)`。
- `recovery_seconds=30`：熔断打开后的拒绝窗口（秒）。不是单次查询超时；单次查询超时由 `ToolPolicy.timeout_seconds` 控制。
- `_failures`：如 `{"search_candidate_evidence": 2}`，记录该工具目前累计多少次最终依赖失败；`succeed` 或恢复放行会清零。
- `_opened_at`：只为已打开的工具保存打开时的单调时钟读数；没有该键，就视为可通行。`time.monotonic()` 测的是流逝时间，避免系统校时造成用墙上时钟计算恢复窗口的跳变；该数值不是可存入审计的日期时间。
- `_lock`：多个请求并发读写字典时，把每个方法**内部**的检查和更新放在一个临界区；它不是给数据库操作加锁，也没有把一次请求的 `allow → 执行 → succeed/fail` 全过程串行化。
- `name` 是隔离键：档案 Tool 的故障默认不会打开证据 Tool 的断路器；反过来，多个 Tool 共用同一下游时也**不会自动联动熔断**。状态位于此进程中的此 `CircuitBreaker` 实例；换进程、换实例没有共享计数。

#### 4.3.2 三个方法逐步执行：谁调用、改什么、返回什么

| 方法 | 调用时机与输入 | 逐句看状态变化 | 输出与业务意义 |
|---|---|---|---|
| `allow(name)` | `ToolExecutor.execute` **进入重试循环前**只调用一次 | 读 `_opened_at[name]`；未打开→直接允许。已打开且未满恢复窗口→拒绝、**不清零**。已满窗口→`_failures[name]=0`、移除打开时间再允许 | `True` 可尝试调用依赖，`False` 立刻走 `_finish(blocked)`；该方法不负责 sleep、业务降级或审计 |
| `succeed(name)` | 任意一次 attempt 成功拿到 `future.result` 后 | 把对应失败次数清零、移除打开时间 | 无返回值；意味着连续失败被一次成功打断。`ToolExecutor` 随后 `_finish(succeeded)` |
| `fail(name)` | 该调用的所有 attempts 均以 `TransientToolError` / `FutureTimeoutError` 结束后 | 对对应工具的失败调用数加一；`>= failure_threshold` 时写 `_opened_at[name]=time.monotonic()` | 无返回值；`ToolExecutor` 随后 `_finish(failed, dependency_unavailable)`。**打开断路器的是这次失败调用，阻断从下一个调用开始** |

注意 `fail` 在阈值已达到时再次被执行，会**刷新打开时间**；例如并发请求先前都通过 `allow`，其中一个已打开熔断，另一个稍后失败仍可刷新窗口。`succeed` 也可能被先前已放行的并发请求稍后调用而清掉打开状态。不能把它说成严格串行、只有一个试探者的状态机。

#### 4.3.3 用实际代码追踪状态与返回值

`execute` 的关键调用顺序（`backend/app/talent_tools.py::ToolExecutor.execute`）是：

```text
新的一次 Tool 调用：生成 call_id，选择默认/工具专属 ToolPolicy
    │
    ├─ circuit_breaker.allow(name) == False
    │    └─ _finish(status="blocked", attempts=0,
    │              error.code="circuit_open", degraded=True) → 审计一条；不 submit operation
    │
    └─ allow == True → attempt = 1..max_attempts
         ├─ future.result(timeout=timeout_seconds) 成功
         │    └─ circuit_breaker.succeed(name) → _finish(succeeded, attempts=当前次数)
         ├─ TransientToolError / FutureTimeoutError 且尚有次数
         │    └─ 退避后再试；此时尚不调用 breaker.fail
         ├─ ValueError / PermissionError
         │    └─ _finish(failed, invalid_argument/permission_denied)，不重试、不调用 breaker.fail
         └─ 瞬时错误用尽所有尝试
              └─ circuit_breaker.fail(name) **只执行一次**
                   → _finish(failed, dependency_unavailable, attempts=max_attempts,
                             degraded=True) → 审计一条
```

因此当 `failure_threshold=3, max_attempts=2`：第 1、2、3 个**失败的工具调用**最多各执行 2 次依赖操作，连续 3 个最终失败后开闸；第 4 个调用被拦截、`attempts=0`，不是“第 3 次 attempt 就熔断”。中间只要一次调用成功，`succeed` 让连续计数重新从 0 开始。`ValueError` 和 `PermissionError` 不应让依赖被误判为不健康。

#### 4.3.4 “熔断”“降级”“恢复”三词不要混用

| 概念 | 本项目已经做了什么 | 没有做什么 |
|---|---|---|
| 熔断 | `allow=False` 时阻断下游调用，返回 `circuit_open`，审计 `blocked/attempts=0` | 不会自动切换数据库或让 ToolNode 重试探测 |
| 降级 | `_finish(..., degraded=True)` 表示这次未能提供可信业务结果；超时用尽重试返回 `dependency_unavailable`，被熔断则返回 `circuit_open` | **没有**缓存、备用证据、部分候选人数据或伪造的空 Evidence Pack；`data=None` 与“查询成功但没有命中”不同 |
| 恢复 | 下一次 `allow` 发现 `monotonic()-opened_at>=recovery_seconds` 后清零并放行；其后的成功会保持健康，继续失败则重新累计 | **没有严格 half-open 单探针**：窗口到期时多个并发请求可以同时得到允许 |

返回的 `error.retryable=True` 是给调用方的错误分类，不表示熔断窗口内马上重新打下游；上层若不管预算持续重试，只会得到 `circuit_open`。当前超时采用线程池 `future.result(timeout=...)`，`shutdown(wait=False, cancel_futures=True)` 不能中止**已经运行**的慢线程，因此开放状态能挡住*后续*调用，却不能取消*已开始*的操作；连续超时可能留下重叠的工作。真正的依赖取消、跨实例共享熔断、探针限流需另行设计，不能声称现有实现具备。

**参数如何选？**调低 `failure_threshold` 保护更快但偶发故障更容易误熔断；调高则更多失败流量进入故障依赖。调短 `recovery_seconds` 更快重探，但持续故障时压力更高；调长可保护下游却延迟恢复。依据各下游 SLA、请求并发和上层 Run 超时预算选择，不要把 `recovery_seconds` 当作单次调用超时；生产若需要数据库与 Milvus 各自独立健康语义，应考虑按**依赖**而非只按 Tool 名称分组，并验证多进程场景。

**老师想你讲清的设计亮点**：重试是“**请求内**吸收短故障”，熔断是“**请求间**隔离持续故障”，降级是“**稳定地告诉上层暂时没有可信结果**”，审计是“证明谁被阻断或失败”。这四件事不属于业务 Service，而由 `ToolExecutor + CircuitBreaker + _finish` 协作完成。

### 4.4 统一出口 `ToolExecutor._finish()`：返回协议与审计为什么必须一起看

`execute` 决定走哪条分支；`_finish` 把分支的终态转成同一份可供 Agent 消费的结构，同时写可追踪的调用摘要。**两者不等价**：`execute` 负责调度/判断/错误分类，`_finish` 负责最后的结构化结果和审计，绝不替 Service 查询业务事实。源码 `backend/app/talent_tools.py`：

```python
def _finish(
    self, name, call_id, context, arguments, started, attempts, status,
    *, data=None, error=None, degraded=False,
) -> dict[str, Any]:
    meta = {
        "tool_name": name,
        "call_id": call_id,
        "attempts": attempts,
        "degraded": degraded,
    }
    self.audit_sink.write(
        {
            "call_id": call_id,
            "tool_name": name,
            "tenant_id": context.tenant_id,
            "actor_id": context.actor_id,
            "run_id": context.run_id,
            "argument_keys": sorted(arguments),
            "status": status,
            "attempts": attempts,
            "duration_ms": round((time.monotonic() - started) * 1000, 2),
            "error_code": error["code"] if error else None,
        }
    )
    return {"ok": error is None, "data": data, "error": error, "meta": meta}
```

#### 4.4.1 先认识入参：`_finish` 不自己重新尝试

| 参数 | 从 `execute` 哪里来 | 具体意义 |
|---|---|---|
| `name/call_id/context/arguments/started` | 调用开始时就确定的五项 | 同一次调用中的每条预期终态共用；`call_id` 将响应和审计关联，`started` 用于耗时 |
| `attempts` | 由分支决定：成功/业务错误取当前 `attempt`，重试耗尽取 `policy.max_attempts`，熔断阻断为 **0** | 代表**本次调用实际尝试数**，不是数据库返回条数，也不是断路器连续失败次数 |
| `status` | 执行器传入 `succeeded / failed / blocked` | **审计终态**；当前返回结构中没有单独的 `status` 字段 |
| `data` | 成功时由 Service 返回；失败时默认 `None` | 可以是空列表（查询成功但无候选人），与 `None`（未获得可信业务结果）语义不同 |
| `error` | 执行器构造的 `{code,message,retryable}`；成功时默认 `None` | 控制 `ok` 和审计的 `error_code`；参数、权限、依赖、熔断各有稳定编码 |
| `degraded` | 被熔断或瞬时错误用尽尝试时传 `True`，其余用默认 `False` | **是否未能提供正常业务结果**的元数据；不是备用数据是否已生成 |

这也是 `_finish` 为何用关键字参数 `*, data=None, error=None, degraded=False`：调用者需明确交代“成功带数据”还是“失败带错误”；`status` 和 `attempts` 由 `execute` 的当前分支计算，`_finish` **不会**再去重试或调用 `CircuitBreaker`。生产可用更严格的结果类型与状态校验，但当前实现只是按入参封装。

#### 4.4.2 逐句看构建、写审计、返回的先后顺序

**第一段，创建 `meta`：**`tool_name` 告诉模型是哪一个工具；`call_id` 是本次 Tool 调用唯一编号，用它在返回与审计表间关联；`attempts` 可以辨别首轮成功、重试后成功、或直接被熔断；`degraded` 告知上层没有正常业务结果。返回中的 `meta` **没有** `duration_ms`，这个字段仅在审计记录中。

**第二段，创建审计摘要并调用 `self.audit_sink.write(record)`：**

- `tenant_id/actor_id/run_id` 来自可信 `context`。它们回答“哪个租户、谁、在什么 Run 中”调用；`_finish` 只记录，不负责从模型参数里推断身份。
- `argument_keys = sorted(arguments)`：字典的默认迭代得到**键名**，`sorted` 让顺序稳定。例如 `arguments={"query":"材料原文", "candidate_ids":["C001"]}` 只留下 `["candidate_ids","query"]`，不存材料原文、候选人 ID、履历或证据正文。`arguments` 不是“空值过滤”，而是只记录**出现了哪些参数名**。
- `duration_ms = round((time.monotonic() - started) * 1000, 2)`：从 `execute` 开始到写审计前的时间，包含熔断检查、单轮等待、重试退避等；这是**整次 Tool 调用耗时**，不是每次 attempt 或后端 SQL 耗时。由于在 `write` 前计算，它**不包含随后审计存储自身的耗时**；超时后后台线程的余下运行时间也不计入。
- `error_code = error["code"] if error else None`：只存稳定编码（如 `dependency_unavailable`），不把后端错误堆栈或业务查询写入审计。成功的 `error` 为 `None`，`error_code` 也为 `None`。
- `status/attempts/call_id/tool_name` 与结果中的 `meta/error` 能做对账：`status` 告诉审计系统成功、失败还是熔断拒绝；`error_code` 可和外部返回的 `error.code` 对比。

**第三段，返回统一对象：**`{"ok": error is None, "data": data, "error": error, "meta": meta}`。`ok` **取决于 `error is None`，不是取决于 `status` 或 `data` 是否为空**。例如 Service 返回 `[]` 时仍为 `ok=True, data=[]`；连接失败时 `ok=False, data=None`。这是工具外层调用协议，和第 9 课 `data.evidence_packs[*].schema_version='2.0'` 的**证据正文协议**属于不同层次；`_finish` 不进行事实合并、引用校验，也不验证 Evidence Pack 版本，版本检查在 `TalentToolService.search_candidate_evidence` 中。

**顺序为什么关键？**源码**先写审计，再返回结果**。在成功与预期失败路径，`execute` 各调用一次 `_finish`，故一次 Tool 调用最多写一条**最终结果**审计；不是每次 attempt 写一条。但若 `audit_sink.write` 抛错，后面的 `return` 根本到不了，Tool 调用可能向外抛出审计异常，而不是原先准备返回的结构。当前没有“审计失败隔离”或保证审计成功的事务兜底，生产要明确要求是 fail-closed（审计失败不交付结果）、fail-open（结果优先但另外告警）还是可靠消息队列/补偿，不可说“审计永不影响工具返回”。

#### 4.4.3 `audit_sink.write` 最后调用到哪里？

`backend/app/talent_tools.py` 给出两个实现：

```python
class InMemoryAuditSink:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def write(self, record: dict[str, Any]) -> None:
        self.records.append(record)


class SqlAuditSink:
    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self.session_factory = session_factory

    def write(self, record: dict[str, Any]) -> None:
        with self.session_factory() as db:
            db.add(ToolCallAudit(**record))
            db.commit()
```

- 内存 Sink 用于测试，可用 `audit.records[-1]` 断言，无数据库持久化；**每个实例**有自己的 `records`。
- SQL Sink 新建自己的 SQLAlchemy Session，`ToolCallAudit(**record)` 将键名映射到 `backend/app/models.py::ToolCallAudit`，`db.add` 加入 Session，`db.commit` 真正提交到 `tool_call_audits`。对应迁移 `database/migrations/012_talent_tools.sql`：包含 `call_id` 唯一约束、租户/Run 索引、`argument_keys` 的 JSONB 字段、`attempts >= 0`、`duration_ms >= 0` 等约束；本地 SQLite 测试通过 ORM 的 JSON 类型建表，不能把测试库当成 PostgreSQL 迁移完全等价。
- 这与 Service 的业务 SQL Session **不是同一事务**：业务查询先发生，`_finish` 后写审计；一旦审计 DB 故障不会自动“撤销查询”，审计也不会自动把未经分类的 Service 异常补记。若审计准确性是监管要求，需要更明确的交付/回执策略与数据库可用性设计。

#### 4.4.4 四个终态样例：同一协议、不同语义

以 `get_candidate_profiles` 为例，`arguments={"candidate_ids": ["C001"]}`；四行是**相互独立的调用情景**，各自会生成不同的 UUID `call_id`。下表省略每行的 `meta.tool_name/meta.call_id`；该 Tool 的成功数据为档案列表，也允许因无匹配记录而返回空列表。

| 场景 | 返回 `ok/data/error/meta`（摘要） | 唯一审计记录（摘要） | 为什么 |
|---|---|---|---|
| 首次查询成功，即使结果为空 | `True / [] / None / attempts=1,degraded=False` | `succeeded / 1 / error_code=None` | 空命中不是错误；`breaker.succeed` 清零 |
| 首次瞬时错误，第二次成功 | `True / <真实数据> / None / attempts=2,degraded=False` | `succeeded / 2 / None` | 重试过程不另写失败审计，最终只写成功 |
| 两次等待超时，重试耗尽 | `False / None / dependency_unavailable(retryable=True) / attempts=2,degraded=True` | `failed / 2 / dependency_unavailable` | `breaker.fail` 一次，`_finish` 记录最终失败 |
| 熔断窗口内直接拒绝 | `False / None / circuit_open(retryable=True) / attempts=0,degraded=True` | `blocked / 0 / circuit_open` | 不访问依赖，也不能把阻断伪装成“没有命中” |

还有一类要辨认：`PermissionError` 或 `ValueError` 直接返回 `failed / attempts=当前次数` 与 `permission_denied / invalid_argument`，`retryable=False, degraded=False`；不会调用 `breaker.fail`。这里的 `error.message` 是对应异常的 `str(exc)`；当前并未统一做敏感信息脱敏，生产环境若底层异常可能包含查询片段，应在边界安全映射后再交执行器。

#### 4.4.5 回到四个 Tool 的完整链路与边界

```text
模型业务参数 → @tool/Pydantic Schema 校验
  → ToolNode 注入 ToolRuntime[TalentToolContext]
  → Tool 包装器：execute(name, lambda: service(..., context), context, arguments)
  → execute：生成 call_id/started、选 Policy、allow 检查、限时执行/异常归类/重试
  → service：可信 Context 校验，SQL 强制 tenant_id/active；证据检索传入范围和权限
  → execute：succeed 或 fail / blocked，选择最终终态
  → _finish：构造 meta → AuditSink.write(摘要) → {ok,data,error,meta}
  → ToolNode/主图消费结果；call_id 可对应 tool_call_audits.call_id
```

**别把所有情况都画进统一返回：**(1) Pydantic Schema 在 `execute` 之前拒绝 101 个候选人 ID，执行器及本层审计都不运行；(2) 第 11 课主图的 `build_candidate_provider` **直接调 Service**，不经过 `ToolExecutor`，所以主图 Provider 路径不会自动有这份审计；(3) 当前未捕获的一般异常、`audit_sink.write` 异常、线程池提交异常等，不保证返回上述稳定结构。若业务规定“每个拒绝调用都需审计”，需要在 ToolNode/网关的输入边界和主图节点补足相应记录，而不能只修改 `_finish`。

**这一设计为什么是课程亮点？**`execute` 的分支都汇合到 `_finish`，实现“**统一错误语义 + 最小化审计摘要 + 同一 call_id 串联响应与记录**”；同时不把 SQL、Candidate Evidence Pack 的加工算法移入执行器。真正讲懂它，不是背 `_finish` 四个返回键，而是能根据任意异常和断路器状态，算出`operation` 被调用几次、是否继续查库、`attempts` 为多少、返回与审计各是什么。

### 4.5 如何验证学会了：从断言倒推设计

1. **Schema/身份**：查看 `tool_call_schema` 中是否无 `runtime`、`tenant_id`；合法调用经 `ToolNode` 注入 Context；101 个 ID 被 Schema 拒绝，数据库查询不发生。
2. **租户/状态**：当前租户 active JD（含不同版本）可返回，inactive 与其他租户不得返回；候选人 SQL/档案也隔离租户、状态。
3. **证据协议**：Stub Provider 记录收到的 `tenant_id/scopes/candidate_ids/include_evidence_pack`，返回 Pack 2.0；测试错版被拒；真正连接检索时再测版本和引用权限。
4. **可靠性**：第一次瞬时错误第二次成功则 `attempts=2`；慢依赖超过 `timeout_seconds` 直至次数耗尽，结果和审计一致；连续失败触发熔断，新调用 `attempts=0`。
5. **主图**：测试中置入不同租户候选人，使用 `build_candidate_provider()` 替换固定 Provider；`graph.invoke()` 返回实际 SQL 的编号而非测试里写死的编号。图的 `evaluate` 当前仍是占位，验证不要超出实现范围。

**从请求入口到结果的两条最终链路：**ToolNode/模型参数 → `@tool` Schema → 注入 `ToolRuntime` → `ToolExecutor` → Service → SQL / 证据 Provider → `_finish()`/审计；主图 `graph.invoke(..., context=DecisionContext(...))` → `prepare_request` → `retrieve_candidates` → 适配 Provider 闭包 → `compile_filters` → Service/SQL → 状态 `candidate_ids` → 分支。两条路径调用了相同的业务筛选 Service，但**目前只有 Tool 路径自动经过 ToolExecutor**。

---

### 4.6 本课最值得讲给老师听的五个关键设计亮点

1. **模型可见 Schema 与可信 Runtime 双轨输入**：`query/filters/candidate_ids` 给模型表达业务意图，`tenant_id/scopes/actor/run` 从框架可信上下文注入；通过 `tool_call_schema` 与 SQL 租户条件分别证明“看不见”和“查不到”。亮点是安全边界落实到**参数和数据库表达式**，不是提示词。
2. **受限 DSL 复用而非模型拼 SQL**：`FilterCondition` 只允许注册字段/操作符；`QueryPlan` 承载结构化与语义两类条件；`build_candidate_statement` 强制租户和在职条件。亮点是第 8 课已有能力直接变成本课 Tool 的内核。
3. **双协议分层**：ToolExecutor 的 `{ok,data,error,meta}` 保证每个 Tool 同一种调用语义；第 9 课 `evidence_packs[*].schema_version='2.0'` 保证**证据内容**语义。两者版本、错误和职责不混淆，错误结果依然有 `call_id` 可定位。
4. **`execute` 分支治理与 `_finish` 单一出口**：`execute` 在 `allow → submit/result → 分类/重试 → succeed/fail` 间按异常类型决策；`_finish` 将最终分支转成统一返回与最小化审计，并用同一 `call_id` 串联。亮点是重试仅在请求内、熔断跨请求、失败与审计可对账；也要能说明已运行的超时线程无法强制取消、未知异常和审计写失败未被兜底。
5. **主图 Provider 是稳定接口而非固定数据源**：第 11 课图只依赖 `(request, DecisionContext)->list[str]`；第 12 课闭包注入 Service 和编译器，把 Context 转换、SQL 筛选封装在图外。换来源不必改图拓扑；但 Provider 直接调用 Service 时不能声称继承 ToolExecutor 审计。

**老师想检查的不是能不能背四个函数名，而是能否解释每一个“不应该由模型决定”的边界在哪里生效、每一种错误何时被捕获、数据从哪一张表或哪一级证据模块来，以及怎样用测试证明这些话是真的。**下面四道作业分别检验：数据正确性和租户隔离；Schema 前置约束；失败治理与审计一致性；图适配与数据库真实结果。

### 4.7 交作业前的总操作路线（先读，再改，再测）

| 作业 | 先打开什么 | 实际需要做什么 | 最硬的验收断言 | 老师在考什么 |
|---|---|---|---|---|
| 一 JD 状态与版本 | `backend/app/models.py::JobDescription`、`backend/app/talent_tools.py::lookup_job_descriptions`、`backend/tests/test_talent_tools.py::session_factory` | 保留现有 fixture，新增**不重复编码**的同名有效版本和停用版本；写正反断言 | 有效不同版本均有 `job_code/version`；停用和其他租户没有 | SQL 权限/状态硬过滤与记录身份 |
| 二 100/101 上限 | `build_talent_tools::get_candidate_profiles`、`test_tool_schema_hides_runtime_context` | 调取 `tool_call_schema`，分别验证 100 可过、101 抛 `ValidationError`；用禁止访问的 Session 工厂证明校验阶段未查库 | 101 项不能通过 Schema，Session 调用计数为 0 | 参数必须先于 Service 被拒绝 |
| 三 超时+审计 | `ToolPolicy`、`ToolExecutor.execute/_finish`、`InMemoryAuditSink` | 注入短超时与慢操作，检查 Tool 结果和同一条审计；可追加真正 ToolNode 调用 | `dependency_unavailable`、`degraded=true`、`failed/attempts=2/error_code` 同步 | 可靠性策略与可追溯失败是一套协议 |
| 四 主图 Provider | `build_candidate_provider`、`_candidate_node`、`verify_talent_decision_graph.py` | 在数据库补停用行；把验证脚本固定 Provider 换成闭包 Provider；测试按租户执行主图 | `candidate_ids` 与同租户 active SQL 结果相等 | 第 11 课图与第 8 课 SQL 的稳定适配 |

**建议每题按同一顺序操作：**(1) 先定位“入口、受信任上下文、查询/执行、返回”四处代码；(2) 写该题失败/成功场景的测试；(3) 运行该题的目标测试观察结果；(4) 仅当测试揭示缺口时修改实现，避免重写已经正确的逻辑；(5) 运行 `python -m pytest -q tests/test_talent_tools.py tests/test_talent_decision_graph.py` 做回归。命令应在 `backend` 目录执行。作业四还应运行 `python scripts/verify_talent_decision_graph.py` 观察主图执行结果（脚本需先按作业说明接数据库）。
以下进入四道作业的可执行步骤。每题都按“题目目的 → 实际代码 → 修改/测试 → 失败排查 → 验收”阅读。
## 5. 作业一：岗位 JD 状态与版本过滤

### 5.1 题目真正考什么

题目给出四类数据：

1. 当前租户的有效岗位；
2. 当前租户的停用岗位；
3. 当前租户中同名但不同版本的岗位；
4. 其他租户的同名岗位。

要求：

- 不能把停用 JD 返回给调用者；
- 不能把其他租户的 JD 返回给调用者；
- 不能因为岗位名称相同而丢掉版本信息；
- 每条结果必须能追溯到明确的 `job_code` 和 `version`。

这道题考的是三件事：

- **租户隔离必须在 SQL 条件中完成；**
- **状态过滤是服务端不变量，不能依赖模型自行记住；**
- **版本是事实身份的一部分，不能只返回 name/content。**

### 5.2 当前代码链路

入口是 Tool：

```python
@tool
def lookup_job_descriptions(query, runtime, limit=5):
    return executor.execute(
        "lookup_job_descriptions",
        lambda: service.lookup_job_descriptions(
            query,
            limit=limit,
            context=runtime.context,
        ),
        context=runtime.context,
        arguments={"query": query, "limit": limit},
    )
```

进入 Service 后，当前实现先检查 Context：

```python
self._check_context(context)
```

再规范化岗位名：

```python
normalized_query = "".join(query.lower().split())
```

再执行数据库查询：

```python
rows = db.scalars(
    select(JobDescription).where(
        JobDescription.tenant_id == context.tenant_id,
        JobDescription.status == "active",
    )
).all()
```

最后通过 `SequenceMatcher` 做简单名称匹配，并返回：

```python
{
    "job_code": row.job_code,
    "name": row.name,
    "match_score": score,
    "version": row.version,
    "content": row.content,
}
```

注意当前实现的业务含义：

> `status == "active"` 是服务端硬过滤；`tenant_id == context.tenant_id` 是租户边界；`version` 是输出字段，不应该被名称匹配逻辑吞掉。

### 5.3 第一步：在现有 fixture 中补齐四种场景（先避免唯一键冲突）

打开 `backend/tests/test_talent_tools.py::session_factory`。它**已含** `JD-AI-001`（`tenant-a`、版本 2、默认 `status='active'`），也已含其他租户 `JD-AI-OTHER`；`backend/app/models.py::JobDescription.job_code` 为全局唯一。**不要再插入一条同样叫 `JD-AI-001` 的数据**，否则在 `db.commit()` 就因唯一约束失败，还没测到岗位逻辑。保留已有数据，新增如下两条并为已有条目显式标注状态：

```python
# 已有 JD-AI-001：version=2、tenant-a、active，不要重复插入
# 在现有 db.add_all([...]) 中新增：
JobDescription(
    job_code="JD-AI-V1", tenant_id="tenant-a",
    name="高级 AI 应用工程师", content="历史有效版本 1",
    version=1, status="active",
),
JobDescription(
    job_code="JD-AI-OFF", tenant_id="tenant-a",
    name="高级 AI 应用工程师", content="已停用版本",
    version=3, status="inactive",
),
# 已有 JD-AI-OTHER：tenant-b、同名、active；也不重复插入
```

四类数据现已齐备：当前租户有效版本 1 + 有效版本 2、当前租户停用版本、其他租户有效版本。保留 fixture 中 Java 岗位和 C001/C002 人才资料，保证原有测试不被破坏。若坚持重新构造独立 fixture，则四个 `job_code` 必须全部唯一。

### 5.4 第二步：先加失败场景断言，再检查 Service

在同一测试文件新增下面的测试；依赖上节**修改后的 fixture**，不需要另建数据库。`"高级 AI 应用工程师"` 与同名 JD 完全匹配，两个有效结果均为 1.0 分，按 `job_code` 升序，因而断言有稳定顺序；已有 Java 岗位的低分近似命中由集合断言排除或另行检查，不要依赖“SQL 的自然顺序”。

```python
def test_job_lookup_keeps_active_versions_and_excludes_inactive_and_other_tenant(
    session_factory, context,
):
    service = TalentToolService(session_factory=session_factory)
    result = service.lookup_job_descriptions(
        "高级 AI 应用工程师", context=context, limit=10,
    )
    by_code = {item["job_code"]: item for item in result}
    assert {"JD-AI-001", "JD-AI-V1"} <= by_code.keys()
    assert "JD-AI-OFF" not in by_code
    assert "JD-AI-OTHER" not in by_code
    assert by_code["JD-AI-001"]["version"] == 2
    assert by_code["JD-AI-V1"]["version"] == 1
    assert all(item["job_code"] and isinstance(item["version"], int) for item in result)
```

再看 `backend/app/talent_tools.py::lookup_job_descriptions()` 中是否已经有 `tenant_id=context.tenant_id`、`status=='active'`、返回 `job_code/version`；当前仓库**已经具备**，所以这题重点是场景测试，除非自己分支上的代码缺失，不需要为了交作业无谓重写业务函数。先运行 `python -m pytest -q tests/test_talent_tools.py -k job_lookup`，若 fixture 提交时报唯一键错误，返回上一步检查重复编码；若停用岗位出现，检查 SQL `where` 而非只修改排序；若丢版本，检查输出字典而非 SQL 表结构。额外可用 `limit=1` 验证截断发生在排序后，但这不是题目硬要求。

### 5.5 第三步：判断是否需要改业务代码

以当前题目文字来看，已有实现已经具备两个关键过滤条件：

```python
JobDescription.tenant_id == context.tenant_id
JobDescription.status == "active"
```

同时返回了：

```python
"job_code": row.job_code
"version": row.version
```

所以本作业很可能主要是 **补齐场景数据和边界测试**，而不是盲目修改代码。

但是要区分两种需求：

#### 需求 A：返回所有匹配的有效版本

当前代码即可满足。重点是测试：同名不同版本都保留，停用和跨租户记录排除。

#### 需求 B：只返回最新有效版本

这不是当前函数签名表达的契约。不要偷偷把“只返回最新版本”塞进现有逻辑，而应该明确改变业务契约，例如增加：

```python
latest_only: Annotated[bool, Field(default=False)] = False
```

或者在 Service 内部明确实现窗口函数/分组规则，并增加专门测试。是否只返回最新版本必须是产品规则，而不是开发者猜测。

### 5.6 常见错误与定位方法

#### 错误 1：只在 Python 循环里过滤状态

```python
rows = db.scalars(select(JobDescription)).all()
for row in rows:
    if row.status == "active":
        ...
```

这会先读出其他租户和停用数据。虽然最终结果可能看似正确，但：

- 权限边界变晚了；
- 查询效率变差；
- 日后很容易在另一个分支把越权数据带出去。

正确做法是把租户和状态放在 SQL `where` 中。

#### 错误 2：以岗位名称去重

```python
unique = {row["name"]: row for row in matches}
```

这会丢掉同名不同版本。岗位名称不是记录主键。

#### 错误 3：只返回 content

只返回内容会失去审计和后续引用能力。至少要保留：

```text
job_code / name / version / content / match_score
```

---

## 6. 作业二：候选人档案批量上限

### 6.1 题目真正考什么

`get_candidate_profiles` 和 `search_candidate_evidence` 都接收候选人 ID 列表。题目要求：

- 允许 1 到 100 个；
- 第 101 个在 Tool Schema 校验阶段失败；
- 失败后不能继续执行 Service；
- 更不能打开数据库查询。

重点不是“Service 里 if len(...) > 100”，而是 **把输入边界放在模型可见的 Schema 层**。

### 6.2 当前实现

```python
@tool
def get_candidate_profiles(
    candidate_ids: Annotated[
        list[str],
        Field(min_length=1, max_length=100),
    ],
    runtime: ToolRuntime[TalentToolContext],
):
    return executor.execute(
        "get_candidate_profiles",
        lambda: service.get_candidate_profiles(
            candidate_ids,
            context=runtime.context,
        ),
        context=runtime.context,
        arguments={"candidate_ids": candidate_ids},
    )
```

这里的 `max_length=100` 作用于列表长度，不是字符串长度。

类似地，证据工具也有相同边界：

```python
candidate_ids: Annotated[list[str], Field(min_length=1, max_length=100)]
```

### 6.3 第一步：写 100 个和 101 个的 Schema 测试

推荐直接拿 Tool 的 `tool_call_schema` 做 Pydantic 校验：

```python
from pydantic import ValidationError


def test_candidate_profile_schema_accepts_100_ids_and_rejects_101(
    session_factory,
):
    service = TalentToolService(session_factory=session_factory)
    tools = build_talent_tools(service, ToolExecutor())
    profiles = next(
        item for item in tools
        if item.name == "get_candidate_profiles"
    )
    schema = profiles.tool_call_schema

    valid = schema.model_validate({
        "candidate_ids": [f"C{i:03d}" for i in range(100)]
    })
    assert len(valid.candidate_ids) == 100

    with pytest.raises(ValidationError):
        schema.model_validate({
            "candidate_ids": [f"C{i:03d}" for i in range(101)]
        })
```

这个测试验证的是纯 Schema 行为。

### 6.4 第二步：证明数据库没有被访问

如果只测试抛出了 `ValidationError`，还没有完全证明“没有继续执行数据库查询”。可以把 `session_factory` 换成一个一旦被调用就失败的函数：

```python
def test_101_candidate_ids_fail_before_service_and_database():
    calls = {"session_factory": 0}

    def forbidden_session_factory():
        calls["session_factory"] += 1
        raise AssertionError("Schema 失败后不应该打开数据库")

    service = TalentToolService(
        session_factory=forbidden_session_factory,
    )
    tools = build_talent_tools(service, ToolExecutor())
    profiles = next(
        item for item in tools
        if item.name == "get_candidate_profiles"
    )

    with pytest.raises(ValidationError):
        profiles.tool_call_schema.model_validate({
            "candidate_ids": [f"C{i:03d}" for i in range(101)]
        })

    assert calls["session_factory"] == 0
```

如果想验证完整 Tool 调用入口，可以通过 `profiles.invoke(...)` 或 `ToolNode` 执行，并在测试中断言 Service spy 没有被调用。但不同 LangChain 版本对 Tool 的异常包装形式可能不同，Schema 单测通常更稳定。

### 6.5 为什么不能只在 Service 里校验

下面这种做法不够好：

```python
def get_candidate_profiles(self, candidate_ids, *, context):
    if len(candidate_ids) > 100:
        raise ValueError("最多 100 个")
```

它当然可以作为第二道防线，但不是第一道防线。原因：

1. 模型拿不到精确的工具约束提示；
2. 参数已经进入业务执行层；
3. 未来其他调用入口可能忘记复用这个检查；
4. 可能已经创建 Session、记录查询日志或产生不必要的资源消耗。

正确的纵深防御是：

```text
Tool Schema：第一道边界，阻止非法 Tool Call
        │
        ▼
Service：第二道边界，防止绕过 Tool 直接调用
        │
        ▼
数据库权限/租户条件：第三道边界
```

### 6.6 不要漏掉空列表和元素格式

题目只强调 101 个，但完整设计还应考虑：

```python
Field(min_length=1, max_length=100)
```

这同时拒绝：

- `[]`；
- 超过 100 个；
- 非列表值。

如果业务要求每个候选人编号也有限制，可以把元素类型改成：

```python
Annotated[
    list[Annotated[str, Field(min_length=1, max_length=64)]],
    Field(min_length=1, max_length=100),
]
```

但不要为了“看起来严格”随意增加题目没有要求的规则，否则会改变既有接口契约。

---

## 7. 作业三：超时故障与审计记录

### 7.1 题目真正考什么

题目要求构造一个执行时间超过 `timeout_seconds` 的慢依赖，并验证：

- 工具返回 `dependency_unavailable`；
- 返回 `degraded=true`；
- 审计状态是失败；
- 审计保存尝试次数；
- 返回错误编码与审计错误编码一致。

它考的是：**业务代码不应该自己感知超时治理，统一执行器才是超时、重试和审计的唯一入口。**

### 7.2 当前执行器是怎样工作的

当前策略：

```python
@dataclass(frozen=True)
class ToolPolicy:
    max_attempts: int = 2
    timeout_seconds: float = 3.0
    backoff_seconds: float = 0.05
```

执行器每次尝试都会建立一个单线程池：

```python
pool = ThreadPoolExecutor(max_workers=1)
future = pool.submit(operation)
data = future.result(timeout=policy.timeout_seconds)
```

如果依赖超过超时时间：

```python
except (TransientToolError, FutureTimeoutError) as exc:
    last_error = exc
```

没有成功后，最终统一返回：

```python
error={
    "code": "dependency_unavailable",
    "message": "依赖服务调用超时",
    "retryable": True,
}
```

同时：

```python
self.circuit_breaker.fail(name)
```

并通过 `_finish()` 写审计。

### 7.3 第一步：构造慢依赖

测试中不要直接 sleep 很长时间。把超时设置得很小，把依赖设置成略长于超时即可：

```python
def test_timeout_returns_degraded_and_writes_matching_audit(context):
    audit = InMemoryAuditSink()
    executor = ToolExecutor(
        policy=ToolPolicy(
            max_attempts=2,
            timeout_seconds=0.01,
            backoff_seconds=0,
        ),
        audit_sink=audit,
    )

    def slow_dependency():
        time.sleep(0.05)
        return {"candidate_ids": ["C001"]}

    result = executor.execute(
        "search_candidate_evidence",
        slow_dependency,
        context=context,
        arguments={"query": "企业知识库经验"},
    )
```

这里 `0.05 > 0.01`，所以每一次尝试都应该超时。

### 7.4 第二步：验证返回协议

```python
    assert result["ok"] is False
    assert result["data"] is None
    assert result["error"]["code"] == "dependency_unavailable"
    assert result["error"]["retryable"] is True
    assert result["meta"]["degraded"] is True
    assert result["meta"]["attempts"] == 2
```

注意：`degraded` 表示系统已经降级返回了错误结果，不表示业务结果是“部分成功”。当前代码的超时场景没有可信业务数据，因此 `data` 应为 `None`。

### 7.5 第三步：验证审计内容

```python
    assert len(audit.records) == 1
    record = audit.records[0]

    assert record["status"] == "failed"
    assert record["attempts"] == 2
    assert record["error_code"] == result["error"]["code"]
    assert record["tool_name"] == "search_candidate_evidence"
    assert record["tenant_id"] == context.tenant_id
    assert record["actor_id"] == context.actor_id
    assert record["run_id"] == context.run_id
    assert record["argument_keys"] == ["query"]
    assert record["duration_ms"] >= 0
```

“相同的错误编码”不要只看字符串相等，还要确认两边都是最终对外编码，而不是把底层异常对象或原始错误文本直接写入审计。

### 7.6 为什么审计只保存参数名，不保存参数值

数据库迁移 `012_talent_tools.sql` 中的审计表字段是：

```sql
argument_keys JSONB NOT NULL DEFAULT '[]'::jsonb,
status VARCHAR(32) NOT NULL,
attempts INTEGER NOT NULL,
duration_ms DOUBLE PRECISION NOT NULL,
error_code VARCHAR(64)
```

它没有保存：

- 查询原文；
- 候选人姓名；
- 候选人档案；
- 证据正文；
- 证据引用内容。

这是有意的隐私和数据最小化设计。审计需要回答“谁在什么时候调用了什么工具、是否成功、尝试了几次”，而不是复制一份敏感业务数据。

### 7.7 第四步：把重试、跨调用熔断、恢复和审计写成可验证的时间线

前面的慢依赖测试主要验证“**单次调用**超时 → `dependency_unavailable` 与审计”。再设 `failure_threshold=2, recovery_seconds=60, max_attempts=2, backoff_seconds=0`，假设每次尝试都抛 `TransientToolError`：

| 调用序号 | 调用开始时 `allow` | 实际执行 `operation` 的次数 | 结束后失败调用数 | 对外错误、审计状态与 attempts |
|---|---|---:|---:|---|
| 第 1 次 | 允许 | 2 | 1 | `dependency_unavailable` / `failed` / 2 |
| 第 2 次 | 允许 | 2 | 2，记录打开时间 | `dependency_unavailable` / `failed` / 2 |
| 第 3 次（窗口内） | 拒绝 | **0** | 仍为 2 | `circuit_open` / `blocked` / 0 |
| 恢复窗口后第 4 次 | 允许且清零 | 依当时依赖状态而定 | 成功则 0；失败重新累计 | 成功返回数据；继续失败仍按规则处理 |

可以在 `backend/tests/test_talent_tools.py` 中增加如下**跨请求**测试；本文件已有 `context` fixture，顶部也已导入 `CircuitBreaker`、`ToolExecutor`、`ToolPolicy`、`TransientToolError` 与 `InMemoryAuditSink`：

```python
def test_breaker_blocks_third_call_without_hitting_dependency_and_audits(context):
    calls = {"dependency": 0}
    audit = InMemoryAuditSink()

    def unavailable():
        calls["dependency"] += 1
        raise TransientToolError("down")

    executor = ToolExecutor(
        policy=ToolPolicy(max_attempts=2, timeout_seconds=1, backoff_seconds=0),
        circuit_breaker=CircuitBreaker(failure_threshold=2, recovery_seconds=60),
        audit_sink=audit,
    )
    results = [
        executor.execute(
            "search_candidate_evidence", unavailable,
            context=context, arguments={"query": "项目经验"},
        )
        for _ in range(3)
    ]

    # 两个调用 × 每调用两次尝试；第三个调用被挡在 future.submit 前。
    assert calls["dependency"] == 4
    assert [r["error"]["code"] for r in results] == [
        "dependency_unavailable", "dependency_unavailable", "circuit_open",
    ]
    assert [r["meta"]["attempts"] for r in results] == [2, 2, 0]
    assert all(r["meta"]["degraded"] is True for r in results)
    assert [record["status"] for record in audit.records] == [
        "failed", "failed", "blocked",
    ]
    assert [record["attempts"] for record in audit.records] == [2, 2, 0]
    assert [record["error_code"] for record in audit.records] == [
        result["error"]["code"] for result in results
    ]
    assert [record["call_id"] for record in audit.records] == [
        result["meta"]["call_id"] for result in results
    ]
```

其中三次请求产生**三条**审计，而不是四次依赖尝试产生四条；`ToolExecutor._finish` 在每次工具调用的最终出口只执行一次。这个测试与本节作业要求的“慢依赖超时测试”互补：前者证明熔断防止持续故障，后者证明超时与审计的一致性。现有测试 `test_circuit_opens_after_consecutive_failures` 与 `test_circuit_breaker_stops_repeated_dependency_calls` 是从另一组参数检验相同的控制流。

如还想检验 `allow` 的恢复逻辑，**不要让测试真实等待 60 秒**。将 `time` 对象只在本模块中替换为可控时钟，单独测 `CircuitBreaker`（勿直接修改全局 `time.monotonic`，那会干扰线程池/测试框架）：

```python
from types import SimpleNamespace


def test_breaker_reopens_after_recovery_window(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(
        "app.talent_tools.time",
        SimpleNamespace(monotonic=lambda: now[0]),
    )
    breaker = CircuitBreaker(failure_threshold=1, recovery_seconds=30)
    breaker.fail("evidence")                  # opened_at = 100.0
    assert breaker.allow("evidence") is False
    now[0] = 129.9
    assert breaker.allow("evidence") is False
    now[0] = 130.0
    assert breaker.allow("evidence") is True   # 清零并移除 opened_at
    assert breaker.allow("evidence") is True
    breaker.fail("evidence")                  # 再失败可重新打开
    assert breaker.allow("evidence") is False
    breaker.succeed("evidence")               # 成功则清除熔断
    assert breaker.allow("evidence") is True
```

这个恢复测试验证的是**本实现的“窗口到期直接放行”**，不是单探针 half-open。`monkeypatch` 在测试结束后会恢复模块属性；可控时钟避免时间边界抖动。提交前检查：每条路径的 `operation` 是否真的执行、返回错误码是否与审计一致、第三次是否 `attempts=0`，以及两个测试分别覆盖“单次超时”和“跨请求持续故障”。

### 7.8 常见问题与解决思路

#### 问题 1：测试偶尔失败

原因通常是把超时时间和 sleep 时间设得太接近，例如 `timeout=0.05, sleep=0.05`。线程调度、CI 负载和计时精度会造成边界抖动。

解决：

```text
timeout_seconds = 0.01
sleep = 0.05
```

并且不要断言一个非常精确的总耗时，只验证编码、尝试次数和审计即可。

#### 问题 2：测试进程退出前仍有慢线程

当前实现使用：

```python
pool.shutdown(wait=False, cancel_futures=True)
```

这能让调用方尽快拿到超时结果，但已经开始执行的 Python 线程不一定能被强制取消，慢任务可能继续运行到结束。

课程设计要你理解“超时返回”和“底层任务真正停止”不是一回事。生产环境可以进一步使用：

- 可取消的异步 I/O；
- HTTP 客户端自己的 timeout；
- 数据库 statement timeout；
- 外部任务队列；
- 进程级隔离。

#### 问题 3：审计没有写入

检查所有业务入口是否都经过 `executor.execute()`。如果直接调用：

```python
service.search_candidate_evidence(...)
```

就不会有统一审计，这是绕过治理层的典型错误。

#### 问题 4：错误文本泄露敏感信息

不要把原始异常中的 SQL、Prompt、候选人材料内容直接返回或写入审计。对外使用稳定编码：

```text
invalid_argument
permission_denied
dependency_unavailable
circuit_open
```

---

### 7.9 加分验证：走真正的 ToolNode 再测慢 Provider

上面 7.3～7.5 是执行器单测，已覆盖统一错误和审计；如果老师想看到从 Agent Tool 入口发出调用，可以在 `backend/tests/test_talent_tools.py` 复用已有 `test_tool_node_injects_runtime_context` 的最小图结构。文件头额外 `import json`，另写独立测试（现有文件已经导入 `time`、`AIMessage`、`StateGraph`、`MessagesState`、`ToolNode`、`START`、`END` 及 Tool 类）：

```python
def test_slow_evidence_tool_node_records_same_error(session_factory, context):
    audit = InMemoryAuditSink()
    def slow_provider(**kwargs):
        time.sleep(0.05)
        return {"evidence_packs": []}
    service = TalentToolService(session_factory=session_factory, evidence_provider=slow_provider)
    executor = ToolExecutor(
        policy=ToolPolicy(max_attempts=2, timeout_seconds=0.01, backoff_seconds=0),
        audit_sink=audit,
    )
    builder = StateGraph(MessagesState, context_schema=TalentToolContext)
    builder.add_node("tools", ToolNode(build_talent_tools(service, executor)))
    builder.add_edge(START, "tools")
    builder.add_edge("tools", END)
    graph = builder.compile()
    call = AIMessage(content="", tool_calls=[{
        "name": "search_candidate_evidence",
        "args": {"query": "知识库经验", "candidate_ids": ["C001"]},
        "id": "slow-evidence-1", "type": "tool_call",
    }])
    message = graph.invoke({"messages": [call]}, context=context)["messages"][-1]
    result = json.loads(message.content)
    assert result["error"]["code"] == "dependency_unavailable"
    assert result["meta"]["degraded"] is True
    assert result["meta"]["attempts"] == 2
    assert len(audit.records) == 1
    assert audit.records[0]["status"] == "failed"
    assert audit.records[0]["attempts"] == 2
    assert audit.records[0]["error_code"] == result["error"]["code"]
```

这样同时验证“模型提交业务参数 → ToolNode 注入 Context → Tool → 执行器 → 慢 Provider → 返回 ToolMessage 与审计”。请保持 `sleep` 有明显超时差距，勿断言过于精确的总时长；底层慢线程可能在 Tool 返回后继续运行。

---
## 8. 作业四：把结构化筛选 Provider 接入主图

### 8.1 题目真正考什么

第 11 课主图可能使用固定 Provider：

```python
def _fixture_candidate_provider(request, context):
    if "ai" in request["original_text"].casefold():
        return ["C001", "C004"]
    return []
```

这个 Provider 只适合演示主图状态流转，不能证明数据库查询真实生效。

第 12 课要求换成：

```text
用户请求
    │
    ▼
主图 prepare_request
    │
    ▼
build_candidate_provider()
    │
    ▼
compile_filters(request["original_text"])
    │
    ▼
TalentToolService.filter_candidates()
    │
    ▼
QueryPlan + SQL Builder
    │
    ▼
按 tenant_id 和 active 过滤的 candidate_ids
    │
    ▼
主图继续 evaluate / compose_report
```

### 8.2 当前适配器代码

文件：

`D:\Code\K_Course\talent-eval-agents_learning\backend\app\talent_tools.py`

```python
def build_candidate_provider(
    service: TalentToolService,
    *,
    compile_filters: Callable[[str], list[FilterCondition]],
):
    def provider(request: dict[str, Any], decision_context: Any) -> list[str]:
        context = TalentToolContext(
            tenant_id=decision_context.tenant_id,
            permission_scopes=tuple(
                decision_context.permission_scopes
            ),
            actor_id="langgraph",
        )
        return service.filter_candidates(
            compile_filters(request["original_text"]),
            context=context,
        )

    return provider
```

这个函数做的是 **协议适配**：

| 第 11 课主图需要 | 第 12 课 Service 提供 |
|---|---|
| `provider(request, decision_context) -> list[str]` | `filter_candidates(filters, context=...) -> list[str]` |
| `DecisionContext` | `TalentToolContext` |
| 自然语言请求 | `list[FilterCondition]` |

适配器把三件事接上：

1. 把 `DecisionContext` 映射成 `TalentToolContext`；
2. 把自然语言转成结构化 `FilterCondition`；
3. 调用已有 SQL 筛选 Service。

### 8.3 第一步：复用现有 C001/C002，新增停用 C003，避免编号冲突

`backend/tests/test_talent_tools.py::session_factory` **已有** `tenant-a` 的上海 C001（默认 active）及 `tenant-b` 的上海 C002。不要照旧示例再次插入 C001 或 C002：`EmployeeProfile.employee_no` 在当前模型里是唯一编号，重复会在提交 fixture 时失败。**只新增**同租户停用的 C003：

```python
EmployeeProfile(
    employee_no="C003", tenant_id="tenant-a",
    name="已停用候选人", region="上海", job_level="L3",
    years_of_experience=8, employment_status="inactive",
)
```

于是 `tenant-a` 的 SQL 查询地区为上海时只能拿到 `['C001']`：C002 是另一租户，C003 是非在职。第 8 课 `build_candidate_statement()` 已强制附加 `tenant_id` 和 `employment_status='active'`。另一种做法是使用一个完全独立的测试 fixture，新建一组不与现有编号冲突的数据，但要明确数据库与 fixture 的隔离。

### 8.4 第二步：先用确定性的 `compile_filters`

为了让作业测试不依赖真实大模型，可以先用一个教学用编译器：

```python
def compile_filters(request_text: str) -> list[FilterCondition]:
    assert "上海" in request_text
    return [
        FilterCondition(
            field="region",
            operator="eq",
            value="上海",
        )
    ]
```

之后再把它替换成第 8 课的 Query Plan 模型编译器。作业四的重点是 Provider 适配和主图注入，不是再次实现自然语言解析模型。

### 8.5 第三步：构造 Provider 和主图

```python
service = TalentToolService(
    session_factory=session_factory,
)

provider = build_candidate_provider(
    service,
    compile_filters=compile_filters,
)

graph = build_talent_decision_graph(provider)
```

然后执行：

```python
result = graph.invoke(
    {
        "messages": [],
        "request_text": "筛选上海候选人",
    },
    context=DecisionContext(
        tenant_id="tenant-a",
        permission_scopes=("hr_private",),
    ),
)
```

断言：

```python
assert result["candidate_ids"] == ["C001"]
assert result["status"] == "completed"
assert "C001" in result["report"]
```

### 8.6 第四步：理解主图的状态路线

`build_talent_decision_graph()` 注册的节点是：

```text
START
  │
  ▼
receive_request
  │
  ▼
prepare_request
  │
  ▼
retrieve_candidates
  │
  ├── candidate_ids 非空 ──> evaluate
  │                              │
  │                              ▼
  │                        compose_report
  │                              │
  │                              ▼
  │                        validate_report
  │                              │
  │                              ▼
  │                            END
  │
  └── candidate_ids 为空 ──> no_candidates ──> END
```

`retrieve_candidates` 只负责把 Provider 结果写入状态：

```python
return {
    "candidate_ids": candidate_ids,
    "status": "candidates_ready",
}
```

因此，作业验收的最关键断言是：

```python
assert result["candidate_ids"] == 数据库筛选结果
```

不是只断言最终报告中出现了某个字符串，因为报告可能是占位文本，不能证明数据确实来自数据库。

### 8.7 第五步：真正替换验证脚本中的固定 Provider（不要只改测试）

文件 `backend/scripts/verify_talent_decision_graph.py` 的真实入口是：

```python
context = DecisionContext(tenant_id="course-demo", permission_scopes=("hr_private",))
graph = build_talent_decision_graph(_fixture_candidate_provider)
```

如果只在测试中构造新 Provider，而脚本仍用 `_fixture_candidate_provider`，**作业“主图接入”尚未完成**。按下面顺序编辑脚本：

1. 导入 `create_engine`、`Session`、`StaticPool`、`Base`、`EmployeeProfile`、`FilterCondition`、`TalentToolService` 与 `build_candidate_provider`；保留原有的 `DecisionContext` / Graph 导入。
2. 建立 SQLite 演示数据库（验证脚本可以不依赖 PostgreSQL，但运行路径必须是真实 SQL）：

```python
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from app.models import Base, EmployeeProfile
from app.query_plan import FilterCondition
from app.talent_tools import TalentToolService, build_candidate_provider

engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
Base.metadata.create_all(engine)
with Session(engine) as db:
    db.add(EmployeeProfile(employee_no="C001", tenant_id="course-demo",
                           name="示例候选人", region="上海", employment_status="active"))
    db.add(EmployeeProfile(employee_no="C999", tenant_id="another-tenant",
                           name="越权候选人", region="上海", employment_status="active"))
    db.commit()
```

3. 替换固定数组来源为受控编译器 + Service。为了维持脚本原来的“AI 请求有候选人、Java 请求无候选人”两个示例，不能写死 `lambda _: [region == 上海]`（否则 Java 请求也会得到 C001）：

```python
def compile_filters(text: str) -> list[FilterCondition]:
    # 仅为离线验证图接线的确定性样例，生产中替换成受控 Query Plan 编译器
    region = "上海" if "ai" in text.casefold() else "不存在的地区"
    return [FilterCondition(field="region", operator="eq", value=region)]

service = TalentToolService(session_factory=lambda: Session(engine))
provider = build_candidate_provider(service, compile_filters=compile_filters)
graph = build_talent_decision_graph(provider)  # 替换原来的固定 Provider
```

4. 保留现有 `graph.stream(...)` 和 `graph.invoke(...)` 两条路径，增加断言：正常路径 `candidate_ids == ['C001']`，Java 路径 `candidate_ids == []` 且 `status == 'no_candidates'`。`tenant_id='course-demo'` 必须与插入的数据一致；若沿用 `tenant-a` fixture 而 Context 仍是 `course-demo`，空结果是**正确的租户隔离**。
5. 在 `backend` 目录运行 `python scripts/verify_talent_decision_graph.py`，观察 `retrieve_candidates` 节点更新，再运行 `python -m pytest -q tests/test_talent_tools.py tests/test_talent_decision_graph.py`。可修改数据库地区或上下文租户再运行：编号应随真实 SQL 数据改变，而不是固定列表。仅凭报告出现 C001 不能证明接线成功，必须检查 `candidate_ids`。
### 8.8 需要注意：Provider 当前没有经过 ToolExecutor

当前 `build_candidate_provider()` 直接调用：

```python
service.filter_candidates(...)
```

所以它复用了业务 Service 和租户隔离，但不会自动产生 ToolExecutor 的重试、熔断和审计。

这不是一定错误，而是一个需要理解的架构选择：

- 如果主图节点是内部可信调用，可以接受 Provider 直接调用 Service；
- 如果主图调用也必须纳入统一工具审计，就应该让 Provider 经过一个受治理的执行入口；
- 不要为了“看起来统一”而在 Service 内部再套一层 Tool 调用，造成循环依赖。

一种可选的治理方式是给 Provider 注入一个执行函数：

```python
def provider(request, decision_context):
    context = TalentToolContext(...)
    result = executor.execute(
        "filter_candidates",
        lambda: service.filter_candidates(
            compile_filters(request["original_text"]),
            context=context,
        ),
        context=context,
        arguments={"filters": ["compiled"]},
    )
    if not result["ok"]:
        raise RuntimeError(result["error"]["code"])
    return result["data"]
```

但是否这样做，应由系统的审计范围决定。课程当前代码把 `build_candidate_provider()` 定义为“主图到结构化筛选 Service 的适配入口”，重点是复用查询能力和租户边界。

---

## 9. Candidate Evidence Pack 2.0：为什么不是返回一段文本

### 9.1 Evidence Pack 的目标

`search_candidate_evidence` 不应该返回“这个人很符合”这类不可追溯的总结，而应该返回结构化证据包：

```json
{
  "schema_version": "2.0",
  "candidate_id": "C001",
  "requirements": [
    {
      "requirement_id": "S1",
      "query": "企业知识库项目经验",
      "status": "sufficient",
      "facts": [
        {
          "event": "知识库项目",
          "period": "2025-01/2025-06",
          "claim": "参与企业知识库建设",
          "answer": "yes",
          "sources": [
            {
              "citation_id": "...",
              "chunk_id": "...",
              "quote": "...",
              "quote_start": 10,
              "quote_end": 24
            }
          ]
        }
      ],
      "conflicts": [],
      "missing_information": []
    }
  ]
}
```

### 9.2 Service 的证据链路

```python
if self.evidence_provider is None:
    raise TransientToolError("evidence_provider_not_configured")

result = self.evidence_provider(
    query=query,
    candidate_ids=candidate_ids,
    tenant_id=context.tenant_id,
    permission_scopes=list(context.permission_scopes),
    include_evidence_pack=True,
)
```

然后强制版本校验：

```python
for pack in result.get("evidence_packs", []):
    if pack.get("schema_version") != "2.0":
        raise ValueError("不支持的证据协议版本")
```

这意味着工具层不接受任意“看起来像证据”的字典。协议版本不对，就属于参数/契约错误，而不是直接返回给 Agent。

### 9.3 为什么候选人范围必须先由结构化筛选产生

正确顺序：

```text
先用 SQL 得到 candidate_ids
        │
        ▼
再把 candidate_ids 作为 EvidenceFilter
        │
        ▼
只在候选人范围内做语义检索
        │
        ▼
再生成 Evidence Pack
```

不能先对全库做语义检索，再把结果“猜测性地”归属给候选人。结构化硬条件和语义证据的顺序决定了权限和可解释性。

---

## 10. 数据库表与审计设计

### 10.1 `job_descriptions`

迁移文件：

`D:\Code\K_Course\talent-eval-agents_learning\database\migrations\012_talent_tools.sql`

核心字段：

```sql
job_code VARCHAR(64) NOT NULL UNIQUE,
tenant_id VARCHAR(64) NOT NULL,
name VARCHAR(255) NOT NULL,
content TEXT NOT NULL,
version INTEGER NOT NULL DEFAULT 1,
status VARCHAR(32) NOT NULL DEFAULT 'active'
```

岗位查询至少使用：

```sql
WHERE tenant_id = :trusted_tenant_id
  AND status = 'active'
```

### 10.2 `tool_call_audits`

```sql
call_id VARCHAR(64) NOT NULL UNIQUE,
tool_name VARCHAR(128) NOT NULL,
tenant_id VARCHAR(64) NOT NULL,
actor_id VARCHAR(128) NOT NULL,
run_id VARCHAR(128) NOT NULL,
argument_keys JSONB NOT NULL DEFAULT '[]'::jsonb,
status VARCHAR(32) NOT NULL,
attempts INTEGER NOT NULL,
duration_ms DOUBLE PRECISION NOT NULL,
error_code VARCHAR(64)
```

典型查询：

```sql
SELECT
    tool_name,
    status,
    error_code,
    attempts,
    duration_ms,
    actor_id,
    run_id,
    created_at
FROM tool_call_audits
WHERE tenant_id = 'tenant-a'
ORDER BY created_at DESC;
```

审计表的用途：

- 追踪某次 Agent 运行调用了哪些工具；
- 统计某个依赖是否频繁超时；
- 发现某个工具的重试次数异常；
- 支持问题复盘，但不复制敏感业务正文。

---

## 11. 一次完整验收应该怎么做

### 11.1 先确认迁移和服务依赖

在仓库根目录，按 README 中的方式启动 PostgreSQL 并执行幂等迁移：

```bash
docker compose -p talent-eval-agents-course up -d postgres

docker compose -p talent-eval-agents-course exec -T postgres \
  psql -U talent -d talent_docs -v ON_ERROR_STOP=1 -f /dev/stdin \
  < database/migrations/012_talent_tools.sql
```

迁移使用 `CREATE TABLE IF NOT EXISTS` 和 `CREATE INDEX IF NOT EXISTS`，重复执行不会重复创建表和索引。

### 11.2 先跑现有 Lesson 12 验证脚本

```bash
cd D:\Code\K_Course\talent-eval-agents_learning\backend
uv run --no-sync python -m scripts.verify_talent_tools
```

重点观察：

- 工具名称列表包含 4 个工具；
- 岗位查询包含 `job_code` 和 `version`；
- 候选人筛选返回结构化 ID；
- 档案读取只返回当前租户的候选人；
- Evidence Pack 版本为 `2.0`；
- retry 最终成功且 `attempts` 正确；
- circuit breaker 最后返回 `circuit_open`；
- 审计状态序列符合预期。

### 11.3 跑作业相关测试

```bash
cd D:\Code\K_Course\talent-eval-agents_learning\backend
uv run --no-sync pytest -q \
  tests/test_talent_tools.py \
  tests/test_talent_decision_graph.py
```

如果只调试一个作业，可以单独跑：

```bash
uv run --no-sync pytest -q tests/test_talent_tools.py -k job_lookup
uv run --no-sync pytest -q tests/test_talent_tools.py -k candidate_profile
uv run --no-sync pytest -q tests/test_talent_tools.py -k timeout
uv run --no-sync pytest -q tests/test_talent_tools.py -k provider
```

### 11.4 再跑完整回归

```bash
uv run --no-sync pytest -q
```

不要只看“测试通过”，还要检查测试是否真的覆盖了题目要求：

| 作业 | 最低验收断言 |
|---|---|
| 1 | inactive 不返回、其他租户不返回、同名版本不丢、结果有 `job_code`/`version` |
| 2 | 100 通过、101 在 Schema 失败、Service/数据库未执行 |
| 3 | `dependency_unavailable`、`degraded=true`、审计 failed、attempts 相同、error_code 相同 |
| 4 | 主图 `candidate_ids` 等于真实 SQL 筛选结果，而不是 fixture 固定值 |

---

## 12. 按错误类型排查问题

### 12.1 报 `invalid_argument`

排查顺序：

1. 看 Pydantic Schema 是否限制了长度、枚举、操作符；
2. 看 `FilterCondition` 的 `model_validator` 是否拒绝了错误组合；
3. 看 Service 是否抛出了 `ValueError`；
4. 确认这种错误不应该进入重试。

例如：

```python
FilterCondition(
    field="region",
    operator="gt",
    value="上海",
)
```

这是非法的，因为 `gt` 只允许数值字段。

### 12.2 报 `permission_denied`

排查：

1. `TalentToolContext.tenant_id` 是否为空；
2. `permission_scopes` 是否为空；
3. HTTP Header 是否传入了租户和权限；
4. ToolNode/Graph invoke 是否传入了 `context=`；
5. 是否错误地从模型参数读取 tenant_id。

当前 Service 的基础保护：

```python
if not context.tenant_id.strip():
    raise PermissionError("缺少可信租户")
if not context.permission_scopes:
    raise PermissionError("缺少可用权限范围")
```

### 12.3 报 `dependency_unavailable`

排查：

1. 是不是 `TransientToolError`；
2. 是不是超时；
3. 当前工具策略的 `max_attempts`、`timeout_seconds` 是否合理；
4. 是否已经触发熔断；
5. 依赖服务是否被正确初始化；
6. 证据 Provider 是否为 `None`。

特别注意：`evidence_provider_not_configured` 是内部异常原因，经过 Executor 后对外应归类为依赖不可用。

### 12.4 报 `circuit_open`

说明不是当前调用本身失败，而是之前同名工具已经连续失败达到阈值：

```python
CircuitBreaker(failure_threshold=2, recovery_seconds=60)
```

恢复方式：

- 等待 `recovery_seconds` 到期；
- 在测试中使用新的 Breaker；
- 不要简单把失败阈值调得极高来“绕过”问题。

### 12.5 主图没有候选人

排查链路：

```text
request_text
  │
  ▼
compile_filters(request_text)
  │
  ▼
FilterCondition 是否正确
  │
  ▼
tenant_id 是否正确
  │
  ▼
employment_status 是否 active
  │
  ▼
SQL 是否匹配 region/job_level/years_of_experience
```

不要先怀疑 LangGraph。先单独调用：

```python
filters = compile_filters("筛选上海候选人")
service.filter_candidates(filters, context=tool_context)
```

如果 Service 单独返回正确，而 Graph 返回错误，再检查 Provider 注入和 `DecisionContext` 映射。

---

## 13. 当前代码中值得你主动理解的设计取舍

### 13.1 `lookup_job_descriptions` 使用模糊匹配，但过滤必须先发生

名称相似度只是匹配策略：

```python
SequenceMatcher(None, normalized_query, normalized_name).ratio()
```

它不能替代权限边界。正确顺序是：

```text
先 SQL 限定 tenant + active
        │
        ▼
再做名称相似度匹配
```

如果反过来对全库做相似度匹配，其他租户的岗位可能先进入候选结果。

### 13.2 Query Plan 不允许模型直接生成 SQL

`FilterCondition` 是受限 DSL：

```python
field: FilterField
operator: Literal["eq", "in", "lt", "lte", "gt", "gte"]
value: str | int | float | list[str]
```

`query_plan.py` 再把 DSL 映射到已注册字段：

```python
FILTER_FIELD_REGISTRY = {
    FilterField.REGION: ...,
    FilterField.JOB_LEVEL: ...,
    FilterField.YEARS_OF_EXPERIENCE: ...,
}
```

这样模型表达的是“我要按 region 等于上海筛选”，而不是“请执行我生成的 SQL”。

### 13.3 事实、证据和判断要分开

本课工具返回：

- 岗位事实；
- 候选人结构化资料；
- 可核验的材料证据。

它不应该直接返回未经证据支撑的“推荐分数”或“适合录用”。

Candidate Evidence Pack 2.0 中的 `status`、`facts`、`citations`、`conflicts`、`missing_information` 让后续评估节点可以在可追溯事实之上做判断。

---

## 14. 建议的作业实施顺序

不要四个作业同时改。建议按以下顺序：

### 阶段 1：先理解现有代码

阅读顺序：

1. `models.py`：看三张表的字段；
2. `query_plan.py`：看 Filter DSL 到 SQL 的映射；
3. `talent_tools.py`：看 Context、Executor、Service、Tool；
4. `talent_decision_graph.py`：看 Provider 注入点；
5. `test_talent_tools.py`：把每个测试当成一份行为契约。

### 阶段 2：先做作业一和二

它们是输入和数据边界问题，最适合先建立信心：

- 作业一：状态、租户、版本；
- 作业二：Schema 边界和“失败前不访问数据库”。

### 阶段 3：再做作业三

它是通用执行器问题，要观察：

- 重试次数；
- timeout；
- degraded；
- audit；
- circuit breaker。

### 阶段 4：最后做作业四

因为主图接入依赖前面已经理解：

- Service 能正确筛选；
- Context 能正确传递；
- SQL 返回的 candidate_ids 正确。

### 阶段 5：最后跑验证脚本和完整回归

验证脚本用于观察链路，单元测试用于固定契约，完整回归用于确认没有破坏前面课程。

---

## 15. 最终学习检查表

完成本课后，你应该能不用看答案解释下面这些问题：

### 工具 Schema

- [ ] 为什么 `runtime` 出现在函数参数里，却不出现在模型 Schema 中？
- [ ] 为什么 `candidate_ids` 的 `max_length=100` 是列表上限？
- [ ] 为什么 101 个 ID 必须在 Service 之前失败？

### 可信上下文

- [ ] `tenant_id` 为什么不能由模型提交？
- [ ] `actor_id` 和 `run_id` 为什么要进入审计？
- [ ] LangGraph 的 `DecisionContext` 怎样映射成 `TalentToolContext`？

### 数据边界

- [ ] 岗位查询为什么必须同时过滤 tenant 和 active status？
- [ ] 同名不同版本为什么不能按 name 去重？
- [ ] 候选人 SQL 为什么必须固定加 `employment_status == "active"`？

### 可靠性

- [ ] 哪些错误可重试，哪些错误不可重试？
- [ ] timeout 后为什么返回 `dependency_unavailable` 而不是 `invalid_argument`？
- [ ] `degraded=true` 和 `ok=false` 分别表达什么？
- [ ] 熔断器为什么需要按工具名称保存失败状态？

### 证据协议

- [ ] 为什么要检查 `schema_version == "2.0"`？
- [ ] 为什么 evidence pack 要有 citation 和 quote？
- [ ] 为什么语义检索必须限制在结构化筛选得到的 candidate_ids 内？

### 主图接入

- [ ] `build_candidate_provider()` 解决了什么接口不兼容？
- [ ] 如何证明候选人 ID 来自数据库，而不是 fixture？
- [ ] 为什么主图本身不应该重新实现 SQL？

如果这些问题都能回答，本课就不只是“把测试改到绿色”，而是真正掌握了 Agent 工具层、可信上下文、可靠性执行器和证据协议的协作方式。

---

## 16. 一句话复盘

第 12 课的关键不是“写四个函数”，而是建立一条不会被模型越权、不会因单次依赖故障失控、不会把未经验证的文本当事实的后端调用链：

```text
业务参数由模型提交
可信身份由运行时注入
结构化边界由 Schema 和 SQL 保证
可靠性由 Executor 统一治理
事实由 Service 和数据库提供
证据由 Evidence Pack 结构化表达
主图通过 Provider 适配真实数据
```

这就是本课要掌握的完整设计思路。

---

## 17. 四道作业完成后，怎样向老师说明你真正掌握了本课

### 17.1 按“问题 → 边界 → 代码 → 证据”口述每题

- **作业一（JD）**：问题是同名 JD 可能属于其他租户、已停用或有多个版本；边界在 `lookup_job_descriptions()` 的 SQL `tenant_id/status`；版本身份保留在 `job_code/version`；用同名四类 fixture 同时断言“应该出现”和“不能出现”。不把“查到一个模糊名称”当成岗位已确认。
- **作业二（档案）**：问题是模型给出 101 个 ID 造成大批量读取；边界在 `@tool` 的 `Annotated[list[str], Field(max_length=100)]`；`tool_call_schema.model_validate()` 在 Service/Session 之前拒绝；用 100/101 边界值及 Session 访问计数说明拒绝发生在哪一层。区分框架 Schema 异常与执行器的 `invalid_argument` 返回。
- **作业三（可靠性）**：问题是依赖超时后调用方看不到稳定错误，也无法追溯尝试次数；边界在 `ToolExecutor.execute` 的每次 `future.result(timeout=...)`、有限重试及 `_finish`；用同一错误码/次数同时断言 Tool 返回与审计。明确超时**返回**不等于后台线程已**停止**。
- **作业四（主图）**：问题是固定数组 Provider 不反映数据库；边界在第 11 课 `CandidateProvider` 抽象，第 12 课闭包适配，第 8 课 SQL 产生编号；更换实际构图点并测试改变租户或记录时 `candidate_ids` 随之变化。解释主图当前 `evaluate` 是占位，且直接 Provider 不经过 `ToolExecutor`。

### 17.2 可作为课程汇报“亮点”的设计取舍，而非泛泛总结

| 设计亮点 | 要展示的一行/一组代码 | 比“只是实现功能”高在哪里 | 要承认的边界 |
|---|---|---|---|
| 可信身份与模型参数分离 | `runtime: ToolRuntime[TalentToolContext]` 与 `tool_call_schema`、SQL `tenant_id == context.tenant_id` | 输入 Schema 和查询层同时守边界 | 真实认证层必须保证 Context 的来源可信 |
| 第 8/9/11/12 课能力可组合 | `QueryPlan` → `select_candidate_ids` → `EvidenceFilter` / Pack → `CandidateProvider` | SQL 硬条件、材料证据、主图状态各司其职 | 当前 Tool 的 `evidence_provider` 仍需实际适配 |
| 双层稳定协议 | `_finish()` 的 `{ok,data,error,meta}` 和 Pack 的 `schema_version='2.0'` | 调用失败与证据内容分别可治理、可演进 | 只检查存在的 Pack 版本，需额外保证结果集合一致性 |
| 按异常分类而非无脑重试 | `except (TransientToolError, FutureTimeoutError)` 对比 `except (ValueError, PermissionError)` | 不把权限错误变成重复访问，减少故障时资源浪费 | 未捕获的异常、底层硬超时需要额外设计 |
| 请求内重试与请求间熔断分工 | `ToolPolicy.max_attempts`、`CircuitBreaker.allow/fail/succeed`、`_finish(blocked, attempts=0)` | 超时吸收单次抖动；连续失败后快速拒绝新调用，降级和审计仍维持统一协议 | 进程内按 Tool 名称隔离；恢复非严格 half-open，已运行的超时线程不能强杀 |
| `execute` 与 `_finish` 分工形成单一终态出口 | `allow → submit/result → 分类 → succeed/fail → _finish → audit_sink.write`，仅存 `argument_keys` | 可靠性分支和统一返回/隐私审计解耦；响应与审计同一 `call_id` 可对账 | Schema 失败、未知异常、绕过 Executor 的 Provider 不在本层审计范围；审计写失败会阻断返回 |
| 闭包式依赖注入 | `build_candidate_provider(service, compile_filters=...)` 返回 `provider` | 图只认识接口，可换数据库实现而不改拓扑 | 当前闭包 Actor 为 `langgraph`，Run ID 未显式传播 |

### 17.3 自测：能回答才算学会

1. 模型能填 `candidate_ids`，为什么仍不能读出别的租户档案？请指出 `ToolRuntime` 和 SQL 各自负责哪一半。
2. 为什么第 8 课筛选到 C001 **不能**推断 C001 具备某个项目经历？第 9 课的引用核验还做了什么？
3. 第一次依赖超时、第二次成功时 `attempts` 是几？熔断阈值 3 统计的是三次 attempt 还是三次用尽重试的 Tool 调用？
4. 101 个 ID 的 Schema 拒绝为何不会产生执行器审计？如果业务要求审计所有拒绝，应该在哪层补？
5. 为什么 `build_candidate_provider` 的外层函数执行结束后，返回的 `provider` 仍能使用 `service`？每次租户是构造时固定的吗？
6. 要让作业四的脚本正常演示空候选人分支，为什么不能把 `compile_filters` 写成永远返回“上海”？

如果能结合源代码和测试逐题回答，并能区分**已有实现**与**需要生产补强的地方**，就掌握了本课最关键的安全边界、复用设计、可靠性治理和可验证性。