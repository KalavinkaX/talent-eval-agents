from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from app.database import SessionLocal
from app.model_provider import get_chat_model
from app.query_plan import Clarification, FilterCondition, QueryPlan, SemanticRequirement, TaskType
from app.talent_decision_graph import DecisionContext
from app.talent_tools import TalentToolContext, TalentToolService


RequestInputMode = Literal["detailed_requirement", "job_name"] # 通过LLM 判断用户input是 职位描述(无具体职位) 还是 具体职位
RequestStatus = Literal[
    "received",
    "request_understood",
    "jobs_found",
    "job_confirmed",
    "plan_ready",
    "clarification_required",
    "cancelled", # 作业1(第1部分)：添加取消状态
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
    clarifications: list[dict[str, str]]
    status: RequestStatus
    errors: list[str]


RequestInterpreter = Callable[[str, JobMatch | None], TalentRequestDraft]
JobLookup = Callable[[str, DecisionContext], list[JobMatch]]

# Context 校验
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
        "clarifications": [],
        "status": "received",
        "errors": [],
    }

# 从query 输出受控草稿 TalentRequestDraft 对象 (后面的 model_request_interpreter()方法LLM提取)
def _interpret_input(request_interpreter: RequestInterpreter):
    def interpret_input(state: TalentRequestState) -> dict[str, Any]:
        draft = request_interpreter(state["request_text"], None)
        return {
            "draft": draft.model_dump(mode="json"),
            "input_mode": draft.input_mode,
            "status": "request_understood",
        }

    return interpret_input

# 路由节点：判断用户input是 职位描述(无具体职位) 还是 具体职位
def _route_input(state: TalentRequestState) -> Literal["lookup_jobs", "build_plan"]:
    return "lookup_jobs" if state["input_mode"] == "job_name" else "build_plan"

# 主要是通过 Runtime (后面的 database_job_lookup()方法找到职位结构化数据)
def _lookup_jobs(job_lookup: JobLookup):
    def lookup_jobs(state: TalentRequestState, runtime: Runtime[DecisionContext]) -> dict[str, Any]:
        job_query = str(state["draft"].get("job_query") or state["request_text"])
        matches = job_lookup(job_query, runtime.context)
        return {"job_matches": matches, "status": "jobs_found"}

    return lookup_jobs


# 对于前面找到相关职位match的后续路由节点
def _route_job_matches(
    state: TalentRequestState,
) -> Literal["select_exact_job", "confirm_job", "no_job_match"]:
    exact_matches = [item for item in state["job_matches"] if item["match_type"] == "exact"]
    # exact 非常精确，不需后续 Human In The Loop 暂停 传入选择后 手动确认。直接走到 select_exact_job 节点
    if len(exact_matches) == 1:
        return "select_exact_job"
    # 职位选择不够精确，需要后续 Human In The Loop 暂停 传入选择后 手动确认。之后走到 confirm_job 节点
    if state["job_matches"]:
        return "confirm_job"
    # 未匹配到相关职位，直接走到 no_job_match
    return "no_job_match"


def _select_exact_job(state: TalentRequestState) -> dict[str, Any]:
    selected = next(item for item in state["job_matches"] if item["match_type"] == "exact")
    return {"selected_job": selected, "status": "job_confirmed"}


def _confirm_job(state: TalentRequestState) -> dict[str, Any]:
    # 中断后若恢复，从该节点第一行开始重新执行，所以不能把写数据库等操作放在方法开头位置，否则可能会重复执行
    selection = interrupt(
        {
            "type": "job_selection",
            "question": "请选择本次人才评估使用的岗位 JD",
            "request_text": state["request_text"],
            # 中断后给用户的职位可选项(从State中取)
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
        }
    )
    if not isinstance(selection, dict) or selection.get("action") != "select":
        raise ValueError("恢复数据必须包含 action=select")
    selected_code = selection.get("job_code")
    selected = next(
        (item for item in state["job_matches"] if item["job_code"] == selected_code),
        None,
    )
    # 作业2：这里就是Human输入job_code不在候选类表里的情况...
    if selected is None:
        raise ValueError("恢复数据中的 job_code 不在待确认岗位列表中")
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
    return {"selected_job": selected, "status": "job_confirmed"}

# # 作业1(第2部分)：为岗位确认增加cancel动作，并为取消路径补充状态与测试
# def _confirm_job(state: TalentRequestState) -> dict[str, Any]:
#     # 中断后若恢复，从该节点第一行开始重新执行，所以不能把写数据库等操作放在方法开头位置，否则可能会重复执行
#     selection = interrupt(
#         {
#             "type": "job_selection",
#             "question": "请选择本次人才评估使用的岗位 JD",
#             "request_text": state["request_text"],
#             # 中断后给用户的职位可选项(从State中取)
#             "options": [
#                 {
#                     "job_code": item["job_code"],
#                     "name": item["name"],
#                     "match_score": item["match_score"],
#                     "match_type": item["match_type"],
#                     "version": item["version"],
#                 }
#                 for item in state["job_matches"]
#             ],
#         }
#     )
#     # 提取Human输入后恢复的键值对
#     if not isinstance(selection, dict):
#         raise ValueError("恢复数据必须是键值对对象")
#     # ! 用户取消 直接返回
#     if selection.get("action") == "cancel":
#         return {
#         "selected_job": None,
#         "talent_request": {},
#         "query_plan": {},
#         "clarifications": [],
#         "errors": [],
#         "status": "cancelled",
#     }
#
#     if selection.get("action") != "select":
#         raise ValueError("恢复数据必须包含 action=select 或 action=cancel")
#     selected_code = selection.get("job_code")
#     selected = next(
#         (item for item in state["job_matches"] if item["job_code"] == selected_code),
#         None,
#     )
#     if selected is None:
#         raise ValueError("恢复数据中的 job_code 不在待确认岗位列表中")
#     return {"selected_job": selected, "status": "job_confirmed"}

# # 作业1(第3部分)：节点边还要改为条件边
# builder.add_edge("confirm_job", "compile_selected_job")
# def _route_after_confirmation(state):
#     return (
#         "compile_selected_job"
#         if state["status"] == "job_confirmed"
#         else END
#     )
#
# builder.add_conditional_edges(
#     "confirm_job",
#     _route_after_confirmation,
#     {
#         "compile_selected_job": "compile_selected_job",
#         END: END,
#     },
# )

# 第2次LLM提取(也是通过传入model_request_interpreter()方法)：把 original_request、confirmed_job 和 job_description 组合给模型，再从已确认 JD 提取具体要求。
# 注意输出 input_mode 被强制保留为 job_name：它标明最初的输入来源
# 第二次 Draft 可以包含 JD 结构化检索(_lookup_jobs节点) 解析出的详细条件(和第1次不同点)
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
    # 构建 talent_request 业务语义对象
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
    # 构建 QueryPlan 执行协议对象(后续检索要用到)
    query_plan = QueryPlan(
        task_type=TaskType.FIND_TALENT,
        filters=draft.filters,
        semantic_requirements=draft.semantic_requirements,
        preferences=draft.evaluation_preferences,
        clarifications=draft.clarifications,
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
        "status": "clarification_required",
    }


def build_talent_request_graph(
    *,
    request_interpreter: RequestInterpreter,
    job_lookup: JobLookup,
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
    builder.add_edge("confirm_job", "compile_selected_job")
    builder.add_edge("compile_selected_job", "build_plan")
    builder.add_edge("build_plan", END)
    builder.add_edge("no_job_match", END)
    return builder.compile(checkpointer=checkpointer) if checkpointer is not None else builder.compile()


MODEL_SYSTEM_PROMPT = """你是人才评估任务编译器。输出 TalentRequestDraft。
如果输入只有岗位名称或岗位简称，input_mode 使用 job_name，只填写 job_query，不根据岗位名称猜测条件。
如果输入包含地区、年限、职级、技能、项目经验或偏好，input_mode 使用 detailed_requirement。
结构化硬条件只能写入 filters。技能、经历和成果写入 semantic_requirements。优先项写入 evaluation_preferences。
含糊、缺失阈值或互相矛盾的条件写入 clarifications。租户和权限不得从用户文本提取。
当输入中包含 confirmed_job 时，按照已确认岗位 JD 编译条件，并保留用户补充要求。"""

# 对于第一次模型调用，产 Draft 而非最终计划
# 这里能在TalentRequestDraft里的 RequestInputMode 写明判断用户input是 职位描述(无具体职位) 还是 具体职位
# 以便 _interpret_input -> _route_input 路由到 按职位查找 还是 按职位描述(无具体职位) 链路
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


# lookup_jobs节点的传入参数，找到具体符合的岗位内容。通过 TalentToolService 服务层调用
def database_job_lookup(query: str, context: DecisionContext) -> list[JobMatch]:
    service = TalentToolService(session_factory=SessionLocal)
    tool_context = TalentToolContext(
        tenant_id=context.tenant_id,
        permission_scopes=context.permission_scopes,
        actor_id="langsmith-studio",
        run_id="lesson-14",
    )
    return service.lookup_job_descriptions(query, context=tool_context)


# Agent Server injects its managed checkpointer when this graph is loaded from langgraph.json.
graph = build_talent_request_graph(
    request_interpreter=model_request_interpreter,
    job_lookup=database_job_lookup,
)
