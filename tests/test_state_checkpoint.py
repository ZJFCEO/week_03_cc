"""C1-C4：状态落盘、检查点内容、结果未知不重放、换进程恢复、恢复时工作区已变化。

C3 用真实子进程跑 harness_cli.py：每条命令都是独立的 Python 进程，内存里什么都不共享，
只能靠 state.json / 检查点 / 审计文件恢复。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from checkpoint import CheckpointManager, UnsafeCheckpoint
from fake_model import call, turn
from state_store import (
    ActionRecord,
    ActionStatus,
    ApprovalStatus,
    Budget,
    EvidenceKind,
    IllegalTransition,
    PlanItem,
    RunState,
    RunStatus,
    StateStore,
    StopReason,
    TaskKind,
)
from support import AUTO_APPROVE, ROOT, TASK, HandlerSpy, copy_demo, events, fixed_pricing, make_harness


# ---------------- 状态存储 ----------------


def test_state_roundtrip_keeps_full_business_state(tmp_path):
    store = StateStore(tmp_path)
    state = RunState.new(task="t", kind=TaskKind.TEST_FIX, workspace="/ws", budget=Budget(max_steps=5))
    state.plan = [PlanItem(step="run tests", status="done")]
    state.actions.append(ActionRecord(action_id="act_0001", tool_call_id="c1", tool="read_file", step=1,
                                      status=ActionStatus.SUCCEEDED, created_at=time.time()))
    state.transition(RunStatus.PAUSED, StopReason.INTERRUPTED, "ctrl-c")
    store.save(state)

    loaded = store.load(state.run_id)
    assert loaded == state
    assert loaded.stop_reason is StopReason.INTERRUPTED and loaded.plan[0].status == "done"


def test_illegal_transitions_are_rejected():
    state = RunState.new(task="t", kind=TaskKind.TEST_FIX, workspace="/ws", budget=Budget())
    state.transition(RunStatus.COMPLETED, StopReason.COMPLETED)
    with pytest.raises(IllegalTransition):
        state.transition(RunStatus.RUNNING)  # 终态不能再回到运行


# ---------------- C1 每个安全步骤后保存 Checkpoint ----------------


def test_c1_checkpoint_after_every_step_contains_business_state(tmp_path):
    ws = copy_demo(tmp_path / "ws")
    fixed = fixed_pricing(ws)
    h, _ = make_harness(tmp_path / "state", [
        turn(call("update_plan", "c1", items=[{"step": "reproduce", "status": "in_progress"}, {"step": "fix pricing"}])),
        turn(call("run_test", "c2", command="pytest -q")),
        turn(call("read_file", "c3", path="src/shop/pricing.py")),
        turn(call("write_file", "c4", path="src/shop/pricing.py", content=fixed)),
    ])
    out = h.start(workspace=ws, task=TASK, kind="test_fix")
    assert out.status is RunStatus.WAITING_APPROVAL

    files = h.checkpoints.history(out.run_id)
    data = [json.loads(p.read_text()) for p in files]
    assert [d["step"] for d in data] == [1, 2, 3, 4]  # 每步一个，最后一步就是进入等待审批的那一步
    assert all(d["reason"] == "step_boundary" for d in data)

    last = h.checkpoints.latest(out.run_id)
    assert last.checksum == last.compute_checksum()
    assert last.state.run_id == out.run_id and last.state.status is RunStatus.WAITING_APPROVAL  # run
    assert last.step == 4  # step
    assert [p.step for p in last.state.plan] == ["reproduce", "fix pricing"]  # 计划
    assert {e.kind for e in last.state.evidence} == {EvidenceKind.TEST_RUN, EvidenceKind.FILE_READ}  # 证据
    assert last.state.budget.used_steps == 4 and last.state.budget.used_tool_calls == 4  # 预算
    pending = last.pending_approvals  # 待审批动作，带完整参数，恢复后可直接执行
    assert [(a.approval_id, a.tool_call_id, a.target) for a in pending] == [("apr_0001", "c4", "src/shop/pricing.py")]
    assert pending[0].args["content"] == fixed
    assert [s["tool_calls"][0]["id"] for s in last.memory if s["type"] == "action"] == ["c1", "c2", "c3", "c4"]
    assert last.workspace["src/shop/pricing.py"] == last.state.expected_files["src/shop/pricing.py"]


def test_c1_no_checkpoint_while_an_action_is_in_flight(tmp_path):
    state = RunState.new(task="t", kind=TaskKind.TEST_FIX, workspace="/ws", budget=Budget())
    state.actions.append(ActionRecord(action_id="act_0001", tool_call_id="c1", tool="write_file", risk="write",
                                      step=1, status=ActionStatus.STARTED, created_at=time.time()))
    with pytest.raises(UnsafeCheckpoint):
        CheckpointManager(tmp_path).save(state=state, memory=[], workspace={}, reason="step_boundary")


def test_corrupted_latest_checkpoint_falls_back_to_previous(tmp_path):
    ws = copy_demo(tmp_path / "ws")
    h, _ = make_harness(tmp_path / "state", [turn(call("list_files", "c1")), turn(call("read_file", "c2", path="README.md"))],
                        max_steps=2)
    run_id = h.start(workspace=ws, task=TASK, kind="test_fix").run_id
    newest = h.checkpoints.history(run_id)[-1]
    newest.write_text(newest.read_text()[:-40])  # 模拟写到一半断电

    ckpt = h.checkpoints.latest(run_id)
    assert ckpt is not None and ckpt.seq < int(newest.stem)
    assert newest.name in h.checkpoints.skipped


# ---------------- C2 写入完成后结果未保存 ----------------


class ProcessCrash(BaseException):
    """模拟进程在“文件已写完、结果还没落盘”时突然死掉；BaseException 不会被 Gate 当作普通工具错误吞掉。"""


def test_c2_write_done_but_result_not_saved_is_unknown_and_never_replayed(tmp_path):
    ws = copy_demo(tmp_path / "ws")
    fixed = fixed_pricing(ws)
    real_write = HandlerSpy().registry.get("write_file").handler

    def write_then_crash(ctx, args):
        real_write(ctx, args)  # 文件真的写进去了
        raise ProcessCrash("power loss right after the write")

    spy = HandlerSpy(overrides={"write_file": write_then_crash})
    state_dir = tmp_path / "state"
    h1, _ = make_harness(state_dir, [turn(call("write_file", "w1", path="src/shop/pricing.py", content=fixed))],
                         spy=spy, gate_policy=AUTO_APPROVE)
    with pytest.raises(ProcessCrash):
        h1.start(workspace=ws, task=TASK, kind="test_fix")

    run_id = StateStore(state_dir).list_runs()[0]
    assert StateStore(state_dir).load(run_id).actions[0].status is ActionStatus.STARTED  # 只有预写记录
    assert (ws / "src/shop/pricing.py").read_text() == fixed

    # 新的 Harness 实例（相当于新进程）恢复：模型脚本里准备了重写，但根本不该被调用
    h2, model2 = make_harness(state_dir, [turn(call("write_file", "w2", path="src/shop/pricing.py", content=fixed))],
                              spy=spy, gate_policy=AUTO_APPROVE)
    out = h2.resume(run_id)

    assert out.status is RunStatus.WAITING_VERIFICATION and out.stop_reason is StopReason.UNKNOWN_RESULT
    assert out.unknown_actions == ["act_0001"]
    assert model2.calls_made == 0 and spy.calls["write_file"] == 1  # 不得自动重放
    record = events(h2, run_id, "recovery.unknown_result")[0]
    assert record.rule == "recovery.no_auto_replay" and record.tool_call_id == "w1"
    assert record.details["hash_now"] == record.details["intended_hash"]  # 给核对人的线索：内容已是目标内容

    # 人工核对后才继续；同一个动作不会再执行
    h2.resolve_unknown(run_id, "act_0001", applied=True, actor="bob")
    h3, model3 = make_harness(state_dir, [
        turn(call("run_test", "t1", command="pytest -q")),
        turn(call("final_answer", "f1", answer="pricing fixed; full suite passes")),
    ], spy=spy, gate_policy=AUTO_APPROVE)
    final = h3.resume(run_id)
    assert final.status is RunStatus.COMPLETED
    assert spy.calls["write_file"] == 1


# ---------------- C3 正常中断后换进程恢复 ----------------


def _cli(state_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ROOT / "harness_cli.py"), "--state-dir", str(state_dir), *args],
                          capture_output=True, text=True, timeout=120, cwd=ROOT)


def _script(path: Path, *turns: dict) -> str:
    path.write_text(json.dumps({"turns": list(turns)}, ensure_ascii=False))
    return f"script:{path}"


def test_c3_resume_in_new_processes_keeps_progress_counters_and_writes_once(tmp_path):
    ws = copy_demo(tmp_path / "ws")
    fixed = fixed_pricing(ws)
    state_dir = tmp_path / "state"

    # 进程 1：跑到写入，进入等待审批后退出
    p1 = _cli(state_dir, "run", "--workspace", str(ws), "--kind", "test_fix", "--task", TASK,
              "--model", _script(tmp_path / "p1.json",
                                 turn(call("update_plan", "c1", items=[{"step": "reproduce"}, {"step": "fix"}])),
                                 turn(call("run_test", "c2", command="pytest -q")),
                                 turn(call("read_file", "c3", path="src/shop/pricing.py")),
                                 turn(call("write_file", "c4", path="src/shop/pricing.py", content=fixed))))
    assert p1.returncode == 3, p1.stdout + p1.stderr
    run_id = StateStore(state_dir).list_runs()[0]

    # 进程 2：人工审批
    assert _cli(state_dir, "approve", run_id, "apr_0001", "--actor", "alice").returncode == 0

    # 进程 3：恢复 → Harness 执行已审批写入 → 跑测试 → 等模型时用户按 Ctrl-C（正常中断）
    p3 = _cli(state_dir, "resume", run_id, "--script-log", str(tmp_path / "p3.log"),
              "--model", _script(tmp_path / "p3.json", turn(call("run_test", "c5", command="pytest -q")),
                                 {"interrupt": True}))
    assert p3.returncode == 5, p3.stdout + p3.stderr  # PAUSED

    # 进程 4：再次恢复 → 模型重复提交同一写入（被拒）→ 完成
    p4 = _cli(state_dir, "resume", run_id, "--script-log", str(tmp_path / "p4.log"),
              "--model", _script(tmp_path / "p4.json",
                                 turn(call("write_file", "c7", path="src/shop/pricing.py", content=fixed)),
                                 turn(call("final_answer", "c8", answer="Fixed tier boundary and rounding; 12 passed."))))
    assert p4.returncode == 0, p4.stdout + p4.stderr

    state = StateStore(state_dir).load(run_id)
    assert state.status is RunStatus.COMPLETED and state.session == 3
    # 找回进度、计数不清零：步号跨进程连续，工具调用累计
    assert [s.number for s in state.steps] == list(range(1, 9))
    assert [s.session for s in state.steps] == [1, 1, 1, 1, 2, 2, 3, 3]
    assert state.budget.used_steps == 8 and state.budget.used_tool_calls == 6
    # 已完成写入不重复：整个运行只有一次成功写入，重复提交被拒
    from audit import AuditLog
    log = AuditLog(state_dir / "audit.jsonl")
    writes = [r for r in log.records(run_id=run_id, event_prefix="exec.result") if r.tool == "write_file" and r.decision == "ok"]
    assert len(writes) == 1 and writes[0].details["result"]["sha256_after"] == state.expected_files["src/shop/pricing.py"]
    dup = [r for r in log.decision_chain(run_id, "c7") if r.event == "gate.decision"][0]
    assert dup.rule == "gate.duplicate_write"
    assert log.verify() > 0
    # 恢复后的模型确实看到了之前的对话和 Harness 的说明
    first_p3 = json.loads((tmp_path / "p3.log").read_text().splitlines()[0])["prompt"]
    assert '"tool_call_id": "c4"' in first_p3 and "APPROVAL_REQUIRED" in first_p3
    assert "Approval apr_0001 was granted by alice" in first_p3
    first_p4 = json.loads((tmp_path / "p4.log").read_text().splitlines()[0])["prompt"]
    assert '"tool_call_id": "c5"' in first_p4 and "Resuming run" in first_p4


# ---------------- C4 恢复时工作区已变化 ----------------


def test_c4_workspace_changed_while_paused_invalidates_evidence_and_approvals(tmp_path):
    ws = copy_demo(tmp_path / "ws")
    fixed = fixed_pricing(ws)
    spy = HandlerSpy()
    state_dir = tmp_path / "state"
    h1, _ = make_harness(state_dir, [
        turn(call("read_file", "c1", path="src/shop/pricing.py")),
        turn(call("run_test", "c2", command="pytest -q")),
        turn(call("write_file", "c3", path="src/shop/pricing.py", content=fixed)),
    ], spy=spy)
    run_id = h1.start(workspace=ws, task=TASK, kind="test_fix").run_id
    h1.approve(run_id, "apr_0001", actor="alice")

    # 暂停期间有人直接改了源码
    pricing = ws / "src/shop/pricing.py"
    pricing.write_text(pricing.read_text() + "\n# hotfix by someone else\n")

    h2, model2 = make_harness(state_dir, [turn(call("list_files", "x1"))], spy=spy)
    out = h2.resume(run_id)

    assert out.status is RunStatus.WAITING_VERIFICATION and out.stop_reason is StopReason.WORKSPACE_CHANGED
    assert model2.calls_made == 0 and spy.calls["write_file"] == 0  # 停下等核对，旧审批没有被执行
    state = h2.status(run_id)
    assert state.approvals["apr_0001"].status is ApprovalStatus.STALE  # 旧审批失效
    stale = {e.tool_call_id: e.invalidated_reason for e in state.evidence if not e.valid}
    assert set(stale) == {"c1", "c2"}  # 读过的文件、测试结果都失效
    drift = events(h2, run_id, "recovery.workspace_changed")[0]
    assert list(drift.details["files"]) == ["src/shop/pricing.py"]

    # 人工确认改动后可以继续，但旧审批不会复活：模型得重新提出修改
    h2.acknowledge_workspace(run_id, actor="carol")
    h3, model3 = make_harness(state_dir, [{"interrupt": True}], spy=spy)
    after = h3.resume(run_id)
    assert after.status is RunStatus.PAUSED and spy.calls["write_file"] == 0
    assert "apr_0001" in model3.last_prompt() and "is void" in model3.last_prompt()
