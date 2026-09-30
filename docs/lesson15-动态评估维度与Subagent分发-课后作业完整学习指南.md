# 第15课：动态评估维度与 Subagent 分发——完整学习与课后作业指南

> 读者：刚刚本科毕业、准备进入 Agent 开发方向的同学。  
> 依据代码：D:/Code/K_Course/talent-eval-agents_learning 当前工作树，HEAD 为提交 d868dda（分发 agent 和多要求并发处理）。  
> 关联提交：6bb1733、d586414、d868dda。  
> 整理日期：2026-09-29。  
> 本文目标：不只告诉你代码做了什么，还要让你能解释为什么这样设计、如何验证、出现问题怎样定位、下一步怎样演进。

---

## 一、先给结论：老师真正想让你学会什么

第 15 课表面上讲的是“动态评估维度”和“Subagent 分发”，本质上训练的是一套 Agent 工程方法：

> 把不稳定的自然语言要求转换成稳定的评估协议，再把评估协议转换成可并行、可追踪、可降级、可聚合的证据任务。

上一课已经把人才需求编译为两类中间表示：

- TalentRequest：保存用户原始意图、硬条件、语义条件和评估偏好；
- QueryPlan：保存可以交给检索系统执行的过滤条件。

本课继续做两步编译：

~~~text
TalentRequest.semantic_conditions
TalentRequest.evaluation_preferences
    │
    ▼
动态评估维度 EvaluationDimensionPlan
    │
    ├── definition：这个维度到底评估什么
    ├── weight_percent：它在最终评估中占多少权重
    ├── evidence_requirements：必须找哪些证据
    ├── retrieval_hints：检索时使用哪些关键词
    ├── score_anchors：未来怎样从 0/3/5 分解释事实
    └── source_requirement_ids：这个维度来自哪条原始要求
    │
    ▼
候选人 × 评估维度
    │
    ▼
AssessmentWorkItem
    │
    ▼
LangGraph Send 分发
    │
    ▼
Branch Worker 取证
    │
    ▼
Evidence Pack / BranchEvidenceDraft
    │
    ▼
branch_results reducer 聚合
~~~

### 1.1 本课不负责什么

当前课的输出是证据草稿，不是最终人才结论。

当前课暂不负责：

- 根据证据计算最终分数；
- 候选人排序；
- 生成最终推荐报告；
- 自动判断谁一定录用；
- 让每个分支自由发挥并自行改变业务协议。

这些工作应该留给后续课程。第 15 课的边界是：

~~~text
动态产生评估标准
    +
按候选人和评估维度并行取证
    +
保留可审计证据
    =
为后续评分和决策准备可靠输入
~~~

### 1.2 面试时如何概括本课

你可以这样回答：

> 我把硬条件留在 QueryPlan 中，用它确定候选人集合；把语义要求和评估偏好交给结构化模型生成动态评估维度；每个候选人和每个维度形成一个工作项，由 LangGraph Send 扇出到确定性的 Branch Worker。Branch Worker 只负责根据证据要求检索材料并生成 Evidence Pack，不直接评分或排序。多个分支结果通过 operator.add reducer 聚合，最终形成可追踪的证据草稿集合。

---

## 二、总体设计框架：从业务问题到代码模块

### 2.1 业务分层

第 15 课至少要分清下面五层：

| 层 | 负责的问题 | 当前课中的代表物 |
|---|---|---|
| 意图层 | 用户想找什么人才 | TalentRequest |
| 执行层 | 硬条件如何筛选人才 | QueryPlan、FilterCondition |
| 评估协议层 | 这个岗位应该从哪些角度评估 | EvaluationDimensionPlan |
| 证据执行层 | 每个候选人的每个维度如何取证 | AssessmentWorkItem、Branch Worker |
| 聚合输出层 | 并发分支如何回到主流程 | branch_results、operator.add |

一个常见错误是把所有事情都丢给一个大模型：“请找人、评估、排序并给出结论”。这样做的问题是：

1. 评估标准不可追踪；
2. 每次模型调用可能关注不同维度；
3. 证据和结论混在一起；
4. 很难知道某个候选人的某个维度为什么失败；
5. 无法稳定计算任务数量；
6. 并行失败时无法做局部降级；
7. 无法对租户、权限和工具范围做清晰控制。

本课的设计是把大问题拆成协议明确的小任务。

### 2.2 模块地图

当前实现主要位于以下绝对路径：

- D:/Code/K_Course/talent-eval-agents_learning/backend/app/talent_evaluation_dispatch.py
- D:/Code/K_Course/talent-eval-agents_learning/backend/app/talent_evaluation_runtime.py
- D:/Code/K_Course/talent-eval-agents_learning/backend/app/evidence_pack.py
- D:/Code/K_Course/talent-eval-agents_learning/backend/app/talent_tools.py
- D:/Code/K_Course/talent-eval-agents_learning/backend/scripts/verify_talent_evaluation_dispatch.py
- D:/Code/K_Course/talent-eval-agents_learning/backend/tests/test_talent_evaluation_dispatch.py

模块职责如下：

| 模块 | 入口/类型 | 输入 | 输出 | 设计重点 |
|---|---|---|---|---|
| 动态维度协议 | EvaluationDimension、EvaluationDimensionPlan | TalentRequest、QueryPlan | 评估维度计划 | 用结构化模型约束模型输出 |
| 维度生成 | build_structured_dimension_generator | semantic_conditions、evaluation_preferences | EvaluationDimensionPlan | 只让模型处理语义要求和偏好 |
| 维度校验 | validate_dimension_plan | 维度计划 | DimensionValidationIssue | Pydantic 做结构校验，函数做业务规则校验 |
| 候选人提供 | build_query_plan_candidate_provider | QueryPlan.filters | candidate_ids | 硬条件先执行，避免对无关人取证 |
| 任务矩阵 | _prepare_work_items | candidate_ids、dimensions | AssessmentWorkItem[] | 候选人和维度做笛卡尔积 |
| 并行分发 | build_work_item_sends | work_items | Send[] | 一项任务对应一个分支 |
| 分支执行 | _run_assessment_branch | work_item、Runtime Context | BranchEvidenceDraft | 处理错误并保持输出协议 |
| 证据工作器 | build_evidence_branch_worker | 一个工作项 | 多个 RequirementEvidence | 查询、工具调用、Evidence Pack 转换 |
| 证据协议 | build_evidence_packs | 检索命中、抽取器 | Evidence Pack 2.0 | 事实、来源、冲突、缺失信息可审计 |
| 运行时装配 | build_runtime_graph | 模型、Embedding、Rerank、DB、工具 | 编译图 | 把依赖注入图，而不是写死在节点里 |
| 验证脚本 | verify_talent_evaluation_dispatch | 真实服务和数据 | 日志、JSON、摘要 | 验证真实链路和分发数量 |

### 2.3 完整图结构

~~~text
START
  │
  ▼
receive_input
  │  校验 tenant_id、permission_scopes、talent_request、query_plan
  ▼
generate_dimensions
  │  semantic_conditions + evaluation_preferences → 评估维度
  ▼
validate_dimensions
  │  Pydantic 结构检查 + 权重合计检查
  ├─────────────── 有问题 ───────────────▶ dimension_invalid → END
  │
  ▼ 无问题
retrieve_candidates
  │  QueryPlan.filters → candidate_ids
  ▼
prepare_work_items
  │  candidate_ids × dimensions → work_items
  ├── 无候选人 ───────────────▶ no_candidates → END
  ├── 超过上限 ───────────────▶ capacity_exceeded → END
  │
  ▼
Send("run_assessment_branch", {"work_item": item})
  │
  ├── candidate A × dimension 1 → Branch Worker ─┐
  ├── candidate A × dimension 2 → Branch Worker ─┤
  ├── candidate B × dimension 1 → Branch Worker ─┤
  └── candidate B × dimension 2 → Branch Worker ─┘
                                                    │
                                                    ▼
                                      branch_results reducer 聚合
                                                    │
                                                    ▼
                                                 finalize
                                                    │
                                                    ▼
                                                   END
~~~

注意：图中的 Send 分支是外层并发；一个分支内部还会对多个 evidence_requirements 使用 ThreadPoolExecutor 做第二层并发。这也是本课最后一条回顾中的“两层并发”。

---

## 三、三个提交体现了什么设计演进

学习时不要只看最终代码，也要理解提交之间的方向变化。

### 3.1 6bb1733：先建立动态维度和分支 Agent

这个阶段的主要工作是：

- 引入动态评估维度协议；
- 用 Pydantic 表达维度、评分锚点、证据要求和检索提示；
- 建立评估分发图；
- 加入模型生成维度的能力；
- 增加验证脚本和测试；
- 页面开始补充知识库管理相关能力。

这个阶段回答的是：

> 动态评估维度应该长什么样？如何把一个候选人的维度任务交给 Agent？

早期设计更偏“每个分支运行一个 Agent”。这在概念上接近 Subagent，但自由度较高。

### 3.2 d586414：改进 Branch Worker 和证据链

这个阶段强化了：

- Evidence Pack 2.0；
- 工具调用审计；
- 可信权限上下文；
- 分支日志；
- 分支内多个证据要求的处理；
- 错误状态和降级状态；
- 真实模型抽取测试。

最重要的思想变化是：

> 分支不应该只返回“我认为这个候选人不错”，而应该返回可检查的证据结构。

### 3.3 d868dda：去掉不稳定的维度 ID，并加强并发模型

当前 HEAD 的关键变化包括：

1. 评估维度不再依赖模型生成的固定 dimension_id，而是使用稳定的 dimension_number。
2. 任务 ID 使用 candidate_id 和 dimension_number 组合，例如 C001:1。
3. 一个维度下的多个 evidence_requirements 使用 ThreadPoolExecutor 并发执行。
4. 外层使用 LangGraph Send 扇出候选人 × 维度工作项。
5. 每个分支结果通过 reducer 聚合。
6. 分支执行被做成确定性的 Branch Worker，而不是另外启动一个自由的 ReAct 循环。

这里的工程经验是：

- 对业务协议而言，模型生成的名字不一定适合当主键；
- 稳定编号和任务 ID 更容易做日志追踪、重试和聚合；
- 复杂任务可以有两层并发，但输出协议必须稳定；
- “使用 Subagent”不等于“让每个分支无限自由行动”。

---

## 四、动态评估维度：先理解数据协议

### 4.1 ScoreAnchor：评分锚点

当前代码中的评分锚点是：

~~~python
class ScoreAnchor(BaseModel):
    score: Literal[0, 3, 5]
    description: str = Field(min_length=1, max_length=300)
~~~

这表示模型只能返回 0、3、5 三个离散分数。

典型含义可以是：

| 分数 | 含义 | 示例 |
|---|---|---|
| 0 | 没有相关证据或明显不满足 | 没有找到 RAG 项目事实 |
| 3 | 有部分证据，但不完整或缺少量化结果 | 参与过项目，但职责和结果不清楚 |
| 5 | 有完整、可验证、与岗位强相关的交付证据 | 有明确项目、职责、技术方案和上线结果 |

注意：第 15 课只是定义锚点，当前不负责真正算分。锚点的作用是提前约束后续评分模块的解释空间。

### 4.2 EvaluationDimension：一个评估维度的完整协议

当前代码核心模型是：

~~~python
class EvaluationDimension(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    definition: str = Field(min_length=1, max_length=500)
    weight_percent: int = Field(ge=1, le=100)
    evidence_requirements: list[str] = Field(min_length=1, max_length=8)
    score_anchors: list[ScoreAnchor] = Field(min_length=3, max_length=3)
    retrieval_hints: list[str] = Field(min_length=1, max_length=8)
    source_requirement_ids: list[str] = Field(min_length=1, max_length=8)
~~~

逐字段解释：

#### name

人类可以阅读的维度名称，例如：

- RAG 项目落地能力；
- Agent 评测与质量保障；
- 协作与交付能力。

当前版本不要求模型返回固定格式的 dimension_id，因此不能把 name 直接当数据库主键。

#### definition

解释“这个维度评估的边界是什么”。

例如“RAG 项目落地能力”不能只写成“是否会 RAG”，更好的定义是：

> 候选人是否有将检索增强生成系统应用到真实业务场景，并完成数据、检索、生成、评测、上线或迭代闭环的可验证经验。

#### weight_percent

这个维度在未来总评分中的权重。当前约束为 1 到 100 的整数。

Pydantic 只能保证单个权重合法，不能保证所有维度合计为 100，所以还需要 validate_dimension_plan。

#### evidence_requirements

必须逐条寻找的证据要求。例如：

~~~text
- 是否真实参与过 RAG 项目
- 在项目中承担了哪些职责
- 是否有上线、质量指标或迭代结果
~~~

这些要求不是最终结论，而是 Branch Worker 的取证清单。

#### retrieval_hints

面向检索系统的关键词组合。例如：

~~~text
- RAG 知识库 检索增强 召回
- 上线 评测 命中率
- Embedding 向量 检索 重排
~~~

它们应尽量是可以直接用于候选人证据检索的词，而不是空泛的能力名。

#### source_requirement_ids

标明这个维度来自哪条原始语义要求或偏好：

~~~text
S1
S2
preference:1
preference:2
~~~

这样后续可以回答：

> 这个评估维度是用户提出的，还是模型自行增加的？

### 4.3 EvaluationDimensionPlan：模型输出的集合

~~~python
class EvaluationDimensionPlan(BaseModel):
    dimensions: list[EvaluationDimension] = Field(min_length=1, max_length=6)
~~~

这里限制最多 6 个维度，是容量和可解释性的双重保护：

- 维度过多，任务矩阵会快速膨胀；
- 每个维度又有多个 evidence_requirements；
- 维度过多也会让最终评分难以解释；
- 当前课只是生成评估框架，不需要无限细分。

### 4.4 为什么 hard_conditions 不能变成评分维度

动态维度提示词明确写了：

~~~text
hard_conditions 已用于候选人过滤，不得再生成评分维度。
~~~

例如：

~~~text
硬条件：region == 上海
语义条件：RAG 项目落地经验
偏好：协作与交付经验优先
~~~

正确设计：

- region == 上海：用于候选人过滤；
- RAG 项目落地经验：生成评估维度；
- 协作与交付经验优先：生成带偏好权重的评估维度。

错误设计：

- 生成“上海工作地点匹配度”评分维度。

因为硬条件已经是准入条件，不能再在评分阶段重复计算，否则会出现“先用硬条件筛一次、后面又给它加分”的双重计算。

---

## 五、动态维度生成：从模型调用到结构化结果

### 5.1 生成器入口

代码路径：

D:/Code/K_Course/talent-eval-agents_learning/backend/app/talent_evaluation_dispatch.py

核心函数：

~~~python
def build_structured_dimension_generator(
    model_provider: ModelProvider,
) -> DimensionGenerator:
    def generate(
        talent_request: dict[str, Any],
        query_plan: dict[str, Any],
    ) -> EvaluationDimensionPlan:
        model = model_provider()
        if model is None:
            raise RuntimeError("评估维度生成模型未配置")
        payload = {
            "talent_request": talent_request,
            "query_plan": query_plan,
        }
        result = model.with_structured_output(EvaluationDimensionPlan).invoke(
            [
                ("system", DIMENSION_GENERATOR_PROMPT),
                ("user", json.dumps(payload, ensure_ascii=False)),
            ]
        )
        return result

    return generate
~~~

这段代码可以拆成六步：

1. 外部传入 model_provider，而不是在函数内部偷偷初始化模型。
2. 调用时取得模型。
3. 检查模型是否配置。
4. 把 TalentRequest 和 QueryPlan 放入 payload。
5. 用 with_structured_output(EvaluationDimensionPlan) 限定返回协议。
6. 返回 Pydantic 对象，而不是不受控的字符串。

### 5.2 为什么要用结构化输出

如果直接调用普通模型：

~~~python
text = model.invoke(prompt)
~~~

你还要自己处理：

- 模型返回 Markdown 还是 JSON；
- JSON 是否缺字段；
- weight_percent 是整数还是字符串；
- score 是否返回 1、2、4；
- 是否多生成了无关字段；
- evidence_requirements 是否为空。

结构化输出将部分责任前移到协议层：

~~~python
model.with_structured_output(EvaluationDimensionPlan)
~~~

但是要注意：结构化输出不是业务正确性的全部保证。模型可能生成一个“格式正确但语义重复”的维度，因此仍然需要额外的业务校验。

### 5.3 动态维度提示词的关键约束

当前提示词表达了这些规则：

1. 只根据 semantic_conditions 和 evaluation_preferences 生成维度；
2. hard_conditions 只用于候选人过滤；
3. 来源 ID 使用原 requirement_id；
4. 偏好来源使用 preference:1、preference:2；
5. 维度之间不得重复；
6. 必须能通过候选人档案或原始材料取证；
7. 权重是整数百分比且合计 100；
8. 分数固定为 0、3、5；
9. retrieval_hints 要能用于候选人证据检索。

提示词是“软规则”，Pydantic 和 validate_dimension_plan 是“硬规则”。好的 Agent 工程通常是：

~~~text
Prompt 负责告诉模型应该怎么做
Schema 负责保证输出长什么样
业务校验负责拒绝格式正确但业务错误的结果
~~~

---

## 六、维度校验：Pydantic 能做什么，不能做什么

### 6.1 当前实现

当前校验函数是：

~~~python
def validate_dimension_plan(
    plan: EvaluationDimensionPlan,
) -> list[DimensionValidationIssue]:
    total = sum(item.weight_percent for item in plan.dimensions)
    if total == 100:
        return []
    return [
        DimensionValidationIssue(
            code="weight_total_invalid",
            message=f"维度权重合计必须为 100，当前为 {total}",
        )
    ]
~~~

当前做法只做一条额外业务规则：所有权重合计必须为 100。

### 6.2 Pydantic 已经承担的检查

下面这些由模型字段完成：

- 维度数量至少 1 个、最多 6 个；
- name 非空且不超过 80 字符；
- definition 非空且不超过 500 字符；
- 单个权重在 1 到 100 之间；
- evidence_requirements 至少一条、最多八条；
- score_anchors 恰好三条；
- retrieval_hints 至少一条、最多八条；
- source_requirement_ids 至少一条、最多八条；
- score 只能是 0、3、5。

### 6.3 Pydantic 不能自动判断的内容

这些需要额外写规则：

- 维度名称是否重复；
- evidence_requirements 是否重复；
- retrieval_hints 是否过于泛化；
- score_anchors 是否确实覆盖 0、3、5；
- source_requirement_ids 是否来自输入；
- 所有必需语义要求是否至少被一个维度覆盖；
- 一个偏好是否被遗漏；
- 不同维度定义是否高度重叠；
- 权重是否过度倾斜；
- 维度是否能在当前证据源中实际取证；
- 维度是否把硬条件重复算入评分。

### 6.4 推荐的增强版校验

作业一可以先增加以下规则：

~~~python
def validate_dimension_plan(
    plan: EvaluationDimensionPlan,
    *,
    source_requirement_ids: set[str] | None = None,
    required_requirement_ids: set[str] | None = None,
) -> list[DimensionValidationIssue]:
    issues: list[DimensionValidationIssue] = []

    total = sum(item.weight_percent for item in plan.dimensions)
    if total != 100:
        issues.append(
            DimensionValidationIssue(
                code="weight_total_invalid",
                message=f"维度权重合计必须为 100，当前为 {total}",
            )
        )

    names: dict[str, list[int]] = {}
    for number, dimension in enumerate(plan.dimensions, start=1):
        normalized_name = dimension.name.strip().lower()
        names.setdefault(normalized_name, []).append(number)

        scores = {anchor.score for anchor in dimension.score_anchors}
        if scores != {0, 3, 5}:
            issues.append(
                DimensionValidationIssue(
                    code="score_anchor_set_invalid",
                    message=f"第 {number} 个维度必须包含 0、3、5 三个评分锚点",
                    dimension_numbers=[number],
                )
            )

        if len(set(dimension.evidence_requirements)) != len(
            dimension.evidence_requirements
        ):
            issues.append(
                DimensionValidationIssue(
                    code="evidence_requirement_duplicated",
                    message=f"第 {number} 个维度存在重复证据要求",
                    dimension_numbers=[number],
                )
            )

        if source_requirement_ids is not None:
            unknown = set(dimension.source_requirement_ids) - source_requirement_ids
            if unknown:
                issues.append(
                    DimensionValidationIssue(
                        code="source_requirement_unknown",
                        message=f"第 {number} 个维度引用了不存在的来源：{sorted(unknown)}",
                        dimension_numbers=[number],
                    )
                )

    for normalized_name, numbers in names.items():
        if len(numbers) > 1:
            issues.append(
                DimensionValidationIssue(
                    code="dimension_name_duplicated",
                    message=f"维度名称重复：{normalized_name}",
                    dimension_numbers=numbers,
                )
            )

    if required_requirement_ids is not None:
        covered = {
            requirement_id
            for dimension in plan.dimensions
            for requirement_id in dimension.source_requirement_ids
        }
        missing = required_requirement_ids - covered
        if missing:
            issues.append(
                DimensionValidationIssue(
                    code="required_requirement_uncovered",
                    message=f"以下原始要求没有被任何维度覆盖：{sorted(missing)}",
                )
            )

    return issues
~~~

这段增强版还没有处理语义相似度，因为那需要模型或向量相似度服务。学习时应先把确定性规则写好，再考虑语义去重。

### 6.5 为什么校验失败要终止分发

图中 validate_dimensions 后面有条件边：

~~~python
def _route_dimension_validation(state):
    if state.get("validation_issues"):
        return "dimension_invalid"
    return "retrieve_candidates"
~~~

如果评估维度无效，不能继续生成任务矩阵。否则会造成：

- 不完整的评估标准被放大到大量候选人；
- 任务和模型调用已经产生费用；
- 最后报告无法解释；
- 失败发生在很后面，排查成本更高。

这是“先验证协议，再大规模并发”的典型 Agent 编排模式。

---

## 七、从运行时入口开始追踪后端调用链

### 7.1 最外层入口

代码路径：

D:/Code/K_Course/talent-eval-agents_learning/backend/app/talent_evaluation_runtime.py

当前文件最后有：

~~~python
# Agent Server 通过 langgraph.json 加载这个编译图
graph = build_runtime_graph()
~~~

配置文件：

D:/Code/K_Course/talent-eval-agents_learning/backend/langgraph.json

其中当前图配置为：

~~~json
{
  "dependencies": ["."],
  "graphs": {
    "talent_request": "./app/talent_request_graph.py:graph",
    "talent_evaluation_dispatch": "./app/talent_evaluation_runtime.py:graph"
  },
  "env": "../.env",
  "python_version": "3.12"
}
~~~

这说明第 15 课已经有独立的 talent_evaluation_dispatch 图注册入口。

### 7.2 build_runtime_graph 的装配顺序

核心装配关系：

~~~python
def build_runtime_graph():
    model = get_chat_model(temperature=0)
    embedder = get_embedding_model()
    reranker = get_reranker()
    store = get_evidence_store()

    service = TalentToolService(
        session_factory=SessionLocal,
        evidence_provider=build_branch_evidence_provider(
            search=search,
            load_sources=load_sources,
            extract=model_extractor(model),
        ),
    )

    runtime_graph = build_talent_evaluation_dispatch_graph(
        dimension_generator=build_structured_dimension_generator(lambda: model),
        candidate_provider=build_query_plan_candidate_provider(service),
        branch_worker=build_evidence_branch_worker(
            service,
            ToolExecutor(...),
        ),
        max_work_items=24,
    )
    return runtime_graph
~~~

这里的设计叫依赖注入：

- 图只知道自己需要 DimensionGenerator、CandidateProvider、BranchWorker；
- 具体使用什么模型、什么数据库、什么检索服务，由 runtime 负责装配；
- 单元测试可以传入 fake generator、fake candidate provider、fake branch worker；
- 真实运行时可以换成数据库、Milvus、Embedding、Rerank 和真实模型。

这是 Agent 系统从能跑 Demo 走向可测试服务的关键。

### 7.3 模型为什么使用 temperature=0

动态维度生成关系到评估协议。使用低随机性有三个目的：

1. 相同输入更容易得到相近维度；
2. 测试和排错更稳定；
3. 评估协议比创意写作更需要一致性。

但 temperature=0 并不等于绝对确定性。模型、服务端采样实现、上下文和输入变化仍可能影响输出，所以必须依靠 schema、业务校验和日志，而不能只相信温度参数。

---

## 八、逐节点理解 LangGraph 主图

### 8.1 State：主图中保存什么

当前状态类型：

~~~python
class TalentEvaluationDispatchState(TypedDict, total=False):
    talent_request: dict[str, Any]
    query_plan: dict[str, Any]
    dimensions: list[dict[str, Any]]
    candidate_ids: list[str]
    work_items: list[dict[str, Any]]
    branch_results: Annotated[list[dict[str, Any]], operator.add]
    validation_issues: list[dict[str, Any]]
    required_work_items: int
    work_item_limit: int
    status: str
    errors: list[str]
~~~

特别注意这一行：

~~~python
branch_results: Annotated[list[dict[str, Any]], operator.add]
~~~

它告诉 LangGraph：当多个分支同时返回 branch_results 时，不是后写覆盖先写，而是用 operator.add 追加合并。

### 8.2 Runtime Context：不能把租户和权限放进用户输入

图使用：

~~~python
StateGraph(
    TalentEvaluationDispatchState,
    context_schema=DecisionContext,
)
~~~

DecisionContext 来自：

D:/Code/K_Course/talent-eval-agents_learning/backend/app/talent_decision_graph.py

它至少承载：

- tenant_id；
- permission_scopes。

这些值应由可信服务上下文提供，不应该由用户在 prompt 中自行填写。这样可以避免用户通过自然语言伪造租户或权限。

### 8.3 receive_input：建立安全边界

代码逻辑：

~~~python
def _receive_input(state, runtime):
    if not runtime.context.tenant_id.strip():
        raise ValueError("Runtime Context 中的 tenant_id 不能为空")
    if not runtime.context.permission_scopes:
        raise ValueError("Runtime Context 中的 permission_scopes 不能为空")
    if not state.get("talent_request"):
        raise ValueError("talent_request 不能为空")
    if not state.get("query_plan"):
        raise ValueError("query_plan 不能为空")
    return {
        "dimensions": [],
        "candidate_ids": [],
        "work_items": [],
        "branch_results": [],
        "validation_issues": [],
        "required_work_items": 0,
        "work_item_limit": 0,
        "status": "received",
        "errors": [],
    }
~~~

它完成两件事：

1. 检查本次运行的身份和输入；
2. 清空本轮中间字段，避免旧结果污染新运行。

### 8.4 generate_dimensions：自然语言要求到评估协议

~~~python
def generate_dimensions(state):
    plan = dimension_generator(
        state["talent_request"],
        state["query_plan"],
    )
    return {
        "dimensions": [
            item.model_dump(mode="json")
            for item in plan.dimensions
        ],
        "status": "dimensions_generated",
    }
~~~

图状态使用 dict，生成器内部使用 Pydantic 模型。这个边界很实用：

- Pydantic 负责验证和开发期类型；
- State 使用可序列化的 JSON 结构，方便 Checkpoint、日志和 API；
- 节点之间不依赖模型对象的 Python 内存身份。

### 8.5 validate_dimensions：小成本阻断大错误

~~~python
def _validate_dimensions(state):
    plan = EvaluationDimensionPlan(dimensions=state["dimensions"])
    issues = validate_dimension_plan(plan)
    return {
        "validation_issues": [item.model_dump(mode="json") for item in issues],
        "status": "dimensions_valid" if not issues else "dimension_invalid",
    }
~~~

这里又重新构造 EvaluationDimensionPlan，是因为上一个节点写入 State 后已经是 JSON 字典，需要重新回到协议对象进行校验。

### 8.6 retrieve_candidates：只根据 QueryPlan 的硬条件筛人

候选人提供器：

~~~python
def build_query_plan_candidate_provider(service):
    def candidate_provider(query_plan, context):
        filters = [
            FilterCondition.model_validate(item)
            for item in query_plan.get("filters", [])
        ]
        return service.filter_candidates(
            filters,
            context=TalentToolContext(
                tenant_id=context.tenant_id,
                permission_scopes=tuple(context.permission_scopes),
                actor_id="evaluation-dispatch",
                run_id="lesson-15",
            ),
        )
    return candidate_provider
~~~

调用链是：

~~~text
QueryPlan.filters
    → FilterCondition.model_validate
    → TalentToolService.filter_candidates
    → 数据库按 tenant_id 和硬条件过滤
    → candidate_ids
~~~

这里不能让模型重新生成 SQL，也不应该把 semantic_conditions 直接当 SQL 条件。语义条件还没有被标准化成数据库字段，它们在后面的动态评估维度和证据检索阶段处理。

### 8.7 prepare_work_items：任务矩阵的核心

当前核心代码：

~~~python
required_work_items = len(state["candidate_ids"]) * len(dimensions)

numbered_dimensions = list(enumerate(dimensions, start=1))
work_items = [
    AssessmentWorkItem(
        task_id=f"{candidate_id}:{dimension_number}",
        candidate_id=candidate_id,
        dimension_number=dimension_number,
        dimension=dimension,
    ).model_dump(mode="json")
    for candidate_id in state["candidate_ids"]
    for dimension_number, dimension in numbered_dimensions
]
~~~

这就是笛卡尔积：

~~~text
候选人集合 × 维度集合 = 评估工作项集合
~~~

例如：

~~~text
candidate_ids = [C001, C002]
dimensions = [维度1, 维度2]

C001:1
C001:2
C002:1
C002:2
~~~

这里的 task_id 是本课非常重要的设计：

- 能够定位具体候选人；
- 能够定位具体维度；
- 能够作为工具调用 run_id；
- 能够出现在日志中；
- 可以在未来用于重试和幂等判断。

### 8.8 容量保护

当前 runtime 设置：

~~~python
max_work_items=24
~~~

当：

~~~text
候选人数 × 维度数量 > 24
~~~

图不会继续分发，而是返回 capacity_exceeded。

这个保护不能只看候选人数量，因为真正的成本单位是任务矩阵。例如：

- 10 个候选人 × 2 个维度 = 20 个任务，可以执行；
- 5 个候选人 × 6 个维度 = 30 个任务，超过上限；
- 即使候选人数量不多，维度过多也可能超限。

### 8.9 _dispatch_work_items：决定是否 Send

~~~python
def _dispatch_work_items(state):
    if state.get("status") == "capacity_exceeded":
        return "capacity_exceeded"
    if not state.get("work_items"):
        return "no_candidates"
    return build_work_item_sends(state["work_items"])
~~~

返回字符串表示去固定节点；返回 Send 列表表示为每个工作项创建分支。

---

## 九、LangGraph Send：为什么它适合候选人 × 维度任务

### 9.1 Send 的核心形式

当前代码：

~~~python
def build_work_item_sends(work_items):
    return [
        Send("run_assessment_branch", {"work_item": item})
        for item in work_items
    ]
~~~

Send 的语义不是简单调用函数，而是告诉 LangGraph：

> 请把每一个工作项作为独立输入，发送到同一个分支节点 run_assessment_branch。

每个分支输入都只有自己的工作项：

~~~json
{
  "work_item": {
    "task_id": "C001:1",
    "candidate_id": "C001",
    "dimension_number": 1,
    "dimension": {
      "name": "RAG 项目落地能力",
      "weight_percent": 60
    }
  }
}
~~~

### 9.2 为什么不直接在一个节点 for 循环

也可以写成：

~~~python
for item in work_items:
    result = branch_worker(item, context)
~~~

但这种方式有明显问题：

- 任务天然串行；
- 一项失败可能影响后面任务；
- LangGraph 看不到每个分支；
- 难以做并行、超时和单项追踪；
- UI 和运行历史难以展示分支状态。

Send 把任务拆成图级别的分支，适合动态数量的 fan-out。

### 9.3 Send 不等于自动保证业务正确

Send 只负责分发。以下责任仍由应用自己承担：

- work_item 是否完整；
- candidate_id 是否属于当前租户；
- dimension_number 是否从 1 开始；
- 分支输出是否符合 BranchEvidenceDraft；
- 失败分支是否隔离；
- 是否需要限制总任务数。

所以当前代码在进入 Send 前先做 Pydantic 校验、候选人筛选和容量保护。

---

### 9.4 Send 发出之后：分支如何对接并继续执行

前面看到：

~~~python
def build_work_item_sends(work_items):
    return [
        Send("run_assessment_branch", {"work_item": item})
        for item in work_items
    ]
~~~

很多初学者会疑惑：这行代码只是构造了 `Send`，它并没有显式调用 `run_assessment_branch`，那后续到底是谁来调用分支？答案是：**`Send` 返回的是 LangGraph 的动态分支调度指令，由 LangGraph 运行时根据目标节点名继续执行，而不是 Python 立即调用函数。**

完整链路可以先记成：

~~~text
_prepare_work_items
    ↓
_dispatch_work_items
    ↓
build_work_item_sends(work_items)
    ↓
[Send("run_assessment_branch", {"work_item": item1}),
 Send("run_assessment_branch", {"work_item": item2}), ...]
    ↓
LangGraph 根据目标节点名创建并调度多个分支
    ↓
每个分支进入 run_assessment_branch
    ↓
AssessmentWorkItem.model_validate(state["work_item"])
    ↓
branch_worker(work_item, runtime.context)
    ↓
得到一个 BranchEvidenceDraft
    ↓
返回 {"branch_results": [draft]}
    ↓
operator.add reducer 聚合所有分支结果
    ↓
finalize
~~~

#### 9.4.1 第一步：条件边返回 Send 列表

主图中先注册节点：

~~~python
builder.add_node("prepare_work_items", _prepare_work_items(max_work_items))
builder.add_node("run_assessment_branch", _run_assessment_branch(branch_worker))
builder.add_node("finalize", _finalize)
~~~

然后把 `prepare_work_items` 的条件边连接到分发函数：

~~~python
builder.add_conditional_edges(
    "prepare_work_items",
    _dispatch_work_items,
    ["run_assessment_branch", "no_candidates", "capacity_exceeded"],
)

builder.add_edge("run_assessment_branch", "finalize")
~~~

因此，图运行到 `prepare_work_items` 后，会调用：

~~~python
def _dispatch_work_items(state):
    if state.get("status") == "capacity_exceeded":
        return "capacity_exceeded"
    if not state.get("work_items"):
        return "no_candidates"
    return build_work_item_sends(state["work_items"])
~~~

正常情况下，`_dispatch_work_items` 不返回普通的节点名字符串，而是返回一个 `Send` 列表。LangGraph 看到这个返回值后，就知道这是动态 fan-out：同一个目标节点要被执行多次，每次使用一个不同的局部输入。

这里的几个对象要严格区分：

| 对象 | 作用 |
|---|---|
| `Send` | 动态分支调度指令，不是业务函数调用 |
| `"run_assessment_branch"` | 目标节点名，必须和 `builder.add_node(...)` 注册的名字一致 |
| `{"work_item": item}` | 该分支收到的局部 state 输入 |
| `_run_assessment_branch(branch_worker)` | 节点工厂，负责构造真正的节点函数 |
| `run_assessment_branch(state, runtime)` | 每一个分支实际执行的节点函数 |
| `branch_worker(work_item, context)` | 基本业务层执行逻辑，负责检索证据并生成草稿 |

> 注意：`Send("run_assessment_branch", {"work_item": item})` 不会直接执行 `branch_worker`。它只是把“把这个工作项送到哪个节点”描述给 LangGraph；真正的函数调用发生在 LangGraph 进入目标节点以后。

#### 9.4.2 第二步：一个 Send 对应一个分支输入

假设当前有 2 名候选人、2 个动态评估维度，任务矩阵为：

~~~text
C001 × 维度 1 → task_id=C001:1
C001 × 维度 2 → task_id=C001:2
C002 × 维度 1 → task_id=C002:1
C002 × 维度 2 → task_id=C002:2
~~~

那么 `work_items` 有 4 个元素，`build_work_item_sends(work_items)` 返回 4 个 `Send`：

~~~python
[
    Send("run_assessment_branch", {"work_item": item_c001_d1}),
    Send("run_assessment_branch", {"work_item": item_c001_d2}),
    Send("run_assessment_branch", {"work_item": item_c002_d1}),
    Send("run_assessment_branch", {"work_item": item_c002_d2}),
]
~~~

这不代表 `build_work_item_sends` 在自己的 `for` 循环里已经执行了 4 次评估。这个 `for` 循环只是在构造 4 条调度指令。之后由 LangGraph 运行时分别启动 4 次 `run_assessment_branch` 节点执行。

每个分支看到的局部 state 类似：

~~~python
{
    "work_item": {
        "task_id": "C001:1",
        "candidate_id": "C001",
        "dimension_number": 1,
        "dimension": {
            "name": "RAG 项目落地能力",
            "weight_percent": 60,
        },
    }
}
~~~

分支之间不会共享这个局部 `work_item`。它们共享的是主图协议中允许被 reducer 合并的字段，例如 `branch_results`；这也是为什么每个分支只返回自己的一个结果，而不是直接修改一个全局 Python 列表。

#### 9.4.3 第三步：目标节点如何接住 Send

目标节点不是手写成一个接收 `item` 参数的函数，而是通过节点工厂生成：

~~~python
def _run_assessment_branch(branch_worker):
    def run_assessment_branch(state, runtime):
        work_item = AssessmentWorkItem.model_validate(state["work_item"])

        try:
            result = branch_worker(work_item, runtime.context)
        except TimeoutError as exc:
            result = BranchEvidenceDraft(
                task_id=work_item.task_id,
                candidate_id=work_item.candidate_id,
                dimension_number=work_item.dimension_number,
                execution_status="failed",
                error_code="branch_timeout",
                error_message=str(exc),
            )
        except BranchExecutionError as exc:
            result = BranchEvidenceDraft(
                task_id=work_item.task_id,
                candidate_id=work_item.candidate_id,
                dimension_number=work_item.dimension_number,
                execution_status="failed",
                error_code=exc.code,
                error_message=str(exc),
            )
        except Exception:
            raise

        return {
            "branch_results": [result.model_dump(mode="json")]
        }

    return run_assessment_branch
~~~

这里发生了四件事：

1. LangGraph 根据 `Send` 中的目标名找到已经注册的 `run_assessment_branch` 节点；
2. 把 `{"work_item": item}` 作为该次分支的 `state`；
3. 节点用 `AssessmentWorkItem.model_validate(...)` 把字典重新校验并还原成领域对象；
4. 节点调用 `branch_worker(work_item, runtime.context)`，得到这个候选人和这个维度对应的一个 `BranchEvidenceDraft`。

因此，Send 后面的“对接点”就是目标节点的函数签名：

~~~python
def run_assessment_branch(
    state: dict[str, Any],
    runtime: Runtime[DecisionContext],
) -> dict[str, Any]:
    ...
~~~

Send 里第二个参数的字段名是 `work_item`，所以节点通过 `state["work_item"]` 取出它。字段名如果不一致，例如 Send 传的是 `{"item": item}`，而节点仍然读取 `state["work_item"]`，就会在运行时出现输入缺失错误。

#### 9.4.4 第四步：为什么每个分支只返回一个列表元素

分支节点返回：

~~~python
{
    "branch_results": [
        result.model_dump(mode="json")
    ]
}
~~~

这里的列表不是多余包装，而是为了和 reducer 配合。每个分支只负责生产一个结果，所以它返回长度为 1 的列表：

~~~text
分支 C001:1 → {"branch_results": [result_C001_1]}
分支 C001:2 → {"branch_results": [result_C001_2]}
分支 C002:1 → {"branch_results": [result_C002_1]}
分支 C002:2 → {"branch_results": [result_C002_2]}
~~~

如果 `branch_results` 的 reducer 配置为 `operator.add`，LangGraph 会把这些列表合并：

~~~text
[result_C001_1]
+ [result_C001_2]
+ [result_C002_1]
+ [result_C002_2]
= [result_C001_1, result_C001_2, result_C002_1, result_C002_2]
~~~

这就是 reducer 的核心作用：每个并行分支返回局部结果，主图负责按字段规则聚合结果。分支不应该通过修改共享变量来“抢写”聚合列表，因为那会引入竞态条件，也绕过 LangGraph 的状态协议。

#### 9.4.5 第五步：分支异常如何与主流程对接

当前包装节点对异常做了分层处理：

- `TimeoutError`：转换为 `execution_status="failed"`、`error_code="branch_timeout"` 的结构化结果；
- `BranchExecutionError`：使用异常携带的 `code` 生成结构化失败结果；
- 其他未知异常：继续抛出，让 LangGraph 感知为真正的未处理错误。

这样设计的含义是：**已知、可预期的单分支失败被收敛为业务结果；未知编程错误不被静默吞掉。**

例如 4 个分支中有 1 个超时，聚合后仍然可以得到 4 个 `branch_results`，其中 1 个的状态是 `failed`。这使得系统能够“部分完成并报告失败”，而不是因为一个候选人的一个维度失败就丢失全部结果。

#### 9.4.6 第六步：所有分支完成后进入 finalize

图中有：

~~~python
builder.add_edge("run_assessment_branch", "finalize")
~~~

它的语义是：分支结果完成后进入后续的 `finalize` 阶段；在 LangGraph 的状态汇聚机制下，`finalize` 读取的是聚合后的 `branch_results`。`finalize` 会检查聚合后的结果：

~~~python
def _finalize(state):
    has_failures = any(
        item["execution_status"] == "failed"
        for item in state.get("branch_results", [])
    )

    status = (
        "branches_ready_with_failures"
        if has_failures
        else "branches_ready"
    )

    return {"status": status}
~~~

因此最终状态有两种典型情况：

~~~text
所有分支 execution_status 都不是 failed
    → status = "branches_ready"

至少一个分支 execution_status == failed
    → status = "branches_ready_with_failures"
~~~

#### 9.4.7 这条链路体现的两层并发

本课不是只有一层并发，而是两层：

~~~text
外层并发：候选人 × 评估维度
    └─ LangGraph Send
       ├─ C001 × 维度 1
       ├─ C001 × 维度 2
       ├─ C002 × 维度 1
       └─ C002 × 维度 2

内层并发：一个维度内的多条 evidence_requirements
    └─ Branch Worker 内部 ThreadPoolExecutor
       ├─ requirement 1
       ├─ requirement 2
       └─ requirement 3
~~~

外层由 LangGraph 管理任务分发、节点生命周期和状态聚合；内层由 Branch Worker 管理证据要求的检索并发、顺序恢复和 Evidence Pack 组装。两层职责不同，不能把 `Send` 误认为会自动替代 Branch Worker 内的线程池，也不能把线程池误认为图级别的分支编排。

#### 9.4.8 学习时的调试方法：沿着四个观察点追踪

出现“Send 发出后不知道去哪了”的问题时，可以按下面顺序排查：

1. **看返回值**：确认 `_dispatch_work_items` 是否返回了 `Send` 列表，列表长度是否等于任务矩阵数量；
2. **看目标节点注册**：确认 `"run_assessment_branch"` 是否和 `builder.add_node(...)` 中的名字完全一致；
3. **看分支输入**：确认节点读取的 `state["work_item"]` 和 Send 写入的字段名一致，并能通过 `AssessmentWorkItem.model_validate`；
4. **看聚合结果**：确认每个分支都返回 `{"branch_results": [one_result]}`，并确认 `branch_results` 的 reducer 是追加/合并，而不是覆盖。

可以用这个不变量检查任务矩阵是否完整：

~~~text
expected_branch_count = candidate_count × dimension_count
actual_branch_count = len(branch_results)
~~~

在没有重试和重复投递的理想链路中，两者应当相等。若不相等，应进一步检查候选人数量、维度数量、容量上限、分支失败策略和是否存在重复 task_id。

这一节最重要的理解是：**`Send` 只是把“要执行什么任务、送到哪个节点”交给 LangGraph；真正的业务执行发生在目标节点，结果通过 state reducer 回到主图，最后由 `finalize` 汇总状态。**

---
## 十、Branch Worker：确定性的分支执行单元

### 10.1 分支入口

代码路径：

D:/Code/K_Course/talent-eval-agents_learning/backend/app/talent_evaluation_dispatch.py

入口包装函数：

~~~python
def _run_assessment_branch(branch_worker):
    def run_assessment_branch(state, runtime):
        work_item = AssessmentWorkItem.model_validate(state["work_item"])
        result = branch_worker(work_item, runtime.context)
        return {
            "branch_results": [
                result.model_dump(mode="json")
            ]
        }
    return run_assessment_branch
~~~

它完成三个动作：

1. 把 Send 传来的字典还原为 AssessmentWorkItem；
2. 调用确定性的 branch_worker；
3. 把一个结果包装成长度为 1 的 branch_results 列表，让 reducer 追加。

### 10.2 分支为什么要重新验证输入

即使上游已经生成过工作项，分支入口仍然应该再次验证：

- 分支可能被单独调用；
- 未来可能从队列恢复；
- 外部状态可能被修改；
- Pydantic 验证是低成本安全边界。

### 10.3 BranchEvidenceDraft：分支输出协议

~~~python
class BranchEvidenceDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    candidate_id: str
    dimension_number: int = Field(ge=1)
    execution_status: Literal["succeeded", "degraded", "failed"]
    requirements: list[RequirementEvidence] = Field(default_factory=list)
    tool_call_ids: list[str] = Field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None
~~~

extra="forbid" 很重要。它拒绝未定义字段，避免分支偷偷返回一些主图没有协议化的字段，例如：

- 未定义的 evidence_refs；
- 未定义的 missing_items；
- 未定义的自由文本 summary；
- 未定义的 score。

当前课有意让输出严格保持在证据草稿范围。

### 10.4 分支状态和证据状态要分开

BranchEvidenceDraft 的 execution_status 描述分支执行过程：

- succeeded：工具调用和分支处理完成；
- degraded：部分证据要求失败或工具降级，但分支仍返回结构化结果；
- failed：分支级别失败，例如超时或明确的执行异常。

RequirementEvidence 的 status 描述某项证据：

- sufficient：证据充分；
- partial：证据部分满足；
- missing：没有可用证据；
- conflicting：证据之间冲突。

例如：

~~~text
execution_status = succeeded
requirement.status = missing
~~~

这完全合理：分支成功执行了检索，但检索没有找到候选人的相关事实。

反过来：

~~~text
execution_status = degraded
requirement.status = sufficient
~~~

也可能合理：某一条要求拿到了证据，另一条要求因工具暂时失败而降级。

---

## 十一、Branch Worker 内部：从证据要求到 Evidence Pack

### 11.1 建立可信工具上下文

当前分支先构造：

~~~python
tool_context = TalentToolContext(
    tenant_id=context.tenant_id,
    permission_scopes=tuple(context.permission_scopes),
    actor_id="evaluation-branch-worker",
    run_id=work_item.task_id,
)
~~~

这一步把主图的可信 DecisionContext 传给工具层，并为每个工作项建立独立 run_id。

它的价值：

- 数据查询按租户隔离；
- 证据检索按权限范围过滤；
- 日志可以定位到具体候选人和维度；
- 工具调用可以审计；
- 后续重试时可以识别同一个任务。

### 11.2 组合分支检索查询

查询函数：

~~~python
def _branch_query(dimension, requirement):
    terms = list(dict.fromkeys([
        *dimension.retrieval_hints,
        requirement,
    ]))
    return " ".join(terms)[:500]
~~~

这里有三个细节：

1. 先放 retrieval_hints，再放当前 requirement；
2. 使用 dict.fromkeys 去重并保持顺序；
3. 最终限制为 500 字符。

示例：

~~~text
retrieval_hints = ["RAG 知识库", "召回率", "上线"]
requirement = "候选人是否负责过检索和重排方案"

最终 query：
RAG 知识库 召回率 上线 候选人是否负责过检索和重排方案
~~~

这就是本课回顾中的：

> retrieval_hints 与 evidence_requirements 共同构成分支检索查询。

### 11.3 每一条证据要求都有稳定编号

当前编号方式：

~~~python
requirement_id = f"{work_item.dimension_number}:{index}"
~~~

例如：

~~~text
维度 1 的第 1 条要求 → 1:1
维度 1 的第 2 条要求 → 1:2
维度 2 的第 1 条要求 → 2:1
~~~

这比让模型随意生成 requirement_id 更稳定，适合日志、重试和测试。

### 11.4 工具执行器负责调用边界

分支通过 ToolExecutor 调用：

~~~python
envelope = executor.execute(
    "search_candidate_evidence",
    lambda query=query: service.search_candidate_evidence(
        query,
        [work_item.candidate_id],
        context=tool_context,
    ),
    context=tool_context,
    arguments={
        "query": query,
        "candidate_ids": [work_item.candidate_id],
    },
)
~~~

工具层不是简单的 Python 函数调用，还可以统一处理：

- 最大重试次数；
- 超时时间；
- 工具调用 ID；
- 错误封装；
- 降级标记；
- 日志和审计。

本课的设计让 Branch Worker 不直接访问数据库，而是访问 TalentToolService。这样权限、租户和工具协议集中在工具层处理。

### 11.5 处理工具失败

当 envelope 的 ok 为 false 时，当前代码不会直接抛出整个分支，而是返回一个 missing requirement：

~~~python
return (
    _missing_requirement(
        requirement_id=requirement_id,
        query=query,
        requirement=requirement,
        reason=str(error.get("code") or "tool_failed"),
    ),
    normalized_call_id,
    True,
)
~~~

这是一种局部降级：

- 当前证据要求标为 missing；
- 当前分支标记 degraded；
- 其他 evidence_requirements 仍有机会继续执行；
- 最终结果保留错误原因。

### 11.6 从 Evidence Pack 取出当前候选人的要求

工具返回的数据中可能包含多个候选人，因此分支要筛选：

~~~python
pack = next(
    (
        item
        for item in data.get("evidence_packs", [])
        if item.get("candidate_id") == work_item.candidate_id
    ),
    None,
)
~~~

如果没有找到对应 pack，则返回 evidence_pack_missing。

注意：没有 Evidence Pack 不等价于候选人没有能力。它代表本次证据协议没有成功产生可消费的结构，需要按执行状态和 error_code 继续区分。

### 11.7 引用归一化

当前代码通过 _citation_refs 把原始引用转换为稳定的小协议：

~~~python
{
    "citation_id": "citation-1",
    "chunk_id": "chunk-1",
    "source_label": "项目复盘"
}
~~~

它会：

- 忽略不是 dict 的引用；
- 必须有 chunk_id；
- 优先使用 citation_id，没有则用 chunk_id；
- 按 chunk_id 去重；
- 使用 document_title 或 source_label 作为来源标签。

这样分支输出不会把过多原始文档内容复制到主图状态中，同时保留后续审查所需的定位信息。

### 11.8 理解函数工厂、闭包和参数来源

这里是本课最关键的调用时机问题：`build_evidence_branch_worker` 并不是在构建图时就取证，而是**创建并返回一个 Worker 函数**。真正的取证要等 LangGraph 执行 Send 分支后发生。

#### 11.8.1 三层函数各管什么

源码：`D:/Code/K_Course/talent-eval-agents_learning/backend/app/talent_evaluation_dispatch.py` 的 `build_evidence_branch_worker`。

```python
def build_evidence_branch_worker(service, executor):
    def branch_worker(work_item, context):
        tool_context = TalentToolContext(...)

        def evaluate_requirement(index, requirement):
            # 对一条证据要求构造查询、调用工具、转换证据
            ...

        indexed_requirements = list(
            enumerate(work_item.dimension.evidence_requirements, start=1)
        )
        with ThreadPoolExecutor(max_workers=len(indexed_requirements)) as pool:
            futures = [
                pool.submit(evaluate_requirement, index, requirement)
                for index, requirement in indexed_requirements
            ]
            outcomes = [future.result() for future in futures]

        # 汇总 outcomes，返回当前候选人 × 维度的草稿
        return BranchEvidenceDraft(...)

    return branch_worker
```

| 层次 | 函数 | 调用时机 | 职责 |
|---|---|---|---|
| 第一层 | `build_evidence_branch_worker(service, executor)` | 构建运行时图 | 把工具服务与执行器绑定到 Worker，返回函数对象；此时不查证据 |
| 第二层 | `branch_worker(work_item, context)` | 一个 Send 分支进入 `run_assessment_branch` 后 | 处理一名候选人 × 一个维度，创建可信上下文，汇总多条证据 |
| 第三层 | `evaluate_requirement(index, requirement)` | 本分支的线程池调用它时 | 处理该维度的一条证据要求，返回证据、调用 ID、降级状态 |

注意 `def` 在 Python 中可以嵌套：执行外层函数时，内层的 `def` **创建函数对象**，并不会立刻执行内层函数体。类似下面这个可运行的小例子：

```python
def outer(prefix):
    def inner(name):
        return f"{prefix}: {name}"
    return inner

fn = outer("候选人")  # 创建 inner，记住 prefix；未执行 inner 的函数体
answer = fn("C001")  # 显式传 name，此时才执行 inner
```

本项目也是一样：`build_evidence_branch_worker(...)` 的返回值是 `branch_worker` **函数对象**，不是候选人的证据草稿；`return branch_worker` 没有圆括号，也就没有当场调用它。

#### 11.8.2 从 Send 到函数实际执行的精确链路

运行时装配模块 `D:/Code/K_Course/talent-eval-agents_learning/backend/app/talent_evaluation_runtime.py` 先把 `service` 和 `ToolExecutor` 传给工厂，得到 `branch_worker`。之后图节点 `_dispatch_work_items` 调用 `build_work_item_sends`，为每个工作项生成：

```python
Send("run_assessment_branch", {"work_item": item})
```

`Send` 是给 LangGraph 的**分支调度指令**，不是 Python 在这里直接调用 `branch_worker`。调度器让目标节点收到含 `work_item` 的分支 state。目标节点从中重新构造 `AssessmentWorkItem`，再调用构图时注入的 Worker，最后返回单元素列表供 reducer 汇总：

```python
work_item = AssessmentWorkItem.model_validate(state["work_item"])
result = branch_worker(work_item, runtime.context)
return {"branch_results": [result.model_dump(mode="json")]}
```

完整时序如下：

```text
图装配：build_evidence_branch_worker(service, executor) → 返回 branch_worker
       ↓
图运行：_dispatch_work_items → Send("run_assessment_branch", {"work_item": item})
       ↓ LangGraph 负责调用目标节点
run_assessment_branch(state, runtime)
       ↓ 取 state["work_item"] 并重新校验
branch_worker(work_item, runtime.context)
       ↓ 创建 evaluate_requirement；线程池分别调用它
RequirementEvidence × 若干 → BranchEvidenceDraft × 1
       ↓
{"branch_results": [草稿]} → operator.add 汇总各分支 → finalize
```

一个 Send 对应一个候选人 × 维度的工作项，并最终产生一个分支结果；其中每条 `evidence_requirement` 则是分支内部的线程池任务，二者不是同一层并发。

#### 11.8.3 “内部函数没传参数”其实是哪两种取值

需要区分**显式调用参数**与**外层作用域捕获的变量**。每个函数的形参在实际调用点都得到了值：

| 当前使用的值 | 谁提供 | 调用点 / 取值方式 |
|---|---|---|
| 第一层的 `service`、`executor` | 运行时装配代码 | `build_evidence_branch_worker(service, ToolExecutor(...))` |
| 第二层的 `work_item` | 分支节点，从 Send 的 state 校验而来 | `branch_worker(work_item, runtime.context)` |
| 第二层的 `context` | LangGraph 运行时上下文 | 同上，第二个实参 `runtime.context` |
| 第三层的 `index`、`requirement` | `enumerate(..., start=1)` 产生并交给线程池 | `pool.submit(evaluate_requirement, index, requirement)` |
| 第三层读到的 `work_item`、`tool_context` | 第二层函数的作用域 | 不必重复传参，闭包读取 |
| 第三层读到的 `service`、`executor` | 第一层函数的作用域 | 不必重复传参，闭包读取 |

例如，两条要求会安排类似两个调用：

```python
pool.submit(evaluate_requirement, 1, "是否承担过 RAG 项目核心开发")
pool.submit(evaluate_requirement, 2, "是否有上线后的效果指标")
```

`submit(fn, arg1, arg2)` 的语义是让线程池执行 `fn(arg1, arg2)`，并立即返回代表运行中或尚未开始的任务的 `Future`。不是把两个参数自动注入 `def`，也不是由 LangGraph 再分发两个 Send。

Python 的词法作用域允许第三层在本地找不到 `work_item` 时向第二层查找，第二层找不到 `executor` 时向第一层查找。这是**闭包（closure）**：函数对象保存了运行时需要的外层变量引用，并非全局变量或框架魔法。可以记为 LEGB：Local → Enclosing → Global → Built-in。

`evaluate_requirement` 只读取 `work_item` 和 `tool_context`；它不直接在多个线程中修改 `requirement_results`、`call_ids` 或 `degraded`。它返回一个三元组，让第二层在拿到 Future 结果后统一更新这些变量，所以这里不需要 `nonlocal`，也减少了线程间共享写入。实际闭包只保存用到的自由变量，并不是把作用域中的所有局部变量都复制一份。

#### 11.8.4 单条要求从输入到输出怎样走

以候选人 `C001` 的维度 2 有两条要求为例，`enumerate(..., start=1)` 得到 `(1, 要求A)` 和 `(2, 要求B)`；两个线程可以同时工作，但每条要求在自己的 `evaluate_requirement` 内按下列固定步骤执行：

1. 通过 `f"{work_item.dimension_number}:{index}"` 生成 `2:1`、`2:2` 等稳定编号。
2. `_branch_query(work_item.dimension, requirement)` 把当前维度的 `retrieval_hints` 与本条 `requirement` 合并，形成检索 query。
3. `executor.execute("search_candidate_evidence", lambda query=query: service.search_candidate_evidence(query, [work_item.candidate_id], context=tool_context), ...)` 通过统一工具边界检索当前候选人的资料；`TalentToolContext` 承载租户、权限、actor、分支 run_id。
4. 从 `envelope["meta"]` 取出工具 `call_id`，作为审计信息。若 `ok` 为假，则返回 `_missing_requirement(...)`、调用 ID 和 `True`（降级）；工具失败**不等于**候选人缺乏该能力。
5. 工具成功时，从 `data["evidence_packs"]` 按 `candidate_id` 找到当前候选人的 pack。**当前实现取这个 pack 的第一条 `requirements[0]`**，因为本次工具调用只针对一条查询；它并不是从列表中按本地 `requirement_id` 匹配。找不到 pack 或首条 requirement 时返回 `evidence_pack_missing`，并标记降级。
6. 使用 `RequirementEvidence.model_validate` 将原始数据与本地稳定编号、query、归一化引用组合，得到统一协议对象；返回 `(result, normalized_call_id, bool(meta.get("degraded")))`。

这里的 `lambda query=query` 是**把当前计算出的 query 绑定为 lambda 的默认参数**，把一个可延迟执行的零必填参数操作交给 `ToolExecutor`。不是马上调用检索服务，也不是给 `evaluate_requirement` 传参数。没有它时，典型循环中延迟执行的 lambda 可能晚绑定到后来变化的变量；这里显式绑定本次 query，更容易推理和审计。

#### 11.8.5 Future、顺序与最终聚合

`pool.submit(...)` 创建的 Future 表示一条取证任务；`future.result()` 是**等待并获取已经安排的任务的返回值**，不会再执行一次函数。源码按提交顺序保存 Future，然后按列表顺序读取：

```python
outcomes = [future.result() for future in futures]
for result, call_id, requirement_degraded in outcomes:
    requirement_results.append(result)
    if call_id is not None:
        call_ids.append(call_id)
    degraded = degraded or requirement_degraded
```

因此哪怕第二条先执行完成，结果仍按第一条、第二条的输入顺序汇总。之后第二层构造：

```python
BranchEvidenceDraft(
    task_id=work_item.task_id,
    candidate_id=work_item.candidate_id,
    dimension_number=work_item.dimension_number,
    execution_status="degraded" if degraded else "succeeded",
    requirements=requirement_results,
    tool_call_ids=call_ids,
)
```

这是一名候选人 × 一个维度的**证据草稿**，不是最终评分。第二层返回该对象，图节点将其转换为 JSON 字典并包装为单元素 `branch_results`；主图的 `operator.add` reducer 合并各分支列表。外层分支完成顺序不一定稳定，不要把内层排序保证误认为所有候选人的输出天然有序；需要稳定展示时应按 `task_id` 或候选人、维度编号排序。

#### 11.8.6 为什么这不等于又启动两个自主 Agent

三个嵌套函数 ≠ 三个 Agent；两个线程 ≠ 两个新的 LangGraph Subagent。当前 Worker 是**确定性的编排代码**，其检索服务内部可以调用模型抽取证据，但本段代码没有为每条 requirement 启动可自由规划的 ReAct Agent。设计目的在于把一个大任务拆成可独立执行、可审计、可归并的证据任务，同时把事实与引用、工具失败与能力缺失、取证与最终评分分开。

一句话记忆：**第一层绑定依赖；第二层处理一个候选人 × 维度；第三层处理其中一条证据要求；显式参数由调用点提供，外层依赖由闭包获取，最终由第二层统一聚合。**

---
## 十二、Evidence Pack 2.0：为什么证据不能只存一段文本

代码路径：

D:/Code/K_Course/talent-eval-agents_learning/backend/app/evidence_pack.py

### 12.1 Evidence Pack 的核心字段

当前证据协议包含：

~~~text
schema_version
candidate_id
requirements[]
    requirement_id
    query
    status
    reason
    extraction_status
    facts[]
        event
        period
        claim
        answer
        sources[]
            chunk_id
            quote
    conflicts[]
    missing_information[]
    citations[]
~~~

### 12.2 事实、来源和结论的关系

正确的关系是：

~~~text
原始材料 chunk
    → 引用 quote
        → ExtractedFact
            → RequirementEvidence
                → BranchEvidenceDraft
~~~

不要直接把模型总结当作事实。事实必须带来源，来源必须能追溯到 chunk。

### 12.3 Evidence Pack 的状态推导

当前逻辑可以概括为：

~~~text
有冲突 → conflicting
没有冲突且有 yes 事实、全部支持且无缺失 → sufficient
有事实但不完整 → partial
没有事实 → missing
~~~

同时，reason 还可以表达为什么：

- no_accessible_hits：没有权限可访问的命中；
- no_relevant_evidence：有材料但没有相关事实；
- evidence_review：已经抽取事实，需要审核；
- extraction_failed：模型抽取失败。

### 12.4 为什么要保留 conflicts 和 missing_information

只保留 sufficient/missing 会损失关键业务信息。

例如：

~~~text
项目复盘文档：候选人主导上线
简历旧版本：候选人仅参与开发
~~~

这不是简单的“有证据”或“没证据”，而是 conflicting，需要交给后续审核或人工确认。

又例如：

~~~text
找到候选人负责过 RAG 项目
但材料没有写上线规模、质量指标或实际结果
~~~

这应该保留：

~~~json
{
  "status": "partial",
  "missing_information": ["缺少上线后的量化结果"]
}
~~~

后续评分模块才能知道不能把“参与过”直接等同于“完整落地能力”。

### 12.5 分支 Worker 为什么使用 Evidence Pack，而不是另做一套抽取逻辑

Evidence Pack 已经提供：

- 来源校验；
- 引用定位；
- 重复事实合并；
- 冲突判断；
- 缺失信息；
- 模型抽取失败降级。

如果 Branch Worker 自己重新实现一套事实抽取，很容易出现两个协议版本不一致。因此当前设计是：

~~~text
Branch Worker 负责任务调度和结果转换
Evidence Pack 负责证据组织和事实审查
~~~

职责清晰，便于复用和测试。

---

## 十三、两层并发：外层 Send，内层 ThreadPoolExecutor

### 13.1 外层并发：候选人 × 维度

外层并发单位是 AssessmentWorkItem：

~~~text
(C001, 维度1)
(C001, 维度2)
(C002, 维度1)
(C002, 维度2)
~~~

它由 LangGraph Send 分发。

### 13.2 内层并发：一个维度的多个证据要求

关于 build_evidence_branch_worker 内部两个 def、闭包变量和每一层参数从哪里传入，请先看 **11.8 节**；此处只聚焦线程池并发与顺序。

Branch Worker 内部：

~~~python
indexed_requirements = list(
    enumerate(work_item.dimension.evidence_requirements, start=1)
)

with ThreadPoolExecutor(
    max_workers=len(indexed_requirements),
    thread_name_prefix="evaluation-requirement",
) as pool:
    futures = [
        pool.submit(evaluate_requirement, index, requirement)
        for index, requirement in indexed_requirements
    ]
    outcomes = [future.result() for future in futures]
~~~

这样一个维度下的多个证据要求可以并发检索。

### 13.3 为什么输出仍然可稳定排序

线程完成顺序可能是：

~~~text
2:2 先完成
2:1 后完成
~~~

但 futures 列表是按照 indexed_requirements 的输入顺序创建的，outcomes 也是按 futures 列表顺序读取，所以最终追加到 requirement_results 的顺序仍然稳定。

这点非常关键：

> 并发执行不应该等于输出乱序。

如果未来改成 as_completed，需要显式按 requirement_id 排序，不能直接使用完成顺序。

### 13.4 两层并发的风险

并发不是免费的。当前代码需要继续关注：

- 外层分支数量上限；
- 内层 evidence_requirements 数量上限；
- 模型服务 QPS；
- 数据库连接池；
- Milvus 和 Rerank 服务并发；
- 单个分支 90 秒超时；
- 线程数与 CPU、IO 的关系。

因此 max_work_items=24 是必要的第一层保护，但生产系统还需要全局限流和批量调度。

---

## 十四、reducer：并行结果如何回到主状态

### 14.1 operator.add 的含义

状态字段：

~~~python
branch_results: Annotated[list[dict[str, Any]], operator.add]
~~~

每个分支返回：

~~~python
{
    "branch_results": [result.model_dump(mode="json")]
}
~~~

多个分支返回后，LangGraph 使用 add 合并：

~~~text
[]
+ [C001:1]
+ [C001:2]
+ [C002:1]
+ [C002:2]
= [C001:1, C001:2, C002:1, C002:2]
~~~

如果没有 reducer，后写结果可能覆盖先写结果，最后只剩一个分支。

### 14.2 reducer 的设计前提

reducer 不是万能去重器。当前 operator.add 只做列表拼接，因此要求：

- 每个工作项只产生一个结果；
- task_id 稳定；
- 不重复派发同一个工作项；
- 如果需要重试，必须做幂等和去重；
- 最终输出如果要求排序，要显式排序。

### 14.3 branch_results 数量应该如何验证

最关键的恒等式：

~~~python
expected = len(result["candidate_ids"]) * len(result["dimensions"])
assert len(result["work_items"]) == expected
assert len(result["branch_results"]) == expected
~~~

如果这两个数量不相等，可能原因包括：

- Send 没有为每项创建分支；
- 分支抛出异常且没有转成结构化失败结果；
- reducer 没有正确配置；
- 重试重复追加；
- 候选人或维度在中途被修改；
- 测试预期字段与当前代码版本不一致。

---

## 十五、失败处理：三类“失败”不能混为一谈

### 15.1 协议失败

例子：

- 模型返回空维度列表；
- 权重不是 100；
- score_anchors 缺 0、3、5；
- 来源 ID 不存在。

处理方式：停止在 dimension_invalid，不进入分发。

### 15.2 工具或证据失败

例子：

- 检索服务暂时不可用；
- 没有命中材料；
- Evidence Pack 没有返回当前候选人；
- 模型抽取失败。

处理方式：尽可能生成 RequirementEvidence，保留 reason、missing_information、extraction_status，并把分支标记为 degraded 或返回 failed。

### 15.3 图执行失败

例子：

- 未捕获的异常；
- Runtime Context 缺失；
- State 字段无法反序列化；
- 分支代码崩溃。

当前 _run_assessment_branch 对已知的 TimeoutError 和 BranchExecutionError 转成失败结果，但对未知异常使用 logger.exception 后继续抛出。这样做是合理的：

- 已知可恢复错误：结构化降级；
- 未知程序错误：不能假装成功，应保留堆栈并让系统感知。

### 15.4 状态组合示例

| Branch 状态 | Requirement 状态 | 含义 |
|---|---|---|
| succeeded | sufficient | 分支执行成功，证据充分 |
| succeeded | partial | 分支执行成功，但事实不完整 |
| succeeded | missing | 检索成功但没有相关事实 |
| succeeded | conflicting | 找到了互相冲突的事实 |
| degraded | sufficient/missing | 部分要求失败，但仍保留已完成结果 |
| failed | 空或部分 | 分支执行级别失败 |

---

## 十六、从验证脚本理解真实链路

代码路径：

D:/Code/K_Course/talent-eval-agents_learning/backend/scripts/verify_talent_evaluation_dispatch.py

### 16.1 验证脚本构造的输入

脚本默认构造：

~~~python
{
    "talent_request": {
        "original_text": "筛选上海且具备 RAG 落地与 Agent 评测经验的人才，协作交付经验优先",
        "source": "detailed_requirement",
        "hard_conditions": [
            {"field": "region", "operator": "eq", "value": "上海"}
        ],
        "semantic_conditions": [
            {"requirement_id": "S1", "query": "RAG 项目落地经验", "required": True},
            {"requirement_id": "S2", "query": "Agent 评测经验", "required": True},
        ],
        "evaluation_preferences": ["协作与交付经验优先"],
    },
    "query_plan": {
        "task_type": "find_talent",
        "filters": [
            {"field": "region", "operator": "eq", "value": "上海"}
        ],
    },
}
~~~

脚本没有固定写死候选人，也没有固定写死分支结果。候选人由真实数据库和 QueryPlan 过滤得到，证据由真实检索链路得到。

### 16.2 运行命令

在当前项目环境中，建议先启动依赖服务，再执行：

~~~powershell
cd D:/Code/K_Course/talent-eval-agents_learning/backend
uv run --no-sync python -m scripts.verify_talent_evaluation_dispatch
~~~

也可以调整参数：

~~~powershell
uv run --no-sync python -m scripts.verify_talent_evaluation_dispatch --tenant-id course-demo --region 上海 --max-concurrency 6 --log-level INFO
~~~

### 16.3 运行前检查项

需要确认：

1. PostgreSQL 已启动；
2. Milvus 已启动；
3. MinIO 已启动；
4. etcd 已启动；
5. 项目根目录 .env 中已配置模型 API Key；
6. Chat Model 可用；
7. Embedding 服务可用；
8. Rerank 服务可用；
9. 当前 tenant 下已有候选人数据；
10. 候选人对应的证据已完成索引；
11. 权限 scope 与证据数据匹配；
12. backend 的 Python 环境依赖已安装。

### 16.4 如何看三类输出

脚本最后打印：

~~~text
[dimensions] ...
[dispatch] ...
[result] ...
~~~

#### dimensions

记录动态维度编号、权重和检索提示。你要检查：

- 维度是否来自 S1、S2 和 preference:1；
- 权重合计是否为 100；
- retrieval_hints 是否能用于检索；
- 有没有明显重复维度。

#### dispatch

记录 candidate_ids、work_items 数量和 max_concurrency 参数。

验证公式：

~~~text
work_items 数量 = candidate_ids 数量 × dimensions 数量
~~~

#### result

记录 status、各 execution_status 数量和 non_succeeded 分支及错误原因。

常见成功状态：

~~~text
branches_ready
~~~

如果存在分支级失败：

~~~text
branches_ready_with_failures
~~~

如果动态维度权重校验失败：

~~~text
dimension_invalid
~~~

如果没有候选人：

~~~text
no_candidates
~~~

如果超过容量上限：

~~~text
capacity_exceeded
~~~

### 16.5 输出 JSON 文件

脚本还会把完整结果写入：

~~~text
D:/Code/K_Course/talent-eval-agents_learning/output/talent_evaluation_dispatch_result_YYYYMMDD_HHMMSS.json
~~~

建议检查 JSON 中的 dimensions、candidate_ids、work_items、branch_results、status、errors，并抽查一个 branch_result：

- task_id 是否与 candidate_id 和 dimension_number 一致；
- requirements 数量是否等于该维度的 evidence_requirements 数量；
- tool_call_ids 是否有调用记录；
- citations 是否能定位到 chunk；
- missing_information 是否保留；
- conflicts 是否丢失。

---

## 十七、课后作业一：怎样把维度校验做得更好

### 17.1 推荐的实现顺序

不要一上来就写复杂的语义相似度。按下面顺序最稳妥：

#### 第一步：为每条规则写失败测试

例如：

~~~python
def test_dimension_validation_rejects_duplicate_names():
    plan = EvaluationDimensionPlan(
        dimensions=[
            make_dimension(name="RAG 能力", weight_percent=50),
            make_dimension(name=" RAG 能力 ", weight_percent=50),
        ]
    )

    issues = validate_dimension_plan(plan)

    assert any(item.code == "dimension_name_duplicated" for item in issues)
~~~

#### 第二步：增加确定性规则

推荐顺序：

1. 权重合计；
2. 名称重复；
3. 证据要求重复；
4. 评分锚点集合；
5. 来源 ID 合法性；
6. 必需语义要求覆盖；
7. 权重极端倾斜。

#### 第三步：再做语义规则

可以使用：

- 关键词 Jaccard 相似度；
- Embedding 相似度；
- 第二次模型审查；
- 规则和模型组合。

但语义规则要输出“告警”还是“阻断”，应该分开：

~~~text
确定性协议错误 → 阻断
明显重复维度 → 阻断或人工确认
可能重复 → 告警
描述略有相似 → 不阻断
~~~

### 17.2 权重校验的进一步思考

只判断合计 100 还不够，可以考虑：

- 单个维度不能超过 70；
- 必选语义要求至少有一个维度覆盖；
- 偏好可以影响权重，但不能把必选要求权重压到 0；
- 维度数量过少可能无法覆盖所有要求；
- 维度数量过多会导致任务矩阵膨胀。

但不要把合理范围写死得过于僵硬。例如不同岗位可能真的有一个核心能力占 80%。更好的做法是：

- 默认规则给出 warning；
- 关键政策才阻断；
- 允许业务方配置规则。

### 17.3 改进后的校验架构

建议把校验拆成三层：

~~~text
Schema Validation
  └── Pydantic 字段和类型

Deterministic Business Validation
  └── 权重、重复、来源、覆盖、数量

Semantic Review
  └── 维度重叠、可取证性、岗位相关性
~~~

这样以后出现问题时，可以明确回答：

- 是格式错误；
- 是业务规则错误；
- 还是语义审查不通过。

---

## 十八、课后作业二：增加候选人并检查任务矩阵

### 18.1 先理解数据路径

增加候选人不是只往数据库插一行姓名，还需要考虑：

~~~text
候选人档案
  → 硬条件筛选
      → candidate_ids
          → 候选人 × 维度任务矩阵
              → 每个任务检索候选人证据
                  → branch_results
~~~

如果只增加候选人档案，不增加相关证据文档，可能会出现：

- 候选人进入 candidate_ids；
- work_items 数量增加；
- branch_results 数量增加；
- 但 RequirementEvidence 大多是 missing。

这并不说明分发失败，而是说明该候选人的证据覆盖不足。

### 18.2 数量推导

假设原来：

~~~text
候选人：C001、C002
维度：2 个
work_items：2 × 2 = 4
branch_results：4
~~~

新增 C003 后：

~~~text
候选人：C001、C002、C003
维度：2 个
work_items：3 × 2 = 6
branch_results：6
~~~

如果动态模型生成了 3 个维度，则是：

~~~text
3 名候选人 × 3 个维度 = 9 个任务
~~~

因此检查数量时一定要以本次真实返回的 dimensions 数量为准，不能把预期有 2 个维度写死。

### 18.3 推荐验证代码

~~~python
candidate_count = len(result["candidate_ids"])
dimension_count = len(result["dimensions"])
expected_count = candidate_count * dimension_count

assert len(result["work_items"]) == expected_count
assert len(result["branch_results"]) == expected_count

work_item_ids = {item["task_id"] for item in result["work_items"]}
branch_result_ids = {item["task_id"] for item in result["branch_results"]}
assert work_item_ids == branch_result_ids
~~~

还应该检查不重复：

~~~python
pairs = {
    (item["candidate_id"], item["dimension_number"])
    for item in result["branch_results"]
}
assert len(pairs) == expected_count
~~~

### 18.4 如果 branch_results 少于 work_items，排查顺序

1. 看日志中的 event=work_items_dispatched；
2. 确认 task_ids 是否完整；
3. 看是否有 graph_branch_failed；
4. 看分支失败是否被包装成 BranchEvidenceDraft；
5. 检查 branch_results 是否设置了 operator.add reducer；
6. 检查是否存在重复 retry 但又被去重；
7. 检查最终状态是否提前进入 capacity_exceeded 或 no_candidates；
8. 检查使用的代码版本是否与测试预期一致。

### 18.5 新增候选人时的最低数据要求

建议至少准备：

- 唯一 employee_no；
- tenant_id；
- name；
- region；
- employment_status=active；
- 和硬条件匹配的字段；
- 至少一份与评估维度相关的证据材料；
- 证据材料已切分、索引并带 candidate_id；
- 权限 scope 可以访问这些材料。

---

## 十九、课后作业三：启动服务并进行真实链路验证

### 19.1 先不要直接运行脚本

真实链路包含多个外部依赖。建议顺序：

~~~text
1. 检查 .env
2. 启动基础设施
3. 检查数据库和索引数据
4. 运行最小健康检查
5. 运行第 15 课验证脚本
6. 保存日志和输出 JSON
7. 记录动态维度、候选人数和分支状态
~~~

### 19.2 启动基础设施

当前仓库提供 Docker Compose 文件：

- D:/Code/K_Course/talent-eval-agents_learning/docker-compose.yml
- D:/Code/K_Course/talent-eval-agents_learning/docker-compose-mineru.yml

具体启动命令以当前机器 Docker 配置和 README 为准。启动后要确认 PostgreSQL、Milvus、MinIO、etcd 等服务处于可用状态。

### 19.3 执行验证脚本

~~~powershell
cd D:/Code/K_Course/talent-eval-agents_learning/backend
uv run --no-sync python -m scripts.verify_talent_evaluation_dispatch
~~~

调试日志：

~~~powershell
uv run --no-sync python -m scripts.verify_talent_evaluation_dispatch --log-level DEBUG
~~~

指定候选人筛选区域：

~~~powershell
uv run --no-sync python -m scripts.verify_talent_evaluation_dispatch --region 上海
~~~

### 19.4 记录实验结果

建议建立如下表格：

| 项目 | 实际值 |
|---|---|
| tenant_id | course-demo |
| region | 上海 |
| 动态维度数量 | 运行后填写 |
| 动态维度名称 | 运行后填写 |
| 权重合计 | 运行后填写，必须为 100 |
| 候选人数 | 运行后填写 |
| work_items 数量 | 运行后填写 |
| branch_results 数量 | 运行后填写 |
| succeeded 数量 | 运行后填写 |
| degraded 数量 | 运行后填写 |
| failed 数量 | 运行后填写 |
| 总体 status | 运行后填写 |

并写出数量验证：

~~~text
candidate_count × dimension_count = work_item_count = branch_result_count
~~~

### 19.5 如何分析 degraded

出现 degraded 不要立即认为程序坏了。先看每个分支的 requirements：

- 是没有命中材料；
- 是没有权限；
- 是 Evidence Pack 缺失；
- 是模型抽取失败；
- 是检索工具超时；
- 还是部分要求成功、部分要求失败。

如果 execution_status 是 degraded，通常仍然可以使用其中成功完成的 RequirementEvidence，但不能把整个分支当作完整证据。

### 19.6 实验报告应该写什么

建议报告结构：

~~~text
1. 本次输入的硬条件、语义条件、偏好
2. 模型生成了哪些动态维度
3. 每个维度的权重和证据要求
4. 硬条件筛出了多少候选人
5. 理论任务数和实际 work_items 数量
6. branch_results 数量是否相等
7. 各执行状态数量
8. 是否存在 missing/conflicting/partial
9. 失败分支的 error_code
10. 你认为下一步最值得改进的地方
~~~

---

## 二十、常见问题与排查方法

### 问题 1：启动时提示模型未配置

现象：

~~~text
评估维度生成模型未配置
模型未配置，请先设置项目 .env 中的 DASHSCOPE_API_KEY
~~~

排查：

1. 检查项目根目录 .env；
2. 确认变量名与 backend/app/config.py 一致；
3. 确认不是默认占位文本；
4. 确认 backend 进程读取的是正确 env 文件；
5. 确认模型服务地址和模型名称可用。

### 问题 2：权重合计不是 100

现象：

~~~text
维度权重合计必须为 100，当前为 90
~~~

排查：

- 打印 dimensions 的 name 和 weight_percent；
- 不要只看模型输出原文；
- 确认 structured_output 确实绑定 EvaluationDimensionPlan；
- 在进入分发前修复，不要在分支里补权重。

### 问题 3：没有候选人

可能原因：

- region 不匹配；
- tenant_id 不匹配；
- employment_status 不是 active；
- QueryPlan.filters 没有正确解析；
- 数据还没有 seed；
- 权限范围导致不可见。

先看 retrieve_candidates 日志，不要先怀疑 Send。

### 问题 4：work_items 有，但 branch_results 少

排查：

- 是否出现 graph_branch_failed；
- 是否在未知异常处直接抛出；
- 是否有分支没有返回 dict；
- 是否把 branch_results 写成普通 list 而没有 reducer；
- 是否运行到了另一个图或另一个代码版本。

### 问题 5：branch_results 有，但 requirements 为空

可能原因：

- 动态维度的 evidence_requirements 为空，但 Pydantic 应该阻止；
- branch_worker 没有执行；
- Evidence Pack 转换失败；
- 测试 fake worker 只返回元数据；
- 当前查看的是旧输出文件。

### 问题 6：候选人有材料却是 missing

先区分：

- 材料是否带正确 candidate_id；
- 检索过滤是否使用了正确 tenant_id；
- permission_scopes 是否允许访问；
- Evidence Pack 的 requirement_ids 是否是 branch_requirement；
- query 是否包含足够的 retrieval_hints；
- 材料是否已建立索引。

### 问题 7：代码字段和测试字段不一致

本课经历了提交演进，早期代码可能使用 dimension_id，当前 HEAD 使用 dimension_number。出现类似错误时：

1. 先确认当前 HEAD；
2. 查看当前模型定义；
3. 查看目标提交的 diff；
4. 不要把旧测试的字段直接当成当前协议；
5. 以当前代码的 Pydantic 模型和验证脚本为准；
6. 如果测试是课程过程文件，需要同步更新。

这是学习 Git 演进和协议迁移的一部分。

---

## 二十一、当前设计中最值得你记住的工程原则

### 原则 1：先分层，再并发

不要直接对用户原话做大量并发。正确顺序是：

~~~text
自然语言
  → TalentRequest
  → QueryPlan / EvaluationDimensionPlan
  → 验证
  → 工作项
  → 并发
~~~

### 原则 2：模型生成协议，代码决定是否执行

模型可以提出维度，但代码负责：

- 检查结构；
- 检查权重；
- 检查来源；
- 检查数量；
- 决定是否进入候选人检索；
- 决定是否超出容量。

### 原则 3：硬条件和语义条件分工

- 硬条件：数据库过滤，决定候选人集合；
- 语义条件：动态维度和证据检索；
- 评估偏好：影响维度和权重，但不能替代硬条件。

### 原则 4：分支只返回证据，不直接返回结论

证据与结论分开，才能：

- 审查；
- 解释；
- 重跑；
- 比较不同评分策略；
- 在后续课程改变打分规则而不重做检索。

### 原则 5：可观测性是协议的一部分

task_id、requirement_id、tool_call_id、execution_status、reason 不是多余字段，它们决定了你能否定位问题。

### 原则 6：降级不等于成功，也不等于全失败

degraded 允许系统保留局部结果，但必须让下游知道证据不完整。

### 原则 7：并行输出必须可聚合、可排序、可去重

Send 负责扇出，reducer 负责聚合，task_id 负责定位，显式排序负责稳定性。

---

## 二十二、建议的进一步改进方向

### 22.1 维度协议版本化

当前可以增加：

~~~text
schema_version: "1.0"
plan_id
created_at
source_request_hash
~~~

这样后续评分模块可以知道自己消费的是哪一版维度协议。

### 22.2 为每个维度使用稳定内部 ID

当前使用 dimension_number 有利于本课稳定运行，但如果维度计划需要跨运行持久化，建议由代码生成稳定 ID：

~~~text
dimension_number：本次运行中的位置
stable_dimension_id：规范化名称和来源的哈希或服务端生成 ID
~~~

不要直接把模型生成的自然语言 name 当主键。

### 22.3 外层并发增加批量和限流

可以增加：

- max_concurrency；
- 每租户并发上限；
- 每个候选人并发上限；
- 每个工具 QPS；
- 批量发送；
- 失败重试队列；
- 熔断和退避。

### 22.4 Branch Worker 改为异步

当前使用 ThreadPoolExecutor 适合阻塞式 IO，但如果检索和模型客户端支持 async，可以考虑：

~~~text
async Send branch
  → asyncio.gather evidence requirements
  → 显式按 requirement_id 排序
~~~

迁移时要特别注意：

- 线程上下文；
- 数据库会话不能跨线程乱用；
- 异步客户端是否真的支持并发；
- reducer 的返回结构不变。

### 22.5 对 Evidence Pack 做强一致性校验

可以校验：

- citation 的 chunk_id 必须在来源集合中；
- fact source 的 quote 必须能在原文中找到；
- quote_start 和 quote_end 合法；
- conflict 下的事实索引有效；
- missing_information 非空时 status 不能是 sufficient；
- status 为 sufficient 时必须至少有一个 yes fact。

### 22.6 评分阶段单独成图

建议后续保持：

~~~text
第15课：Evaluation Dispatch Graph
  → 只取证

第16课：Scoring Graph
  → 读取 Evidence Pack
  → 根据 score_anchors 评分
  → 汇总加权分

后续：Decision / Report Graph
  → 排序、解释、人工复核、报告
~~~

这是比一个巨大 Agent 一次完成全部工作更容易维护的架构。

---

## 二十三、完全掌握本课的学习检查清单

如果你能不看代码回答下面问题，就说明已经掌握了主线：

### 概念检查

- [ ] 为什么 hard_conditions 不应该生成评分维度？
- [ ] semantic_conditions 和 evaluation_preferences 分别影响什么？
- [ ] evidence_requirements 和 retrieval_hints 的差别是什么？
- [ ] 为什么当前课只输出证据草稿，不直接输出最终分数？
- [ ] Subagent 和确定性 Branch Worker 的关系是什么？

### LangGraph 检查

- [ ] State 中为什么 branch_results 要使用 reducer？
- [ ] Send 的输入和目标节点是什么？
- [ ] 为什么要在 Send 前做容量检查？
- [ ] 分支失败怎样不影响其他分支？
- [ ] Runtime Context 和 State 的区别是什么？

### 数据协议检查

- [ ] EvaluationDimension 的每个字段做什么？
- [ ] Pydantic 和 validate_dimension_plan 的职责差别是什么？
- [ ] Evidence Pack 为什么需要 citations、conflicts、missing_information？
- [ ] execution_status 和 requirement.status 的区别是什么？

### 工程检查

- [ ] 如何计算理论 work_items 数量？
- [ ] 如何确认 branch_results 没有丢失？
- [ ] degraded 和 failed 如何区分？
- [ ] 为什么 task_id 要稳定？
- [ ] 为什么不能让用户输入 tenant_id 和 permission_scopes？

### 实操检查

- [ ] 能否定位 build_runtime_graph？
- [ ] 能否定位 build_talent_evaluation_dispatch_graph？
- [ ] 能否运行验证脚本？
- [ ] 能否从日志找到具体 candidate_id、dimension_number 和 requirement_id？
- [ ] 能否增加一个候选人并证明任务数增加了维度数？

---

## 二十四、最后的完整复盘

本课真正的学习路径不是记住几个 LangGraph API，而是理解以下因果链：

~~~text
岗位要求不是一个可以直接交给 Agent 的字符串
    ↓
先拆成硬条件、语义条件和偏好
    ↓
硬条件进入 QueryPlan，筛选候选人
    ↓
语义条件和偏好生成动态评估维度
    ↓
维度必须经过结构和业务校验
    ↓
候选人和维度形成笛卡尔积工作矩阵
    ↓
Send 把工作矩阵拆成并行分支
    ↓
确定性的 Branch Worker 按证据要求取证
    ↓
Evidence Pack 保留事实、引用、冲突和缺失信息
    ↓
reducer 把所有局部分支聚合成主状态
    ↓
为后续评分、排序和报告提供可信输入
~~~

你作为刚毕业、准备做 Agent 开发的同学，最应该带走的不是“我会调用 Send”，而是下面这句话：

> Agent 系统的可靠性，不是来自模型一次生成了多漂亮的答案，而是来自清晰的协议、受控的工具边界、确定性的分支、可追溯的证据和可验证的聚合结果。

当你未来面对一个新的 Agent 需求时，可以用本课的方法重新拆解：

1. 哪些内容是硬条件，应该先过滤？
2. 哪些内容是语义要求，需要动态解释？
3. 模型应该生成什么协议，而不是直接生成什么结论？
4. 哪些任务可以组成笛卡尔积并行执行？
5. 每个分支最小需要什么上下文？
6. 分支输出怎样结构化、可审计、可降级？
7. 并行结果如何聚合，如何保证数量、顺序和幂等？
8. 哪些判断必须留到后续评分或人工审核？

如果你能按照这八个问题设计系统，你就已经开始从“会调用大模型”进入“会设计 Agent 系统”的阶段。
