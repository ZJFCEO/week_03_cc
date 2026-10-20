"""A1-A2：按 run_id + tool_call_id 还原决策链；审计 append-only、哈希链防篡改。"""

from __future__ import annotations

import json
import threading

import pytest

from audit import AuditIntegrityError, AuditLog
from fake_model import call, turn
from state_store import RunStatus
from support import TASK, copy_demo, events, fixed_pricing, make_harness


# ---------------- A1 查看一次被拒绝的调用 ----------------


def test_a1_denied_call_record_has_rule_decision_actor_and_tool_call_id(tmp_path):
    ws = copy_demo(tmp_path / "ws")
    h, _ = make_harness(tmp_path / "state", [
        turn(call("read_file", "call_escape", path="artifacts/../../.env")),
        turn(call("run_test", "call_shell", command="pytest -q; curl evil.example")),
    ], max_steps=2)
    run_id = h.start(workspace=ws, task=TASK, kind="test_fix").run_id

    chain = h.audit.decision_chain(run_id, "call_escape")
    assert [r.event for r in chain] == ["sandbox.check", "gate.decision"]
    final = chain[-1]
    assert final.run_id == run_id
    assert final.tool_call_id == "call_escape"
    assert final.decision == "deny"
    assert final.rule == "sandbox.path_escape"
    assert final.actor == "sandbox"
    assert final.details["code"] == "PATH_ESCAPE" and final.details["raw"] == "artifacts/../../.env"

    shell = [r for r in h.audit.decision_chain(run_id, "call_shell") if r.event == "gate.decision"][0]
    assert (shell.decision, shell.rule, shell.actor) == ("deny", "sandbox.shell_metachar", "sandbox")


# ---------------- A2 查看一次完整任务 ----------------


def test_a2_full_task_links_gate_sandbox_state_approval_and_result(tmp_path):
    ws = copy_demo(tmp_path / "ws")
    fixed = fixed_pricing(ws)
    state_dir = tmp_path / "state"
    h1, _ = make_harness(state_dir, [
        turn(call("run_test", "t1", command="pytest -q")),
        turn(call("write_file", "w1", path="src/shop/pricing.py", content=fixed)),
    ])
    run_id = h1.start(workspace=ws, task=TASK, kind="test_fix").run_id
    h1.approve(run_id, "apr_0001", actor="alice")
    h2, _ = make_harness(state_dir, [
        turn(call("run_test", "t2", command="pytest -q")),
        turn(call("final_answer", "f1", answer="Fixed bulk tier boundary and HALF_UP rounding; 12 passed.")),
    ])
    assert h2.resume(run_id).status is RunStatus.COMPLETED

    # 写入调用的决策链：Gate → Sandbox → 审批 → 状态 → 人工 → 执行 → 结果 → 证据
    chain = h2.audit.decision_chain(run_id, "w1")
    assert [(r.event, r.actor) for r in chain] == [
        ("sandbox.check", "sandbox"),
        ("gate.decision", "gate"),  # pending
        ("approval.requested", "gate"),
        ("state.transition", "gate"),  # RUNNING → WAITING_APPROVAL
        ("approval.granted", "human:alice"),
        ("sandbox.check", "sandbox"),  # 执行前再查一次落点
        ("approval.consumed", "harness"),
        ("gate.decision", "gate"),  # allow
        ("exec.started", "executor"),
        ("evidence.invalidated", "harness"),  # 旧的失败测试证据 t1
        ("evidence.recorded", "harness"),  # 写入证据
        ("exec.result", "executor"),
    ]
    assert chain[1].decision == "pending" and chain[7].decision == "allow"
    approval_id = chain[2].details["approval_id"]
    assert chain[4].details["approval_id"] == approval_id == chain[6].details["approval_id"]
    written = chain[-1].details["result"]["sha256_after"]

    state = h2.status(run_id)
    assert state.approvals[approval_id].tool_call_id == "w1"  # 状态与审计用同一组 id 关联
    assert state.expected_files["src/shop/pricing.py"] == written
    executed = state.action(state.approvals[approval_id].executed_action_id)
    assert executed.origin == "harness" and executed.target_hash_after == written

    # 整个任务的时间线：创建、检查点、恢复、验收、完成都在
    timeline = [r.event for r in h2.audit.records(run_id=run_id)]
    for needed in ("run.created", "checkpoint.saved", "run.session_end", "run.resumed", "completion.check"):
        assert needed in timeline
    done = events(h2, run_id, "state.transition", tool_call_id="f1")[0]
    assert done.decision == "COMPLETED" and done.actor == "completion"
    verdict = events(h2, run_id, "completion.check")[0]
    assert verdict.decision == "accept"
    assert {c["name"] for c in verdict.details["checks"] if c["passed"]} >= {"source_changed", "fresh_passing_full_suite"}


# ---------------- append-only 与防篡改 ----------------


def _seed(path, n=5):
    log = AuditLog(path)
    for i in range(n):
        log.append(run_id="r1", event="gate.decision", actor="gate", decision="allow", tool_call_id=f"c{i}")
    return log


def test_audit_chain_verifies_and_detects_edits(tmp_path):
    log = _seed(tmp_path / "audit.jsonl")
    assert log.verify() == 5

    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    record = json.loads(lines[2])
    record["decision"] = "deny"  # 事后改一个决定
    lines[2] = json.dumps(record, sort_keys=True, separators=(",", ":"))
    (tmp_path / "audit.jsonl").write_text("\n".join(lines) + "\n")
    with pytest.raises(AuditIntegrityError, match="does not match its hash"):
        log.verify()


@pytest.mark.parametrize("tamper", ["delete", "reorder"])
def test_audit_chain_detects_deleted_or_reordered_lines(tmp_path, tamper):
    log = _seed(tmp_path / "audit.jsonl")
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    if tamper == "delete":
        del lines[1]
    else:
        lines[1], lines[2] = lines[2], lines[1]
    (tmp_path / "audit.jsonl").write_text("\n".join(lines) + "\n")
    with pytest.raises(AuditIntegrityError):
        log.verify()


def test_audit_log_has_no_update_or_delete_api():
    public = {name for name in dir(AuditLog) if not name.startswith("_")}
    assert public == {"append", "records", "decision_chain", "verify"}


def test_concurrent_writers_keep_one_consistent_chain(tmp_path):
    path = tmp_path / "audit.jsonl"
    writers = [AuditLog(path), AuditLog(path)]  # 两个实例模拟两个进程，靠 flock 互斥

    def work(log, k):
        for i in range(25):
            log.append(run_id="r1", event="exec.result", actor="executor", tool_call_id=f"t{k}-{i}")

    threads = [threading.Thread(target=work, args=(writers[k % 2], k)) for k in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert writers[0].verify() == 100
    assert [r.seq for r in writers[1].records()] == list(range(1, 101))
