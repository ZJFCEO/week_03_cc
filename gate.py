"""Gate：所有工具调用的唯一入口。

模型发出的每个 tool call、以及 Harness 代为执行的已审批动作，都要走 submit() /
execute_approved()。检查顺序固定，任何一步拒绝都不会进入 handler，
返回带原 tool_call_id 的结构化错误：

    1. 运行状态        run 不在 RUNNING（等待审批、已取消……）时一律不执行
    2. 工具存在        UNKNOWN_TOOL
    3. 任务权限        当前任务类型没开放这个工具            PERMISSION_DENIED
    4. 重复动作        连续第 3 次相同工具 + 相同参数        REPEATED_CALL        (G2)
    5. 参数校验        缺字段 / 类型错 / 多字段              INVALID_ARGUMENTS    (G1)
    6. 执行边界        路径逃逸、敏感目录、命令白名单        Sandbox 的错误码     (S1-S3)
    7. 预算            工具调用次数                          BUDGET_EXHAUSTED
    8. 写入幂等        相同写入已生效且文件未变              DUPLICATE_WRITE      (G4)
    9. 审批            未审批的写入 → WAITING_APPROVAL        APPROVAL_REQUIRED    (G3)
   10. 完成验收        final_answer 交给 CompletionChecker   COMPLETION_REJECTED  (H1/H2)
   11. 执行            先落盘 STARTED（预写），再调 handler，再落盘结果

业务副作用（记证据、让旧证据失效、更新期望的工作区指纹、写计划、进入完成态）
都在 Gate 里统一处理，handler 只做 I/O。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Callable, Iterable, Literal, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator
from smolagents import Tool

from audit import AuditLog
from checkpoint import evidence_affected, workspace_drift
from sandbox import PreparedCommand, ResolvedPath, Sandbox, SandboxViolation, bound_text, fingerprint
from state_store import (
    ActionRecord,
    ActionStatus,
    Approval,
    ApprovalStatus,
    Evidence,
    EvidenceKind,
    PlanItem,
    RunState,
    RunStatus,
    StateStore,
    StopReason,
)


class Risk(StrEnum):
    """工具风险等级：决定走哪些检查、执行后产生哪些副作用。"""

    READ = "read"  # 只读文件：记录读取证据
    WRITE = "write"  # 改文件：幂等检查 + 审批 + 预写记录 + 让旧证据失效
    EXEC = "exec"  # 跑命令：命令白名单 + 超时 + 记录测试证据
    STATE = "state"  # 只改 Harness 自己的状态（计划）
    COMPLETE = "complete"  # final_answer：交给完成验收


class StrictArgs(BaseModel):
    """工具参数基类：多余字段、类型不符一律拒绝，不做隐式转换（"5" 不会变成 5）。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    @model_validator(mode="before")
    @classmethod
    def _optional_null_means_default(cls, data: Any) -> Any:
        # 给模型的 schema 把可选参数标成 nullable，显式传 null 等同于没传；必填字段传 null 照样报错。
        if isinstance(data, dict):
            return {
                k: v for k, v in data.items()
                if not (v is None and k in cls.model_fields and not cls.model_fields[k].is_required())
            }
        return data


class ToolError(Exception):
    """handler 报告的业务错误：文件不存在、替换目标不唯一、命令超时……"""

    def __init__(self, code: str, message: str, *, rule: str = "tool.error", actor: str = "executor", **details: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.rule = rule
        self.actor = actor
        self.details = details


class GateBypassError(RuntimeError):
    """有代码绕过 Gate 直接调用工具。"""


class CompletionVerdict(Protocol):
    """完成验收的结论（completion.Verdict 满足这个协议）。用 Protocol 避免 gate 反向依赖 completion。"""

    accepted: bool

    def failures(self) -> list[str]: ...

    def failed_checks(self) -> list[str]: ...

    def model_dump(self) -> dict[str, Any]: ...


class CompletionEvaluator(Protocol):
    """完成检查器接口：Gate 收到 final_answer 时调用。"""

    def evaluate(self, state: RunState, answer: str) -> CompletionVerdict: ...


@dataclass
class ToolContext:
    """handler 能拿到的全部东西：都是 Gate 检查过、解析好的，不给原始字符串。"""

    run_id: str
    tool_call_id: str
    sandbox: Sandbox  # 只能通过 Sandbox 的原语读写文件、起进程
    path: ResolvedPath | None  # 已通过路径校验的真实落点
    command: PreparedCommand | None  # 已通过白名单、规范化后的 argv
    cancel: threading.Event  # 用户取消信号，跑命令时用来终止进程组


Handler = Callable[[ToolContext, Any], Any]


@dataclass(frozen=True)
class ToolSpec:
    """一个工具的完整声明：参数 schema、handler、风险等级，以及 Gate 需要知道的元信息。

    业务规则都由这些声明驱动（哪个参数是路径、要不要审批、产生什么证据），
    而不是写在 handler 里。
    """

    name: str
    description: str  # 给模型看的说明
    args_model: type[BaseModel]  # 参数 schema，同时用来生成给模型的工具定义
    handler: Handler  # 只做 I/O
    risk: Risk
    path_arg: str | None = None  # 哪个参数是路径 → 交给 Sandbox.resolve
    command_arg: str | None = None  # 哪个参数是命令 → 交给 Sandbox.check_command
    requires_approval: bool = False  # 写操作是否需要人工审批（artifacts/ 等前缀可以免审批）
    preview: Callable[[ToolContext, Any], str] | None = None  # 审批前生成 diff，不产生副作用
    evidence: EvidenceKind | None = None  # 执行成功后记录哪类证据


class ToolRegistry:
    """工具注册表：Gate 按名字查 ToolSpec；同时生成给 smolagents 的“只带 schema 的工具外壳”。"""

    def __init__(self, specs: Iterable[ToolSpec]):
        self._specs = {spec.name: spec for spec in specs}

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def names(self) -> list[str]:
        return list(self._specs)

    def with_handler(self, name: str, handler: Handler) -> "ToolRegistry":
        """测试用：替换某个 handler（比如包一层计数器），返回新的注册表。"""
        specs = dict(self._specs)
        specs[name] = replace(specs[name], handler=handler)
        return ToolRegistry(specs.values())

    def smolagents_tools(self, names: Iterable[str]) -> list[Tool]:
        """给 ToolCallingAgent 的工具列表：模型据此看到工具定义，但这些对象本身执行不了任何事。"""
        return [_schema_carrier(self._specs[n]) for n in names if n in self._specs]


# ---------------- 给 smolagents 的工具外壳：只携带 schema，forward 永远不该被调用 ----------------


def _inline(prop: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    """把 pydantic 生成的 JSON schema 片段改写成 smolagents 认识的格式：
    展开 $ref，把 anyOf[X, null] 变成 type=X + nullable=True。"""
    if "$ref" in prop:
        prop = defs[prop["$ref"].rsplit("/", 1)[-1]]
    nullable = False
    if "anyOf" in prop:
        options = [o for o in prop["anyOf"] if o.get("type") != "null"]
        nullable = len(options) != len(prop["anyOf"])
        merged = _inline(options[0], defs) if options else {"type": "string"}
        prop = {**merged, **{k: v for k, v in prop.items() if k != "anyOf"}}
    out: dict[str, Any] = {"type": prop.get("type", "string")}
    if "enum" in prop:
        out["enum"] = prop["enum"]
    if out["type"] == "array" and "items" in prop:
        out["items"] = _inline(prop["items"], defs)
    if out["type"] == "object" and "properties" in prop:
        out["properties"] = {k: _inline(v, defs) for k, v in prop["properties"].items()}
        out["required"] = prop.get("required", [])
    if nullable:
        out["nullable"] = True
    return out


def schema_inputs(model: type[BaseModel]) -> dict[str, dict[str, Any]]:
    """pydantic 参数模型 → smolagents Tool.inputs。可选参数标 nullable，smolagents 就不会把它列为 required。"""
    schema = model.model_json_schema()
    defs = schema.get("$defs", {})
    required = set(schema.get("required", []))
    inputs: dict[str, dict[str, Any]] = {}
    for name, prop in schema.get("properties", {}).items():
        entry = _inline(prop, defs)
        entry["description"] = prop.get("description") or prop.get("title") or name
        if name not in required:
            entry["nullable"] = True
        inputs[name] = entry
    return inputs


def _schema_carrier(spec: ToolSpec) -> Tool:
    """动态生成一个 smolagents Tool 子类：只携带名字、说明和参数 schema。

    forward 永远抛 GateBypassError：即使有人绕开 Harness 直接调用工具对象，也执行不了。
    skip_forward_signature_validation 让 smolagents 不去校验 forward 的签名和 inputs 是否一致。
    """

    def forward(self, *args: Any, **kwargs: Any) -> Any:
        raise GateBypassError(f"tool '{spec.name}' can only be executed through the Gate")

    cls = type(
        f"Gated_{spec.name}",
        (Tool,),
        {
            "name": spec.name,
            "description": spec.description,
            "inputs": schema_inputs(spec.args_model),
            "output_type": "string",
            "skip_forward_signature_validation": True,
            "forward": forward,
        },
    )
    return cls()


# ---------------- 请求与结果 ----------------


@dataclass(frozen=True)
class ToolCallRequest:
    """进入 Gate 的一次调用请求。"""

    tool_call_id: str  # 模型给的原始 id，所有结果和审计都带回它
    name: str
    arguments: Any
    step: int  # 全局步号（跨进程连续）
    origin: Literal["model", "harness"] = "model"  # harness = 审批后由 Harness 代为执行
    batch_size: int = 1  # 同一步里模型一共发了几个调用


class GateError(BaseModel):
    """返回给模型的结构化错误。"""

    code: str  # 给模型判断用，如 PATH_ESCAPE、APPROVAL_REQUIRED
    rule: str  # 命中的规则，和审计里的 rule 一致
    message: str  # 人能读懂的说明（不含宿主机绝对路径）
    details: dict[str, Any] = {}


# 这些大段文本不塞进 JSON，单独附在观察结果后面，模型读起来更自然
RAW_TEXT_FIELDS = ("content", "output")


class GateResult(BaseModel):
    """Gate 的输出：放行并执行成功、放行但执行失败、拒绝、等待审批，四种情况统一用它表示。"""

    tool_call_id: str
    tool: str
    decision: Literal["allow", "deny", "pending"]
    ok: bool
    output: Any = None
    error: GateError | None = None
    action_id: str | None = None
    is_final_answer: bool = False

    def to_observation(self, limit: int = 24_000) -> str:
        """转成模型看到的观察文本：第一行是 JSON（含 tool_call_id / ok / decision / result 或 error），
        文件内容、测试输出这类大段文本放在后面的 <content> / <output> 块里，最后整体限长。"""
        head: dict[str, Any] = {
            "tool_call_id": self.tool_call_id,
            "tool": self.tool,
            "ok": self.ok,
            "decision": self.decision,
        }
        blocks: list[tuple[str, str]] = []
        if self.ok:
            result = self.output
            if isinstance(result, dict):
                result = dict(result)
                for key in RAW_TEXT_FIELDS:
                    if isinstance(result.get(key), str):
                        blocks.append((key, result.pop(key)))
            head["result"] = result
        else:
            head["error"] = self.error.model_dump() if self.error else None
        text = json.dumps(head, ensure_ascii=False, default=str)
        for key, body in blocks:
            text += f"\n<{key}>\n{body}\n</{key}>"
        return bound_text(text, limit)[0]


@dataclass(frozen=True)
class GatePolicy:
    """Gate 的可配置项；allowed_tools 由 Harness 按任务类型填入。"""

    max_identical_consecutive: int = 2  # 连续相同调用最多放行 2 次，第 3 次执行前拦截
    auto_approve_write_prefixes: tuple[str, ...] = ("artifacts/",)  # 按“真实落点”判断，"" 表示全部免审批
    allowed_tools: frozenset[str] | None = None  # None = 不限制
    observation_limit: int = 24_000  # 单个观察结果的最大字符数


class Denied(Exception):
    """Gate 内部用的“拒绝”信号：任何一步检查不通过就抛出，由 submit() 统一转成结构化错误。"""

    def __init__(self, code: str, rule: str, message: str, *, actor: str = "gate", **details: Any):
        super().__init__(message)
        self.code = code
        self.rule = rule
        self.message = message
        self.actor = actor
        self.details = details


# 返回给模型的 details 只挑这些字段；完整细节（含宿主机绝对路径）只进审计
_MODEL_VISIBLE = {
    "raw", "matched", "command", "streak", "errors", "approval_id", "target", "status",
    "failed_checks", "previous_tool_call_id", "available", "timeout_seconds",
}


def _public(details: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in details.items() if k in _MODEL_VISIBLE}


def args_digest(tool: str, arguments: Any) -> str:
    """工具名 + 参数的规范化哈希（键排序后序列化）：用于识别“相同调用”和“相同写入”。"""
    canonical = json.dumps(
        {"tool": tool, "arguments": arguments}, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def summarize(value: Any, limit: int = 160) -> Any:
    """审计和状态里不存大段内容：长字符串换成哈希 + 长度 + 开头预览。"""
    if isinstance(value, str) and len(value) > limit:
        return {
            "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
            "chars": len(value),
            "preview": value[:60],
        }
    if isinstance(value, dict):
        return {k: summarize(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [summarize(v, limit) for v in value[:20]]
    return value


def _hash_or_none(rp: ResolvedPath) -> str | None:
    """文件当前内容的 sha256；文件不存在返回 None。"""
    return hashlib.sha256(rp.abs.read_bytes()).hexdigest() if rp.abs.is_file() else None


class Gate:
    """所有工具调用的唯一入口。只有两个公开入口：submit()（模型发起）和 execute_approved()（审批后代执行）。"""

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        sandbox: Sandbox,
        store: StateStore,
        audit: AuditLog,
        completion: CompletionEvaluator,
        policy: GatePolicy | None = None,
        cancel_event: threading.Event | None = None,
    ):
        self.registry = registry
        self.sandbox = sandbox
        self.store = store
        self.audit = audit
        self.completion = completion
        self.policy = policy or GatePolicy()
        self.cancel_event = cancel_event or threading.Event()

    # ================= 入口一：模型发起的调用 =================

    def submit(self, state: RunState, req: ToolCallRequest) -> GateResult:
        """按固定顺序检查一次模型发起的调用；任何一步不通过都在执行前返回结构化错误。"""
        action = self._open_action(state, req)  # 先建动作记录：被拒绝的调用也要留痕
        spec = self.registry.get(req.name)
        try:
            self._check_active(state)  # 1. 运行状态
            if spec is None:  # 2. 工具存在
                raise Denied(
                    "UNKNOWN_TOOL", "gate.unknown_tool", f"unknown tool '{req.name}'", available=self.registry.names()
                )
            action.risk = str(spec.risk)
            self._check_permission(spec)  # 3. 任务权限
            self._check_repetition(state, req)  # 4. 重复动作（G2）
            args = self._validate(spec, req.arguments)  # 5. 参数校验（G1）
            action.args = args.model_dump(mode="json")
            action.args_hash = args_digest(spec.name, action.args)
            path, command = self._check_sandbox(state, req, spec, args)  # 6. 执行边界（S1-S3）
            action.target = path.rel if path else None  # 之后即使被拒绝，记录里也能看到目标文件
            self._check_budget(state, spec)  # 7. 工具调用预算
            if spec.risk is Risk.WRITE:
                assert path is not None
                self._check_duplicate_write(state, action, path)  # 8. 写入幂等（G4）
                if self._needs_approval(spec, path):  # 9. 审批（G3）：不执行，run 进入 WAITING_APPROVAL
                    return self._request_approval(state, action, req, spec, args, path)
            if spec.risk is Risk.COMPLETE:
                self._check_completion(state, req, args)  # 10. 完成验收（H1 / H2）
        except Denied as denied:
            return self._deny(state, action, req, denied)
        return self._execute(state, action, req, spec, args, path, command)  # 11. 执行

    # ================= 入口二：人工审批后由 Harness 代为执行 =================

    def execute_approved(self, state: RunState, approval_id: str) -> GateResult:
        """执行一张已批准的审批（resume 时由 Harness 调用）。

        审批是一次性的、绑定参数和目标文件的：已消费的拒绝（G4），
        目标文件在申请审批之后变过则审批作废（C4），执行前同样要过 Sandbox。
        """
        approval = state.approvals[approval_id]
        req = ToolCallRequest(
            tool_call_id=approval.tool_call_id,
            name=approval.tool,
            arguments=approval.args,
            step=state.budget.used_steps,
            origin="harness",
        )
        action = self._open_action(state, req)
        action.approval_id = approval_id
        spec = self.registry.get(approval.tool)
        try:
            if approval.status is ApprovalStatus.CONSUMED:
                raise Denied(
                    "APPROVAL_CONSUMED",
                    "approval.consumed",
                    f"approval {approval_id} was already used by {approval.executed_action_id}; the write is not repeated",
                    approval_id=approval_id,
                )
            if approval.status is not ApprovalStatus.GRANTED:
                raise Denied(
                    "APPROVAL_NOT_GRANTED", "approval.not_granted", f"approval {approval_id} is {approval.status}",
                    approval_id=approval_id, status=str(approval.status),
                )
            self._check_active(state)
            assert spec is not None
            action.risk = str(spec.risk)
            args = self._validate(spec, approval.args)
            action.args = args.model_dump(mode="json")
            action.args_hash = args_digest(spec.name, action.args)
            path, _ = self._check_sandbox(state, req, spec, args)
            assert path is not None
            action.target = path.rel
            if _hash_or_none(path) != approval.target_hash:
                approval.status = ApprovalStatus.STALE
                approval.reason = "target changed after the approval was requested"
                self._audit(state, req, "approval.stale", actor="harness", decision="stale", rule="approval.stale",
                            details={"approval_id": approval_id, "target": path.rel})
                raise Denied(
                    "APPROVAL_STALE",
                    "approval.stale",
                    f"'{path.rel}' changed after approval {approval_id} was requested; the approval is void",
                    approval_id=approval_id,
                )
            self._check_duplicate_write(state, action, path)
        except Denied as denied:
            return self._deny(state, action, req, denied)

        # 先消费再执行：即使执行中途崩溃，这张审批也不会被第二次使用，结果交给 UNKNOWN_RESULT 流程核对。
        approval.status = ApprovalStatus.CONSUMED
        approval.executed_action_id = action.action_id
        self._audit(state, req, "approval.consumed", actor="harness", decision="consumed", rule="approval.single_use",
                    details={"approval_id": approval_id, "action_id": action.action_id})
        return self._execute(state, action, req, spec, args, path, None)

    # ================= 各项检查 =================

    def _open_action(self, state: RunState, req: ToolCallRequest) -> ActionRecord:
        """为这次调用新建一条动作记录（初始状态 DENIED，放行后再改）。"""
        action = ActionRecord(
            action_id=state.next_id("act"),
            tool_call_id=req.tool_call_id,
            tool=req.name,
            origin=req.origin,
            step=req.step,
            created_at=time.time(),
        )
        state.actions.append(action)
        return action

    def _check_active(self, state: RunState) -> None:
        """只有 RUNNING 状态才执行：进入等待审批后，同一步里排在后面的调用也一律不执行。"""
        if self.cancel_event.is_set():
            raise Denied("CANCELLED", "gate.cancelled", "the run was cancelled by the user; nothing more is executed")
        if state.status is not RunStatus.RUNNING:
            raise Denied(
                "RUN_NOT_ACTIVE",
                "gate.run_not_active",
                f"run is {state.status}; this call was not executed",
                status=str(state.status),
            )

    def _check_permission(self, spec: ToolSpec) -> None:
        """任务画像限制可用工具：比如代码理解任务没有 replace_in_file。"""
        allowed = self.policy.allowed_tools
        if allowed is not None and spec.name not in allowed:
            raise Denied("PERMISSION_DENIED", "gate.permission", f"tool '{spec.name}' is not available for this task")

    def _check_repetition(self, state: RunState, req: ToolCallRequest) -> None:
        """连续相同调用（工具名 + 原始参数完全一致）超过上限就拦截，防止模型原地打转。

        计数器存在 RunState 里，跨步骤、跨进程都连续；任何一个不同的调用都会把计数清零。
        Harness 代执行的调用不计入。
        """
        if req.origin != "model":
            return
        signature = args_digest(req.name, req.arguments)  # 用原始参数：参数不合法的重复调用也能识别
        tracker = state.repeat
        if tracker.last_signature == signature:
            tracker.streak += 1
        else:
            tracker.last_signature, tracker.streak = signature, 1
        if tracker.streak > self.policy.max_identical_consecutive:
            raise Denied(
                "REPEATED_CALL",
                "gate.repeated_call",
                f"identical '{req.name}' call #{tracker.streak} in a row was blocked; change approach instead of repeating it",
                streak=tracker.streak,
            )

    def _validate(self, spec: ToolSpec, arguments: Any) -> BaseModel:
        """严格参数校验：必须是 JSON 对象；缺字段、类型不符、多余字段都拒绝，错误逐字段列出。"""
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):  # 比如模型给了一段解析不了的 JSON 字符串
            raise Denied(
                "INVALID_ARGUMENTS",
                "gate.schema",
                f"arguments for '{spec.name}' must be a JSON object",
                errors=[{"field": "", "type": "not_an_object", "message": f"got {type(arguments).__name__}"}],
            )
        try:
            return spec.args_model.model_validate(arguments)
        except ValidationError as exc:
            errors = [
                {"field": ".".join(str(p) for p in err["loc"]), "type": err["type"], "message": err["msg"]}
                for err in exc.errors()
            ]
            raise Denied("INVALID_ARGUMENTS", "gate.schema", f"invalid arguments for '{spec.name}'", errors=errors) from exc

    def _check_sandbox(
        self, state: RunState, req: ToolCallRequest, spec: ToolSpec, args: BaseModel
    ) -> tuple[ResolvedPath | None, PreparedCommand | None]:
        """路径交给 Sandbox.resolve，命令交给 Sandbox.check_command；放行和拒绝都写一条 sandbox.check 审计。"""
        if spec.path_arg is None and spec.command_arg is None:
            return None, None
        path = command = None
        access = "write" if spec.risk is Risk.WRITE else "read"
        try:
            if spec.path_arg is not None:
                path = self.sandbox.resolve(getattr(args, spec.path_arg), access=access)
            if spec.command_arg is not None:
                command = self.sandbox.check_command(getattr(args, spec.command_arg))
        except SandboxViolation as v:
            self._audit(state, req, "sandbox.check", actor="sandbox", decision="deny", rule=v.rule,
                        details={"code": v.code, "message": v.message, "access": access, **v.details})
            raise Denied(v.code, v.rule, v.message, actor="sandbox", **v.details) from v
        self._audit(
            state, req, "sandbox.check", actor="sandbox", decision="allow", rule="sandbox.ok",
            details={"access": access, "path": path.rel if path else None, "command": command.display if command else None},
        )
        return path, command

    def _check_budget(self, state: RunState, spec: ToolSpec) -> None:
        """工具调用次数预算。final_answer 不计数，避免“预算刚好用完时连答案都交不了”。"""
        if spec.risk is Risk.COMPLETE:
            return
        budget = state.budget
        if budget.used_tool_calls >= budget.max_tool_calls:
            raise Denied(
                "BUDGET_EXHAUSTED",
                "gate.budget",
                f"tool call budget used up ({budget.used_tool_calls}/{budget.max_tool_calls})",
            )
        budget.used_tool_calls += 1

    def _check_duplicate_write(self, state: RunState, action: ActionRecord, path: ResolvedPath) -> None:
        """同样的写入（工具 + 参数完全相同）已经成功过、且文件从那以后没变，就不再写第二次。

        只比较“参数哈希 + 写后哈希”：如果文件之后又被改过，同样的写入是有意义的新改动，交给审批流程。
        """
        current = _hash_or_none(path)
        for prev in state.actions:
            if prev is action or prev.status is not ActionStatus.SUCCEEDED:
                continue
            if prev.args_hash == action.args_hash and prev.target_hash_after == current:
                raise Denied(
                    "DUPLICATE_WRITE",
                    "gate.duplicate_write",
                    f"identical write was already applied by {prev.tool_call_id} ({prev.action_id}) "
                    "and the file has not changed since; it is not written again",
                    previous_tool_call_id=prev.tool_call_id,
                    previous_action_id=prev.action_id,
                )

    def _needs_approval(self, spec: ToolSpec, path: ResolvedPath) -> bool:
        """用 Sandbox 解析后的真实相对路径判断免审批前缀，artifacts/../src/x.py 骗不过去。"""
        if not spec.requires_approval:
            return False
        return not any(path.rel.startswith(prefix) for prefix in self.policy.auto_approve_write_prefixes)

    def _request_approval(
        self, state: RunState, action: ActionRecord, req: ToolCallRequest, spec: ToolSpec, args: BaseModel, path: ResolvedPath
    ) -> GateResult:
        """未审批的写入（G3）：不执行，生成一张待审批单，run 进入 WAITING_APPROVAL，目标文件保持不变。

        审批单绑定完整参数（含参数哈希）和目标文件当前哈希；恢复后由 Harness 直接执行这张单，
        不依赖模型把同样的调用再发一遍。
        """
        preview = None
        if spec.preview is not None:
            try:
                preview = spec.preview(self._context(state, req, path, None), args)
            except ToolError as exc:  # 改不了的编辑没必要拿去问人
                raise Denied(exc.code, exc.rule, exc.message, actor=exc.actor, **exc.details) from exc
        now = time.time()
        approval_id = state.next_id("apr")
        target_hash = _hash_or_none(path)
        state.approvals[approval_id] = Approval(
            approval_id=approval_id,
            tool_call_id=req.tool_call_id,
            action_id=action.action_id,
            tool=spec.name,
            args=action.args,
            args_hash=action.args_hash or "",
            target=path.rel,
            target_hash=target_hash,
            code_fingerprint=fingerprint(self.sandbox.code_files(state.expected_files)),
            preview=preview,
            requested_at=now,
        )
        # 动作记录、状态迁移、审计三件事：先落盘状态，再写审计
        action.status = ActionStatus.PENDING_APPROVAL
        action.approval_id = approval_id
        action.rule = "approval.required"
        action.target = path.rel
        action.target_hash_before = target_hash
        action.finished_at = now
        previous = state.transition(
            RunStatus.WAITING_APPROVAL, StopReason.WAITING_APPROVAL, f"{approval_id}: {spec.name} {path.rel}"
        )
        self.store.save(state)
        self._audit(state, req, "gate.decision", actor="gate", decision="pending", rule="approval.required",
                    details={"origin": req.origin, "args": summarize(action.args), "approval_id": approval_id})
        self._audit(state, req, "approval.requested", actor="gate", decision="pending", rule="approval.required",
                    details={"approval_id": approval_id, "target": path.rel, "target_hash": target_hash,
                             "args_hash": action.args_hash})
        self._audit(state, req, "state.transition", actor="gate", decision=str(RunStatus.WAITING_APPROVAL),
                    rule="approval.required", details={"from": str(previous), "to": str(state.status)})
        return GateResult(
            tool_call_id=req.tool_call_id,
            tool=req.name,
            decision="pending",
            ok=False,
            error=GateError(
                code="APPROVAL_REQUIRED",
                rule="approval.required",
                message=f"writing '{path.rel}' needs human approval ({approval_id}); the run is paused until a human "
                "decides, so do not retry this call",
                details={"approval_id": approval_id, "target": path.rel},
            ),
            action_id=action.action_id,
        )

    def _check_completion(self, state: RunState, req: ToolCallRequest, args: BaseModel) -> None:
        """final_answer 的验收（H1 / H2）：模型说完成不算，CompletionChecker 的每一项都通过才放行。"""
        if req.batch_size > 1:  # 和其他调用混在同一步里：其他调用的结果模型还没看到，不能据此宣布完成
            raise Denied(
                "FINAL_ANSWER_NOT_ALONE",
                "completion.not_alone",
                "final_answer must be the only tool call in its step",
                actor="completion",
            )
        verdict = self.completion.evaluate(state, getattr(args, "answer"))
        state.completion = verdict.model_dump()
        self._audit(
            state, req, "completion.check", actor="completion",
            decision="accept" if verdict.accepted else "reject",
            rule="completion.accepted" if verdict.accepted else "completion.rejected",
            details=verdict.model_dump(),
        )
        if not verdict.accepted:
            raise Denied(
                "COMPLETION_REJECTED",
                "completion.rejected",
                "final answer rejected: " + "; ".join(verdict.failures()),
                actor="completion",
                failed_checks=verdict.failed_checks(),
            )

    # ================= 拒绝 / 执行 =================

    def _deny(self, state: RunState, action: ActionRecord, req: ToolCallRequest, denied: Denied) -> GateResult:
        """拒绝：记动作、写审计（含完整细节），返回给模型的只有结构化错误和原 tool_call_id。handler 从未被调用。"""
        action.status = ActionStatus.DENIED
        action.rule = denied.rule
        action.error = {"code": denied.code, "message": denied.message}
        action.finished_at = time.time()
        self.store.save(state)
        self._audit(
            state, req, "gate.decision", actor=denied.actor, decision="deny", rule=denied.rule,
            details={"code": denied.code, "message": denied.message, "origin": req.origin,
                     "args": summarize(req.arguments), **denied.details},
        )
        return GateResult(
            tool_call_id=req.tool_call_id,
            tool=req.name,
            decision="deny",
            ok=False,
            error=GateError(code=denied.code, rule=denied.rule, message=denied.message, details=_public(denied.details)),
            action_id=action.action_id,
        )

    def _context(
        self, state: RunState, req: ToolCallRequest, path: ResolvedPath | None, command: PreparedCommand | None
    ) -> ToolContext:
        """组装传给 handler（和 preview）的上下文。"""
        return ToolContext(
            run_id=state.run_id,
            tool_call_id=req.tool_call_id,
            sandbox=self.sandbox,
            path=path,
            command=command,
            cancel=self.cancel_event,
        )

    def _execute(
        self,
        state: RunState,
        action: ActionRecord,
        req: ToolCallRequest,
        spec: ToolSpec,
        args: BaseModel,
        path: ResolvedPath | None,
        command: PreparedCommand | None,
    ) -> GateResult:
        """真正执行 handler。顺序很关键：

            审计 allow → 落盘 STARTED（预写记录）→ 审计 exec.started → handler → 落盘结果 → 审计 exec.result

        进程在 handler 执行中或刚执行完时死掉，状态里留下的是 STARTED；恢复时据此识别“结果未知”（C2）。
        """
        self._audit(state, req, "gate.decision", actor="gate", decision="allow", rule="gate.allow",
                    details={"origin": req.origin, "args": summarize(action.args), "approval_id": action.approval_id})
        # 跑命令前记下代码指纹：测试证据绑定的是“运行时的代码版本”（H2）
        code_fp_before = self.sandbox.code_fingerprint() if spec.risk is Risk.EXEC else None
        if path is not None:
            action.target = path.rel
        if spec.risk is Risk.WRITE:
            assert path is not None
            # 第一次写这个文件前保存原件，运行记录据此生成 diff
            original = path.abs.read_bytes() if path.abs.is_file() else None
            action.target_hash_before = hashlib.sha256(original).hexdigest() if original is not None else None
            self.store.save_preimage(state.run_id, path.rel, original)
        action.status = ActionStatus.STARTED
        self.store.save(state)  # 预写记录：此后崩溃，恢复时就知道这一步“开始了但结果未知”
        self._audit(state, req, "exec.started", actor="executor",
                    details={"action_id": action.action_id, "target": action.target})

        try:
            output = spec.handler(self._context(state, req, path, command), args)
        except ToolError as exc:  # 业务错误：文件不存在、超时、被取消……
            return self._fail(state, action, req, exc.code, exc.rule, exc.message, exc.actor, exc.details)
        except SandboxViolation as exc:  # 执行时的二次校验失败（如检查后被换成软链接）
            return self._fail(state, action, req, exc.code, exc.rule, exc.message, "sandbox", exc.details)
        except Exception as exc:  # handler 自身的 bug 不能拖垮整个运行
            return self._fail(state, action, req, "TOOL_EXCEPTION", "tool.exception", f"{type(exc).__name__}: {exc}",
                              "executor", {})

        action.status = ActionStatus.SUCCEEDED
        if isinstance(output, dict):
            action.result = summarize({k: v for k, v in output.items() if k not in RAW_TEXT_FIELDS})
        else:
            action.result = {"value": summarize(output)}
        action.finished_at = time.time()
        self._apply_effects(state, action, req, spec, output, command, code_fp_before)
        self.store.save(state)
        self._audit(state, req, "exec.result", actor="executor", decision="ok",
                    details={"action_id": action.action_id, "result": action.result})
        if state.status is RunStatus.COMPLETED:
            self._audit(state, req, "state.transition", actor="completion", decision=str(RunStatus.COMPLETED),
                        rule="completion.accepted", details={"from": str(RunStatus.RUNNING), "to": str(state.status)})
        return GateResult(
            tool_call_id=req.tool_call_id,
            tool=req.name,
            decision="allow",
            ok=True,
            output=output,
            action_id=action.action_id,
            is_final_answer=spec.risk is Risk.COMPLETE,
        )

    def _fail(
        self, state: RunState, action: ActionRecord, req: ToolCallRequest,
        code: str, rule: str, message: str, actor: str, details: dict[str, Any],
    ) -> GateResult:
        """放行了但执行失败：结果是确定的（失败），记 FAILED，不是 UNKNOWN_RESULT。"""
        action.status = ActionStatus.FAILED
        action.rule = rule
        action.error = {"code": code, "message": message}
        action.finished_at = time.time()
        self.store.save(state)
        self._audit(state, req, "exec.result", actor=actor, decision="error", rule=rule,
                    details={"action_id": action.action_id, "code": code, "message": message, **summarize(details)})
        return GateResult(
            tool_call_id=req.tool_call_id,
            tool=req.name,
            decision="allow",
            ok=False,
            error=GateError(code=code, rule=rule, message=message, details=_public(details)),
            action_id=action.action_id,
        )

    # ================= 执行后的业务副作用 =================

    def _apply_effects(
        self, state: RunState, action: ActionRecord, req: ToolCallRequest, spec: ToolSpec,
        output: Any, command: PreparedCommand | None, code_fp_before: str | None,
    ) -> None:
        """执行成功后的业务副作用，统一在这里处理，handler 里一行都没有。"""
        if spec.risk is Risk.WRITE:
            action.target_hash_after = output["sha256_after"]
            self.record_write(state, req, rel=action.target or "", hash_after=output["sha256_after"])
        elif spec.evidence is EvidenceKind.FILE_READ:
            # 读取证据绑定文件哈希：之后这个文件被改，证据就失效
            self._add_evidence(state, req, Evidence(
                evidence_id=state.next_id("ev"), kind=EvidenceKind.FILE_READ, tool_call_id=req.tool_call_id,
                step=req.step, path=output["path"], file_hash=output["sha256"],
                code_fingerprint=fingerprint(self.sandbox.code_files(state.expected_files)), created_at=time.time(),
            ))
        elif spec.risk is Risk.EXEC:
            # 测试证据：命令、退出码、是否完整测试集、运行时的代码指纹
            assert command is not None and code_fp_before is not None
            after = self.sandbox.snapshot()
            stable = self.sandbox.code_fingerprint(after) == code_fp_before  # 运行前后代码没变，结果才可信
            if not stable:
                # 测试过程中代码被改了（比如被测代码写文件）：这次结果不可信，变化本身是我们看着发生的，不算外部漂移
                drift = workspace_drift(state.expected_files, after)
                self.invalidate_evidence(state, evidence_affected(state, drift, self.sandbox.is_code_path),
                                         "workspace changed while the command ran", req)
                state.expected_files = after
                self._audit(state, req, "workspace.mutated_by_command", actor="sandbox", decision="observed",
                            rule="evidence.unstable", details={"changed": sorted(drift)})
            self._add_evidence(state, req, Evidence(
                evidence_id=state.next_id("ev"), kind=EvidenceKind.TEST_RUN, tool_call_id=req.tool_call_id,
                step=req.step, command=command.display, exit_code=output.get("exit_code"),
                passed=output.get("passed"), full_suite=command.full_suite, summary=output.get("summary"),
                code_fingerprint=code_fp_before, valid=stable,
                invalidated_reason=None if stable else "workspace changed while the command ran",
                created_at=time.time(),
            ))
        elif spec.risk is Risk.STATE:
            state.plan = [PlanItem(**item) for item in output["items"]]  # 计划整表替换
        elif spec.risk is Risk.COMPLETE:
            # 能走到这里说明验收已通过：这是进入完成态的唯一路径
            state.final_answer = output
            state.transition(RunStatus.COMPLETED, StopReason.COMPLETED, "final answer accepted by completion check")

    def record_write(self, state: RunState, req: ToolCallRequest, *, rel: str, hash_after: str) -> None:
        """一次写入生效后的统一处理：更新期望指纹、作废依赖旧内容的证据、记一条写入证据。"""
        state.expected_files[rel] = hash_after
        self.invalidate_evidence(state, evidence_affected(state, [rel], self.sandbox.is_code_path),
                                 f"{rel} modified by {req.tool_call_id}", req)
        self._add_evidence(state, req, Evidence(
            evidence_id=state.next_id("ev"), kind=EvidenceKind.FILE_WRITE, tool_call_id=req.tool_call_id,
            step=req.step, path=rel, file_hash=hash_after,
            code_fingerprint=fingerprint(self.sandbox.code_files(state.expected_files)), created_at=time.time(),
        ))

    def _add_evidence(self, state: RunState, req: ToolCallRequest, evidence: Evidence) -> None:
        """记一条证据并写审计。证据只追加、只会被标记失效，不会被删除。"""
        state.evidence.append(evidence)
        self._audit(state, req, "evidence.recorded", actor="harness", decision="valid" if evidence.valid else "invalid",
                    details=evidence.model_dump(exclude={"created_at"}))

    def invalidate_evidence(
        self, state: RunState, evidence_ids: list[str], reason: str, req: ToolCallRequest | None = None
    ) -> None:
        """把证据标记为失效并写明原因（H2 / C4）。req 为空表示不是由某次工具调用引起的（比如恢复时发现漂移）。"""
        for ev in state.evidence:
            if ev.evidence_id in evidence_ids and ev.valid:
                ev.valid = False
                ev.invalidated_reason = reason
                self._audit(state, req, "evidence.invalidated", actor="harness", decision="invalid",
                            rule="evidence.stale", details={"evidence_id": ev.evidence_id, "kind": str(ev.kind),
                                                            "reason": reason})

    def _audit(
        self, state: RunState, req: ToolCallRequest | None, event: str, *, actor: str,
        decision: str | None = None, rule: str | None = None, details: dict[str, Any] | None = None,
    ) -> None:
        """写一条审计，自动带上 run_id / tool_call_id / tool / step，决策链靠这几个字段串起来。"""
        self.audit.append(
            run_id=state.run_id, event=event, actor=actor, decision=decision, rule=rule,
            tool_call_id=req.tool_call_id if req else None, tool=req.name if req else None,
            step=req.step if req else None, details=details,
        )
