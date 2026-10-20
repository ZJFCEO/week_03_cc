"""Agent 主循环：两类任务的正常路径，以及拒绝、驳回、模型异常、中断等失败路径。"""

from __future__ import annotations

import subprocess
import sys

from fake_model import call, turn
from state_store import ActionStatus, ApprovalStatus, RunStatus, StopReason
from support import TASK, HandlerSpy, copy_demo, events, fixed_pricing, make_harness, observation

UNDERSTAND_TASK = "阅读 shop 仓库，说明各模块职责、调用关系和折扣规则"
REPORT = """# shop 架构说明

- `src/shop/cart.py`：购物车。`Cart.subtotal()` 对每个明细行调用 `pricing.line_total`，`Cart.total()` 再叠加优惠券。
- `src/shop/pricing.py`：批量折扣（10 件起 5%，100 件起 15%）与两位小数舍入 `round_money`。
- `src/shop/coupons.py`：`SAVE10` 满 100 减 10，`VIP5` 全场 95 折，未知券码抛 `UnknownCoupon`。

调用关系：cart → pricing（逐行计价）→ coupons（整单优惠）。
"""


def test_test_fix_happy_path_end_to_end(tmp_path):
    ws = copy_demo(tmp_path / "ws")
    fixed = fixed_pricing(ws)
    state_dir = tmp_path / "state"
    h1, _ = make_harness(state_dir, [
        turn(call("update_plan", "p1", items=[{"step": "reproduce", "status": "in_progress"}, {"step": "fix"}])),
        turn(call("run_test", "t1", command="pytest -q")),
        turn(call("search_code", "s1", pattern="ROUND_HALF", path="src")),
        turn(call("read_file", "r1", path="src/shop/pricing.py")),
        turn(call("write_file", "w1", path="src/shop/pricing.py", content=fixed)),
    ])
    out1 = h1.start(workspace=ws, task=TASK, kind="test_fix")
    assert out1.status is RunStatus.WAITING_APPROVAL
    h1.approve(out1.run_id, "apr_0001", actor="alice")

    h2, model2 = make_harness(state_dir, [
        turn(call("run_test", "t2", command="pytest -q")),
        turn(call("update_plan", "p2", items=[{"step": "reproduce", "status": "done"}, {"step": "fix", "status": "done"}])),
        turn(call("final_answer", "f1", answer="Bulk tiers now include the threshold and money rounds HALF_UP; 12 passed.")),
    ])
    out2 = h2.resume(out1.run_id)

    assert out2.success and out2.final_answer.startswith("Bulk tiers")
    assert observation(model2, "t2", request=1)["result"]["summary"].startswith("12 passed")
    state = h2.status(out1.run_id)
    assert [p.status for p in state.plan] == ["done", "done"]
    assert state.completion["accepted"] is True
    # 独立再跑一遍测试，确认交付物真实有效
    check = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=ws,
                           capture_output=True, text=True)
    assert check.returncode == 0, check.stdout


def test_code_understanding_happy_path_writes_report_without_approval(tmp_path):
    ws = copy_demo(tmp_path / "ws")
    before = {p: p.read_bytes() for p in (ws / "src").rglob("*.py")}
    h, _ = make_harness(tmp_path / "state", [
        turn(call("list_files", "l1", path="src")),
        turn(call("read_file", "r1", path="src/shop/cart.py")),
        turn(call("read_file", "r2", path="src/shop/pricing.py")),
        turn(call("read_file", "r3", path="src/shop/coupons.py")),
        turn(call("write_file", "w1", path="artifacts/architecture.md", content=REPORT)),
        turn(call("final_answer", "f1", answer="Report written to artifacts/architecture.md")),
    ])
    out = h.start(workspace=ws, task=UNDERSTAND_TASK, kind="code_understanding")

    assert out.status is RunStatus.COMPLETED
    assert (ws / "artifacts/architecture.md").read_text() == REPORT
    assert {p: p.read_bytes() for p in (ws / "src").rglob("*.py")} == before  # 源码只读
    assert h.status(out.run_id).approvals == {}  # 写报告不需要审批


def test_rejected_approval_is_reported_to_the_model_and_nothing_is_written(tmp_path):
    ws = copy_demo(tmp_path / "ws")
    original = (ws / "src/shop/pricing.py").read_text()
    spy = HandlerSpy()
    state_dir = tmp_path / "state"
    h1, _ = make_harness(state_dir, [turn(call("write_file", "w1", path="src/shop/pricing.py", content="broken = True\n"))],
                         spy=spy)
    run_id = h1.start(workspace=ws, task=TASK, kind="test_fix").run_id
    h1.reject(run_id, "apr_0001", actor="alice", reason="this deletes the whole module")

    h2, model2 = make_harness(state_dir, [{"interrupt": True}], spy=spy)
    out = h2.resume(run_id)
    assert out.status is RunStatus.PAUSED
    assert spy.calls["write_file"] == 0 and (ws / "src/shop/pricing.py").read_text() == original
    assert "REJECTED by alice: this deletes the whole module" in model2.last_prompt()
    assert h2.status(run_id).approvals["apr_0001"].status is ApprovalStatus.REJECTED


def test_final_answer_must_be_the_only_call_in_its_step(tmp_path):
    ws = copy_demo(tmp_path / "ws")
    h, model = make_harness(tmp_path / "state", [
        turn(call("list_files", "l1"), call("final_answer", "f1", answer="done")),
        turn(call("list_files", "l2", path="src")),
    ], max_steps=2)
    out = h.start(workspace=ws, task=TASK, kind="test_fix")
    assert out.status is RunStatus.STOPPED  # 没有被当成完成
    assert observation(model, "f1", request=1)["error"]["code"] == "FINAL_ANSWER_NOT_ALONE"


def test_model_failure_marks_run_failed_and_it_can_be_resumed(tmp_path):
    ws = copy_demo(tmp_path / "ws")
    state_dir = tmp_path / "state"
    h1, _ = make_harness(state_dir, [turn(call("list_files", "l1"))])  # 第二次请求时脚本耗尽 → 模型报错
    out1 = h1.start(workspace=ws, task=TASK, kind="test_fix")
    assert out1.status is RunStatus.FAILED and out1.stop_reason is StopReason.MODEL_ERROR

    h2, model2 = make_harness(state_dir, [turn(call("read_file", "r1", path="README.md")), {"interrupt": True}])
    out2 = h2.resume(out1.run_id)
    assert out2.status is RunStatus.PAUSED and model2.calls_made == 2
    assert h2.status(out1.run_id).budget.used_steps == 4  # 1 + 失败的 1 步 + 恢复后的 2 步


def test_keyboard_interrupt_during_a_test_run_is_safe_to_redo(tmp_path):
    ws = copy_demo(tmp_path / "ws")
    state_dir = tmp_path / "state"
    real_run = HandlerSpy().registry.get("run_test").handler

    def interrupted_run(ctx, args):
        real_run(ctx, args)
        raise KeyboardInterrupt  # 测试跑到一半用户按了 Ctrl-C

    spy = HandlerSpy(overrides={"run_test": interrupted_run})
    h1, _ = make_harness(state_dir, [turn(call("run_test", "t1", command="pytest -q"))], spy=spy)
    out1 = h1.start(workspace=ws, task=TASK, kind="test_fix")
    assert out1.status is RunStatus.PAUSED and out1.stop_reason is StopReason.INTERRUPTED

    h2, model2 = make_harness(state_dir, [{"interrupt": True}])
    out2 = h2.resume(out1.run_id)
    # 读 / 测试类动作中断后可以安全重做：标记失败，不需要人工核对，也不阻塞恢复
    assert out2.status is RunStatus.PAUSED and model2.calls_made == 1
    action = [a for a in h2.status(out1.run_id).actions if a.tool_call_id == "t1"][0]
    assert action.status is ActionStatus.FAILED and action.error["code"] == "INTERRUPTED"
    assert events(h2, out1.run_id, "recovery.interrupted_action")
