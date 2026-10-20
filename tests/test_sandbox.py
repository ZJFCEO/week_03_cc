"""S1-S4：路径逃逸、软链接 / 敏感目录、命令白名单、超时与取消；外加环境变量和输出边界。

“真实拦截”的含义：
    越界路径    → handler 不执行，工作区外的文件没有被创建或读取
    危险命令    → 命令根本没有启动（用 touch 哨兵文件验证）
    超时 / 取消 → 进程组里的子进程、孙进程都确实死了（os.kill(pid, 0) 验证），不只是不再等待
"""

from __future__ import annotations

import os
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from fake_model import ScriptedModel, call, turn
from harness import GuardedHarness, HarnessConfig
from sandbox import Sandbox, SandboxPolicy, SandboxViolation
from state_store import RunStatus, StopReason
from support import copy_demo, events, gate_env


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    return Sandbox(copy_demo(tmp_path / "ws"))


def _gone(pid: int, timeout: float = 5.0) -> bool:
    """等进程真正消失（被回收）；超时仍存在返回 False。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


# ---------------- S1 artifacts/../../.env 路径逃逸 ----------------


@pytest.mark.parametrize("raw", ["artifacts/../../.env", "../outside.txt", "src/../../../etc/hosts", "/etc/passwd"])
def test_s1_lexical_escape_is_rejected(sandbox, raw):
    with pytest.raises(SandboxViolation) as exc:
        sandbox.resolve(raw, access="read")
    assert exc.value.code == "PATH_ESCAPE"
    assert exc.value.rule == "sandbox.path_escape"


@pytest.mark.parametrize("tool", ["read_file", "write_file"])
def test_s1_escape_never_reaches_the_handler(tmp_path, tool):
    env = gate_env(tmp_path)
    outside = tmp_path / ".env"  # 工作区上一级，正好是 artifacts/../../.env 的落点
    outside.write_text("REAL_SECRET=1\n")
    args = {"path": "artifacts/../../.env"} | ({"content": "pwned"} if tool == "write_file" else {})

    result = env.submit(tool, "s1", **args)

    assert result.decision == "deny" and result.error.code == "PATH_ESCAPE"
    assert result.tool_call_id == "s1"
    assert env.spy.calls[tool] == 0
    assert outside.read_text() == "REAL_SECRET=1\n"
    assert "/" not in result.error.message.replace("artifacts/../../.env", "")  # 不把宿主机绝对路径透给模型


def test_s1_dotdot_inside_workspace_still_hits_sensitive_rule(sandbox):
    with pytest.raises(SandboxViolation) as exc:
        sandbox.resolve("artifacts/../.env", access="read")
    assert exc.value.code == "SENSITIVE_PATH"


# ---------------- S2 软链接逃逸与敏感目录 ----------------


def test_s2_symlink_escape_is_detected_by_real_target(tmp_path):
    ws = copy_demo(tmp_path / "ws")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("top secret")
    (ws / "src" / "linked_dir").symlink_to(outside, target_is_directory=True)
    (ws / "notes.txt").symlink_to(outside / "secret.txt")
    sb = Sandbox(ws)

    for raw in ("src/linked_dir/secret.txt", "notes.txt"):
        with pytest.raises(SandboxViolation) as exc:
            sb.resolve(raw, access="read")
        assert exc.value.code == "SYMLINK_ESCAPE"
    with pytest.raises(SandboxViolation):  # 往软链接目录里新建文件同样拦截
        sb.resolve("src/linked_dir/new.py", access="write")


def test_s2_symlink_pointing_at_sensitive_file_inside_workspace(tmp_path):
    ws = copy_demo(tmp_path / "ws")
    (ws / "config.txt").symlink_to(ws / ".env")  # 名字无害，真实落点是 .env
    with pytest.raises(SandboxViolation) as exc:
        Sandbox(ws).resolve("config.txt", access="read")
    assert exc.value.code == "SENSITIVE_PATH"


@pytest.mark.parametrize("raw", [".env", ".ENV", "src/.env.local", ".git/config", "secrets/key.txt", "deploy/id_rsa",
                                 ".harness/audit.jsonl", "certs/server.pem"])
def test_s2_sensitive_locations_are_blocked(sandbox, raw):
    with pytest.raises(SandboxViolation) as exc:
        sandbox.resolve(raw, access="read")
    assert exc.value.code == "SENSITIVE_PATH"


def test_s2_symlink_denial_is_audited(tmp_path):
    env = gate_env(tmp_path)
    outside = tmp_path / "secret.txt"
    outside.write_text("top secret")
    (env.ws / "notes.txt").symlink_to(outside)

    result = env.submit("read_file", "s2", path="notes.txt")

    assert result.error.code == "SYMLINK_ESCAPE" and env.spy.calls["read_file"] == 0
    check = [r for r in env.audit(tool_call_id="s2") if r.event == "sandbox.check"][0]
    assert check.decision == "deny" and check.actor == "sandbox" and check.rule == "sandbox.symlink_escape"
    assert check.details["real_target"] == str(outside.resolve())  # 审计里留下真实落点，模型看不到


def test_s2_listing_and_search_hide_sensitive_files(tmp_path):
    env = gate_env(tmp_path)
    listing = env.submit("list_files", "l1")
    assert ".env" not in listing.output["entries"]
    found = env.submit("search_code", "g1", pattern="SHOP_PAYMENT_TOKEN")
    assert found.output["matches"] == []


# ---------------- S3 run_test 配置外命令 ----------------


@pytest.mark.parametrize(
    "command, rule",
    [
        ("rm -rf /", "sandbox.command_not_allowed"),
        ("touch pwned", "sandbox.command_not_allowed"),
        ("bash -c 'pytest -q'", "sandbox.command_not_allowed"),
        ("python -c 'import os'", "sandbox.command_not_allowed"),
        ("pytest -q; touch pwned", "sandbox.shell_metachar"),
        ("pytest -q && touch pwned", "sandbox.shell_metachar"),
        ("pytest $(touch pwned)", "sandbox.shell_metachar"),
        ("pytest -q > out.txt", "sandbox.shell_metachar"),
        ("pytest --rootdir=/", "sandbox.flag_not_allowed"),
        ("pytest -p evil_plugin", "sandbox.flag_not_allowed"),
        ("pytest -c /tmp/evil.ini", "sandbox.flag_not_allowed"),
        ("pytest ../outside", "sandbox.path_escape"),
        ("pytest .env", "sandbox.sensitive_path"),
    ],
)
def test_s3_commands_outside_the_allowlist_are_rejected(sandbox, command, rule):
    with pytest.raises(SandboxViolation) as exc:
        sandbox.check_command(command)
    assert exc.value.rule == rule


@pytest.mark.parametrize("command", ["touch pwned", "pytest -q; touch pwned"])
def test_s3_rejected_command_is_never_started(tmp_path, command):
    env = gate_env(tmp_path)
    result = env.submit("run_test", "s3", command=command)
    assert result.decision == "deny" and result.error.code == "COMMAND_NOT_ALLOWED"
    assert env.spy.calls["run_test"] == 0
    assert not (env.ws / "pwned").exists()


def test_s3_allowed_commands_are_normalized(sandbox):
    full = sandbox.check_command("pytest -q")
    assert full.argv[:5] == (sys.executable, "-m", "pytest", "-p", "no:cacheprovider")
    assert full.full_suite is True and full.display == "pytest -q"
    subset = sandbox.check_command("python -m pytest -q tests/test_cart.py::test_subtotal_sums_lines")
    assert subset.full_suite is False and subset.targets == ("tests/test_cart.py::test_subtotal_sums_lines",)
    assert sandbox.check_command('pytest -q -k "coupon and not unknown"').full_suite is False


# ---------------- S4 超时与用户取消：进程组真实终止 ----------------

SPAWNER = textwrap.dedent(
    """
    import os, subprocess, sys, time
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    with open(sys.argv[1], "w") as fh:
        fh.write(f"{os.getpid()} {child.pid}")
    time.sleep(60)
    """
)


def _spawner(tmp_path: Path) -> tuple[Sandbox, list[str], Path]:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "spawner.py").write_text(SPAWNER)
    pidfile = tmp_path / "pids.txt"
    return Sandbox(ws, SandboxPolicy(kill_grace=0.5)), [sys.executable, "spawner.py", str(pidfile)], pidfile


def _wait_for(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not (path.exists() and path.read_text().strip()) and time.monotonic() < deadline:
        time.sleep(0.02)


def test_s4_timeout_terminates_the_whole_process_group(tmp_path):
    sb, argv, pidfile = _spawner(tmp_path)
    started = time.monotonic()
    result = sb.run(argv, timeout=1.0)

    assert result.timed_out and result.exit_code is None
    assert time.monotonic() - started < 5  # 没有等满 60 秒
    parent, grandchild = map(int, pidfile.read_text().split())
    assert _gone(parent) and _gone(grandchild)  # 孙进程也被终止


def test_s4_user_cancel_terminates_the_whole_process_group(tmp_path):
    sb, argv, pidfile = _spawner(tmp_path)
    cancel = threading.Event()
    threading.Thread(target=lambda: (_wait_for(pidfile), cancel.set()), daemon=True).start()

    result = sb.run(argv, timeout=30, cancel=cancel)

    assert result.cancelled and not result.timed_out
    parent, grandchild = map(int, pidfile.read_text().split())
    assert _gone(parent) and _gone(grandchild)


HANGING_TEST = textwrap.dedent(
    """
    import os, subprocess, sys, time
    from pathlib import Path

    def test_hangs():
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        Path("pids.txt").write_text(f"{os.getpid()} {child.pid}")
        time.sleep(60)
    """
)


def _hanging_repo(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "tests").mkdir(parents=True)
    (ws / "tests" / "test_hang.py").write_text(HANGING_TEST)
    (ws / "src").mkdir()
    (ws / "src" / "app.py").write_text("VALUE = 1\n")
    return ws


def test_s4_run_test_timeout_through_the_gate(tmp_path):
    ws = _hanging_repo(tmp_path)
    h = GuardedHarness(
        state_dir=tmp_path / "state",
        model=ScriptedModel([turn(call("run_test", "slow", command="pytest -q", timeout_seconds=1))]),
        config=HarnessConfig(max_steps=1),
    )
    out = h.start(workspace=ws, task="run the tests", kind="test_fix")

    result = [r for r in events(h, out.run_id, "exec.result") if r.tool_call_id == "slow"][0]
    assert result.decision == "error" and result.rule == "sandbox.timeout" and result.actor == "sandbox"
    parent, grandchild = map(int, (ws / "pids.txt").read_text().split())
    assert _gone(parent) and _gone(grandchild)


def test_s4_user_cancel_stops_the_run_and_kills_the_test_process(tmp_path):
    ws = _hanging_repo(tmp_path)
    h = GuardedHarness(
        state_dir=tmp_path / "state",
        model=ScriptedModel([turn(call("run_test", "slow", command="pytest -q")), turn(call("list_files", "next"))]),
        config=HarnessConfig(max_steps=5),
    )
    pidfile = ws / "pids.txt"
    threading.Thread(target=lambda: (_wait_for(pidfile), h.cancel()), daemon=True).start()
    started = time.monotonic()
    out = h.start(workspace=ws, task="run the tests", kind="test_fix")

    assert out.status is RunStatus.STOPPED and out.stop_reason is StopReason.CANCELLED
    assert time.monotonic() - started < 15
    assert h.model.calls_made == 1  # 取消后不再请求模型
    parent, grandchild = map(int, pidfile.read_text().split())
    assert _gone(parent) and _gone(grandchild)


# ---------------- 环境变量、输出边界、写入范围 ----------------


def test_child_process_does_not_inherit_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-0123456789abcdef")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_0123456789")
    sb = Sandbox(copy_demo(tmp_path / "ws"))
    result = sb.run([sys.executable, "-c", "import os; print(sorted(os.environ))"])
    assert "DEEPSEEK_API_KEY" not in result.output and "GITHUB_TOKEN" not in result.output
    assert "PYTHONDONTWRITEBYTECODE" in result.output


def test_output_is_bounded_and_secret_values_are_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOP_API_KEY", "sk-live-should-never-appear")
    ws = copy_demo(tmp_path / "ws")
    (ws / "leak.txt").write_text("sk-live-should-never-appear")
    sb = Sandbox(ws, SandboxPolicy(max_output_chars=2_000))
    code = "print(open('leak.txt').read()); print('x' * 50_000); print('TAIL-MARKER')"
    result = sb.run([sys.executable, "-c", code])
    assert "sk-live-should-never-appear" not in result.output and "[REDACTED]" in result.output
    assert result.truncated and len(result.output) < 2_200
    assert "TAIL-MARKER" in result.output  # 头尾都保留


def test_write_scope_depends_on_task_kind(tmp_path):
    fix = gate_env(tmp_path / "fix")
    assert fix.submit("write_file", "w1", path="tests/test_cart.py", content="").error.code == "READONLY_PATH"
    assert fix.submit("write_file", "w2", path="pytest.ini", content="").error.code == "READONLY_PATH"

    understand = gate_env(tmp_path / "read", kind="code_understanding")
    assert understand.submit("write_file", "w3", path="src/shop/cart.py", content="").error.code == "WRITE_SCOPE"
    ok = understand.submit("write_file", "w4", path="artifacts/notes.md", content="# notes\n")
    assert ok.ok and understand.spy.calls["write_file"] == 1  # artifacts/ 免审批
