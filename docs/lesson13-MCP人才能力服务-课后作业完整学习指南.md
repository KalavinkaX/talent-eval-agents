# 第13课：MCP 人才能力服务——从架构到源码，再到课后作业

> 读者：刚毕业、希望从事 Agent 开发的同学。代码依据：`D:\Code\K_Course\talent-eval-agents_learning` 当前分支，梳理于 2026-09-27。以下所称 `backend/...` 均从该项目根目录起算。本文是**源码解读和实施指南**：仓库里的课后作业改动尚未由本文代为实现；Inspector 清单是待现场填写的模板。

## 0. 先建立思考框架：这节课到底解决什么问题

### 0.1 从第12课到第13课：为什么需要 MCP

第12课已有 `TalentToolService` 和 `ToolExecutor`，可以在一个 Python 进程里被 LangChain Tool 调用。设想企业同时有“招聘助手”“内部人才盘点”“HR 工作台”三个 AI 应用：每个应用若都直接复制 Python 函数和参数 Schema，不仅集成重复，而且 tenant 身份映射、权限、工具升级很容易出现三套不一致的实现。

第13课的设计不是“重写人才检索”，而是增加一个**共享能力边界**：原有业务层不动或尽量少动，MCP Server 将它公布给不同 Host；MCP Client 处理连接、协商和调用；HTTP 入口负责把经过验证的 token 转成可信业务 Context。最终再把远程 Tool 包装为 LangChain Tool，放入 LangGraph `ToolNode`。

用一句话回答老师：“**把进程内业务能力，升级成有协议契约、有身份边界、可被多个 Agent Host 发现和调用的服务；业务权限仍然由服务端强制执行。**”

### 0.2 总体设计图：先认清每层的责任

```text
用户提出“筛选上海的候选人”
    │
    ▼
[Host：招聘助手 / LangGraph]   管对话、业务状态、决策、工具调用时机
    │  MCPAdapter 发现远程工具；ToolNode 执行模型给出的 tool_call
    ▼
[MCP Client]                 管连接、协议协商、list_tools/call_tool/read_resource/get_prompt
    │  STDIO（本地子进程）或 Streamable HTTP（/mcp + Bearer token）
    ▼
[MCP Server]                 注册四个 Tools、两个 Resource Templates、固定 Policy Resource、Prompt
    │  HTTP 受保护入口：TokenVerifier → scope/audience → token claims → TalentToolContext
    ├── Tool  ── ToolExecutor ── TalentToolService ── SQL/证据 provider
    ├── Resource ────────────── TalentToolService ── SQL
    └── Prompt ── 文本模板（不自动查询数据库）
```

**必须记住的边界：** Host 能让模型请求 Tool，但模型提供的参数不构成身份；Client 管协议，不应擅自赋予业务权限；Server 的 token 验证和业务 Service 的租户过滤缺一不可。Resource 直接访问 Service，当前**不经过** ToolExecutor；Prompt 只生成模板文本。图内 ToolNode 不承担鉴权，鉴权要沿真正的 HTTP 请求一路传到 Server。

### 0.3 四条设计原则，决定你之后如何写作业

| 原则 | 本仓库对应例子 | 如果忽视会怎样 |
|---|---|---|
| **协议层薄，业务层复用** | `build_talent_mcp_server` 注册工具，仍调用 `TalentToolService` | 在装饰器里复制 SQL，进程内与 MCP 两套规则逐渐分叉 |
| **身份从可信入口来** | `get_access_token()` → `TalentToolContext` → SQL `tenant_id` | 让模型传 `tenant_id`，跨租户读取无法防住 |
| **能力类型按控制者划分** | 四个 Tool、JD/档案 Resource、评估 Prompt | 将 Resource 当 Tool 或认为 Prompt 会自动执行查询 |
| **契约用正反测试证明** | 发现、调用、401/403、租户 A/B、Graph ToolMessage | 只看 Inspector 页面就宣称安全/端到端完整 |

控制边界再细化为一张表：

| MCP 能力 | 谁主要决定使用 | 本课例子 | 具体行为 |
|---|---|---|---|
| Tool | 模型可以提出调用，Host 决定是否执行 | `filter_candidates` | 接受结构化参数、执行业务查询、返回结果 |
| Resource | Host/应用选择要读哪个 URI | `talent://jobs/{job_code}` | 以 URI 读取受租户约束的内容 |
| Prompt | 用户/应用选择使用哪个任务模板 | `talent_assessment` | 根据参数生成评估请求文本；不自动查库 |

这里的“主要决定”不是绝对禁令：真正的接入权限仍由 Host 配置和 Server 鉴权共同约束，三类能力都不能绕过租户过滤。

### 0.4 老师要你学到的“可迁移能力”

按重要性排序：① 画出 Host/Client/Server 的责任边界；② 设计 Tool/Resource/Prompt 的控制边界；③ 懂协议协商与 SDK 迁移，不混淆二者版本；④ 在 HTTP 上做 token、scope、audience、tenant/数据权限的分层验证；⑤ 把远程工具放入 Agent 执行图；⑥ 用反例、Inspector、集成脚本形成验收证据。**会写 `@server.tool()` 只是起点，不是课程终点。**

### 0.5 阅读本文的顺序

先看第1章按模块追一次真实请求，每个模块都有“职责→真实代码→逐行解读→输入输出→边界/验证”。再按第2、3、4章做三个作业；第5章是测试和问题定位；第6章自测。阅读代码时，请同时在编辑器中打开相应源文件；不要单独背摘录代码。

## 1. 按模块拆解完整后端链路

> 本章不是按文件名流水账，而是按**请求从哪里进入、在哪里变可信、在哪里真正访问数据**组织。请先读 1.1 的入口，随后用 `filter_candidates` 贯穿 1.2～1.5，再比较 Resource、Prompt 和 Graph。

### 模块 A：Server 装配、协议与传输入口

**职责：** 创建 Server 对象，登记能力，选择传输；它本身不存储候选人、不决定模型该调用什么。

**代码定位：** `backend/app/talent_mcp_server.py:48-62` 与 `:118-152`，`backend/samples/lesson13/codestats_mcp_v2.py` 文件末尾，`backend/scripts/verify_talent_mcp.py::verify_langgraph`。

```python
# talent_mcp_server.py 中的核心装配，省略参数注解之外的无关行
server = MCPServer(
    "Talent Capability Service",
    version="13.1.0",
    instructions="提供受租户与权限约束的人才检索和证据读取能力。",
    token_verifier=token_verifier,
    auth=auth,
)
# 下文依次 @server.tool(...)、@server.resource(...)、@server.prompt(...)
return server
```

逐行看：`MCPServer` 是 SDK v2 的服务构造器；`version="13.1.0"` 是此服务的实现/展示版本，**不是**线协议修订号；`token_verifier` 和 `auth` 是受保护 HTTP 的注入点；函数最后返回对象供不同入口启动，而不是在 import 时启动网络服务。当前的 `talent_mcp_server.py` **只有工厂**，没有模块级 `mcp` 和 `mcp.run("stdio")`，因此不能把它直接当作已就绪的人才 STDIO 入口。

本仓库有三种不同层次的入口，容易混淆：

| 入口 | 源码中的实际写法 | 用来证明什么 | 不证明什么 |
|---|---|---|---|
| STDIO 演示 | `codestats_mcp_v2.py` 末尾 `mcp = build_server(...)`、`mcp.run("stdio")` | v1 的 FastMCP 示例迁移到 SDK v2；本地进程可启动 | 不是人才 Server，且无 HTTP token 校验 |
| 进程内协议测试 | `async with Client(server) as client` | 注册的 Tool/Resource/Prompt 可按 MCP Client API 发现、调用 | 不经过 HTTP 中间件，不能证明 Bearer token 生效 |
| Streamable HTTP | `server.streamable_http_app()` 挂进 Uvicorn/ASGI | 网络传输、鉴权中间件及远程 Adapter 可验证 | 如果 factory 注入的是 demo_context 而非 auth 配置，也不自动具备生产鉴权 |

**协议问题怎么理解：** `Client(server)` 默认 `mode="auto"`；当前安装的 SDK v2 Client 在 auto 下尝试现代发现并可对历史服务回退。`Client(..., mode="legacy")` 强制旧初始化握手；若显式固定现代协议版本，则没有自动回退。SDK 包版本（`mcp[cli]>=2.2,<3`，见 `backend/pyproject.toml`）、Server 自身 `13.1.0`、`client.protocol_version` 是三个不同概念。运行时记录协商的 `client.protocol_version`，不要只引用 README 的示例输出。

**本模块必须回答：** 服务怎么启动？连接到哪个真实端点？当前有没有鉴权？协议版本到底从哪个属性读取？STDIO 的 stdout 要留给协议流，日志应走 stderr。

### 模块 B：HTTP 身份——把不可信请求变成可信业务 Context

**职责：** 在远程访问时验证 Bearer token 有效性、适用目标与访问 scope，再将经过验证的 claims 映射成业务身份。此模块不是“用户传一个 tenant 字符串”。

**代码定位：** `backend/app/talent_mcp_server.py:20-45`；受保护装配见 `backend/tests/test_talent_mcp_server.py::_protected_server`；反例见该文件两个 Streamable HTTP 测试。

```python
def authenticated_talent_context() -> TalentToolContext:
    token = get_access_token()
    if token is None:
        raise PermissionError("缺少经过验证的 MCP Access Token")
    return talent_context_from_token(token)

# talent_context_from_token 内的关键映射：
claims = token.claims or {}
tenant_id = str(claims.get("tenant_id", "")).strip()
raw_permission_scopes = claims.get("permission_scopes", [])
# 检查列表型、去空项、拒绝空 tenant/空权限后：
return TalentToolContext(
    tenant_id=tenant_id,
    permission_scopes=permission_scopes,
    actor_id=token.subject or token.client_id,
    run_id=str(claims.get("run_id", "unknown")),
)
```

按请求顺序理解：

1. HTTP `Authorization: Bearer ...` 进入 `server.streamable_http_app()`；测试用 `_TokenVerifier.verify_token` 返回已验证的 `AccessToken` 对象，生产应由真正的验签/令牌校验实现提供。**演示 verifier 只是内存映射，不是生产 OAuth 服务。**
2. `AuthSettings(issuer_url=..., resource_server_url=resource, validate_token_resource=True, required_scopes=["talent:read"])` 要求 token 指向本 `/mcp` 资源（audience/resource）、具备访问 scope。测试断言：缺 token 401，缺 scope 403，audience 错误 401。
3. 只有校验通过，`get_access_token()` 才可作为当前请求的可信 token 来源。`tenant_id`、`permission_scopes` 由 claims 映射；`subject`/`client_id` 用于审计 actor。客户端 `clientInfo`、工具参数和 URL 片段均不能自行声明可信 tenant。
4. Service 还要使用该 Context 在 SQL 中追加 tenant 约束。**HTTP 入口放行 ≠ 允许访问任意租户数据。**

两类“scope”不能混成一个：token 的 `scopes=["talent:read"]` 是能否访问 MCP 服务的 OAuth/服务级条件；claims 的 `permission_scopes=["hr_private"]` 被映射给人才业务。当前 `TalentToolService._check_context` 仅要求后者非空，未实现完整字段/材料级授权；这正是读源码时需要识别的真实能力边界。

**最小正反验证：** `test_streamable_http_enforces_token_scope_and_audience` 检查 401/403/401；`test_streamable_http_token_claims_reach_business_service` 用有效 token 调用档案 Tool，输入 `C001,C002`，只返回租户 A 的 `C001`。这是“token → Context → 查询”的闭环，不可被 `Client(server)` 的进程内测试替代。

### 模块 C：四个 Tool 的 MCP 外壳——以 `filter_candidates` 贯穿

**职责：** 定义可发现的名字、描述和参数 Schema；获得可信 Context；委托既有执行器和业务服务。Tool 外壳不是直接写 SQL 的地方。

**代码定位：** `backend/app/talent_mcp_server.py:64-116`，重点读 `:78-89`：

```python
@server.tool(structured_output=True)
def filter_candidates(
    filters: Annotated[list[FilterCondition], Field(max_length=20)],
) -> dict[str, Any]:
    """按结构化条件筛选授权租户内的候选人。"""
    context = context_provider()
    return executor.execute(
        "filter_candidates",
        lambda: service.filter_candidates(filters, context=context),
        context=context,
        arguments={"filters": filters},
    )
```

**逐行解读：**

- 装饰器把 Python 函数注册成 MCP Tool。`structured_output=True` 使统一结果 `{ok,data,error,meta}` 通过 `CallToolResult.structured_content` 可读。Tool 名默认函数名；docstring 是能力说明。
- `filters` 不只是“任意 JSON 数组”：它最多20项，每项须符合 `FilterCondition` 的字段和操作符限制。比如 `[{"field":"region","operator":"eq","value":"上海"}]`。
- `context_provider()` 才是本次执行的身份来源；生产 HTTP 装配应传 `authenticated_talent_context`，本地测试可传 `_context`/`demo_context`。函数参数里**没有 tenant_id**，是正确的边界设计。
- `lambda` 延迟执行，交由 `ToolExecutor` 处理重试/超时/熔断/审计；`arguments` 是审计参数信息，不是权限来源。调用链最终返回执行器包装的字典。

| Tool 名 | 输入接口 | 业务方法及结果 | 你该注意的权限问题 |
|---|---|---|---|
| `lookup_job_descriptions` | `query: str`（1～200字）、`limit: int`（1～10，默认5） | `service.lookup_job_descriptions` → 当前租户 active 岗位列表及匹配分数 | 不能跨租户搜索同名 JD |
| `filter_candidates` | `filters: list[FilterCondition]`（至多20） | `service.filter_candidates` → 候选 ID 列表 | SQL 必须附可信 tenant 且只选在职 |
| `search_candidate_evidence` | `query`（1～500字）、`candidate_ids`（1～100） | `service.search_candidate_evidence` → Candidate Evidence Pack 2.0 | 下游 evidence_provider 也必须执行 tenant/scopes/候选范围约束 |
| `get_candidate_profiles` | `candidate_ids`（1～100） | `service.get_candidate_profiles` → 结构化基础档案 | 即使请求混入 B 的 ID，也不能返回 B 的档案 |

**请求/返回实例（结构示意，`call_id` 每次不同）：**

```python
result = await client.call_tool(
    "filter_candidates",
    {"filters": [{"field": "region", "operator": "eq", "value": "上海"}]},
)
assert result.is_error is False                         # MCP 调用层
assert result.structured_content["ok"] is True          # 业务封装层
assert result.structured_content["data"] == ["C001"]    # 查询结果层
```

三个断言是三层语义，别把 `is_error=False` 错当“业务一定成功”。现有 `backend/tests/test_talent_mcp_server.py::test_mcp_tools_reuse_existing_service_and_result_protocol` 就是此接口的可执行例子。**作业二的 Tool Annotation 是发现时的元数据**，不会替代以上服务端权限；详细改法放到第3章。

### 模块 D：执行器和业务 Service——真正执行、真正过滤

**职责分工：** `ToolExecutor` 管调用可靠性和结果协议；`TalentToolService` 管具体业务数据；`query_plan.py` 管结构化条件的 SQL 编译。别把三者合并成“一个 MCP Tool”。

**代码定位：** `backend/app/talent_tools.py:22-35`（Context/Policy）、`:75-174`（Executor）、`:177-263`（Service）；`backend/app/query_plan.py:30-47`（FilterCondition）、`:129-150`（SQL 构建）。

```python
@dataclass(frozen=True)
class TalentToolContext:
    tenant_id: str
    permission_scopes: tuple[str, ...]
    actor_id: str = "unknown"
    run_id: str = "unknown"

# Service 中的业务入口

def filter_candidates(self, filters: list[FilterCondition], *, context: TalentToolContext) -> list[str]:
    self._check_context(context)
    plan = QueryPlan(task_type=TaskType.FIND_TALENT, filters=filters)
    with self.session_factory() as db:
        return select_candidate_ids(db, plan, tenant_id=context.tenant_id)
```

**逐跳追数据：** `filters` 首先由 `FilterCondition` 校验字段/比较符/值类型；`QueryPlan` 表达要找人才；`select_candidate_ids` 调 `build_candidate_statement`，强制拼入 `EmployeeProfile.tenant_id == tenant_id` 和 `employment_status == "active"`；字段从 `FILTER_FIELD_REGISTRY` 白名单取出，按 operator 编译条件；最后 `SELECT employee_no ... ORDER BY employee_no`。以 region=上海为例，租户 A 的 `C001` 满足；租户 B 的 `C002` 即便也在上海，仍被 tenant 条件剔除。**不是先查全库、拿到结果后再筛掉 B。**

再看执行器（提炼真实逻辑，不是建议新增代码）：它取每个 Tool 的 `ToolPolicy(max_attempts=2, timeout_seconds=3.0, backoff_seconds=0.05)`；熔断器先判断是否允许，在线程池调用 operation；成功返回 `_finish(..., status="succeeded", data=data)`；`ValueError/PermissionError` 映射为不可重试错误；超时/`TransientToolError` 可重试；`_finish` 写审计并返回固定外壳：

```python
{"ok": error is None, "data": data, "error": error,
 "meta": {"tool_name": name, "call_id": call_id,
          "attempts": attempts, "degraded": degraded}}
```

审计记录含 `tenant_id`、`actor_id`、`run_id`、工具名、参数**键名**、状态、耗时、错误码。这说明 Tool 的**业务读取**与执行过程的**运行状态/审计写入**不是同一概念；对第3章 Annotation 的真实性很重要。另一个工程限制：`future.result(timeout=3)` 到时只停止等待，不保证已启动的后端线程/外部 IO 立即停止；重试可能与旧请求并行。因此写操作不能照搬这个重试策略。

在四种业务方法里，`lookup_job_descriptions` 按可信 tenant 查 active JD 并作名称相似匹配；`get_candidate_profiles` 同时限定 tenant、ID 集合和 active；`search_candidate_evidence` 把 `tenant_id` 与 `permission_scopes` 传给 evidence_provider，再检查 evidence pack 的 `schema_version="2.0"`。**当前文件没有证明 evidence_provider 已把任意外来 candidate_ids 与授权候选集合取交集；要追下游实现或加端到端隔离测试。**

**本模块问题诊断：** 输入 DSL 报 `invalid_argument`，先查 `FilterCondition`、字段注册表和 operator；返回 `permission_denied`，先查 Context；返回 `dependency_unavailable`，先查 evidence_provider/超时；业务 `ok=false` 时不要只看 MCP 层的 `is_error`。

### 模块 E：Resources——Host 按 URI 读取，不经 ToolExecutor

**职责：** 让应用按稳定 URI 取岗位 JD、候选人档案或评估规则。Resource 是数据读取契约；Tool 则是带输入 Schema 的操作契约。应用通常决定何时读取和放入上下文；URI 自身不能代表授权。

**代码定位：** `backend/app/talent_mcp_server.py:118-146` 与 `backend/app/talent_tools.py:219-236,244-261`。

```python
@server.resource("talent://jobs/{job_code}", title="岗位 JD", mime_type="application/json")
def job_description(job_code: str) -> dict[str, Any]:
    item = service.get_job_description(job_code, context=context_provider())
    if item is None:
        raise ResourceNotFoundError("岗位 JD 不存在或当前身份无权访问")
    return item
```

**逐行解读：** URI 的 `{job_code}` 被 SDK 绑定为函数参数；`context_provider()` 提供可信身份；Service 的 SQL 同时匹配 `job_code`、`tenant_id=context.tenant_id`、`status="active"`，返回 `{job_code,name,version,content}`；不存在和越权都变成同一类 NotFound，避免明确暴露其他租户的存在性。该 Resource **直接调 Service**，不经过 ToolExecutor 的结果包装/工具审计。

另两个资源：`talent://candidates/{candidate_id}/profile` 通过 `get_candidate_profiles([candidate_id])` 读当前租户在职档案；`talent://policies/evaluation/current` 返回静态 Markdown 评估规则。后者当前没有租户敏感数据；若以后做租户定制，必须补身份检查。Resource Template 在 `list_resource_templates()` 中；固定政策 URI 在 `list_resources()` 中：

```text
list_resources           → talent://policies/evaluation/current
list_resource_templates  → talent://jobs/{job_code}
                           talent://candidates/{candidate_id}/profile
read_resource            → talent://jobs/JD-AI-001（具体 URI）
```

`read_resource` 的 JSON 当前作为 `TextResourceContents.text` 返回，测试用 `json.loads(...)` 解码。`backend/tests/test_talent_mcp_server.py::test_resource_templates_apply_the_same_tenant_boundary` 同时读 A 的 JD/C001，并断言 B 的 JD/C002 抛 `MCPError`。**第2章版本 URI 作业就是在本模块增加新的模板与版本语义，而不是新增一个 Tool。**

### 模块 F：Prompts——用户/应用选择的评估模板

**职责：** 告诉 Host“人才评估请求可以如何组织语言”；不会自动访问 JD、自动检索候选人或赋予用户数据权限。

**代码定位：** `backend/app/talent_mcp_server.py:148-150`：

```python
@server.prompt(name="talent_assessment", title="人才评估与推荐")
def talent_assessment(job_code: str, request_text: str) -> str:
    return f"按照岗位 {job_code} 处理人才评估与推荐请求：{request_text}"
```

Client `list_prompts()` 发现 `talent_assessment`；`get_prompt("talent_assessment", {"job_code":"JD-AI-001", "request_text":"筛选上海候选人"})` 返回一条包含这两项的 user 消息（见 `test_prompt_renders_user_selected_talent_request`）。当前代码只是插值，没有查岗位是否存在；`request_text` 是用户输入，Host 仍需防提示注入，不能把模板文本当成授权证明。

### 模块 G：Client 侧适配 LangGraph——能力能执行，不等于 Agent 已完整

**职责：** 发现远程 MCP Tool，包装成 LangChain Tool，交给图的执行节点。该模块在 Host 一侧，不是另一个业务 Server。

**代码定位：** `backend/app/talent_mcp_client.py:11-17`；端到端示例见 `backend/scripts/verify_talent_mcp.py::verify_langgraph`。

```python
def build_mcp_tool_graph(tools: Sequence[BaseTool]) -> Any:
    builder = StateGraph(MessagesState)
    builder.add_node("mcp_tools", ToolNode(list(tools)))
    builder.add_edge(START, "mcp_tools")
    builder.add_edge("mcp_tools", END)
    return builder.compile()
```

**逐行解读：** `tools` 已是 Adapter 发现并转换好的 `BaseTool`，不是 Server 端 Python 函数；`MessagesState` 存对话消息；`ToolNode` 消费上一条 `AIMessage.tool_calls`，执行指定工具并追加 `ToolMessage`；两条边表示只执行一次后结束，没有 LLM 选择节点或循环。

脚本中 `async with MCPAdapter(url) as adapter`、`await adapter.list_tools()`、`graph = build_mcp_tool_graph(tools)`；随后人为构造 `AIMessage(tool_calls=[{"name":"filter_candidates", "args":{"filters":[...]}, ...}])`，`graph.ainvoke(...)`，从末尾 `ToolMessage.artifact["structured_content"]["data"]` 取 `C001`。所以它证明 **HTTP → MCPAdapter → BaseTool → ToolNode → MCP Server → Service → 返回** 这条链可工作，但既不证明 LLM 会正确选 Tool，也不证明当前 demo Graph 已携带生产用户 token：脚本启动的是 `demo_context` 的无鉴权 Server。受保护服务的 Adapter 需要明确的逐用户 token 传递与隔离设计。

### 1.1 用一个案例把七个模块串起来

| 顺序 | “筛选上海候选人”的实际流转 | 当前源码/测试证据 |
|---|---|---|
| 1 | Host 构造或接收 tool_call；Client 调 MCP Tool | `verify_langgraph` 的 `AIMessage.tool_calls`；`MCPAdapter.list_tools()` |
| 2 | HTTP 请求身份校验；从 token 映射 tenant A（若使用受保护装配） | `authenticated_talent_context`；`_protected_server` 测试 |
| 3 | Tool 外壳校验 `filters`、取 Context | `talent_mcp_server.py::filter_candidates` |
| 4 | 执行器负责调用策略、结果包装、审计 | `ToolExecutor.execute/_finish` |
| 5 | Service 将 Context 与 Filter DSL 编译进 SQL | `TalentToolService.filter_candidates` → `select_candidate_ids` |
| 6 | 查询返回 `C001`，B 的 `C002` 被租户条件拒绝 | `test_mcp_tools_reuse_existing_service_and_result_protocol` |
| 7 | 结果变成 MCP `structured_content`，图内是 `ToolMessage` | `test_talent_mcp_client.py` 与 `verify_langgraph` |

**注意：第2行的身份测试和第7行的 demo Graph 属于两个不同的验证装配。** 把两者连成生产端到端安全链，还需要给 Adapter 添加正确的 token 传递与并发隔离测试，不能因为图画在一起就声称仓库已实现。


## 2. 作业一：岗位 JD 增加带版本的 Resource URI，并验证租户隔离

**任务卡：目标**是让 Host 能按明确版本读取岗位内容；**改动点**是 MCP Resource Template、必要时 Service/数据库、测试；**最重要的验收**是版本语义清楚且猜到 B 租户 URI 仍读不到。建议顺序：先读模块 E → 写失败测试 → 实现最小版 → 测错版本/跨租户 → 决定是否需要真历史库。

**从接口入口重走一遍：** `Client.read_resource("talent://jobs/JD-AI-001/versions/2")` 发 `resources/read` → MCPServer 模板绑定 `job_code=JD-AI-001, version=2` → 新处理函数检查版本格式 → `context_provider()` 得可信租户 → `TalentToolService.get_job_description` 以 tenant+job_code+active 查当前行 → 对比数据库 `version` → SDK 序列化 JSON 文本或抛统一 NotFound。此链路中**不存在** ToolExecutor 和 Tool Annotation；租户条件真正位于 Service SQL。

### 2.1 先做设计判断，不要只改字符串

当前 `JobDescription` 位于 `backend/app/models.py`，`job_code` 全局 `unique=True`，一条记录上有 `version` 整数和 `content`，**没有** `(job_code, version)` 历史快照表。现有 `get_job_description(job_code, *, context)` 只查当前 active 行，按 tenant 过滤。因此有两个合理层次：

| 实施层次 | 能保证什么 | 不能保证什么 |
|---|---|---|
| 本次最小作业：带版本 URI + 当前行版本精确匹配 | `/versions/2` 在当前是2时可读；版本错/跨租户返回同样“不可见” | 更新为3后，`/versions/2` 不再可读；它**不是**历史版本归档/永久引用 |
| 真正历史版本服务：新增不可变 JD 版本表 | 已发布的 `/versions/2` 始终指向版本2内容，可审计、对比 | 需要数据库迁移、写入事务、保留策略和更全面的授权测试 |

**推荐：** 先完成最小层并在 README/验收中明确“仅当前版本”；如果老师的“版本 URI”隐含历史回溯，再做历史表，不要通过返回当前内容冒充旧版本。`job_code` 全局唯一也不适合未来“各租户允许相同岗位编码”；真正多租户模型宜将唯一键改为 `(tenant_id, job_code)`，历史表用 `(tenant_id, job_code, version)`。

### 2.2 从测试开始：证明版本正确且绝不串租户

在 `backend/tests/test_talent_mcp_server.py` 利用已有 `_server(tmp_path)`（固定租户 A）增加测试。先运行，预期因模板未注册失败；再做最小实现：

```python
# 需要的 pytest、anyio、Client、MCPError、json、TextResourceContents 已在该文件导入

def test_versioned_job_resource_current_version_and_tenant_boundary(tmp_path):
    async def scenario():
        async with Client(_server(tmp_path)) as client:
            templates = await client.list_resource_templates()
            good = await client.read_resource("talent://jobs/JD-AI-001/versions/2")
            with pytest.raises(MCPError):
                await client.read_resource("talent://jobs/JD-AI-001/versions/1")
            with pytest.raises(MCPError):
                await client.read_resource("talent://jobs/JD-AI-OTHER/versions/1")
            with pytest.raises(MCPError):
                await client.read_resource("talent://jobs/JD-AI-OTHER/versions/2")
            return templates, good

    templates, good = anyio.run(scenario)
    assert "talent://jobs/{job_code}/versions/{version}" in {
        t.uri_template for t in templates.resource_templates
    }
    body = json.loads(good.contents[0].text)
    assert body["job_code"] == "JD-AI-001"
    assert body["version"] == 2
    assert body["content"] == "负责企业知识库与 Agent 应用建设"
```

建议再加 `versions/0`、`versions/-1`、`versions/foo`、`versions/0002`、过长数字、未知岗位、停用岗位；对租户 A 与 B 用**相同** `job_code` 的更强反例需先解决现有全局 unique 约束，不能硬塞重复编码到当前模型。现有租户 B `JD-AI-OTHER` 足以证明“猜对对方 URI 仍无法读”。若 `MCPError` 包装随 SDK 版本变化，应以本项目现有失败测试的断言风格为准。

### 2.3 最小后端改法：入口→参数→服务→数据库

在 `backend/app/talent_mcp_server.py` 的现有 `job_description` 模板后注册**新的** Resource Template；保留旧 URI 以免旧 Host 失效（是否将旧 URI 定义为“最新”应写入契约）。

```python
import re  # 放在文件 import 区

@server.resource(
    "talent://jobs/{job_code}/versions/{version}",
    title="岗位 JD（当前版本精确读取）",
    mime_type="application/json",
)
def job_description_version(job_code: str, version: str) -> dict[str, Any]:
    if re.fullmatch(r"[1-9][0-9]{0,9}", version) is None:
        raise ResourceNotFoundError("岗位 JD 不存在或当前身份无权访问")
    item = service.get_job_description(job_code, context=context_provider())
    if item is None or item["version"] != int(version):
        raise ResourceNotFoundError("岗位 JD 不存在或当前身份无权访问")
    return item
```

这里的 `version` 是 URI 字符串而不是数据库版本对象；限制为正整数、十进制规范形式和有限长度。**真正的授权判定在 `service.get_job_description` 的 SQL**：`job_code` + `tenant_id=context.tenant_id` + `status="active"`；URI 中即便以后添加 tenant 片段，也不能替代 token tenant。不要先全局查询再依据 `item["tenant"]` 在应用层判断；不要将跨租户与不存在返回两种可区分的资源内容。

如需把“版本相等”也下推数据库，可将 service 扩成 `get_job_description(job_code, *, context, version: int | None = None)`，只在 `version is not None` 时追加 `JobDescription.version == version`，响应保持同样的 NotFound。这样查询边界更易审计，且避免读后版本变化竞态（单条当前行查询的快照语义仍需由事务隔离保证）。最小实现不必改 service API，但需如实标注局限。

### 2.4 如果要真能读历史版本，具体该补哪些层

1. 数据库迁移：新增 `job_description_versions`：`id`、`tenant_id`、`job_code`、`version`、`name`、`content`、`created_at`、`published_at`；唯一约束 `(tenant_id, job_code, version)`，并依 tenant+job+version 建索引。**不可只在原 `job_descriptions` 行上递增 version**。
2. 写入路径：发布新 JD 时，在同一事务插入不可变版本行并更新当前指针/状态；并发发布加乐观锁/唯一键冲突处理；禁止静默覆盖历史内容。迁移时对现有当前行回填**仅它当前的版本**，不要凭空生成 v1。
3. Service 增 `get_job_description_version(job_code, version, *, context)`，`SELECT ... WHERE tenant_id=:trusted_tenant AND job_code=:job_code AND version=:version`；再根据业务策略检查是否公开/生效，而非仅依“版本存在”。Resource 用该方法，不必改变现有无版本 URI。
4. 用租户 A/B 相同岗位编码（完成复合唯一迁移后）、同版本不同内容测试隔离；v1→v2 更新后两个 URI 内容保持各自不变；未发布/撤销版本不可访问；历史授权/删除和缓存隔离都要验证。

**验收语句：** “我新增了模板、拒绝非法/不匹配版本，租户 B 的 URI 对 A 返回不可见；当前实现仅可读当前版本。历史版本需新增不可变快照表。”如果完成历史表，才可改说“支持历史回溯”。

## 3. 作业二：`filter_candidates` 的 Tool Annotation

**任务卡：目标**是让 Client 在 `tools/list` 发现这个工具的行为提示；**改动点**只在 `@server.tool(...)` 的 metadata 与发现测试；**不应改变**筛选 SQL 或调用链。先辨析只读与审计写入是否冲突，再写发现测试、注册 Annotation、复测既有调用。

**从接口入口重走一遍：** `Client.list_tools()` → MCPServer 序列化工具名称、Schema 与 `annotations` → Host 可参考这些提示；真正 `Client.call_tool("filter_candidates", ...)` 仍走 `context_provider → ToolExecutor → TalentToolService → select_candidate_ids`。如果只写 Annotation 而 SQL 不按租户过滤，仍然是不安全的 Tool。

### 3.1 先认识 Annotation：它是给谁看的、管什么

`@server.tool(...)` 是**注册工具**，`annotations=ToolAnnotations(...)` 是给 MCP Client/Host 在 `tools/list` 发现阶段读取的**行为提示**。它可以帮助 Host 决定怎样向用户解释风险、何时请求确认，但它**不是**权限校验器、事务约束或数据库只读开关：不可信 Server 可以谎报，Host 不能凭提示授予权限。真实的数据隔离仍由可信 token → `TalentToolContext` → Service 查询条件/数据库权限保证。

先把装饰器的两个层次分开：

```python
@server.tool(
    structured_output=True,          # 注册选项：把返回值按结构化结果暴露；不属于 ToolAnnotations
    annotations=ToolAnnotations(     # 元数据：关于工具行为的提示
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
)
```

`ToolAnnotations` 在本项目安装的 `mcp_types._types` 中**可设置的字段共五个**。Python 代码写 `snake_case`；MCP JSON 中对应 `camelCase`（例如 `read_only_hint` ↔ `readOnlyHint`）。布尔字段类型是 `bool | None`：显式 `True`/`False` 是作出声明；不填或设 `None` 表示**未声明**，不是“实际行为等于该默认值”。规范为未声明的提示规定如下保守解释；不要把反序列化后的 `None` 与显式 `False` 混淆。

| Python 参数 / 协议字段 | 可设置值；未声明时的语义 | `True` 和 `False` 究竟说了什么 | 本作业为什么这样写 |
|---|---|---|---|
| `title` / `title` | `str` 或 `None`；未设置则没有此显示标题提示 | 不是布尔开关；可写 `title="筛选候选人"` 供客户端展示。标题不改变工具名、入参或权限 | 可选；不必为完成作业而添加 |
| `read_only_hint` / `readOnlyHint` | `bool` 或 `None`；未声明按 `false` 理解 | `True`：**不修改环境**；`False`：不能宣称只读，可能写入。只读的定义必须先明确审计、遥测等副作用是否计入环境 | 业务查询不修改候选人/JD 数据，因此示例标 `True`；但下文审计写入是必须披露的前提 |
| `destructive_hint` / `destructiveHint` | `bool` 或 `None`；未声明按 `true` 理解 | `True`：可能破坏性更新（如删除、覆盖）；`False`：如果有更新，**只做新增式更新**，绝不意味着“完全不修改”。只在 `read_only_hint=False` 时有区分意义 | 示范为 `False`，但对已经声明只读的工具是冗余提示，不能据此证明只读 |
| `idempotent_hint` / `idempotentHint` | `bool` 或 `None`；未声明按 `false` 理解 | `True`：相同参数重复调用**不会对环境产生额外效果**；`False`：不承诺这一点。只在 `read_only_hint=False` 时有区分意义 | 示范为 `True`，但只读场景同样冗余；不承诺两次筛选结果或审计记录完全相同 |
| `open_world_hint` / `openWorldHint` | `bool` 或 `None`；未声明按 `true` 理解 | `True`：可能和开放世界的外部实体交互，如互联网搜索；`False`：交互范围封闭，如限定在受控内部数据库。这是**交互范围**，不是是否只读，也不能仅凭是否走 HTTP 判断 | 此工具仅访问授权租户内的受控候选人数据时标 `False`；若将来扩成外部简历平台搜索须重评 |

> **注意两个“默认”的层次：** 类定义的字段默认是 Python 的 `None`（未提供）；上表“按 false/true 理解”是协议给消费者的**语义默认**，不是 SDK 自动把字段填成布尔值，更不说明真实实现确实如此。为了让测试和 Inspector 看见确定的声明，作业里四个布尔提示都显式设置。

### 3.2 用三个反例建立判断能力

- **内部数据库新增一条记录**：`read_only_hint=False, destructive_hint=False` 可以成立，因为“只新增”仍然是写入；如果每次都新增一行，则 `idempotent_hint=False`，如果以稳定业务键去重且重复调用无额外环境效果，才考虑 `True`。
- **删除或覆盖档案**：`read_only_hint=False, destructive_hint=True`；即使实现了幂等删除，`destructive_hint` 仍可为 `True`。所以“幂等”≠“安全/无破坏”。
- **互联网搜索**：可以是 `read_only_hint=True, open_world_hint=True`（假定它不修改被讨论的环境）。所以“只读”≠“封闭世界”；反过来 `open_world_hint=False` 也不自动阻止更新内部数据库。

**回到本例逐项自问：** ①筛选是否只读业务数据？②是否会删除或覆盖？③重复调用有没有新增环境效果，审计是否每次新增？④是否仅访问受控内部数据？⑤这些声明会不会被误当作授权？回答完再选参数。尤其不要用 `destructive_hint=False` 推导只读，也不要用 `idempotent_hint=True` 推导“响应永远一致”；候选人资料更新、筛选基准日期变化都可能改变响应。

### 3.3 先从实际副作用出发

`filter_candidates` 的业务查询是只读 SQL；但是 `ToolExecutor` 会向 `audit_sink.write` 写审计（默认内存 sink，生产可能 `SqlAuditSink` 写 DB），而熔断器内部也更新状态。协议的 `ToolAnnotations` 是**客户端参考提示，不是安全保证**，`readOnlyHint` 的规范含义是“不修改环境”；因此不能无条件宣称它“绝无任何修改”或把 Annotation 当权限控制。若老师按“无外部修改行为”指业务数据不变，应在验收中明确审计/运行状态例外；若要求字面意义**完全无外部修改**，要先分离审计写入/遥测并重新评估熔断器等副作用，再决定是否可声明 read-only。尤其不能为了让测试变绿，关闭必需的合规审计。

`idempotentHint` 在规范中只对 `readOnlyHint=false` 的工具有实际区分意义；只读操作本就没有额外环境副作用。重复调用参数相同，不等于结果一定相同：候选人数据或年龄基准日可能变化。`destructiveHint=false` 对 read-only 也属冗余描述；`openWorldHint=false` 只有在本查询确实只触达受控内部数据库时适用。声明这些时应解释语义和前提，而不是机械勾选。

### 3.4 最小改动和发现测试

`mcp[cli]` 在当前仓库的 SDK 源码中，`MCPServer.tool(..., annotations: ToolAnnotations | None = None, ...)`；`ToolAnnotations` Python 字段采用 snake_case，线协议序列化为 camelCase。**在确认并记录上述语义前提后**，可修改 `backend/app/talent_mcp_server.py`：

```python
from mcp.types import ToolAnnotations  # 放到 import 区

@server.tool(
    structured_output=True,
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
)
def filter_candidates(
    filters: Annotated[list[FilterCondition], Field(max_length=20)],
) -> dict[str, Any]:
    """按结构化条件筛选授权租户内的候选人。"""
    context = context_provider()
    return executor.execute(
        "filter_candidates",
        lambda: service.filter_candidates(filters, context=context),
        context=context,
        arguments={"filters": filters},
    )
```

此处保留了原函数体，改动只有装饰器参数。Annotation 不实现只读，真实只读要靠代码审查/数据库权限保证。若外部审计写入被纳入“环境修改”，应谨慎考虑 `read_only_hint=False` 并向老师解释为何与作业文字存在冲突，或先征求规范口径；不要对外发布自己知道不真实的 metadata。

在同文件已有的 `test_server_advertises_talent_tools_resources_and_prompt` 后加独立断言，先红后绿：

```python
def test_filter_candidates_advertises_read_only_hints(tmp_path):
    async def scenario():
        async with Client(_server(tmp_path)) as client:
            return await client.list_tools()

    listed = anyio.run(scenario)
    item = next(tool for tool in listed.tools if tool.name == "filter_candidates")
    assert item.annotations is not None
    assert item.annotations.read_only_hint is True
    assert item.annotations.destructive_hint is False
    assert item.annotations.idempotent_hint is True
    assert item.annotations.open_world_hint is False
```

再运行既有 `test_mcp_tools_reuse_existing_service_and_result_protocol`，验证装饰器元数据没改变功能。**不要**写测试“有 Annotation 就能越权”，那是错误安全模型。生产中如 `SqlAuditSink` 被视作外部改动，应另写审计副作用测试，并在安全评审中解释/修正该声明。

## 4. 作业三：用 Inspector 验收并保存能力清单

**任务卡：目标**是将服务端实际公布的能力形成可核对的人工验收证据；**改动点**是本地调试入口（人才 Server 工厂需包装成模块级 `mcp`）与清单，不是凭截图替代自动测试。顺序：确定连接方式 → 启动 → 分别做发现/调用/资源读取/Prompt 渲染 → 记录异常与脱敏证据。

### 4.1 先搭好可调试的服务，不要连错端口

已有的 `backend/samples/lesson13/codestats_mcp_v2.py` 暴露模块级 `mcp`，可在 `backend` 目录用 PowerShell：

```powershell
$env:CODE_STATS_ROOT = (Resolve-Path .).Path
uv run --no-sync mcp dev samples/lesson13/codestats_mcp_v2.py
```

它只证明 SDK v2 示例 Tool/Resource，**并不包含人才 Prompt**。`backend/app/talent_mcp_server.py` 是工厂，不暴露可供 `mcp dev` 直接导入的模块级人才 `mcp`；`verify_talent_mcp.py` 测完即退出，不是持久在线调试器。要验收人才服务，建议新建**仅用于本地演示**的 `backend/samples/lesson13/talent_mcp_inspector.py`：

```python
from pathlib import Path
from tempfile import TemporaryDirectory
from app.talent_mcp_server import build_talent_mcp_server
from app.talent_tools import ToolExecutor
from scripts.verify_talent_mcp import build_demo_service, demo_context

_demo_dir = TemporaryDirectory(prefix="lesson13-inspector-")
_service = build_demo_service(Path(_demo_dir.name) / "talent.db")
mcp = build_talent_mcp_server(
    _service, ToolExecutor(), context_provider=demo_context
)
```

此模块载入时创建租户 A 的隔离演示库，进程结束后删除临时目录。运行 `uv run --no-sync mcp dev samples/lesson13/talent_mcp_inspector.py`；若 CLI 的模块导入环境找不到 `app`，在 backend 目录设置 `$env:PYTHONPATH=(Resolve-Path .).Path` 再启动。这个 demo_context **不经过 HTTP Access Token**，只供能力清单/功能观察；不能拿它证明 HTTP OAuth 安全。CLI 输出的 Inspector 页面 URL 中可能有 API token，用于本地页面连接，勿把调试 token 发到公网/截图。Inspector 页面端口（通常6274）不是业务 `/mcp`。

如果要人工验收 HTTP 鉴权，还需单独部署受保护 `streamable_http_app()`，在 Inspector 中选 Streamable HTTP、指向实际 `/mcp` URL，并配置匹配 audience/scope/租户的 Bearer token；不要在截图里暴露 token。是否能在界面填 Authorization 与 Inspector/CLI 具体版本有关，若不可用，用集成脚本加受保护客户端测试，不应因此关闭鉴权。

### 4.2 建议逐项操作，记录观察值而非仅打勾

1. **连接与协议：** 启动 Inspector，选 STDIO 或 HTTP；确认连接成功、实际协议修订号；保存 CLI 启动命令（隐去敏感参数）。
2. **Tools 页：** 列出精确四个名称；展开 `filter_candidates` 看 input schema 的 filters/field/operator/value 约束以及 annotation 的 wire 字段 `readOnlyHint=true`、`destructiveHint=false`、`idempotentHint=true`；调用 region=上海，检查 `ok=true`、`data=["C001"]`，没有 `C002`。证据 Tool 若演示 service 未配置 evidence_provider，会返回统一失败；这是**演示夹具局限**，不能谎称已验证成功路径。成功路径已有 `test_evidence_tool_preserves_candidate_evidence_pack_v2` 的专用 provider 测试。
3. **Resources 页：** 固定 `talent://policies/evaluation/current` 在普通 Resources；岗位与候选人在 Resource Templates；读 A 的 `JD-AI-001`、`C001`，尝试 B 的 `JD-AI-OTHER`、`C002` 应不可见；完成作业一后读 `/versions/2` 成功、错版本/跨租户失败。记录 URI、MIME、简化后的返回摘要，不截取私人数据全文。
4. **Prompts 页：** `talent_assessment(job_code, request_text)` 填入示例，核对渲染 user 消息含 JD 编码和请求原话；它不等于执行岗位评估。
5. **鉴权与 Graph：** Inspector 不代替自动化。附上 `test_streamable_http_enforces_token_scope_and_audience` 结果（401/403/401）、Graph 测试/脚本结果；注明测试方式、日期、SDK/Inspector 版本、未测项和风险。

### 4.3 可直接复制填写的能力验收清单（这是**模板，不是完成记录**）

| 编号 | 能力/边界 | 操作/输入 | 预期 | 实测/截图或日志证据 | 状态 |
|---|---|---|---|---|---|
| 01 | 启动/协商 | STDIO 或 HTTP 连接 | 连接成功并记录协议号 | 待填写 | 未执行 |
| 02 | Tool 发现 | Tools/list | 4 名称、Schema | 待填写 | 未执行 |
| 03 | filter 注解 | 展开该 Tool | readOnly/非破坏/idempotent 等与真实副作用口径一致 | 待填写 | 未执行 |
| 04 | Tool 调用 | 上海 region=eq | A 返回 C001、不含 C002 | 待填写 | 未执行 |
| 05 | 固定 Resource | 列表+读取 policy/current | Markdown 文本 | 待填写 | 未执行 |
| 06 | Resource Templates | 列表+读取两个模板 | JD 与 C001 可读；B 不可见 | 待填写 | 未执行 |
| 07 | 版本 URI（新增后） | versions/2、versions/1、B 版本 | 正确版本可读，错误/越权不可见 | 待填写 | 未执行 |
| 08 | Prompt | talent_assessment | 渲染含岗位码/请求的 user 消息 | 待填写 | 未执行 |
| 09 | HTTP token/scope/audience | 无 token/无 scope/错 aud | 401/403/401 | 待填写（自动化补证） | 未执行 |
| 10 | MCPAdapter→ToolNode | 集成脚本 | 发现4 Tool；C001；ToolMessage success | 待填写（自动化补证） | 未执行 |

保存记录时写明运行日期、Git commit/工作区状态、Python/`mcp`/`langchain` 版本、Inspector 连接方式、异常、解决办法与复测结论；**Inspector 的 screenshot 要脱敏候选人姓名、token 与企业资料**。验收清单可作为本指南的一节，也可复制为 `lesson13-mcp-inspector-acceptance.md` 现场逐项填写。

## 5. 本次可复现的测试路径与常见问题

### 5.1 顺序和命令（在 `backend` 目录执行）

```powershell
Set-Location 'D:\Code\K_Course\talent-eval-agents_learning\backend'
$testTemp = '.pytest-lesson13-' + [guid]::NewGuid().ToString('N')
uv run --no-sync pytest -q "--basetemp=$testTemp" `
  tests/test_codestats_mcp_v2.py `
  tests/test_talent_mcp_server.py `
  tests/test_talent_mcp_client.py
uv run --no-sync python scripts/verify_talent_mcp.py
```

上一版撰写时，**未修改业务代码的现有三文件定向测试**以项目内 `--basetemp` 运行得到 `15 passed in 4.04s`。该结果只是已有功能的基线，不是这次文档重写后的新增测试。这是基线，不是作业一/二新增测试通过的证明；`verify_talent_mcp.py` 和 Inspector 本次未实际运行，不应写成已通过。加入作业代码/测试后重跑这三文件和 `tests/test_talent_tools.py`，最后视环境运行整个后端测试。README 的历史通过数字是历史记录，不代表今天的测试结果。

### 5.2 遇到问题时的“现象→原因→定位→解决→复测”表

| 现象 | 优先定位 | 解决与复测 |
|---|---|---|
| `uv` 报缓存目录拒绝访问 | 当前执行账号对 `C:\Users\Kalav\AppData\Local\uv\cache` 权限不足，不是 MCP 代码报错 | 用授权环境运行或把 `UV_CACHE_DIR` 指向自己可写的临时目录；不要删除他人的全局缓存；重跑原命令。本文第一次沙箱运行即遇到此问题。 |
| pytest `tmp_path` 生成失败、`pytest-of-...` PermissionError | 默认系统 temp 目录属另一个 Windows 用户 | 显式 `--basetemp` 指向**新建且确认属于当前工作区**的测试目录（上面用 GUID 生成唯一名称）；本文首次越沙箱测试出现10个 setup errors、5 passed，第二次改 basetemp 后15 passed。切忌把 setup error 误判成业务回归失败。 |
| `backend/.venv/Scripts/python.exe` 直接启动失败 | 该 venv 记录的原始 Python 路径在当前运行账号下不存在 | 优先 `uv run --no-sync ...` 使用项目环境；若要迁移环境，按团队流程重建 venv，不要靠临时复制解释器糊弄。 |
| Resource 在 `list_resources` 中找不到 | 模板 URI 应从 `list_resource_templates()` 查，固定 URI 才在 `list_resources()` | 分别列出两类；用完整实例 URI `read_resource(...)` 验证，而非直接读带花括号的模板。 |
| 成功调用 Tool 却业务 `ok=false` | MCP 层 `is_error` 与统一业务封装是两层错误语义 | 同时查看 `result.is_error`、`result.structured_content["ok"]`、`error.code` 和 `meta`；审计是否记录失败。 |
| B 租户 JD 可见/不同身份得到相同私有数据 | 测试夹具用固定 demo_context、误从参数/headers 读 tenant，或 SQL 未加 tenant | 用受保护 HTTP token claims 实测，再检查 service SQL `tenant_id=context.tenant_id`；对不同租户同编码数据的长期模型用复合唯一键。 |
| 无 token/scope/audience 返回 401/403/401 | 这是预期阻断；如果全部200则未使用受保护装配 | 核实 `token_verifier`、`AuthSettings(validate_token_resource=True, required_scopes=["talent:read"])` 与 `authenticated_talent_context` 同时传入；确认 URL 与 token resource 指向同一资源。 |
| `MCPAdapter` 发现到工具但生产调用 401 | 演示 Graph 使用无鉴权 server；受保护 Server 不会自动继承 Host 用户 token | 将身份/凭证传递明确设计在 Host/Client 层；拒绝在进程级共享跨用户 token；增加 A/B 并发隔离集成测试。 |
| `mcp dev` 看得到 CodeStats，找不到人才 Prompt | CodeStats 示例只有 Tool + Resource，人才模块只有工厂 | 用 4.1 节的本地人才 Inspector 入口，别把临时 `verify` 脚本当常驻服务。 |
| `filter_candidates` 标 read-only 与审计写入冲突 | 语义口径未事先明确；`SqlAuditSink` 确实写审计数据库 | 区分业务数据与遥测/审计，并如实在设计评审记录；若以“零环境修改”解释则不能宣称 read-only，需调整架构/注解或请教师确认口径。 |

**额外值得审视的现有集成脚本细节：** `backend/scripts/verify_talent_mcp.py::verify_auth` 中名为 `true-audience` 的演示 token，其 `resource="https://auth.example.com/mcp"`，但 `AuthSettings.resource_server_url` 是 `http://127.0.0.1:18081/mcp`；两者不相同，因此它事实上不是有效 audience 的正例。若想证明“正确 audience 能通过”，应把该 token 的 resource 改为与 `resource_server_url` 相同，再用真正 MCP 初始化/工具调用验证 200/业务数据；仅对 `{}` 发 POST，即使认证通过也可能由于 JSON-RPC 请求体无效返回 4xx，不能用它作为成功调用的断言。已有 `test_streamable_http_token_claims_reach_business_service` 通过有效 token 执行 `get_candidate_profiles` 是更有说服力的正向链路测试。修改脚本时先写失败测试，避免把测试数据名称当事实。

### 5.3 高级工程风险：知道局限才算掌握

- `ToolExecutor` 用 `future.result(timeout=...)` 控制等待，`pool.shutdown(wait=False, cancel_futures=True)` 不一定能停止已经运行的线程/外部 IO；超时重试可能出现重复调用。只读查询比写操作容易重试，但下游仍需超时/取消和幂等契约。
- `search_candidate_evidence` 的候选 ID 是来自调用者的参数；Service 把 tenant/scopes 传给 evidence_provider，仍应在检索实现确认候选 ID 交集和租户向量过滤，不能因为输入 Schema 限制100个就认为已授权。
- `permission_scopes` 非空只是一道最低门槛；真正的字段、材料、行列权限需要策略映射。对敏感人才数据还要关注 token 过期/撤销、审计留痕、响应脱敏和缓存键带 tenant。
- Resource 的 URI 应稳定且语义明确：`/versions/2` 不能今天是内容 A、明天回源内容 B。若只是“当前版本过滤”，更新后返回 NotFound，不要缓存为历史快照。
- Annotation 是不可信提示，客户端不能依此省略用户授权、禁用隔离或做自动重试决定；`idempotentHint` 不代表结果恒定，更不自动保证执行器底层线程已停止。
- `ToolNode` 只执行已给出的 `tool_calls`；完整 Agent 还需模型消息生成、工具选择约束、循环终止条件、错误处理、身份绑定和观测。课程刻意使用最小图来把“协议接入”与“智能决策”拆开。

## 6. 对照课程目标的学习路线与自测题

### 6.1 建议用四轮掌握，而不是一次性背 API

1. **画图（半天）：** 不看文档默写本指南第0节图，逐个解释 Host/Client/Server、Tool/Resource/Prompt、STDIO/HTTP；打开 `talent_mcp_server.py` 核对所有入口。
2. **手动追链（半天）：** 对 `filter_candidates` 和 `talent://jobs/JD-AI-001` 各画从请求到 SQL/响应的箭头，指出 context 从何而来、哪个层会审计、哪个层不会。
3. **反例驱动（一天）：** 先加跨租户、错版本、无 token、错 audience 的失败测试，再加新模板/注解；区分 `is_error`、`ok=false` 和 HTTP 401/403。
4. **真实接入（半天）：** 本地 Inspector 实际读能力；运行 `verify_talent_mcp.py`；给 Graph 增加受保护 HTTP 的身份传播设计草图。保留截图/日志摘要，不保存秘密。

### 6.2 面试/复盘时你应能回答

- 为什么不把 `tenant_id` 放进 `filter_candidates` 参数？因为模型/客户端可伪造，必须由服务端验证的 token 映射；SQL 再强制收缩 tenant。
- Tool 与 Resource 有什么区别？Tool 是模型可发起的有 schema 的操作，Resource 是 Host 以 URI 为单位选择读取的内容；两者均须授权，是否执行/展示由 Host 控制。
- `list_resources` 没有 JD 是否代表失败？不是，JD 是 URI 模板，查 `list_resource_templates`。
- SDK v2 与协议 revision 是否相同？不是；SDK 是实现库的版本，连接时协商/指定的 protocol revision 是线协议版本。
- `readOnlyHint=true` 是否可替代数据库只读权限？不能；元数据只是提示，而且当前审计/熔断副作用需具体分析。
- 为什么只改 `/versions/{version}` URI 不等于历史版本？因为现表只有当前内容；旧版本数据不在持久化模型中。
- Graph 测试证明了什么、没证明什么？证明 Adapter→ToolNode 能执行远程工具并收到 ToolMessage；不证明 LLM 推理正确或生产身份传播已实现。

### 6.3 对照交付物的完成定义

- [ ] 作业一：版本 URI 在 `list_resource_templates` 发现；正确版返回、错误版/跨租户不可见；明确“只读当前”还是“真历史”。
- [ ] 作业二：`filter_candidates` 的 Annotation 在 `list_tools`/Inspector 可见；审计副作用的语义口径写入验收；业务行为及隔离测试不退化。
- [ ] 作业三：Inspector 验收清单由本人填写日期、版本、实测、脱敏证据；Tool、固定/模板 Resource、Prompt 均逐项验证。
- [ ] 安全：受保护 HTTP 的 token/scope/audience/租户正反路径；Graph 的生产身份传播另行设计/验证。
- [ ] 回归：新测试先失败再通过，现有定向测试及所需全套测试记录准确，不引用过时的 README 通过数字。

## 7. 源码与延伸阅读

**本地一手依据：** `backend/app/talent_mcp_server.py`（注册/身份）、`backend/app/talent_tools.py`（执行/业务）、`backend/app/query_plan.py`（SQL 边界）、`backend/app/models.py`（JD 版本数据模型）、`backend/app/talent_mcp_client.py`（最小图）、`backend/scripts/verify_talent_mcp.py`（验证链）、`backend/tests/test_talent_mcp_server.py` 与 `test_talent_mcp_client.py`（可执行契约）、`backend/samples/lesson13/codestats_mcp_v2.py`（迁移/STDIO）、`backend/pyproject.toml`（版本范围）。阅读时以自己当前提交的文件和实际运行结果为准。

**协议与工具的一手资料（链接用于升级时核验，版本可能变化）：**

- [MCP 规范与版本索引](https://modelcontextprotocol.io/specification/2026-07-28)：重点核对 Tools/Resources/Prompts、Tool annotations、授权与 Streamable HTTP；实际实现对应哪一 revision，以协商值为准。
- [MCP Python SDK 官方仓库](https://github.com/modelcontextprotocol/python-sdk)：迁移、`MCPServer`、`Client`、`mcp dev` 与示例；本项目 pin 的安装包及测试比网上其他版本的片段更直接。
- [MCP Inspector 官方仓库](https://github.com/modelcontextprotocol/inspector)：Inspector 使用方式及版本更新；不要把 UI 端口当 MCP 服务端点。
- [LangChain Python 官方仓库](https://github.com/langchain-ai/langchain)：`MCPAdapter` API 升级需对照本项目回归；[LangGraph 官方仓库](https://github.com/langchain-ai/langgraph) 用于理解 `ToolNode`。

> 最后一句工程原则：**先证明身份边界，再公布能力；先证明数据确实存在，再承诺版本语义；先看真实副作用，再声明 Tool 注解；先跑反例测试，再打验收勾。**




