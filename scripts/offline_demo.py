"""离线演示：用脚本模型跑一遍完整的跨进程流程，生成提交用的三份记录。

    .venv/bin/python scripts/offline_demo.py

每一步都是独立的 `harness_cli.py` 子进程（内存不共享），依次是：
    run（进程 1）     → 越界读 .env 被拦、跑测试失败、读源码、提交修改 → WAITING_APPROVAL
    approve（进程 2） → 人工批准
    resume（进程 3）  → Harness 执行已批准的写入、跑测试通过，等模型时 Ctrl-C → PAUSED
    resume（进程 4）  → 模型重复提交同一写入被拒、提交答案 → COMPLETED

输出：
    records/checkpoint_recovery.md      每个进程的命令、退出码、输出 + 检查点列表（Checkpoint 恢复记录）
    records/run_record_offline.md       运行记录（diff / 证据 / 互证 / 审计决策链）
    records/audit_offline.jsonl         这个 run 的原始审计记录
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from audit import AuditLog  # noqa: E402
from fake_model import call, turn  # noqa: E402
from state_store import StateStore  # noqa: E402

STATE = ROOT / ".harness" / "offline"
WS = ROOT / "workspaces" / "offline_demo"
SCRIPTS = ROOT / ".harness" / "offline_scripts"
RECORDS = ROOT / "records"
TASK = "tests/ 下有失败的测试，修复 src/ 下的源码让 pytest -q 全部通过"


def cli(*args: str) -> tuple[int, str]:
    proc = subprocess.run([sys.executable, "harness_cli.py", "--state-dir", str(STATE), *args],
                          cwd=ROOT, capture_output=True, text=True, timeout=300)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def script(name: str, *turns: dict) -> str:
    path = SCRIPTS / f"{name}.json"
    path.write_text(json.dumps({"model_id": "scripted-fake", "turns": list(turns)}, ensure_ascii=False, indent=2))
    return f"script:{path.relative_to(ROOT)}"


def main() -> None:
    for path in (STATE, WS, SCRIPTS):
        shutil.rmtree(path, ignore_errors=True)
    SCRIPTS.mkdir(parents=True)
    shutil.copytree(ROOT / "demo_repos" / "shop", WS, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    pricing = (WS / "src/shop/pricing.py").read_text(encoding="utf-8")
    fixed = pricing.replace("ROUND_HALF_EVEN", "ROUND_HALF_UP").replace("quantity > threshold", "quantity >= threshold")

    steps: list[tuple[str, list[str], int, str]] = []

    def record(title: str, args: list[str]) -> int:
        code, out = cli(*args)
        steps.append((title, args, code, out))
        print(f"[{code}] {title}")
        return code

    record("进程 1：run（脚本模型）", [
        "run", "--workspace", str(WS.relative_to(ROOT)), "--kind", "test_fix", "--task", TASK,
        "--model", script("p1",
                          turn(call("update_plan", "call_01", items=[
                              {"step": "复现失败", "status": "in_progress"}, {"step": "定位并修复 pricing"},
                              {"step": "完整测试集回归"}])),
                          turn(call("read_file", "call_02", path="artifacts/../../.env")),
                          turn(call("run_test", "call_03", command="pytest -q; cat .env")),
                          turn(call("run_test", "call_04", command="pytest -q")),
                          turn(call("read_file", "call_05", path="src/shop/pricing.py")),
                          turn(call("write_file", "call_06", path="src/shop/pricing.py", content=fixed))),
    ])
    run_id = StateStore(STATE).list_runs()[0]
    record("进程 2：approve", ["approve", run_id, "apr_0001", "--actor", "reviewer-alice"])
    record("进程 3：resume（执行已批准写入，随后 Ctrl-C）", [
        "resume", run_id, "--model", script("p3",
                                            turn(call("run_test", "call_07", command="pytest -q")),
                                            {"interrupt": True}),
    ])
    record("进程 4：resume（继续到完成）", [
        "resume", run_id, "--model", script("p4",
                                            turn(call("write_file", "call_08", path="src/shop/pricing.py", content=fixed)),
                                            turn(call("update_plan", "call_09", items=[
                                                {"step": "复现失败", "status": "done"},
                                                {"step": "定位并修复 pricing", "status": "done"},
                                                {"step": "完整测试集回归", "status": "done"}])),
                                            turn(call("final_answer", "call_10", answer=(
                                                "修复了 src/shop/pricing.py 的两处问题：批量折扣档位改为“达到即享受”（>=），"
                                                "金额舍入改为 ROUND_HALF_UP。在最终代码上运行完整测试集：12 passed。")))),
    ])

    # ---- Checkpoint 恢复记录 ----
    lines = ["# Checkpoint 恢复记录（离线脚本模型）", "",
             "每一步都是独立的 `harness_cli.py` 子进程，进程之间只通过 `.harness/offline/` 下的状态、检查点、审计文件交接。",
             "由 `scripts/offline_demo.py` 生成，可重复执行。", "",
             f"run_id：`{run_id}`", ""]
    for title, args, code, out in steps:
        shown = " ".join(a if " " not in a else repr(a) for a in args)
        lines += [f"## {title}", "", "```bash", f"python harness_cli.py {shown}", "```", "",
                  f"退出码：`{code}`", "", "```", out, "```", ""]
    state = StateStore(STATE).load(run_id)
    lines += ["## 恢复后的连续性", "",
              "| 步 | 会话（进程） | tool_call_ids |", "|---|---|---|"]
    lines += [f"| {s.number} | {s.session} | {', '.join(s.tool_call_ids) or '（被 Ctrl-C 打断）'} |" for s in state.steps]
    log = AuditLog(STATE / "audit.jsonl")
    resumed = [r for r in log.records(run_id=run_id) if r.event == "run.resumed"]
    lines += ["", "每次恢复时 Harness 的判断（`run.resumed` 审计里的恢复计划）：", ""]
    for r in resumed:
        plan = r.details["plan"]
        lines.append(f"- 会话 {r.details['session']}：从检查点 #{r.details['checkpoint_seq']}（第 {r.details['checkpoint_step']} 步）恢复；"
                     f"待执行审批 {plan['approvals_to_execute']}；检查点后结束的动作 {plan['after_checkpoint']}；"
                     f"漂移 {list(plan['drift'])}；结果未知 {plan['unresolved_unknown']}；"
                     f"恢复时计数 steps={r.details['counters']['used_steps']} tool_calls={r.details['counters']['used_tool_calls']}")
    writes = [r for r in log.records(run_id=run_id, event_prefix="exec.result") if r.tool == "write_file" and r.decision == "ok"]
    dup = [r for r in log.decision_chain(run_id, "call_08") if r.event == "gate.decision"][0]
    lines += ["", f"- 整个运行中 `write_file` 成功执行 **{len(writes)}** 次（由 Harness 在会话 2 代为执行）；",
              f"- 会话 3 里模型重复提交的 `call_08` 被拒绝：`{dup.rule}` —— {dup.details['message']}",
              f"- 最终状态：{state.status}，步数 {state.budget.used_steps}/{state.budget.max_steps}，"
              f"工具调用 {state.budget.used_tool_calls}/{state.budget.max_tool_calls}（跨三个进程累计，没有清零）", ""]
    ckpts = sorted((STATE / "runs" / run_id / "checkpoints").glob("*.json"))
    lines += ["## 检查点文件", "", "| 文件 | 步 | 原因 | 状态 | 计划项 | 证据 | 待审批 | 对话步数 |", "|---|---|---|---|---|---|---|---|"]
    for path in ckpts:
        d = json.loads(path.read_text())
        pending = [k for k, v in d["state"]["approvals"].items() if v["status"] == "PENDING"]
        lines.append(f"| {path.name} | {d['step']} | {d['reason']} | {d['state']['status']} | {len(d['state']['plan'])} | "
                     f"{len(d['state']['evidence'])} | {', '.join(pending)} | "
                     f"{sum(1 for m in d['memory'] if m['type'] == 'action')} |")
    waiting = next(json.loads(p.read_text()) for p in ckpts if json.loads(p.read_text())["state"]["status"] == "WAITING_APPROVAL")
    excerpt = {
        "run_id": waiting["run_id"], "seq": waiting["seq"], "step": waiting["step"], "reason": waiting["reason"],
        "state.status": waiting["state"]["status"], "state.plan": waiting["state"]["plan"],
        "state.budget": waiting["state"]["budget"],
        "state.evidence": [{k: e[k] for k in ("evidence_id", "kind", "path", "command", "passed", "valid")}
                           for e in waiting["state"]["evidence"]],
        "pending_approvals": [{k: a[k] for k in ("approval_id", "tool_call_id", "tool", "target", "target_hash", "status")}
                              for a in waiting["state"]["approvals"].values()],
        "memory": f"{len(waiting['memory'])} entries", "workspace": f"{len(waiting['workspace'])} files",
        "checksum": waiting["checksum"],
    }
    lines += ["", "进入等待审批时的检查点（节选）：", "", "```json", json.dumps(excerpt, ensure_ascii=False, indent=2), "```", ""]
    RECORDS.mkdir(exist_ok=True)
    (RECORDS / "checkpoint_recovery.md").write_text("\n".join(lines), encoding="utf-8")

    # ---- 运行记录 + 审计记录 ----
    code, _ = cli("report", run_id, "--out", str(RECORDS / "run_record_offline.md"))
    assert code == 0
    with (RECORDS / "audit_offline.jsonl").open("w", encoding="utf-8") as fh:
        for rec in log.records(run_id=run_id):
            fh.write(json.dumps(rec.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")
    print(f"hash chain OK: {log.verify()} records; run {run_id} → {state.status}")


if __name__ == "__main__":
    main()
