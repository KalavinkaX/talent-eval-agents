# 第14课：人才任务理解、条件澄清与执行计划——LangGraph 图与数据流完整学习指南

> 读者：刚毕业、准备从事 Agent 开发的同学。本文依据 `D:\Code\K_Course\talent-eval-agents_learning` 当前工作树，整理于 2026-09-27。阅读路线是**先看整张图和 State，再按节点追踪三条输入路径，最后由图结构推导三项作业**。文中明确区分「仓库现有实现」与「作业待实现方案」。本次只改学习文档，不改本课业务代码。

## 一、老师要你学到什么：先建立整节课的心智模型

第11课有“人才决策主图”，第12、13课提供了岗位 JD 和人才数据工具，但它们并不能保证用户的第一句话已足够明确。例如：“高级 AI 应用工程师”是岗位名，“筛选上海且有企业知识库经验的人才”是具体条件，“AI 应用工程师”可能对应多个 JD，“经验丰富”缺少年限阈值。若未经澄清就检索，后续评估看起来完整，实则从一开始就选错了目标。

**本课的核心问题：怎样把不确定的自然语言，逐步编译为有来源、有边界、可暂停、可恢复的执行计划？**

```text
用户 request_text
  → TalentRequestDraft：模型理解出的中间表示（输入类型、硬条件、语义条件、偏好、待澄清）
  → [若是岗位名] JobMatch：工具查出的真实岗位候选，而非模型编造的岗位
  → [若有歧义] 用户选择：interrupt 暂停，同一 Thread resume
  → TalentRequest：保留原文、来源、确认的目标岗位和业务要求
  → QueryPlan：限定字段与运算符、标记可执行性的受控检索协议
```

**四个应学会的能力**：①先分类再调用工具；②用状态与条件边控制模型，而不是把控制权交给模型；③把业务歧义变成人工确认节点；④用 Checkpoint 和测试证明暂停与恢复是同一任务，已完成节点不重复运行。

面试时可以这样概括：“我把自然语言理解看作编译前端，Draft 是中间表示，QueryPlan 是受控执行协议。图负责显式分支，工具提供岗位事实，HITL 中断解决模型不该自行裁决的歧义。”

## 二、总体设计框架：以 LangGraph 图为主线

### 2.1 先画图，再看文件

```text
START
  │
  ▼
receive_request   校验 Context 和输入，清空本轮工作字段
  │
  ▼
interpret_input   request_text → TalentRequestDraft → input_mode
  │
  ├── detailed_requirement ────────────────────────────────┐
  │                                                        ▼
  │                                                     build_plan → END
  │
  └── job_name → lookup_jobs → _route_job_matches
                             ├── 唯一 exact → select_exact_job ─────────┐
                             ├── 非唯一/包含/模糊 → confirm_job          │
                             │                     │ interrupt(payload)  │
                             │                     │ 等同一 Thread resume│
                             │                     └─────────────────────┤
                             └── 空候选 → no_job_match → END             │
                                                                         ▼
                                                          compile_selected_job
                                                                         │
                                                                         ▼
                                                                  build_plan → END
```

注意 `_route_input` 与 `_route_job_matches` **是条件边的路由函数，不是图节点**；Graph mode 看到的节点是 `interpret_input`、`lookup_jobs` 等。`no_job_match` 和 `build_plan` 都有通往 END 的边，当前 `confirm_job` 则无条件流入 `compile_selected_job`。作业一、二正是要改变这条边的语义。

### 2.2 一张模块表把职责分开

| 模块 | 节点/文件 | 输入 → 输出 | 为什么要单独成模块 |
|---|---|---|---|
| 注册与调度 | `backend/langgraph.json`、`build_talent_request_graph` | 图名 → 编译后的图 | Agent Server 加载图，不是 FastAPI 手写接口 |
| 接收与身份 | `receive_request`、`DecisionContext` | 文本+可信租户/权限 → 干净 State | 安全边界在模型之外 |
| 自然语言理解 | `interpret_input`、`TalentRequestDraft` | 文本 → 分类+初步条件 | 模型只负责解释，不决定岗位和 SQL |
| 岗位事实与路由 | `lookup_jobs`、`_route_job_matches` | 岗位名 → 候选+匹配类型 → 下一节点 | 字符串相似度不是业务确认 |
| 人工确认与恢复 | `confirm_job`、Checkpointer、`thread_id` | 候选 → 暂停 → 用户选择 | 歧义必须有人做明确决定 |
| 条件编译 | `compile_selected_job`、`build_plan` | 已确认岗位/详细要求 → TalentRequest+QueryPlan | 业务意图与执行协议分开 |
| 结果与验证 | `no_job_match`、tests、Studio | 澄清结果/路径/输出 | 证明不能执行时不会悄悄继续 |

### 2.3 入口是 Agent Server 图，不是 FastAPI 的第8课接口

`backend/langgraph.json` 注册：

```json
{"graphs": {"talent_request": "./app/talent_request_graph.py:graph"}}
```

模块末尾的全局 `graph` 由 `build_talent_request_graph(request_interpreter=model_request_interpreter, job_lookup=database_job_lookup)` 构造。开发态 Agent Server 加载该图，并管理 checkpoint；本地脚本/测试则显式注入固定解释器、固定查询器和 `InMemorySaver()`。`backend/app/api.py` 中已有的 `/...` QueryPlan HTTP 入口是另一条路径，**本课图既没有调用这个 FastAPI 路由，也没有自动接到第11课决策主图的后续检索与评估**。

### 2.4 四种数据不要混淆：Input、State、Context、Config

**Input（用户输入）**：`TalentRequestInput` 只有 `request_text: str`，如 `{"request_text":"AI 应用工程师"}`。这是不可信的自然语言。

**State（沿图传递的工作记忆）**：`TalentRequestState(TypedDict, total=False)` 包括 `request_text`、`input_mode`、`draft`、`job_matches`、`selected_job`、`talent_request`、`query_plan`、`clarifications`、`status`、`errors`。节点只返回自己要更新的字段，不必每次复制完整 State；`total=False` 也意味着中间节点不要假设所有字段已存在。

**Runtime Context（可信运行身份）**：`DecisionContext(tenant_id: str, permission_scopes: tuple[str,...])`。调用方注入，岗位查询工具使用它过滤租户和权限；它不由用户输入/模型生成。

**Config（Checkpoint 定位）**：`{"configurable":{"thread_id":"lesson14-ambiguous"}}`。它定位同一执行线程，既不等于 tenant_id，也不能取代用户身份鉴权。生产服务还应把 Thread 的访问权和实际认证主体绑定；课程演示不等于生产鉴权完整实现。

### 2.5 真正理解 State：每个字段由谁生产，谁消费

| 字段 | 生产者 | 消费者 | 此字段解决的疑问 |
|---|---|---|---|
| `request_text` | `receive_request` | 解释/中断/计划节点 | 用户原本说了什么？ |
| `draft` | `interpret_input`；岗位确认后由 `compile_selected_job` 覆盖 | `_lookup_jobs`、`_build_plan` | 模型提取出什么条件？ |
| `input_mode` | `interpret_input`；确认后被固定为 `job_name` | `_route_input`、`_build_plan` | 走哪条分支、计划来源是什么？ |
| `job_matches` | `lookup_jobs` | `_route_job_matches`、`confirm_job` | 真实系统里有哪些候选 JD？ |
| `selected_job` | `select_exact_job` 或 `confirm_job` | `compile_selected_job`、`build_plan` | 已确定哪个 JD？ |
| `talent_request` | `build_plan` | 图输出/未来主图 | 用户目标的业务语义是什么？ |
| `query_plan` | `build_plan` | 图输出/未来检索器 | 能按什么受控协议检索？ |
| `clarifications` | `build_plan` 或 `no_job_match` | 调用方 | 什么还不能安全执行？ |
| `status` | 每个关键节点 | 调用方/作业新条件边 | 任务当前处于什么生命周期？ |

下文每个节点都按固定顺序讲：**读 State → 实际代码 → 写 State → 下一跳 → 设计理由**。

## 三、逐节点拆解：把整张图的代码读通

### 3.1 `START → receive_request`：先做运行边界，不做语义推断

`_receive_request(state, runtime)` 从 `runtime.context` 检查租户与权限，从 `state["request_text"]` 取文本并 `.strip()`；任一为空抛 `ValueError`。返回值是：

```python
return {
    "request_text": request_text,
    "job_matches": [],
    "selected_job": None,
    "talent_request": {},
    "query_plan": {},
    "clarifications": [],
    "status": "received",
    "errors": [],
}
```

它**读**用户文本与可信 Context；**写**清洗后文本和初始状态；**下一跳**固定是 `interpret_input`。为什么拆节点？身份校验、数据初始化与模型理解是不同责任。作业二的“非法选择 job_code”发生在另一节点，不应和这里的输入缺失错误混淆。

### 3.2 `interpret_input`：第一次模型调用，产 Draft 而非最终计划

现有节点工厂核心：

```python
def _interpret_input(request_interpreter):
    def interpret_input(state):
        draft = request_interpreter(state["request_text"], None)
        return {
            "draft": draft.model_dump(mode="json"),
            "input_mode": draft.input_mode,
            "status": "request_understood",
        }
    return interpret_input
```

正式解释器 `model_request_interpreter` 用 `get_chat_model(temperature=0)` 和 `model.with_structured_output(TalentRequestDraft).invoke([("system", MODEL_SYSTEM_PROMPT), ("user", user_content)])`；未配置模型密钥时报 `RuntimeError`。这个节点**读** `request_text`，**写** `draft/input_mode/status`，**下一跳**由 `_route_input` 决定。Draft 定义：

```python
class TalentRequestDraft(BaseModel):
    input_mode: RequestInputMode  # detailed_requirement | job_name
    job_query: str | None = None
    filters: list[FilterCondition] = Field(default_factory=list)
    semantic_requirements: list[SemanticRequirement] = Field(default_factory=list)
    evaluation_preferences: list[str] = Field(default_factory=list)
    clarifications: list[Clarification] = Field(default_factory=list)
```

映射思路：`上海` → `FilterCondition(field="region", operator="eq", value="上海")`；`企业知识库经验` → `SemanticRequirement(query="企业知识库经验")`；`有 Agent 评测经验优先` → `evaluation_preferences`；`经验丰富` → `Clarification(expression=..., reason=...)`，不得编造阈值。`MODEL_SYSTEM_PROMPT` 还禁止从用户原话提取租户/权限。模型只输出受控草稿，**是否查 JD 仍由后面的条件边决定**。

### 3.3 `_route_input`：第一个“编译分叉”

```python
def _route_input(state) -> Literal["lookup_jobs", "build_plan"]:
    return "lookup_jobs" if state["input_mode"] == "job_name" else "build_plan"
```

它**读** `input_mode`，**不写** State。岗位名必须先查事实；具体人才要求可以直达 `build_plan`。不能用“输入字数少”或“包含 AI”这样的关键词规则替代这里的明确分类。

### 3.4 `lookup_jobs`：岗位查询要用可信 Context

```python
def _lookup_jobs(job_lookup):
    def lookup_jobs(state, runtime):
        job_query = str(state["draft"].get("job_query") or state["request_text"])
        matches = job_lookup(job_query, runtime.context)
        return {"job_matches": matches, "status": "jobs_found"}
    return lookup_jobs
```

它**读** `draft.job_query` 或原文、`runtime.context`；**写** `job_matches/status`；**下一跳**由 `_route_job_matches` 决定。正式 `database_job_lookup` 把 `DecisionContext` 变成 `TalentToolContext`，再调用 `TalentToolService.lookup_job_descriptions`。工具层从 `JobDescription` 只取同租户且 `active` 的 JD，按归一化岗位名计算：

```python
if normalized_query == normalized_name:
    match_type, score = "exact", 1.0
elif normalized_query in normalized_name or normalized_name in normalized_query:
    match_type, score = "contains", 1.0
else:
    match_type = "fuzzy"
    score = round(SequenceMatcher(None, normalized_query, normalized_name).ratio(), 4)
# 当前服务只保留 score >= 0.3 的结果，再排序并截取 limit（默认 5）
```

**关键分辨**：`contains` 的分数同样可能是 `1.0`，这不等于“精确匹配”。`match_score` 表示相似度，`match_type` 表示确认力度。`JobMatch` 还带 `job_code/name/version/content`；中断选项展示前五个字段，完整 `content` 仍留在 State 供确认后解释。

### 3.5 `_route_job_matches`：第二个分叉，不能拿最高分冒充用户决策

```python
def _route_job_matches(state):
    exact_matches = [item for item in state["job_matches"]
                     if item["match_type"] == "exact"]
    if len(exact_matches) == 1:
        return "select_exact_job"
    if state["job_matches"]:
        return "confirm_job"
    return "no_job_match"
```

它**读** `job_matches`，不更新 State。决策表：唯一 exact → 自动选；非唯一的 exact 或只有 contains/fuzzy → 暂停确认；空列表 → 澄清终态。即使匹配分数为 1.0，也只有 `match_type="exact"` 才满足自动确认的业务规则。多个 exact 时仍需要用户确认。

### 3.6 `select_exact_job`：只确定目标，不重复编译

```python
def _select_exact_job(state):
    selected = next(item for item in state["job_matches"]
                    if item["match_type"] == "exact")
    return {"selected_job": selected, "status": "job_confirmed"}
```

它**读**候选列表，**写** `selected_job/status`，**下一跳** `compile_selected_job`。它不单独生成计划，目的在于自动确认与人工确认共用同一个编译出口，减少两套逻辑分叉。

### 3.7 `confirm_job`：一个真实的图暂停点

现有逻辑摘要（与源码字段一致，省略 options 中的重复键）：

```python
def _confirm_job(state):
    selection = interrupt({
        "type": "job_selection",
        "question": "请选择本次人才评估使用的岗位 JD",
        "request_text": state["request_text"],
        "options": [{"job_code": item["job_code"], "name": item["name"],
                     "match_score": item["match_score"],
                     "match_type": item["match_type"],
                     "version": item["version"]}
                    for item in state["job_matches"]],
    })
    if not isinstance(selection, dict) or selection.get("action") != "select":
        raise ValueError("恢复数据必须包含 action=select")
    selected_code = selection.get("job_code")
    selected = next((item for item in state["job_matches"]
                     if item["job_code"] == selected_code), None)
    if selected is None:
        raise ValueError("恢复数据中的 job_code 不在待确认岗位列表中")
    return {"selected_job": selected, "status": "job_confirmed"}
```

**首次到达**：读取 `request_text/job_matches`，构造给人的候选 payload；`interrupt()` 暂停节点并把 payload 传回调用方。Checkpoint 保存 State 与等待继续的位置。此时不应该有 `selected_job`，更不应该有 QueryPlan。你可用 `graph.get_state(config).next == ("confirm_job",)` 观察等待节点。

**同 Thread 恢复**：

```python
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

config = {"configurable": {"thread_id": "lesson14-ambiguous"}}
context = DecisionContext(tenant_id="course-demo", permission_scopes=("hr_private",))
graph = build_talent_request_graph(
    request_interpreter=FixtureInterpreter(),
    job_lookup=fixed_lookup,
    checkpointer=InMemorySaver(),
)
first = graph.invoke({"request_text": "AI 应用工程师"}, config, context=context)
assert first["__interrupt__"][0].value["type"] == "job_selection"
resumed = graph.invoke(Command(resume={"action": "select", "job_code": "JD-AI-002"}),
                       config, context=context)
```

示例中的 `FixtureInterpreter` 和 `fixed_lookup` 是单测中构造的测试替身，不是生产图的全局变量。恢复时 `interrupt(...)` 的返回值就是 `Command(resume=...)` 传来的字典；节点**从开头重入**，之前已经完成的 `lookup_jobs` 不重跑。这解释了为什么仓库将查询和中断放在两个节点，也解释了为什么不该在 `interrupt()` 前发邮件、写数据库等未做幂等的副作用。校验 `job_code` 时只看本次 `state["job_matches"]` 候选，不信任用户传来的 JD 内容。

目前只有 `action=select` 合法；`cancel` 与非法 code 分别会触发通用异常，这就是作业一和二真正要改的行为。当前构图是 `builder.add_edge("confirm_job", "compile_selected_job")`，无条件出边只适合“成功选择”这一种结局。

### 3.8 `compile_selected_job`：为什么岗位路线要解释两次

```python
def _compile_selected_job(request_interpreter):
    def compile_selected_job(state):
        draft = request_interpreter(state["request_text"], state["selected_job"])
        return {"draft": draft.model_dump(mode="json"), "input_mode": "job_name"}
    return compile_selected_job
```

它**读**原文与已确认 JD，**写**覆盖后的 Draft，**下一跳** `build_plan`。第一次模型调用仅负责识别“这是岗位名”；在用户确认前，模型不该拿一个不确定的候选 JD 编造条件。第二次调用把 `original_request`、`confirmed_job` 和 `job_description` 组合给模型，再从**已确认 JD** 提取具体要求。注意输出 `input_mode` 被强制保留为 `job_name`：它标明最初的输入来源，虽然第二次 Draft 可以包含 JD 解析出的详细条件。

### 3.9 `build_plan`：同一 Draft 编译出两种不同对象

`_build_plan` 先 `TalentRequestDraft.model_validate(state["draft"])`；若有 `selected_job`，把其 `job_code/name/version` 写为 `target_job`。业务语义对象：

```python
talent_request = {
    "original_text": state["request_text"],
    "task_type": "evaluate_and_recommend",
    "source": state["input_mode"],
    "target_job": target_job,   # 详细要求路线为 None
    "hard_conditions": [item.model_dump(mode="json") for item in draft.filters],
    "semantic_conditions": [item.model_dump(mode="json")
                            for item in draft.semantic_requirements],
    "evaluation_preferences": draft.evaluation_preferences,
}
```

执行协议对象：

```python
query_plan = QueryPlan(
    task_type=TaskType.FIND_TALENT,
    filters=draft.filters,
    semantic_requirements=draft.semantic_requirements,
    preferences=draft.evaluation_preferences,
    clarifications=draft.clarifications,
)
status = "plan_ready" if query_plan.executable else "clarification_required"
```

它**读** Draft、原文、可选的 selected_job；**写** `talent_request/query_plan/clarifications/status`；**下一跳** END。`TalentRequest` 记录“用户要做什么、从哪里来、目标岗位是什么”，`QueryPlan` 限定“下一步可以怎样检索”。例如 `backend/app/query_plan.py` 的 `FilterCondition` 使用预定义 `FilterField` 和 `eq/in/lt/lte/gt/gte`，范围运算符只允许数值字段；`SemanticRequirement` 约束 `requirement_id` 如 `S1`。`QueryPlan.executable` 目前只判断 `not self.clarifications`；`plan_ready` 仅表示计划可执行，**尚未真正运行候选检索或第11课的评估主图**。

### 3.10 `no_job_match`：没有岗位事实时，选择澄清而不是猜测

`_no_job_match` 返回空 `talent_request/query_plan`、`status="clarification_required"`，并追加 `{"expression": state["request_text"], "reason": "未找到可确认的岗位 JD"}`。这是业务上的“无法确定目标”，不是模型调用失败，也不应伪造默认岗位。

## 四、把上面节点连起来：三条逐步执行轨迹

> 下列具体输出依据 `backend/tests/test_talent_request_graph.py` 与固定解释器脚本 `backend/scripts/verify_talent_request_graph.py`；真实 Studio 模型可能产生不同 Draft，不能用测试替身的输出冒充 Studio 实测。

### 4.1 轨迹 A：详细要求，直接形成计划

输入：`筛选上海且有企业知识库经验的人才，有 Agent 评测经验优先`

```text
receive_request:
  request_text=原文, status=received, job_matches=[]
  ↓
interpret_input:
  input_mode=detailed_requirement
  draft.filters=[region eq 上海]
  draft.semantic_requirements=[S1: 企业知识库经验]
  draft.evaluation_preferences=[有 Agent 评测经验优先]
  ↓ _route_input == build_plan
build_plan:
  talent_request.source=detailed_requirement, target_job=None
  query_plan.task_type=find_talent
  query_plan.filters=[region eq 上海]
  query_plan.semantic_requirements=[S1: 企业知识库经验]
  query_plan.preferences=[有 Agent 评测经验优先]
  status=plan_ready → END
```

测试断言 `lookup_calls == []`：这证明分支跳过岗位查询。如果用户说“经验丰富”并被模型放入 `clarifications`，则构造出的 `QueryPlan.executable=False`，最终为 `clarification_required`，不应在没有阈值时自行设 5 年。

### 4.2 轨迹 B：唯一精确岗位，不中断

输入：`高级 AI 应用工程师`

```text
interpret_input: input_mode=job_name, job_query=原文
  ↓
lookup_jobs: 候选 JD-AI-001 为 exact（还可能有 fuzzy）
  ↓ _route_job_matches: exact 数量=1
select_exact_job: selected_job=JD-AI-001, status=job_confirmed
  ↓
compile_selected_job: 用该 JD + 原文重新产 Draft
  ↓
build_plan: target_job.job_code=JD-AI-001, status=plan_ready → END
```

`test_unique_exact_job_match_continues_without_interrupt` 还断言解释器调用两次：先传 `selected_job=None` 分类，再传 `JD-AI-001` 编译 JD；第二次不是重复查询岗位，而是不同阶段的语义理解。

### 4.3 轨迹 C：歧义岗位，暂停、同线程恢复

输入：`AI 应用工程师`

```text
receive_request → interpret_input(input_mode=job_name)
→ lookup_jobs(job_matches=[JD-AI-001 contains, JD-AI-002 contains])
→ confirm_job → interrupt(payload)；get_state(config).next=("confirm_job",)
                                   │
                                   ▼ 用户选择 JD-AI-002
                      Command(resume={"action":"select","job_code":"JD-AI-002"})
                                   │ 同一个 thread_id
                                   ▼
                    confirm_job 返回 selected_job=JD-AI-002
                    → compile_selected_job → build_plan → END
```

关键断言：`resumed["status"] == "plan_ready"`、`resumed["talent_request"]["target_job"]["job_code"] == "JD-AI-002"`、`graph.get_state(config).next == ()`、`lookup_calls == ["AI 应用工程师"]`。最后一项证明已经完成的岗位查询没有再次执行。

### 4.4 第四种终局：找不到岗位

`量子招聘架构师` 在固定查询返回空候选时走 `no_job_match → END`，状态 `clarification_required`，计划 `{}`。这一路径帮助你检验“不确定时停下来”，而不是让模型想象一个岗位。

## 五、作业一：为岗位确认增加 cancel 动作

> 三项作业都按同一顺序：**先写行为契约 → 写失败测试 → 改状态类型/节点 → 改条件边 → 跑回归**。节点的返回状态和下一跳的边必须一起考虑。

### 5.1 先定义取消后的图状态

用户在 `confirm_job` 看到候选后发送：

```json
{"action":"cancel"}
```

期望这是正常业务终态，而不是异常：

```json
{
  "status": "cancelled",
  "selected_job": null,
  "talent_request": {},
  "query_plan": {},
  "clarifications": [],
  "errors": []
}
```

取消路径不能进入 `compile_selected_job`，因此不能生成第二次模型调用，也不能出现 QueryPlan。

### 5.2 第一步：新增失败测试

文件：`backend/tests/test_talent_request_graph.py`。先复制歧义岗位测试的构造方式，使用两个 `contains` 候选、`InMemorySaver()`、同一 `config`。核心断言应包括：

```python
first = graph.invoke({"request_text":"AI 应用工程师"}, config, context=_context())
assert first["__interrupt__"][0].value["type"] == "job_selection"

result = graph.invoke(
    Command(resume={"action":"cancel"}), config, context=_context()
)

assert result["status"] == "cancelled"
assert result["selected_job"] is None
assert result["talent_request"] == {}
assert result["query_plan"] == {}
assert result["clarifications"] == []
assert graph.get_state(config).next == ()
assert lookup_calls == ["AI 应用工程师"]
assert interpreter.calls == [("AI 应用工程师", None)]
```

同时断言中断 payload 暴露可用动作：`payload["actions"] == ["select", "cancel"]`。在代码还没改时，这个测试应失败；这样才能证明测试确实覆盖了作业。

### 5.3 第二步：扩展状态契约和中断协议

```python
RequestStatus = Literal[
    "received", "request_understood", "jobs_found", "job_confirmed",
    "plan_ready", "clarification_required", "cancelled",
]
```

在 `interrupt` payload 加：

```python
"actions": ["select", "cancel"],
```

这是给 Studio/UI 的协议声明，让调用方知道中断不是只能提交 `select`。

### 5.4 第三步：在 `_confirm_job` 识别 cancel

```python
selection = interrupt({...})
if not isinstance(selection, dict):
    raise ValueError("恢复数据必须是对象")

if selection.get("action") == "cancel":
    return {
        "selected_job": None,
        "talent_request": {},
        "query_plan": {},
        "clarifications": [],
        "errors": [],
        "status": "cancelled",
    }

if selection.get("action") != "select":
    raise ValueError("恢复数据必须包含 action=select 或 action=cancel")
```

注意这里的语义：`interrupt` 只负责把动作交给节点，节点再验证动作并返回状态。不要把 `cancel` 当成 `job_code` 缺失，也不要给取消自动填默认岗位。

### 5.5 第四步：把无条件出边改成条件边

现有代码：

```python
builder.add_edge("confirm_job", "compile_selected_job")
```

作业实现建议：

```python
def _route_after_confirmation(state):
    return (
        "compile_selected_job"
        if state["status"] == "job_confirmed"
        else END
    )

builder.add_conditional_edges(
    "confirm_job",
    _route_after_confirmation,
    {
        "compile_selected_job": "compile_selected_job",
        END: END,
    },
)
```

删除原无条件边。设计逻辑只有一句：

```text
job_confirmed → 编译
cancelled    → END
```

### 5.6 第五步：回归验收

```powershell
cd D:\Code\K_Course\talent-eval-agents_learning\backend
uv run --no-sync pytest -q --basetemp .pytest-lesson14-cancel tests/test_talent_request_graph.py
```

必须同时证明：详细要求、唯一 exact、合法 select、无匹配路径没有被破坏；取消不产生计划；取消后下一节点为空；岗位查询只执行一次；解释器没有第二次调用。

## 六、作业二：非法 `job_code` 返回结构化错误

### 6.1 先分清三类错误

| 情况 | 业务含义 | 建议处理 |
|---|---|---|
| `tenant_id` 缺失 | 调用边界不合法 | 保持边界异常/由服务入口映射 4xx |
| resume 不是对象或 action 不支持 | 恢复协议错误 | 返回明确的协议错误或拒绝 |
| `action=select`，但 code 不在当前候选列表 | 用户选择了无效岗位 | 返回 `INVALID_JOB_CODE` 结构化结果 |

作业二重点是第三类。不能因为找不到 code 就选择第一个候选；也不要拿这个 code 去查询全库，避免绕过本轮候选白名单和租户边界。

### 6.2 先定义错误数据契约

建议新增：

```python
class SelectionError(TypedDict):
    code: str
    message: str
    field: str
    retryable: bool
```

把状态扩展为：

```python
RequestStatus = Literal[
    # 原有状态...
    "cancelled",
    "invalid_selection",
]
```

在 `TalentRequestState` 和 `TalentRequestOutput` 中增加：

```python
error_details: list[SelectionError]
```

`errors: list[str]` 可以保留，兼容原调用方；`error_details` 给前端和测试机器可读字段。

### 6.3 先写失败测试

```python
def test_invalid_job_code_returns_structured_result():
    # 两个 contains 候选，先运行到 interrupt
    graph.invoke({"request_text":"AI 应用工程师"}, config, context=_context())
    result = graph.invoke(
        Command(resume={"action":"select", "job_code":"JD-NOT-OFFERED"}),
        config,
        context=_context(),
    )

    assert result["status"] == "invalid_selection"
    assert result["selected_job"] is None
    assert result["talent_request"] == {}
    assert result["query_plan"] == {}
    assert result["error_details"] == [{
        "code": "INVALID_JOB_CODE",
        "message": "所选岗位不在本次待确认列表中",
        "field": "job_code",
        "retryable": True,
    }]
    assert graph.get_state(config).next == ()
```

测试在未改代码时应暴露当前 `ValueError`，改完后才转为正常结果。

### 6.4 修改 `_confirm_job`：用当前候选做白名单校验

```python
selected_code = selection.get("job_code")
selected = next(
    (item for item in state["job_matches"]
     if item["job_code"] == selected_code),
    None,
)
if selected is None:
    return {
        "selected_job": None,
        "talent_request": {},
        "query_plan": {},
        "clarifications": [],
        "errors": ["岗位选择无效"],
        "error_details": [{
            "code": "INVALID_JOB_CODE",
            "message": "所选岗位不在本次待确认列表中",
            "field": "job_code",
            "retryable": True,
        }],
        "status": "invalid_selection",
    }
```

再由作业一的 `_route_after_confirmation` 让所有非 `job_confirmed` 状态结束。不要用 `except Exception` 包住整段函数：数据库错误、模型错误和业务选择错误不是一回事。

### 6.5 “retryable=true”不等于可以盲目再次 resume

上面的最小实现把错误作为终态返回，`retryable` 表示调用方可以重新发起一次选择流程。若你想在**同一 Thread** 重新展示候选，必须额外设计：

```text
invalid_selection → retry_confirm_job → interrupt → 新 resume
```

要添加循环边、定义最大重试次数并测试 checkpoint；不能以为终态后再次传 `Command(resume=...)` 会自动回到中断点。本作业的最低验收只要求结构化错误结果。

## 七、作业三：Studio 运行三组输入，记录路径和 QueryPlan

### 7.1 准备与区分测试替身

运行固定图控制流验证：

```powershell
cd D:\Code\K_Course\talent-eval-agents_learning\backend
uv run --no-sync python -m scripts.verify_talent_request_graph
```

启动 Agent Server/Studio：

```powershell
uv run langgraph dev --no-browser --no-reload --port 2024
```

正式图来自 `backend/langgraph.json` 的全局 `graph`，使用真实 `model_request_interpreter` 和 `database_job_lookup`；固定脚本使用 `DemoInterpreter`、固定候选和 `InMemorySaver`。前者观察真实服务，后者证明控制流，两者不能互相冒充。

执行前准备：配置模型密钥、数据库连接，在确认使用的是本地课程数据库后再运行：

```powershell
uv run --no-sync python -m scripts.seed_lesson14_jobs
```

Assistant Context 使用可信配置：`tenant_id=course-demo`、`permission_scopes=["hr_private"]`。三组实验使用不同 Thread；歧义组的第一次运行和恢复必须使用同一个 Thread。

### 7.2 三组实验的“输入 → 节点 → 输出”记录表

| 实验 | 输入 | 预期路径 | 记录内容 |
|---|---|---|---|
| 详细要求 | `筛选上海且有企业知识库经验的人才，有 Agent 评测经验优先` | `receive_request → interpret_input → build_plan → END` | input_mode、filters、semantic_requirements、preferences、clarifications、是否跳过 lookup |
| 精确岗位 | `高级 AI 应用工程师` | `receive_request → interpret_input → lookup_jobs → select_exact_job → compile_selected_job → build_plan → END` | exact 候选、selected_job、最终 QueryPlan |
| 歧义岗位 | `AI 应用工程师` | 第一次到 `confirm_job → interrupt`；恢复后 `compile_selected_job → build_plan → END` | payload、Thread ID、resume 值、target_job、lookup 次数 |

建议每次复制下面模板：

```text
Thread ID：
request_text：
Context：
第一次 next：
interrupt payload：
resume 值：
最终 status：
TalentRequest：
QueryPlan：
节点路径：
本次观察到的设计原因：
```

不要直接把固定脚本的输出填成 Studio 实测；真实模型可能把某些语义表达放入 clarification，应该记录并分析，而不是为符合预期手工改结果。

## 八、常见问题：按图定位故障

| 症状 | 图上的定位 | 原因与处理 |
|---|---|---|
| 详细要求也去查岗位 | `interpret_input → _route_input` | Draft 的 `input_mode` 错了；先检查结构化输出，不要先改工具 |
| contains 自动选中 | `lookup_jobs → _route_job_matches` | 把 score 当成确认语义；必须检查 `match_type` |
| 恢复后岗位查询执行两次 | `confirm_job` 前混入查询 | 查询和 interrupt 未分节点，或恢复用了新 Thread |
| 取消后仍生成计划 | `confirm_job → compile_selected_job` | 只改了返回状态，忘改无条件出边 |
| 非法 code 抛通用异常 | `_confirm_job` | 缺少结构化错误状态和输出字段 |
| plan_ready 但没有候选人才 | `build_plan → END` | 本图只编译计划，尚未接下游人才检索/评估 |
| 跨租户岗位出现 | `database_job_lookup → TalentToolService` | Context 没传对，或工具层缺少 tenant/status 过滤 |

## 九、验收清单：用来判断是否真正学会

### 图结构

- [ ] 能画出 `START → receive_request → interpret_input`。
- [ ] 能解释 `_route_input` 的两个分支。
- [ ] 能解释 `_route_job_matches` 为什么不按最高分自动选择。
- [ ] 能指出 `interrupt` 的暂停位置和恢复后的下一节点。
- [ ] 能解释为什么 cancel/invalid selection 必须修改出边。

### State 数据流

- [ ] 能说清 `request_text → draft → job_matches → selected_job → talent_request/query_plan`。
- [ ] 能区分 State、Runtime Context、`thread_id`。
- [ ] 能解释 Draft、TalentRequest、QueryPlan 的不同层次。
- [ ] 能解释 `clarifications` 如何让状态变为 `clarification_required`。

### 工程可靠性

- [ ] 不从自然语言提取 tenant/permission。
- [ ] 不允许模型直接生成 SQL 或任意 job_code。
- [ ] 只接受当前候选列表白名单中的 job_code。
- [ ] 恢复使用同一 Thread。
- [ ] 恢复不重复执行已经完成的岗位查询。

### 作业测试

- [ ] 详细要求跳过岗位查询。
- [ ] 唯一 exact 不 interrupt。
- [ ] contains/fuzzy interrupt。
- [ ] 合法 select 得到 plan_ready。
- [ ] cancel 得到 cancelled 且不编译。
- [ ] 非法 code 得到 INVALID_JOB_CODE 结构化结果。
- [ ] 无匹配得到 clarification_required。
- [ ] Studio 三组实验都有 Thread、路径和最终 QueryPlan 记录。

## 十、本课总结

**第14课不是“给图加一个中断”，而是建立 Agent 的输入编译层：先把自然语言解析成受控中间表示，再根据状态做显式路由，在岗位歧义处暂停，把人的选择恢复到同一条图，最后只输出可追溯、可执行的 QueryPlan。**

推荐源码阅读顺序：

1. `backend/langgraph.json`：确认 Agent Server 加载哪个图。
2. `backend/app/talent_request_graph.py`：先看 State/节点/边，再看解释器。
3. `backend/app/talent_tools.py`：看岗位事实和 `match_type`。
4. `backend/app/query_plan.py`：看 QueryPlan 的白名单和可执行性。
5. `backend/tests/test_talent_request_graph.py`：用测试反推行为契约。
6. `backend/scripts/verify_talent_request_graph.py`：观察三条确定性运行轨迹。

附：本次指南重写只涉及文档；若你开始实现作业，请先在测试中建立 cancel 和非法 code 的行为契约，再修改图代码。




