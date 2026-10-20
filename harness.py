"""Harness：装配模型、工具、Gate、Sandbox、State、Checkpoint、Audit 和完成检查。

唯一运行入口是 GuardedHarness._drive()：start() 和 resume() 只负责准备状态，然后交给它。
GuardedToolCallingAgent.run() 不在 _drive 里调用会直接报错。

没有改 smolagents 源码，只用了它的扩展点：
    process_tool_calls          覆盖：逐个把 tool call 交给 Gate，拿回结构化结果作为观察
    execute_tool_call           覆盖：直接抛错，堵住绕过 Gate 调工具的路径
    _handle_max_steps_reached   覆盖：不再让模型凭记忆“编”最终答案，改成明确的 MAX_STEPS 停止
    step_callbacks              每步结束：记步骤、算预算、存检查点，需要停时 interrupt()
    final_answer_checks         兜底：只有 Gate 已经验收通过（COMPLETED）才允许结束
    interrupt()                 等待审批、预算耗尽、用户取消时，在步骤边界停下
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from rich.panel import Panel
from rich.text import Text
from smolagents import Model, ToolCallingAgent
from smolagents.agents import ToolOutput
from smolagents.memory import ActionStep, ToolCall
from smolagents.monitoring import LogLevel
from smolagents.utils import AgentError, AgentGenerationError

from audit import AuditLog
from checkpoint import (
    Checkpoint,
    CheckpointManager,
    RecoveryPlan,
    dump_memory,
    evidence_affected,
    load_memory,
    plan_recovery,
    workspace_drift,
)
from coding_tools import build_registry
from completion import CompletionChecker
from gate import Gate, GateBypassError, GatePolicy, GateResult, ToolCallRequest, ToolRegistry
from sandbox import Sandbox, SandboxPolicy
from state_store import (
    TERMINAL_STATUSES,
    ActionStatus,
    Approval,
    ApprovalStatus,
    Budget,
    RunState,
    RunStatus,
    StateStore,
    StepRecord,
    StopReason,
    TaskKind,
)

# 追加到 smolagents 系统提示词里的规则说明（英文，给模型看）：告诉模型 Gate 的存在和各种错误码该怎么应对
HARNESS_INSTRUCTIONS = """You are working inside a guarded coding harness on a local repository.
- Every tool call goes through a gate. Each result is JSON with "ok". When ok is false, read error.code and error.message and adapt; never repeat an identical call more than twice.
- Paths are relative to the repository root. .env files, .git and anything outside the repository are off limits.
- run_test only accepts pytest commands such as "pytest -q" (no shell features like ; | && >).
- Writes outside artifacts/ need human approval. When a result says APPROVAL_REQUIRED the run pauses: do not retry that call. After approval the harness applies the change itself and tells you.
- Keep a short plan with update_plan and update it as you go.
- final_answer must be the only tool call in its step. It is accepted only when the deliverable exists and the evidence is fresh; otherwise you get COMPLETION_REJECTED with the failed checks."""

# 两类任务各自的交付要求，拼在任务描述后面
TASK_RULES = {
    TaskKind.TEST_FIX: (
        "Task type: test_fix. Fix the source code so that the whole test suite passes. Files under tests/ and the "
        "test configuration are read-only. Before final_answer, run the full suite with `pytest -q` on your final "
        "code; if you change code after a passing run, run it again."
    ),
    TaskKind.CODE_UNDERSTANDING: (
        "Task type: code_understanding. Read the relevant code, then write a Markdown report to artifacts/<name>.md "
        "that cites the files you read by path. Source files are read-only. Then call final_answer with a short summary."
    ),
}

WRITE_TOOLS = frozenset({"write_file", "replace_in_file"})


@dataclass(frozen=True)
class TaskProfile:
    """任务画像：一类任务能用哪些工具、哪些路径只读、能写到哪里。"""

    allowed_tools: frozenset[str]
    readonly_patterns: tuple[str, ...]
    writable_prefixes: tuple[str, ...]


PROFILES: dict[TaskKind, TaskProfile] = {
    TaskKind.CODE_UNDERSTANDING: TaskProfile(
        allowed_tools=frozenset(
            {"list_files", "read_file", "search_code", "write_file", "run_test", "update_plan", "final_answer"}
        ),
        readonly_patterns=(),
        writable_prefixes=("artifacts/",),  # 代码理解任务只能写报告
    ),
    TaskKind.TEST_FIX: TaskProfile(
        allowed_tools=frozenset(
            {"list_files", "read_file", "search_code", "write_file", "replace_in_file", "run_test", "update_plan",
             "final_answer"}
        ),
        # 防止“改测试让测试通过”：测试与测试配置只读
        readonly_patterns=("tests/", "*conftest.py", "pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini"),
        writable_prefixes=(),
    ),
}


@dataclass
class HarnessConfig:
    """Harness 配置：预算上限 + Gate / Sandbox 策略。"""

    max_steps: int = 20  # 模型轮数上限（跨进程累计）
    max_tool_calls: int = 60  # 工具调用次数上限
    max_tokens: int | None = None  # token 上限，None 不限
    max_wall_seconds: float | None = None  # 墙钟时间上限（秒），None 不限
    gate_policy: GatePolicy = field(default_factory=GatePolicy)
    sandbox_policy: SandboxPolicy = field(default_factory=SandboxPolicy)
    verbosity: LogLevel = LogLevel.OFF


@dataclass
class RunOutcome:
    """一次 start / resume 停下来时的结果：停在哪个状态、为什么、下一步需要人做什么。"""

    run_id: str
    status: RunStatus
    stop_reason: StopReason | None
    detail: str | None
    final_answer: str | None
    steps_used: int
    pending_approvals: list[dict[str, Any]]
    unknown_actions: list[str]

    @property
    def success(self) -> bool:
        # 只有通过验收进入 COMPLETED 才算成功；预算耗尽等停止都不伪装成功（H3）
        return self.status is RunStatus.COMPLETED


class HarnessInterrupt(Exception):
    """从 smolagents 主循环里带着停止原因跳出来（目前用于 MAX_STEPS）。"""

    def __init__(self, reason: StopReason, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class GuardedToolCallingAgent(ToolCallingAgent):
    """ToolCallingAgent 本体不变，只把“执行工具”这一段换成 Gate。"""

    def __init__(self, *, harness: "GuardedHarness", **kwargs: Any):
        self._harness = harness
        super().__init__(**kwargs)

    def run(self, *args: Any, **kwargs: Any) -> Any:
        # 唯一运行入口：只有 Harness._drive() 里才允许跑；直接 agent.run() 会绕过状态、检查点和恢复
        if not self._harness._driving:
            raise RuntimeError("GuardedToolCallingAgent can only run through GuardedHarness.start() / resume()")
        return super().run(*args, **kwargs)

    def process_tool_calls(self, chat_message, memory_step):
        """覆盖 ToolCallingAgent.process_tool_calls：原实现会并行地直接调用 tool，这里改成逐个交给 Gate。

        产出的 ToolCall / ToolOutput 事件和写回 memory_step 的字段与原实现保持一致，
        所以 smolagents 主循环的其余部分（记忆、final answer 判定、日志）都不用改。
        """
        calls = [
            ToolCall(name=tc.function.name, arguments=tc.function.arguments, id=tc.id)
            for tc in chat_message.tool_calls or []
        ]
        for call in calls:
            yield call
        observations = []
        # 串行执行：一旦某个调用让 run 进入等待审批，同一步里后面的调用会被 Gate 以 RUN_NOT_ACTIVE 拒绝
        for call in calls:
            self.logger.log(Panel(Text(f"Calling tool: '{call.name}' with arguments: {call.arguments}")), level=LogLevel.INFO)
            result = self._harness._dispatch(call, batch_size=len(calls))
            observation = result.to_observation(self._harness.config.gate_policy.observation_limit)
            self.logger.log(f"Observations: {observation.replace('[', '|')}", level=LogLevel.INFO)
            observations.append(observation)
            # 只有 Gate 验收通过的 final_answer 才会标记 is_final_answer=True，smolagents 才会结束循环
            yield ToolOutput(
                id=call.id,
                output=result.output if result.is_final_answer else observation,
                is_final_answer=result.is_final_answer,
                observation=observation,
                tool_call=call,
            )
        memory_step.tool_calls = calls
        memory_step.observations = "\n".join(observations) if observations else None

    def execute_tool_call(self, tool_name: str, arguments: Any) -> Any:
        # 原实现直接调用 tool(**arguments)；这里彻底堵死，任何绕过 Gate 的执行路径都会报错
        raise GateBypassError("execute_tool_call is disabled; every tool call goes through the Gate")

    def _handle_max_steps_reached(self, task: str) -> Any:
        # 原实现会再调一次模型，让它根据记忆“总结”一个最终答案并当作成功返回；这里改成明确停止（H3）
        raise HarnessInterrupt(StopReason.MAX_STEPS, f"reached the step limit ({self._harness._state.budget.max_steps})")


@dataclass
class _RunContext:
    """一次运行装配出来的三件套（按任务类型配置）。"""

    sandbox: Sandbox
    gate: Gate
    agent: GuardedToolCallingAgent


class GuardedHarness:
    """Guarded Coding Harness。模型负责决策；门禁、执行边界、状态、审计、恢复、最终验收都由这里负责。

    对外 API：start / resume（驱动运行），approve / reject / resolve_unknown / acknowledge_workspace
    （人工决策），cancel（取消），status（查询）。所有 Agent 执行都经过唯一入口 _drive()。
    """

    def __init__(
        self,
        *,
        state_dir: Path,
        model: Model,
        config: HarnessConfig | None = None,
        registry: ToolRegistry | None = None,
    ):
        self.state_dir = Path(state_dir)
        self.model = model
        self.config = config or HarnessConfig()
        self.registry = registry or build_registry()
        self.store = StateStore(self.state_dir)
        self.checkpoints = CheckpointManager(self.state_dir)
        self.audit = AuditLog(self.state_dir / "audit.jsonl")
        self.cancel_event = threading.Event()  # 用户取消信号：Gate、Sandbox、步骤回调共用
        self._driving = False  # 是否处在 _drive() 内：GuardedToolCallingAgent.run() 据此拒绝外部调用
        self._state: RunState | None = None
        self._ctx: _RunContext | None = None
        self._session_started = 0.0
        self._wall_base = 0.0
        self._last_checkpoint: tuple[str, int, RunStatus] | None = None

    # ======================= 对外 API =======================

    def start(self, *, workspace: Path | str, task: str, kind: TaskKind | str) -> RunOutcome:
        """新建一次运行：记下工作区基线快照（diff 与漂移检测的起点），然后交给 _drive()。"""
        kind = TaskKind(kind)
        budget = Budget(
            max_steps=self.config.max_steps,
            max_tool_calls=self.config.max_tool_calls,
            max_tokens=self.config.max_tokens,
            max_wall_seconds=self.config.max_wall_seconds,
        )
        state = RunState.new(task=task, kind=kind, workspace=str(Path(workspace).resolve()), budget=budget)
        with self.store.lock(state.run_id):
            ctx = self._assemble(state)
            state.baseline_files = ctx.sandbox.snapshot()
            state.expected_files = dict(state.baseline_files)
            self.store.save(state)
            self._log(state, "run.created", actor="harness", decision=str(state.status), details={
                "task": task, "kind": str(kind), "workspace": state.workspace, "budget": budget.model_dump(),
                "model": getattr(self.model, "model_id", None) or type(self.model).__name__,
                "code_fingerprint": ctx.sandbox.code_fingerprint(state.baseline_files),
            })
            return self._drive(state, ctx, prompt=f"{task}\n\n{TASK_RULES[kind]}", memory=None)

    def resume(self, run_id: str) -> RunOutcome:
        """恢复一次运行（通常在新进程里）。步骤：

            1. 读“活”状态和最近的检查点，算恢复计划（结果未知的动作、工作区漂移……）
            2. 按计划改状态：结果未知 → UNKNOWN_RESULT；漂移 → 证据失效、审批作废
            3. 有需要人核对的事就停在 WAITING_VERIFICATION，不调用模型
            4. 否则执行已批准的审批，把对话记忆恢复回 smolagents，带着“恢复说明”继续跑
        """
        with self.store.lock(run_id):  # 同一个 run 同一时刻只能有一个进程在推进
            state = self.store.load(run_id)
            if state.status in TERMINAL_STATUSES:  # 完成或停止的运行不能“继续跑”
                return self._outcome(state)
            ctx = self._assemble(state)
            ckpt = self.checkpoints.latest(run_id)
            plan = plan_recovery(state, ckpt, ctx.sandbox.snapshot(), ctx.sandbox.is_code_path)
            state.session += 1
            self._log(state, "run.resumed", actor="harness", decision=str(state.status), details={
                "session": state.session,
                "checkpoint_seq": ckpt.seq if ckpt else None,
                "checkpoint_step": ckpt.step if ckpt else None,
                "skipped_checkpoints": list(self.checkpoints.skipped),
                "counters": state.budget.model_dump(),
                "plan": plan.as_dict(),
            })
            self._apply_recovery(state, ctx, plan)

            if plan.blocked is StopReason.WAITING_APPROVAL:  # 审批还没人处理：原样返回
                self.store.save(state)
                return self._outcome(state)
            if plan.blocked is not None:  # 结果未知或工作区漂移：停下来等人核对（C2 / C4）
                detail = self._blocked_detail(plan)
                self._transition(state, RunStatus.WAITING_VERIFICATION, plan.blocked, detail)
                self.store.save(state)
                return self._outcome(state)

            self._transition(state, RunStatus.RUNNING, None, None)
            self.store.save(state)
            # 已批准的写入由 Harness 直接执行（经过 Gate），不等模型重新发起
            notes = self._execute_granted(state, ctx, plan.approvals_to_execute)
            prompt = self._resume_prompt(state, ckpt, plan, notes)
            memory = load_memory(ckpt.memory) if ckpt else []
            return self._drive(state, ctx, prompt=prompt, memory=memory)

    def approve(self, run_id: str, approval_id: str, *, actor: str) -> Approval:
        """人工批准。只改审批状态并写审计，不执行；执行发生在下一次 resume 里。"""
        return self._decide(run_id, approval_id, actor=actor, grant=True, reason=None)

    def reject(self, run_id: str, approval_id: str, *, actor: str, reason: str) -> Approval:
        """人工驳回。下一次 resume 时会把驳回理由告诉模型。"""
        return self._decide(run_id, approval_id, actor=actor, grant=False, reason=reason)

    def resolve_unknown(self, run_id: str, action_id: str, *, applied: bool, actor: str) -> None:
        """人工核对一个 UNKNOWN_RESULT 动作：确认生效（applied）或确认没有生效。"""
        with self.store.lock(run_id):
            state = self.store.load(run_id)
            action = state.action(action_id)
            if action.status is not ActionStatus.UNKNOWN_RESULT:
                raise ValueError(f"{action_id} is {action.status}, not UNKNOWN_RESULT")
            ctx = self._assemble(state)
            req = ToolCallRequest(tool_call_id=action.tool_call_id, name=action.tool, arguments={}, step=action.step,
                                  origin="harness")
            current = self._current_hash(state, action.target)
            if applied:  # 确认写入已生效：按正常写入的副作用处理（更新期望指纹、让旧证据失效）
                action.status = ActionStatus.SUCCEEDED
                action.target_hash_after = current
                action.result = {"verified_by": actor, "sha256_after": current}
                if action.target and current is not None:
                    ctx.gate.record_write(state, req, rel=action.target, hash_after=current)
            else:  # 确认没有生效：记为失败，模型可以重新提出修改（需要新的审批）
                action.status = ActionStatus.FAILED
                action.error = {"code": "NOT_APPLIED", "message": f"{actor} verified that the action did not take effect"}
            action.finished_at = time.time()
            self.store.save(state)
            self._log(state, "recovery.verified", actor=f"human:{actor}", decision="applied" if applied else "not_applied",
                      rule="recovery.human_verification", tool_call_id=action.tool_call_id, tool=action.tool,
                      step=action.step, details={"action_id": action_id, "target": action.target, "current_hash": current})

    def acknowledge_workspace(self, run_id: str, *, actor: str) -> dict[str, list[str | None]]:
        """人工确认工作区的外部改动：相关证据和未执行的审批作废，以当前工作区为新的期望状态。"""
        with self.store.lock(run_id):
            state = self.store.load(run_id)
            ctx = self._assemble(state)
            current = ctx.sandbox.snapshot()
            unknown_targets = {a.target for a in state.unknown_results() if a.target}
            drift = workspace_drift(state.expected_files, current, ignore=unknown_targets)
            if drift:
                ctx.gate.invalidate_evidence(
                    state, evidence_affected(state, drift, ctx.sandbox.is_code_path), "workspace change acknowledged by a human"
                )
                for ap in state.approvals.values():
                    if ap.status in (ApprovalStatus.PENDING, ApprovalStatus.GRANTED):
                        ap.status = ApprovalStatus.STALE
                        ap.reason = "workspace changed outside the harness"
            for path, (_, actual) in drift.items():
                if actual is None:
                    state.expected_files.pop(path, None)
                else:
                    state.expected_files[path] = actual
            self.store.save(state)
            self._log(state, "recovery.workspace_acknowledged", actor=f"human:{actor}", decision="acknowledged",
                      rule="recovery.human_verification", details={"files": drift})
            return drift

    def cancel(self) -> None:
        """用户取消：正在跑的测试进程组会被终止，Agent 在步骤边界停下。可以从其他线程调用。"""
        self.cancel_event.set()
        if self._ctx is not None:
            self._ctx.agent.interrupt()

    def status(self, run_id: str) -> RunState:
        return self.store.load(run_id)

    # ======================= 唯一运行入口 =======================

    def _drive(self, state: RunState, ctx: _RunContext, *, prompt: str, memory: list | None) -> RunOutcome:
        """唯一运行入口：调用 smolagents 主循环，并把各种结束方式统一成 状态 + 停止原因。

        memory 为 None 表示新运行（reset=True）；否则是从检查点恢复的对话记忆。
        """
        self._state, self._ctx = state, ctx
        if state.budget.remaining_steps <= 0:
            self._transition(state, RunStatus.STOPPED, StopReason.MAX_STEPS, "no steps left in the budget")
            self.store.save(state)
            return self._outcome(state)
        if memory is not None:
            ctx.agent.memory.steps = memory  # 恢复对话：模型能看到中断前的所有步骤
        self._wall_base = state.budget.used_wall_seconds
        self._session_started = time.monotonic()
        self._driving = True
        try:
            # max_steps 传剩余步数：跨进程累计，恢复后不会重新从 0 数
            ctx.agent.run(prompt, reset=memory is None, max_steps=state.budget.remaining_steps)
        except HarnessInterrupt as stop:  # 轮数用完
            if state.status is RunStatus.RUNNING:
                self._transition(state, RunStatus.STOPPED, stop.reason, stop.detail)
        except AgentGenerationError as exc:  # 模型调用本身失败（网络、限流……），可以稍后 resume 重试
            if state.status is RunStatus.RUNNING:
                self._transition(state, RunStatus.FAILED, StopReason.MODEL_ERROR, str(exc)[:500])
        except AgentError:
            # interrupt() 之后 smolagents 抛 "Agent interrupted."；状态已在步骤回调里设好，这里只兜底
            if state.status is RunStatus.RUNNING:
                if self.cancel_event.is_set():
                    self._transition(state, RunStatus.STOPPED, StopReason.CANCELLED, "cancelled by the user")
                else:
                    self._transition(state, RunStatus.PAUSED, StopReason.INTERRUPTED, "agent loop interrupted")
        except KeyboardInterrupt:  # 正常中断：最近的检查点已经保存，直接 resume 即可
            if state.status is RunStatus.RUNNING:
                self._transition(state, RunStatus.PAUSED, StopReason.INTERRUPTED, "interrupted by the user (Ctrl-C)")
        finally:
            # 不论怎么结束都要：落盘状态、在安全边界补一个检查点、写会话结束审计
            self._driving = False
            state.budget.used_wall_seconds = self._wall_base + (time.monotonic() - self._session_started)
            self.store.save(state)
            if not state.in_flight() and self._last_checkpoint != (state.run_id, state.budget.used_steps, state.status):
                self._save_checkpoint(state, ctx, reason=f"session_end:{state.status}")
            self._log(state, "run.session_end", actor="harness", decision=str(state.status),
                      rule=str(state.stop_reason) if state.stop_reason else None,
                      details={"detail": state.stop_detail, "session": state.session, "budget": state.budget.model_dump()})
        if state.status is RunStatus.RUNNING:  # 理论上到不了：结束循环要么验收通过，要么带停止原因
            self._transition(state, RunStatus.FAILED, StopReason.MODEL_ERROR, "agent loop ended without a decision")
            self.store.save(state)
        return self._outcome(state)

    # ======================= smolagents 回调 =======================

    def _dispatch(self, call: ToolCall, *, batch_size: int) -> GateResult:
        """GuardedToolCallingAgent.process_tool_calls 的唯一出口：把 smolagents 的 ToolCall 包成请求交给 Gate。"""
        state = self._state
        assert state is not None and self._ctx is not None
        req = ToolCallRequest(
            tool_call_id=call.id,
            name=call.name,
            arguments=call.arguments,
            step=state.budget.used_steps + 1,  # 当前正在进行的全局步号
            batch_size=batch_size,
        )
        return self._ctx.gate.submit(state, req)

    def _on_action_step(self, step: ActionStep, agent: ToolCallingAgent) -> None:
        """smolagents 每步结束的回调（step_callbacks）。这里就是“安全步骤边界”：

            记步骤 → 累计预算 → 判断是否要停 → 落盘状态 → 存检查点（C1）→ 需要停就 interrupt()
        """
        state, ctx = self._state, self._ctx
        assert state is not None and ctx is not None
        usage = step.token_usage
        state.budget.used_steps += 1
        state.budget.used_tokens += usage.total_tokens if usage else 0
        state.budget.used_wall_seconds = self._wall_base + (time.monotonic() - self._session_started)
        state.steps.append(StepRecord(
            number=state.budget.used_steps,
            session=state.session,
            tool_call_ids=[tc.id for tc in step.tool_calls or []],
            model_output=(step.model_output[:500] if isinstance(step.model_output, str) else None),
            error=str(step.error)[:500] if step.error else None,
            input_tokens=usage.input_tokens if usage else 0,
            output_tokens=usage.output_tokens if usage else 0,
            started_at=step.timing.start_time,
            ended_at=step.timing.end_time,
        ))
        if state.status is RunStatus.RUNNING:
            if self.cancel_event.is_set():
                self._transition(state, RunStatus.STOPPED, StopReason.CANCELLED, "cancelled by the user")
            elif (which := state.budget.exhausted()) is not None:  # token / 工具调用 / 墙钟预算（H3）
                self._transition(state, RunStatus.STOPPED, StopReason.BUDGET_EXHAUSTED, f"{which} budget exhausted")
        self.store.save(state)
        if not state.in_flight():  # 安全边界才存检查点
            # 这个回调执行时当前步还没追加进 agent.memory，所以显式带上
            self._save_checkpoint(state, ctx, reason="step_boundary", extra=[step])
        if state.status is not RunStatus.RUNNING:
            # 等待审批 / 预算耗尽 / 取消：smolagents 会在下一步开始前抛 "Agent interrupted."
            agent.interrupt()

    def _completed_by_gate(self, final_answer: Any, memory: Any, agent: Any = None) -> bool:
        """final_answer_checks 兜底：只有 Gate 的验收已经把 run 置为 COMPLETED，smolagents 才能结束。"""
        return self._state is not None and self._state.status is RunStatus.COMPLETED

    # ======================= 内部工具 =======================

    def _assemble(self, state: RunState) -> _RunContext:
        """按任务类型装配 Sandbox → CompletionChecker → Gate → Agent。start / resume / 人工操作都用它。"""
        profile = PROFILES[state.kind]
        sandbox = Sandbox(
            Path(state.workspace),
            replace(self.config.sandbox_policy, readonly_patterns=profile.readonly_patterns,
                    writable_prefixes=profile.writable_prefixes),
        )
        gate = Gate(
            registry=self.registry,
            sandbox=sandbox,
            store=self.store,
            audit=self.audit,
            completion=CompletionChecker(sandbox, protected_patterns=profile.readonly_patterns),
            policy=replace(self.config.gate_policy, allowed_tools=profile.allowed_tools),
            cancel_event=self.cancel_event,
        )
        # 交给 smolagents 的工具只是“schema 外壳”，执行全部经由 Gate
        agent = GuardedToolCallingAgent(
            harness=self,
            tools=self.registry.smolagents_tools(n for n in self.registry.names() if n in profile.allowed_tools),
            model=self.model,
            instructions=HARNESS_INSTRUCTIONS,
            max_steps=state.budget.max_steps,
            verbosity_level=self.config.verbosity,
            step_callbacks={ActionStep: [self._on_action_step]},
            final_answer_checks=[self._completed_by_gate],
        )
        return _RunContext(sandbox=sandbox, gate=gate, agent=agent)

    def _save_checkpoint(self, state: RunState, ctx: _RunContext, *, reason: str, extra: list | None = None) -> Checkpoint:
        """存检查点：业务状态 + 精简后的对话记忆 + 工作区快照，并写一条 checkpoint.saved 审计。"""
        memory = dump_memory([*ctx.agent.memory.steps, *(extra or [])])
        ckpt = self.checkpoints.save(state=state, memory=memory, workspace=ctx.sandbox.snapshot(), reason=reason)
        self._last_checkpoint = (state.run_id, ckpt.step, state.status)
        self.store.save(state)
        self._log(state, "checkpoint.saved", actor="harness", decision=str(state.status), step=ckpt.step, details={
            "seq": ckpt.seq,
            "reason": reason,
            "plan_items": len(state.plan),
            "evidence": len(state.evidence),
            "pending_approvals": [a.approval_id for a in state.pending_approvals()],
            "budget": state.budget.model_dump(),
        })
        return ckpt

    def _apply_recovery(self, state: RunState, ctx: _RunContext, plan: RecoveryPlan) -> None:
        """把恢复计划落实到状态上，每一项都写审计。"""
        for action_id in plan.interrupted:  # 中断的读 / 测试：重做无害，直接记失败
            action = state.action(action_id)
            action.status = ActionStatus.FAILED
            action.error = {"code": "INTERRUPTED", "message": "interrupted before completion; safe to redo"}
            action.finished_at = time.time()
            self._log(state, "recovery.interrupted_action", actor="harness", decision="failed",
                      rule="recovery.safe_to_redo", tool_call_id=action.tool_call_id, tool=action.tool,
                      step=action.step, details={"action_id": action_id, "risk": action.risk})
        for action_id in plan.new_unknown:  # 中断的写入：可能写了也可能没写，绝不自动重放（C2）
            action = state.action(action_id)
            action.status = ActionStatus.UNKNOWN_RESULT
            action.error = {
                "code": "UNKNOWN_RESULT",
                "message": "the process stopped after this action started and before its result was recorded",
            }
            # 给核对的人留线索：写前哈希、现在的哈希、本来要写入的内容的哈希
            intended = action.args.get("content")
            self._log(state, "recovery.unknown_result", actor="harness", decision="unknown",
                      rule="recovery.no_auto_replay", tool_call_id=action.tool_call_id, tool=action.tool,
                      step=action.step, details={
                          "action_id": action_id,
                          "target": action.target,
                          "hash_before": action.target_hash_before,
                          "hash_now": self._current_hash(state, action.target),
                          "intended_hash": hashlib.sha256(intended.encode("utf-8")).hexdigest()
                          if isinstance(intended, str) else None,
                      })
        if plan.drift:  # 工作区在暂停期间被改过（C4）：旧审批作废、依赖旧内容的证据失效
            self._log(state, "recovery.workspace_changed", actor="harness", decision="drift",
                      rule="recovery.workspace_changed", details={"files": plan.drift})
            for approval_id in plan.stale_approvals:
                ap = state.approvals[approval_id]
                ap.status = ApprovalStatus.STALE
                ap.reason = "workspace changed while the run was paused"
                self._log(state, "approval.stale", actor="harness", decision="stale", rule="approval.stale",
                          tool_call_id=ap.tool_call_id, tool=ap.tool, details={"approval_id": approval_id})
            ctx.gate.invalidate_evidence(state, plan.invalid_evidence, "workspace changed while the run was paused")

    def _execute_granted(self, state: RunState, ctx: _RunContext, approval_ids: list[str]) -> list[str]:
        """执行已批准的审批，返回写给模型的说明（让它知道改动已生效、不要重复）。"""
        notes = []
        for approval_id in approval_ids:
            ap = state.approvals[approval_id]
            result = ctx.gate.execute_approved(state, approval_id)
            if result.ok:
                notes.append(
                    f"- Approval {approval_id} was granted by {ap.decided_by}; the harness applied {ap.tool} on "
                    f"{ap.target} for your call {ap.tool_call_id} (sha256 {result.output['sha256_after'][:12]}). "
                    "It is done: do not repeat it."
                )
            else:
                err = result.error
                notes.append(f"- Approval {approval_id} could not be applied: {err.code if err else '?'} "
                             f"{err.message if err else ''}")
        return notes

    def _resume_prompt(self, state: RunState, ckpt: Checkpoint | None, plan: RecoveryPlan, notes: list[str]) -> str:
        """恢复时追加给模型的说明（smolagents 里作为新的 TaskStep）：

        检查点之后发生了什么、哪些审批被批准 / 驳回 / 作废、哪些证据失效、预算用了多少、原任务是什么。
        """
        report = list(notes)
        # 检查点之后才结束的动作：恢复的对话记忆里没有它们，要明确告诉模型（尤其是已生效的写入）
        for action_id in plan.after_checkpoint:
            act = state.action(action_id)
            if act.origin != "model":
                continue
            done = " It is already applied; do not redo it." if (
                act.status is ActionStatus.SUCCEEDED and act.tool in WRITE_TOOLS) else ""
            report.append(f"- {act.tool_call_id} {act.tool} {act.target or ''} finished after that checkpoint: "
                          f"{act.status}.{done}")
        since = ckpt.created_at if ckpt else 0.0
        for ap in state.approvals.values():
            if ap.status is ApprovalStatus.REJECTED and (ap.decided_at or 0) > since:
                report.append(f"- Approval {ap.approval_id} ({ap.tool} {ap.target}) was REJECTED by {ap.decided_by}: "
                              f"{ap.reason}. Take another approach.")
            elif ap.status is ApprovalStatus.STALE:
                report.append(f"- Approval {ap.approval_id} ({ap.tool} {ap.target}) is void: {ap.reason}. "
                              "Re-read the file and propose the change again if it is still needed.")
        stale = [e for e in state.evidence if not e.valid]
        if stale:
            report.append("- Invalid evidence (redo it if you still need it): " + ", ".join(
                f"{e.evidence_id} {e.kind} {e.path or e.command or ''}".strip() for e in stale[-6:]))

        b = state.budget
        restored = (f"The conversation above was restored from checkpoint #{ckpt.seq}, taken after step {ckpt.step}."
                    if ckpt else "No checkpoint was available, so earlier conversation is not shown.")
        return "\n".join([
            f"Resuming run {state.run_id} (session {state.session}). {restored}",
            "Harness report:",
            *(report or ["- Nothing changed while the run was paused."]),
            f"Budget used so far: {b.used_steps}/{b.max_steps} steps, {b.used_tool_calls}/{b.max_tool_calls} tool calls.",
            f"Continue the original task:\n{state.task}\n\n{TASK_RULES[state.kind]}",
        ])

    def _blocked_detail(self, plan: RecoveryPlan) -> str:
        if plan.blocked is StopReason.UNKNOWN_RESULT:
            return f"actions with unknown result need human verification: {', '.join(plan.unresolved_unknown)}"
        return f"workspace changed outside the harness: {', '.join(plan.drift)}"

    def _decide(self, run_id: str, approval_id: str, *, actor: str, grant: bool, reason: str | None) -> Approval:
        """审批决定的公共实现：只能处理 PENDING 的审批，审计里 actor 记为 human:<名字>。"""
        with self.store.lock(run_id):
            state = self.store.load(run_id)
            ap = state.approvals.get(approval_id)
            if ap is None:
                raise KeyError(f"unknown approval {approval_id}")
            if ap.status is not ApprovalStatus.PENDING:
                raise ValueError(f"{approval_id} is {ap.status}, not PENDING")
            ap.status = ApprovalStatus.GRANTED if grant else ApprovalStatus.REJECTED
            ap.decided_by = actor
            ap.decided_at = time.time()
            ap.reason = reason
            self.store.save(state)
            self._log(state, "approval.granted" if grant else "approval.rejected", actor=f"human:{actor}",
                      decision="granted" if grant else "rejected", rule="approval.human_decision",
                      tool_call_id=ap.tool_call_id, tool=ap.tool, details={
                          "approval_id": approval_id, "target": ap.target, "args_hash": ap.args_hash, "reason": reason,
                      })
            return ap

    def _current_hash(self, state: RunState, rel: str | None) -> str | None:
        if not rel:
            return None
        path = Path(state.workspace) / rel
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None

    def _transition(self, state: RunState, to: RunStatus, reason: StopReason | None, detail: str | None) -> None:
        """状态迁移 + 审计。合法性由 RunState.transition 按状态机校验。"""
        if state.status is to:
            return
        previous = state.transition(to, reason, detail)
        self._log(state, "state.transition", actor="harness", decision=str(to),
                  rule=str(reason) if reason else None, details={"from": str(previous), "to": str(to), "detail": detail})

    def _log(self, state: RunState, event: str, *, actor: str, decision: str | None = None, rule: str | None = None,
             tool_call_id: str | None = None, tool: str | None = None, step: int | None = None,
             details: dict[str, Any] | None = None) -> None:
        self.audit.append(run_id=state.run_id, event=event, actor=actor, decision=decision, rule=rule,
                          tool_call_id=tool_call_id, tool=tool, step=step, details=details)

    def _outcome(self, state: RunState) -> RunOutcome:
        return RunOutcome(
            run_id=state.run_id,
            status=state.status,
            stop_reason=state.stop_reason,
            detail=state.stop_detail,
            final_answer=state.final_answer if state.status is RunStatus.COMPLETED else None,
            steps_used=state.budget.used_steps,
            pending_approvals=[
                {"approval_id": a.approval_id, "tool": a.tool, "target": a.target, "tool_call_id": a.tool_call_id,
                 "preview": a.preview}
                for a in state.pending_approvals()
            ],
            unknown_actions=[a.action_id for a in state.unknown_results()],
        )
