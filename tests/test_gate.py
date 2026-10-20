"""G1-G4：参数校验、重复动作、未审批写入、审批后的重复执行；以及 Gate 是唯一入口。

共同断言：被拒绝的调用不进入 handler，返回结构化错误，并带回原 tool_call_id。
"""

from __future__ import annotations

import pytest

from coding_tools import build_registry
from fake_model import call, turn
from gate import GateBypassError
from state_store import ActionStatus, ApprovalStatus, RunStatus, StopReason
from support import TASK, HandlerSpy, copy_demo, events, fixed_pricing, gate_env, make_harness, observation


# ---------------- G1 参数缺字段或类型错误 ----------------


@pytest.mark.parametrize(
    "arguments, field",
    [
        ({}, "path"),  # 缺必填字段
        ({"path": 123}, "path"),  # 类型错误
        ({"path": "README.md", "start_line": "1"}, "start_line"),  # 字符串冒充整数，严格模式不转换
        ({"path": "README.md", "start_line": 1.0}, "start_line"),  # 浮点冒充整数
        ({"path": None}, "path"),  # 必填字段传 null
        ({"path": "README.md", "mode": "rb"}, "mode"),  # 多余字段
    ],
)
def test_g1_invalid_arguments_return_error_and_handler_never_runs(tmp_path, arguments, field):
    env = gate_env(tmp_path)
    result = env.submit_raw("read_file", "call_g1", arguments)

    assert result.ok is False and result.decision == "deny"
    assert result.tool_call_id == "call_g1"  # 原 tool_call_id 带回
    assert result.error.code == "INVALID_ARGUMENTS"
    assert result.error.rule == "gate.schema"
    assert field in {e["field"] for e in result.error.details["errors"]}
    assert env.spy.calls["read_file"] == 0  # handler 调用次数为 0


def test_g1_unparseable_json_arguments_are_rejected(tmp_path):
    env = gate_env(tmp_path)
    result = env.submit_raw("write_file", "call_raw", '{"path": "a.txt", "content": ')  # 模型给了半截 JSON
    assert result.error.code == "INVALID_ARGUMENTS"
    assert env.spy.calls["write_file"] == 0


def test_g1_structured_error_reaches_the_model_with_tool_call_id(tmp_path):
    ws = copy_demo(tmp_path / "ws")
    spy = HandlerSpy()
    h, model = make_harness(
        tmp_path / "state",
        [turn(call("read_file", "call_bad", path=7)), turn(call("list_files", "call_next"))],
        spy=spy,
        max_steps=2,
    )
    h.start(workspace=ws, task=TASK, kind="test_fix")

    seen = observation(model, "call_bad", request=1)  # 第二次请求时模型看到的观察结果
    assert seen == {
        "tool_call_id": "call_bad",
        "tool": "read_file",
        "ok": False,
        "decision": "deny",
        "error": seen["error"],
    }
    assert seen["error"]["code"] == "INVALID_ARGUMENTS"
    assert spy.calls["read_file"] == 0


# ---------------- G2 连续三次相同工具和参数 ----------------


def test_g2_third_identical_call_is_blocked_before_execution(tmp_path):
    env = gate_env(tmp_path)
    results = [env.submit("read_file", f"call_{i}", path="src/shop/cart.py") for i in range(1, 4)]

    assert [r.ok for r in results] == [True, True, False]
    assert results[2].error.code == "REPEATED_CALL"
    assert results[2].tool_call_id == "call_3"
    assert env.spy.calls["read_file"] == 2  # handler 只执行两次


def test_g2_a_different_call_resets_the_streak(tmp_path):
    env = gate_env(tmp_path)
    sequence = ["src/shop/cart.py", "src/shop/cart.py", "README.md", "src/shop/cart.py", "src/shop/cart.py"]
    results = [env.submit("read_file", f"c{i}", path=p) for i, p in enumerate(sequence)]
    assert all(r.ok for r in results)
    assert env.spy.calls["read_file"] == 5


def test_g2_repetition_is_counted_across_steps_in_the_agent_loop(tmp_path):
    ws = copy_demo(tmp_path / "ws")
    spy = HandlerSpy()
    same = {"path": "src/shop/pricing.py"}
    h, model = make_harness(
        tmp_path / "state",
        [turn(call("read_file", "r1", **same)), turn(call("read_file", "r2", **same)),
         turn(call("read_file", "r3", **same)), turn(call("list_files", "r4"))],
        spy=spy,
        max_steps=4,
    )
    out = h.start(workspace=ws, task=TASK, kind="test_fix")

    assert spy.calls["read_file"] == 2
    assert observation(model, "r3", request=3)["error"]["code"] == "REPEATED_CALL"
    denied = events(h, out.run_id, "gate.decision", tool_call_id="r3")[0]
    assert denied.decision == "deny" and denied.rule == "gate.repeated_call"


# ---------------- G3 未审批的写操作 ----------------


def test_g3_unapproved_write_waits_for_approval_and_file_is_untouched(tmp_path):
    ws = copy_demo(tmp_path / "ws")
    target = ws / "src/shop/pricing.py"
    original = target.read_bytes()
    spy = HandlerSpy()
    h, _ = make_harness(
        tmp_path / "state",
        [turn(
            call("write_file", "w1", path="src/shop/pricing.py", content=fixed_pricing(ws)),
            call("run_test", "t1", command="pytest -q"),  # 同一步里排在写入之后的调用
        )],
        spy=spy,
    )
    out = h.start(workspace=ws, task=TASK, kind="test_fix")

    assert out.status is RunStatus.WAITING_APPROVAL
    assert out.stop_reason is StopReason.WAITING_APPROVAL
    assert target.read_bytes() == original  # 目标文件不变
    assert spy.calls["write_file"] == 0
    assert spy.calls["run_test"] == 0  # run 已暂停，后续调用也不执行

    state = h.status(out.run_id)
    approval = state.approvals["apr_0001"]
    assert approval.status is ApprovalStatus.PENDING
    assert approval.tool_call_id == "w1" and approval.target == "src/shop/pricing.py"
    assert "+from decimal import ROUND_HALF_UP" in approval.preview  # 审批人能看到 diff
    assert out.pending_approvals[0]["approval_id"] == "apr_0001"
    follow_up = [a for a in state.actions if a.tool_call_id == "t1"][0]
    assert follow_up.status is ActionStatus.DENIED and follow_up.rule == "gate.run_not_active"


def test_g3_path_prefix_trick_cannot_skip_approval(tmp_path):
    # artifacts/ 下免审批，但判断用的是真实落点：artifacts/../src/... 仍然要审批
    env = gate_env(tmp_path)
    result = env.submit("write_file", "w_trick", path="artifacts/../src/shop/cart.py", content="x = 1\n")
    assert result.decision == "pending" and result.error.code == "APPROVAL_REQUIRED"
    assert env.spy.calls["write_file"] == 0


# ---------------- G4 审批后重复执行同一调用 ----------------


def test_g4_approved_write_runs_once_and_repeats_are_rejected(tmp_path):
    ws = copy_demo(tmp_path / "ws")
    fixed = fixed_pricing(ws)
    spy = HandlerSpy()
    state_dir = tmp_path / "state"

    h1, _ = make_harness(state_dir, [turn(call("write_file", "w1", path="src/shop/pricing.py", content=fixed))], spy=spy)
    run_id = h1.start(workspace=ws, task=TASK, kind="test_fix").run_id
    h1.approve(run_id, "apr_0001", actor="alice")

    # 恢复：Harness 代为执行已审批的写入；模型又发了一次一模一样的写入，然后用户按了 Ctrl-C
    h2, _ = make_harness(state_dir, [
        turn(call("write_file", "w2", path="src/shop/pricing.py", content=fixed)),
        {"interrupt": True},
    ], spy=spy)
    out2 = h2.resume(run_id)
    assert out2.status is RunStatus.PAUSED
    assert spy.calls["write_file"] == 1  # 第一次只执行一次
    assert (ws / "src/shop/pricing.py").read_text(encoding="utf-8") == fixed

    state = h2.status(run_id)
    dup = [a for a in state.actions if a.tool_call_id == "w2"][0]
    assert dup.status is ActionStatus.DENIED and dup.rule == "gate.duplicate_write"  # 第二次拒绝
    assert state.approvals["apr_0001"].status is ApprovalStatus.CONSUMED

    # 再次恢复不会重放已消费的审批；直接要求再执行这张审批也会被拒绝
    h3, _ = make_harness(state_dir, [{"interrupt": True}], spy=spy)
    h3.resume(run_id)
    state = h3.status(run_id)
    replay = h3._ctx.gate.execute_approved(state, "apr_0001")
    assert replay.ok is False and replay.error.code == "APPROVAL_CONSUMED"
    assert replay.tool_call_id == "w1"
    assert spy.calls["write_file"] == 1  # 不重复写入
    writes = [r for r in events(h3, run_id, "exec.result") if r.tool == "write_file" and r.decision == "ok"]
    assert len(writes) == 1


# ---------------- Gate 是唯一入口 ----------------


def test_schema_carriers_given_to_smolagents_cannot_execute(tmp_path):
    tool = build_registry().smolagents_tools(["write_file"])[0]
    with pytest.raises(GateBypassError):
        tool(path="x.txt", content="y")


def test_agent_cannot_be_run_or_execute_tools_outside_the_harness(tmp_path):
    env = gate_env(tmp_path)
    agent = env.harness._assemble(env.state).agent
    with pytest.raises(RuntimeError, match="only run through GuardedHarness"):
        agent.run("do something")
    with pytest.raises(GateBypassError):
        agent.execute_tool_call("write_file", {"path": "x.txt", "content": "y"})


def test_unknown_tool_and_permission_are_checked_by_the_gate(tmp_path):
    env = gate_env(tmp_path, kind="code_understanding")
    unknown = env.submit("delete_repo", "u1")
    assert unknown.error.code == "UNKNOWN_TOOL" and unknown.tool_call_id == "u1"
    not_allowed = env.submit("replace_in_file", "p1", path="src/shop/cart.py", old="a", new="b")
    assert not_allowed.error.code == "PERMISSION_DENIED"
    assert env.spy.calls["replace_in_file"] == 0
