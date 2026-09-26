from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from difflib import SequenceMatcher
from threading import Lock
from typing import Annotated, Any, Callable
from uuid import uuid4

from langchain.tools import ToolRuntime, tool
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import EmployeeProfile, JobDescription, ToolCallAudit
from app.query_plan import FilterCondition, QueryPlan, TaskType, select_candidate_ids


@dataclass(frozen=True)
class TalentToolContext:
    tenant_id: str # 当前请求所属企业或租户 (安全设计：数据库查询永远从 Context 取租户)
    permission_scopes: tuple[str, ...] # 例如 ("hr_private",)，表示可以读取哪些资料范围
    actor_id: str = "unknown" # 真正发起调用的用户、服务账号或 Agent 身份
    run_id: str = "unknown" # 一次主图运行或一次业务请求的关联编号


@dataclass(frozen=True)
class ToolPolicy: # 超时重试等策略参数
    max_attempts: int = 2 # 一次工具调用最多执行几次，包含首次调用
    timeout_seconds: float = 3.0 # 每一次尝试允许占用的最长时间
    backoff_seconds: float = 0.05 # 第一次重试前的等待基数


class TransientToolError(RuntimeError):
    pass


class InMemoryAuditSink:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def write(self, record: dict[str, Any]) -> None:
        self.records.append(record)


class SqlAuditSink:
    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self.session_factory = session_factory

    def write(self, record: dict[str, Any]) -> None:
        with self.session_factory() as db:
            db.add(ToolCallAudit(**record))
            db.commit()


class CircuitBreaker: # 熔断与降级
    # 处理的问题：
    # 1. 依赖持续故障时，单次重试已经无法恢复
    # 2. 继续把每个新请求发送到故障服务，会增加排队、线程占用和下游压力
    # 熔断处理：
    # 1. 连续瞬时失败达到阈值后，熔断器记录打开时间
    # 2. 恢复窗口结束前，新调用不再访问故障依赖
    def __init__(self, *, failure_threshold: int = 3, recovery_seconds: float = 30) -> None:
        self.failure_threshold = failure_threshold # 连续失败达到多少次后打开熔断器
        self.recovery_seconds = recovery_seconds # 熔断保持时间，到期后允许新的探测调用
        self._failures: dict[str, int] = {} # [用于记录最近连续失败多少次？]
        self._opened_at: dict[str, float] = {} # 只为已打开的工具保存打开时的单调时钟读数；[用于记录如果已经熔断，是什么时候开始熔断的？]没有该键，就视为可通行
        self._lock = Lock() # 多个请求并发读写字典时，把每个方法内部的检查和更新放在一个临界区；它不是给数据库操作加锁，也没有把一次请求的 allow → 执行 → succeed/fail 全过程串行化

    def allow(self, name: str) -> bool:
        # 作用：返回 现在允许不允许调用这个工具
        # True是能够正常调用，未熔断了；False是处于熔断中，不能调用
        with self._lock:
            opened_at = self._opened_at.get(name)
            if opened_at is None:
                return True
            if time.monotonic() - opened_at >= self.recovery_seconds:
                # 超过熔断设定的阈值，当前Tool熔断参数重置
                self._failures[name] = 0
                self._opened_at.pop(name, None)
                return True
            return False

    def succeed(self, name: str) -> None:
        # 作用：这个工具这次调用成功了，所以把它恢复成正常状态
        with self._lock:
            self._failures[name] = 0
            self._opened_at.pop(name, None)

    def fail(self, name: str) -> None:
        # 作用：这个工具刚刚调用失败了，更新熔断器状态
        with self._lock:
            failures = self._failures.get(name, 0) + 1
            self._failures[name] = failures
            if failures >= self.failure_threshold:
                self._opened_at[name] = time.monotonic()


class ToolExecutor: # 工具执行器
    def __init__(
        self,
        *,
        policy: ToolPolicy | None = None, # 默认通用Policy参数在创建执行器时设置
        tool_policies: dict[str, ToolPolicy] | None = None, # 这里可以为不同工具设置不同的超时，重试等Policy策略[例如：档案查询是本地数据库读取，应使用较小值]
        circuit_breaker: CircuitBreaker | None = None, # 熔断策略
        audit_sink: Any | None = None,
    ) -> None:
        self.policy = policy or ToolPolicy()
        self.tool_policies = tool_policies or {}
        self.circuit_breaker = circuit_breaker or CircuitBreaker() # 熔断策略
        self.audit_sink = audit_sink or InMemoryAuditSink()

    def execute(
        self,
        name: str,
        operation: Callable[[], Any],
        *,
        context: TalentToolContext,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        call_id = str(uuid4())
        started = time.monotonic()
        policy = self.tool_policies.get(name, self.policy)
        if not self.circuit_breaker.allow(name):
            # Tool 调用处于 熔断中
            return self._finish(
                name, call_id, context, arguments, started, 0, "blocked",
                error={"code": "circuit_open", "message": "依赖服务暂时不可用", "retryable": True},
                degraded=True,
            )

        last_error: Exception | None = None
        for attempt in range(1, policy.max_attempts + 1): # 正常执行 [包含首次尝试及重试]
            pool = ThreadPoolExecutor(max_workers=1) # 为每次 attempt 新建单线程池、提交业务闭包，返回 Future
            try:
                future = pool.submit(operation)
                data = future.result(timeout=policy.timeout_seconds) # 当前线程最多等这一轮给定秒数；正常返回的 data 可以是列表、字典、空列表等，空列表也是成功得到的业务结果
                self.circuit_breaker.succeed(name) # 若执行成功，清零该工具连续失败熔断器数据
                return self._finish(name, call_id, context, arguments, started, attempt, "succeeded", data=data)
            except (TransientToolError, FutureTimeoutError) as exc:
                last_error = exc
                if attempt < policy.max_attempts and policy.backoff_seconds:
                    # 指数退避时间按重试次数递增(这里设计次数不是固定不变的，而是指数级增长的设计)
                    time.sleep(policy.backoff_seconds * (2 ** (attempt - 1)))
                    # 实际生产环境中 SLA(Service Level Agreement，服务级别协议）是下游服务对调用方作出的服务承诺(具体需看课程文档等内容)
            except (ValueError, PermissionError) as exc:
                code = "permission_denied" if isinstance(exc, PermissionError) else "invalid_argument"
                return self._finish(
                    name, call_id, context, arguments, started, attempt, "failed",
                    error={"code": code, "message": str(exc), "retryable": False},
                )
            finally:
                # 无论前面成功、失败还是 return，都释放这一轮线程池的提交入口；
                # wait=False 不等待已运行任务结束，cancel_futures=True 只能取消尚未运行的任务，不会杀死正在执行的慢查询
                pool.shutdown(wait=False, cancel_futures=True)

        self.circuit_breaker.fail(name)
        message = "依赖服务调用超时" if isinstance(last_error, FutureTimeoutError) else "依赖服务暂时不可用"
        # 所有人才工具使用相同的_finish()返回结构
        return self._finish(
            name, call_id, context, arguments, started, policy.max_attempts, "failed",
            error={"code": "dependency_unavailable", "message": message, "retryable": True},
            degraded=True,
        )

    def _finish(
        self, name, call_id, context, arguments, started, attempts, status,
        *, data=None, error=None, degraded=False,
    ) -> dict[str, Any]:
        # 所有工具最终都通过 ToolExecutor._finish() 返回同一种外层结构
        meta = {
            "tool_name": name,
            "call_id": call_id,
            "attempts": attempts,
            "degraded": degraded,
        }
        # Tool 执行状态持久化入库
        self.audit_sink.write(
            {
                "call_id": call_id,
                "tool_name": name,
                "tenant_id": context.tenant_id,
                "actor_id": context.actor_id,
                "run_id": context.run_id,
                "argument_keys": sorted(arguments),
                "status": status,
                "attempts": attempts,
                "duration_ms": round((time.monotonic() - started) * 1000, 2),
                "error_code": error["code"] if error else None,
            }
        )
        return {"ok": error is None, "data": data, "error": error, "meta": meta}


class TalentToolService:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        evidence_provider: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.evidence_provider = evidence_provider

    @staticmethod
    def _check_context(context: TalentToolContext) -> None: # 检查 Context 是否完整
        if not context.tenant_id.strip():
            raise PermissionError("缺少可信租户")
        if not context.permission_scopes:
            raise PermissionError("缺少可用权限范围")

    def lookup_job_descriptions(self, query: str, *, context: TalentToolContext, limit: int = 5) -> list[dict[str, Any]]:
        self._check_context(context)
        normalized_query = "".join(query.lower().split())
        if not normalized_query:
            raise ValueError("岗位名称不能为空")
        with self.session_factory() as db:
            rows = db.scalars(
                select(JobDescription).where(
                    JobDescription.tenant_id == context.tenant_id,
                    JobDescription.status == "active",
                )
            ).all()
        matches = []
        for row in rows:
            normalized_name = "".join(row.name.lower().split())
            score = 1.0 if normalized_query in normalized_name or normalized_name in normalized_query else round(
                SequenceMatcher(None, normalized_query, normalized_name).ratio(), 4
            )
            if score >= 0.3:
                matches.append(
                    {"job_code": row.job_code, "name": row.name, "match_score": score,
                     "version": row.version, "content": row.content}
                )
        return sorted(matches, key=lambda item: (-item["match_score"], item["job_code"]))[:limit]

    def filter_candidates(self, filters: list[FilterCondition], *, context: TalentToolContext) -> list[str]:
        self._check_context(context)
        plan = QueryPlan(task_type=TaskType.FIND_TALENT, filters=filters)
        with self.session_factory() as db:
            return select_candidate_ids(db, plan, tenant_id=context.tenant_id)

    def get_candidate_profiles(self, candidate_ids: list[str], *, context: TalentToolContext) -> list[dict[str, Any]]:
        self._check_context(context)
        if not candidate_ids:
            return []
        with self.session_factory() as db:
            rows = db.scalars(
                select(EmployeeProfile).where(
                    EmployeeProfile.tenant_id == context.tenant_id,
                    EmployeeProfile.employee_no.in_(candidate_ids),
                    EmployeeProfile.employment_status == "active",
                ).order_by(EmployeeProfile.employee_no)
            ).all()
        return [
            {"candidate_id": row.employee_no, "name": row.name, "region": row.region,
             "current_position": row.current_position, "job_level": row.job_level,
             "years_of_experience": row.years_of_experience, "department": row.department}
            for row in rows
        ]

    def search_candidate_evidence(
        self, query: str, candidate_ids: list[str], *, context: TalentToolContext,
    ) -> dict[str, Any]:
        self._check_context(context)
        if self.evidence_provider is None:
            raise TransientToolError("evidence_provider_not_configured")
        result = self.evidence_provider(
            query=query,
            candidate_ids=candidate_ids,
            tenant_id=context.tenant_id,
            permission_scopes=list(context.permission_scopes),
            include_evidence_pack=True,
        )
        for pack in result.get("evidence_packs", []):
            if pack.get("schema_version") != "2.0":
                raise ValueError("不支持的证据协议版本")
        return result


def build_talent_tools(service: TalentToolService, executor: ToolExecutor):
    @tool
    def lookup_job_descriptions(
        query: Annotated[str, Field(min_length=1, max_length=200)],
        runtime: ToolRuntime[TalentToolContext],
        limit: Annotated[int, Field(ge=1, le=10)] = 5,
    ):
        """按岗位名称查询企业已经维护的岗位 JD，不生成或补写岗位要求。"""
        return executor.execute(
            "lookup_job_descriptions",
            lambda: service.lookup_job_descriptions(query, limit=limit, context=runtime.context),
            context=runtime.context,
            arguments={"query": query, "limit": limit},
        )

    @tool
    def filter_candidates(
        filters: Annotated[list[FilterCondition], Field(max_length=20)],
        runtime: ToolRuntime[TalentToolContext],
    ):
        """按地区、职级、年限等结构化条件筛选授权租户内的候选人。"""
        return executor.execute(
            "filter_candidates", lambda: service.filter_candidates(filters, context=runtime.context),
            context=runtime.context, arguments={"filters": filters},
        )

    @tool
    def search_candidate_evidence(
        query: Annotated[str, Field(min_length=1, max_length=500)],
        candidate_ids: Annotated[list[str], Field(min_length=1, max_length=100)],
        runtime: ToolRuntime[TalentToolContext],
    ):
        """在给定候选人范围内检索材料证据并返回 Candidate Evidence Pack 2.0。"""
        return executor.execute(
            "search_candidate_evidence",
            lambda: service.search_candidate_evidence(query, candidate_ids, context=runtime.context),
            context=runtime.context, arguments={"query": query, "candidate_ids": candidate_ids},
        )

    @tool
    def get_candidate_profiles(
        candidate_ids: Annotated[list[str], Field(min_length=1, max_length=100)],
        runtime: ToolRuntime[TalentToolContext],
    ):
        """批量读取授权租户内候选人的结构化基础信息，用于评估取证。"""
        return executor.execute(
            "get_candidate_profiles",
            lambda: service.get_candidate_profiles(candidate_ids, context=runtime.context),
            context=runtime.context, arguments={"candidate_ids": candidate_ids},
        )
    # 返回构造好的 4 个 LangChain Tool
    return [lookup_job_descriptions, filter_candidates, search_candidate_evidence, get_candidate_profiles]


def build_candidate_provider(
    service: TalentToolService,
    *,
    compile_filters: Callable[[str], list[FilterCondition]],
):
    """Adapt the lesson 12 structured filter service to the lesson 11 graph contract."""
    def provider(request: dict[str, Any], decision_context: Any) -> list[str]:
        context = TalentToolContext(
            tenant_id=decision_context.tenant_id,
            permission_scopes=tuple(decision_context.permission_scopes),
            actor_id="langgraph",
        )
        return service.filter_candidates(compile_filters(request["original_text"]), context=context)

    return provider
