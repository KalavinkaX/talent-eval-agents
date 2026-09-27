from __future__ import annotations

import argparse
import json
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.talent_decision_engine import (
    TalentDecisionEngineState,
    build_talent_decision_engine,
)
from app.talent_decision_graph import DecisionContext


def _linear_graph(nodes: list[tuple[str, Any]]):
    builder = StateGraph(TalentDecisionEngineState)
    for name, node in nodes:
        builder.add_node(name, node)
    builder.add_edge(START, nodes[0][0])
    for (source, _), (target, _) in zip(nodes, nodes[1:], strict=False):
        builder.add_edge(source, target)
    builder.add_edge(nodes[-1][0], END)
    return builder.compile()


def build_demo_graph():
    request_graph = _linear_graph(
        [
            (
                "receive_request",
                lambda state: {
                    "request_text": state["request_text"].strip(),
                    "status": "received",
                },
            ),
            (
                "compile_request",
                lambda state: {
                    "talent_request": {
                        "original_text": state["request_text"],
                        "semantic_conditions": ["AI 项目经验"],
                    },
                    "query_plan": {
                        "task_type": "find_talent",
                        "filters": [{"field": "region", "value": "上海"}],
                    },
                    "status": "plan_ready",
                },
            ),
            (
                "filter_candidates",
                lambda _: {
                    "candidate_ids": ["C001", "C002"],
                    "status": "candidates_ready",
                },
            ),
        ]
    )
    evaluation_graph = _linear_graph(
        [
            (
                "generate_dimensions",
                lambda _: {
                    "dimensions": [
                        {"name": "AI 项目交付", "weight_percent": 100}
                    ],
                    "status": "dimensions_ready",
                },
            ),
            (
                "evaluate_candidates",
                lambda state: {
                    "branch_results": [
                        {"task_id": f"{candidate_id}:1", "candidate_id": candidate_id}
                        for candidate_id in state["candidate_ids"]
                    ],
                    "status": "branches_ready",
                },
            ),
        ]
    )
    report_graph = _linear_graph(
        [
            (
                "score_candidates",
                lambda _: {
                    "candidate_assessments": [
                        {"candidate_id": "C001", "confirmed_score": 88},
                        {"candidate_id": "C002", "confirmed_score": 76},
                    ],
                    "ranking": ["C001", "C002"],
                    "status": "scores_ready",
                },
            ),
            (
                "compose_report",
                lambda state: {
                    "report": "# 人才评估报告\n\n1. C001\n2. C002",
                    "ranking": state["ranking"],
                    "status": "completed",
                },
            ),
        ]
    )
    return build_talent_decision_engine(
        request_graph=request_graph,
        evaluation_graph=evaluation_graph,
        report_graph=report_graph,
    )


def _scope(namespace: tuple[str, ...]) -> str:
    if not namespace:
        return "main"
    return "/".join(item.split(":", 1)[0] for item in namespace)


def collect_demo_events() -> list[dict[str, Any]]:
    graph = build_demo_graph()
    context = DecisionContext(
        tenant_id="course-demo",
        permission_scopes=("hr_private",),
    )
    events: list[dict[str, Any]] = []
    for namespace, chunk in graph.stream(
        {"request_text": "筛选上海且有 AI 项目经验的候选人"},
        context=context,
        stream_mode="updates",
        subgraphs=True,
    ):
        for node, update in chunk.items():
            events.append(
                {
                    "sequence": len(events) + 1,
                    "scope": _scope(namespace),
                    "node": node,
                    "update": update,
                }
            )
    return events


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="输出人才决策主图与子图的逐节点更新"
    )
    parser.add_argument(
        "--pause",
        action="store_true",
        help="每个节点输出后等待回车，用于课堂单步讲解",
    )
    return parser


def _display_update(event: dict[str, Any]) -> Any:
    update = event["update"]
    if event["scope"] != "main" or not isinstance(update, dict):
        return update
    keys_by_node = {
        "request_subgraph": ("candidate_ids", "status"),
        "evaluation_subgraph": ("candidate_ids", "dimensions", "status"),
        "report_subgraph": ("candidate_ids", "ranking", "report", "status"),
    }
    keys = keys_by_node.get(event["node"], tuple(update))
    return {key: update[key] for key in keys if key in update}


def main() -> None:
    args = _build_parser().parse_args()
    for event in collect_demo_events():
        print(
            f"[{event['sequence']:02d}] "
            f"scope={event['scope']} node={event['node']}"
        )
        print(json.dumps(_display_update(event), ensure_ascii=False, sort_keys=True))
        if args.pause:
            input("按回车继续...")


if __name__ == "__main__":
    main()
