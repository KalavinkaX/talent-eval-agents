"""第 17 课：Human-in-the-Loop 与可恢复执行链路验证

注入固定任务解释器和内存候选人集合，覆盖：
- 可执行计划直达 candidates_ready
- 空候选集 interrupt、leave-one-out 诊断与放宽恢复
- 矛盾条件 interrupt 与修订恢复
- 过期 interaction_id 拒绝
- 取消路径与暂停时长检查
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.query_plan import Clarification, FilterCondition
from app.talent_decision_graph import DecisionContext
from app.talent_request_graph import TalentRequestDraft, build_talent_request_graph


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


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
    return False


class DemoCandidateFilter:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, filters: list[FilterCondition], context: DecisionContext) -> list[str]:
        self.calls.append([f"{item.field}{item.operator}{item.value}" for item in filters])
        return [
            item["id"]
            for item in CANDIDATES
            if all(_matches(item, condition) for condition in filters)
        ]


class DemoInterpreter:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, request_text: str, selected_job: dict | None = None) -> TalentRequestDraft:
        self.calls.append(request_text)
        if "筛选上海 5 年以上" in request_text:
            return TalentRequestDraft(
                input_mode="detailed_requirement",
                filters=[
                    FilterCondition(field="region", operator="eq", value="上海"),
                    FilterCondition(field="years_of_experience", operator="gte", value=5),
                ],
            )
        if request_text == "年龄不超过 40 的人才":
            return TalentRequestDraft(
                input_mode="detailed_requirement",
                filters=[FilterCondition(field="age", operator="lte", value=40)],
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
            filters=[FilterCondition(field="region", operator="eq", value="上海")],
        )


def run_verification() -> None:
    interpreter = DemoInterpreter()
    candidate_filter = DemoCandidateFilter()
    graph = build_talent_request_graph(
        request_interpreter=interpreter,
        job_lookup=lambda query, context: [],
        candidate_filter=candidate_filter,
        checkpointer=InMemorySaver(),
    )
    context = DecisionContext(tenant_id="course-demo", permission_scopes=("hr_private",))

    ready = graph.invoke(
        {"request_text": "筛选上海的人才"},
        {"configurable": {"thread_id": "lesson17-ready"}},
        context=context,
    )
    print(
        "\n[ready]\n",
        _json({"status": ready["status"], "candidate_ids": ready["candidate_ids"]}),
    )

    config = {"configurable": {"thread_id": "lesson17-empty"}}
    interrupted = graph.invoke({"request_text": "筛选上海 15 年以上的人才"}, config, context=context)
    payload = interrupted["__interrupt__"][0].value
    print(
        "\n[empty-interrupt]\n",
        _json(
            {
                "thread_id": config["configurable"]["thread_id"],
                "next": list(graph.get_state(config).next),
                "payload": payload,
            }
        ),
    )

    resumed = graph.invoke(
        Command(
            resume={
                "interaction_id": "rr-v0",
                "action": "revise",
                "revision_text": "筛选上海 5 年以上工作经验的人才",
            }
        ),
        config,
        context=context,
    )
    print(
        "\n[revise-resume]\n",
        _json(
            {
                "status": resumed["status"],
                "candidate_ids": resumed["candidate_ids"],
                "plan_version": resumed["plan_version"],
                "condition_revisions": resumed["condition_revisions"],
                "next": list(graph.get_state(config).next),
                "interpret_calls": len(interpreter.calls),
                "filter_calls": candidate_filter.calls,
            }
        ),
    )

    clarify_config = {"configurable": {"thread_id": "lesson17-clarify"}}
    clarify = graph.invoke({"request_text": "年龄条件矛盾的人才"}, clarify_config, context=context)
    print(
        "\n[clarify-interrupt]\n",
        _json({"payload": clarify["__interrupt__"][0].value}),
    )
    revised = graph.invoke(
        Command(
            resume={
                "interaction_id": "rr-v0",
                "action": "revise",
                "revision_text": "年龄不超过 40 的人才",
            }
        ),
        clarify_config,
        context=context,
    )
    print(
        "\n[revise-resume]\n",
        _json(
            {
                "status": revised["status"],
                "candidate_ids": revised["candidate_ids"],
                "clarifications": revised["clarifications"],
                "condition_revisions": revised["condition_revisions"],
            }
        ),
    )

    stale_config = {"configurable": {"thread_id": "lesson17-stale"}}
    graph.invoke({"request_text": "筛选上海 15 年以上的人才"}, stale_config, context=context)
    rejected = graph.invoke(
        Command(
            resume={
                "interaction_id": "rr-v9",
                "action": "revise",
                "revision_text": "筛选上海 5 年以上工作经验的人才",
            }
        ),
        stale_config,
        context=context,
    )
    print(
        "\n[stale-rejected]\n",
        _json(
            {
                "resume_error": rejected["__interrupt__"][0].value["resume_error"],
                "next": list(graph.get_state(stale_config).next),
            }
        ),
    )

    snapshot = graph.get_state(stale_config)
    paused_at = datetime.fromisoformat(snapshot.created_at)
    waited_seconds = (datetime.now(timezone.utc) - paused_at).total_seconds()
    print(
        "\n[wait-inspect]\n",
        _json(
            {
                "paused_at": snapshot.created_at,
                "waited_seconds": round(waited_seconds, 2),
                "timeout_policy": "等待超过 30 分钟由调用方提交 cancel",
            }
        ),
    )
    cancelled = graph.invoke(
        Command(resume={"interaction_id": "rr-v0", "action": "cancel"}),
        stale_config,
        context=context,
    )
    print(
        "\n[cancel]\n",
        _json({"status": cancelled["status"], "next": list(graph.get_state(stale_config).next)}),
    )

    travel_config = {"configurable": {"thread_id": "lesson17-travel"}}
    graph.invoke({"request_text": "筛选上海 15 年以上的人才"}, travel_config, context=context)
    history = [
        {
            "step": item.metadata.get("step"),
            "next": list(item.next),
            "created_at": item.created_at,
        }
        for item in graph.get_state_history(travel_config)
    ]
    print("\n[state-history]\n", _json(history))

    fork_source = next(
        item for item in graph.get_state_history(travel_config) if item.next == ("build_plan",)
    )
    forked_draft = {
        "input_mode": "detailed_requirement",
        "job_query": None,
        "filters": [{"field": "region", "operator": "eq", "value": "上海"}],
        "semantic_requirements": [],
        "evaluation_preferences": [],
        "clarifications": [],
    }
    fork_config = graph.update_state(
        fork_source.config,
        {"draft": forked_draft},
        as_node="interpret_input",
    )
    replayed = graph.invoke(None, fork_config, context=context)
    print(
        "\n[time-travel-replay]\n",
        _json(
            {
                "fork_from_step": fork_source.metadata.get("step"),
                "interrupted": "__interrupt__" in replayed,
                "status": replayed["status"],
                "candidate_ids": replayed["candidate_ids"],
            }
        ),
    )


def main() -> None:
    run_verification()


if __name__ == "__main__":
    main()
