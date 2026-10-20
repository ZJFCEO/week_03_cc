"""业务状态：Run、计划、步骤、动作、证据、预算、审批、停止原因。

RunState 是一次运行的全部业务事实，用 pydantic 描述、整份 JSON 原子落盘
（写临时文件 → fsync → rename）。完成、暂停、崩溃之后都能 load 出完整状态。

和 checkpoint 的分工：
    state_store  “活”的业务状态，每次关键变化都落盘，包括执行前的预写记录（STARTED）
    checkpoint   安全步骤边界上的快照，额外带上对话记忆和工作区指纹，用来 resume
"""

from __future__ import annotations

import fcntl
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterator, Literal

from pydantic import BaseModel, Field


class TaskKind(StrEnum):
    """两类仓库任务：代码理解（只读 + 写报告）、测试修复（改源码 + 跑测试）。"""

    CODE_UNDERSTANDING = "code_understanding"
    TEST_FIX = "test_fix"


class RunStatus(StrEnum):
    """运行状态。COMPLETED / STOPPED 是终态，其余都可以 resume。"""

    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    WAITING_VERIFICATION = "WAITING_VERIFICATION"  # 结果未知或工作区漂移，等人核对
    PAUSED = "PAUSED"  # 正常中断（Ctrl-C），可直接 resume
    COMPLETED = "COMPLETED"
    STOPPED = "STOPPED"  # 预算 / 轮数耗尽、用户取消，终态
    FAILED = "FAILED"  # 模型调用失败等，可 resume 重试


class StopReason(StrEnum):
    """为什么停下：每一种非 RUNNING 状态都必须带一个明确的原因，不允许“不明不白地结束”。"""

    COMPLETED = "COMPLETED"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    UNKNOWN_RESULT = "UNKNOWN_RESULT"
    WORKSPACE_CHANGED = "WORKSPACE_CHANGED"
    INTERRUPTED = "INTERRUPTED"
    CANCELLED = "CANCELLED"
    MAX_STEPS = "MAX_STEPS"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    MODEL_ERROR = "MODEL_ERROR"


TERMINAL_STATUSES = frozenset({RunStatus.COMPLETED, RunStatus.STOPPED})

# 状态机：只允许这些迁移，其余一律抛 IllegalTransition（比如 COMPLETED 不能回到 RUNNING）
ALLOWED_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.RUNNING: frozenset(
        {RunStatus.WAITING_APPROVAL, RunStatus.WAITING_VERIFICATION, RunStatus.PAUSED,
         RunStatus.COMPLETED, RunStatus.STOPPED, RunStatus.FAILED}
    ),
    RunStatus.WAITING_APPROVAL: frozenset({RunStatus.RUNNING, RunStatus.WAITING_VERIFICATION, RunStatus.STOPPED}),
    RunStatus.WAITING_VERIFICATION: frozenset({RunStatus.RUNNING, RunStatus.STOPPED}),
    RunStatus.PAUSED: frozenset({RunStatus.RUNNING, RunStatus.WAITING_VERIFICATION, RunStatus.STOPPED}),
    RunStatus.FAILED: frozenset({RunStatus.RUNNING, RunStatus.WAITING_VERIFICATION, RunStatus.STOPPED}),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.STOPPED: frozenset(),
}


class ActionStatus(StrEnum):
    """单个动作（一次工具调用）的状态。"""

    DENIED = "DENIED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    STARTED = "STARTED"  # 预写记录：handler 已开始，结果尚未落盘
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN_RESULT = "UNKNOWN_RESULT"  # 恢复时发现 STARTED 没有结果，不得自动重放


class ApprovalStatus(StrEnum):
    """审批单状态：PENDING → GRANTED → CONSUMED 是正常路径。"""

    PENDING = "PENDING"
    GRANTED = "GRANTED"
    REJECTED = "REJECTED"
    CONSUMED = "CONSUMED"  # 一次性：执行前即消费
    STALE = "STALE"  # 工作区已变化，审批失效


class EvidenceKind(StrEnum):
    """证据类型：完成验收只认有效的证据。"""

    FILE_READ = "FILE_READ"
    FILE_WRITE = "FILE_WRITE"
    TEST_RUN = "TEST_RUN"


class IllegalTransition(RuntimeError):
    pass


class RunLockedError(RuntimeError):
    """另一个进程正在驱动或修改这个 run。"""


class Budget(BaseModel):
    """预算：上限和已用量放在一起，随状态落盘，换进程恢复后继续累计（计数不清零）。"""

    max_steps: int = 20
    max_tool_calls: int = 60
    max_tokens: int | None = None
    max_wall_seconds: float | None = None
    used_steps: int = 0
    used_tool_calls: int = 0
    used_tokens: int = 0
    used_wall_seconds: float = 0.0

    @property
    def remaining_steps(self) -> int:
        return max(0, self.max_steps - self.used_steps)

    def exhausted(self) -> str | None:
        """返回第一个耗尽的预算名；步数由 MAX_STEPS 单独处理。"""
        if self.used_tool_calls >= self.max_tool_calls:
            return "tool_calls"
        if self.max_tokens is not None and self.used_tokens >= self.max_tokens:
            return "tokens"
        if self.max_wall_seconds is not None and self.used_wall_seconds >= self.max_wall_seconds:
            return "wall_seconds"
        return None


class PlanItem(BaseModel):
    step: str
    status: Literal["pending", "in_progress", "done"] = "pending"


class StepRecord(BaseModel):
    """一个模型轮次的摘要。"""

    number: int  # 全局步号，跨进程连续
    session: int  # 第几次运行会话，每次 resume +1
    tool_call_ids: list[str] = Field(default_factory=list)
    model_output: str | None = None
    error: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    started_at: float
    ended_at: float | None = None


class ActionRecord(BaseModel):
    """一次工具调用从进入 Gate 到有结果的全部事实。被拒绝的调用也有记录。"""

    action_id: str
    tool_call_id: str
    tool: str
    risk: str | None = None  # read / write / exec / state / complete；恢复时据此区分“结果未知的写入”
    origin: Literal["model", "harness"] = "model"
    step: int
    args: dict[str, Any] = Field(default_factory=dict)
    args_hash: str | None = None
    status: ActionStatus = ActionStatus.DENIED
    rule: str | None = None
    error: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    approval_id: str | None = None
    target: str | None = None  # 路径类工具的真实落点（相对路径）
    target_hash_before: str | None = None  # 写入前的文件哈希
    target_hash_after: str | None = None  # 写入后的文件哈希（用于幂等判断和互证）
    created_at: float
    finished_at: float | None = None


class Approval(BaseModel):
    """审批单：绑定完整参数和目标文件哈希，只能使用一次。"""

    approval_id: str
    tool_call_id: str
    action_id: str
    tool: str
    args: dict[str, Any]
    args_hash: str
    target: str | None
    target_hash: str | None  # 申请时目标文件的哈希，None 表示文件尚不存在
    code_fingerprint: str
    preview: str | None = None  # 给审批人看的 diff
    status: ApprovalStatus = ApprovalStatus.PENDING
    requested_at: float
    decided_by: str | None = None
    decided_at: float | None = None
    reason: str | None = None
    executed_action_id: str | None = None


class Evidence(BaseModel):
    """证据：读过什么、写了什么、测试在哪个代码版本上跑出了什么结果。只会被标记失效，不会被删除。"""

    evidence_id: str
    kind: EvidenceKind
    tool_call_id: str
    step: int
    path: str | None = None
    file_hash: str | None = None
    command: str | None = None
    exit_code: int | None = None
    passed: bool | None = None
    full_suite: bool | None = None
    summary: str | None = None
    code_fingerprint: str  # 产生证据时的代码指纹：测试证据只对这个版本有效
    valid: bool = True
    invalidated_reason: str | None = None
    created_at: float


class RepeatTracker(BaseModel):
    """连续相同调用计数器（G2），存在状态里，跨步骤、跨进程连续。"""

    last_signature: str | None = None
    streak: int = 0


class RunState(BaseModel):
    """一次运行的全部业务状态，也是检查点和运行记录的数据来源。"""

    run_id: str
    task: str
    kind: TaskKind
    workspace: str
    status: RunStatus = RunStatus.RUNNING
    stop_reason: StopReason | None = None
    stop_detail: str | None = None
    created_at: float
    updated_at: float
    session: int = 1
    budget: Budget = Field(default_factory=Budget)
    plan: list[PlanItem] = Field(default_factory=list)
    steps: list[StepRecord] = Field(default_factory=list)
    actions: list[ActionRecord] = Field(default_factory=list)
    approvals: dict[str, Approval] = Field(default_factory=dict)
    evidence: list[Evidence] = Field(default_factory=list)
    baseline_files: dict[str, str] = Field(default_factory=dict)  # 运行开始时的工作区快照
    expected_files: dict[str, str] = Field(default_factory=dict)  # Harness 认为现在应有的快照
    repeat: RepeatTracker = Field(default_factory=RepeatTracker)
    final_answer: str | None = None
    completion: dict[str, Any] | None = None
    last_checkpoint_seq: int = 0
    revision: int = 0

    @classmethod
    def new(cls, *, task: str, kind: TaskKind, workspace: str, budget: Budget) -> "RunState":
        now = time.time()
        run_id = f"run_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        return cls(run_id=run_id, task=task, kind=kind, workspace=workspace, budget=budget, created_at=now, updated_at=now)

    # ---- 状态机 ----

    def transition(self, to: RunStatus, reason: StopReason | None = None, detail: str | None = None) -> RunStatus:
        """按状态机迁移，返回迁移前的状态；回到 RUNNING 时清空停止原因。"""
        if to not in ALLOWED_TRANSITIONS[self.status]:
            raise IllegalTransition(f"{self.status} -> {to} is not allowed")
        previous = self.status
        self.status = to
        self.stop_reason = None if to is RunStatus.RUNNING else reason
        self.stop_detail = None if to is RunStatus.RUNNING else detail
        return previous

    # ---- 查询 ----

    def action(self, action_id: str) -> ActionRecord:
        for act in self.actions:
            if act.action_id == action_id:
                return act
        raise KeyError(action_id)

    def next_id(self, prefix: str) -> str:
        """生成 act_0001 / apr_0001 / ev_0001 这样的编号；在一次运行内单调递增，跨进程也不重复。"""
        counts = {"act": len(self.actions), "apr": len(self.approvals), "ev": len(self.evidence)}
        return f"{prefix}_{counts[prefix] + 1:04d}"

    def in_flight(self) -> list[ActionRecord]:
        """正在执行（只有预写记录、还没结果）的动作；非空时不是安全边界。"""
        return [a for a in self.actions if a.status is ActionStatus.STARTED]

    def unknown_results(self) -> list[ActionRecord]:
        return [a for a in self.actions if a.status is ActionStatus.UNKNOWN_RESULT]

    def pending_approvals(self) -> list[Approval]:
        return [a for a in self.approvals.values() if a.status is ApprovalStatus.PENDING]

    def granted_approvals(self) -> list[Approval]:
        return [a for a in self.approvals.values() if a.status is ApprovalStatus.GRANTED]

    def valid_evidence(self, kind: EvidenceKind | None = None) -> list[Evidence]:
        return [e for e in self.evidence if e.valid and (kind is None or e.kind is kind)]


def atomic_write_text(path: Path, text: str) -> None:
    """临时文件 + fsync + rename：读者要么看到旧版本，要么看到完整的新版本。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


class StateStore:
    """状态存储：每个 run 一个目录，state.json 整份原子覆盖写；另有写前原件和进程锁。"""

    def __init__(self, root: Path):
        self.root = Path(root)
        (self.root / "runs").mkdir(parents=True, exist_ok=True)

    def run_dir(self, run_id: str) -> Path:
        return self.root / "runs" / run_id

    def save(self, state: RunState) -> None:
        """整份原子落盘。revision 每次 +1，便于排查“哪个版本的状态”。"""
        state.revision += 1
        state.updated_at = time.time()
        atomic_write_text(self.run_dir(state.run_id) / "state.json", state.model_dump_json(indent=2))

    def load(self, run_id: str) -> RunState:
        path = self.run_dir(run_id) / "state.json"
        if not path.exists():
            raise KeyError(f"unknown run {run_id}")
        return RunState.model_validate_json(path.read_text(encoding="utf-8"))

    def list_runs(self) -> list[str]:
        return sorted(p.name for p in (self.root / "runs").iterdir() if (p / "state.json").exists())

    # ---- 写前原件：第一次写某个文件前保存原内容，运行记录里据此生成 diff ----

    def save_preimage(self, run_id: str, rel: str, data: bytes | None) -> None:
        target = self.run_dir(run_id) / "preimages" / rel
        marker = target.with_name(target.name + ".absent")
        if target.exists() or marker.exists():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        if data is None:
            marker.touch()
        else:
            target.write_bytes(data)

    def load_preimage(self, run_id: str, rel: str) -> bytes | None | Literal[False]:
        """返回原内容；None 表示写之前文件不存在；False 表示没有记录。"""
        target = self.run_dir(run_id) / "preimages" / rel
        if target.exists():
            return target.read_bytes()
        if target.with_name(target.name + ".absent").exists():
            return None
        return False

    # ---- 进程锁：同一个 run 同一时刻只允许一个进程推进 ----

    @contextmanager
    def lock(self, run_id: str) -> Iterator[None]:
        path = self.run_dir(run_id) / ".lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RunLockedError(f"run {run_id} is being driven by another process") from exc
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
