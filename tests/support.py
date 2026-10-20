"""测试公用工具：演示仓库副本、带计数器的 handler、Harness 工厂、观察结果解析。

所有测试只用 ScriptedModel（离线脚本模型），不依赖真实模型和网络。
"""

from __future__ import annotations

import json
import shutil
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from audit import AuditRecord
from coding_tools import build_registry
from fake_model import ScriptedModel, flatten
from gate import Gate, GatePolicy, GateResult, ToolCallRequest, ToolRegistry
from harness import GuardedHarness, HarnessConfig
from sandbox import Sandbox
from state_store import Budget, RunState, TaskKind

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo_repos" / "shop"
TASK = "tests/ 下有失败的测试，修复源码让 pytest -q 全部通过"


def copy_demo(dst: Path) -> Path:
    shutil.copytree(DEMO, dst, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    return dst


def fixed_pricing(ws: Path) -> str:
    """修好两处 bug 的 pricing.py：档位边界 >= 和 ROUND_HALF_UP。"""
    text = (ws / "src/shop/pricing.py").read_text(encoding="utf-8")
    return text.replace("ROUND_HALF_EVEN", "ROUND_HALF_UP").replace("quantity > threshold", "quantity >= threshold")


class HandlerSpy:
    """把注册表里每个 handler 包一层计数器：断言“handler 调用次数为 0 / 只执行两次”。"""

    def __init__(self, registry: ToolRegistry | None = None, overrides: dict[str, Callable] | None = None):
        reg = registry or build_registry()
        self.calls: Counter[str] = Counter()
        for name in reg.names():
            handler = (overrides or {}).get(name, reg.get(name).handler)
            reg = reg.with_handler(name, self._count(name, handler))
        self.registry = reg

    def _count(self, name: str, handler: Callable) -> Callable:
        def wrapped(ctx: Any, args: Any) -> Any:
            self.calls[name] += 1
            return handler(ctx, args)

        return wrapped


def make_harness(
    state_dir: Path,
    turns: list,
    *,
    spy: HandlerSpy | None = None,
    gate_policy: GatePolicy | None = None,
    **config: Any,
) -> tuple[GuardedHarness, ScriptedModel]:
    model = ScriptedModel(turns)
    cfg = HarnessConfig(**config)
    if gate_policy is not None:
        cfg.gate_policy = gate_policy
    harness = GuardedHarness(state_dir=state_dir, model=model, config=cfg, registry=spy.registry if spy else None)
    return harness, model


AUTO_APPROVE = GatePolicy(auto_approve_write_prefixes=("",))  # 只在关注别的场景时用：所有写入免审批


@dataclass
class GateEnv:
    """不经过模型、直接驱动 Gate 的测试环境（用于 G1-G4、S1-S3 的单元测试）。"""

    harness: GuardedHarness
    gate: Gate
    state: RunState
    sandbox: Sandbox
    spy: HandlerSpy
    ws: Path

    def submit(self, name: str, call_id: str, step: int = 1, **arguments: Any) -> GateResult:
        return self.gate.submit(self.state, ToolCallRequest(tool_call_id=call_id, name=name, arguments=arguments, step=step))

    def submit_raw(self, name: str, call_id: str, arguments: Any) -> GateResult:
        return self.gate.submit(self.state, ToolCallRequest(tool_call_id=call_id, name=name, arguments=arguments, step=1))

    def audit(self, **filters: Any) -> list[AuditRecord]:
        return self.harness.audit.records(run_id=self.state.run_id, **filters)


def gate_env(tmp_path: Path, kind: TaskKind = TaskKind.TEST_FIX, *, policy: GatePolicy | None = None,
             budget: Budget | None = None) -> GateEnv:
    ws = copy_demo(tmp_path / "ws")
    spy = HandlerSpy()
    config = HarnessConfig(gate_policy=policy or GatePolicy())
    harness = GuardedHarness(state_dir=tmp_path / "state", model=ScriptedModel([]), config=config, registry=spy.registry)
    state = RunState.new(task="unit", kind=kind, workspace=str(ws), budget=budget or Budget())
    ctx = harness._assemble(state)  # 与真实运行同一套装配（任务画像、Sandbox 策略、完成检查）
    state.baseline_files = ctx.sandbox.snapshot()
    state.expected_files = dict(state.baseline_files)
    harness.store.save(state)
    return GateEnv(harness=harness, gate=ctx.gate, state=state, sandbox=ctx.sandbox, spy=spy, ws=ws)


def observation(model: ScriptedModel, tool_call_id: str, request: int = -1) -> dict[str, Any]:
    """从模型第 request 次收到的 prompt 里找出某个 tool_call_id 的结构化观察结果。"""
    prefix = f'{{"tool_call_id": "{tool_call_id}"'
    for line in flatten(model.requests[request]).splitlines():
        if line.startswith(prefix):
            return json.loads(line)
    raise AssertionError(f"no observation for {tool_call_id} in request {request}")


def events(harness: GuardedHarness, run_id: str, event: str | None = None, **filters: Any) -> list[AuditRecord]:
    records = harness.audit.records(run_id=run_id, **filters)
    return [r for r in records if event is None or r.event == event]
