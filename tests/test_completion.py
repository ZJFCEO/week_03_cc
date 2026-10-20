"""H1-H3：完成验收。模型说“做完了”不算，交付物和新鲜的证据对得上才算；预算耗尽要明确停止。"""

from __future__ import annotations

from fake_model import call, turn
from state_store import RunStatus, StopReason
from support import AUTO_APPROVE, TASK, copy_demo, events, fixed_pricing, make_harness, observation


def _failed(model, call_id: str, request: int) -> list[str]:
    seen = observation(model, call_id, request=request)
    assert seen["error"]["code"] == "COMPLETION_REJECTED"
    return seen["error"]["details"]["failed_checks"]


# ---------------- H1 模型说完成但无交付物 ----------------


def test_h1_test_fix_claimed_done_without_deliverable_is_rejected(tmp_path):
    ws = copy_demo(tmp_path / "ws")
    h, model = make_harness(tmp_path / "state", [
        turn(call("final_answer", "f1", answer="All tests pass now.")),
        turn(call("list_files", "l1")),
    ], max_steps=2)
    out = h.start(workspace=ws, task=TASK, kind="test_fix")

    assert set(_failed(model, "f1", request=1)) == {"source_changed", "fresh_passing_full_suite"}
    assert out.status is not RunStatus.COMPLETED and out.final_answer is None
    assert h.status(out.run_id).final_answer is None
    verdict = events(h, out.run_id, "completion.check", tool_call_id="f1")[0]
    assert verdict.decision == "reject" and verdict.actor == "completion"


def test_h1_code_understanding_without_report_is_rejected(tmp_path):
    ws = copy_demo(tmp_path / "ws")
    h, model = make_harness(tmp_path / "state", [
        turn(call("read_file", "r1", path="src/shop/cart.py")),
        turn(call("final_answer", "f1", answer="cart.py sums lines and applies coupons.")),
        turn(call("list_files", "l1")),
    ], max_steps=3)
    out = h.start(workspace=ws, task="说明 cart 的职责", kind="code_understanding")

    failed = _failed(model, "f1", request=2)
    assert {"report_written", "grounded_in_reads", "report_cites_read_files"} <= set(failed)
    assert out.status is not RunStatus.COMPLETED


def test_h1_fix_without_test_evidence_is_rejected(tmp_path):
    ws = copy_demo(tmp_path / "ws")
    h, model = make_harness(tmp_path / "state", [
        turn(call("write_file", "w1", path="src/shop/pricing.py", content=fixed_pricing(ws))),
        turn(call("final_answer", "f1", answer="Fixed, trust me.")),
        turn(call("list_files", "l1")),
    ], gate_policy=AUTO_APPROVE, max_steps=3)
    h.start(workspace=ws, task=TASK, kind="test_fix")
    assert _failed(model, "f1", request=2) == ["fresh_passing_full_suite"]


# ---------------- H2 测试通过后又修改源码 ----------------


def test_h2_source_change_after_passing_tests_requires_a_rerun(tmp_path):
    ws = copy_demo(tmp_path / "ws")
    h, model = make_harness(tmp_path / "state", [
        turn(call("write_file", "w1", path="src/shop/pricing.py", content=fixed_pricing(ws))),
        turn(call("run_test", "t1", command="pytest -q")),  # 通过
        turn(call("replace_in_file", "e1", path="src/shop/cart.py",
                  old='"""购物车：汇总明细行，再叠加优惠券。"""', new='"""购物车：汇总明细行，再叠加优惠券（含批量折扣）。"""')),
        turn(call("final_answer", "f1", answer="done")),  # 旧证据已失效，必须拒绝
        turn(call("run_test", "t2", command="pytest -q")),  # 在当前版本上重跑
        turn(call("final_answer", "f2", answer="Fixed pricing; suite rerun on the final code: 12 passed.")),
    ], gate_policy=AUTO_APPROVE)
    out = h.start(workspace=ws, task=TASK, kind="test_fix")

    assert _failed(model, "f1", request=4) == ["fresh_passing_full_suite"]
    detail = observation(model, "f1", request=4)["error"]["message"]
    assert "stale" in detail and "src/shop/cart.py modified by e1" in detail
    assert out.status is RunStatus.COMPLETED
    state = h.status(out.run_id)
    t1 = [e for e in state.evidence if e.tool_call_id == "t1"][0]
    t2 = [e for e in state.evidence if e.tool_call_id == "t2"][0]
    assert t1.passed and not t1.valid and t2.passed and t2.valid
    assert t1.code_fingerprint != t2.code_fingerprint == state.completion["code_fingerprint"]


def test_h2_passing_subset_of_tests_does_not_count(tmp_path):
    ws = copy_demo(tmp_path / "ws")
    h, model = make_harness(tmp_path / "state", [
        turn(call("write_file", "w1", path="src/shop/pricing.py", content=fixed_pricing(ws))),
        turn(call("run_test", "t1", command="pytest -q tests/test_pricing.py")),
        turn(call("final_answer", "f1", answer="pricing tests pass")),
        turn(call("list_files", "l1")),
    ], gate_policy=AUTO_APPROVE, max_steps=4)
    h.start(workspace=ws, task=TASK, kind="test_fix")
    assert _failed(model, "f1", request=3) == ["fresh_passing_full_suite"]
    assert "only ran a subset" in observation(model, "f1", request=3)["error"]["message"]


def test_h2_change_outside_the_harness_blocks_completion(tmp_path):
    ws = copy_demo(tmp_path / "ws")

    def sneaky_edit_then_finish(messages):
        # 测试通过之后、提交答案之前，有人绕过 Harness 改了源码
        (ws / "src/shop/coupons.py").write_text((ws / "src/shop/coupons.py").read_text() + "\nDEBUG = True\n")
        return turn(call("final_answer", "f1", answer="done"))

    h, model = make_harness(tmp_path / "state", [
        turn(call("write_file", "w1", path="src/shop/pricing.py", content=fixed_pricing(ws))),
        turn(call("run_test", "t1", command="pytest -q")),
        sneaky_edit_then_finish,
        turn(call("list_files", "l1")),
    ], gate_policy=AUTO_APPROVE, max_steps=4)
    h.start(workspace=ws, task=TASK, kind="test_fix")
    assert set(_failed(model, "f1", request=3)) == {"workspace_consistent", "fresh_passing_full_suite"}


# ---------------- H3 达到最大轮数或预算 ----------------


def test_h3_max_steps_stops_with_explicit_reason_and_no_fabricated_answer(tmp_path):
    ws = copy_demo(tmp_path / "ws")
    h, model = make_harness(tmp_path / "state", [
        turn(call("list_files", "l1")),
        turn(call("read_file", "r1", path="README.md")),
        turn(call("read_file", "r2", path="src/shop/cart.py")),
        turn(call("final_answer", "never", answer="should not be asked for")),
    ], max_steps=3)
    out = h.start(workspace=ws, task=TASK, kind="test_fix")

    assert out.status is RunStatus.STOPPED and out.stop_reason is StopReason.MAX_STEPS
    assert out.success is False and out.final_answer is None
    assert model.calls_made == 3  # smolagents 默认会再让模型“总结一个答案”，这里没有
    assert h.status(out.run_id).final_answer is None


def test_h3_token_budget_stops_the_run(tmp_path):
    ws = copy_demo(tmp_path / "ws")
    h, model = make_harness(tmp_path / "state", [
        turn(call("list_files", "l1"), tokens=(200, 50)),
        turn(call("read_file", "r1", path="README.md"), tokens=(200, 50)),
        turn(call("read_file", "r2", path="src/shop/cart.py"), tokens=(200, 50)),
    ], max_tokens=450)
    out = h.start(workspace=ws, task=TASK, kind="test_fix")

    assert out.status is RunStatus.STOPPED and out.stop_reason is StopReason.BUDGET_EXHAUSTED
    assert out.detail == "tokens budget exhausted"
    assert model.calls_made == 2 and h.status(out.run_id).budget.used_tokens == 500


def test_h3_tool_call_budget_stops_the_run(tmp_path):
    ws = copy_demo(tmp_path / "ws")
    h, model = make_harness(tmp_path / "state", [
        turn(call("list_files", "l1"), call("read_file", "r1", path="README.md")),
        turn(call("read_file", "r2", path="src/shop/cart.py")),
    ], max_tool_calls=2)
    out = h.start(workspace=ws, task=TASK, kind="test_fix")
    assert out.status is RunStatus.STOPPED and out.stop_reason is StopReason.BUDGET_EXHAUSTED
    assert out.detail == "tool_calls budget exhausted" and model.calls_made == 1


def test_h3_resume_after_budget_stop_is_refused(tmp_path):
    ws = copy_demo(tmp_path / "ws")
    state_dir = tmp_path / "state"
    h1, _ = make_harness(state_dir, [turn(call("list_files", "l1"))], max_steps=1)
    run_id = h1.start(workspace=ws, task=TASK, kind="test_fix").run_id
    h2, model2 = make_harness(state_dir, [turn(call("list_files", "l2"))])
    out = h2.resume(run_id)
    assert out.status is RunStatus.STOPPED and out.stop_reason is StopReason.MAX_STEPS
    assert model2.calls_made == 0  # 终态不能恢复成“继续跑”
