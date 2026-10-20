"""命令行入口：run / resume / approve / reject / verify / ack-workspace / status / audit / report / prepare。

典型流程（每条命令都是独立进程，天然演示“换进程恢复”）：

    python harness_cli.py prepare --name demo                       # 复制一份干净的演示仓库
    python harness_cli.py run --workspace workspaces/demo --kind test_fix --task "修复失败的测试"
    python harness_cli.py approve <run_id> apr_0001 --actor alice    # 人工审批
    python harness_cli.py resume <run_id>                            # 新进程恢复，Harness 代为执行已审批的写入
    python harness_cli.py report <run_id> --out records/run.md       # 运行记录：diff、证据、审计互相印证

模型：--model deepseek（读环境变量 DEEPSEEK_API_KEY / DEEPSEEK_MODEL / DEEPSEEK_BASE_URL），
或 --model script:path.json（离线脚本，测试和演示用）。
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import shutil
import sys
from pathlib import Path

from audit import AuditLog
from checkpoint import CheckpointManager
from fake_model import ScriptedModel
from harness import GuardedHarness, HarnessConfig, RunOutcome
from sandbox import Sandbox
from state_store import ActionStatus, EvidenceKind, RunState, RunStatus, StateStore
from smolagents.monitoring import LogLevel

ROOT = Path(__file__).resolve().parent

# 退出码：脚本和测试据此判断停在了哪里
EXIT_CODES = {
    RunStatus.COMPLETED: 0,
    RunStatus.WAITING_APPROVAL: 3,
    RunStatus.WAITING_VERIFICATION: 4,
    RunStatus.PAUSED: 5,
    RunStatus.STOPPED: 6,
    RunStatus.FAILED: 7,
}


def build_model(spec: str, script_log: str | None):
    """--model 参数 → smolagents Model。真实模型走 OpenAI 兼容接口。"""
    if spec.startswith("script:"):
        return ScriptedModel.from_file(spec.removeprefix("script:"), log_path=Path(script_log) if script_log else None)
    if spec == "deepseek":
        from smolagents import OpenAIModel

        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            sys.exit("DEEPSEEK_API_KEY is not set")
        return OpenAIModel(
            model_id=os.getenv("DEEPSEEK_MODEL", "deepseek-flash"),
            api_base=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            api_key=api_key,
            flatten_messages_as_text=True,  # DeepSeek 只收字符串 content，不收内容块列表
            # smolagents 默认 tool_choice="required"，DeepSeek 的思考模式不支持；
            # 用 auto 时模型偶尔只回文本，smolagents 会把它当解析错误反馈给模型，下一步再调工具
            tool_choice=os.getenv("DEEPSEEK_TOOL_CHOICE", "auto"),
        )
    sys.exit(f"unknown model spec: {spec}")


def print_outcome(out: RunOutcome) -> int:
    """打印一次运行（或恢复）停在哪里、下一步该做什么，返回退出码。"""
    print(f"run_id      : {out.run_id}")
    print(f"status      : {out.status}")
    print(f"stop_reason : {out.stop_reason}")
    if out.detail:
        print(f"detail      : {out.detail}")
    print(f"steps_used  : {out.steps_used}")
    if out.final_answer:
        print(f"final_answer: {out.final_answer}")
    for ap in out.pending_approvals:
        print(f"\n待审批 {ap['approval_id']}: {ap['tool']} {ap['target']} (tool_call_id={ap['tool_call_id']})")
        if ap.get("preview"):
            print(ap["preview"])
        print(f"→ python harness_cli.py approve {out.run_id} {ap['approval_id']} --actor <name>")
    for action_id in out.unknown_actions:
        print(f"\n结果未知 {action_id}：核对工作区后执行")
        print(f"→ python harness_cli.py verify {out.run_id} {action_id} --applied|--not-applied --actor <name>")
    if out.status is RunStatus.WAITING_VERIFICATION and not out.unknown_actions:
        print(f"→ 核对改动后：python harness_cli.py ack-workspace {out.run_id} --actor <name>")
    return EXIT_CODES[out.status]


# ---------------- 运行记录 ----------------


def _diff_section(store: StateStore, state: RunState, sandbox: Sandbox) -> tuple[list[str], list[dict]]:
    """用第一次写入前保存的原件和当前文件生成 diff，同时收集每个文件的哈希供互证。"""
    current = sandbox.snapshot()
    changed = sorted(p for p in set(state.baseline_files) | set(current)
                     if state.baseline_files.get(p) != current.get(p))
    lines, files = [], []
    for rel in changed:
        original = store.load_preimage(state.run_id, rel)
        now_path = Path(state.workspace) / rel
        new_text = now_path.read_text(encoding="utf-8", errors="replace") if now_path.is_file() else ""
        if original is False:
            lines.append(f"# {rel}: changed outside the harness (no pre-image recorded)")
        else:
            old_text = original.decode("utf-8", errors="replace") if original is not None else ""
            lines.extend(difflib.unified_diff(old_text.splitlines(), new_text.splitlines(),
                                              fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm=""))
        files.append({"path": rel, "baseline": state.baseline_files.get(rel), "current": current.get(rel),
                      "expected": state.expected_files.get(rel)})
    return lines, files


def render_report(state_dir: Path, run_id: str) -> str:
    store = StateStore(state_dir)
    audit = AuditLog(state_dir / "audit.jsonl")
    state = store.load(run_id)
    sandbox = Sandbox(Path(state.workspace))
    records = audit.records(run_id=run_id)
    chain_len = audit.verify()
    b = state.budget
    out = [
        f"# 运行记录 {run_id}",
        "",
        f"- 任务：{state.task}",
        f"- 类型：`{state.kind}`　工作区：`{state.workspace}`",
        f"- 结果：**{state.status}**　停止原因：`{state.stop_reason}`　{state.stop_detail or ''}",
        f"- 预算：步数 {b.used_steps}/{b.max_steps}，工具调用 {b.used_tool_calls}/{b.max_tool_calls}，"
        f"token {b.used_tokens}，耗时 {b.used_wall_seconds:.1f}s，会话数 {state.session}",
    ]
    if state.final_answer:
        out += ["", "## 最终答案", "", state.final_answer]
    out += ["", "## 计划", ""] + ([f"- [{p.status}] {p.step}" for p in state.plan] or ["（无）"])

    out += ["", "## 步骤与动作", "", "| 步 | 会话 | tool_call_id | 工具 | 来源 | 状态 | 规则 / 错误 |", "|---|---|---|---|---|---|---|"]
    session_of = {s.number: s.session for s in state.steps}
    for a in state.actions:
        why = a.rule or ""
        if a.error:
            why = f"{why} {a.error.get('code', '')}".strip()
        out.append(f"| {a.step} | {session_of.get(a.step, '')} | {a.tool_call_id} | {a.tool} {a.target or ''} | "
                   f"{a.origin} | {a.status} | {why} |")

    out += ["", "## 审批", ""]
    for ap in state.approvals.values():
        out.append(f"- {ap.approval_id} `{ap.tool} {ap.target}` ← {ap.tool_call_id}：**{ap.status}**，"
                   f"审批人 {ap.decided_by or '-'}，执行动作 {ap.executed_action_id or '-'}")
    if not state.approvals:
        out.append("（无）")

    current_fp = sandbox.code_fingerprint()
    out += ["", "## 证据", "", "| id | 类型 | 对象 | 结果 | 代码指纹 | 有效 | 失效原因 |", "|---|---|---|---|---|---|---|"]
    for e in state.evidence:
        obj = e.path or e.command or ""
        result = e.summary or ("passed" if e.passed else "") or (e.file_hash or "")[:12]
        out.append(f"| {e.evidence_id} | {e.kind} | {obj} | {result} | {e.code_fingerprint[:12]} | "
                   f"{'✅' if e.valid else '❌'} | {e.invalidated_reason or ''} |")

    diff_lines, files = _diff_section(store, state, sandbox)
    out += ["", "## Diff（基线 → 当前）", "", "```diff", *(diff_lines or ["（无改动）"]), "```"]

    # ---- 互证：diff、审计、状态、测试证据指向同一个版本 ----
    write_results = {}
    for r in records:
        if r.event == "exec.result" and r.decision == "ok" and r.tool in ("write_file", "replace_in_file"):
            write_results[r.details["result"]["path"]] = r.details["result"]["sha256_after"]
    passing = [e for e in state.evidence if e.kind is EvidenceKind.TEST_RUN and e.valid and e.passed and e.full_suite]
    out += ["", "## 互证检查", ""]
    for f in files:
        audit_hash = write_results.get(f["path"])
        ok = f["current"] == f["expected"] and (audit_hash is None or audit_hash == f["current"])
        out.append(f"- {'✅' if ok else '❌'} `{f['path']}`：当前 {str(f['current'])[:12]} / 状态期望 "
                   f"{str(f['expected'])[:12]} / 审计最后写入 {str(audit_hash)[:12]}")
    if passing:
        last = passing[-1]
        same = last.code_fingerprint == current_fp
        out.append(f"- {'✅' if same else '❌'} 最后一次通过的完整测试 {last.evidence_id}（{last.summary}）的代码指纹 "
                   f"{last.code_fingerprint[:12]}，当前代码指纹 {current_fp[:12]}")
    else:
        out.append(f"- 没有有效的完整测试通过记录；当前代码指纹 {current_fp[:12]}")
    out.append(f"- ✅ 审计哈希链校验通过（全文件 {chain_len} 条，本 run {len(records)} 条）")

    ckpts = CheckpointManager(state_dir).history(run_id)
    out += ["", "## 检查点", "", "| seq | 步 | 原因 | 状态 | 待审批 |", "|---|---|---|---|---|"]
    for path in ckpts:
        data = json.loads(path.read_text(encoding="utf-8"))
        pending = [a for a, v in data["state"]["approvals"].items() if v["status"] == "PENDING"]
        out.append(f"| {data['seq']} | {data['step']} | {data['reason']} | {data['state']['status']} | {', '.join(pending)} |")

    out += ["", "## 审计决策链", "", "```"]
    out += [r.brief() for r in records]
    out += ["```", ""]
    return "\n".join(out)


# ---------------- 子命令 ----------------


def cmd_prepare(args: argparse.Namespace) -> int:
    """复制一份干净的演示仓库到 workspaces/<name>，每次运行都在副本上改，原件不动。"""
    src = Path(args.source).resolve()
    dst = (ROOT / "workspaces" / args.name).resolve()
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    print(dst)
    return 0


def _harness(args: argparse.Namespace, model=None) -> GuardedHarness:
    config = HarnessConfig(
        max_steps=getattr(args, "max_steps", 20),
        max_tool_calls=getattr(args, "max_tool_calls", 60),
        max_tokens=getattr(args, "max_tokens", None),
        verbosity=LogLevel.INFO if getattr(args, "verbose", False) else LogLevel.OFF,
    )
    return GuardedHarness(state_dir=Path(args.state_dir), model=model, config=config)


def cmd_run(args: argparse.Namespace) -> int:
    h = _harness(args, build_model(args.model, args.script_log))
    return print_outcome(h.start(workspace=args.workspace, task=args.task, kind=args.kind))


def cmd_resume(args: argparse.Namespace) -> int:
    h = _harness(args, build_model(args.model, args.script_log))
    return print_outcome(h.resume(args.run_id))


def cmd_approve(args: argparse.Namespace) -> int:
    ap = _harness(args).approve(args.run_id, args.approval_id, actor=args.actor)
    print(f"{ap.approval_id} → {ap.status}（{ap.tool} {ap.target}），执行 resume 继续")
    return 0


def cmd_reject(args: argparse.Namespace) -> int:
    ap = _harness(args).reject(args.run_id, args.approval_id, actor=args.actor, reason=args.reason)
    print(f"{ap.approval_id} → {ap.status}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    _harness(args).resolve_unknown(args.run_id, args.action_id, applied=args.applied, actor=args.actor)
    print(f"{args.action_id} 已核对为 {'applied' if args.applied else 'not applied'}，执行 resume 继续")
    return 0


def cmd_ack(args: argparse.Namespace) -> int:
    drift = _harness(args).acknowledge_workspace(args.run_id, actor=args.actor)
    print(f"已确认 {len(drift)} 个文件的外部改动：{', '.join(drift) or '（无）'}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    state = StateStore(Path(args.state_dir)).load(args.run_id)
    if args.json:
        print(state.model_dump_json(indent=2))
    else:
        b = state.budget
        print(f"{state.run_id} {state.status} {state.stop_reason or ''} steps={b.used_steps}/{b.max_steps} "
              f"tool_calls={b.used_tool_calls}/{b.max_tool_calls} session={state.session}")
        for a in state.actions:
            if a.status in (ActionStatus.UNKNOWN_RESULT, ActionStatus.PENDING_APPROVAL):
                print(f"  {a.action_id} {a.tool_call_id} {a.tool} {a.target} {a.status}")
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    log = AuditLog(Path(args.state_dir) / "audit.jsonl")
    if args.verify:
        print(f"hash chain OK: {log.verify()} records")
    for rec in log.records(run_id=args.run_id, tool_call_id=args.tool_call_id):
        print(json.dumps(rec.to_dict(), ensure_ascii=False) if args.json else rec.brief())
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    text = render_report(Path(args.state_dir), args.run_id)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
        print(args.out)
    else:
        print(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Guarded Coding Harness on smolagents")
    parser.add_argument("--state-dir", default=str(ROOT / ".harness"), help="状态、检查点、审计的存放目录")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("prepare", help="复制演示仓库到 workspaces/<name>")
    p.add_argument("--source", default=str(ROOT / "demo_repos" / "shop"))
    p.add_argument("--name", required=True)
    p.set_defaults(func=cmd_prepare)

    for name, func in (("run", cmd_run), ("resume", cmd_resume)):
        p = sub.add_parser(name)
        if name == "run":
            p.add_argument("--workspace", required=True)
            p.add_argument("--kind", required=True, choices=["test_fix", "code_understanding"])
            p.add_argument("--task", required=True)
        else:
            p.add_argument("run_id")
        p.add_argument("--model", default="deepseek", help="deepseek 或 script:<path.json>")
        p.add_argument("--script-log", help="脚本模型把收到的 prompt 追加写到这里")
        p.add_argument("--max-steps", type=int, default=20)
        p.add_argument("--max-tool-calls", type=int, default=60)
        p.add_argument("--max-tokens", type=int)
        p.add_argument("--verbose", action="store_true", help="打印 smolagents 的逐步日志")
        p.set_defaults(func=func)

    p = sub.add_parser("approve")
    p.add_argument("run_id")
    p.add_argument("approval_id")
    p.add_argument("--actor", required=True)
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("reject")
    p.add_argument("run_id")
    p.add_argument("approval_id")
    p.add_argument("--actor", required=True)
    p.add_argument("--reason", required=True)
    p.set_defaults(func=cmd_reject)

    p = sub.add_parser("verify", help="核对一个 UNKNOWN_RESULT 动作")
    p.add_argument("run_id")
    p.add_argument("action_id")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--applied", dest="applied", action="store_true")
    g.add_argument("--not-applied", dest="applied", action="store_false")
    p.add_argument("--actor", required=True)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("ack-workspace", help="确认工作区的外部改动")
    p.add_argument("run_id")
    p.add_argument("--actor", required=True)
    p.set_defaults(func=cmd_ack)

    p = sub.add_parser("status")
    p.add_argument("run_id")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("audit")
    p.add_argument("run_id", nargs="?")
    p.add_argument("--tool-call-id")
    p.add_argument("--verify", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("report")
    p.add_argument("run_id")
    p.add_argument("--out")
    p.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
