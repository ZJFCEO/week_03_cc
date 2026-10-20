"""完成验收：模型说“做完了”不算数，交付物和证据对得上才算。

Gate 收到 final_answer 时调用 evaluate()。任何一项不通过，final answer 被拒绝，
运行保持 RUNNING，模型拿到失败项清单继续干活。

通用检查：
    answer_present          答案非空
    no_open_approvals       没有待审批或已批未执行的写入
    no_unknown_results      没有结果未知的动作
    workspace_consistent    工作区和 Harness 记录一致（没有绕过 Gate 的改动）

test_fix：
    source_changed          至少一个源码文件相对基线有改动（交付物 = diff）
    tests_untouched         测试和测试配置没被改
    fresh_passing_full_suite  在“当前”代码指纹上有一次通过的完整测试集运行；
                              改过代码之后的旧证据不算 (H2)

code_understanding：
    report_written          artifacts/*.md 由本次运行写出，且内容未被改过
    report_substantive      报告长度达到下限
    grounded_in_reads       至少读过 N 个不同文件
    report_cites_read_files 报告里引用了实际读过的文件
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from checkpoint import workspace_drift
from sandbox import Sandbox, fingerprint, match_path
from state_store import EvidenceKind, RunState, TaskKind


class CheckResult(BaseModel):
    """一项检查的结果；detail 会原样返回给模型，告诉它还差什么。"""

    name: str
    passed: bool
    detail: str


class Verdict(BaseModel):
    """验收结论：全部检查通过才 accepted。整份结论会写进审计和 RunState.completion。"""

    accepted: bool
    kind: str
    code_fingerprint: str
    checks: list[CheckResult]

    def failures(self) -> list[str]:
        return [f"{c.name}: {c.detail}" for c in self.checks if not c.passed]

    def failed_checks(self) -> list[str]:
        return [c.name for c in self.checks if not c.passed]


@dataclass
class CompletionChecker:
    """完成检查器。只看事实（工作区快照、状态里的证据），不看模型怎么说。"""

    sandbox: Sandbox
    protected_patterns: tuple[str, ...] = ()  # test_fix 下不允许改的路径（测试与测试配置）
    min_report_chars: int = 200
    min_files_read: int = 2
    report_prefix: str = "artifacts/"

    def evaluate(self, state: RunState, answer: str) -> Verdict:
        """对当前工作区重新拍快照再判断：不信任任何缓存的结论。"""
        snapshot = self.sandbox.snapshot()
        code_now = self.sandbox.code_files(snapshot)
        checks = [
            CheckResult(
                name="answer_present",
                passed=bool(answer and answer.strip()),
                detail="final answer text is present" if answer and answer.strip() else "final answer is empty",
            ),
            self._no_open_approvals(state),
            self._no_unknown_results(state),
            self._workspace_consistent(state, snapshot),
        ]
        if state.kind is TaskKind.TEST_FIX:
            checks += self._test_fix_checks(state, code_now)
        else:
            checks += self._understanding_checks(state, snapshot)
        return Verdict(
            accepted=all(c.passed for c in checks),
            kind=str(state.kind),
            code_fingerprint=fingerprint(code_now),
            checks=checks,
        )

    # ---------------- 通用 ----------------

    def _no_open_approvals(self, state: RunState) -> CheckResult:
        open_ids = [a.approval_id for a in state.pending_approvals() + state.granted_approvals()]
        return CheckResult(
            name="no_open_approvals",
            passed=not open_ids,
            detail="no pending approvals" if not open_ids else f"approvals still open: {', '.join(open_ids)}",
        )

    def _no_unknown_results(self, state: RunState) -> CheckResult:
        unknown = [a.action_id for a in state.unknown_results() + state.in_flight()]
        return CheckResult(
            name="no_unknown_results",
            passed=not unknown,
            detail="every action has a known result" if not unknown else f"unverified actions: {', '.join(unknown)}",
        )

    def _workspace_consistent(self, state: RunState, snapshot: dict[str, str]) -> CheckResult:
        """工作区必须和 Harness 记录的一致：绕过 Gate 的改动（不论是谁做的）都会让验收失败。"""
        drift = workspace_drift(state.expected_files, snapshot)
        return CheckResult(
            name="workspace_consistent",
            passed=not drift,
            detail="workspace matches the harness record"
            if not drift
            else f"files changed outside the harness: {', '.join(sorted(drift))}",
        )

    # ---------------- test_fix ----------------

    def _test_fix_checks(self, state: RunState, code_now: dict[str, str]) -> list[CheckResult]:
        """测试修复的三项：有源码改动、没动测试、当前代码版本上有一次通过的完整测试。"""
        # 交付物 = 相对基线的源码改动
        base = self.sandbox.code_files(state.baseline_files)
        changed = sorted(p for p in set(base) | set(code_now) if base.get(p) != code_now.get(p))
        protected = [p for p in changed if match_path(p, self.protected_patterns)]
        source = [p for p in changed if p not in protected]

        # 测试证据必须“新鲜”：有效、跑的是完整测试集、代码指纹等于当前代码指纹（H2）
        current_fp = fingerprint(code_now)
        runs = [e for e in state.evidence if e.kind is EvidenceKind.TEST_RUN]
        fresh_full = [e for e in runs if e.valid and e.full_suite and e.code_fingerprint == current_fp]
        latest = fresh_full[-1] if fresh_full else None  # 同一版本跑了多次，以最后一次为准
        if latest is not None and latest.passed:
            suite = CheckResult(
                name="fresh_passing_full_suite",
                passed=True,
                detail=f"{latest.evidence_id} `{latest.command}` passed on the current code ({latest.summary})",
            )
        elif latest is not None:
            suite = CheckResult(
                name="fresh_passing_full_suite",
                passed=False,
                detail=f"latest full-suite run on the current code failed ({latest.summary}); fix the code and rerun",
            )
        else:
            # 没有可用证据时，尽量告诉模型具体原因：旧的通过记录失效了，还是只跑了一部分
            stale = [e for e in runs if e.passed and not e.valid]
            partial = [e for e in runs if e.valid and e.passed and not e.full_suite and e.code_fingerprint == current_fp]
            if stale:
                hint = f"passing run {stale[-1].evidence_id} is stale ({stale[-1].invalidated_reason})"
            elif partial:
                hint = f"{partial[-1].evidence_id} `{partial[-1].command}` only ran a subset"
            else:
                hint = "no test run on the current code"
            suite = CheckResult(
                name="fresh_passing_full_suite",
                passed=False,
                detail=f"{hint}; run the full suite (`pytest -q`) on the current code",
            )

        return [
            CheckResult(
                name="source_changed",
                passed=bool(source),
                detail=f"changed: {', '.join(source)}" if source else "no source file differs from the baseline; nothing to deliver",
            ),
            CheckResult(
                name="tests_untouched",
                passed=not protected,
                detail="tests and test config unchanged" if not protected else f"protected files changed: {', '.join(protected)}",
            ),
            suite,
        ]

    # ---------------- code_understanding ----------------

    def _understanding_checks(self, state: RunState, snapshot: dict[str, str]) -> list[CheckResult]:
        """代码理解的四项：报告由本次运行写出且未被改动、有一定篇幅、读过足够的文件、报告引用了读过的文件。"""
        # 报告必须是本次运行通过 Gate 写的，且写完之后没被别人改过（哈希一致）
        writes = {
            e.path: e
            for e in state.valid_evidence(EvidenceKind.FILE_WRITE)
            if e.path and e.path.startswith(self.report_prefix) and e.path.endswith(".md")
        }
        reports = [path for path, ev in writes.items() if snapshot.get(path) == ev.file_hash]
        texts = {path: (self.sandbox.root / path).read_text(encoding="utf-8", errors="replace") for path in reports}
        longest = max((len(t) for t in texts.values()), default=0)
        # “言之有据”：报告里至少提到一个真正读过的文件（全路径或文件名）
        read_paths = sorted({e.path for e in state.valid_evidence(EvidenceKind.FILE_READ) if e.path})
        cited = sorted({p for p in read_paths for t in texts.values() if p in t or p.rsplit("/", 1)[-1] in t})
        return [
            CheckResult(
                name="report_written",
                passed=bool(reports),
                detail=f"report: {', '.join(reports)}" if reports else f"no report written to {self.report_prefix}*.md in this run",
            ),
            CheckResult(
                name="report_substantive",
                passed=longest >= self.min_report_chars,
                detail=f"report has {longest} chars (minimum {self.min_report_chars})",
            ),
            CheckResult(
                name="grounded_in_reads",
                passed=len(read_paths) >= self.min_files_read,
                detail=f"read {len(read_paths)} distinct files (minimum {self.min_files_read})",
            ),
            CheckResult(
                name="report_cites_read_files",
                passed=bool(cited),
                detail=f"report cites: {', '.join(cited)}" if cited else "report does not mention any file that was actually read",
            ),
        ]
