"""
    作业2(仅复制)：由调用层检查交互等待超时，并提交取消。
"""
from datetime import datetime, timedelta, timezone

from langgraph.types import Command


def cancel_if_expired(
    graph, config, *, interaction_id, paused_at, context,
    timeout_seconds=1800, now=None,
):
    """只处理当前 requirement_revision；实际提交取消返回 True。"""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds 必须大于 0")
    now = now if now is not None else datetime.now(timezone.utc)
    if paused_at.tzinfo is None or now.tzinfo is None:
        raise ValueError("paused_at 和 now 必须携带时区")

    # 1. 重新读取同一个 Thread，避免对已结束的任务重复取消。
    snapshot = graph.get_state(config)
    pending = [item for task in snapshot.tasks for item in task.interrupts]
    if not snapshot.next or not pending:
        return False

    # 2. 本作业只处理统一的要求修订中断，且不能误取消下一轮中断。
    payload = pending[0].value
    if payload.get("type") != "requirement_revision":
        return False
    if payload.get("interaction_id") != interaction_id:
        return False

    # 3. 到达截止时间才取消；相等时也视为超时。
    deadline = paused_at + timedelta(seconds=timeout_seconds)
    if now < deadline:
        return False

    # 4. 不直接改 State，沿用图已有的合法取消协议。
    graph.invoke(
        Command(resume={"interaction_id": interaction_id, "action": "cancel"}),
        config,
        context=context,
    )
    return True