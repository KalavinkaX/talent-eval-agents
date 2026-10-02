from __future__ import annotations

from collections.abc import Callable
from operator import add
from typing import Annotated, Any, Literal

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt
from pydantic import BaseModel, Field, ValidationError
from typing_extensions import TypedDict

from app.database import SessionLocal
from app.model_provider import get_chat_model
from app.query_plan import Clarification, FilterCondition, QueryPlan, SemanticRequirement, TaskType
from app.talent_decision_graph import DecisionContext
from app.talent_tools import TalentToolContext, TalentToolService


RequestInputMode = Literal["detailed_requirement", "job_name"]
RequestStatus = Literal[
    "received",
    "request_understood",
    "jobs_found",
    "job_confirmed",
    "no_job_match",
    "plan_ready",
    "clarification_required",
    "candidates_empty",
    "candidates_ready",
    "plan_revised",
    "request_revised",
    "cancelled",
    "revision_limit_reached" # 作业1：新增达到最大修订的status
]


class JobMatch(TypedDict):
    job_code: str
    name: str
    match_score: float
    match_type: Literal["exact", "contains", "fuzzy"]
    version: int
    content: str


class TargetJob(TypedDict):
    job_code: str
    name: str
    version: int


class TalentRequestDraft(BaseModel):
    input_mode: RequestInputMode
    job_query: str | None = None
    filters: list[FilterCondition] = Field(default_factory=list)
    semantic_requirements: list[SemanticRequirement] = Field(default_factory=list)
    evaluation_preferences: list[str] = Field(default_factory=list)
    clarifications: list[Clarification] = Field(default_factory=list)


class TalentRequestInput(TypedDict):
    request_text: str


class TalentRequestOutput(TypedDict):
    status: RequestStatus
    input_mode: RequestInputMode
    job_matches: list[JobMatch]
    selected_job: JobMatch | None
    talent_request: dict[str, Any]
    query_plan: dict[str, Any]
    candidate_ids: list[str]
    condition_revisions: list[dict[str, Any]]
    plan_version: int
    clarifications: list[dict[str, str]]
    errors: list[str]


class TalentRequestState(TypedDict, total=False):
    request_text: str
    input_mode: RequestInputMode
    draft: dict[str, Any]
    job_matches: list[JobMatch]
    selected_job: JobMatch | None
    talent_request: dict[str, Any]
    query_plan: dict[str, Any]
    candidate_ids: list[str] # 候选人 IDs
    condition_revisions: Annotated[list[dict[str, Any]], add] # 用户的完整修订历史(add reducer 可追加)
    plan_version: int # 用户当前修订代数
    clarifications: list[dict[str, str]]
    status: RequestStatus
    errors: list[str]


RequestInterpreter = Callable[[str, JobMatch | None], TalentRequestDraft]
JobLookup = Callable[[str, DecisionContext], list[JobMatch]]
CandidateFilter = Callable[[list[FilterCondition], DecisionContext], list[str]]


def _receive_request(state: TalentRequestState, runtime: Runtime[DecisionContext]) -> dict[str, Any]:
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


def _interpret_input(request_interpreter: RequestInterpreter):
    def interpret_input(state: TalentRequestState) -> dict[str, Any]:
        draft = request_interpreter(state["request_text"], None)
        return {
            "draft": draft.model_dump(mode="json"),
            "input_mode": draft.input_mode,
            "status": "request_understood",
        }

    return interpret_input


def _route_input(state: TalentRequestState) -> Literal["lookup_jobs", "build_plan"]:
    return "lookup_jobs" if state["input_mode"] == "job_name" else "build_plan"


def _lookup_jobs(job_lookup: JobLookup):
    def lookup_jobs(state: TalentRequestState, runtime: Runtime[DecisionContext]) -> dict[str, Any]:
        job_query = str(state["draft"].get("job_query") or state["request_text"])
        matches = job_lookup(job_query, runtime.context)
        return {"job_matches": matches, "status": "jobs_found"}

    return lookup_jobs


def _route_job_matches(
    state: TalentRequestState,
) -> Literal["select_exact_job", "confirm_job", "no_job_match"]:
    exact_matches = [item for item in state["job_matches"] if item["match_type"] == "exact"]
    if len(exact_matches) == 1:
        return "select_exact_job"
    if state["job_matches"]:
        return "confirm_job"
    return "no_job_match"


def _select_exact_job(state: TalentRequestState) -> dict[str, Any]:
    selected = next(item for item in state["job_matches"] if item["match_type"] == "exact")
    return {"selected_job": selected, "status": "job_confirmed"}


def _confirm_job(state: TalentRequestState) -> dict[str, Any]:
    selection = interrupt(
        {
            "type": "job_selection",
            "question": "请选择本次人才评估使用的岗位 JD",
            "request_text": state["request_text"],
            "options": [
                {
                    "job_code": item["job_code"],
                    "name": item["name"],
                    "match_score": item["match_score"],
                    "match_type": item["match_type"],
                    "version": item["version"],
                }
                for item in state["job_matches"]
            ],
            "actions": ["select", "cancel"],
        }
    )
    if not isinstance(selection, dict):
        raise ValueError("恢复数据必须是 JSON 对象")
    # 作业的另外 select 或 cancel 状态选择 实现
    if selection.get("action") == "cancel":
        return {"status": "cancelled"}
    if selection.get("action") != "select":
        raise ValueError("action 必须是 select 或 cancel")
    selected_code = selection.get("job_code")
    selected = next(
        (item for item in state["job_matches"] if item["job_code"] == selected_code),
        None,
    )
    if selected is None:
        raise ValueError("恢复数据中的 job_code 不在待确认岗位列表中")
    return {"selected_job": selected, "status": "job_confirmed"} # 正常选择后 status 设置为下一个节点


def _route_job_confirmation(state: TalentRequestState) -> str:
    return "compile_selected_job" if state["status"] == "job_confirmed" else END


def _compile_selected_job(request_interpreter: RequestInterpreter):
    def compile_selected_job(state: TalentRequestState) -> dict[str, Any]:
        draft = request_interpreter(state["request_text"], state["selected_job"])
        return {
            "draft": draft.model_dump(mode="json"),
            "input_mode": "job_name",
        }

    return compile_selected_job


def _build_plan(state: TalentRequestState) -> dict[str, Any]:
    draft = TalentRequestDraft.model_validate(state["draft"])
    selected_job = state.get("selected_job")
    target_job: TargetJob | None = None
    if selected_job is not None:
        target_job = {
            "job_code": selected_job["job_code"],
            "name": selected_job["name"],
            "version": selected_job["version"],
        }

    talent_request = {
        "original_text": state["request_text"],
        "task_type": "evaluate_and_recommend",
        "source": state["input_mode"],
        "target_job": target_job,
        "hard_conditions": [item.model_dump(mode="json") for item in draft.filters],
        "semantic_conditions": [
            item.model_dump(mode="json") for item in draft.semantic_requirements
        ],
        "evaluation_preferences": draft.evaluation_preferences,
    }
    query_plan = QueryPlan(
        task_type=TaskType.FIND_TALENT,
        filters=draft.filters,
        semantic_requirements=draft.semantic_requirements,
        preferences=draft.evaluation_preferences,
        clarifications=draft.clarifications, # 构建plan的时候就会确定是否需要走到后续的澄清节点
    )
    status: RequestStatus = "plan_ready" if query_plan.executable else "clarification_required"
    return {
        "talent_request": talent_request,
        "query_plan": query_plan.model_dump(mode="json"),
        "clarifications": [item.model_dump(mode="json") for item in draft.clarifications],
        "status": status,
    }


def _no_job_match(state: TalentRequestState) -> dict[str, Any]:
    clarification = {
        "expression": state["request_text"],
        "reason": "未找到可确认的岗位 JD",
    }
    return {
        "query_plan": {},
        "talent_request": {},
        "clarifications": [clarification],
        "status": "no_job_match",
    }


# ---------------------------------------------------------------------------
# 第 17 课：候选人检索与可恢复人机协同
# ---------------------------------------------------------------------------


def _route_after_plan(state: TalentRequestState) -> str:
    if state["status"] == "clarification_required":  #
        return "resolve_requirements"
    return "filter_candidates"


def _route_candidates(state: TalentRequestState) -> str:
    if state["status"] == "candidates_empty":
        return "resolve_requirements"
    return END


def _route_resolution(state: TalentRequestState) -> str:
    if state["status"] == "request_revised":
        return "interpret_input"
    return END


def _await_decision(payload: dict[str, Any], parse: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
    """等待恢复数据并完成校验

    校验失败时不抛出异常，而是携带 resume_error 重新 interrupt，
    让 Thread 停留在暂停点等待下一次提交，避免节点失败导致线程卡死
    """
    decision = interrupt(payload)                                       # A：首次暂停(打断停在这)
    # 打断后恢复：
    # 第一次中断后恢复会从当前"_resolve_requirements"节点第一行开始重新执行
    # 然后打断后用户输入的内容 赋值到上面的 decision 参数(打断的作用我可以粗略的理解最主要是用户输入修改的内容返回赋值到decision参数上)

    while True:
        try:
            return parse(decision)                                      # B：校验，成功就退出函数
        except ValueError as exc:
            # parse 失败时不让节点终止；
            # 重新 interrupt，附加 resume_error
            decision = interrupt({**payload, "resume_error": str(exc)}) # C：校验失败，再次暂停


def _require_interaction(decision: Any, expected_id: str) -> dict[str, Any]:
    # 防止传入 decision(交互定义的payload) 是非对象恢复值
    if not isinstance(decision, dict):
        raise ValueError("恢复数据必须是 JSON 对象")

    # 确保打断点id和恢复点id相同
    if decision.get("interaction_id") != expected_id:
        raise ValueError(f"恢复数据与当前暂停点不匹配，期望 interaction_id={expected_id}")
    return decision


def _filter_candidates_node(candidate_filter: CandidateFilter):
    def filter_candidates(state: TalentRequestState, runtime: Runtime[DecisionContext]) -> dict[str, Any]:
        plan = QueryPlan.model_validate(state["query_plan"])
        # 检索候选人ids列表是否存在
        candidate_ids = candidate_filter(plan.filters, runtime.context)
        if candidate_ids:
            # 候选人存在则返回 "status": "candidates_ready" 正常走向下一个节点
            return {"candidate_ids": candidate_ids, "status": "candidates_ready"}
        # 候选人不存在，则返回 "status": "candidates_empty"
        return {"candidate_ids": [], "status": "candidates_empty"}

    return filter_candidates


def _diagnose_filters(
    filters: list[FilterCondition],
    candidate_filter: CandidateFilter,
    context: DecisionContext,
) -> list[dict[str, Any]]:
    """leave-one-out 诊断：逐个移除条件，观察候选数变化，定位导致空集的条件"""
    diagnostics = []
    for index, condition in enumerate(filters):
        reduced = filters[:index] + filters[index + 1 :]
        hit_count = len(candidate_filter(reduced, context))
        diagnostics.append(
            {
                "field": str(condition.field),
                "operator": condition.operator,
                "value": condition.value,
                "candidates_without_condition": hit_count,
            }
        )
    return diagnostics

# Node Human In The Loop (lesson-17重点关键节点)
def _resolve_requirements():
    def resolve_requirements(
        state: TalentRequestState,
        runtime: Runtime[DecisionContext],
    ) -> dict[str, Any]:
        plan_version = state.get("plan_version", 0)
        interaction_id = f"rr-v{plan_version}"
        reason_code = "plan_blocked" if state["status"] == "clarification_required" else "empty_candidates"
        filters = state.get("draft", {}).get("filters", [])
        # 两种需要 Human In The Loop 的情况原因
        if reason_code == "plan_blocked":
            blocking_reason = "当前要求存在歧义或冲突，无法生成可执行查询计划"
        else:
            rendered = "、".join(
                f"{item['field']} {item['operator']} {item['value']}" for item in filters
            )
            blocking_reason = f"使用以下结构化条件筛选后未命中候选人：{rendered}"
        selected_job = state.get("selected_job")
        if selected_job is not None:
            # 如果已经选中岗位
            editable_request = selected_job["content"]
            request_source = "job_profile"
            selected_job_source: dict[str, Any] | None = {
                "job_code": selected_job["job_code"],
                "name": selected_job["name"],
                "version": selected_job["version"],
            }
        else:
            editable_request = state["request_text"]
            request_source = "user_request"
            selected_job_source = None
        # payload 是人机协作接口契约
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

        def parse(decision: Any) -> dict[str, Any]:
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

        parsed = _await_decision(payload, parse) # 返回值为 parse() 返回值
        if parsed["action"] == "cancel":
            return {"status": "cancelled"}

        revision_text = parsed["revision_text"]
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
            "plan_version": plan_version + 1, # 接受一次人工修订后 version 自增
            "status": "request_revised",
        }

    return resolve_requirements


def _apply_relaxations(
    current: list[FilterCondition],
    relaxations: list[dict[str, Any]],
) -> tuple[list[FilterCondition], list[dict[str, Any]]]:
    updated = list(current)
    changes: list[dict[str, Any]] = []
    for item in relaxations:
        field = item.get("field")
        op = item.get("op")
        index = next((i for i, cond in enumerate(updated) if str(cond.field) == field), None)
        if index is None:
            raise ValueError(f"放宽目标条件不存在：{field}")
        if op == "remove":
            removed = updated.pop(index)
            changes.append({"field": field, "op": "remove", "removed": removed.model_dump(mode="json")})
        elif op == "update":
            replacement = FilterCondition.model_validate(
                {"field": field, "operator": item.get("operator"), "value": item.get("value")}
            )
            previous = updated[index]
            updated[index] = replacement
            changes.append(
                {
                    "field": field,
                    "op": "update",
                    "previous": previous.model_dump(mode="json"),
                    "current": replacement.model_dump(mode="json"),
                }
            )
        else:
            raise ValueError(f"不支持的放宽操作：{op}")
    return updated, changes

MAX_REQUIREMENT_REVISIONS = 3 # 作业1：HumanInTheLoop 最大修订次数

def _resolve_conditions(state: TalentRequestState) -> dict[str, Any]:
    plan_version = state.get("plan_version", 0)
    # 作业1 ： 加个对state里condition_revisions参数的长度校验，因为是add的reducer
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

    interaction_id = f"cc-v{plan_version}"
    draft = dict(state["draft"])
    payload = {
        "type": "condition_clarification",
        "interaction_id": interaction_id,
        "question": "以下条件无法直接执行，请修订后重新提交，或取消本次任务",
        "clarifications": state["clarifications"],
        "current_filters": draft.get("filters", []),
        "current_semantic_requirements": draft.get("semantic_requirements", []),
        "actions": ["revise", "cancel"],
    }

    def parse(decision: Any) -> dict[str, Any]:
        decision = _require_interaction(decision, interaction_id)
        action = decision.get("action")
        if action == "cancel":
            return {"action": "cancel"}
        if action != "revise":
            raise ValueError("action 必须是 revise 或 cancel")
        parsed: dict[str, Any] = {"action": "revise"}
        if "filters" in decision:
            try:
                parsed["filters"] = [
                    FilterCondition.model_validate(item) for item in decision["filters"]
                ]
            except ValidationError as exc:
                raise ValueError(f"filters 校验失败：{exc.errors()[0]['msg']}") from exc
        if "semantic_requirements" in decision:
            items = decision["semantic_requirements"]
            if not isinstance(items, list) or any(not isinstance(item, dict) or not item.get("query") for item in items):
                raise ValueError("semantic_requirements 的每一项都必须包含 query")
            parsed["semantic_requirements"] = [
                SemanticRequirement(
                    requirement_id=f"S{index + 1}",
                    query=str(item["query"]),
                    required=bool(item.get("required", True)),
                )
                for index, item in enumerate(items)
            ]
        if "filters" not in parsed and "semantic_requirements" not in parsed:
            raise ValueError("revise 动作必须携带 filters 或 semantic_requirements")
        return parsed

    parsed = _await_decision(payload, parse)
    if parsed["action"] == "cancel":
        return {"status": "cancelled"}

    changes: list[dict[str, Any]] = []
    invalidates: set[str] = set()
    if "filters" in parsed:
        draft["filters"] = [item.model_dump(mode="json") for item in parsed["filters"]]
        changes.append({"kind": "filters", "count": len(parsed["filters"])})
        invalidates.add("candidate_set")
    if "semantic_requirements" in parsed:
        draft["semantic_requirements"] = [
            item.model_dump(mode="json") for item in parsed["semantic_requirements"]
        ]
        changes.append({"kind": "semantic_requirements", "count": len(parsed["semantic_requirements"])})
        invalidates.update(["evidence_retrieval", "evaluation_dimensions", "evaluation_report"])

    resolved = list(draft.get("clarifications", []))
    draft["clarifications"] = []
    revision = {
        "interaction_id": interaction_id,
        "action": "revise",
        "changes": changes,
        "resolved_clarifications": resolved,
        "invalidates": sorted(invalidates),
        "plan_version": plan_version + 1,
    }
    return {
        "draft": draft,
        "condition_revisions": [revision],
        "plan_version": plan_version + 1,
        "status": "plan_revised",
    }


def _resolve_empty_candidates(candidate_filter: CandidateFilter):
    def resolve_empty_candidates(
        state: TalentRequestState,
        runtime: Runtime[DecisionContext],
    ) -> dict[str, Any]:
        plan = QueryPlan.model_validate(state["query_plan"])
        plan_version = state.get("plan_version", 0)
        interaction_id = f"ec-v{plan_version}"
        diagnostics = _diagnose_filters(plan.filters, candidate_filter, runtime.context)
        current = [
            FilterCondition.model_validate(item) for item in state["draft"].get("filters", [])
        ]
        payload = {
            "type": "empty_candidate_set",
            "interaction_id": interaction_id,
            "question": "当前条件未命中任何候选人，可放宽部分条件后重试，或取消本次任务",
            "filters": diagnostics,
            "actions": ["relax", "cancel"],
        }

        def parse(decision: Any) -> dict[str, Any]:
            decision = _require_interaction(decision, interaction_id)
            action = decision.get("action")
            if action == "cancel":
                return {"action": "cancel"}
            if action != "relax":
                raise ValueError("action 必须是 relax 或 cancel")
            relaxations = decision.get("relaxations") or []
            if not relaxations:
                raise ValueError("relax 动作必须携带 relaxations")
            updated, changes = _apply_relaxations(current, relaxations)
            return {"action": "relax", "updated": updated, "changes": changes}

        parsed = _await_decision(payload, parse)
        if parsed["action"] == "cancel":
            return {"status": "cancelled"}

        draft = dict(state["draft"])
        draft["filters"] = [item.model_dump(mode="json") for item in parsed["updated"]]
        revision = {
            "interaction_id": interaction_id,
            "action": "relax",
            "changes": parsed["changes"],
            "resolved_clarifications": [],
            "invalidates": ["candidate_set"],
            "plan_version": plan_version + 1,
        }
        return {
            "draft": draft,
            "condition_revisions": [revision],
            "plan_version": plan_version + 1,
            "status": "plan_revised",
        }

    return resolve_empty_candidates


def build_talent_request_graph(
    *,
    request_interpreter: RequestInterpreter,
    job_lookup: JobLookup,
    candidate_filter: CandidateFilter | None = None,
    checkpointer: Any | None = None,
):
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
    builder.add_edge(START, "receive_request")
    builder.add_edge("receive_request", "interpret_input")
    builder.add_conditional_edges("interpret_input", _route_input)
    builder.add_conditional_edges("lookup_jobs", _route_job_matches)
    builder.add_edge("select_exact_job", "compile_selected_job")
    builder.add_conditional_edges(
        "confirm_job",
        _route_job_confirmation,
        {"compile_selected_job": "compile_selected_job", END: END},
    )
    builder.add_edge("compile_selected_job", "build_plan")
    builder.add_edge("no_job_match", END)

    if candidate_filter is None:
        # 若为None，则为14课原路径
        # 第 14 课行为：编译出 QueryPlan 即结束，候选人检索与澄清节点不挂载
        builder.add_edge("build_plan", END)
    else:
        # 本节17课路径，
        builder.add_node("filter_candidates", _filter_candidates_node(candidate_filter))
        builder.add_node("resolve_requirements", _resolve_requirements())
        builder.add_conditional_edges(
            "build_plan",
            _route_after_plan,
            {"resolve_requirements": "resolve_requirements", "filter_candidates": "filter_candidates"},
        )
        builder.add_conditional_edges(
            "filter_candidates",
            _route_candidates, # candidates 候选人在DB为空则走 resolve_requirements 节点
            {"resolve_requirements": "resolve_requirements", END: END},
        )
        builder.add_conditional_edges(
            "resolve_requirements",
            _route_resolution,
            {"interpret_input": "interpret_input", END: END},
        )
    return builder.compile(checkpointer=checkpointer) if checkpointer is not None else builder.compile()


MODEL_SYSTEM_PROMPT = """你是人才评估任务编译器。输出 TalentRequestDraft。
如果输入只有岗位名称或岗位简称，input_mode 使用 job_name，只填写 job_query，不根据岗位名称猜测条件。
如果输入包含地区、年限、职级、技能、项目经验或偏好，input_mode 使用 detailed_requirement。
结构化硬条件只能写入 filters。技能、经历和成果写入 semantic_requirements。优先项写入 evaluation_preferences。
只有歧义或矛盾导致无法形成可执行查询计划时才写入 clarifications。资深、经验丰富等可以用于语义检索或排序的描述写入 semantic_requirements 或 evaluation_preferences，不要求补充年龄等结构化阈值。租户和权限不得从用户文本提取。
当输入中包含 confirmed_job 时，按照已确认岗位 JD 编译条件，并保留用户补充要求。"""


def model_request_interpreter(request_text: str, selected_job: JobMatch | None = None) -> TalentRequestDraft:
    model = get_chat_model(temperature=0)
    if model is None:
        raise RuntimeError("任务理解模型未配置，请设置 DASHSCOPE_API_KEY")
    user_content = request_text
    if selected_job is not None:
        user_content = (
            f"original_request={request_text}\n"
            f"confirmed_job={selected_job['name']}\n"
            f"job_description={selected_job['content']}"
        )
    return model.with_structured_output(TalentRequestDraft).invoke(
        [("system", MODEL_SYSTEM_PROMPT), ("user", user_content)]
    )


def database_job_lookup(query: str, context: DecisionContext) -> list[JobMatch]:
    service = TalentToolService(session_factory=SessionLocal)
    tool_context = TalentToolContext(
        tenant_id=context.tenant_id,
        permission_scopes=context.permission_scopes,
        actor_id="langsmith-studio",
        run_id="lesson-14",
    )
    return service.lookup_job_descriptions(query, context=tool_context)


def database_candidate_filter(filters: list[FilterCondition], context: DecisionContext) -> list[str]:
    service = TalentToolService(session_factory=SessionLocal)
    tool_context = TalentToolContext(
        tenant_id=context.tenant_id,
        permission_scopes=context.permission_scopes,
        actor_id="langsmith-studio",
        run_id="lesson-17",
    )
    return service.filter_candidates(filters, context=tool_context)


# Agent Server injects its managed checkpointer when this graph is loaded from langgraph.json.
graph = build_talent_request_graph(
    request_interpreter=model_request_interpreter,
    job_lookup=database_job_lookup,
    candidate_filter=database_candidate_filter,
)
