# 第 8 课：复合人才检索与查询计划 · 完整学习与课后作业指导

> 用途：系统掌握“自然语言人才需求 → Query Plan → 参数化 SQL 候选集 → 限定候选范围的混合检索 → 证据合并与查询优化”完整链路，并逐步完成第 8 课五项作业。
>
> 记录时间：2026-09-23。
>
> 代码基线：当前分支提交 `b08dba4 人才查询计划`。
>
> 项目目录：`D:\Code\K_Course\talent-eval-agents_learning`。
>
> 重点代码：`backend/app/query_plan.py`、`backend/app/api.py`、`backend/tests/test_query_plan.py`、`backend/tests/test_talent_search_api.py`。

---

> **源码核验与勘误（请先读）**：正文沿用仓库已有的第 8 课完整讲义，基于 `b08dba4`。下述五项作业步骤和示例是**作业指导，不表示作业 1/2/3/5 已写进代码**。请区分“当前行为”“建议修改”“修改后期望结果”。
>
> **整岁年龄边界**：正文 §3.5 的表格描述的是当前 `_age_spec()` 实现，但 `age <= n`、`age > n` 不符合完整整岁语义。以 2026-09-23 和 `n=35` 为例，35 岁的人出生于 1990-09-24 至 1991-09-23；现有 `age <= 35 → birth_date >= 1991-09-23` 会错误排除其中绝大多数 35 岁人，`age > 35 → birth_date < 1991-09-23` 会错误纳入一部分 35 岁人。整数整岁定义应是：`age < n → birth_date > B(n)`、`age >= n → birth_date <= B(n)`、`age <= n → birth_date > B(n+1)`、`age > n → birth_date <= B(n+1)`，其中 `B(k)` 是查询日减去 `k` 年的生日边界。修复前先写生日当天、前一天、后一天与 2 月 29 日政策的回归测试。作业 2 的冲突检测和这个现存边界缺陷是**两个独立问题**。
>
> **年龄数值形状**：`FilterCondition.value` 当前容许字符串和浮点数；`_age_spec` 却调用 `int(value)`。例如 `35.9` 会被截断，非数字字符串会在执行时失败。建议在字段级约束 `age` 为非负整数、工作年限为非负数，拒绝布尔值/NaN/无穷大；不要依赖 SQL 出错兜底。
>
> **身份与权限边界**：`api.py` 直接从 `Header(...)` 读取 `X-Tenant-ID` 和 `X-Permission-Scopes`，只对空权限列表返回 403；这条链路没有证明调用者有权声明这个租户或权限。课程代码展示的是**过滤条件下推**，不能单独证明已“防越权”。上线时应由可信认证组件从已验证身份派生租户和权限，阻止客户端伪造权限头，并做跨租户/材料授权回归测试。正文的“来自可信上下文”是目标架构而非当前 API 的既成事实。
>
> **最终结果边界**：`/api/talent-search` 当前逐项检索并去重合并 Chunk；`required=True` 没有在候选人级执行 AND，偏好尚未真正加权。返回的 `candidate_ids` 是 SQL 初筛的人，`chunks` 是命中证据，并非“同时满足所有经历要求的最终人才名单”。`execute_composite_search()` 是独立示范函数，完整 HTTP 接口并不直接调用。
>
> **敏感字段练习**：§9 的“政治面貌”用于学习 schema、迁移、写入/读取、DSL 注册表与测试的联动；实际招聘/人才决策不能仅因技术可筛选就启用。应先明确目的、授权、访问审计、保留周期与公平性风险，交组织的隐私/合规负责人审核；也可先用非敏感字段练习同一工程步骤。
>
> **验证记录**：本次只制作学习文档，未改动后端功能代码。仓库 `.venv` 指向不可用的 Python 解释器；工作区捆绑 Python 缺少 `pytest`，因此**没有声称测试通过**。文中新测试是待实践示例，请在可用课程环境运行。

---
## 0. 先用一句话理解第 8 课

**第 8 课不是教模型直接“查数据库”，而是教模型把人的自然语言编译成受约束的中间计划，再由确定性程序分别执行 SQL 和检索。**

第 7 课已经解决：

```text
自然语言查询
  → Dense 向量召回
  + BM25 关键词召回
  → RRF 融合
  → Rerank 精排
  → 相关材料 Chunk
```

但真实人才需求往往是混合的：

> 找深圳或广州、35 岁以下、L4 以上，有 Flink 实时计算项目和团队管理经历的人，金融行业经验优先。

其中不同条件不能用同一种执行方式：

| 用户条件 | 类型 | 应由谁执行 | 原因 |
|---|---|---|---|
| 深圳或广州 | 结构化硬条件 | PostgreSQL | 字段明确，必须精确过滤 |
| 35 岁以下 | 结构化硬条件 | PostgreSQL | 可以由出生日期确定性计算 |
| L4 以上 | 目前是待澄清/DSL 能力不足 | 澄清或扩展 DSL | 当前 `job_level` 只支持 `eq/in`，不能擅自解释等级比较 |
| 有 Flink 实时计算项目 | 语义要求 | Milvus 混合检索 | 信息存在简历、项目材料等非结构化文本中 |
| 有团队管理经历 | 语义要求 | Milvus 混合检索 | 需要从材料中寻找证据 |
| 金融行业经验优先 | 偏好 | 后续排序/评估 | “优先”不能错误地变成排除条件 |

所以这节课的核心架构是：

```text
用户自然语言
    │
    ▼
LLM 只负责编译 Query Plan
    │
    ├── filters --------------------→ SQLAlchemy → PostgreSQL → candidate_ids
    │
    ├── semantic_requirements ------→ 在 candidate_ids 范围内做混合检索
    │
    ├── preferences ----------------→ 保留给后续评分/排序，不参与硬过滤
    │
    └── clarifications -------------→ 阻止执行，要求补充或修正条件
                                           │
                                           ▼
                            每项语义要求分别保留证据
                                           │
                                           ▼
                               合并 Chunk 并返回结果
```

### 0.1 老师真正想让你理解什么

五项作业表面上分别在写测试、校验、扩展字段，实际对应五个工程能力：

| 作业 | 表面任务 | 真正考察的能力 |
|---|---|---|
| 1 | 测试 `department in [...]` | 理解 SQLAlchemy 参数绑定和集合参数展开，拒绝字符串拼 SQL |
| 2 | 年龄区间冲突检测 | 理解 Schema 合法不代表业务可执行，必须做跨条件一致性校验 |
| 3 | 两项经历生成两个语义要求 | 理解复合语义条件要拆成独立证据通道，不能混成一句模糊查询 |
| 4 | 分析 3.6 优化策略 | 理解“查询改写”不是万能补丁，要有触发、预算、融合和质量评估 |
| 5 | 新增政治面貌字段 | 理解一个业务字段的端到端影响面，以及注册表作为单一事实源的价值 |

### 0.2 学完本课应具备的能力

完成本课后，你应能够独立回答：

1. 为什么不能让 LLM 直接生成 SQL？
2. 为什么 SQL 必须先于向量检索执行？
3. 为什么 `candidate_ids=[]` 必须立即返回，而不能继续查询 Milvus？
4. 租户条件和材料权限为什么不能从用户文本提取？
5. `semantic_requirements` 为什么需要 `requirement_id`？
6. 为什么“有两个经历要求”不能只生成一个长查询？
7. 为什么 Pydantic 字段类型校验不足以发现“35 岁以上且 29 岁以下”？
8. SQLAlchemy 的 `IN` 为什么会出现 `POSTCOMPILE` 参数？
9. 为什么新增一个查询字段不只是给枚举加一行？
10. 当前查询优化为什么在单元测试里可用，但端到端能力仍有限？

---

## 1. 第 8 课代码地图

### 1.1 主要文件

| 文件 | 职责 |
|---|---|
| `backend/app/query_plan.py` | Query Plan Schema、Filter DSL 注册表、SQL Builder、语义检索编排、查询优化 |
| `backend/app/api.py` | FastAPI 请求模型和三个第 8 课接口 |
| `backend/app/models.py` | `EmployeeProfile` SQLAlchemy ORM 模型 |
| `backend/app/milvus_store.py` | `EvidenceFilter` 与 Milvus 过滤表达式 |
| `backend/app/hybrid_search_service.py` | Dense + BM25 + RRF + Rerank 混合检索服务 |
| `database/init.sql` | 新建 PostgreSQL 数据卷时的完整表结构 |
| `backend/tests/test_query_plan.py` | Query Plan、DSL、SQL 编译、空集和优化逻辑单元测试 |
| `backend/tests/test_talent_search_api.py` | `/api/talent-search` 端到端编排测试 |
| `frontend/src/main.tsx` | 员工字段类型、列表展示、新增员工表单 |

### 1.2 三个接口的分工

| 接口 | 输入 | 输出 | 适合观察什么 |
|---|---|---|---|
| `POST /api/talent-search/plan` | 自然语言 `query` | `QueryPlan` | 看模型如何分类条件 |
| `POST /api/talent-search/candidates` | 已生成的 `QueryPlan` + 租户头 | `candidate_ids` | 单独验证 DSL 与 SQL Builder |
| `POST /api/talent-search` | 自然语言 + 检索参数 + 租户/权限头 | Plan、候选集、检索过程、Chunk | 验证完整复合人才检索链路 |

### 1.3 当前 Query Plan 数据结构

`backend/app/query_plan.py:60-65`：

```python
class QueryPlan(BaseModel):
    task_type: TaskType
    filters: list[FilterCondition] = Field(default_factory=list)
    semantic_requirements: list[SemanticRequirement] = Field(default_factory=list)
    preferences: list[str] = Field(default_factory=list)
    clarifications: list[Clarification] = Field(default_factory=list)
```

四个列表不是为了“结构好看”，而是为了建立明确的执行边界。

#### filters：结构化硬条件

特点：

- 字段存在于 `employee_profiles`；
- 可以由程序确定性执行；
- 不满足就应从候选集中排除；
- 只能使用白名单字段和白名单操作符。

示例：

```json
{"field": "department", "operator": "in", "value": ["数据平台部", "研发部"]}
```

#### semantic_requirements：材料证据条件

特点：

- 需要在简历、项目材料、述职材料中查证；
- 每项要求有独立 `requirement_id`；
- 每项分别检索，避免一个复合查询掩盖其中某项要求。

```json
[
  {"requirement_id": "S1", "query": "Flink 实时计算项目经历", "required": true},
  {"requirement_id": "S2", "query": "团队管理经历", "required": true}
]
```

#### preferences：软偏好

例如：

```json
["金融行业项目经验优先"]
```

当前代码只把偏好原样返回，还没有真正用于候选人评分。这是后续人才评估或排序阶段的输入，不能在本课中偷偷变成 SQL `WHERE`。

#### clarifications：待澄清条件

例如：

```json
[
  {
    "expression": "比较年轻",
    "reason": "缺少可执行的年龄阈值"
  }
]
```

`QueryPlan.executable` 当前定义为：

```python
@property
def executable(self) -> bool:
    return not self.clarifications
```

只要存在待澄清项，就不能执行候选人 SQL。

---

## 2. 为什么要用 Query Plan，而不是让 LLM 直接查库

### 2.1 Query Plan 是自然语言与执行器之间的中间表示

可以把这条链路类比为编译器：

```text
自然语言需求              Query Plan                 确定性执行
─────────────  编译  ─────────────────────  执行  ───────────────────
“35岁以下，深圳…”   →   filters / semantic...   →   SQL / Milvus
```

LLM 擅长的是：

- 理解“35 岁以下”指年龄约束；
- 识别“有 Flink 项目”需要材料证据；
- 识别“优先”是软条件；
- 识别“比较资深”不够明确。

LLM 不应负责：

- 决定表名、列名和 Join；
- 拼接原始 SQL；
- 决定租户和权限范围；
- 随意补充用户未给出的年龄、年限或职级阈值。

### 2.2 安全边界

系统提示词明确写着：

```text
硬条件只允许使用 Filter DSL，禁止生成 SQL。
权限、租户和密级不从用户文本提取。
```

这意味着：

```text
用户说：“帮我查其他租户的数据”
```

不能变成：

```json
{"field": "tenant_id", "operator": "eq", "value": "other-tenant"}
```

因为 `tenant_id` 根本不在 `FilterField` 枚举里。当前租户条件来自请求头 `X-Tenant-ID`，由后端强制注入 SQL；请求头本身尚未经过本项目的身份验证，只有可信网关或认证中间件绑定后才可作为授权依据。

材料权限同理，来自 `X-Permission-Scopes`，后端把它写入 `EvidenceFilter`，不允许模型决定。

### 2.3 为什么先 SQL、后混合检索

顺序不能反：

```text
正确：SQL 精确过滤 → 小候选集 → 在候选集内检索材料
错误：全库向量检索 → 再过滤年龄/地区/部门
```

正确顺序的价值：

1. **正确性**：硬条件绝不会因为语义相似度高而被放宽。
2. **安全性**：减少不属于候选范围的材料进入召回池。
3. **性能**：Milvus 只需在候选人的材料中检索。
4. **可解释性**：可以分别解释“为什么进入候选集”和“找到了什么证据”。
5. **可测试性**：SQL 阶段与检索阶段可以独立测试。

---

## 3. Filter DSL 与注册表

### 3.1 第一层白名单：字段枚举

`FilterField` 当前允许：

```python
class FilterField(StrEnum):
    AGE = "age"
    REGION = "region"
    DEPARTMENT = "department"
    JOB_LEVEL = "job_level"
    YEARS_OF_EXPERIENCE = "years_of_experience"
    CURRENT_POSITION = "current_position"
    EMPLOYMENT_STATUS = "employment_status"
```

模型即使输出 `salary`，Pydantic 也会拒绝。

当前测试：

```python
def test_rejects_unknown_filter_field():
    with pytest.raises(ValidationError):
        FilterCondition(field="salary", operator="gt", value=10000)
```

### 3.2 第二层白名单：操作符和 value 形状

`FilterCondition` 做了两类通用校验：

- `lt/lte/gt/gte` 只能用于年龄和工作年限；
- `in` 必须接收列表，非 `in` 不能接收列表。

但要注意：这一层只知道“数值类字段”与“列表值”，不知道每个字段的完整操作符集合。

例如下面对象能够通过 `FilterCondition` 构造：

```python
FilterCondition(field="age", operator="eq", value=35)
```

真正执行到 `build_candidate_statement()` 时，注册表才会拒绝，因为年龄注册项只允许 `lt/lte/gt/gte`。

### 3.3 第三层白名单：`FILTER_FIELD_REGISTRY`

注册表把三个东西放在一起：

```text
字段说明 + 可用操作符 + SQLAlchemy 构造函数
```

当前定义：

```python
FILTER_FIELD_REGISTRY = {
    FilterField.AGE: FilterFieldSpec(
        "年龄，由 birth_date 按查询基准日换算",
        frozenset({"lt", "lte", "gt", "gte"}),
        _age_spec,
    ),
    FilterField.REGION: _column_spec(EmployeeProfile.region, "工作地区", {"eq", "in"}),
    FilterField.DEPARTMENT: _column_spec(EmployeeProfile.department, "所属部门", {"eq", "in"}),
    # ...
}
```

这是本课很重要的设计：**注册表既指导模型，也指导执行器。**

`filter_dsl_catalog()` 从同一个注册表生成提示词：

```python
def filter_dsl_catalog() -> str:
    return "\n".join(
        f"- {field.value}: {spec.description}; operators={','.join(sorted(spec.operators))}"
        for field, spec in FILTER_FIELD_REGISTRY.items()
    )
```

因此新增字段时，不需要在提示词里再手写一份字段清单。否则很容易出现：

```text
提示词允许，但执行器不支持
或
执行器支持，但提示词没有告诉模型
```

### 3.4 确定性 SQL Builder

核心代码：

```python
clauses = [
    EmployeeProfile.tenant_id == tenant_id,
    EmployeeProfile.employment_status == "active",
]

for item in plan.filters:
    spec = FILTER_FIELD_REGISTRY[item.field]
    if item.operator not in spec.operators:
        raise ValueError(...)
    clauses.append(spec.build(item, current_day))

return (
    select(EmployeeProfile.employee_no)
    .where(and_(*clauses))
    .order_by(EmployeeProfile.employee_no)
)
```

这里有两个系统强制条件：

1. `tenant_id == 请求头租户`；
2. `employment_status == "active"`。

它们不需要也不允许由模型生成。

### 3.5 年龄为什么要反转比较符

数据库存的是 `birth_date`，用户说的是 `age`。

以查询基准日 2026-09-23 为例：

```text
35 岁边界日期 = 1991-09-23
```

年龄越小，出生日期越晚，因此比较方向要反转：

| 用户年龄条件 | 出生日期条件 |
|---|---|
| `age < 35` | `birth_date > 1991-09-23` |
| `age <= 35` | `birth_date >= 1991-09-23` |
| `age > 35` | `birth_date < 1991-09-23` |
| `age >= 35` | `birth_date <= 1991-09-23` |

代码：

```python
reversed_operator = {
    "lt": ">",
    "lte": ">=",
    "gt": "<",
    "gte": "<=",
}[item.operator]
```

`_age_boundary()` 还处理了 2 月 29 日替换年份时的异常，退到 2 月 28 日。

---

## 4. 从请求入口开始：三个后端接口完整链路

## 4.1 FastAPI 路由如何生效

程序入口在 `backend/app/main.py`：

```python
from app.api import router


def create_app() -> FastAPI:
    app = FastAPI(...)
    # 中间件、健康检查等
    app.include_router(router)
    return app
```

`backend/app/api.py` 中：

```python
router = APIRouter(prefix="/api")
```

所以：

```python
@router.post("/talent-search")
```

最终路径是：

```text
POST /api/talent-search
```

---

## 4.2 接口一：`POST /api/talent-search/plan`

### 请求模型

```python
class QueryPlanInput(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
```

请求示例：

```json
{
  "query": "查找深圳或广州、35岁以下，具有Flink实时计算项目经历和团队管理经历的人，金融行业经验优先"
}
```

### 调用链

```text
HTTP POST /api/talent-search/plan
  → FastAPI 将 JSON 校验为 QueryPlanInput
  → get_chat_model(temperature=0)
  → compile_query_plan(payload.query, model)
  → model.with_structured_output(QueryPlan)
  → 系统提示词注入 filter_dsl_catalog()
  → LLM 返回受 Pydantic Schema 约束的 QueryPlan
  → FastAPI 序列化返回
```

核心函数：

```python
def compile_query_plan(query: str, model: Any) -> QueryPlan:
    structured_model = model.with_structured_output(QueryPlan)
    return structured_model.invoke(
        [
            ("system", QUERY_PLAN_SYSTEM_PROMPT.format(
                filter_dsl=filter_dsl_catalog()
            )),
            ("user", query),
        ]
    )
```

一个合理的输出应类似：

```json
{
  "task_type": "find_talent",
  "filters": [
    {"field": "region", "operator": "in", "value": ["深圳", "广州"]},
    {"field": "age", "operator": "lt", "value": 35}
  ],
  "semantic_requirements": [
    {
      "requirement_id": "S1",
      "query": "Flink 实时计算项目经历",
      "required": true
    },
    {
      "requirement_id": "S2",
      "query": "团队管理经历",
      "required": true
    }
  ],
  "preferences": ["金融行业经验优先"],
  "clarifications": []
}
```

### 这个接口不做什么

它只负责编译，不会：

- 查询 PostgreSQL；
- 调用 Embedding；
- 查询 Milvus；
- 执行 Rerank；
- 接受或信任用户文本中的租户/权限条件。

### 当前错误处理的不足

现在任何编译异常都返回 503：

```python
except Exception as exc:
    raise HTTPException(503, f"查询计划生成失败: {exc}")
```

但“用户条件自相矛盾”不是服务不可用，工程上更合理的是返回 422。作业二会进一步处理这个问题。

---

## 4.3 接口二：`POST /api/talent-search/candidates`

这个接口的请求体不是自然语言，而是已经生成的 `QueryPlan`。

请求示例：

```powershell
$body = @{
  task_type = "find_talent"
  filters = @(
    @{ field = "department"; operator = "in"; value = @("数据平台部", "研发部") }
    @{ field = "age"; operator = "lt"; value = 35 }
  )
  semantic_requirements = @(
    @{ requirement_id = "S1"; query = "Flink 实时计算项目经历"; required = $true }
  )
  preferences = @()
  clarifications = @()
} | ConvertTo-Json -Depth 8

Invoke-RestMethod `
  -Uri "http://127.0.0.1:18080/api/talent-search/candidates" `
  -Method Post `
  -Headers @{ "X-Tenant-ID" = "course-demo" } `
  -ContentType "application/json; charset=utf-8" `
  -Body $body
```

### 调用链

```text
POST /api/talent-search/candidates
  → FastAPI 把请求体校验为 QueryPlan
  → 检查 plan.executable
      ├─ 有 clarifications → HTTP 422
      └─ 无 clarifications → 继续
  → 从 X-Tenant-ID 读取租户
  → select_candidate_ids(db, plan, tenant_id)
  → build_candidate_statement(...)
  → 注册表逐项生成 SQLAlchemy 表达式
  → db.scalars(statement).all()
  → 返回 employee_no 列表
```

核心代码：

```python
candidate_ids = select_candidate_ids(db, plan, tenant_id=x_tenant_id)
```

内部执行：

```python
def select_candidate_ids(...):
    statement = build_candidate_statement(...)
    return list(db.scalars(statement).all())
```

响应：

```json
{
  "candidate_ids": ["C001", "C004"],
  "candidate_count": 2,
  "semantic_requirements": [
    {"requirement_id": "S1", "query": "Flink 实时计算项目经历", "required": true}
  ]
}
```

### 为什么返回 `employee_no` 而不是数据库 UUID

Milvus 证据记录中的 `candidate_id` 对应员工工号，因此 SQL 阶段选择的是：

```python
select(EmployeeProfile.employee_no)
```

这样得到的 `candidate_ids` 可以直接传给 `EvidenceFilter`，不需要再做一次 UUID 到工号的映射。

---

## 4.4 接口三：`POST /api/talent-search`

这是第 8 课完整接口。

### 请求模型

```python
class TalentSearchInput(BaseModel):
    query: str
    retrieval_mode: Literal["standard", "auto_optimize"] = "standard"
    limit: int = 10
    ef: int = 80
    rrf_k: int = 60
    rerank_top_n: int = 20
```

请求示例：

```powershell
$body = @{
  query = "查找35岁以下、深圳或广州，有Flink实时项目和团队管理经历的人"
  retrieval_mode = "auto_optimize"
  limit = 10
  ef = 80
  rrf_k = 60
  rerank_top_n = 20
} | ConvertTo-Json

Invoke-RestMethod `
  -Uri "http://127.0.0.1:18080/api/talent-search" `
  -Method Post `
  -Headers @{
    "X-Tenant-ID" = "course-demo"
    "X-Permission-Scopes" = "hr_private"
  } `
  -ContentType "application/json; charset=utf-8" `
  -Body $body
```

### 完整调用链

```text
1. FastAPI 校验 TalentSearchInput
2. 解析 X-Permission-Scopes
3. 获取 Chat Model / Embedder / Reranker
4. compile_query_plan(query)
5. 若 plan.clarifications 非空 → 422，不执行
6. select_candidate_ids(..., tenant_id=X-Tenant-ID)
7. 若 candidate_ids 为空 → 立即返回空结果
8. 创建 run_hybrid_search 闭包
9. 遍历每个 semantic_requirement
10. standard：直接混合检索
    auto_optimize：首轮失败时改写并重试
11. 记录每个 Chunk 满足了哪些 requirement_id
12. merge_query_results 按 chunk_id 去重
13. 返回 Query Plan、候选集、检索轨迹和 Chunk
```

### 第一步：权限头处理

```python
permission_scopes = [
    value.strip()
    for value in x_permission_scopes.split(",")
    if value.strip()
]

if not permission_scopes:
    raise HTTPException(403, "缺少可用的证据权限范围")
```

租户和权限的来源不同：

| 条件 | 进入哪里 | 用途 |
|---|---|---|
| `X-Tenant-ID` | PostgreSQL SQL + Milvus Filter | 隔离租户 |
| `X-Permission-Scopes` | Milvus Filter | 限制可检索材料密级 |

### 第二步：编译 Query Plan

```python
plan = compile_query_plan(payload.query, model)
```

如果含待澄清条件：

```python
if not plan.executable:
    raise HTTPException(
        422,
        {
            "code": "clarification_required",
            "items": [item.model_dump() for item in plan.clarifications],
        },
    )
```

### 第三步：执行 SQL 候选筛选

```python
candidate_ids = select_candidate_ids(
    db,
    plan,
    tenant_id=x_tenant_id,
)
```

### 第四步：空候选集短路

```python
if not candidate_ids:
    return {
        "query_plan": plan,
        "candidate_ids": [],
        "searches": [],
        "chunks": [],
    }
```

这是安全关键点。很多检索 API 对：

```python
candidate_ids=None
```

解释为“不限制候选人”。如果误把空列表处理成 `None`，原本 SQL 证明“无人满足”的查询会退化成全库检索。

正确语义必须是：

```text
None / 未提供：调用方没有设置候选过滤
[]：已经执行了候选筛选，而且结果为空，必须停止
```

### 第五步：把 SQL 候选集传给混合检索

后端创建闭包：

```python
def run_hybrid_search(query: str):
    return hybrid_search_evidence_service(
        query=query,
        filters=EvidenceFilter(
            tenant_id=x_tenant_id,
            permission_scopes=permission_scopes,
            candidate_ids=candidate_ids,
        ),
        store=store,
        embedder=embedder,
        reranker=reranker,
        limit=payload.limit,
        ef=payload.ef,
        rrf_k=payload.rrf_k,
        rerank_top_n=payload.rerank_top_n,
    )
```

三个过滤维度缺一不可：

```text
tenant_id          防跨租户
permission_scopes  防材料越权
candidate_ids      保证只在 SQL 硬条件通过者中检索
```

### 第六步：每项语义要求分别检索

```python
for requirement in plan.semantic_requirements:
    results = run_hybrid_search(requirement.query)
```

若有两个要求，会调用两次：

```text
S1: Flink 实时计算项目经历 → 一组结果
S2: 团队管理经历           → 一组结果
```

不是把它们拼成：

```text
“Flink 实时计算项目经历并且团队管理经历”
```

因为一个长查询可能只召回与其中一半高度相关的材料，无法知道另一半是否真的有证据。

### 第七步：保留证据归属

```python
requirement_ids_by_chunk.setdefault(item.chunk_id, []).append(
    requirement.requirement_id
)
```

最终 Chunk 会包含：

```json
{
  "chunk_id": "chunk-001",
  "candidate_id": "C001",
  "content": "……",
  "score": 0.91,
  "requirement_ids": ["S1", "S2"]
}
```

这让后续系统知道该 Chunk 是哪项要求的证据。

### 第八步：去重合并

```python
for item in merge_query_results(result_sets)
```

当前规则是：

- 按 `chunk_id` 去重；
- 保留第一次出现的位置；
- 不做跨查询分数归一化；
- 不做候选人级 AND 判断。

后两点正是作业四要分析的限制。

---

## 4.5 一个完整例子的逐层变化

输入：

```text
查找深圳或广州、35岁以下，有Flink实时计算项目经历和团队管理经历的人，金融行业经验优先
```

### A. Query Plan

```json
{
  "task_type": "find_talent",
  "filters": [
    {"field": "region", "operator": "in", "value": ["深圳", "广州"]},
    {"field": "age", "operator": "lt", "value": 35}
  ],
  "semantic_requirements": [
    {"requirement_id": "S1", "query": "Flink 实时计算项目经历", "required": true},
    {"requirement_id": "S2", "query": "团队管理经历", "required": true}
  ],
  "preferences": ["金融行业经验优先"],
  "clarifications": []
}
```

### B. SQLAlchemy 逻辑

```text
WHERE tenant_id = :tenant_id
  AND employment_status = :active
  AND region IN (:region_1, :region_2)
  AND birth_date > :age_boundary
ORDER BY employee_no
```

### C. SQL 返回

```json
["C001", "C004"]
```

### D. Milvus 过滤范围

```text
tenant_id == "course-demo"
AND permission_scope in ["hr_private"]
AND candidate_id in ["C001", "C004"]
```

### E. 两次语义检索

```text
Flink 实时计算项目经历 → 只查 C001、C004 的材料
团队管理经历           → 只查 C001、C004 的材料
```

### F. 返回结果

返回中应同时看到：

- 原始 `query_plan`；
- SQL 候选集；
- 每项语义要求的检索策略、查询和命中数；
- 每个 Chunk 对应的 `requirement_ids`。

---

# 5. 课后作业一：为 `department in [...]` 增加测试，并检查绑定参数

## 5.1 先用“输入 → 输出 → 完成标准”理解题目

**这道题要测试 SQL 构造器，不是测试 LLM，也不是实际访问数据库。**输入是人为构造的 `QueryPlan` 与租户 ID；输出是 SQLAlchemy 的 `Select` 对象。通过 `statement.compile(dialect=postgresql.dialect())`，才能观察 SQL 文本和绑定参数；**编译不执行 SQL，因此不会得到候选人 ID**。

| 项目 | 本题具体值或期望 |
|---|---|
| 输入 1：计划 | `QueryPlan(task_type="find_talent", filters=[FilterCondition(field="department", operator="in", value=["数据平台部", "研发部"])])`；可以沿用测试文件里的 `_plan(...)` 助手，其预置的语义条件在本测试中不参与 SQL |
| 输入 2：调用上下文 | `tenant_id="course-demo"`。这是租户 ID，**不是** `talent_id`；在实际 `/api/talent-search` 中来自 `x_tenant_id` 请求头。真实系统还必须对头部做认证授权，不能仅相信客户端自报 |
| 输入 3：基准日 | `today=date(2026, 9, 23)` 只为测试确定性；本题没有年龄条件，它不会影响部门 SQL |
| 直接返回值 | `Select[tuple[str]]`：选取 `EmployeeProfile.employee_no`，不是员工记录、不是候选 ID 列表 |
| 编译后应见 SQL 形状 | `SELECT employee_profiles.employee_no ... WHERE tenant_id = ... AND employment_status = ... AND department IN (__[POSTCOMPILE_department_1]) ORDER BY employee_no`（占位符名字和换行可能随方言/版本变化） |
| 编译后应见绑定参数 | 租户为 `course-demo`、状态为 `active`、部门列表为 `["数据平台部", "研发部"]`；部门名称**不直接出现在 SQL 文本中** |
| pytest 成功 | 以上断言均成立、命令显示 `1 passed` 且退出码为 0；仅仅打印 SQL 或仅仅检查 `IN` 字样不够 |

**额外提醒**：SQL 的 `department IN ('数据平台部', '研发部')` 表示“任一部门”——两个值之间是 **OR**。它再与系统注入的租户、`active` 条件以 **AND** 组合。

---

## 5.2 源码的设计链路：每层分别做什么

文件：`backend/app/query_plan.py`。

```text
测试直接构造 QueryPlan（跳过 LLM）
  → build_candidate_statement(plan, tenant_id="course-demo", today=固定日期)
  → 检查 tenant_id 非空、plan 可执行
  → clauses 先放两个系统条件：tenant_id == course-demo；employment_status == active
  → 遍历 plan.filters
  → 在 FILTER_FIELD_REGISTRY 中找到 department 的规格
  → 确认 operator="in" 被允许
  → _column_spec 的 build() 调用 EmployeeProfile.department.in_([两个部门])
  → 把部门表达式加入 clauses
  → select(employee_no).where(and_(*clauses)).order_by(employee_no)
  → 返回 Select；此时尚未连接 PostgreSQL
  → statement.compile(...) 生成可检查的 SQL 文本、绑定参数
```

对应关键代码（删去无关部分）：

```python
FILTER_FIELD_REGISTRY = {
    FilterField.DEPARTMENT: _column_spec(
        EmployeeProfile.department, "所属部门", {"eq", "in"}
    ),
}

# _column_spec() 中：
"in": lambda: column.in_(value)

# build_candidate_statement() 中：
clauses = [
    EmployeeProfile.tenant_id == tenant_id,
    EmployeeProfile.employment_status == "active",
]
for item in plan.filters:
    spec = FILTER_FIELD_REGISTRY[item.field]
    if item.operator not in spec.operators:
        raise ValueError(...)
    clauses.append(spec.build(item, current_day))
return select(EmployeeProfile.employee_no).where(and_(*clauses)).order_by(EmployeeProfile.employee_no)
```

所以你可以这样理解：`plan.filters` **只贡献用户明确表达的部门条件**；`tenant_id` **由调用方另传**，`active` **由 SQL Builder 固定加入**。如果走完整接口，真正执行 SQL 的下一层才是：

```python
def select_candidate_ids(db, plan, *, tenant_id, today=None):
    statement = build_candidate_statement(plan, tenant_id=tenant_id, today=today)
    return list(db.scalars(statement).all())
```

这才会返回例如 `["C001", "C004"]`。**作业一没有调用 `select_candidate_ids()`，也无法在没有数据库记录的情况下断言候选人的实际 ID。**

---

## 5.3 先看可观察的中间结果

构造测试输入：

```python
plan = _plan(
    FilterCondition(
        field="department",
        operator="in",
        value=["数据平台部", "研发部"],
    )
)
statement = build_candidate_statement(
    plan, tenant_id="course-demo", today=date(2026, 9, 23)
)
```

注意：`_plan(...)` 是 `backend/tests/test_query_plan.py` 的测试助手，会创建 `QueryPlan`，并额外放入一个 `semantic_requirement`。本测试只关心 `filters`；SQL Builder **不会**把那条语义条件写进 WHERE。

默认编译 PostgreSQL 方言时，SQL 关键部分的**示意**是：

```sql
SELECT employee_profiles.employee_no
FROM employee_profiles
WHERE employee_profiles.tenant_id = %(tenant_id_1)s
  AND employee_profiles.employment_status = %(employment_status_1)s
  AND employee_profiles.department IN (__[POSTCOMPILE_department_1])
ORDER BY employee_profiles.employee_no
```

相应 `compiled.params` **示意**：

```python
{
    "tenant_id_1": "course-demo",
    "employment_status_1": "active",
    "department_1": ["数据平台部", "研发部"],
}
```

`__[POSTCOMPILE_department_1]` 是 SQLAlchemy 的 expanding 参数标记，表示执行时根据列表长度生成占位符，**不是把两个部门直接拼进 SQL**。这是本题的观察重点。

---

## 5.4 你需要亲手增加的 pytest（最小完成版）

在 `backend/tests/test_query_plan.py` 增加以下测试。文件已有 `date`、`postgresql`、`FilterCondition`、`build_candidate_statement` 和 `_plan` 的定义/导入，不用重复添加：

```python
def test_department_in_uses_bound_parameters():
    # Arrange：模拟“模型已经编译好计划”的结果；不调用真实 LLM。
    plan = _plan(
        FilterCondition(
            field="department",
            operator="in",
            value=["数据平台部", "研发部"],
        )
    )

    # Act：构造 SQLAlchemy 查询对象；此处不访问数据库。
    statement = build_candidate_statement(
        plan, tenant_id="course-demo", today=date(2026, 9, 23)
    )
    compiled = statement.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    params = compiled.params

    # Assert A：选的是工号；WHERE 中有租户、系统 active 状态和部门 IN。
    assert "employee_profiles.employee_no" in sql
    assert "employee_profiles.tenant_id =" in sql
    assert "employee_profiles.employment_status =" in sql
    assert "employee_profiles.department IN" in sql
    assert "ORDER BY employee_profiles.employee_no" in sql

    # Assert B：三个条件的值都通过绑定参数携带。
    department_key = next(key for key in params if key.startswith("department_"))
    tenant_key = next(key for key in params if key.startswith("tenant_id_"))
    status_key = next(key for key in params if key.startswith("employment_status_"))
    assert params[department_key] == ["数据平台部", "研发部"]
    assert params[tenant_key] == "course-demo"
    assert params[status_key] == "active"

    # Assert C：IN 列表尚未被字符串插入 SQL。
    assert f"__[POSTCOMPILE_{department_key}]" in sql
    assert "数据平台部" not in sql
    assert "研发部" not in sql
```

这组断言共同证明：

1. **查哪一列**：只选 `employee_no`；
2. **有哪些条件**：租户、在职状态、部门集合；
3. **条件值在哪里**：在 `compiled.params`，而不在 SQL 字符串里；
4. **列表如何绑定**：`IN` 对应一个 post-compile 列表参数。

如果错误地只写 `assert "department IN" in sql`，可能漏掉“参数是空的/值不对/部门被直接拼进 SQL”等问题，不能完成“检查编译后的绑定参数”这一要求。

---

## 5.5 进阶观察：列表最后如何展开（可选）

在同一测试中继续加入：

```python
rendered = statement.compile(
    dialect=postgresql.dialect(),
    compile_kwargs={"render_postcompile": True},
)
rendered_sql = str(rendered)
rendered_department = {
    key: value
    for key, value in rendered.params.items()
    if key.startswith("department_")
}

assert len(rendered_department) == 2
assert set(rendered_department.values()) == {"数据平台部", "研发部"}
assert rendered_sql.count("%(department_") == 2
assert "数据平台部" not in rendered_sql
assert "研发部" not in rendered_sql
```

这时能看到两个独立占位符，如 `%(department_1_1)s`、`%(department_1_2)s`，及各自的参数值。`render_postcompile=True` **仍然没有执行查询**。不要用 `literal_binds=True` 来证明绑定安全：它是调试展示，会把值渲染到 SQL 文本中。

---

## 5.6 如何运行与判定完成

在项目后端目录执行：

```powershell
cd D:\Code\K_Course\talent-eval-agents_learning\backend
uv run pytest tests/test_query_plan.py::test_department_in_uses_bound_parameters -q
```

成功输出示例：

```text
1 passed in ...s
```

再运行整个测试文件，确认没有破坏原有条件：

```powershell
uv run pytest tests/test_query_plan.py -q
```

**验收清单**：

- [ ] 新测试确实被 pytest 收集（不是只写了函数但没运行）；
- [ ] 单项测试为 `1 passed`，退出码 0；
- [ ] SQL 选取 `employee_no`，包含租户、`active` 和部门 `IN`；
- [ ] `compiled.params` 中有正确的租户、状态及**两个部门组成的列表**；
- [ ] 部门名称不直接出现在默认编译的 SQL 文本中；
- [ ] 整个 `test_query_plan.py` 通过。

本题测试的是**SQL 构造与参数绑定**。要测试“数据库里究竟查出哪两个人”，应另写带测试数据的数据库集成测试调用 `select_candidate_ids()`；那是更下一层，不是本题的最低验收要求。

## 5.7 设计取舍与常见误解

- **为什么不在测试里先调用 `compile_query_plan()`？**本题只验证固定计划能否安全翻译为 SQL；接入 LLM 会让输出不确定、测试变慢，还无法孤立定位是模型还是 SQL 构造器出错。
- **为什么不手工拼 `department IN ('数据平台部','研发部')`？**会绕过 SQLAlchemy 的绑定与转义机制，并引入注入和引号处理风险。应使用 `EmployeeProfile.department.in_(value)`。
- **`today` 为什么传入？**为了与现有测试风格一致并使时间相关条件可重复；只测部门时它不改变 SQL。
- **`IN []` 怎么办？**当前 DSL 的 `FilterCondition` 会接受空列表；是否拒绝应由产品定义。可追加边界测试，但不要把“空列表”误认为“没有部门过滤条件”。

---
## 5.8 当前控制台报错：pytest 尚未运行到作业一

你贴出的报错重点是：

```text
backend/app/query_plan.py, line 53
    continue
SyntaxError: 'continue' not properly in loop
ERROR: found no collectors for ...::test_department_in_uses_bound_parameters
```

这不是部门 `IN` 或绑定参数断言失败，也不是 `uv` 的 hardlink 警告导致失败。Python 在导入 `app.query_plan` 时首先解析整个文件；当前第 50～56 行的 `FilterCondition.validate_age_range(self)` 仍是未完成的作业二草稿，`continue` 位于任何 `for`/`while` 之外，整个模块无法导入。**即使只指定作业一的单个测试，pytest 仍必须先导入模块，因此收集阶段直接失败。** `found no collectors` 是导入失败的连带信息；测试函数在 `backend/tests/test_query_plan.py` 中已经存在。

修复顺序（不需要先改作业一的测试）：

1. 在 `backend/app/query_plan.py` 暂时删除/注释掉第 49～56 行未完成的 `@model_validator` 和方法；或者依照下面 6.8 节完整实现静态方法并从 `QueryPlan` 调用。不要只把 `continue` 改成 `return`：那既不能检测两条年龄条件，还可能没有正确返回 `self`。
2. 先运行 `uv run python -c "import app.query_plan; print('import ok')"` 确认模块可导入，再运行 `uv run pytest tests/test_query_plan.py::test_department_in_uses_bound_parameters -q`。
3. 只有看到该测试实际被收集且通过（如 `1 passed`），再确认 `compiled.params` 的断言，作业一才算完成；然后运行整个测试文件。若出现新报错，按新的堆栈分别排查，不能把当前收集失败理解为绑定参数问题。

`tokenizers` 缺少 `RECORD` 和 hardlink 回退是环境警告，应单独排查；它们不是当前 `SyntaxError` 的根因。不要为了消除警告而直接删除整个虚拟环境。

---
# 6. 课后作业二：增加年龄区间冲突检测

## 6.1 要处理的查询

```text
查找 35 岁以上、29 岁以下的候选人
```

合理的模型输出可能是：

```json
[
  {"field": "age", "operator": "gte", "value": 35},
  {"field": "age", "operator": "lte", "value": 29}
]
```

每一项单独看都合法，但交集为空：

```text
age >= 35 AND age <= 29
```

这说明：

> **字段级校验通过，不代表整个查询计划在业务上可满足。**

---

## 6.2 老师想让你理解什么

Pydantic 可以检查：

- 字段是不是 `age`；
- 操作符是不是允许值；
- `in` 是否接收列表。

但冲突检测属于跨对象不变量：

```text
多个 FilterCondition 组合起来是否还有解？
```

它需要 Query Plan 级别的校验。

类似问题还包括：

- `department == 数据部` 且 `department == 财务部`；
- `years_of_experience >= 10` 且 `< 3`；
- `region in [深圳]` 且 `region == 北京`；
- 相同字段多个 `in` 集合交集为空。

作业只要求年龄，但你应理解这是“约束可满足性”问题。

---

## 6.3 最小可用实现：计算年龄上下界

在 `backend/app/query_plan.py` 增加辅助函数：

```python
def _validate_age_range(filters: list[FilterCondition]) -> None:
    lower: tuple[float, bool] | None = None
    upper: tuple[float, bool] | None = None

    for item in filters:
        if item.field != FilterField.AGE:
            continue

        if item.operator not in {"gt", "gte", "lt", "lte"}:
            continue

        value = float(item.value)

        if item.operator in {"gt", "gte"}:
            inclusive = item.operator == "gte"
            if (
                lower is None
                or value > lower[0]
                or (value == lower[0] and not inclusive and lower[1])
            ):
                lower = (value, inclusive)

        if item.operator in {"lt", "lte"}:
            inclusive = item.operator == "lte"
            if (
                upper is None
                or value < upper[0]
                or (value == upper[0] and not inclusive and upper[1])
            ):
                upper = (value, inclusive)

    if lower is None or upper is None:
        return

    lower_value, lower_inclusive = lower
    upper_value, upper_inclusive = upper

    impossible = (
        lower_value > upper_value
        or (
            lower_value == upper_value
            and not (lower_inclusive and upper_inclusive)
        )
    )

    if impossible:
        raise ValueError(
            "年龄区间冲突："
            f"下界为 {lower_value}，上界为 {upper_value}"
        )
```

### 为什么需要记录 inclusive

下面两个范围不同：

```text
age >= 35 AND age <= 35  → age == 35，有解
age > 35  AND age <= 35  → 无解
age >= 35 AND age < 35   → 无解
```

只比较上下界数值，不记录开区间/闭区间，会漏掉第二、第三种冲突。

---

## 6.4 把校验放在哪里

### 方案 A：放进 `QueryPlan` 的 model validator

```python
class QueryPlan(BaseModel):
    # 原字段省略

    @model_validator(mode="after")
    def validate_filter_consistency(self):
        _validate_age_range(self.filters)
        return self
```

优点：

- 一旦 Query Plan 被构造，立即保证内部一致；
- API 请求体、LLM 输出、测试手工构造都走同一套规则；
- 最符合“不合法状态不可表示”的思想。

缺点：

- LLM 结构化输出发生冲突时会抛 Pydantic `ValidationError`；
- 当前 `/talent-search/plan` 把所有异常都映射成 503，需要同步改错误码。

### 方案 B：执行 SQL 前校验

在 `build_candidate_statement()` 中：

```python
_validate_age_range(plan.filters)
```

优点：

- `/talent-search/candidates` 已经把 `ValueError` 映射为 422；
- 改动更小。

缺点：

- `/talent-search/plan` 仍可能返回一个逻辑冲突的计划；
- Query Plan 在系统中存在一段“结构合法但不可执行”的状态。

### 推荐结论

作业最小方案可以放在 SQL Builder 前；工程化方案推荐：

1. Query Plan 构造时校验；
2. API 把业务校验错误统一映射为 422；
3. 错误响应中返回稳定错误码和冲突细节。

---

## 6.5 添加测试

如果使用 Query Plan validator：

```python
def test_rejects_impossible_age_range():
    with pytest.raises(ValidationError, match="年龄区间冲突"):
        QueryPlan(
            task_type="find_talent",
            filters=[
                FilterCondition(field="age", operator="gte", value=35),
                FilterCondition(field="age", operator="lte", value=29),
            ],
        )
```

再补三个重要边界：

```python
def test_accepts_single_point_inclusive_age_range():
    plan = QueryPlan(
        task_type="find_talent",
        filters=[
            FilterCondition(field="age", operator="gte", value=35),
            FilterCondition(field="age", operator="lte", value=35),
        ],
    )
    assert plan.executable


def test_rejects_equal_boundary_when_lower_is_exclusive():
    with pytest.raises(ValidationError, match="年龄区间冲突"):
        QueryPlan(
            task_type="find_talent",
            filters=[
                FilterCondition(field="age", operator="gt", value=35),
                FilterCondition(field="age", operator="lte", value=35),
            ],
        )


def test_accepts_normal_age_range():
    plan = QueryPlan(
        task_type="find_talent",
        filters=[
            FilterCondition(field="age", operator="gte", value=29),
            FilterCondition(field="age", operator="lte", value=35),
        ],
    )
    assert len(plan.filters) == 2
```

如果校验放在 `build_candidate_statement()`，则测试改为：

```python
def test_candidate_statement_rejects_impossible_age_range():
    plan = _plan(
        FilterCondition(field="age", operator="gte", value=35),
        FilterCondition(field="age", operator="lte", value=29),
    )

    with pytest.raises(ValueError, match="年龄区间冲突"):
        build_candidate_statement(
            plan,
            tenant_id="course-demo",
            today=date(2026, 9, 23),
        )
```

---

## 6.6 API 应返回 422，而不是 503

在 `create_talent_query_plan()` 中建议区分业务错误：

```python
@router.post("/talent-search/plan")
def create_talent_query_plan(payload: QueryPlanInput):
    model = get_chat_model(temperature=0)
    if model is None:
        raise HTTPException(503, "查询计划模型未配置")

    try:
        return compile_query_plan(payload.query, model)
    except ValueError as exc:
        raise HTTPException(
            422,
            {
                "code": "invalid_query_plan",
                "message": str(exc),
            },
        ) from exc
    except Exception as exc:
        logger.exception("query_plan_compile_failed")
        raise HTTPException(503, f"查询计划生成失败: {exc}") from exc
```

在完整检索接口的异常处理末尾也增加：

```python
except HTTPException:
    raise
except ValueError as exc:
    raise HTTPException(
        422,
        {
            "code": "invalid_query_plan",
            "message": str(exc),
        },
    ) from exc
except Exception as exc:
    # 真正的服务故障才返回 503
```

### 422 与 503 的语义区别

| 状态码 | 含义 | 示例 |
|---|---|---|
| 422 | 请求语义不可执行 | 35 岁以上且 29 岁以下 |
| 503 | 依赖服务当前不可用 | Chat Model、Embedding、Rerank 未配置或调用失败 |

---

## 6.7 运行和验收

```powershell
cd D:\Code\K_Course\talent-eval-agents_learning\backend
uv run pytest tests/test_query_plan.py -q
uv run pytest tests/test_talent_search_api.py -q
```

验收清单：

- `[35, 29]` 的矛盾范围被拒绝；
- `>=35 AND <=35` 被接受；
- `>35 AND <=35` 被拒绝；
- 普通年龄范围仍能编译；
- 接口返回 422，而不是 503；
- 错误消息能指出年龄区间冲突。

---

## 6.8 另一种完成方法：把跨条件检测写为 `FilterCondition` 的静态方法

**你的判断有道理，但要区分“方法放在哪个类里”与“校验由谁触发”。**已有的 `validate_operator_and_value(self)` 是 Pydantic 单条条件校验器：`self` 只是一条 `age >= 35`，无法知道另一条 `age <= 29`。因此不能仅给它追加一次 `if self.value ...` 来完成区间冲突检测。下面方案把**处理一组条件的算法**放进 `FilterCondition.validate_age_range(filters)`，由**持有整个列表的 `QueryPlan` 校验器**调用；保留现有单条条件校验器。

> 本方案与 6.3～6.4 节的模块级 `_validate_age_range(filters)` 是**二选一的实现路径**：把算法搬到静态方法后，不要在两个地方重复维护相同逻辑。示例中的代码是拟实施方案，不能在当前尚有 `SyntaxError` 的项目上直接声称测试通过。

### 第一步：移除当前写了一半的方法

当前 `backend/app/query_plan.py` 第 49～56 行是 `@model_validator(mode="after") def validate_age_range(self)`，其中 `continue` 不在循环内，导致整个模块无法导入。删除这一整段未完成的方法（**不要删前面的 `validate_operator_and_value`**）。`@model_validator` 的单个 `self` 不适合读取另一个 `FilterCondition`。

### 第二步：在 `FilterCondition` 类中添加静态方法

保留现有字段与 `validate_operator_and_value()` 原样，在该类内、`class SemanticRequirement` 之前添加下列方法；`from __future__ import annotations` 和 `FilterField` 在当前文件中已经存在：

```python
    @staticmethod
    def validate_age_range(filters: list[FilterCondition]) -> None:
        """检查整个计划中的多条年龄条件是否还有交集。"""
        lower: tuple[float, bool] | None = None  # (下界值, 是否包含端点)
        upper: tuple[float, bool] | None = None  # (上界值, 是否包含端点)

        for item in filters:
            if item.field != FilterField.AGE:
                continue  # 此处在 for 循环内部，可以使用 continue
            if item.operator not in {"gt", "gte", "lt", "lte"}:
                continue
            if not isinstance(item.value, (int, float)) or isinstance(item.value, bool):
                raise ValueError("年龄阈值必须是数字")
            value = float(item.value)

            if item.operator in {"gt", "gte"}:
                inclusive = item.operator == "gte"
                if (lower is None or value > lower[0]
                        or (value == lower[0] and not inclusive and lower[1])):
                    lower = (value, inclusive)
            else:
                inclusive = item.operator == "lte"
                if (upper is None or value < upper[0]
                        or (value == upper[0] and not inclusive and upper[1])):
                    upper = (value, inclusive)

        if lower is None or upper is None:
            return  # 只有一侧边界，不构成上下界冲突
        if lower[0] > upper[0] or (
            lower[0] == upper[0] and not (lower[1] and upper[1])
        ):
            raise ValueError(
                f"年龄区间冲突：下界 {lower[0]}，上界 {upper[0]}"
            )
```

`lower` 选最大的下界，`upper` 选最小的上界；同值时，排他边界 `>` 或 `<` 比包含边界更严格。这里校验的是**年龄数值区间**，而非 `birth_date` 的日历换算。为与当前 `_age_spec()` 的 `int(item.value)` 保持完全一致，正式实现还应在单条年龄条件校验中要求非负整数（避免小数被 SQL 编译静默截断、非数值字符串延迟到 SQL 阶段才报错）；这个输入类型收紧属于额外完善，不是下面冲突测试的前提。

### 第三步：让 `QueryPlan` 在构造时调用它

在 `QueryPlan` 字段定义之后、`executable` 属性之前添加：

```python
    @model_validator(mode="after")
    def validate_filter_consistency(self):
        FilterCondition.validate_age_range(self.filters)
        return self
```

**调用链**：`QueryPlan(...)` 或 Pydantic 解析 API 请求体 / LLM 结构化输出 → 每条 `FilterCondition` 先做自身校验 → `QueryPlan.validate_filter_consistency()` 查看整组条件 → `FilterCondition.validate_age_range(self.filters)` → 有冲突抛 `ValueError`，Pydantic 将其包装为 `ValidationError`；无冲突则返回 `QueryPlan`。因此 SQL Builder 不需要再写一份年龄区间检查。

### 第四步：按输入和输出验证（复用 6.5 节测试）

```python
# 单条各自合法，但组合后无解：创建 QueryPlan 时应抛 ValidationError。
with pytest.raises(ValidationError, match="年龄区间冲突"):
    _plan(
        FilterCondition(field="age", operator="gte", value=35),
        FilterCondition(field="age", operator="lte", value=29),
    )

# 两端均包含 35：构造成功。
plan = _plan(
    FilterCondition(field="age", operator="gte", value=35),
    FilterCondition(field="age", operator="lte", value=35),
)
assert plan.executable

# 一侧排除 35：构造时应抛 ValidationError。
with pytest.raises(ValidationError, match="年龄区间冲突"):
    _plan(
        FilterCondition(field="age", operator="gt", value=35),
        FilterCondition(field="age", operator="lte", value=35),
    )
```

还要测多条下界/上界取最严格值、正常范围，以及混入 `region` 条件不影响年龄校验。可使用 6.7 节命令运行全文件。**请勿同时保留 6.5 节的 SQL Builder 校验版测试**：静态方法 + `QueryPlan` 校验版在构造 `QueryPlan` 的阶段就报错，根本到不了 `build_candidate_statement()`。

### 接口错误语义（与校验位置有关）

手动向 `/api/talent-search/candidates` 发送冲突计划时，FastAPI 解析请求体会直接返回 422，处理函数及其内部的 `try/except ValueError` 都尚未执行；`/api/talent-search/plan` 与完整 `/api/talent-search` 在 `compile_query_plan()` 内拿到模型输出后才验证，它们目前把异常兜底为 503。生产环境需**专门识别可归因于用户条件的冲突**并映射成 422，但不要把所有 LLM 解析失败、网络错误或无关 `ValueError` 一概映射为 422；具体改造与接口测试见 6.6 节的方向性示例。

---
# 7. 课后作业三：两项经历要求生成两个 `semantic_requirements`

## 7.1 推荐测试查询

```text
查找有 Flink 实时计算项目经历，并且有团队管理经历的候选人
```

期望：

```json
"semantic_requirements": [
  {
    "requirement_id": "S1",
    "query": "Flink 实时计算项目经历",
    "required": true
  },
  {
    "requirement_id": "S2",
    "query": "团队管理经历",
    "required": true
  }
]
```

不期望：

```json
"semantic_requirements": [
  {
    "requirement_id": "S1",
    "query": "Flink 实时计算项目经历并且团队管理经历"
  }
]
```

---

## 7.2 老师想让你理解什么

### 一个复合查询不等于两个独立约束

如果把两个要求混成一句，检索器可能返回：

- 只讲 Flink、完全没讲管理的 Chunk；
- 只讲管理、完全没讲 Flink 的 Chunk；
- 两者都浅浅提到但都不足以构成证据的 Chunk。

拆成 S1、S2 后，系统可以形成证据矩阵：

| 候选人 | S1：Flink | S2：团队管理 | 是否覆盖全部必需项 |
|---|---:|---:|---:|
| C001 | 有 | 有 | 是 |
| C004 | 有 | 无 | 否 |
| C008 | 无 | 有 | 否 |

当前项目已经保留 `requirement_id`，但还没有真正按候选人执行最后一列的 AND 判断。这是作业四应指出的限制。

---

## 7.3 增加“编译结果包含两项”的单元测试

在 `backend/tests/test_query_plan.py` 中添加：

```python
def test_compiler_generates_two_semantic_requirements_for_two_experiences():
    seen = {}

    class StructuredModel:
        def invoke(self, messages):
            seen["messages"] = messages
            return QueryPlan.model_validate(
                {
                    "task_type": "find_talent",
                    "filters": [],
                    "semantic_requirements": [
                        {
                            "requirement_id": "S1",
                            "query": "Flink 实时计算项目经历",
                            "required": True,
                        },
                        {
                            "requirement_id": "S2",
                            "query": "团队管理经历",
                            "required": True,
                        },
                    ],
                    "preferences": [],
                    "clarifications": [],
                }
            )

    class Model:
        def with_structured_output(self, schema):
            assert schema is QueryPlan
            return StructuredModel()

    plan = compile_query_plan(
        "查找有 Flink 实时计算项目经历，并且有团队管理经历的候选人",
        Model(),
    )

    assert [item.requirement_id for item in plan.semantic_requirements] == [
        "S1",
        "S2",
    ]
    assert [item.query for item in plan.semantic_requirements] == [
        "Flink 实时计算项目经历",
        "团队管理经历",
    ]
    assert all(item.required for item in plan.semantic_requirements)
    assert "Flink" in seen["messages"][1][1]
    assert "团队管理" in seen["messages"][1][1]
```

这个测试验证的是“结构化输出与后续执行契约”，不是验证真实模型理解能力。Fake Model 的价值是让测试稳定、快速、不消耗外部 API。

---

## 7.4 加强系统提示词（推荐）

当前提示词只说：

```text
经历、技能、项目成果等材料内容写入 semantic_requirements。
```

可以补充：

```text
若用户明确提出多项彼此独立的经历、技能或成果要求，必须拆成多个
semantic_requirements，并使用连续编号 S1、S2、S3；不得把多项必需要求
合并为一个长查询。每项 query 应保持原始约束，不增加用户未表达的条件。
```

修改后应补提示词测试：

```python
def test_query_compiler_prompt_requires_independent_semantic_requirements():
    # 复用记录 messages 的 Fake Model
    compile_query_plan("有 Flink 和管理经历", Model())
    system_prompt = seen["messages"][0][1]
    assert "拆成多个" in system_prompt
    assert "S1、S2" in system_prompt
```

### 为什么 Schema 本身不能保证“一项需求一个元素”

Pydantic 只能保证：

- 列表元素类型正确；
- ID 格式满足 `^S\d+$`；
- query 非空且不超过 500 字。

它不能仅凭结构判断一句 query 中是否偷偷合并了两项语义要求。这仍然需要：

1. 提示词约束；
2. 代表性样例；
3. 模型输出评测集；
4. 必要时增加第二阶段计划审查器。

---

## 7.5 增加执行层测试

仓库现有测试已经覆盖：

```python
def test_each_semantic_requirement_keeps_its_own_evidence():
```

它证明 `execute_composite_search()` 会按 `S1/S2` 分开保存证据。建议再明确验证调用次数和查询：

```python
def test_two_semantic_requirements_run_two_searches():
    calls = []
    plan = QueryPlan(
        task_type="find_talent",
        semantic_requirements=[
            {"requirement_id": "S1", "query": "Flink 项目经历"},
            {"requirement_id": "S2", "query": "团队管理经历"},
        ],
    )

    execute_composite_search(
        plan,
        candidate_ids=["C001", "C004"],
        search_requirement=lambda query, candidate_ids: calls.append(
            (query, candidate_ids)
        ) or [],
    )

    assert calls == [
        ("Flink 项目经历", ["C001", "C004"]),
        ("团队管理经历", ["C001", "C004"]),
    ]
```

### 验收标准

- 编译结果有且只有 S1、S2 两项；
- 两项 `required=True`；
- 两项分别调用检索；
- 两次调用都携带同一个 SQL 候选集；
- 证据能够追溯到对应 requirement；
- 没有把两项要求拼成一个检索字符串。

---

# 8. 课后作业四：分析 3.6 查询优化策略的限制与改进

## 8.1 当前实现做了什么

优化相关代码见 `backend/app/query_plan.py` 的 `QueryOptimizationPlan`、`choose_optimization_strategy()` 和 `search_with_optimization()`（当前约在第 273～335 行；以实际文件为准）。

### 优化计划 Schema

```python
class QueryOptimizationPlan(BaseModel):
    strategy: Literal["rewrite", "multi_query", "decompose"]
    queries: list[str] = Field(min_length=1, max_length=3)
    reason: str
```

### 策略选择

```python
def choose_optimization_strategy(
    *,
    hit_count: int,
    requirement_count: int,
    expression_is_vague: bool = False,
) -> str | None:
    if hit_count > 0:
        return None
    if requirement_count > 1:
        return "decompose"
    if expression_is_vague:
        return "rewrite"
    return "multi_query"
```

逻辑是：

```text
有命中 → 不优化
无命中 + 多项要求 → decompose
无命中 + 表达模糊 → rewrite
无命中 + 其他情况 → multi_query
```

### 执行流程

```python
first_pass = search_query(requirement.query)
strategy = choose_optimization_strategy(...)

if strategy is None:
    return first_pass

optimization = optimize_semantic_query(...)
fallback_results = [search_query(query) for query in optimization.queries]
return merge_query_results([first_pass, *fallback_results])
```

这是一个合理的教学版骨架：先正常检索，失败后才付出额外模型调用和多次搜索成本。

---

## 8.2 限制一：端到端接口没有传策略所需上下文

`search_with_optimization()` 支持：

```python
requirement_count
expression_is_vague
```

但 `backend/app/api.py` 实际调用是：

```python
outcome = search_with_optimization(
    requirement,
    search_query=run_hybrid_search,
    model=model,
)
```

没有传后两个参数，因此永远使用默认值：

```python
requirement_count = 1
expression_is_vague = False
```

于是完整接口中：

- `rewrite` 基本不会触发；
- `decompose` 基本不会触发；
- 首轮为空时通常只会进入 `multi_query`。

这就是“单元测试覆盖了策略函数，但端到端能力没有真正接通”的典型问题。

### 如何改进

如果每个 `SemanticRequirement` 已经是原子要求，那么 `requirement_count` 不应简单使用：

```python
len(plan.semantic_requirements)
```

因为 API 正在逐项调用优化器；S1 的优化不应该因为 Query Plan 还有 S2 就把 S1 再次分解。

更合理的设计是给语义要求增加元数据，例如：

```python
class SemanticRequirement(BaseModel):
    requirement_id: str
    query: str
    required: bool = True
    atomic: bool = True
    vague: bool = False
```

或者让编译阶段直接把不可执行的模糊表达放入 `clarifications`，让 `rewrite` 只处理“表达宽泛但不需要用户补信息”的情况。

必须区分：

| 情况 | 应对方式 |
|---|---|
| “比较资深” | 缺阈值，要求澄清，不能擅自 rewrite 成“5年以上” |
| “做过 AI 相关工作” | 条件宽泛但可检索，可改写成多个不增加约束的同义查询 |
| “Flink 项目和团队管理” | 编译阶段拆成 S1、S2，不应等到检索失败再 decompose |

---

### 8.2.1 两个参数是什么、从哪里来

在 `backend/app/query_plan.py` 中，`search_with_optimization()` 接收一条 `SemanticRequirement`，**先用它的原始 `query` 检索一次**，把结果长度作为 `hit_count`，然后调用：

```python
strategy = choose_optimization_strategy(
    hit_count=len(first_pass),
    requirement_count=requirement_count,
    expression_is_vague=expression_is_vague,
)
```

这里的同名写法 `requirement_count=requirement_count` 是“**左侧：被调用函数的参数名；右侧：当前函数收到的局部变量**”，没有重新计算数量。`expression_is_vague=expression_is_vague` 同理。两者是**优化策略上下文**，不属于 SQL 候选集过滤条件，也不是 `QueryOptimizationPlan.queries` 的数量上限。

| 参数 | 含义及作用 | 来源（当前实现） |
|---|---|---|
| `requirement_count: int = 1` | 预期表示**当前正在优化的这一条检索表达里有几个独立语义子要求**；大于 1 且首轮零命中时，优先选择 `decompose`（拆分复合要求）。它**不是** `len(first_pass)`，也不应机械地等于整个 `plan.semantic_requirements` 的条数。 | `search_with_optimization()` 的调用者可显式传入；当前 HTTP 接口没有传，故默认 `1`。代码没有自动解析/验证表达里的子要求数量。 |
| `expression_is_vague: bool = False` | 表示当前表达**足够宽泛、适合做不增加约束的检索改写**；当首轮零命中、且没有多子要求需要拆分时，`True` 选择 `rewrite`，`False` 选择 `multi_query`。 | 调用者显式判断并传入；当前没有从文本或 LLM 自动标注，HTTP 接口也没有传，故默认 `False`。 |

例如“Flink 项目经历和团队管理经历”如果**尚未拆分**、仍是同一条 `SemanticRequirement.query`，可以将它的子要求数标为 `2`；如果 `compile_query_plan()` 已把它拆为 `S1=Flink 项目经历`、`S2=团队管理经历`，循环中分别优化的当前要求通常各有 `1` 个子要求，**不能因为全计划有两条要求就给每次调用传 `requirement_count=2`**。如果直接将 `len(plan.semantic_requirements)` 当作此参数，两个已原子化的要求会被错误地分别送进 `decompose`。

“做过 AI 相关工作”可能宽泛，`expression_is_vague=True` 可以作为**检索改写策略提示**；但“比较资深”缺少明确阈值，属于 `QueryPlan.clarifications`，应请用户澄清，**不能**假设 `True` 后让模型擅自补出“5 年以上”。这个布尔值表达的是系统已经做出的判断，并非优化函数自动完成的歧义检测。

### 8.2.2 参数怎样影响分支：优先级与返回值

`choose_optimization_strategy()` 的真实优先级是：

```text
首轮 hit_count > 0         → None（不优化；后两个参数即使传入也不会生效）
首轮 hit_count == 0 且 requirement_count > 1 → decompose
首轮 hit_count == 0 且 requirement_count <= 1 且 expression_is_vague=True → rewrite
其他零命中情况             → multi_query
```

因此当 `requirement_count=2` 且 `expression_is_vague=True` 同时成立时，**decompose 优先**。函数并未检查负数等非法数量；上游若新增元数据，应在 Schema 或调用层约束有效范围。几个仅针对策略函数的测试输入和预期输出：

```python
assert choose_optimization_strategy(
    hit_count=3, requirement_count=2, expression_is_vague=True
) is None
assert choose_optimization_strategy(
    hit_count=0, requirement_count=2, expression_is_vague=True
) == "decompose"
assert choose_optimization_strategy(
    hit_count=0, requirement_count=1, expression_is_vague=True
) == "rewrite"
assert choose_optimization_strategy(
    hit_count=0, requirement_count=1, expression_is_vague=False
) == "multi_query"
```

`search_with_optimization()` 的返回值是字典 `{"strategy": ..., "queries": [...], "results": [...]}`：若首轮有命中，`strategy=None`，`queries` 只有原始文本，`results` 就是首轮结果；若零命中，先用**选中的策略**请求模型生成 `QueryOptimizationPlan`（其中 `queries` 最多 3 条），再对生成的查询逐条调用 `search_query`，把首轮和补检的 Chunk 按 `chunk_id` 去重，作为 `results` 返回。`queries` 则列出原始文本和模型生成的文本。**调用这两个参数本身既不召回结果，也不生成查询；它们只决定零命中后的策略分支。**当前返回的 `strategy` 取自模型输出的 `optimization.strategy`，尚未程序化检查是否与刚才选中的策略相同，这是另一个待完善点。

### 8.2.3 系统设计：把“检索信号”和“表达语义”分开

`hit_count` 是**运行时反馈**（第一轮有没有 Chunk）；`requirement_count` 和 `expression_is_vague` 是**对当前检索表达的语义诊断**。把两类信号结合才能回答“是否值得付出额外模型/检索成本，以及该怎样补救”：复合意图先拆分、宽泛表述可在原约束内改写、明确而零命中的表述可做同义多查询。但目前只是按规则分支的教学版，不是经过效果评估的优化决策器。

若要真正接入，建议编译阶段尽量将复合查询拆成独立 `SemanticRequirement`，并在语义要求上显式记录**针对当前要求**的原子性/可安全改写性（或定义单独的优化上下文）；在检索入口把经过验证的上下文传给 `search_with_optimization()`，为 `rewrite` 保留不新增用户约束的守卫。现在 `/api/talent-search` 中 `for requirement in plan.semantic_requirements` 的调用没有传两个参数，所以端到端运行时默认 `1/False`，**首轮零命中实际选择 `multi_query`**；单元测试显式传值只能证明选择函数的分支，不能证明 HTTP 链路已支持 rewrite/decompose。`retrieval_mode="standard"` 更是不会调用 `search_with_optimization()`，这两个参数也就无从发挥作用。

---
## 8.3 限制二：只看 `hit_count == 0`，没有质量门槛

当前逻辑：

```python
if hit_count > 0:
    return None
```

只要返回一条结果，即使它：

- Rerank 分数很低；
- 只是弱相关；
- 来自模板性文字；
- 没有包含关键实体；
- 只有一个候选人的重复 Chunk；

也不会触发优化。

### 如何改进

定义“有效命中”，不要只看数量：

```python
class RetrievalQuality(BaseModel):
    hit_count: int
    top_score: float | None
    distinct_candidates: int
    required_term_coverage: float
```

示例判断：

```python
def needs_optimization(quality: RetrievalQuality) -> bool:
    return (
        quality.hit_count == 0
        or quality.top_score is None
        or quality.top_score < 0.35
        or quality.distinct_candidates == 0
    )
```

阈值不能拍脑袋，应使用验证集标定。更进一步可以把原因分类为：

```text
no_hits
low_rerank_score
low_candidate_coverage
entity_mismatch
insufficient_evidence
```

不同失败原因选择不同优化策略。

---

## 8.4 限制三：生成查询只受提示词约束，缺少程序化守卫

提示词说：

```text
保留原始人才条件，不增加年限、职级或技能要求。
```

但当前程序只验证：

- strategy 是三个 Literal 之一；
- query 列表 1~3 条；
- 字符串类型正确。

无法防止模型把：

```text
“AI 项目经验”
```

改写成：

```text
“5 年以上大模型项目经验并担任负责人”
```

这增加了用户没有提供的条件。

### 如何改进

1. 优化计划返回“每条改写与原要求的关系”：

```python
class OptimizedQuery(BaseModel):
    query: str
    preserved_constraints: list[str]
    added_constraints: list[str] = []
```

2. 程序拒绝 `added_constraints` 非空；
3. 使用第二个结构化模型或规则检查约束漂移；
4. 保留原始 query，并把改写查询视为补充召回，而非替代原意；
5. 对技能名、数值、否定词等关键 token 做保留检查。

还应校验模型返回的策略与请求策略一致：

```python
if optimization.strategy != strategy:
    raise ValueError("优化模型返回了与请求不一致的策略")
```

当前代码直接信任 `optimization.strategy`。

---

## 8.5 限制四：多查询结果只是“按首次出现去重”

当前：

```python
def merge_query_results(result_sets):
    # 保留每个 chunk 第一次出现的位置
```

问题：

1. 第一条查询天然占据排序优势；
2. 不同查询分别 Rerank 后的分数不一定可直接比较；
3. 同一个 Chunk 被多个改写查询命中，没有获得额外共识加分；
4. 没有在合并后的候选池上做一次全局 Rerank；
5. 结果顺序依赖查询生成顺序。

### 更好的合并方案

```text
每条改写查询各自召回 Top-N
  → 对各列表按 rank 做 RRF
  → 按 chunk_id 去重并累计多查询共识
  → 形成较大的统一候选池
  → 用原始 requirement.query 做一次全局 Rerank
  → 截取最终 Top-K
```

关键点：最终 Rerank 应使用**原始要求**，而不是某条改写，避免排序目标被改写查询带偏。

伪代码：

```python
query_result_sets = [search_recall_only(query) for query in queries]
fused = rrf_fuse(query_result_sets)
final = rerank(
    query=requirement.query,
    documents=fused[:rerank_pool_size],
)
```

---

## 8.6 限制五：多个必需语义要求没有候选人级 AND 语义

当前完整接口把所有要求的 Chunk 合并后返回：

```python
result_sets.append(outcome["results"])
chunks = merge_query_results(result_sets)
```

但没有做：

```text
候选人 C001 是否同时有 S1 和 S2 的有效证据？
```

因此用户要求：

```text
必须有 Flink 项目，并且必须有团队管理经历
```

结果中可能同时出现：

- 只满足 S1 的 C001；
- 只满足 S2 的 C004。

如果上层直接把两人都展示为“满足查询”，逻辑就是错的。

### 如何改进：建立候选人证据覆盖表

```python
coverage: dict[str, set[str]] = {}

for requirement_id, results in evidence_by_requirement.items():
    for item in results:
        coverage.setdefault(item.candidate_id, set()).add(requirement_id)

required_ids = {
    item.requirement_id
    for item in plan.semantic_requirements
    if item.required
}

qualified_candidate_ids = [
    candidate_id
    for candidate_id, covered_ids in coverage.items()
    if required_ids.issubset(covered_ids)
]
```

但还必须定义“有效证据”阈值，不能任意低分 Chunk 都算覆盖。

推荐最终输出候选人级结构：

```json
{
  "candidate_id": "C001",
  "required_coverage": 1.0,
  "matched_requirement_ids": ["S1", "S2"],
  "missing_requirement_ids": [],
  "evidence": {
    "S1": ["chunk-a"],
    "S2": ["chunk-b"]
  }
}
```

---

## 8.7 限制六：`required` 字段当前没有参与执行决策

`SemanticRequirement.required` 已经存在，但 API 对 `required=True/False` 的处理完全相同。

更合理的语义：

- `required=True`：候选人必须有达到阈值的证据；
- `required=False`：作为加分项，不排除候选人；
- `preferences`：可能需要独立评估和排序，不应与证据硬要求混淆。

可以把最后评分拆成：

```text
先用 SQL 硬条件过滤
再用 required semantic requirements 做证据覆盖门槛
最后用 optional requirements + preferences 做排序加分
```

---

## 8.8 限制七：额外调用串行执行，延迟与费用线性增加

当前：

```python
fallback_results = [
    search_query(query)
    for query in optimization.queries
]
```

最多三条改写会串行调用：

```text
Embedding → Milvus Hybrid → Rerank
```

每次都可能包含外部网络请求。延迟接近线性叠加。

### 如何改进

- 多查询并发执行；
- 对相同查询做短期缓存；
- 设定每次请求的检索预算；
- 设定总超时；
- 首轮质量尚可时不要盲目扩展；
- 对昂贵 Rerank 采用“召回并行、统一精排一次”。

建议预算对象：

```python
class RetrievalBudget(BaseModel):
    max_generated_queries: int = 3
    max_total_retrieval_calls: int = 4
    timeout_ms: int = 5000
    max_rerank_documents: int = 50
```

---

## 8.9 限制八：缺少可观测性和离线评测

当前返回了：

- strategy；
- queries；
- chunk_count。

但生产上还需要：

| 指标 | 用途 |
|---|---|
| 首轮命中数与 top score | 判断为什么触发优化 |
| 生成查询数 | 控制成本 |
| 每条查询耗时 | 找到慢点 |
| Embedding / Milvus / Rerank 分段耗时 | 区分服务瓶颈 |
| 优化前后候选覆盖变化 | 判断优化是否真正有收益 |
| 新增有效 Chunk 数 | 避免只有重复结果 |
| 约束漂移检测结果 | 防止改写改变用户意图 |
| 最终 required coverage | 判断复合条件是否满足 |

离线评测应至少包含：

- 明确条件；
- 同义表达；
- 专有名词；
- 两项以上经历；
- 无结果查询；
- 模糊但可改写查询；
- 必须澄清的查询；
- 对抗性越权查询。

---

## 8.10 推荐的分阶段改进路线

### 第一阶段：先把现有能力接通

1. 在 API 中传入真实的优化上下文；
2. 明确哪些模糊表达要澄清、哪些可以 rewrite；
3. 记录触发原因、首轮质量和额外调用次数；
4. 验证返回策略与请求策略一致。

### 第二阶段：提升结果质量

1. 从 `hit_count` 改为质量门槛；
2. 多查询结果用 RRF 融合；
3. 使用原始 query 做统一全局 Rerank；
4. 建立候选人级 requirement coverage。

### 第三阶段：控制性能和成本

1. 多查询并发召回；
2. 统一精排一次；
3. 查询缓存；
4. 超时和预算；
5. 离线评测驱动阈值。

### 作业报告可以这样总结

> 当前实现提供了 Rewrite、Multi Query、Decompose 三种策略的教学骨架，但端到端接口没有传入 `requirement_count` 和 `expression_is_vague`，实际主要只会触发 Multi Query。优化触发只依赖零命中，未使用 Rerank 分数、候选覆盖或证据质量；多查询结果按首次出现去重，没有跨查询融合和全局精排；多个必需语义要求也没有形成候选人级 AND 约束。改进方向应是：在编译阶段原子化语义要求，在检索阶段基于质量门槛触发优化，用 RRF 和统一 Rerank 合并改写查询，并在候选人层计算必需要求覆盖率，同时通过并发、缓存、预算与可观测性控制延迟和成本。

---

# 9. 课后作业五：新增“政治面貌”字段

## 9.1 老师想让你理解什么

新增业务字段不是只改一个类，而是一条端到端契约：

```text
数据库列
  → SQLAlchemy ORM
  → 写入 API 请求模型
  → EmployeeProfile 创建
  → API 响应序列化
  → 前端类型/表单/列表
  → FilterField 枚举
  → FILTER_FIELD_REGISTRY
  → 自动生成的 Filter DSL 提示词
  → SQL Builder
  → 单元测试与接口测试
```

如果只改 DSL，不改数据库，SQL Builder 会引用不存在的列。

如果只改数据库和 ORM，不改 API，用户无法写入也看不到字段。

如果只改提示词，不改注册表，模型会生成执行器不支持的条件。

---

## 9.2 第一步：先确定字段语义和取值

建议字段名：

```text
political_status
```

教学版可允许中文标准值：

```text
中共党员
中共预备党员
共青团员
群众
民主党派
其他
```

建议不要让数据库中同时出现：

```text
党员
中共党员
中国共产党党员
```

否则 `eq/in` 精确过滤会把同义值当成不同值。

更工程化的设计是存稳定 code：

```text
party_member
probationary_party_member
league_member
mass
other_party
other
```

展示层再映射中文。本作业为了直观，可以先存规范化中文值，但要在报告中说明标准化问题。

同时要确认该字段是否属于敏感个人信息。真实系统中政治面貌可能需要：

- 更严格的访问控制；
- 审计日志；
- 数据最小化；
- 合规与使用目的限制。

课程作业重点是代码链路，但不能忽略安全属性。

---

## 9.3 第二步：修改新数据库初始化脚本

在 `database/init.sql` 的 `employee_profiles` 中加入：

```sql
political_status VARCHAR(32),
```

示例位置：

```sql
CREATE TABLE employee_profiles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    employee_no VARCHAR(64) NOT NULL UNIQUE,
    tenant_id VARCHAR(64) NOT NULL DEFAULT 'course-demo',
    name VARCHAR(128) NOT NULL,
    gender VARCHAR(16),
    birth_date DATE,
    region VARCHAR(128),
    current_position VARCHAR(255),
    job_level VARCHAR(64),
    years_of_experience DOUBLE PRECISION,
    department VARCHAR(255),
    political_status VARCHAR(32),
    employment_status VARCHAR(32) NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### 重要：`init.sql` 不会修改已有数据库卷

Compose 中：

```yaml
./database/init.sql:/docker-entrypoint-initdb.d/001-init.sql:ro
```

PostgreSQL 初始化脚本只在数据目录为空时执行一次。已有 `postgres_data` 卷不会因为你修改 `init.sql` 自动加列。

所以必须增加迁移，例如：

`database/migrations/008_add_political_status.sql`

```sql
ALTER TABLE employee_profiles
ADD COLUMN IF NOT EXISTS political_status VARCHAR(32);

CREATE INDEX IF NOT EXISTS idx_employee_profiles_tenant_political_status
ON employee_profiles (tenant_id, political_status);
```

为什么索引把 `tenant_id` 放前面：

```text
所有候选查询都强制带 tenant_id，复合索引更贴合实际 WHERE 条件。
```

但是否创建索引仍应看：

- 数据量；
- 字段基数；
- 查询频率；
- `EXPLAIN ANALYZE` 结果。

小表或低频字段不一定需要单独索引。

执行迁移示例：

```powershell
Get-Content database\migrations\008_add_political_status.sql -Raw |
  docker compose -p talent-eval-agents-course exec -T postgres `
  psql -U talent -d talent_docs
```

验证：

```powershell
docker compose -p talent-eval-agents-course exec -T postgres `
  psql -U talent -d talent_docs -c "\d employee_profiles"
```

---

## 9.4 第三步：修改 SQLAlchemy ORM

`backend/app/models.py`：

```python
class EmployeeProfile(Base):
    # ...
    department: Mapped[str | None] = mapped_column(String(255))
    political_status: Mapped[str | None] = mapped_column(String(32))
    employment_status: Mapped[str] = mapped_column(
        String(32),
        default="active",
    )
```

ORM 与真实表必须一致，否则可能出现：

```text
column employee_profiles.political_status does not exist
```

---

## 9.5 第四步：修改后端员工写入与输出接口

### 请求模型

`backend/app/api.py` 的 `EmployeeInput`：

```python
class EmployeeInput(BaseModel):
    employee_no: str
    name: str
    gender: str | None = None
    birth_date: date | None = None
    region: str | None = None
    current_position: str | None = None
    job_level: str | None = Field(default=None, pattern=r"^L\d+$")
    years_of_experience: float | None = None
    department: str | None = None
    political_status: str | None = None
```

更严格可使用 Literal：

```python
PoliticalStatus = Literal[
    "中共党员",
    "中共预备党员",
    "共青团员",
    "群众",
    "民主党派",
    "其他",
]

class EmployeeInput(BaseModel):
    # ...
    political_status: PoliticalStatus | None = None
```

### 创建员工为什么不需要再手写赋值

当前：

```python
item = EmployeeProfile(**payload.model_dump())
```

只要 `EmployeeInput` 和 `EmployeeProfile` 都有同名字段，创建接口会自动传入。

### 响应序列化

`employee_json()` 必须加入：

```python
"political_status": item.political_status,
```

否则数据库写入成功，前端响应仍看不到。

建议把当前单行大字典改成多行，减少遗漏：

```python
def employee_json(item: EmployeeProfile, material_count: int = 0):
    # age 计算省略
    return {
        "id": item.id,
        "employee_no": item.employee_no,
        "name": item.name,
        "gender": item.gender,
        "birth_date": item.birth_date,
        "age": age,
        "region": item.region,
        "current_position": item.current_position,
        "job_level": item.job_level,
        "years_of_experience": item.years_of_experience,
        "department": item.department,
        "political_status": item.political_status,
        "employment_status": item.employment_status,
        "material_count": material_count,
    }
```

---

## 9.6 第五步：把新字段加入 Query Plan DSL[重点]

### 枚举

`backend/app/query_plan.py`：

```python
class FilterField(StrEnum):
    # ...
    POLITICAL_STATUS = "political_status"
```

### 注册表

```python
FILTER_FIELD_REGISTRY = {
    # ...
    FilterField.POLITICAL_STATUS: _column_spec(
        EmployeeProfile.political_status,
        "政治面貌，使用系统标准值",
        {"eq", "in"},
    ),
}
```

为什么只允许 `eq/in`：

- 政治面貌是分类字段；
- 没有大小顺序；
- `gt/gte/lt/lte` 没有业务意义。

### 提示词是否需要手工增加字段

不需要在 `QUERY_PLAN_SYSTEM_PROMPT` 再写一遍：

```python
QUERY_PLAN_SYSTEM_PROMPT.format(
    filter_dsl=filter_dsl_catalog()
)
```

注册表更新后，DSL 目录会自动出现：

```text
- political_status: 政治面貌，使用系统标准值; operators=eq,in
```

这正是注册表设计的价值。

---

## 9.7 第六步：修改前端

如果作业要求字段真正可用，前端也应同步。

### TypeScript 类型

`frontend/src/main.tsx`：

```tsx
type Employee = {
  // ...
  department?: string;
  political_status?: string;
  material_count: number;
};
```

### 新增员工表单

```tsx
<label>
  政治面貌
  <select name="political_status" defaultValue="">
    <option value="">未填写</option>
    <option value="中共党员">中共党员</option>
    <option value="中共预备党员">中共预备党员</option>
    <option value="共青团员">共青团员</option>
    <option value="群众">群众</option>
    <option value="民主党派">民主党派</option>
    <option value="其他">其他</option>
  </select>
</label>
```

### 列表展示

当前花名册是固定九列。若增加新列，需要同步：

1. 表头；
2. 每行内容；
3. CSS `grid-template-columns`；
4. 必要时最小宽度。

否则表头和数据会错位。

如果不希望列表过宽，可以先只在详情或搜索条件中展示，但要在作业说明中明确范围。

---

## 9.8 第七步：增加测试

### ORM/API 输入测试

`backend/tests/test_models.py`：

```python
def test_employee_input_accepts_political_status():
    value = EmployeeInput(
        employee_no="C008",
        name="测试员工",
        political_status="中共党员",
    )
    assert value.political_status == "中共党员"
```

如果使用 Literal，再加非法值测试：

```python
def test_employee_input_rejects_unknown_political_status():
    with pytest.raises(ValidationError):
        EmployeeInput(
            employee_no="C009",
            name="测试员工",
            political_status="随意填写的值",
        )
```

### DSL 目录测试

```python
def test_political_status_is_exposed_in_filter_dsl():
    catalog = filter_dsl_catalog()
    assert "political_status: 政治面貌" in catalog
    assert "operators=eq,in" in catalog
```

### 参数化 SQL 测试

```python
def test_political_status_in_is_parameterized():
    plan = _plan(
        FilterCondition(
            field="political_status",
            operator="in",
            value=["中共党员", "中共预备党员"],
        )
    )

    compiled = build_candidate_statement(
        plan,
        tenant_id="course-demo",
    ).compile(dialect=postgresql.dialect())

    parameter_name = next(
        name for name in compiled.params
        if name.startswith("political_status_")
    )

    assert compiled.params[parameter_name] == [
        "中共党员",
        "中共预备党员",
    ]
    assert "中共党员" not in str(compiled)
```

### 操作符限制测试

```python
def test_political_status_rejects_range_operator_at_execution():
    # FilterCondition 的通用 validator 已经会因为非数值字段使用 gte 而拒绝。
    with pytest.raises(ValidationError, match="范围运算符只允许用于数值字段"):
        FilterCondition(
            field="political_status",
            operator="gte",
            value="中共党员",
        )
```

### 接口测试

至少验证 `POST /api/employees`：

- 请求可写入字段；
- 响应返回字段；
- 再次 GET 列表仍能读取字段。

如果使用真实测试数据库，注意回滚或隔离测试数据。

---

## 9.9 第八步：为什么不需要修改 Milvus Schema

政治面貌是结构化硬条件，执行路径是：

```text
PostgreSQL 筛选政治面貌
  → 得到 candidate_ids
  → Milvus 只按 candidate_ids 检索材料
```

因此不需要把政治面貌复制到每个 Milvus Chunk。

这是数据职责分离：

- 员工结构化事实放 PostgreSQL；
- 材料内容和检索索引放 Milvus；
- 候选人 ID 是两个系统的连接键。

只有当你需要在没有 SQL 候选步骤的场景中直接按政治面貌过滤 Milvus，才考虑冗余字段。但那会引入同步、一致性和重建问题，本课不建议这样做。

---

## 9.10 完整改动清单

| 位置 | 是否必须 | 改动 |
|---|---:|---|
| `database/init.sql` | 是 | 新数据库包含列 |
| `database/migrations/008_add_political_status.sql` | 是 | 已有数据库加列 |
| `backend/app/models.py` | 是 | ORM 字段 |
| `backend/app/api.py: EmployeeInput` | 是 | 接收写入 |
| `backend/app/api.py: employee_json` | 是 | 返回字段 |
| `backend/app/query_plan.py: FilterField` | 是 | DSL 字段枚举 |
| `backend/app/query_plan.py: FILTER_FIELD_REGISTRY` | 是 | 描述、操作符、SQL 构造 |
| `backend/tests/test_models.py` | 是 | 输入约束 |
| `backend/tests/test_query_plan.py` | 是 | DSL 与 SQL 编译 |
| `frontend/src/main.tsx` 类型 | 完整产品需要 | 前端识别字段 |
| 前端表单 | 完整产品需要 | 用户录入 |
| 前端展示/CSS | 按产品范围 | 展示字段 |
| Milvus Schema | 否 | 通过 candidate_ids 间接限定 |

---

# 10. 五项作业的推荐实施顺序

不要按作业编号机械修改。推荐按依赖关系实施：

```text
第一步：只加测试理解现有行为
  ├─ 作业 1：department in 参数绑定
  └─ 作业 3：两项语义要求的结构与执行

第二步：增强 Query Plan 合法性
  └─ 作业 2：年龄冲突检测 + 422 错误语义

第三步：扩展完整业务字段
  └─ 作业 5：政治面貌端到端改造

第四步：基于已理解的链路做设计分析
  └─ 作业 4：评价查询优化限制与改进路线
```

原因：

- 作业 1 先帮助你理解 SQL Builder 和参数化；
- 作业 3 帮助你理解 SemanticRequirement 的执行方式；
- 有了这两部分认知，才能正确判断作业 2 应在哪个层级校验；
- 作业 5 是一次综合练习，会同时经过数据库、API、DSL 和测试；
- 作业 4 需要你理解完整链路后才能写出有内容的分析。

## 10.1 每次改动采用“小步测试”

建议循环：

```text
写一个失败测试
  → 运行并确认失败原因正确
  → 做最小实现
  → 运行目标测试
  → 运行同文件测试
  → 运行后端全量测试
  → 检查真实接口或编译输出
```

不是：

```text
一次改十个文件 → 最后统一运行测试 → 不知道哪一步出错
```

## 10.2 推荐命令

```powershell
cd D:\Code\K_Course\talent-eval-agents_learning\backend

# 只跑 Query Plan
uv run pytest tests/test_query_plan.py -q

# 只跑人才检索接口
uv run pytest tests/test_talent_search_api.py -q

# 员工输入模型
uv run pytest tests/test_models.py -q

# 相关测试一起跑
uv run pytest `
  tests/test_query_plan.py `
  tests/test_talent_search_api.py `
  tests/test_models.py `
  -q

# 后端全量回归
uv run pytest -q
```

如果本地 `backend/.venv` 指向了已经删除或移动的 Python，可先执行：

```powershell
cd backend
Remove-Item .venv -Recurse -Force
uv sync --dev
```

删除虚拟环境前应确认当前目录确实是项目 `backend`，不要对不确定的计算路径执行递归删除。

---

# 11. 分层测试：每一层到底在证明什么

## 11.1 Schema 测试

证明：输入结构与局部规则正确。

示例：

- 未知字段 `salary` 被拒绝；
- `in` 必须是列表；
- 非数值字段不能使用范围运算符；
- `requirement_id` 必须符合 `S数字`。

这类测试不应连接数据库或模型服务。

## 11.2 Query Plan 业务一致性测试

证明：多个合法条件组合后仍然可执行。

示例：

- 年龄上下界有交集；
- 年龄上下界冲突被拒绝；
- 相等边界的开闭区间语义正确。

这是作业二所在层。

## 11.3 SQL 编译测试

证明：

- 条件生成了正确 SQLAlchemy 表达式；
- 用户值通过参数绑定；
- 租户和 active 条件被强制加入；
- 年龄比较方向正确；
- `in` 集合通过 expanding 参数处理。

这是作业一所在层。

注意：SQL 编译测试证明 SQL 的形状和参数，不完全等价于真实 PostgreSQL 执行正确。涉及方言、索引或 NULL 语义时，还应补数据库集成测试。

## 11.4 编排单元测试

用 Fake Search 验证：

- 空候选集不调用检索；
- 每个语义要求独立调用；
- 所有检索都带相同 candidate IDs；
- 证据按 requirement ID 保存；
- 优化查询在首轮失败后执行；
- Chunk 按 ID 去重。

## 11.5 API 测试

用 `TestClient` + dependency override 验证真实 HTTP 边界：

```text
请求 JSON / Header
  → FastAPI 参数解析
  → Query Plan
  → SQL Statement
  → EvidenceFilter
  → 混合检索
  → HTTP Response
```

当前 `test_talent_search_endpoint_runs_query_plan_sql_and_hybrid_search` 已验证：

- 候选集为 `C001/C004`；
- `candidate_ids` 被传给 Milvus Store；
- tenant 和 permission scope 被传入；
- Rerank 分数进入最终结果。

建议新增：

1. 冲突年龄返回 422；
2. 空候选集时 Store 完全未被获取或调用；
3. 两项语义要求调用两次 Store；
4. 缺权限头返回 422/请求校验错误；
5. 空权限字符串返回 403；
6. LLM 试图输出越权字段时计划校验失败；
7. 政治面貌条件进入 SQL 参数。

## 11.6 真实服务验收

单元测试全部通过后，再做：

```text
真实 PostgreSQL
真实 Chat Model
真实 Embedding
真实 Milvus Hybrid Search
真实 Rerank
```

否则所有问题混在一起，很难定位。

推荐按接口逐级验证：

```text
/plan       先看分类是否正确
/candidates 再看 SQL 候选是否正确
/talent-search 最后看材料证据是否正确
```

---

# 12. 当前实现中容易忽略的问题

## 12.1 `execute_composite_search()` 没被完整接口直接调用

`backend/app/query_plan.py` 中有：

```python
def execute_composite_search(...):
```

测试验证了它的空集短路和多要求证据分组，但 `/api/talent-search` 在 `api.py` 中重新写了一套相似编排逻辑，并没有调用它。

风险：

- 单元测试通过的是辅助函数；
- 生产接口可能逐渐与辅助函数行为分叉；
- 修复一处忘记修另一处。

改进方向：

- 把 API 中的循环提取为统一 service；
- API 只做 HTTP 解析和错误码映射；
- Query Plan 编译、候选筛选、语义检索、合并分别由服务函数负责。

理想分层：

```text
api.py
  └─ 参数解析、依赖注入、HTTP 错误映射

talent_search_service.py
  └─ 业务编排

query_plan.py
  └─ Schema、DSL、编译与校验

candidate_query.py
  └─ SQL Builder

hybrid_search_service.py
  └─ 单次混合检索
```

课程项目规模小，放在一个文件便于教学；真实项目应逐步拆分。

## 12.2 `job_level` 只支持 `eq/in`

用户说：

```text
L4 以上
```

当前注册表：

```python
FilterField.JOB_LEVEL: _column_spec(..., {"eq", "in"})
```

不能直接生成 `gte L4`。原因是 `L10` 与 `L2` 按字符串比较会出错：

```text
"L10" < "L2"  # 字典序可能成立，但业务等级显然更高
```

正确方案之一：

- 数据库新增数值 `job_level_rank`；
- 或用受控映射 `L1 -> 1`；
- 或定义专用 `_job_level_spec()`；
- 在未实现前把“L4 以上”放入 clarifications。

不能为了让查询跑起来而错误地把“以上”改成 `eq L4`。

## 12.3 NULL 字段语义

如果员工没有 `birth_date`：

```sql
birth_date > :boundary
```

结果是 SQL UNKNOWN，不会进入候选集。

这通常符合硬条件语义：无法证明年龄满足，就不应通过。但产品要明确：

- 未知值是排除；还是
- 需要提示数据不完整；还是
- 返回“无法判断”的候选人列表。

类似问题也适用于部门、地区、政治面貌。

## 12.4 `employment_status` 既是系统强制条件，又在 DSL 中开放

SQL Builder 始终加入：

```python
EmployeeProfile.employment_status == "active"
```

但注册表又允许用户生成：

```python
employment_status eq inactive
```

两者组合会变成：

```text
active AND inactive
```

结果必然为空。

需要明确设计：

- 如果系统只允许查在职人员，应从 DSL 移除 `employment_status`；
- 如果允许查询离职人员，应由权限或接口参数控制，不应无条件强制 active；
- 至少应检测冲突并返回明确 422，而不是静默空集。

这是与年龄冲突同类的“系统约束与用户约束可满足性”问题。

## 12.5 `employee_no` 的全局唯一与租户唯一

数据库当前：

```sql
employee_no VARCHAR(64) NOT NULL UNIQUE
```

这意味着不同租户不能有相同员工工号。若业务预期工号只在租户内唯一，更合理的是：

```sql
UNIQUE (tenant_id, employee_no)
```

同时 Milvus 的 `candidate_id` 是否只存工号也需要重新考虑。跨租户过滤已经存在，但全局唯一键设计仍影响数据模型。

## 12.6 检索结果是 Chunk，不是最终人才排名

当前 `/api/talent-search` 返回的是合并后的 Chunk：

```json
{
  "chunks": [...]
}
```

它还不是最终的“候选人排行榜”。要形成候选人结果，还需要：

1. 按 `candidate_id` 聚合；
2. 计算 required semantic coverage；
3. 汇总每项最佳证据；
4. 应用偏好加分；
5. 处理证据冲突或不足；
6. 生成可解释的候选人级评分。

因此第 8 课准确定位是：

```text
复合检索与证据准备
```

不是完整人才决策。

---

# 13. 常见问题、原因与解决思路

## 13.1 查询返回 503，但其实是条件冲突

### 原因

业务验证异常被通用 `except Exception` 映射为 503。

### 解决

先捕获 `ValueError` / `ValidationError`，返回 422；只有外部服务或基础设施错误返回 503。

---

## 13.2 SQL 候选集为空，Milvus 却返回了全库结果

### 原因

空列表在某层被转换成 `None` 或忽略了 candidate filter。

### 解决

在业务编排最前面短路：

```python
if not candidate_ids:
    return empty_result
```

并增加测试证明 Store 没有被调用。

---

## 13.3 SQL 文本里看到 `__[POSTCOMPILE_xxx]`，以为 SQL 不完整

### 原因

不了解 SQLAlchemy expanding parameter。

### 解决

检查：

```python
compiled.params
```

或用：

```python
compile_kwargs={"render_postcompile": True}
```

观察最终占位符。不要因此改成字符串拼接。

---

## 13.4 两项经历只有一个 `semantic_requirement`

### 原因

提示词只说“放入 semantic requirements”，没有明确要求原子化拆分；模型把并列条件当成一个长查询。

### 解决

- 提示词增加“独立要求必须拆分”；
- 增加 few-shot 示例；
- 建立多要求评测集；
- 必要时增加计划审查步骤。

---

## 13.5 增加政治面貌后查询报列不存在

### 原因

只改了 `database/init.sql`，已有 PostgreSQL 数据卷没有执行新初始化脚本。

### 解决

对已有数据库执行增量迁移，再用 `\d employee_profiles` 验证。

---

## 13.6 模型已经知道新字段，但 SQL Builder 报未知字段

### 原因

提示词、枚举和注册表不是同一个来源，或只改了提示词。

### 解决

以 `FILTER_FIELD_REGISTRY` 为执行事实源，并让 `filter_dsl_catalog()` 自动生成提示词目录；同步更新 `FilterField`。

---

## 13.7 查询优化从不触发 rewrite/decompose

### 原因

API 没有传 `requirement_count` 和 `expression_is_vague`，默认总是 1/False。

### 解决

重新定义优化上下文并从编译阶段显式传递；同时不要把必须澄清的表达错误地交给 rewrite。

---

## 13.8 有任意一条弱相关结果就不再优化

### 原因

触发条件只检查 `hit_count > 0`。

### 解决

使用 top Rerank score、候选覆盖、关键实体覆盖等质量门槛，并由离线评测标定阈值。

---

## 13.9 两个 required 条件分别有人命中，却没有一个人同时满足

### 原因

系统在 Chunk 层做并集，没有在候选人层做 requirement coverage。

### 解决

按 `candidate_id` 建立证据覆盖集合，只保留覆盖所有 required IDs 的候选人；optional requirement 用于加分而非淘汰。

---

## 13.10 Windows PowerShell 中文请求异常

建议始终：

```powershell
-ContentType "application/json; charset=utf-8"
```

复杂中文请求体优先使用 `ConvertTo-Json`，不要手拼未转义 JSON 字符串。

如果终端仍出现乱码，要区分：

- 请求发送前编码损坏；
- 响应显示编码错误；
- 数据库存储本身错误。

可以先把服务端收到的 query 记录到日志判断是哪一层损坏。

---

# 14. 关键设计原则总结

## 14.1 LLM 负责理解，不负责执行

```text
LLM：把自然语言编译成受约束计划
程序：校验、授权、构造 SQL、执行检索
```

这是把概率系统与确定性系统分离。

## 14.2 Query Plan 是安全契约，不只是 JSON 格式

一个合格 Query Plan 要同时满足：

1. Schema 合法；
2. 字段和操作符在白名单；
3. 多个条件组合后可满足；
4. 不包含租户和权限越权信息；
5. 语义要求被原子化；
6. 模糊条件没有被擅自补全；
7. 执行成本在预算内。

## 14.3 硬条件、证据条件、偏好必须分层

错误做法：

```text
所有条件都拼进一个向量查询
```

正确做法：

```text
结构化硬条件 → SQL
材料证据条件 → Hybrid Search
偏好 → 排序/评估
不清楚 → Clarification
```

## 14.4 安全条件必须来自可信上下文

```text
租户、权限、密级
```

不能由用户文本或 LLM 决定，必须由认证/授权上下文注入，并下推到真实数据访问层。

## 14.5 空集是有业务含义的结果

```text
candidate_ids=[]
```

表示 SQL 已经证明无人满足，必须结束。它不是“没有设置过滤”。

## 14.6 参数化不仅防注入，也定义了正确的执行边界

SQLAlchemy 表达式提供：

- 方言适配；
- 参数绑定；
- 列表展开；
- NULL 和类型处理；
- 可测试的 SQL AST。

因此不要让模型或字符串模板生成原始 SQL。

## 14.7 多项语义要求要保留独立证据

`requirement_id` 的价值是：

- 分开检索；
- 分开解释；
- 计算覆盖率；
- 支持 required/optional；
- 后续形成候选人级评分。

## 14.8 查询优化需要闭环，而不只是“再问模型一次”

完整闭环应包含：

```text
失败识别
  → 原因分类
  → 选择策略
  → 受约束地产生查询
  → 执行与融合
  → 统一质量评估
  → 成本和超时控制
  → 指标记录与离线评测
```

## 14.9 注册表减少规则漂移

字段说明、允许操作符、SQL 构造函数放在同一注册表，使：

```text
提示词看到的 DSL == 执行器真正支持的 DSL
```

新增政治面貌就是对这一设计的综合验证。

---

# 15. 作业提交时可以使用的答案框架

## 作业一结论

> 我为 `department in [...]` 增加了 SQL 编译测试。SQLAlchemy 将部门列表保存为 expanding/post-compile 绑定参数，普通编译结果中出现 `__[POSTCOMPILE_department_x]`，参数字典保存原始列表；使用 `render_postcompile=True` 后会展开为多个独立占位符。部门名称不直接出现在 SQL 文本中，证明实现没有通过字符串拼接生成 `IN` 条件，同时租户与在职状态仍由系统强制绑定。

## 作业二结论

> 原有校验只能验证单个 FilterCondition 的字段、操作符和值形状，不能发现多个条件组合后的不可满足范围。我增加了年龄上下界归并与开闭区间判断：最强下界大于最强上界，或边界相等但至少一侧为开区间时拒绝计划。`age >= 35 AND age <= 29` 因交集为空返回 422；`age >= 35 AND age <= 35` 仍合法。

## 作业三结论

> 对“有 Flink 实时计算项目经历，并有团队管理经历”的查询，Query Plan 生成 S1、S2 两个独立 `semantic_requirements`。执行阶段分别检索两次，并保留每个 Chunk 对应的 requirement ID。这样后续可以计算每个候选人是否覆盖所有必需条件，而不是依赖一个复合长查询的模糊相关性。

## 作业四结论

> 当前优化实现以零命中为唯一触发条件，API 又没有传入 `requirement_count` 和 `expression_is_vague`，所以端到端主要只能触发 Multi Query。改写查询只受提示词约束，多查询结果按首次出现去重，没有跨查询融合、统一精排、候选人级 required coverage、并发执行、预算控制和离线评测。改进应从质量门槛、约束守卫、RRF 融合、原查询统一 Rerank、候选人覆盖矩阵以及成本可观测性几个方面推进。

## 作业五结论

> 新增政治面貌需要同时改数据库初始化与增量迁移、SQLAlchemy ORM、员工写入和响应 Schema、前端类型/表单/展示、FilterField 枚举、FILTER_FIELD_REGISTRY 以及测试。注册表更新后提示词中的 Filter DSL 自动生成，无需重复维护字段清单。政治面貌属于 PostgreSQL 结构化硬条件，通过 SQL 生成 candidate IDs 后限制 Milvus 检索，因此无需修改 Milvus Schema。

---

# 16. 最终验收清单

## 16.1 作业一

- [ ] `department in` 有独立测试；
- [ ] 检查 `POSTCOMPILE` 或展开后的绑定占位符；
- [ ] 检查部门列表绑定参数；
- [ ] SQL 文本中没有直接出现部门名称；
- [ ] tenant 与 active 条件仍存在。

## 16.2 作业二

- [ ] 35 岁以上且 29 岁以下被拒绝；
- [ ] 相等边界的开闭区间正确；
- [ ] 正常年龄范围不受影响；
- [ ] API 返回 422；
- [ ] 错误信息明确。

## 16.3 作业三

- [ ] 查询生成 S1、S2；
- [ ] 两项都标记 required；
- [ ] 两项分别检索；
- [ ] 每次检索都限制在 SQL candidate IDs；
- [ ] Chunk 能追溯到 requirement ID。

## 16.4 作业四

- [ ] 指出策略上下文没有接入 API；
- [ ] 指出零命中触发条件过于粗糙；
- [ ] 指出查询约束漂移风险；
- [ ] 指出简单去重而非融合与全局精排；
- [ ] 指出没有候选人级 AND；
- [ ] 指出串行调用、费用和可观测性问题；
- [ ] 给出分阶段改进方案。

## 16.5 作业五

- [ ] `init.sql` 更新；
- [ ] 已有数据库迁移更新；
- [ ] ORM 更新；
- [ ] EmployeeInput 更新；
- [ ] employee_json 更新；
- [ ] FilterField 更新；
- [ ] 注册表更新；
- [ ] DSL 测试更新；
- [ ] SQL 参数测试更新；
- [ ] 前端类型/表单/展示按范围更新；
- [ ] 明确无需修改 Milvus Schema。

---

# 17. 自我检查题

如果下面问题都能不看代码回答，说明你真正掌握了本课。

1. 为什么 `FilterCondition` 合法仍可能导致整个 Query Plan 不可执行？
2. `age < 35` 为什么会变成 `birth_date > boundary`？
3. `age >= 35 AND age <= 35` 与 `age > 35 AND age <= 35` 有何区别？
4. 为什么 SQLAlchemy 的 `IN` 使用 post-compile 参数？
5. `candidate_ids=[]` 与 `candidate_ids=None` 为什么必须区分？
6. 为什么租户条件不能出现在 LLM 可生成的 FilterField 中？
7. `/talent-search/candidates` 为什么不需要材料权限头？
8. `/talent-search` 为什么必须同时有租户头和权限头？
9. 两个 semantic requirements 为什么不能简单拼成一个长查询？
10. 当前 `required` 字段为什么还没有真正实现“必须满足”？
11. 为什么查询优化只看是否有结果不够？
12. 为什么不同改写查询各自的 Rerank 分数不宜直接比较？
13. 为什么最终统一 Rerank 应使用原始 query？
14. 政治面貌为什么属于 PostgreSQL 条件，而不是 Milvus Chunk 字段？
15. 修改 `database/init.sql` 为什么不会自动更新已有数据库？
16. 为什么 `FILTER_FIELD_REGISTRY` 是这节课最重要的扩展点之一？
17. 如果用户说“L4 以上”，当前系统为什么应澄清或扩展 DSL，而不是改成 `eq L4`？
18. `employment_status=inactive` 与系统强制 active 会发生什么？
19. 怎样判断一个候选人同时满足 S1、S2？
20. 查询优化应如何限制额外延迟与费用？

---

# 18. 本课最终知识图

```text
自然语言人才查询
│
├─ 编译层：LLM + structured output
│   ├─ filters
│   ├─ semantic_requirements
│   ├─ preferences
│   └─ clarifications
│
├─ 计划校验层
│   ├─ Pydantic 类型与枚举
│   ├─ 注册表字段/操作符白名单
│   ├─ 跨条件可满足性
│   └─ 禁止租户与权限由模型生成
│
├─ 候选人层：PostgreSQL + SQLAlchemy
│   ├─ tenant_id 强制注入
│   ├─ active 状态强制注入
│   ├─ 参数化 SQL
│   ├─ age → birth_date
│   └─ 输出 employee_no candidate_ids
│
├─ 安全短路
│   └─ candidate_ids 为空 → 立即结束
│
├─ 证据检索层：Milvus Hybrid Search
│   ├─ tenant filter
│   ├─ permission filter
│   ├─ candidate_ids filter
│   ├─ Dense + BM25
│   ├─ RRF
│   └─ Rerank
│
├─ 多要求证据层
│   ├─ S1 独立检索
│   ├─ S2 独立检索
│   ├─ Chunk → requirement IDs
│   └─ 候选人 required coverage（当前待完善）
│
└─ 优化与工程化
    ├─ 失败原因分类
    ├─ rewrite / multi-query / decompose
    ├─ 约束漂移守卫
    ├─ 多查询融合 + 全局精排
    ├─ 并发、缓存、预算、超时
    └─ 指标、日志、离线评测
```

---

# 19. 最后的核心结论

第 8 课最重要的不是记住某个 Pydantic 类或 SQLAlchemy API，而是形成下面的系统设计思维：

> **模型只能在受约束的计划空间里表达用户意图；确定性程序负责校验、授权和执行。先用结构化数据库确定“谁有资格进入候选集”，再在这个集合中检索非结构化材料证据。任何模糊、冲突或越权条件都不能靠模型自行猜测。**

从工程角度，这节课建立了四道防线：

1. **Schema 防线**：限制输出结构；
2. **DSL 防线**：限制字段和操作符；
3. **业务一致性防线**：拒绝无解或冲突的组合条件；
4. **授权与数据访问防线**：租户、权限和候选集由后端强制下推。

五项作业分别让你亲手验证这四道防线，以及系统如何安全扩展新字段。真正掌握后，你面对的就不再只是“人才查询”场景，而是任何“自然语言 + 结构化过滤 + 非结构化检索”的企业搜索问题。


