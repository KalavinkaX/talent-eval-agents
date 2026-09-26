from __future__ import annotations

import json

from langchain_core.messages import HumanMessage
from langgraph.errors import InvalidUpdateError

from app.talent_decision_graph import (
    DecisionContext,
    build_missing_reducer_demo,
    build_reducer_demo,
    build_talent_decision_graph,
)


def _fixture_candidate_provider(request, context):
    if "ai" in request["original_text"].casefold():
        return ["C001", "C004"]
    return []


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def main() -> None:
    context = DecisionContext(tenant_id="course-demo", permission_scopes=("hr_private",))
    graph = build_talent_decision_graph(_fixture_candidate_provider)
    mermaid = graph.get_graph().draw_mermaid()
    print(f"[graph mermaid]\n{mermaid}")
    print("[normal] updates")
    trace = [] # 作业4：打印节点更新字段测试
    for update in graph.stream(
        {
            "messages": [HumanMessage(content="筛选有 AI 项目经验的技术负责人")],
            "request_text": "筛选有 AI 项目经验的技术负责人",
        },
        context=context,
        stream_mode="updates",
    ):
        for node, delta in update.items():
            record = {"node": node, "fields": list(delta.keys()), "values": delta}
            trace.append(record)
            print(_json(record))

        # update 是Graph每个Node节点的节点名称 以及 节点运行后 return的参数 (可Debug)
        print(_json(update))

    print("[normal] field trace:", [(row["node"], row["fields"]) for row in trace])

    empty_result = graph.invoke(
        {"messages": [], "request_text": "筛选 Java 工程师"},
        context=context,
    )
    print("\n\n[empty]\n", _json(empty_result))

    print("\n\n[reducer] 对比实验")
    try:
        build_missing_reducer_demo().invoke({"items": []})
    except InvalidUpdateError as exc:
        print("[without_reducer]", type(exc).__name__, str(exc).split("\n", 1)[0])

    reducer_result = build_reducer_demo().invoke({"items": []})
    print("[with_reducer]", _json(reducer_result))


if __name__ == "__main__":
    main()
