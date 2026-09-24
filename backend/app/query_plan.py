from __future__ import annotations

from datetime import date
from enum import StrEnum
from dataclasses import dataclass
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import Select, and_, select
from sqlalchemy.orm import Session

from app.models import EmployeeProfile


class TaskType(StrEnum):
    FIND_TALENT = "find_talent"
    LOOKUP_PROFILE = "lookup_profile"


class FilterField(StrEnum):
    AGE = "age"
    REGION = "region"
    DEPARTMENT = "department"
    JOB_LEVEL = "job_level"
    YEARS_OF_EXPERIENCE = "years_of_experience"
    CURRENT_POSITION = "current_position"
    EMPLOYMENT_STATUS = "employment_status"


class FilterCondition(BaseModel):
    field: FilterField = Field(description="结构化人才字段，只能使用 FilterField 枚举中的值") # 对应pg数据库中employee_profiles表的字段结结构
    operator: Literal["eq", "in", "lt", "lte", "gt", "gte"] = Field(
        description="比较操作符：eq 等于，in 属于集合，lt/lte/gt/gte 为数值比较"
    )
    value: str | int | float | list[str] = Field(description="从用户原话提取的比较值，不得补充用户未提供的阈值")

    @model_validator(mode="after")
    def validate_operator_and_value(self):
        numeric = {FilterField.AGE, FilterField.YEARS_OF_EXPERIENCE}
        if self.operator in {"lt", "lte", "gt", "gte"} and self.field not in numeric:
            raise ValueError("范围运算符只允许用于数值字段")
        if self.operator == "in" and not isinstance(self.value, list):
            raise ValueError("in 运算符必须接收列表")
        if self.operator != "in" and isinstance(self.value, list):
            raise ValueError("只有 in 运算符允许列表值")
        return self


class SemanticRequirement(BaseModel):
    requirement_id: str = Field(pattern=r"^S\d+$", description="语义要求编号，如 S1、S2")
    query: str = Field(min_length=1, max_length=500, description="需要在人才材料中检索的经历、技能或成果要求")
    required: bool = Field(default=True, description="用户是否把该语义要求表达为必须条件")


class Clarification(BaseModel):
    expression: str
    reason: str


class QueryPlan(BaseModel):
    task_type: TaskType = Field(description="查询任务类型，用于选择后续执行器")
    filters: list[FilterCondition] = Field(default_factory=list, description="可由 PostgreSQL 精确执行的结构化硬条件") # 对应employee_profiles表的可筛选字段结构
    # 示例：
    # [
    #   {
    #       "field": "department",
    #       "operator": "in",
    #       "value": ["数据平台部", "研发部"]
    #   },
    # ]

    semantic_requirements: list[SemanticRequirement] = Field(default_factory=list, description="需要调用混合检索寻找材料证据的条件") # 向量化查询非结构化数据
    # 示例：
    # [
    #   {
    #       "requirement_id": "S1",
    #       "query": "Flink 实时计算项目经历",
    #       "required": true
    #   },
    #   {"requirement_id": "S2", "query": "团队管理经历", "required": true}
    # ]

    preferences: list[str] = Field(default_factory=list, description="优先、倾向等软条件，保留给后续排序或评估，不作为 SQL 排除条件") # (当前lesson-8仅定义 偏好 属性，暂未实现)
    # 示例：
    # ["金融行业项目经验优先"]

    clarifications: list[Clarification] = Field(default_factory=list, description="缺少阈值、含义不明或互相矛盾的条件") # 只要存在待澄清项，就不能执行候选人 SQL。(当前lesson-8仅定义待 澄清条件 属性，暂未实现)
    # 示例：
    # [
    #   {
    #       "expression": "比较年轻",
    #       "reason": "缺少可执行的年龄阈值"
    #   },
    # ]

    @property
    def executable(self) -> bool:
        return not self.clarifications

    # 作业2：对age属性不可能情况进行提前检测
    @model_validator(mode="after")
    def validate_filter_consistency(self):
        _validate_age_range(self.filters)
        return self

def _validate_age_range(filters: list[FilterCondition]):
    lower: tuple[float,bool] | None = None
    upper: tuple[float,bool] | None = None

    for item in filters:
        if item.field != FilterField.AGE:
            continue

        if item.operator not in {"lt", "lte", "gt", "gte"}:
            continue

        value = float(item.value)

        # 判断是否重新设置下界
        if item.operator in {"gt", "gte"}:
            inclusive = item.operator == "gte"
            if (
                lower is None
                or value > lower[0]
                or (value == lower[0] and not inclusive and lower[1]) # 原本是 ">=" 现在新参数是 ">" 的情况
            ):
                lower = (value,inclusive)
        # 判断是否重新设置上界
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

    # 判断上下界边界
    impossble = (
        lower_value > upper_value
        or (
            lower_value == upper_value
            and not (lower_inclusive and upper_inclusive)
        )
    )

    if impossble:
        raise ValueError(
            "年龄区间冲突："
            f"下界为 {lower_value}，上界为 {upper_value}"
        )

def _age_boundary(value: int, today: date) -> date:
    try:
        return today.replace(year=today.year - value)
    except ValueError:
        return today.replace(month=2, day=28, year=today.year - value)


@dataclass(frozen=True)
class FilterFieldSpec:
    description: str
    operators: frozenset[str]
    build: Callable[[FilterCondition, date], Any]


def _column_spec(column: Any, description: str, operators: set[str]) -> FilterFieldSpec:
    def build(item: FilterCondition, _: date):
        value = item.value
        return {
            "eq": lambda: column == value,
            "in": lambda: column.in_(value),
            "lt": lambda: column < value,
            "lte": lambda: column <= value,
            "gt": lambda: column > value,
            "gte": lambda: column >= value,
        }[item.operator]()

    return FilterFieldSpec(description, frozenset(operators), build)


def _age_spec(item: FilterCondition, today: date):
    if item.operator not in {"lt", "lte", "gt", "gte"}:
        raise ValueError("age 只支持 lt、lte、gt、gte")
    boundary = _age_boundary(int(item.value), today)
    reversed_operator = {"lt": ">", "lte": ">=", "gt": "<", "gte": "<="}[item.operator]
    return EmployeeProfile.birth_date.op(reversed_operator)(boundary)


FILTER_FIELD_REGISTRY = {
    FilterField.AGE: FilterFieldSpec("年龄，由 birth_date 按查询基准日换算", frozenset({"lt", "lte", "gt", "gte"}), _age_spec),
    FilterField.REGION: _column_spec(EmployeeProfile.region, "工作地区", {"eq", "in"}),
    FilterField.DEPARTMENT: _column_spec(EmployeeProfile.department, "所属部门", {"eq", "in"}),
    FilterField.JOB_LEVEL: _column_spec(EmployeeProfile.job_level, "职级，编码格式为 L1、L2 等", {"eq", "in"}),
    FilterField.YEARS_OF_EXPERIENCE: _column_spec(EmployeeProfile.years_of_experience, "工作年限", {"eq", "lt", "lte", "gt", "gte"}),
    FilterField.CURRENT_POSITION: _column_spec(EmployeeProfile.current_position, "当前岗位", {"eq", "in"}),
    FilterField.EMPLOYMENT_STATUS: _column_spec(EmployeeProfile.employment_status, "在职状态", {"eq", "in"}),
}


def filter_dsl_catalog() -> str:
    return "\n".join(
        f"- {field.value}: {spec.description}; operators={','.join(sorted(spec.operators))}"
        for field, spec in FILTER_FIELD_REGISTRY.items()
    )


def build_candidate_statement(
    plan: QueryPlan, *, tenant_id: str, today: date | None = None
) -> Select[tuple[str]]:
    if not tenant_id:
        raise ValueError("tenant_id 不能为空")
    if not plan.executable:
        raise ValueError("查询计划仍有待澄清条件")

    current_day = today or date.today()
    clauses: list[Any] = [
        EmployeeProfile.tenant_id == tenant_id,
        EmployeeProfile.employment_status == "active",
    ]
    for item in plan.filters:
        spec = FILTER_FIELD_REGISTRY[item.field]
        if item.operator not in spec.operators:
            raise ValueError(f"{item.field.value} 不支持 {item.operator} 操作符")
        clauses.append(spec.build(item, current_day))

    return select(EmployeeProfile.employee_no).where(and_(*clauses)).order_by(EmployeeProfile.employee_no)


def select_candidate_ids(db: Session, plan: QueryPlan, *, tenant_id: str, today: date | None = None) -> list[str]:
    return list(db.scalars(build_candidate_statement(plan, tenant_id=tenant_id, today=today)).all())


def execute_composite_search(
    plan: QueryPlan,
    *,
    candidate_ids: list[str],
    search_requirement: Any,
) -> dict[str, Any]:
    """Run semantic requirements only inside the SQL-selected candidate set."""
    if not plan.executable:
        raise ValueError("查询计划仍有待澄清条件")
    if not candidate_ids:
        return {
            "task_type": plan.task_type,
            "candidate_ids": [],
            "evidence_by_requirement": {},
            "preferences": plan.preferences,
        }

    evidence = {
        requirement.requirement_id: search_requirement(
            query=requirement.query,
            candidate_ids=candidate_ids,
        )
        for requirement in plan.semantic_requirements
    }
    return {
        "task_type": plan.task_type,
        "candidate_ids": candidate_ids,
        "evidence_by_requirement": evidence,
        "preferences": plan.preferences,
    }


class QueryOptimizationPlan(BaseModel):
    strategy: Literal["rewrite", "multi_query", "decompose"]
    queries: list[str] = Field(min_length=1, max_length=3, description="最多 3 条检索查询，不能改变原始约束")
    reason: str


def choose_optimization_strategy(*, hit_count: int, requirement_count: int, expression_is_vague: bool = False) -> str | None:
    if hit_count > 0:
        return None
    if requirement_count > 1:
        return "decompose" # 拆分复杂要求
    if expression_is_vague:
        return "rewrite" # # 改写模糊表达
    return "multi_query" # 用多个不同说法扩展查询


def optimize_semantic_query(requirement: SemanticRequirement, *, strategy: str, model: Any) -> QueryOptimizationPlan:
    structured_model = model.with_structured_output(QueryOptimizationPlan)
    return structured_model.invoke(
        [
            ("system", "根据指定策略生成检索查询。保留原始人才条件，不增加年限、职级或技能要求。最多返回 3 条查询。"),
            ("user", f"strategy={strategy}\noriginal_requirement={requirement.query}"),
        ]
    )


def merge_query_results(result_sets: list[list[Any]]) -> list[Any]:
    """Keep the first and therefore highest-ranked occurrence of each chunk."""
    merged: list[Any] = []
    seen: set[str] = set()
    for result_set in result_sets:
        for item in result_set:
            chunk_id = item.chunk_id if hasattr(item, "chunk_id") else item["chunk_id"]
            if chunk_id not in seen:
                seen.add(chunk_id)
                merged.append(item)
    return merged


def search_with_optimization(
    requirement: SemanticRequirement,
    *,
    search_query: Callable[[str], list[Any]],
    model: Any,
    requirement_count: int = 1,
    expression_is_vague: bool = False,
) -> dict[str, Any]:
    first_pass = search_query(requirement.query)
    strategy = choose_optimization_strategy(
        hit_count=len(first_pass),
        requirement_count=requirement_count,
        expression_is_vague=expression_is_vague,
    )
    if strategy is None:
        return {"strategy": None, "queries": [requirement.query], "results": first_pass}

    optimization = optimize_semantic_query(requirement, strategy=strategy, model=model) # 针对一项检索不到的经历要求，生成最多三条替代检索文本，输出 QueryOptimizationPlan
    fallback_results = [search_query(query) for query in optimization.queries]
    return {
        "strategy": optimization.strategy,
        "queries": [requirement.query, *optimization.queries],
        "results": merge_query_results([first_pass, *fallback_results]),
    }


QUERY_PLAN_SYSTEM_PROMPT = """你是人才查询计划编译器。只把用户明确表达的条件转换为 QueryPlan。
硬条件只允许使用下面的 Filter DSL，禁止生成 SQL：
{filter_dsl}
经历、技能、项目成果等材料内容写入 semantic_requirements。
“优先”等软偏好写入 preferences，不能改写为硬条件。
含糊表达写入 clarifications，禁止自行补充阈值。权限、租户和密级不从用户文本提取。"""


def compile_query_plan(query: str, model: Any) -> QueryPlan:
    structured_model = model.with_structured_output(QueryPlan) # 通过with_structured_outputg方法限定LLM输出的结构 不是已返回json!

    return structured_model.invoke(
        [("system", QUERY_PLAN_SYSTEM_PROMPT.format(filter_dsl=filter_dsl_catalog())), ("user", query)]
    )
    # 拿到结构定义model后再根据插入 filter_dsl的system prompt 输出 QueryPlan
