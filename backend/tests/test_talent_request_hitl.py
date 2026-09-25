from __future__ import annotations

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.query_plan import Clarification, FilterCondition, SemanticRequirement
from app.talent_decision_graph import DecisionContext
from app.talent_request_graph import TalentRequestDraft, build_talent_request_graph


CANDIDATES = [
    {"id": "C001", "region": "上海", "years_of_experience": 6, "age": 29},
    {"id": "C002", "region": "上海", "years_of_experience": 12, "age": 36},
    {"id": "C003", "region": "深圳", "years_of_experience": 8, "age": 33},
]


def _matches(candidate: dict, condition: FilterCondition) -> bool:
    value = candidate.get(str(condition.field))
    if value is None:
        return False
    target = condition.value
    if condition.operator == "eq":
        return value == target
    if condition.operator == "gte":
        return value >= target
    if condition.operator == "gt":
        return value > target
    if condition.operator == "lte":
        return value <= target
    if condition.operator == "lt":
        return value < target
    if condition.operator == "in":
        return value in target
    return False


class StubCandidateFilter:
    def __init__(self) -> None:
        self.calls: list[list[dict]] = []

    def __call__(self, filters: list[FilterCondition], context: DecisionContext) -> list[str]:
        self.calls.append([item.model_dump(mode="json") for item in filters])
        return [
            item["id"]
            for item in CANDIDATES
            if all(_matches(item, condition) for condition in filters)
        ]


class HitlInterpreter:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, request_text: str, selected_job: dict | None = None) -> TalentRequestDraft:
        self.calls.append(request_text)
        if "上海或杭州 5 年以上" in request_text:
            return TalentRequestDraft(
                input_mode="detailed_requirement",
                filters=[
                    FilterCondition(field="region", operator="in", value=["上海", "杭州"]),
                    FilterCondition(field="years_of_experience", operator="gte", value=5),
                ],
            )
        if "15 年" in request_text:
            return TalentRequestDraft(
                input_mode="detailed_requirement",
                filters=[
                    FilterCondition(field="region", operator="eq", value="上海"),
                    FilterCondition(field="years_of_experience", operator="gte", value=15),
                ],
            )
        if "矛盾" in request_text:
            return TalentRequestDraft(
                input_mode="detailed_requirement",
                filters=[
                    FilterCondition(field="age", operator="lt", value=30),
                    FilterCondition(field="age", operator="gt", value=40),
                ],
                clarifications=[
                    Clarification(expression="年龄小于 30 且大于 40", reason="条件互相矛盾")
                ],
            )
        return TalentRequestDraft(
            input_mode="detailed_requirement",
            filters=[
                FilterCondition(field="region", operator="eq", value="上海"),
                FilterCondition(field="years_of_experience", operator="gte", value=6),
            ],
            semantic_requirements=[
                SemanticRequirement(requirement_id="S1", query="企业知识库经验", required=True)
            ],
        )


def _context() -> DecisionContext:
    return DecisionContext(tenant_id="course-demo", permission_scopes=("hr_private",))


def _build(interpreter: HitlInterpreter, candidate_filter: StubCandidateFilter):
    return build_talent_request_graph(
        request_interpreter=interpreter,
        job_lookup=lambda query, context: [],
        candidate_filter=candidate_filter,
        checkpointer=InMemorySaver(),
    )


def test_executable_plan_reaches_candidates_ready_without_interrupt():
    interpreter = HitlInterpreter()
    candidate_filter = StubCandidateFilter()
    graph = _build(interpreter, candidate_filter)

    result = graph.invoke(
        {"request_text": "筛选上海 6 年以上的人才"},
        {"configurable": {"thread_id": "hitl-ready"}},
        context=_context(),
    )

    assert result["status"] == "candidates_ready"
    assert result["candidate_ids"] == ["C001", "C002"]
    assert result["plan_version"] == 0
    assert result["condition_revisions"] == []
    assert "__interrupt__" not in result


def test_plan_block_and_empty_result_use_one_free_text_revision_node():
    interpreter = HitlInterpreter()
    candidate_filter = StubCandidateFilter()
    graph = _build(interpreter, candidate_filter)

    blocked_config = {"configurable": {"thread_id": "hitl-unified-blocked"}}
    blocked = graph.invoke(
        {"request_text": "年龄条件矛盾的人才"}, blocked_config, context=_context()
    )
    blocked_payload = blocked["__interrupt__"][0].value
    assert graph.get_state(blocked_config).next == ("resolve_requirements",)
    assert blocked_payload["type"] == "requirement_revision"
    assert blocked_payload["reason_code"] == "plan_blocked"

    empty_config = {"configurable": {"thread_id": "hitl-unified-empty"}}
    empty = graph.invoke(
        {"request_text": "筛选上海 15 年以上的人才"}, empty_config, context=_context()
    )
    empty_payload = empty["__interrupt__"][0].value
    assert graph.get_state(empty_config).next == ("resolve_requirements",)
    assert empty_payload["type"] == "requirement_revision"
    assert empty_payload["reason_code"] == "empty_candidates"
    assert "region eq 上海" in empty_payload["blocking_reason"]
    assert "years_of_experience gte 15" in empty_payload["blocking_reason"]

    resumed = graph.invoke(
        Command(
            resume={
                "interaction_id": empty_payload["interaction_id"],
                "action": "revise",
                "revision_text": "筛选上海或杭州 5 年以上工作经验的人才",
            }
        ),
        empty_config,
        context=_context(),
    )
    assert resumed["status"] == "candidates_ready"
    final_state = graph.get_state(empty_config).values
    assert final_state["request_text"] == "筛选上海或杭州 5 年以上工作经验的人才"


def test_job_profile_conflict_payload_uses_full_confirmed_job_content():
    job_content = "负责 AI 应用研发，工作地点以上海或杭州为准，具体地点待确认"

    class JobProfileInterpreter:
        def __call__(self, request_text: str, selected_job: dict | None = None) -> TalentRequestDraft:
            if selected_job is None:
                return TalentRequestDraft(input_mode="job_name", job_query=request_text)
            return TalentRequestDraft(
                input_mode="job_name",
                job_query=request_text,
                clarifications=[
                    Clarification(
                        expression="工作地点以上海或杭州为准",
                        reason="岗位画像没有确定唯一工作地点",
                    )
                ],
            )

    job = {
        "job_code": "JD-AI-009",
        "name": "AI 应用工程师",
        "match_score": 1.0,
        "match_type": "exact",
        "version": 3,
        "content": job_content,
    }
    graph = build_talent_request_graph(
        request_interpreter=JobProfileInterpreter(),
        job_lookup=lambda query, context: [job],
        candidate_filter=StubCandidateFilter(),
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "hitl-job-profile-conflict"}}

    interrupted = graph.invoke({"request_text": "AI 应用工程师"}, config, context=_context())
    payload = interrupted["__interrupt__"][0].value

    assert payload["request_text"] == job_content
    assert payload["request_source"] == "job_profile"
    assert payload["selected_job"] == {
        "job_code": "JD-AI-009",
        "name": "AI 应用工程师",
        "version": 3,
    }


def test_stale_interaction_id_reasks_and_thread_stays_resumable():
    interpreter = HitlInterpreter()
    candidate_filter = StubCandidateFilter()
    graph = _build(interpreter, candidate_filter)
    config = {"configurable": {"thread_id": "hitl-stale"}}

    graph.invoke({"request_text": "筛选上海 15 年以上的人才"}, config, context=_context())

    rejected = graph.invoke(
        Command(
            resume={
                "interaction_id": "rr-v9",
                "action": "revise",
                "revision_text": "筛选上海或杭州 5 年以上工作经验的人才",
            }
        ),
        config,
        context=_context(),
    )

    payload = rejected["__interrupt__"][0].value
    assert "不匹配" in payload["resume_error"]
    # 校验失败后线程不终止，而是携带错误原因重新暂停，仍可继续提交
    assert rejected["__interrupt__"][0].value["interaction_id"] == "rr-v0"

    resumed = graph.invoke(
        Command(
            resume={
                "interaction_id": "rr-v0",
                "action": "revise",
                "revision_text": "筛选上海或杭州 5 年以上工作经验的人才",
            }
        ),
        config,
        context=_context(),
    )
    assert resumed["status"] == "candidates_ready"
    assert resumed["candidate_ids"] == ["C001", "C002"]


def test_empty_revision_text_reasks_with_error():
    interpreter = HitlInterpreter()
    candidate_filter = StubCandidateFilter()
    graph = _build(interpreter, candidate_filter)
    config = {"configurable": {"thread_id": "hitl-unknown-field"}}

    graph.invoke({"request_text": "筛选上海 15 年以上的人才"}, config, context=_context())

    rejected = graph.invoke(
        Command(
            resume={
                "interaction_id": "rr-v0",
                "action": "revise",
                "revision_text": "   ",
            }
        ),
        config,
        context=_context(),
    )

    payload = rejected["__interrupt__"][0].value
    assert "非空 revision_text" in payload["resume_error"]
    assert rejected["__interrupt__"][0].value["interaction_id"] == "rr-v0"


def test_cancel_from_resolve_requirements_ends_run():
    interpreter = HitlInterpreter()
    candidate_filter = StubCandidateFilter()
    graph = _build(interpreter, candidate_filter)
    config = {"configurable": {"thread_id": "hitl-cancel"}}

    graph.invoke({"request_text": "年龄条件矛盾的人才"}, config, context=_context())
    result = graph.invoke(
        Command(resume={"interaction_id": "rr-v0", "action": "cancel"}),
        config,
        context=_context(),
    )

    assert result["status"] == "cancelled"
    assert result["candidate_ids"] == []
    assert graph.get_state(config).next == ()
