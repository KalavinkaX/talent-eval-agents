# 第17课：Human-in-the-Loop 与可恢复执行——Agent 开发面试导向的完整学习与课后作业指南

> **读者**：刚本科毕业、目标是 Agent 开发岗位、第一次系统学习多 Agent / Subagent / LangGraph HITL 的同学。  
> **依据代码**：`D:\Code\K_Course\talent-eval-agents_learning` 当前工作树；课程实现对应提交 `e511193`，第14课基线为 `269e5586105909e4e00fd8281f81b8864217ddc6`。  
> **目标**：先建立设计框架，再从图的入口一路追到节点、State、interrupt、Checkpoint、恢复校验、取消和超时；最后给出两个课后作业的 TDD 实现步骤、代码骨架和面试表达。  
> **边界**：本文只生成学习文档，不修改本课业务代码；“当前实现”和“作业方案”会明确区分。

---

## 0. 先给结论：老师真正想让你学会什么

这一课表面上在讲 `interrupt()`，实际上要解决的是：

> **当 Agent 无法安全地自行决定下一步时，如何把任务暂停成一个可解释、可恢复、可校验、可取消、可超时的业务交互，而不是抛异常、丢上下文或让用户重新开始？**

你应该最终掌握六件事：

1. **识别阻塞原因**：区分“计划不可执行”和“计划可执行但候选集为空”。
2. **统一中断协议**：payload 告诉前端为什么停、当前要求是什么、有哪些条件、用户可以做什么。
3. **自由文本重新规划**：用户修改完整要求；系统把新文本写回 State，重新经过任务理解和计划编译。
4. **恢复前安全校验**：校验 JSON、action、`interaction_id`；错误不能把 Thread 弄死，而要带错误重新提问。
5. **区分暂停、取消、超时**：暂停不是失败；取消是业务终态；超时是调用层策略。
6. **理解多 Agent 边界**：本课是后续主图的“计划确认边界”，不负责证据检索、评分和报告；Subagent 应消费已确认的 `QueryPlan`。

### 面试版一句话

> 我把任务理解和候选筛选设计成一个可恢复的计划确认边界：模型负责把自然语言编译成受控的 `TalentRequestDraft`，程序负责路由和校验；当计划不可执行或候选为空时，图通过统一 `requirement_revision` payload 暂停，用户的完整修订文本写回 State，带版本号和交互 ID 防止过期提交，恢复后重新编译，取消和超时则由调用层显式收敛为终态。

---

## 1. 课程在整个项目中的位置

### 1.1 从第14课到第17课

第14课已经完成了第一种 HITL：用户从岗位候选中选择一个 JD。

```text
用户原始要求
  -> interpret_input
  -> 如果是岗位名：lookup_jobs
  -> exact 唯一：自动选
  -> contains/fuzzy：confirm_job interrupt
  -> 用户选择
  -> 同一个 Thread resume
  -> compile_selected_job
  -> build_plan
  -> END
```

第14课的 `confirm_job` 解决了“目标岗位不确定”，但图在 `QueryPlan` 编译完成后结束，执行层还有两个问题：

- **条件不可执行**：例如“经验丰富”缺少阈值，或条件互相矛盾，`QueryPlan.executable == False`。
- **条件可执行但结果为空**：例如“上海且工作年限 >= 15”，语法正确但没有候选；用户只看到了空数组，不知道哪个条件太严格。

第17课扩成：

```text
START
  |
  v
receive_request -> interpret_input
  |
  +-- job_name -> lookup_jobs -> select_exact_job / confirm_job / no_job_match
  |                              |
  |                              v
  +-- detailed_requirement -> build_plan
                                  |
                 +----------------+----------------+
                 |                                 |
       clarification_required                 plan_ready
                 |                                 |
                 v                                 v
       resolve_requirements                 filter_candidates
                 |                                 |
                 |                     candidates_ready / candidates_empty
                 |                                 |
                 +-----------<---------------------+
                 |
       revise -> request_text 写回 -> interpret_input
       cancel -> cancelled -> END
```

### 1.2 第18课为什么还要存在

第17课只负责输出一个**已确认、可执行、候选集可用的计划边界**：

```text
第17课：计划确认边界
输入：用户原始要求 + 历次修订
内部：岗位确认、任务理解、计划构建、条件修订、候选集可用性检查
输出：可执行 QueryPlan + candidate_ids，或 cancelled / no_job_match / 上限退出
不做：证据检索、动态评估维度、评分、报告合成

第18课：主图接入
输入：第17课输出
内部：证据检索、Subagent 评估、冲突检查、排序、报告
重点：父图/子图 State 映射、Checkpoint namespace、错误传播
```

这条边界是面试中的架构意识：**不要把所有工作塞进一个“大 Agent”；先把可恢复的计划确认和后续评估拆成稳定契约。**

---

## 2. 总体设计框架：按模块理解

| 模块 | 当前代码 | 输入 -> 输出 | 关键问题 |
|---|---|---|---|
| 运行时入口 | `backend/langgraph.json`、`graph` | 图名 -> 编译图 | 谁加载图、谁管理 Checkpoint |
| Context 与安全 | `DecisionContext`、`Runtime` | tenant/权限 -> 可信 Context | 租户和权限不能由模型生成 |
| 输入接收 | `_receive_request` | 文本 -> 干净 State | 是否有脏状态 |
| 任务理解 | `_interpret_input`、`TalentRequestDraft` | 自然语言 -> Draft | 模型只解释，不决定 SQL |
| 岗位事实 | `_lookup_jobs`、`TalentToolService` | 岗位名 -> `job_matches` | 事实来自数据库 |
| 岗位 HITL | `_confirm_job` | 候选岗位 -> `selected_job` | 选择校验与恢复 |
| 计划编译 | `_build_plan`、`QueryPlan` | Draft -> QueryPlan | 不可执行项显式化 |
| 候选筛选 | `_filter_candidates_node` | filters + Context -> candidate IDs | 空集是业务状态 |
| 统一修订 | `_resolve_requirements` | 阻塞 State -> payload/新文本 | 同时覆盖两类阻塞 |
| 恢复协议 | `_await_decision`、`_require_interaction` | `Command(resume=...)` -> decision | 防过期、带错重问 |
| 审计版本 | `plan_version`、`condition_revisions` | 修订 -> 历史 | 防旧表单覆盖新状态 |
| 调用层超时 | 验证脚本/未来 API | 暂停时刻 -> cancel | 图不持有长等待 |
| 测试观测 | `test_talent_request_hitl.py` | 固定依赖 -> 可重复结果 | 证明恢复语义 |

### 2.1 四个职责边界

**模型层**：输出 `TalentRequestDraft`，不得决定路由、租户、SQL、最终排名。  
**程序控制层**：决定路由、判断 `executable`、校验恢复数据、处理取消和版本。  
**工具数据层**：提供岗位和候选人事实，使用可信 `DecisionContext`。  
**调用交互层**：保存 `thread_id`、展示 payload、接收恢复请求、执行超时取消。

---

## 3. 先看 State：这节课最重要的数据协议

代码入口是 `backend/app/talent_request_graph.py` 中的 `TalentRequestState`。

| 字段 | 含义 | 生产语义 |
|---|---|---|
| `request_text` | 当前完整用户要求 | 每次自由文本修订后覆盖 |
| `input_mode` | `job_name` / `detailed_requirement` | 由模型解释，程序路由 |
| `draft` | `TalentRequestDraft` JSON | 可重新生成的中间表示 |
| `job_matches` | 岗位候选 | 岗位事实结果 |
| `selected_job` | 已确认岗位 | 后续编译的上下文 |
| `talent_request` | 业务任务对象 | 原文、目标岗位、硬条件、语义条件、偏好 |
| `query_plan` | 执行协议 | 结构化条件、语义要求、偏好、clarifications |
| `candidate_ids` | 候选人 ID | 空列表可以是可修订状态 |
| `condition_revisions` | 修订历史 | 使用 `operator.add` 追加 |
| `plan_version` | 当前修订代数 | 派生 `interaction_id` |
| `clarifications` | 未解决歧义 | 给用户和下游展示 |
| `status` | 运行状态 | 路由和对外结果 |
| `errors` | 错误列表 | 可展示、可观测 |

### 3.1 为什么 `condition_revisions` 使用追加 reducer

当前定义：

```python
condition_revisions: Annotated[list[dict[str, Any]], add]
```

节点返回：

```python
{"condition_revisions": [revision]}
```

不会覆盖以前记录，而是追加。这样恢复后能看到用户的完整修订历史。`plan_version` 是当前版本号，`condition_revisions` 是审计历史，两者相关但不是同一个字段。

### 3.2 为什么要把完整 `request_text` 写回

用户提交：

```json
{
  "interaction_id": "rr-v0",
  "action": "revise",
  "revision_text": "筛选上海或杭州 5 年以上工作经验的人才"
}
```

节点写回：

```python
{
    "request_text": revision_text,
    "selected_job": None,
    "job_matches": [],
    "candidate_ids": [],
    "condition_revisions": [revision],
    "plan_version": plan_version + 1,
    "status": "request_revised",
}
```

下一跳重新走 `interpret_input`。这样可以支持增加、删除、改写多个条件；不需要前端理解完整 DSL；也不会残留旧 Draft。代价是重新依赖模型，生产化要保存原文版本、结构化 diff 和 QueryPlan 快照。

---

## 4. 从入口开始追代码：完整调用链

### 4.1 Agent Server 入口

`backend/langgraph.json` 注册：

```json
{
  "graphs": {
    "talent_request": "./app/talent_request_graph.py:graph"
  }
}
```

文件末尾导出：

```python
graph = build_talent_request_graph(
    request_interpreter=model_request_interpreter,
    job_lookup=database_job_lookup,
    candidate_filter=database_candidate_filter,
)
```

Agent Server 加载的是编译好的图；单元测试用 `InMemorySaver()`。

### 4.2 `build_talent_request_graph`：图组装工厂

核心组装：

```python
builder = StateGraph(
    TalentRequestState,
    context_schema=DecisionContext,
    input_schema=TalentRequestInput,
    output_schema=TalentRequestOutput,
)

builder.add_node("receive_request", _receive_request)
builder.add_node("interpret_input", _interpret_input(request_interpreter))
builder.add_node("lookup_jobs", _lookup_jobs(job_lookup))
builder.add_node("select_exact_job", _select_exact_job)
builder.add_node("confirm_job", _confirm_job)
builder.add_node("compile_selected_job", _compile_selected_job(request_interpreter))
builder.add_node("build_plan", _build_plan)
builder.add_node("no_job_match", _no_job_match)
```

候选筛选启用时再挂载：

```python
builder.add_node("filter_candidates", _filter_candidates_node(candidate_filter))
builder.add_node("resolve_requirements", _resolve_requirements())
```

当前代码还保留第14课兼容行为：`candidate_filter=None` 时，`build_plan` 直接结束；注入筛选器后才进入第17课执行链路。这是**依赖注入 + 可组合图构建**。

### 4.3 `receive_request`：清理初始状态

核心行为：

```python
def _receive_request(state, runtime):
    if not runtime.context.tenant_id.strip():
        raise ValueError("Runtime Context 中的 tenant_id 不能为空")
    if not runtime.context.permission_scopes:
        raise ValueError("Runtime Context 中的 permission_scopes 不能为空")

    request_text = state["request_text"].strip()
    if not request_text:
        raise ValueError("request_text 不能为空")

    return {
        "request_text": request_text,
        "job_matches": [],
        "selected_job": None,
        "talent_request": {},
        "query_plan": {},
        "candidate_ids": [],
        "condition_revisions": [],
        "plan_version": 0,
        "clarifications": [],
        "status": "received",
        "errors": [],
    }
```

它验证 Context、验证输入，并清空旧岗位、旧计划、旧候选人。

### 4.4 `interpret_input`：自然语言到 Draft

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

模型输出先进入 `TalentRequestDraft`，再由 Pydantic 校验；模型没有直接输出 SQL，也没有直接改变图的下一跳。

路由是确定性的：

```python
def _route_input(state):
    return "lookup_jobs" if state["input_mode"] == "job_name" else "build_plan"
```

### 4.5 岗位路径：第14课 HITL 与本课关系

岗位查询：

```python
def lookup_jobs(state, runtime):
    job_query = str(state["draft"].get("job_query") or state["request_text"])
    matches = job_lookup(job_query, runtime.context)
    return {"job_matches": matches, "status": "jobs_found"}
```

路由规则：

```python
def _route_job_matches(state):
    exact_matches = [
        item for item in state["job_matches"]
        if item["match_type"] == "exact"
    ]
    if len(exact_matches) == 1:
        return "select_exact_job"
    if state["job_matches"]:
        return "confirm_job"
    return "no_job_match"
```

面试陷阱：`match_score == 1.0` 不等于业务上的 `exact`；只有唯一 `match_type == "exact"` 才自动选择。

岗位确认的恢复值类似：

```python
Command(resume={
    "action": "select",
    "job_code": "JD-AI-002",
})
```

第17课沿用同一核心思想，但把交互对象从“选择一个岗位”扩展为“修订整个计划要求”。

### 4.6 `build_plan`：把 Draft 编译成两个下游对象(和14课相比无变化)

```python
draft = TalentRequestDraft.model_validate(state["draft"])

query_plan = QueryPlan(
    task_type=TaskType.FIND_TALENT,
    filters=draft.filters,
    semantic_requirements=draft.semantic_requirements,
    preferences=draft.evaluation_preferences,
    clarifications=draft.clarifications,
)

status = "plan_ready" if query_plan.executable else "clarification_required"
```

`QueryPlan.executable` 当前是：

```python
@property
def executable(self) -> bool:
    return not self.clarifications
```

不变量是：任何查询执行器都不应该接收 `clarifications` 非空的计划。

路由：

```python
def _route_after_plan(state):
    if state["status"] == "clarification_required":
        return "resolve_requirements"
    return "filter_candidates"
```

### 4.7 `filter_candidates`：进入数据层

```python
def _filter_candidates_node(candidate_filter):
    def filter_candidates(state, runtime):
        plan = QueryPlan.model_validate(state["query_plan"])
        candidate_ids = candidate_filter(plan.filters, runtime.context)
        if candidate_ids:
            return {"candidate_ids": candidate_ids, "status": "candidates_ready"}
        return {"candidate_ids": [], "status": "candidates_empty"}
    return filter_candidates
```

第17课只把结构化硬条件交给候选筛选器；语义条件留给后续主图。空候选是业务状态，不是异常：

```python
def _route_candidates(state):
    if state["status"] == "candidates_empty":
        return "resolve_requirements"
    return END
```

---

## 5. 统一 `resolve_requirements`：本课最核心节点

### 5.1 为什么两类问题使用一个节点

计划阻塞和候选为空的共同点：

- 都不能继续安全执行；
- 都需要用户提供新要求；
- 都要把用户决策写回 State；
- 都要重新生成 Draft 和 QueryPlan；
- 都需要取消、错误重问和过期提交防护。

区别只在 `reason_code` 和 `blocking_reason`：

```python
reason_code = (
    "plan_blocked"
    if state["status"] == "clarification_required"
    else "empty_candidates"
)
```

统一节点的价值是：前端只处理一种交互类型，后端只维护一套自由文本修订协议。当前图确实把 `candidates_empty` 路由到 `_resolve_requirements`；文件中保留的 `_resolve_conditions`、`_resolve_empty_candidates` 是旧/更细粒度实现素材，不是当前主图实际使用的两条边。

### 5.2 payload 是人机协作接口契约

当前 payload 的关键字段：

```python
payload = {
    "type": "requirement_revision",
    "interaction_id": interaction_id,
    "reason_code": reason_code,
    "blocking_reason": blocking_reason,
    "question": "请补充或修订人才要求后重新提交，或取消本次任务",
    "request_text": editable_request,
    "request_source": request_source,
    "selected_job": selected_job_source,
    "filters": filters,
    "semantic_requirements": state.get("draft", {}).get("semantic_requirements", []),
    "clarifications": state.get("clarifications", []),
    "actions": ["revise", "cancel"],
}
```

| 字段 | 前端用途 | 后端意义 |
|---|---|---|
| `type` | 选择交互组件 | 协议类型 |
| `interaction_id` | 隐藏保存并回传 | 防旧表单提交 |
| `reason_code` | 显示计划阻塞/空候选 | 路由和埋点 |
| `blocking_reason` | 告诉用户为何暂停 | 可解释性 |
| `request_text` | 初始化完整编辑框 | 下一轮解释器输入 |
| `request_source` | 区分用户原文/JD | 审计和展示 |
| `selected_job` | 显示当前岗位上下文 | 避免用户不知基于哪个 JD |
| `filters` | 展示当前硬条件 | 空集诊断 |
| `semantic_requirements` | 展示语义条件 | 告知后续检索范围 |
| `clarifications` | 展示歧义/矛盾 | 不把模型失败变黑盒 |
| `actions` | 渲染动作 | 后端白名单的一部分 |

### 5.3 岗位画像作为可编辑原文

如果已经选中岗位，当前代码使用：

```python
editable_request = selected_job["content"]
request_source = "job_profile"
```

而不是只把岗位名称放回输入框。真正触发歧义的可能是 JD 中的“工作地点以上海或杭州为准”，用户必须看到完整画像才能准确修订。

### 5.4 `interaction_id`：用版本号防过期提交

```python
plan_version = state.get("plan_version", 0)
interaction_id = f"rr-v{plan_version}"
```

第一次暂停是 `rr-v0`；接受一次修订后：

```python
"plan_version": plan_version + 1
```

如果旧页面仍提交 `rr-v0`，而当前状态已是 `rr-v1`，就应该拒绝。这是一个轻量 optimistic concurrency token。

### 5.5 `_require_interaction`：恢复第一道门

```python
def _require_interaction(decision, expected_id):
    if not isinstance(decision, dict):
        raise ValueError("恢复数据必须是 JSON 对象")
    if decision.get("interaction_id") != expected_id:
        raise ValueError(
            f"恢复数据与当前暂停点不匹配，期望 interaction_id={expected_id}"
        )
    return decision
```

它防止：

- 字符串、数组等非对象恢复值；
- 旧浏览器提交已经过期的表单；
- 一个 Thread 的 decision 错发到另一个暂停点；
- 重试时把上一轮 action 发到新版本。

### 5.6 `_await_decision`：错误不抛出，带错重问

```python
def _await_decision(payload, parse):
    decision = interrupt(payload)
    while True:
        try:
            return parse(decision)
        except ValueError as exc:
            decision = interrupt({
                **payload,
                "resume_error": str(exc),
            })
```

恢复模式是：

1. 第一次 `interrupt(payload)` 暂停；
2. 用户提交恢复值；
3. LangGraph 从中断节点函数开头重新执行，再次遇到对应的 `interrupt()` 时返回恢复值，然后继续向下执行（详见 7.3）；
4. `parse` 失败时不让节点终止；
5. 重新 `interrupt`，附加 `resume_error`；
6. 同一个 Thread 等待下一次提交。

这与普通异常的区别：

```text
普通异常：节点失败 -> 调用方看到 error -> 可能需要重启任务
带错重问：节点留在交互边界 -> 用户修正 decision -> 同一个 Thread 继续
```

### 5.7 decision 校验协议

`resolve_requirements` 的 parse 逻辑等价于：

```python
def parse(decision):
    decision = _require_interaction(decision, interaction_id)
    action = decision.get("action")

    if action == "cancel":
        return {"action": "cancel"}
    if action != "revise":
        raise ValueError("action 必须是 revise 或 cancel")

    revision_text = decision.get("revision_text")
    if not isinstance(revision_text, str) or not revision_text.strip():
        raise ValueError("revise 动作必须携带非空 revision_text")

    return {"action": "revise", "revision_text": revision_text.strip()}
```

先校验对象和交互 ID，再校验 action 和参数，错误信息才稳定。

### 5.8 成功修订如何写回 State

```python
revision = {
    "interaction_id": interaction_id,
    "action": "revise",
    "reason_code": reason_code,
    "revision_text": revision_text,
    "plan_version": plan_version + 1,
}

return {
    "request_text": revision_text,
    "selected_job": None,
    "job_matches": [],
    "candidate_ids": [],
    "condition_revisions": [revision],
    "plan_version": plan_version + 1,
    "status": "request_revised",
}
```

清空 `selected_job` 和 `job_matches` 是保守设计：自由文本可能改变岗位目标，不能把新要求与旧岗位静默混合。路由重新回到解释器：

```python
def _route_resolution(state):
    if state["status"] == "request_revised":
        return "interpret_input"
    return END
```

---

## 6. 三条必须能画出来的时序链路

### 6.1 可执行且有候选

```text
invoke -> receive_request -> interpret_input
  -> build_plan(QueryPlan.executable=True)
  -> filter_candidates([C001, C002])
  -> END(status=candidates_ready)
```

应满足：无 `__interrupt__`、`plan_version == 0`、`condition_revisions == []`。

### 6.2 计划不可执行

```text
用户：年龄小于30且大于40
  -> interpret_input(clarifications)
  -> build_plan(clarification_required)
  -> resolve_requirements interrupt(plan_blocked)
       +-- cancel -> cancelled -> END
       +-- revise(full text)
              -> request_text 覆盖
              -> interpret_input -> build_plan -> filter_candidates
```

计划阻塞时不应调用候选查询。

### 6.3 计划可执行但候选为空

```text
用户：上海 + 年限>=15
  -> build_plan(plan_ready)
  -> filter_candidates([])
  -> candidates_empty
  -> resolve_requirements interrupt(empty_candidates)
```

payload 要告诉用户当前条件和阻塞原因，而不是只返回空数组。

---

## 7. Checkpoint、Thread、暂停和恢复到底是什么

### 7.1 `thread_id` 是任务身份

第一次调用：

```python
config = {"configurable": {"thread_id": "hitl-empty"}}
first = graph.invoke(
    {"request_text": "筛选上海 15 年以上的人才"},
    config,
    context=context,
)
```

恢复必须复用同一个 config：

```python
resumed = graph.invoke(
    Command(resume={
        "interaction_id": "rr-v0",
        "action": "revise",
        "revision_text": "筛选上海 5 年以上工作经验的人才",
    }),
    config,
    context=context,
)
```

`thread_id` 标识整个任务；`interaction_id` 标识这个任务当前某一次暂停。换 Thread 就不是恢复，而是启动另一个任务。

### 7.2 为什么暂停不占进程

`interrupt()` 把 State 和暂停位置交给 Checkpoint，调用方拿到结果后返回给用户。用户可能十分钟或几小时后再提交，期间不应有 Python 线程一直 `sleep`。

```text
错误：while not answer: time.sleep(1)
正确：保存 thread_id + checkpoint + payload；HTTP 请求结束；稍后 Command(resume)
```

### 7.3 恢复会不会重复执行已完成节点

**准确结论：普通恢复不会把已经完成的上游节点从头再跑一遍，但当前被中断的节点会从节点函数开头重新执行。再次遇到对应的 `interrupt()` 时，框架返回该中断对应的恢复值，之后才继续执行后面的代码。**

因此，“从 `interrupt()` 后继续”只能描述取得恢复值后的逻辑效果，不能理解为保存了一个正在运行的 Python 函数栈，然后直接跳到下一行。Checkpoint 保存图状态和恢复所需信息，不是冻结整个进程或 Python 调用栈。

#### 7.3.1 区分图级恢复、节点重跑和业务回环

| 层次 | 当前项目恢复时的行为 |
|---|---|
| 已完成的上游节点，例如之前的 `lookup_jobs` | 不会仅因为恢复当前暂停点而重新执行 |
| 当前被中断的 `resolve_requirements` 节点 | 从节点函数开头重新执行，包括构造 payload 等逻辑 |
| 节点调用的 `_await_decision()` | 会随节点重新执行而再次调用 |
| 合法修订之后的 `interpret_input`、`build_plan`、候选筛选 | 根据图的条件边主动再次运行，这是业务回环，不是恢复把整张图重跑 |

限定条件：这里讨论普通 `Command(resume=...)` 恢复，不是主动重启任务、历史 Checkpoint 重放或图路由明确返回某个上游节点。因此，不要把“不重复执行”扩大为“某个节点永远只会执行一次”。

**函数名称也要分清：** `_resolve_requirements()` 是构造节点函数的工厂；它返回的内部函数 `resolve_requirements(state, runtime)` 才是注册到图中的节点。恢复时重跑的是后者，不是从工厂函数或整张图的入口重新开始。

代码位置：`D:\Code\K_Course\talent-eval-agents_learning\backend\app\talent_request_graph.py`，查看 `_resolve_requirements()`、`_await_decision()` 和 `_route_resolution()`。

#### 7.3.2 对照 `_await_decision()` 理解一次正常恢复

```python
def _await_decision(payload, parse):
    decision = interrupt(payload)                  # A：首次暂停
    while True:
        try:
            return parse(decision)                 # B：校验成功，退出整个函数
        except ValueError as exc:
            decision = interrupt({                 # C：校验失败，带错再次暂停
                **payload,
                "resume_error": str(exc),
            })
```

第一次调用：

```text
resolve_requirements 从头执行
  → 计算 interaction_id、构造 payload
  → _await_decision
  → A：interrupt(payload)
  → 暂停，本次图调用返回中断信息
```

这时 A 没有正常返回用户决策，`decision` 尚未取得恢复值，也没有进入下面的校验循环。用户等待期间，不是某个 Python 线程一直停在这里轮询输入。

调用方稍后用原来的 `thread_id` 提交：

```python
graph.invoke(
    Command(resume={
        "interaction_id": "rr-v0",  # 示例：应从当前暂停 payload 读取
        "action": "revise",
        "revision_text": "筛选上海至少5年工作经验的人才，学历不限",
    }),
    config,  # 原来的 thread_id；context 按项目调用方式传入
    context=context,
)
```

恢复过程：

```text
重新进入 resolve_requirements 节点函数
  → 重新计算 interaction_id、重新构造 payload
  → 再次调用 _await_decision
  → A：interrupt(payload) 返回对应的 Command(resume=...) 恢复值
  → 恢复值赋给局部变量 decision
  → B：parse(decision)
  → return 退出 _await_decision
  → 外层节点按 cancel/revise 分支返回 State 更新
```

**注意 `decision` 是局部变量，不是 `_await_decision` 的参数。** 它接收的是整个恢复对象，例如 `interaction_id + action + revision_text`，不只是用户输入的一段文字。`payload` 是发送给调用方的问题与上下文，`decision` 是调用方送回的答案，两者不是同一个对象。

#### 7.3.3 为什么 `while True` 不会一直空转？错误重问怎么恢复？

- 校验成功：`return parse(decision)` 退出整个 `_await_decision()`，不会继续循环。
- 校验失败：进入 C，新的 `interrupt()` 暂停图并返回带 `resume_error` 的载荷，而不是不停尝试同一个输入。
- 再次恢复：节点仍从头重跑，节点内已有的恢复值按中断调用顺序被取回；不是直接跳到 C 下一行。

假设第一次答案 D1 的 `revision_text` 为空，第二次答案 D2 修正了文本：

```text
第1次调用：A 暂停，等待 D1
第2次调用：节点重跑 → A 返回 D1 → parse(D1) 失败 → C 暂停，等待 D2
第3次调用：节点重跑 → A 返回 D1 → parse(D1) 再次失败
                         → C 返回 D2 → parse(D2) 成功 → return
```

所以，**校验函数也可能因恢复而重复执行**。它适合做纯校验，不适合顺便扣费、发送邮件或创建业务记录。若连续多次提交错误，当前循环会出现多次中断；更复杂的流程可以将提问、校验和错误状态拆成独立节点与条件边，以降低节点内重放逻辑的复杂度。

#### 7.3.4 `interaction_id` 校验不等于完整幂等

项目的恢复校验：

```python
def _require_interaction(decision, expected_id):
    if not isinstance(decision, dict):
        raise ValueError("恢复数据必须是 JSON 对象")
    if decision.get("interaction_id") != expected_id:
        raise ValueError("恢复数据与当前暂停点不匹配")
    return decision
```

当前需求修订节点使用：

```python
interaction_id = f"rr-v{plan_version}"
```

这段代码主要回答：**“这个答案声明的交互版本，是否匹配当前 Thread 中这一轮需求修订？”** 它是交互关联与过期提交校验，不是副作用幂等的完整实现。

例如：当前已经是 `rr-v1`，再提交 `rr-v0`，会被拒绝；但是同一轮收到两次 `rr-v0`，只要两次校验时预期值都还是 `rr-v0`，这个函数本身并不能原子地保证只有一次通过。后端如何调度恢复、是否串行执行，同样影响实际结果。

| 机制 | 主要解决的问题 | 不要误认为它解决了什么 |
|---|---|---|
| `thread_id` | 找到目标任务的 Checkpoint 状态 | 不是用户身份认证或权限校验 |
| `interaction_id` / `plan_version` | 在目标任务内校验当前交互版本，拒绝过期答案 | 不是全局唯一 ID，也不是副作用只执行一次的保证 |
| LangGraph 的中断恢复匹配 | 让恢复值返回给对应的 `interrupt()` 调用 | 不是依赖自定义 `_require_interaction()` 才能重跑节点 |
| 幂等键与持久化去重记录 | 同一业务操作重复请求，仍只产生一次预期效果 | 不能只用普通内存判断应对多进程竞争 |
| 锁、唯一约束、条件更新或版本 CAS | 原子地控制并发争抢 | 仍需结合外部工具的幂等能力处理跨系统副作用 |

特别注意：`rr-v0` 在不同 Thread 中可能相同；同一版本的带错重问也可能继续使用相同值。因此，它更接近“这一轮业务交互的版本标签”，不是每一次底层 `interrupt()` 的全局唯一标识。业务自定义 `interaction_id` 与 LangGraph 内部的中断标识也不是同一概念。

#### 7.3.5 真正需要保护幂等的是副作用

错误示例（示意代码，不是项目已有功能）：

```python
def resolve_requirements(state):
    send_email("请补充要求")       # 节点每次重跑，可能再次发送
    decision = interrupt(payload)
    # ...
```

即使后面调用 `_require_interaction()` 检查了答案，恢复时仍可能已经重复执行 `send_email()`。这个校验既不能防止中断前的重复发送，也不能撤销已发生的副作用。

设计思路：

1. 中断前优先保留读取 State、构造 payload 等可安全重复的逻辑。
2. 尽量把外部写入等副作用与暂停逻辑分开，放到独立节点或持久化任务中；但拆分本身不自动解决重试窗口中的重复写入。
3. 给具体业务操作设计幂等键，例如由 `thread_id + 交互版本 + 操作类型` 组成。需要多次合法同类操作时，还应加入对应的业务操作标识。
4. 用数据库唯一约束、原子去重记录或目标工具的幂等接口保证重复调用不会产生额外效果；涉及跨系统一致性时再设计事务、outbox 等机制。
5. 调用层对同一 Thread 的恢复提交做串行化或原子条件校验；已经处理过的重复请求可以返回已记录结果，而不是再次执行。

**可替换到代码中的理解性注释（本次只更新文档，不修改你的代码）：**

```python
# 恢复时，LangGraph 从当前 resolve_requirements 节点函数开头重新执行。
# 再次遇到对应的 interrupt() 时，返回该中断对应的恢复值，
# 并将整个决策对象赋给局部变量 decision，再进入 parse 校验。
# 已完成的上游节点不因本次恢复自动重跑，但图的业务回环可能让某些节点再次运行。
# 中断前的副作用必须可安全重复或有幂等保护；interaction_id 只校验交互版本。
```

**面试表达：**

> LangGraph 恢复不是恢复 Python 活跃调用栈，而是基于 Checkpoint 重新执行中断节点，在对应的 interrupt 调用处取得恢复值。已完成的上游节点不会因普通恢复自动重跑，但当前节点内的代码可能重放。interaction_id 用于交互关联和过期提交校验；副作用幂等、重复请求去重和并发控制，还需要调用层与存储层共同保证。

官方核对资料：LangGraph Interrupts（`https://docs.langchain.com/oss/python/langgraph/interrupts`）、Persistence（`https://docs.langchain.com/oss/python/langgraph/persistence`）、`interrupt` 源码（`https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/types.py`）。具体 API 与框架行为以项目安装版本为准。

---

## 8. 取消与超时：图状态和调用层策略分开

### 8.1 取消是用户决策，进入终态

```python
Command(resume={
    "interaction_id": "rr-v0",
    "action": "cancel",
})
```

节点返回：

```python
{"status": "cancelled"}
```

然后 `_route_resolution` 返回 `END`。区别如下：

| 情况 | 是否可恢复 | 状态 |
|---|---:|---|
| 当前暂停等待用户 | 是 | `next` 有节点 |
| 恢复值格式错误 | 是 | 重新 interrupt，带 `resume_error` |
| 用户主动取消 | 通常否 | `cancelled`，`next=()` |
| 超时自动取消 | 通常否 | `cancelled` 或 `expired` |
| 程序异常 | 取决于错误策略 | 不能当作用户取消 |

### 8.2 为什么超时策略放调用层

#### 技术视角

1. `graph` 负责状态转换，不负责持有 wall-clock 定时器。
2. `interrupt` 后调用通常已经返回；用户等待时间发生在两次调用之间。
3. 图可能跑在 Agent Server、队列 worker、Serverless 或多副本环境；图内定时器难以保证只触发一次。
4. Checkpoint 负责保存状态和暂停时间，调用层/调度器适合统一读取并决定是否 cancel。
5. 调用层可以统一管理多个图、通知、重试、SLA、租户策略和审计。

#### 产品视角

1. “多久算超时”属于产品 SLA，不是任务理解逻辑。
2. 招聘筛选可能 30 分钟，审批可能 24 小时。
3. 超时后的动作可能是提醒、转人工、保存草稿或取消。
4. 把策略放调用层，不改图拓扑就能换产品策略。
5. 图只需要支持合法 `cancel` 决策，减少图内生命周期分支。

**面试表达：**

> 交互等待发生在两次图调用之间，图调用本身已经结束；超时是跨任务的生命周期策略，所以由调用层保存 `paused_at`，scheduler 到期后用原 `interaction_id` 提交 `cancel`。图只负责处理合法状态转换。

### 8.3 调用层超时实现骨架

本作业只需要一条最小链路：

```text
首次收到 interrupt → 固定本轮 paused_at 和 interaction_id
    → 调用层检查当前暂停与截止时间
    → 原暂停到期时提交 Command(resume={interaction_id, action: "cancel"})
    → 图写回 cancelled 并结束
```

完整可运行函数、调用示例和测试见 **第 12 节**，直接按那一份实现即可，无需维护两套代码。该函数还包含已结束不重复取消、旧轮次不误取消新轮次的基本检查。

### 8.4 不要把 `asyncio.wait_for` 当成用户等待超时

```text
单次执行超时：限制一次模型/工具/图调用在进程内运行多久
交互等待超时：保存 paused_at，定时扫描，跨请求代提交 cancel
```

`asyncio.wait_for(graph.invoke(...), timeout=30)` 管不到“调用返回 interrupt 后，用户 30 分钟没有回复”。

---

## 9. 本课与多 Agent / Subagent 的关系

本课不是让多个 Agent 互相聊天，而是先给 Agent 系统建立可恢复边界。

### 9.1 为什么先有 QueryPlan，再有 Subagent

```text
确认的 QueryPlan
  -> 候选人集合
  -> 动态评估维度
  -> 候选人 × 维度的 Subagent
  -> 证据评估
  -> 冲突检查
  -> 确定性排序
  -> 报告合成
```

如果计划未确认就启动 Subagent，不同分支可能使用不同解释；用户修订后还要取消大量旧任务，结果也无法证明基于哪一版要求。

后续任务应该携带版本：

```json
{
  "task_id": "candidate:C001:dimension:2",
  "plan_version": 1,
  "query_plan_hash": "..."
}
```

这样可拒绝旧计划的晚到结果。

### 9.2 可恢复子图的原则

1. 子图输入/输出用 TypedDict 或 Pydantic 契约定义。
2. 只传下游必需字段，不复制整个父图 State。
3. `plan_version`、`query_plan_hash` 随任务传递。
4. 父图和子图的 Checkpoint namespace 隔离。
5. 先确认计划，再启动昂贵的评估分支。
6. 模型负责解释/候选方案，程序负责最终校验和状态转移。

---

## 10. 建议阅读顺序

1. 先看 `RequestStatus`、`TalentRequestInput/Output/State`、`TalentRequestDraft`、`QueryPlan`。
2. 只看 `build_talent_request_graph` 的 `add_node`、`add_edge`、`add_conditional_edges`。
3. 追正常路径：`receive_request -> interpret_input -> build_plan -> filter_candidates`。
4. 追计划阻塞：`build_plan -> resolve_requirements -> interrupt -> resume`。
5. 追空候选：`filter_candidates -> candidates_empty -> resolve_requirements`。
6. 追错误和取消：`_require_interaction`、`_await_decision`、`_route_resolution`。
7. 最后读测试：
   - `test_executable_plan_reaches_candidates_ready_without_interrupt`
   - `test_plan_block_and_empty_result_use_one_free_text_revision_node`
   - `test_stale_interaction_id_reasks_and_thread_stays_resumable`
   - `test_empty_revision_text_reasks_with_error`
   - `test_cancel_from_resolve_requirements_ends_run`

---

## 11. 课后作业一：为 `resolve_requirements` 增加最大修订次数

### 11.1 真实问题

当前可以无限循环：

```text
resolve_requirements -> revise -> interpret_input -> build_plan
    -> clarification_required -> resolve_requirements -> ...
```

后果是成本失控、用户无限等待、任务无法收敛，也没有“转人工”的清晰出口。

### 11.2 先定义语义

推荐：

> `MAX_REQUIREMENT_REVISIONS = 3` 表示最多接受 3 次有效 `revise`；第一次展示交互不计数，`plan_version` 从 0 开始，每次成功接受修订后加 1。

状态：

```text
初始暂停：v0，允许 revise
第1次成功修订：v1
第2次成功修订：v2
第3次成功修订：v3
第3次修订后仍阻塞，再次到达 resolver：revision_limit_reached -> END
```

这样第3次用户输入仍被完整编译一次；只有它仍不能解决问题才退出。

### 11.3 先写失败测试（TDD）

不要先改实现。用一个每次都返回 clarification 的解释器：

```python
class AlwaysBlockedInterpreter:
    def __init__(self):
        self.calls = []

    def __call__(self, request_text, selected_job=None):
        self.calls.append(request_text)
        return TalentRequestDraft(
            input_mode="detailed_requirement",
            filters=[],
            clarifications=[
                Clarification(
                    expression="条件始终无法确定",
                    reason="测试解释器故意保持阻塞",
                )
            ],
        )
```

测试目标：

```python
def test_requirement_revision_limit_returns_terminal_status():
    interpreter = AlwaysBlockedInterpreter()
    candidate_filter = StubCandidateFilter()
    graph = build_talent_request_graph(
        request_interpreter=interpreter,
        job_lookup=lambda query, context: [],
        candidate_filter=candidate_filter,
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "hitl-revision-limit"}}
    context = _context()

    result = graph.invoke(
        {"request_text": "一个始终无法确定的要求"},
        config,
        context=context,
    )

    for index in range(3):
        assert "__interrupt__" in result
        payload = result["__interrupt__"][0].value
        assert payload["interaction_id"] == f"rr-v{index}"
        result = graph.invoke(
            Command(resume={
                "interaction_id": f"rr-v{index}",
                "action": "revise",
                "revision_text": f"第 {index + 1} 次仍然模糊的要求",
            }),
            config,
            context=context,
        )

    assert result["status"] == "revision_limit_reached"
    assert graph.get_state(config).next == ()
    assert len(graph.get_state(config).values["condition_revisions"]) == 3
```

如果采用不同的上限语义，测试中的循环次数要同步调整；关键是测试必须证明“超过限制后退出”，而不是只测常规修订成功。

### 11.4 最小实现方案

在 `backend/app/talent_request_graph.py` 增加：

```python
MAX_REQUIREMENT_REVISIONS = 3

RequestStatus = Literal[
    # 现有状态...
    "revision_limit_reached",
]
```

在 `_resolve_requirements` 构建 payload 前判断：

```python
revision_count = len(state.get("condition_revisions", []))
if revision_count >= MAX_REQUIREMENT_REVISIONS:
    return {
        "candidate_ids": [],
        "clarifications": state.get("clarifications", []),
        "errors": [
            f"已达到最大要求修订次数 {MAX_REQUIREMENT_REVISIONS}，请重新发起任务或转人工"
        ],
        "status": "revision_limit_reached",
    }
```

现有路由已经是：

```python
def _route_resolution(state):
    if state["status"] == "request_revised":
        return "interpret_input"
    return END
```

新状态不是 `request_revised`，自然进入 `END`。如果项目希望显式写出所有终态，也可以在映射中加入 `revision_limit_reached: END`。

### 11.5 边界：非法提交不消耗次数

这些情况不应该追加 `condition_revisions`：

```json
{"interaction_id":"rr-v0","action":"revise","revision_text":"   "}
```

```json
{"interaction_id":"rr-v9","action":"revise","revision_text":"新的要求"}
```

前者没有通过非空校验，后者没有通过交互 ID 校验；它们应该由 `_await_decision` 带错重问，而不是消耗修订次数。

### 11.6 作业一验证清单

- [ ] 初始阻塞仍生成 `rr-v0`。
- [ ] 3 次合法修订都写入历史。
- [ ] 第3次修订会重新跑解释器。
- [ ] 第3次修订后仍阻塞时返回 `revision_limit_reached`。
- [ ] 终态 `graph.get_state(config).next == ()`。
- [ ] 达到上限后不再调用 `interrupt`。
- [ ] 非法 action、空文本、过期 ID 不增加次数。
- [ ] 取消仍返回 `cancelled`，不与上限退出混同。
- [ ] 空候选路径也受同一上限保护。

### 11.7 生产化思考

课程作业用 State 中的 `condition_revisions` 即可；生产还应考虑按租户配置上限、区分计划修订和候选放宽、把上限退出转人工、记录模型成本，并用 `query_plan_hash` 识别“新文本但计划未变化”。

---

## 12. 课后作业二：调用层超时取消——最小可运行实现

> 本节按“先简单实现，完成作业要求即可”收敛：**一个超时函数 + 一个调用脚本 + 几个测试**。不需要 SQLite、FastAPI、分布式锁或完整调度平台，也不需要修改图的业务节点。
>
> 下列新增文件是你按步骤创建的作业代码，不代表课程仓库已经存在这些文件。本次只更新学习文档，示例已经在临时目录中用项目依赖验证。

### 12.1 先理解：为什么超时放在调用层，而不是 graph 内？

**技术视角：**

- 当前图调用执行到 `interrupt(payload)` 后返回暂停结果；Checkpoint 保留可恢复状态。等待用户回复不等于节点持续执行。
- 用户可能在几分钟后才发起下一次调用；在某次调用外等待了多久，应由调用脚本或服务检查。
- 图已经支持合法的 `cancel` 决策，调用层到期后代用户提交这个决策即可，不必另加一套超时节点。

**产品视角：**

- 等待 5 秒、30 分钟还是一天，是产品规则；超时后取消、提醒还是保留草稿，也是产品规则。
- 本作业选择最简单的一种：**本轮要求澄清超时，直接取消任务。** 调整期限只改调用参数，不改图拓扑。

一句话总结：**图负责“怎么处理取消”，调用层负责“什么时候因超时发起取消”。**

### 12.2 本次只实现这条链路

```text
调用 graph.invoke(初始输入, 同一个 thread_id)
    ↓
resolve_requirements → interrupt(payload) → 返回调用层
    ↓
调用层记录本轮 interaction_id 和暂停时刻 paused_at（只记录一次）
    ↓
调用层周期检查：是否仍在原暂停？是否到达截止时间？
    ├─ 未超时 / 已结束 / 已进入下一轮 → 不取消
    └─ 原暂停已超时
           ↓
       graph.invoke(Command(resume={interaction_id, action: "cancel"}))
           ↓
       当前 resolve_requirements 节点从头重跑
           ↓
       _await_decision → interrupt 获得恢复值 → 校验取消决策
           ↓
       返回 {"status": "cancelled"} → 路由 END
```

结合现有代码阅读：

1. 图的入口与节点注册：`D:\Code\K_Course\talent-eval-agents_learning\backend\app\talent_request_graph.py` 中的 `build_talent_request_graph`。
2. 统一中断：同文件中 `_resolve_requirements` 工厂生成的 `resolve_requirements` 节点；载荷类型为 `requirement_revision`，版本号如 `rr-v0`。
3. 恢复校验：同文件的 `_await_decision` 与 `_require_interaction`。
4. 取消分支：解析出的 `action == "cancel"` 返回 `{"status": "cancelled"}`，路由走 `END`。

本作业只处理 `requirement_revision`，所以**计划不可执行、候选集为空**都能使用它；不宣称覆盖 `confirm_job` 的岗位确认，因为那个载荷没有相同的 `interaction_id` 协议。

### 12.3 第一步：写一个调用层超时函数

新建：`D:\Code\K_Course\talent-eval-agents_learning\backend\scripts\hitl_timeout.py`。

```python
"""作业二：由调用层检查交互等待超时，并提交取消。"""
from datetime import datetime, timedelta, timezone

from langgraph.types import Command


def cancel_if_expired(
    graph, config, *, interaction_id, paused_at, context,
    timeout_seconds=1800, now=None,
):
    """只处理当前 requirement_revision；实际提交取消返回 True。"""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds 必须大于 0")
    now = now if now is not None else datetime.now(timezone.utc)
    if paused_at.tzinfo is None or now.tzinfo is None:
        raise ValueError("paused_at 和 now 必须携带时区")

    # 1. 重新读取同一个 Thread，避免对已结束的任务重复取消。
    snapshot = graph.get_state(config)
    pending = [item for task in snapshot.tasks for item in task.interrupts]
    if not snapshot.next or not pending:
        return False

    # 2. 本作业只处理统一的要求修订中断，且不能误取消下一轮中断。
    payload = pending[0].value
    if payload.get("type") != "requirement_revision":
        return False
    if payload.get("interaction_id") != interaction_id:
        return False

    # 3. 到达截止时间才取消；相等时也视为超时。
    deadline = paused_at + timedelta(seconds=timeout_seconds)
    if now < deadline:
        return False

    # 4. 不直接改 State，沿用图已有的合法取消协议。
    graph.invoke(
        Command(resume={"interaction_id": interaction_id, "action": "cancel"}),
        config,
        context=context,
    )
    return True
```

逐段理解即可，不用抽象成多个类：

| 代码部分 | 做什么 | 为什么需要 |
|---|---|---|
| `get_state(config)` | 读取当前 Thread 的最新快照 | 用户可能已经回复，不能凭旧记录直接取消 |
| `snapshot.next`、`task.interrupts` | 检查任务是否还在暂停 | 终态再次检查直接返回，不重复提交 |
| 对比 `interaction_id` | 确认还是记录时间的那一轮 | 老一轮计时不能误取消修订后的新一轮 |
| `paused_at + timedelta(...)` | 算出截止时间 | 必须基于固定起点，而非每次重新计时 |
| `Command(resume=...)` | 按已有决策协议恢复并取消 | 状态写回由图节点完成，而非调用层强改状态 |

返回 `True` 表示本次已调用图提交取消；`False` 表示本次没有提交。若图调用异常，异常直接抛出，不把失败伪装成成功。

### 12.4 第二步：写一个最简单的调用脚本

新建：`D:\Code\K_Course\talent-eval-agents_learning\backend\scripts\demo_hitl_timeout.py`。

为了只关注超时机制，复用现有的 `D:\Code\K_Course\talent-eval-agents_learning\backend\scripts\verify_talent_hitl_resume.py` 中的 `DemoInterpreter` 和 `DemoCandidateFilter`：它们使用固定条件与内存候选人，不需要真正调用模型或查询候选数据库。

```python
"""最小自动取消演示：5 秒后取消，不接收用户输入。"""
from datetime import datetime
from time import sleep
from uuid import uuid4

from langgraph.checkpoint.memory import InMemorySaver

from app.talent_decision_graph import DecisionContext
from app.talent_request_graph import build_talent_request_graph
from scripts.verify_talent_hitl_resume import DemoCandidateFilter, DemoInterpreter
from scripts.hitl_timeout import cancel_if_expired


def main():
    graph = build_talent_request_graph(
        request_interpreter=DemoInterpreter(),
        job_lookup=lambda _query, _context: [],
        candidate_filter=DemoCandidateFilter(),
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": f"timeout-demo-{uuid4()}"}}
    context = DecisionContext(tenant_id="course-demo", permission_scopes=("hr_private",))

    result = graph.invoke(
        {"request_text": "筛选上海 15 年以上的人才"}, config, context=context,
    )
    payload = result["__interrupt__"][0].value
    # 收到本轮 interrupt 后只记录一次，不要在每次检查时重新赋值。
    paused_at = datetime.fromisoformat(graph.get_state(config).created_at)
    interaction_id = payload["interaction_id"]
    print("已暂停：", payload["reason_code"], interaction_id)

    # 定时检查在调用脚本中，不在 graph 节点中。
    while graph.get_state(config).next:
        if cancel_if_expired(
            graph, config, interaction_id=interaction_id, paused_at=paused_at,
            context=context, timeout_seconds=5,
        ):
            print("等待超时，已提交 cancel")
            break
        sleep(1)

    snapshot = graph.get_state(config)
    print("最终状态：", snapshot.values["status"])
    print("后续节点：", snapshot.next)


if __name__ == "__main__":
    main()
```

运行：

```powershell
cd D:\Code\K_Course\talent-eval-agents_learning\backend
uv run --no-sync python -m scripts.demo_hitl_timeout
```

预期约 5 秒后输出：

```text
已暂停： empty_candidates rr-v0
等待超时，已提交 cancel
最终状态： cancelled
后续节点： ()
```

这里有四个容易写错的地方：

1. **首次暂停时读取一次时间。** 这里从 `snapshot.created_at` 读取本轮暂停快照的时间；后续检查复用 `paused_at`，不能每次重新读取最新快照时间当起点。错误恢复也可能产生新 Checkpoint，否则期限会被不断延后。
2. **恢复使用原 Thread。** 初次调用与取消调用必须使用同一个 `config`；示例也继续传入相同的租户、权限 `context`。
3. **sleep 在调用脚本里。** 图节点没有 `sleep`。这段演示脚本会在进程里等待，但不意味着暂停的图本身持续运行；作业无需实现生产调度器。
4. **演示脚本只展示自动取消。** 它不接收用户输入。接入已有人工恢复入口时，收到新的 `interaction_id` 就记录新一轮 `paused_at`；同一 ID 的带错重问不重新开始计时。

### 12.5 第三步：用几个测试证明超时策略正确

新建：`D:\Code\K_Course\talent-eval-agents_learning\backend\tests\test_hitl_timeout.py`。

测试中的 `now` 是注入的固定时间，**不用真的 sleep 等 30 秒**；图仍然使用项目真实的 `build_talent_request_graph`，不是用假图代替恢复链路。

```python
from datetime import datetime, timedelta, timezone

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.talent_decision_graph import DecisionContext
from app.talent_request_graph import build_talent_request_graph
from scripts.verify_talent_hitl_resume import DemoCandidateFilter, DemoInterpreter
from scripts.hitl_timeout import cancel_if_expired


@pytest.fixture
def paused_task():
    graph = build_talent_request_graph(
        request_interpreter=DemoInterpreter(),
        job_lookup=lambda _query, _context: [],
        candidate_filter=DemoCandidateFilter(),
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "timeout-test"}}
    context = DecisionContext(tenant_id="course-demo", permission_scopes=("hr_private",))
    result = graph.invoke(
        {"request_text": "筛选上海 15 年以上的人才"}, config, context=context,
    )
    pause_args = {
        "interaction_id": result["__interrupt__"][0].value["interaction_id"],
        "paused_at": datetime(2026, 10, 3, tzinfo=timezone.utc),
        "context": context,
        "timeout_seconds": 30,
    }
    return graph, config, pause_args


def test_before_deadline_keeps_paused(paused_task):
    graph, config, args = paused_task
    assert not cancel_if_expired(
        graph, config, **args, now=args["paused_at"] + timedelta(seconds=29),
    )
    snapshot = graph.get_state(config)
    assert snapshot.values["status"] == "candidates_empty"
    assert snapshot.next == ("resolve_requirements",)


@pytest.mark.parametrize("elapsed", [30, 31])
def test_deadline_cancels_and_recheck_is_noop(paused_task, elapsed):
    graph, config, args = paused_task
    now = args["paused_at"] + timedelta(seconds=elapsed)
    assert cancel_if_expired(graph, config, **args, now=now)
    snapshot = graph.get_state(config)
    assert snapshot.values["status"] == "cancelled"
    assert snapshot.next == ()
    assert not cancel_if_expired(graph, config, **args, now=now)


def test_completed_task_is_not_cancelled(paused_task):
    graph, config, args = paused_task
    graph.invoke(Command(resume={
        "interaction_id": args["interaction_id"], "action": "revise",
        "revision_text": "筛选上海 5 年以上的人才",
    }), config, context=args["context"])
    assert not cancel_if_expired(
        graph, config, **args, now=args["paused_at"] + timedelta(seconds=31),
    )
    assert graph.get_state(config).values["status"] == "candidates_ready"


def test_old_timer_does_not_cancel_new_revision(paused_task):
    graph, config, args = paused_task
    result = graph.invoke(Command(resume={
        "interaction_id": args["interaction_id"], "action": "revise",
        "revision_text": "筛选上海 15 年以上的人才",
    }), config, context=args["context"])
    assert result["__interrupt__"][0].value["interaction_id"] == "rr-v1"
    assert not cancel_if_expired(
        graph, config, **args, now=args["paused_at"] + timedelta(seconds=31),
    )
    assert graph.get_state(config).next == ("resolve_requirements",)
```

运行：

```powershell
cd D:\Code\K_Course\talent-eval-agents_learning\backend
uv run --no-sync pytest -q tests/test_hitl_timeout.py
```

4 个测试函数，其中截止时间测试参数化为 2 个用例，合计 **5 个测试用例**：

| 场景 | 预期 |
|---|---|
| 暂停 29 秒，期限 30 秒 | 不取消，仍等待 `resolve_requirements` |
| 恰好 30 秒 | 取消，`status == "cancelled"`，`next == ()` |
| 已经 31 秒 | 同样取消；取消后再次检查不提交 |
| 用户已修订成功 | 保持 `candidates_ready`，不被旧计时取消 |
| 用户修订后进入 `rr-v1` | `rr-v0` 的旧计时不能取消新一轮 |

本节示例在临时目录中使用项目现有依赖验证：**5 passed**。验证时另有项目 `model_provider.py` 导入依赖产生的弃用警告，与超时功能无关。演示脚本也已单独运行验证：约 5 秒后进入 `cancelled`，`snapshot.next == ()`。

### 12.6 面试需要掌握的关键点（先讲清基础即可）

**问 1：为什么不在 graph 节点里等待用户到超时？**

> interrupt 后当前调用已返回，Checkpoint 保存暂停状态。交互等待发生在两次调用之间，所以调用层检查期限，图只处理恢复决策。等待时间与超时动作属于产品策略。

**问 2：超时怎么真正改变 State？**

> 不是只显示“已超时”，也不是直接 update_state。调用层沿用原 thread_id，提交 Command(resume={interaction_id, action: "cancel"})；图校验决策后返回 cancelled 并走 END。

**问 3：为什么保存暂停时间，不能每次用当前时间作起点？**

> 每次重置起点会让任务永远不到期。同一轮错误重问不应自动续期；下一轮 interaction_id 改变时，才按本作业规则重新记录时间。

**问 4：这里如何避免重复取消？interaction_id 是幂等锁吗？**

> 串行调用下先查最新状态；已经结束就不再提交，版本 ID 不一致就不取消新一轮。interaction_id 主要是版本校验，不是分布式幂等锁。本作业不处理多进程同时检查或“检查后立刻有人回复”的竞争，面试不应宣称 exactly-once。

**问 5：用户等待超时和模型调用超时有什么区别？**

> 模型调用超时限制一次执行耗时；交互等待超时限制 interrupt 后用户多久没回复。给单次调用设置超时，不能替代本作业的跨调用等待检查。

**问 6：怎么测试时间相关逻辑？**

> 注入 now，不做真实等待，覆盖截止时间前、恰好截止时间、截止时间后，再覆盖已完成与旧版本两种保护情况。

### 12.7 作业交付清单与边界

完成以下内容就够了，不必继续搭基础设施：

- [ ] 能从首次暂停结果拿到 `interaction_id`，并记录一次 `paused_at`。
- [ ] 在调用层计算期限并周期检查，而不是在 graph 内等待。
- [ ] 到期使用同 Thread 的 `Command(resume=...)` 提交 `cancel`。
- [ ] 未到期、已结束、已进入新一轮都不误取消。
- [ ] 5 个测试通过，演示输出 `cancelled` 和 `()`。
- [ ] 能从技术、产品两个视角解释为什么超时属于调用层。

**边界说明：** 当前方案是单进程演示与串行检查，`InMemorySaver` 只在当前进程中保存 Checkpoint；进程退出后不保证恢复。这不妨碍完成本次作业。持久化存储、跨进程扫描与并发控制属于后续工程扩展，不是本节的前置任务。

---

## 13. 测试与验证：如何证明你真的学会了

### 13.1 定向测试

```powershell
cd D:\Code\K_Course\talent-eval-agents_learning\backend
uv run --no-sync pytest -q tests/test_talent_request_hitl.py
```

当前已有测试覆盖：

- 可执行计划直达 `candidates_ready`；
- 计划阻塞和空候选共用 `requirement_revision`；
- 自由文本修订后重新得到候选；
- 岗位画像冲突使用完整 JD 内容；
- 过期 `interaction_id` 重新提问；
- 空 `revision_text` 重新提问；
- `cancel` 进入终态。

### 13.2 验证脚本

```powershell
cd D:\Code\K_Course\talent-eval-agents_learning\backend
uv run --no-sync python scripts/verify_talent_hitl_resume.py
```

观察：

```text
[ready]
[interrupt]
[revise-resume]
[clarify-interrupt]
[stale-rejected]
[wait-inspect]
[cancel]
[state-history]
[time-travel-replay]
```

### 13.3 用断言思维读测试

| 断言 | 证明 |
|---|---|
| `status == candidates_ready` | 正常路径完成 |
| 无 `__interrupt__` | 没有无谓打断 |
| `candidates_empty` 后有 payload | 空集是业务状态 |
| `type == requirement_revision` | 统一协议 |
| `request_text` 等于完整文本 | 用户可编辑完整要求 |
| `interaction_id == rr-v0` | 版本化交互 |
| 旧 ID 返回 `resume_error` | 过期提交防护 |
| 错误后新 ID 成功 | Thread 仍可恢复 |
| `status == cancelled` | 取消是终态 |
| `next == ()` | 图已结束 |
| `lookup_calls == 1` | 不重复执行已完成岗位查询 |

### 13.4 手工调试要看什么

```python
snapshot = graph.get_state(config)
print(snapshot.values)
print(snapshot.next)
print(snapshot.created_at)
```

要能说清楚当前 `status`、`plan_version`、`condition_revisions`、`next` 节点、payload 的 `interaction_id` 和恢复后从哪里继续。

---

## 14. 常见错误和正确设计

### 错误一：所有阻塞都抛异常

业务可修订条件应使用 `interrupt`；数据库故障、模型服务不可用等系统问题才用异常。

### 错误二：空候选直接返回 `[]`

用户不知道哪个条件导致空集；应该设置 `candidates_empty` 并进入统一修订。

### 错误三：恢复时只传 `revision_text`

无法确认它属于哪个暂停点。正确协议是 `interaction_id + action + revision_text`。

### 错误四：恢复校验失败就让节点崩溃

应由 `_await_decision` 捕获 `ValueError`，把 `resume_error` 放回 interrupt payload，让用户在同一 Thread 修正。

### 错误五：只用最高匹配分自动选岗位

模糊岗位也可能得高分；要区分 `match_type` 和 `match_score`，唯一 exact 才自动确认。

### 错误六：修订时只 patch 旧 QueryPlan

旧 clarification、岗位上下文和语义条件可能残留。当前课程采用完整 `request_text` 重编译；生产化再增加结构化 patch 优化。

### 错误七：在 graph 节点里 `sleep` 等超时

占 worker、难扩展、和产品 SLA 耦合。正确做法是调用层保存 `paused_at`，scheduler 到期后提交合法 cancel。

### 错误八：把用户取消和次数耗尽都叫 `cancelled`

产品无法区分用户主动放弃和系统上限退出。建议使用 `cancelled`、`revision_limit_reached`、`expired` 等可观测终态。

### 错误九：计划未确认就启动 Subagent

不同分支可能使用不同解释，修订后旧结果难撤销。先完成第17课计划边界，再进入第18课评估主图。

---

## 15. 面试问题与高质量回答

### Q1：为什么不用普通函数返回“需要用户输入”，而要用 `interrupt`？

**答：** 普通函数只能把状态交给调用层；`interrupt` 配合 Checkpoint 能保存 State 和暂停位置，在未来同一 Thread 继续，不必重跑已完成节点。恢复值还可以在图内部做 action、版本和取消校验。

### Q2：`interaction_id` 和 `thread_id` 的区别？

**答：** `thread_id` 标识整个任务会话；`interaction_id` 标识该会话当前某一次暂停。一个 Thread 可以有 `rr-v0`、`rr-v1`。用版本派生 ID 可以拒绝旧页面提交，防止过期决策覆盖新状态。

### Q3：为什么空候选不是异常？

**答：** 查询语法和数据访问都成功，只是业务结果为空，是可预期、可修订的分支。系统错误才是异常；把空集建模成状态后，才能生成诊断并交互。

### Q4：为什么用户修改完整要求，而不是只提交 relaxation？

**答：** 第一版降低前端 DSL 复杂度，支持增加、删除、改写多个条件，并复用同一个自然语言编译入口。代价是可能语义漂移，生产化要保存原文版本、结构化 diff 和计划快照。

### Q5：旧 interaction ID 为什么不直接结束？

**答：** 对当前 Thread 来说，它是可修复的用户输入错误。带 `resume_error` 重新 interrupt 能让用户刷新后继续；HTTP 层可以返回 409/422，但图内状态仍应可恢复。

### Q6：超时为什么不放 graph？

**答：** 用户等待发生在两次图调用之间，图调用已返回；图内等待会占用 worker，并耦合产品 SLA。调用层记录暂停时间，scheduler 到期后用原 ID 提交 cancel，更适合分布式部署。

### Q7：多 Agent 后为什么还要计划确认？

**答：** 多 Agent 会放大输入不确定性。如果候选范围不稳定，不同 Subagent 会使用不同解释，结果不可比较。第17课先把自然语言编译成带版本的 QueryPlan，第18课再启动证据和评估分支。

---

## 16. 最终学习检查表

### 设计理解

- [ ] 我能解释第14课只做到哪里、第17课新增什么。
- [ ] 我能画出 `build_plan -> filter_candidates -> resolve_requirements`。
- [ ] 我能区分 `clarification_required` 和 `candidates_empty`。
- [ ] 我能说明为什么使用统一 resolver。
- [ ] 我能说明 `request_text`、`plan_version`、`condition_revisions` 的职责。

### 代码理解

- [ ] 我能从 `langgraph.json` 找到 `graph`。
- [ ] 我能解释图工厂如何通过依赖注入挂载候选筛选。
- [ ] 我能解释 `_await_decision` 为什么循环 interrupt。
- [ ] 我能解释恢复时为何复用同一个 `thread_id`。
- [ ] 我能说明岗位查询为什么不会因恢复重复执行。

### 作业能力

- [ ] 我能写最大修订次数的失败测试。
- [ ] 我能实现 `revision_limit_reached` 并保持取消语义独立。
- [ ] 我能在调用层记录 `paused_at` 和 `interaction_id`。
- [ ] 我能用可注入时钟测试超时。
- [ ] 我能说明 scheduler 的幂等条件更新。

### 面试能力

- [ ] 我能用“模型解释、程序控制、工具提供事实、调用层管理生命周期”概括架构。
- [ ] 我能解释为什么先确认 QueryPlan 再启动 Subagent。
- [ ] 我能区分单次执行超时和用户交互等待超时。
- [ ] 我能指出当前课程代码和生产化方案的差异。

---

## 17. 一页纸复习版

```text
第17课的核心不是暂停，而是“可恢复的决策写回”。

输入：用户原始要求 + 历次补充
  |
  v
receive_request
  - 校验 tenant / permissions / request_text
  - 初始化干净 State
  |
  v
interpret_input
  - 模型输出 TalentRequestDraft
  - 程序根据 input_mode 路由
  |
  v
build_plan
  - Draft -> TalentRequest + QueryPlan
  - clarifications 非空 => clarification_required
  |
  +--------------------------+
  |                          |
  v                          v
resolve_requirements    filter_candidates
  |                          |
  |                          +-- 有候选 -> candidates_ready
  |                          +-- 空候选 -> candidates_empty
  |                                      |
  +--------------------------------------+
                 |
                 v
         interrupt(payload)
         type=requirement_revision
         reason_code=plan_blocked / empty_candidates
         request_text=完整可编辑要求
         interaction_id=rr-v{plan_version}
                 |
       +---------+---------+
       |                   |
     cancel              revise
       |                   |
  cancelled              写回 request_text
       |                   |
      END          plan_version + 1
                            |
                            v
                    interpret_input 重跑

恢复防护：
- decision 必须是 dict
- interaction_id 必须匹配
- action 必须在白名单
- revision_text 必须非空
- 校验失败带 resume_error 重新 interrupt

生命周期：
- interrupt：状态暂停，不占进程
- cancel：显式终态
- timeout：调用层读取 paused_at，代提交 cancel
- max revisions：图内业务上限，返回 revision_limit_reached
```

如果你能独立讲清楚这一页，并写出两个作业的测试，你就不只是“看过 interrupt”，而是已经掌握了 Agent 系统里重要的**人机协同、状态恢复、版本校验和执行生命周期治理**。
