from __future__ import annotations

from typing import Any, Literal

from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from app.talent_decision_graph import DecisionContext


class TalentDecisionEngineInput(TypedDict):
    request_text: str


class TalentDecisionEngineOutput(TypedDict, total=False):
    status: str
    candidate_ids: list[str]
    ranking: list[str]
    report: str
    errors: list[str]


class TalentDecisionEngineState(TypedDict, total=False):
    request_text: str
    input_mode: str
    job_matches: list[dict[str, Any]]
    selected_job: dict[str, Any] | None
    talent_request: dict[str, Any]
    query_plan: dict[str, Any]
    candidate_ids: list[str]
    condition_revisions: list[dict[str, Any]]
    plan_version: int
    clarifications: list[dict[str, str]]
    dimensions: list[dict[str, Any]]
    work_items: list[dict[str, Any]]
    branch_results: list[dict[str, Any]]
    validation_issues: list[dict[str, Any]]
    required_work_items: int
    work_item_limit: int
    evidence_items: list[dict[str, Any]]
    dimension_assessments: list[dict[str, Any]]
    consistency_issues: list[dict[str, Any]]
    candidate_assessments: list[dict[str, Any]]
    ranking: list[str]
    report_draft: dict[str, Any]
    report: str
    status: str
    errors: list[str]


def _route_after_request(
    state: TalentDecisionEngineState,
) -> Literal["evaluation", "stop"]:
    return "evaluation" if state.get("status") == "candidates_ready" else "stop"


def _route_after_evaluation(
    state: TalentDecisionEngineState,
) -> Literal["report", "stop"]:
    return (
        "report"
        if state.get("status") in {"branches_ready", "branches_ready_with_failures"}
        else "stop"
    )


def build_talent_decision_engine(*, request_graph: Any, evaluation_graph: Any, report_graph: Any):
    builder = StateGraph(
        TalentDecisionEngineState,
        context_schema=DecisionContext,
        input_schema=TalentDecisionEngineInput,
        output_schema=TalentDecisionEngineOutput,
    )
    builder.add_node("request_subgraph", request_graph)
    builder.add_node("evaluation_subgraph", evaluation_graph)
    builder.add_node("report_subgraph", report_graph)
    builder.add_edge(START, "request_subgraph")
    builder.add_conditional_edges(
        "request_subgraph",
        _route_after_request,
        {"evaluation": "evaluation_subgraph", "stop": END},
    )
    builder.add_conditional_edges(
        "evaluation_subgraph",
        _route_after_evaluation,
        {"report": "report_subgraph", "stop": END},
    )
    builder.add_edge("report_subgraph", END)
    return builder.compile()
