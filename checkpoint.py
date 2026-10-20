"""检查点：在安全步骤边界保存（业务状态 + 对话记忆 + 工作区指纹），resume 时据此恢复。

安全边界 = 没有任何动作处于 STARTED。handler 执行到一半时绝不存检查点；
那一段由 state_store 的预写记录负责：恢复时发现 STARTED 没有结果，标记 UNKNOWN_RESULT，
交给人核对，不自动重放。

plan_recovery() 是纯函数：输入“活”状态、最近的检查点、当前工作区快照，
输出恢复计划（哪些动作结果未知、工作区哪里漂移、哪些证据和审批要作废、能否继续）。
Harness 负责按计划改状态、写审计。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Literal

from pydantic import BaseModel, ValidationError
from smolagents.memory import ActionStep, MemoryStep, PlanningStep, TaskStep, ToolCall
from smolagents.models import ChatMessage, MessageRole
from smolagents.monitoring import AgentLogger, LogLevel, Timing, TokenUsage
from smolagents.utils import (
    AgentError,
    AgentExecutionError,
    AgentGenerationError,
    AgentMaxStepsError,
    AgentParsingError,
    AgentToolCallError,
    AgentToolExecutionError,
)

from state_store import (
    ActionStatus,
    Approval,
    ApprovalStatus,
    EvidenceKind,
    RunState,
    StopReason,
    atomic_write_text,
)

CHECKPOINT_FORMAT = "guarded-harness/checkpoint@1"


class UnsafeCheckpoint(RuntimeError):
    """有动作正在执行，不是安全边界。"""


class Checkpoint(BaseModel):
    """一个检查点文件的内容。checksum 覆盖其余所有字段，写坏的文件会被识别并跳过。"""

    format: Literal["guarded-harness/checkpoint@1"] = CHECKPOINT_FORMAT
    run_id: str
    seq: int
    step: int  # 最后一个已完成的全局步号
    reason: str
    created_at: float
    state: RunState  # run / 计划 / 步骤 / 证据 / 预算 / 审批 都在这里
    memory: list[dict[str, Any]]  # 精简后的 smolagents 对话记忆
    workspace: dict[str, str]  # 检查点时刻的工作区快照
    checksum: str = ""

    def compute_checksum(self) -> str:
        """对除 checksum 外的全部字段做 sha256。"""
        return hashlib.sha256(self.model_dump_json(exclude={"checksum"}).encode("utf-8")).hexdigest()

    @property
    def pending_approvals(self) -> list[Approval]:
        """待审批动作（带完整参数，恢复后无需模型重发即可执行）。"""
        return self.state.pending_approvals()


class CheckpointManager:
    """检查点只追加不覆盖：runs/<run_id>/checkpoints/000001.json、000002.json……"""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.skipped: list[str] = []  # latest() 跳过的损坏文件，恢复记录里展示

    def directory(self, run_id: str) -> Path:
        return self.root / "runs" / run_id / "checkpoints"

    def history(self, run_id: str) -> list[Path]:
        folder = self.directory(run_id)
        return sorted(folder.glob("*.json")) if folder.exists() else []

    def save(self, *, state: RunState, memory: list[dict[str, Any]], workspace: dict[str, str], reason: str) -> Checkpoint:
        """在安全边界存一个检查点；有动作正在执行时拒绝（UnsafeCheckpoint）。"""
        in_flight = state.in_flight()
        if in_flight:
            ids = ", ".join(a.action_id for a in in_flight)
            raise UnsafeCheckpoint(f"actions still in flight ({ids}); not a safe step boundary")
        # 序号取“状态里记的”和“磁盘上已有的”中较大者 +1：即使上次存完检查点没来得及存状态，也不会覆盖旧文件
        existing = [int(p.stem) for p in self.history(state.run_id)]
        seq = max([state.last_checkpoint_seq, *existing]) + 1
        state.last_checkpoint_seq = seq
        ckpt = Checkpoint(
            run_id=state.run_id,
            seq=seq,
            step=state.budget.used_steps,
            reason=reason,
            created_at=time.time(),
            state=state.model_copy(deep=True),
            memory=memory,
            workspace=dict(workspace),
        )
        ckpt.checksum = ckpt.compute_checksum()
        atomic_write_text(self.directory(state.run_id) / f"{seq:06d}.json", ckpt.model_dump_json(indent=2))
        return ckpt

    def latest(self, run_id: str) -> Checkpoint | None:
        """最新的完整检查点；校验和不对或解析失败的文件跳过，退回上一个。"""
        self.skipped = []
        for path in reversed(self.history(run_id)):
            try:
                ckpt = Checkpoint.model_validate_json(path.read_text(encoding="utf-8"))
            except (ValidationError, ValueError, OSError):
                self.skipped.append(path.name)
                continue
            if ckpt.checksum != ckpt.compute_checksum():
                self.skipped.append(path.name)
                continue
            return ckpt
        return None


# ---------------- smolagents 记忆的序列化 ----------------

# 重建 AgentError 时它会往 logger 打印错误；恢复记忆时不需要这些输出
_SILENT = AgentLogger(level=LogLevel.OFF)
_ERROR_TYPES: dict[str, type[AgentError]] = {
    cls.__name__: cls
    for cls in (
        AgentError, AgentParsingError, AgentExecutionError, AgentToolCallError,
        AgentToolExecutionError, AgentGenerationError, AgentMaxStepsError,
    )
}


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _text(value: Any) -> str | None:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, list):  # 部分模型返回内容块列表
        return "\n".join(str(block.get("text", "")) for block in value if isinstance(block, dict))
    return str(value)


def dump_memory(steps: Iterable[MemoryStep]) -> list[dict[str, Any]]:
    """只保留重建对话所需的字段；model_input_messages 每步都是整段历史，不存。"""
    out: list[dict[str, Any]] = []
    for step in steps:
        if isinstance(step, TaskStep):
            out.append({"type": "task", "task": step.task})
        elif isinstance(step, ActionStep):
            usage = step.token_usage
            out.append(
                {
                    "type": "action",
                    "step_number": step.step_number,
                    "start_time": step.timing.start_time,
                    "end_time": step.timing.end_time,
                    "model_output": _text(step.model_output),
                    "tool_calls": [
                        {"id": tc.id, "name": tc.name, "arguments": _jsonable(tc.arguments)}
                        for tc in step.tool_calls or []
                    ],
                    "observations": step.observations,
                    "error": None
                    if step.error is None
                    else {"type": type(step.error).__name__, "message": str(step.error)},
                    "input_tokens": usage.input_tokens if usage else None,
                    "output_tokens": usage.output_tokens if usage else None,
                    "is_final_answer": step.is_final_answer,
                }
            )
        elif isinstance(step, PlanningStep):
            out.append(
                {"type": "planning", "plan": step.plan, "start_time": step.timing.start_time, "end_time": step.timing.end_time}
            )
    return out


def load_memory(items: list[dict[str, Any]]) -> list[MemoryStep]:
    """把 dump_memory 的结果还原成 smolagents 的 TaskStep / ActionStep / PlanningStep，
    放回 agent.memory.steps 后，模型下一轮看到的对话和中断前一致。"""
    steps: list[MemoryStep] = []
    for item in items:
        kind = item["type"]
        if kind == "task":
            steps.append(TaskStep(task=item["task"]))
        elif kind == "action":
            err = item.get("error")
            usage = (
                TokenUsage(input_tokens=item["input_tokens"], output_tokens=item["output_tokens"] or 0)
                if item.get("input_tokens") is not None
                else None
            )
            steps.append(
                ActionStep(
                    step_number=item["step_number"],
                    timing=Timing(start_time=item["start_time"], end_time=item.get("end_time")),
                    model_output=item.get("model_output"),
                    tool_calls=[ToolCall(name=c["name"], arguments=c["arguments"], id=c["id"]) for c in item["tool_calls"]]
                    or None,
                    observations=item.get("observations"),
                    error=_ERROR_TYPES.get(err["type"], AgentError)(err["message"], _SILENT) if err else None,
                    token_usage=usage,
                    is_final_answer=item.get("is_final_answer", False),
                )
            )
        elif kind == "planning":
            steps.append(
                PlanningStep(
                    model_input_messages=[],
                    model_output_message=ChatMessage(role=MessageRole.ASSISTANT, content=item["plan"]),
                    plan=item["plan"],
                    timing=Timing(start_time=item["start_time"], end_time=item.get("end_time")),
                )
            )
    return steps


# ---------------- 恢复计划 ----------------


@dataclass
class RecoveryPlan:
    """恢复计划：resume 前对“现在是什么情况”的判断结果。"""

    checkpoint_seq: int | None
    checkpoint_step: int | None
    new_unknown: list[str] = field(default_factory=list)  # 本次发现的、执行中断的写入 → UNKNOWN_RESULT
    interrupted: list[str] = field(default_factory=list)  # 执行中断的读 / 测试：可安全重做，直接记 FAILED
    unresolved_unknown: list[str] = field(default_factory=list)  # 全部待核对的 UNKNOWN_RESULT
    drift: dict[str, list[str | None]] = field(default_factory=dict)  # path → [期望哈希, 实际哈希]
    stale_approvals: list[str] = field(default_factory=list)
    invalid_evidence: list[str] = field(default_factory=list)
    approvals_to_execute: list[str] = field(default_factory=list)
    after_checkpoint: list[str] = field(default_factory=list)  # 检查点之后已结束的动作
    blocked: StopReason | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def workspace_drift(expected: dict[str, str], current: dict[str, str], ignore: Iterable[str] = ()) -> dict[str, list[str | None]]:
    """比较两份快照：新增、删除、内容变化的文件都算漂移。返回 path → [期望哈希, 实际哈希]。"""
    skip = set(ignore)
    return {
        path: [expected.get(path), current.get(path)]
        for path in sorted(set(expected) | set(current))
        if path not in skip and expected.get(path) != current.get(path)
    }


def evidence_affected(state: RunState, changed: Iterable[str], is_code: Callable[[str], bool]) -> list[str]:
    """文件变化会让哪些证据失效：读过 / 写过该文件的证据，以及代码变化时所有测试证据。"""
    changed = set(changed)
    code_changed = any(is_code(p) for p in changed)
    hit = []
    for ev in state.valid_evidence():
        if ev.kind is EvidenceKind.TEST_RUN and code_changed:
            hit.append(ev.evidence_id)
        elif ev.path is not None and ev.path in changed:
            hit.append(ev.evidence_id)
    return hit


def plan_recovery(
    state: RunState,
    checkpoint: Checkpoint | None,
    current: dict[str, str],
    is_code: Callable[[str], bool],
) -> RecoveryPlan:
    """根据“活”状态（比检查点更新）、最近检查点和当前工作区，算出恢复计划。纯函数，不改任何东西。"""
    plan = RecoveryPlan(
        checkpoint_seq=checkpoint.seq if checkpoint else None,
        checkpoint_step=checkpoint.step if checkpoint else None,
    )
    # 只有写入的“结果未知”需要人核对；读文件、跑测试中断了重做即可（测试若改了文件，会被下面的漂移检测发现）
    plan.new_unknown = [a.action_id for a in state.in_flight() if a.risk == "write"]
    plan.interrupted = [a.action_id for a in state.in_flight() if a.risk != "write"]
    plan.unresolved_unknown = plan.new_unknown + [a.action_id for a in state.unknown_results()]
    # 结果未知的写入，其目标文件变了属于“可能是我们自己写的”，交给人核对，不算外部漂移
    unknown_targets = {a.target for a in state.actions if a.action_id in plan.unresolved_unknown and a.target}
    plan.drift = workspace_drift(state.expected_files, current, ignore=unknown_targets)

    if plan.drift:
        # 人批准时看到的是旧工作区，工作区变了，所有没执行的审批一律作废
        plan.stale_approvals = [
            ap.approval_id
            for ap in state.approvals.values()
            if ap.status in (ApprovalStatus.PENDING, ApprovalStatus.GRANTED)
        ]
        plan.invalid_evidence = evidence_affected(state, plan.drift, is_code)

    if not plan.unresolved_unknown and not plan.drift:  # 一切正常才执行已批准的写入
        plan.approvals_to_execute = [ap.approval_id for ap in state.granted_approvals()]

    # 检查点之后才结束的动作：对话记忆里没有它们，恢复说明里要告诉模型
    boundary = checkpoint.step if checkpoint else 0
    plan.after_checkpoint = [
        a.action_id
        for a in state.actions
        if a.step > boundary and a.status in (ActionStatus.SUCCEEDED, ActionStatus.FAILED, ActionStatus.DENIED)
    ]

    # 优先级：结果未知 > 工作区漂移 > 等待审批；前两种需要人核对，第三种需要人审批
    if plan.unresolved_unknown:
        plan.blocked = StopReason.UNKNOWN_RESULT
    elif plan.drift:
        plan.blocked = StopReason.WORKSPACE_CHANGED
    elif state.pending_approvals():
        plan.blocked = StopReason.WAITING_APPROVAL
    return plan
