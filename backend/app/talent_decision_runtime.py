from __future__ import annotations

from app.talent_decision_engine import build_talent_decision_engine
from app.talent_evaluation_runtime import graph as evaluation_graph
from app.talent_evaluation_runtime import report_graph
from app.talent_request_graph import graph as request_graph


# Agent Server provides the parent checkpointer. The three subgraphs use their
# default per-invocation persistence and inherit that checkpointer at runtime.
graph = build_talent_decision_engine(
    request_graph=request_graph,
    evaluation_graph=evaluation_graph,
    report_graph=report_graph,
)
