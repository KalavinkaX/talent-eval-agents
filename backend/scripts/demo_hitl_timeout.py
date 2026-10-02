"""
    作业2(仅复制)
    最小自动取消演示：5 秒后取消，不接收用户输入。
"""
from datetime import datetime
from time import sleep
from uuid import uuid4

from langgraph.checkpoint.memory import InMemorySaver

from app.talent_decision_graph import DecisionContext
from app.talent_request_graph import build_talent_request_graph
from scripts.verify_talent_hitl_resume import DemoCandidateFilter, DemoInterpreter
from scripts.hitl_timeout import cancel_if_expired


def main():
    graph = build_talent_request_graph(
        request_interpreter=DemoInterpreter(),
        job_lookup=lambda _query, _context: [],
        candidate_filter=DemoCandidateFilter(),
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": f"timeout-demo-{uuid4()}"}}
    context = DecisionContext(tenant_id="course-demo", permission_scopes=("hr_private",))

    result = graph.invoke(
        {"request_text": "筛选上海 15 年以上的人才"}, config, context=context,
    )
    payload = result["__interrupt__"][0].value
    # 收到本轮 interrupt 后只记录一次，不要在每次检查时重新赋值。
    paused_at = datetime.fromisoformat(graph.get_state(config).created_at)
    interaction_id = payload["interaction_id"]
    print("已暂停：", payload["reason_code"], interaction_id)

    # 定时检查在调用脚本中，不在 graph 节点中。
    while graph.get_state(config).next:
        if cancel_if_expired(
            graph, config, interaction_id=interaction_id, paused_at=paused_at,
            context=context, timeout_seconds=5,
        ):
            print("等待超时，已提交 cancel")
            break
        sleep(1)

    snapshot = graph.get_state(config)
    print("最终状态：", snapshot.values["status"])
    print("后续节点：", snapshot.next)


if __name__ == "__main__":
    main()