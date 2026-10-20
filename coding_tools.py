"""编码任务的工具：参数模型 + handler。

handler 只做 I/O：拿 Gate 已经解析好的 ctx.path / ctx.command，调用 Sandbox 原语，
返回结构化结果。参数校验、权限、审批、证据、预算这些规则都在 Gate 里，不在这里。
"""

from __future__ import annotations

import difflib
import hashlib
import re
from typing import Literal

from pydantic import Field

from gate import Risk, StrictArgs, ToolContext, ToolError, ToolRegistry, ToolSpec
from sandbox import bound_text
from state_store import EvidenceKind

READ_LIMIT_CHARS = 20_000
SEARCH_MAX_MATCHES = 100
SEARCH_MAX_FILE_BYTES = 500_000
_PYTEST_SUMMARY = re.compile(r"\b\d+ (passed|failed|errors?|skipped|xfailed|xpassed|deselected)\b|no tests ran")


# ---------------- 参数模型 ----------------


class ListFilesArgs(StrictArgs):
    path: str = Field(".", description="Directory relative to the repository root (default '.')")


class ReadFileArgs(StrictArgs):
    path: str = Field(description="File path relative to the repository root")
    start_line: int | None = Field(None, ge=1, description="First line to return, 1-based (optional)")
    end_line: int | None = Field(None, ge=1, description="Last line to return, inclusive (optional)")


class SearchCodeArgs(StrictArgs):
    pattern: str = Field(min_length=1, max_length=200, description="Python regular expression")
    path: str = Field(".", description="File or directory to search (default '.')")


class WriteFileArgs(StrictArgs):
    path: str = Field(description="File path relative to the repository root")
    content: str = Field(description="The complete new content of the file")


class ReplaceInFileArgs(StrictArgs):
    path: str = Field(description="File path relative to the repository root")
    old: str = Field(min_length=1, description="Exact text to replace; it must occur exactly once in the file")
    new: str = Field(description="Replacement text")


class RunTestArgs(StrictArgs):
    command: str = Field(description="A pytest command, e.g. 'pytest -q' or 'pytest -q tests/test_x.py::test_y'")
    timeout_seconds: int | None = Field(None, ge=1, le=300, description="Time limit in seconds (optional)")


class PlanEntry(StrictArgs):
    step: str = Field(min_length=1, max_length=200, description="What to do")
    status: Literal["pending", "in_progress", "done"] = Field("pending", description="pending, in_progress or done")


class UpdatePlanArgs(StrictArgs):
    items: list[PlanEntry] = Field(min_length=1, max_length=20, description="The whole plan, in order")


class FinalAnswerArgs(StrictArgs):
    answer: str = Field(min_length=1, description="Final answer: what was done and how it was verified")


# ---------------- handler ----------------


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_text(ctx: ToolContext) -> tuple[bytes, str]:
    """读 Gate 已解析好的文件，返回 (原始字节, UTF-8 文本)。"""
    rp = ctx.path
    assert rp is not None
    if not rp.abs.is_file():
        raise ToolError("NOT_FOUND", f"'{rp.rel}' does not exist or is not a file")
    data = ctx.sandbox.read_bytes(rp)
    try:
        return data, data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolError("NOT_TEXT", f"'{rp.rel}' is not UTF-8 text") from exc


def unified_diff(rel: str, old: str, new: str, limit: int = 6_000) -> str:
    """给审批人看的 unified diff（限长）。"""
    lines = difflib.unified_diff(old.splitlines(), new.splitlines(), fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm="")
    return bound_text("\n".join(lines), limit)[0]


def list_files(ctx: ToolContext, args: ListFilesArgs) -> dict:
    """列目录。敏感文件和缓存目录由 Sandbox 隐藏。"""
    base = ctx.path
    assert base is not None
    if not base.abs.is_dir():
        raise ToolError("NOT_A_DIRECTORY", f"'{base.rel}' is not a directory")
    entries, truncated = ctx.sandbox.list_tree(base)
    return {"path": base.rel, "entries": entries, "truncated": truncated}


def read_file(ctx: ToolContext, args: ReadFileArgs) -> dict:
    """读文件（可选行范围）。返回内容和整个文件的 sha256，Gate 用 sha256 记录读取证据。"""
    data, text = _read_text(ctx)
    lines = text.splitlines()
    start = args.start_line or 1
    end = min(args.end_line or len(lines), len(lines))
    if lines and start > len(lines):
        raise ToolError("BAD_RANGE", f"start_line {start} is beyond the end of the file ({len(lines)} lines)")
    if args.end_line is not None and args.end_line < start:
        raise ToolError("BAD_RANGE", "end_line is before start_line")
    # 脱敏 + 限长
    content, truncated = ctx.sandbox.bound("\n".join(lines[start - 1 : end]), READ_LIMIT_CHARS)
    return {
        "path": ctx.path.rel,
        "sha256": _sha256(data),
        "total_lines": len(lines),
        "start_line": start,
        "end_line": end,
        "truncated": truncated,
        "content": content,
    }


def search_code(ctx: ToolContext, args: SearchCodeArgs) -> dict:
    """正则搜索。为了不被病态正则拖垮：只看每行前 500 个字符，最多返回 100 条。"""
    try:
        regex = re.compile(args.pattern)
    except re.error as exc:
        raise ToolError("INVALID_PATTERN", f"invalid regular expression: {exc}") from exc
    assert ctx.path is not None
    matches: list[str] = []
    scanned = 0
    for rel, path in ctx.sandbox.iter_files(ctx.path):
        data = path.read_bytes()
        if len(data) > SEARCH_MAX_FILE_BYTES or b"\0" in data[:1024]:
            continue  # 跳过大文件和二进制
        scanned += 1
        for lineno, line in enumerate(data.decode("utf-8", "replace").splitlines(), start=1):
            if regex.search(line[:500]):
                matches.append(ctx.sandbox.redact(f"{rel}:{lineno}: {line.strip()[:200]}"))
                if len(matches) >= SEARCH_MAX_MATCHES:
                    return {"pattern": args.pattern, "files_scanned": scanned, "matches": matches, "truncated": True}
    return {"pattern": args.pattern, "files_scanned": scanned, "matches": matches, "truncated": False}


def write_file(ctx: ToolContext, args: WriteFileArgs) -> dict:
    """整文件写入。能执行到这里，说明 Gate 的审批 / 幂等 / 路径检查都已通过。"""
    assert ctx.path is not None
    before, after = ctx.sandbox.write_text(ctx.path, args.content)
    return {
        "path": ctx.path.rel,
        "created": before is None,
        "bytes": len(args.content.encode("utf-8")),
        "sha256_before": before,
        "sha256_after": after,
    }


def preview_write(ctx: ToolContext, args: WriteFileArgs) -> str:
    """审批预览：当前内容 → 新内容的 diff，不落盘。"""
    assert ctx.path is not None
    old = _read_text(ctx)[1] if ctx.path.abs.is_file() else ""
    return unified_diff(ctx.path.rel, old, args.content)


def _replaced(ctx: ToolContext, args: ReplaceInFileArgs) -> tuple[str, str]:
    """计算替换后的内容；要替换的片段必须恰好出现一次，避免改错地方。"""
    _, text = _read_text(ctx)
    count = text.count(args.old)
    if count == 0:
        raise ToolError("OLD_NOT_FOUND", f"the text to replace was not found in '{ctx.path.rel}'")
    if count > 1:
        raise ToolError("OLD_NOT_UNIQUE", f"the text to replace occurs {count} times in '{ctx.path.rel}'; add context")
    return text, text.replace(args.old, args.new, 1)


def replace_in_file(ctx: ToolContext, args: ReplaceInFileArgs) -> dict:
    """局部替换后整文件原子写回。"""
    _, new_text = _replaced(ctx, args)
    before, after = ctx.sandbox.write_text(ctx.path, new_text)
    return {
        "path": ctx.path.rel,
        "created": False,
        "bytes": len(new_text.encode("utf-8")),
        "sha256_before": before,
        "sha256_after": after,
    }


def preview_replace(ctx: ToolContext, args: ReplaceInFileArgs) -> str:
    """审批预览；替换片段找不到或不唯一时直接报错，不会拿一个改不了的编辑去打扰审批人。"""
    old, new = _replaced(ctx, args)
    return unified_diff(ctx.path.rel, old, new)


def pytest_summary(output: str) -> str:
    """从 pytest 输出里找最后的汇总行，如 “5 failed, 7 passed in 0.03s”。"""
    for line in reversed(output.splitlines()):
        if _PYTEST_SUMMARY.search(line):
            return line.strip("= ").strip()
    return ""


def run_test(ctx: ToolContext, args: RunTestArgs) -> dict:
    """执行 Gate 已规范化的 pytest 命令；超时 / 取消转成结构化错误（进程组已由 Sandbox 终止）。"""
    command = ctx.command
    assert command is not None
    limit = min(args.timeout_seconds or ctx.sandbox.policy.default_timeout, ctx.sandbox.policy.max_timeout)
    result = ctx.sandbox.run(command.argv, timeout=limit, cancel=ctx.cancel)
    if result.cancelled:
        raise ToolError(
            "CANCELLED", "test command cancelled by the user; its process group was terminated",
            rule="sandbox.cancelled", actor="sandbox", pgid=result.pgid, duration=result.duration,
        )
    if result.timed_out:
        raise ToolError(
            "TIMEOUT", f"test command exceeded {limit:g}s; its process group was terminated",
            rule="sandbox.timeout", actor="sandbox", pgid=result.pgid, duration=result.duration, timeout_seconds=limit,
        )
    return {
        "command": command.display,
        "exit_code": result.exit_code,
        "passed": result.exit_code == 0,
        "summary": pytest_summary(result.output),
        "duration": result.duration,
        "truncated": result.truncated,
        "output": result.output,
    }


def update_plan(ctx: ToolContext, args: UpdatePlanArgs) -> dict:
    """计划本身由 Gate 写进状态，这里只是原样返回。"""
    return {"items": [item.model_dump() for item in args.items]}


def final_answer(ctx: ToolContext, args: FinalAnswerArgs) -> str:
    """能执行到这里说明验收已通过；返回值就是最终答案。"""
    return args.answer


def build_registry() -> ToolRegistry:
    """全部 8 个工具。哪些参数是路径 / 命令、要不要审批、产生什么证据，都在这里声明，由 Gate 执行。"""
    return ToolRegistry(
        [
            ToolSpec(
                "list_files",
                "List files and directories under a repository path (sensitive and cache directories are hidden).",
                ListFilesArgs, list_files, Risk.READ, path_arg="path",
            ),
            ToolSpec(
                "read_file",
                "Read a UTF-8 file of the repository, optionally only a line range. Returns the content and its sha256.",
                ReadFileArgs, read_file, Risk.READ, path_arg="path", evidence=EvidenceKind.FILE_READ,
            ),
            ToolSpec(
                "search_code",
                "Search repository files with a regular expression; returns 'path:line: text' matches.",
                SearchCodeArgs, search_code, Risk.READ, path_arg="path",
            ),
            ToolSpec(
                "write_file",
                "Create or overwrite a file with its complete new content. Writes outside artifacts/ need human approval.",
                WriteFileArgs, write_file, Risk.WRITE, path_arg="path", requires_approval=True,
                preview=preview_write, evidence=EvidenceKind.FILE_WRITE,
            ),
            ToolSpec(
                "replace_in_file",
                "Replace one exact snippet that occurs exactly once in a file. Writes outside artifacts/ need human approval.",
                ReplaceInFileArgs, replace_in_file, Risk.WRITE, path_arg="path", requires_approval=True,
                preview=preview_replace, evidence=EvidenceKind.FILE_WRITE,
            ),
            ToolSpec(
                "run_test",
                "Run pytest inside the repository without a shell, e.g. 'pytest -q' or 'pytest -q tests/test_x.py'.",
                RunTestArgs, run_test, Risk.EXEC, command_arg="command", evidence=EvidenceKind.TEST_RUN,
            ),
            ToolSpec(
                "update_plan",
                "Record your current plan; send the whole list every time.",
                UpdatePlanArgs, update_plan, Risk.STATE,
            ),
            ToolSpec(
                "final_answer",
                "Finish the task. Accepted only when the deliverable and fresh evidence exist; it must be the only "
                "tool call in its step.",
                FinalAnswerArgs, final_answer, Risk.COMPLETE,
            ),
        ]
    )
