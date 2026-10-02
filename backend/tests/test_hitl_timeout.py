"""
    作业2(仅复制)：
    测试中的 now 是注入的固定时间，不用真的 sleep 等 30 秒；图仍然使用项目真实的 build_talent_request_graph，不是用假图代替恢复链路。
"""
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