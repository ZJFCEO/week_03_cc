"""执行边界：文件路径、敏感目录、命令白名单、超时、环境变量、输出上限。

Sandbox 不做业务判断（要不要审批、算不算完成），只回答“这个落点 / 这条命令能不能碰”，
并提供带边界的读写和子进程原语。Gate 在调用 handler 之前用它做检查，
handler 只能拿到 Sandbox 已经解析好的 ResolvedPath / PreparedCommand。

路径校验分三层，顺序固定：
    1. 词法归一化：artifacts/../../.env 这类 .. 逃逸在这里拦下（PATH_ESCAPE）
    2. 真实落点：os.path.realpath 跟随软链接后再判断一次（SYMLINK_ESCAPE）
    3. 敏感 / 只读 / 可写范围：对词法路径和真实路径都检查（SENSITIVE_PATH 等）

子进程用 start_new_session=True 放进独立进程组，超时或取消时对整个进程组
先 SIGTERM 再 SIGKILL，测试代码 fork 出来的孙进程也一起终止，而不是只停止等待。
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Literal, Sequence

Access = Literal["read", "write"]

# run_test 不经过 shell；出现这些字符说明模型以为自己在写 shell 命令，直接拒绝。
_SHELL_META = frozenset(";&|`$<>\n\r")
_TMP_PREFIX = ".harness-tmp-"


class SandboxViolation(Exception):
    """越过执行边界。code 给模型看，rule 进审计。"""

    def __init__(self, code: str, rule: str, message: str, **details: Any):
        super().__init__(message)
        self.code = code
        self.rule = rule
        self.message = message
        self.details = details


@dataclass(frozen=True)
class SandboxPolicy:
    """执行边界的全部配置。Harness 按任务类型覆盖 readonly_patterns / writable_prefixes。"""

    # 逐个路径分量匹配（大小写不敏感，macOS 默认文件系统不区分大小写）
    sensitive_patterns: tuple[str, ...] = (
        ".env", ".env.*", "*.pem", "*.key", "id_rsa*", "id_ed25519*",
        ".ssh", ".aws", ".git", ".harness", "secrets", "credentials*",
    )
    # 只读：以 / 结尾按前缀匹配，否则按 fnmatch 匹配整条相对路径
    readonly_patterns: tuple[str, ...] = ()
    # 可写范围：为空表示整个工作区（减去只读和敏感路径）
    writable_prefixes: tuple[str, ...] = ()
    ignored_dirs: frozenset[str] = frozenset(
        {".git", ".harness", "__pycache__", ".pytest_cache", ".venv", "node_modules", ".mypy_cache", ".ruff_cache"}
    )
    # “代码指纹”不包含产物目录：写报告不会让测试证据失效
    code_excluded_prefixes: tuple[str, ...] = ("artifacts/",)
    max_file_bytes: int = 2_000_000  # 单个文件读取上限
    max_output_chars: int = 6_000  # 命令输出上限（保留头尾）
    default_timeout: float = 60.0  # 命令默认超时（秒）
    max_timeout: float = 300.0  # 模型自己指定超时也不能超过这个值
    kill_grace: float = 1.0  # SIGTERM 之后等多久再 SIGKILL
    # 允许的测试启动方式；最终都会被规范化成 “当前解释器 -m pytest”
    test_launchers: tuple[tuple[str, ...], ...] = (("pytest",), ("python", "-m", "pytest"), ("python3", "-m", "pytest"))
    pytest_flags: frozenset[str] = frozenset(
        {"-q", "-qq", "-v", "-vv", "-x", "-s", "-l", "-ra", "-rA", "-rf", "--quiet", "--verbose", "--exitfirst",
         "--no-header", "--tb=short", "--tb=line", "--tb=no", "--tb=long", "--tb=native"}
    )
    pytest_value_flags: frozenset[str] = frozenset({"-k", "--maxfail"})  # 后面跟一个参数值的选项
    python_executable: str = sys.executable
    # 子进程只继承这些环境变量；名字里带 secret_env_markers 的即使列在这里也不传
    env_passthrough: tuple[str, ...] = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR", "SYSTEMROOT")
    secret_env_markers: tuple[str, ...] = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "COOKIE")


@dataclass(frozen=True)
class ResolvedPath:
    raw: str  # 模型给的原始字符串
    rel: str  # 真实落点相对工作区根的 posix 路径，根目录是 "."
    abs: Path  # 真实落点的绝对路径（软链接已解析）


@dataclass(frozen=True)
class PreparedCommand:
    argv: tuple[str, ...]  # 实际执行的 argv，第一个元素是解释器
    display: str  # 规范化后的命令，写进证据和审计
    targets: tuple[str, ...]
    full_suite: bool  # 没有 -k、没有指定测试路径，才算“完整测试集”


@dataclass(frozen=True)
class ProcessResult:
    argv: tuple[str, ...]
    exit_code: int | None  # 超时或取消时为 None
    output: str
    truncated: bool
    timed_out: bool
    cancelled: bool
    duration: float
    pgid: int


def file_sha256(path: Path | str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bound_text(text: str, limit: int) -> tuple[str, bool]:
    """超过上限时保留头尾、中间标注省略量。"""
    if len(text) <= limit:
        return text, False
    head = limit // 2
    tail = limit - head
    omitted = len(text) - head - tail
    return f"{text[:head]}\n...[{omitted} chars truncated]...\n{text[-tail:]}", True


def _within(path: str, root: str) -> bool:
    """path 是否等于 root 或在 root 之下（两者都是规范化后的绝对路径）。"""
    return path == root or os.path.commonpath([path, root]) == root


def _match_component(rel: str, patterns: Sequence[str]) -> str | None:
    """逐级比较路径分量：src/.env.local 的 “.env.local” 命中 “.env.*”。返回命中的分量。"""
    for part in Path(rel).parts:
        low = part.lower()
        for pattern in patterns:
            if fnmatch.fnmatchcase(low, pattern.lower()):
                return part
    return None


def match_path(rel: str, patterns: Sequence[str]) -> str | None:
    """整条相对路径匹配：以 / 结尾的模式按目录前缀匹配，其他按 fnmatch。返回命中的模式。"""
    low = rel.lower()
    for pattern in patterns:
        pat = pattern.lower()
        if pat.endswith("/"):
            if low == pat.rstrip("/") or low.startswith(pat):
                return pattern
        elif fnmatch.fnmatchcase(low, pat):
            return pattern
    return None


class _BoundedCollector:
    """读子进程输出：只保留头尾各一半，防止测试刷屏把内存或上下文撑爆。"""

    def __init__(self, limit: int):
        self.head_limit = limit // 2
        self.tail_limit = limit - self.head_limit
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def drain(self, stream: BinaryIO) -> None:
        try:
            while chunk := stream.read1(65536):  # type: ignore[attr-defined]
                self._feed(chunk)
        except (OSError, ValueError):
            pass

    def _feed(self, chunk: bytes) -> None:
        self.total += len(chunk)
        room = self.head_limit - len(self.head)
        if room > 0:
            self.head += chunk[:room]
            chunk = chunk[room:]
        if chunk:
            self.tail += chunk
            if len(self.tail) > 2 * self.tail_limit:
                del self.tail[: len(self.tail) - self.tail_limit]

    def text(self) -> tuple[str, bool]:
        tail = bytes(self.tail[-self.tail_limit :]) if self.tail_limit else b""
        kept = len(self.head) + len(tail)
        head_text = self.head.decode("utf-8", "replace")
        if self.total <= kept:
            return head_text + tail.decode("utf-8", "replace"), False
        return f"{head_text}\n...[{self.total - kept} bytes truncated]...\n{tail.decode('utf-8', 'replace')}", True


def _signal_group(pgid: int, sig: int) -> bool:
    """给整个进程组发信号；返回进程组是否还有成员。

    macOS 上组里只剩僵尸进程（等 launchd 回收）时 killpg 会短暂返回 EPERM，
    这时按“还有成员”处理、继续等，而不是当成已经结束。
    """
    try:
        os.killpg(pgid, sig)
        return True
    except ProcessLookupError:  # ESRCH：组里一个进程都没有了
        return False
    except PermissionError:  # EPERM：成员是正在被回收的僵尸
        return True


def _terminate_group(pgid: int, proc: subprocess.Popen, grace: float) -> None:
    """终止整个进程组：先 SIGTERM，宽限期内等全部退出，否则 SIGKILL，最后回收组长。"""
    if not _signal_group(pgid, signal.SIGTERM):
        proc.wait()
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        proc.poll()  # 先回收组长，否则组长自己的僵尸会让进程组一直“存在”
        if not _signal_group(pgid, 0):  # 信号 0 只探测、不投递
            break
        time.sleep(0.01)
    else:
        _signal_group(pgid, signal.SIGKILL)  # 宽限期到了还有进程（比如忽略了 SIGTERM）就强杀
    proc.wait()


@dataclass
class Sandbox:
    """一个工作区的执行边界。所有路径都相对 root，所有判断都基于真实落点。"""

    root: Path
    policy: SandboxPolicy = field(default_factory=SandboxPolicy)

    def __post_init__(self) -> None:
        # 根目录本身也先 realpath（macOS 的 /var → /private/var），后面才能和子路径的 realpath 比较
        self.root = Path(os.path.realpath(self.root))
        if not self.root.is_dir():
            raise ValueError(f"workspace {self.root} is not a directory")
        self._root_str = str(self.root)
        # 父进程环境里像密钥的值：输出里一旦出现就替换成 [REDACTED]（长的先替换，避免部分替换）
        self._secret_values = sorted(
            {v for k, v in os.environ.items() if self._looks_secret(k) and len(v) >= 8}, key=len, reverse=True
        )

    # ---------------- 路径边界 ----------------

    def resolve(self, raw: Any, *, access: Access) -> ResolvedPath:
        """把模型给的路径解析成工作区内的真实落点，越界就抛 SandboxViolation。

        access="write" 时额外检查只读路径和可写范围。返回的 ResolvedPath 是 handler 唯一能用的路径。
        """
        if not isinstance(raw, str) or not raw.strip():
            raise SandboxViolation("INVALID_PATH", "sandbox.invalid_path", "path must be a non-empty string")
        if "\x00" in raw:
            raise SandboxViolation("INVALID_PATH", "sandbox.invalid_path", "path contains a NUL byte")

        # 第 1 层：词法归一化。join 遇到绝对路径会直接用它，所以 /etc/passwd 也在这里被拦
        lexical = os.path.normpath(os.path.join(self._root_str, raw))
        if not _within(lexical, self._root_str):
            raise SandboxViolation(
                "PATH_ESCAPE", "sandbox.path_escape", f"path '{raw}' resolves outside the workspace", raw=raw
            )
        lexical_rel = os.path.relpath(lexical, self._root_str)
        self._check_sensitive(lexical_rel, raw)

        # 第 2 层：跟随软链接后的真实落点（不存在的尾部保持原样，所以新建文件也能检查）
        real = os.path.realpath(lexical)
        if not _within(real, self._root_str):
            raise SandboxViolation(
                "SYMLINK_ESCAPE",
                "sandbox.symlink_escape",
                f"path '{raw}' points outside the workspace through a symlink",
                raw=raw,
                real_target=real,
            )
        # 第 3 层：对真实落点再查一次敏感路径（名字无害的软链接可能指向 .env）
        rel = Path(os.path.relpath(real, self._root_str)).as_posix()
        self._check_sensitive(rel, raw)

        if access == "write":
            self._check_writable(rel, raw, real)
        return ResolvedPath(raw=raw, rel=rel, abs=Path(real))

    def _check_sensitive(self, rel: str, raw: str) -> None:
        """任何一级目录或文件名命中敏感模式（.env、.git、*.pem……）就拒绝，读写都一样。"""
        hit = _match_component(rel, self.policy.sensitive_patterns)
        if hit is not None:
            raise SandboxViolation(
                "SENSITIVE_PATH",
                "sandbox.sensitive_path",
                f"path '{raw}' touches a protected location ({hit})",
                raw=raw,
                matched=hit,
                real_rel=rel,
            )

    def _check_writable(self, rel: str, raw: str, real: str) -> None:
        """写入额外的限制：只读路径（如 test_fix 下的 tests/）、可写范围（如代码理解只能写 artifacts/）。"""
        if rel == ".":
            raise SandboxViolation("READONLY_PATH", "sandbox.readonly_path", "cannot write the workspace root")
        hit = match_path(rel, self.policy.readonly_patterns)
        if hit is not None:
            raise SandboxViolation(
                "READONLY_PATH",
                "sandbox.readonly_path",
                f"'{rel}' is read-only for this task ({hit})",
                raw=raw,
                matched=hit,
            )
        prefixes = self.policy.writable_prefixes
        if prefixes and not any(rel == p.rstrip("/") or rel.startswith(p) for p in prefixes):
            raise SandboxViolation(
                "WRITE_SCOPE",
                "sandbox.write_scope",
                f"'{rel}' is outside the writable area ({', '.join(prefixes)})",
                raw=raw,
            )
        if os.path.isdir(real):
            raise SandboxViolation("READONLY_PATH", "sandbox.readonly_path", f"'{rel}' is a directory")

    def is_code_path(self, rel: str) -> bool:
        """属于“代码”还是“产物”：代码变化会让测试证据失效，产物（报告）变化不会。"""
        return not any(rel.startswith(p) for p in self.policy.code_excluded_prefixes)

    # ---------------- 文件读写 ----------------

    def read_bytes(self, rp: ResolvedPath) -> bytes:
        """读文件，超过大小上限直接拒绝（不读进内存）。"""
        size = rp.abs.stat().st_size
        if size > self.policy.max_file_bytes:
            raise SandboxViolation(
                "FILE_TOO_LARGE", "sandbox.file_too_large", f"'{rp.rel}' is {size} bytes, limit {self.policy.max_file_bytes}"
            )
        return rp.abs.read_bytes()

    def write_text(self, rp: ResolvedPath, content: str) -> tuple[str | None, str]:
        """原子写入，返回 (写前哈希, 写后哈希)。写之前重新解析一次，防止检查后被换成软链接。"""
        again = self.resolve(rp.raw, access="write")
        if again.abs != rp.abs:
            raise SandboxViolation(
                "SYMLINK_ESCAPE", "sandbox.toctou", f"target of '{rp.raw}' changed between check and write"
            )
        target = rp.abs
        before = file_sha256(target) if target.exists() else None
        mode = target.stat().st_mode & 0o777 if target.exists() else 0o644  # 保留原文件权限
        target.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode("utf-8")
        # 同目录临时文件 + fsync + rename：崩溃时文件要么是旧内容，要么是完整的新内容，不会写一半
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=_TMP_PREFIX)
        try:
            os.write(fd, data)
            os.fsync(fd)
            os.fchmod(fd, mode)
        finally:
            os.close(fd)
        os.replace(tmp, target)  # rename 不跟随目标处的软链接
        return before, hashlib.sha256(data).hexdigest()

    def iter_files(self, base: ResolvedPath) -> Iterator[tuple[str, Path]]:
        """遍历 base 下可读的普通文件：跳过忽略目录、敏感路径、指向工作区外的软链接。"""
        if base.abs.is_file():
            yield base.rel, base.abs
            return
        for dirpath, dirnames, filenames in os.walk(base.abs):
            dirnames[:] = sorted(
                d for d in dirnames
                if d not in self.policy.ignored_dirs and _match_component(d, self.policy.sensitive_patterns) is None
            )
            for name in sorted(filenames):
                full = os.path.join(dirpath, name)
                rel = Path(os.path.relpath(full, self._root_str)).as_posix()
                if name.startswith(_TMP_PREFIX) or _match_component(rel, self.policy.sensitive_patterns):
                    continue
                real = os.path.realpath(full)
                if not _within(real, self._root_str) or not os.path.isfile(real):
                    continue
                yield rel, Path(real)

    def list_tree(self, base: ResolvedPath, *, max_entries: int = 300, max_depth: int = 4) -> tuple[list[str], bool]:
        """列目录（目录名以 / 结尾），隐藏缓存目录和敏感文件，限制深度和条数。返回 (条目, 是否截断)。"""
        entries: list[str] = []
        base_depth = len(base.abs.parts)
        for dirpath, dirnames, filenames in os.walk(base.abs):
            depth = len(Path(dirpath).parts) - base_depth
            dirnames[:] = sorted(
                d for d in dirnames
                if d not in self.policy.ignored_dirs and _match_component(d, self.policy.sensitive_patterns) is None
            )
            rel_dir = Path(os.path.relpath(dirpath, self._root_str)).as_posix()
            for d in dirnames:
                entries.append(f"{d}/" if rel_dir == "." else f"{rel_dir}/{d}/")
            for name in sorted(filenames):
                if name.startswith(_TMP_PREFIX) or _match_component(name, self.policy.sensitive_patterns):
                    continue
                entries.append(name if rel_dir == "." else f"{rel_dir}/{name}")
            if depth >= max_depth:
                dirnames[:] = []
            if len(entries) >= max_entries:
                return sorted(entries)[:max_entries], True
        return sorted(entries), False

    # ---------------- 工作区指纹 ----------------

    def snapshot(self) -> dict[str, str]:
        """相对路径 → sha256。软链接记录指向，不跟随；忽略缓存目录。"""
        files: dict[str, str] = {}
        for dirpath, dirnames, filenames in os.walk(self._root_str):
            dirnames[:] = sorted(d for d in dirnames if d not in self.policy.ignored_dirs)
            for name in filenames:
                if name.startswith(_TMP_PREFIX):
                    continue
                full = os.path.join(dirpath, name)
                rel = Path(os.path.relpath(full, self._root_str)).as_posix()
                if os.path.islink(full):
                    target = os.path.realpath(full)
                    where = os.path.relpath(target, self._root_str) if _within(target, self._root_str) else "<outside>"
                    files[rel] = f"symlink:{where}"
                elif os.path.isfile(full):
                    files[rel] = file_sha256(full)
        return dict(sorted(files.items()))

    def code_files(self, snapshot: dict[str, str]) -> dict[str, str]:
        """从快照里去掉产物目录，剩下的就是“代码”。"""
        return {k: v for k, v in snapshot.items() if self.is_code_path(k)}

    def code_fingerprint(self, snapshot: dict[str, str] | None = None) -> str:
        """代码指纹：测试证据绑定它。任何源码文件变化都会让指纹改变。"""
        return fingerprint(self.code_files(self.snapshot() if snapshot is None else snapshot))

    # ---------------- 命令白名单 ----------------

    def check_command(self, command: Any) -> PreparedCommand:
        """run_test 的命令白名单。只允许 pytest，选项走白名单，路径参数走 resolve()。

        不经过 shell：命令被 shlex 拆成 argv 后直接 exec，并且最终 argv 由这里重新拼装
        （解释器固定为当前 Python，加 -p no:cacheprovider 避免在工作区写缓存）。
        """
        if not isinstance(command, str) or not command.strip():
            raise SandboxViolation("COMMAND_NOT_ALLOWED", "sandbox.command_empty", "command must be a non-empty string")
        meta = sorted({ch for ch in command if ch in _SHELL_META})
        if meta:
            raise SandboxViolation(
                "COMMAND_NOT_ALLOWED",
                "sandbox.shell_metachar",
                f"run_test does not use a shell; found {''.join(meta)!r} in the command",
                command=command,
            )
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            raise SandboxViolation("COMMAND_NOT_ALLOWED", "sandbox.command_parse", f"cannot parse command: {exc}") from exc

        launcher = next((lch for lch in self.policy.test_launchers if tuple(argv[: len(lch)]) == lch), None)
        if launcher is None:
            allowed = ", ".join(" ".join(lch) for lch in self.policy.test_launchers)
            raise SandboxViolation(
                "COMMAND_NOT_ALLOWED",
                "sandbox.command_not_allowed",
                f"'{argv[0]}' is not an allowed command; run_test only runs: {allowed}",
                command=command,
            )

        flags: list[str] = []
        targets: list[str] = []
        selective = False  # 用了 -k 或指定了测试路径 → 只跑了一部分，不算完整测试集
        rest = argv[len(launcher) :]
        i = 0
        while i < len(rest):
            tok = rest[i]
            if tok in self.policy.pytest_flags:
                flags.append(tok)
            elif tok in self.policy.pytest_value_flags:
                if i + 1 >= len(rest):
                    raise SandboxViolation("COMMAND_NOT_ALLOWED", "sandbox.flag_not_allowed", f"{tok} needs a value")
                value = rest[i + 1]
                if tok == "--maxfail" and not value.isdigit():
                    raise SandboxViolation("COMMAND_NOT_ALLOWED", "sandbox.flag_not_allowed", "--maxfail needs a number")
                flags += [tok, value]
                selective = selective or tok == "-k"
                i += 1
            elif tok.startswith("--maxfail=") and tok.split("=", 1)[1].isdigit():
                flags.append(tok)
            elif tok.startswith("-"):  # 不在白名单里的选项：--rootdir、-p、-c 这类都能改变 pytest 的行为
                raise SandboxViolation(
                    "COMMAND_NOT_ALLOWED",
                    "sandbox.flag_not_allowed",
                    f"pytest option {tok!r} is not allowed",
                    command=command,
                )
            else:
                # 测试路径或节点 id（tests/x.py::test_y）：路径部分必须在工作区内且不敏感
                path_part, sep, node = tok.partition("::")
                target = self.resolve(path_part, access="read")
                targets.append(f"{target.rel}{sep}{node}")
                selective = True
            i += 1

        exec_argv = (self.policy.python_executable, "-m", "pytest", "-p", "no:cacheprovider", *flags, *targets)
        display = " ".join(["pytest", *flags, *targets])
        return PreparedCommand(argv=exec_argv, display=display, targets=tuple(targets), full_suite=not selective)

    # ---------------- 子进程 ----------------

    def child_env(self) -> dict[str, str]:
        """子进程只拿到白名单里的环境变量；名字像密钥的一律不传。"""
        env = {
            k: os.environ[k]
            for k in self.policy.env_passthrough
            if k in os.environ and not self._looks_secret(k)
        }
        env.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1", "PYTHONHASHSEED": "0", "NO_COLOR": "1"})
        return env

    def run(self, argv: Sequence[str], *, timeout: float | None = None, cancel: threading.Event | None = None) -> ProcessResult:
        """在工作区里起子进程：独立进程组、干净环境、输出限长；超时或取消时终止整个进程组。

        超时 / 取消时 exit_code 为 None，由调用方（run_test handler）转成 TIMEOUT / CANCELLED 错误。
        """
        limit = min(timeout or self.policy.default_timeout, self.policy.max_timeout)
        start = time.monotonic()
        proc = subprocess.Popen(
            list(argv),
            cwd=self._root_str,
            env=self.child_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # 独立会话 = 独立进程组，pgid == pid
        )
        pgid = proc.pid
        # 单独线程读输出：不读的话子进程写满管道缓冲区会卡住，主线程就没法按时检查超时
        collector = _BoundedCollector(self.policy.max_output_chars)
        reader = threading.Thread(target=collector.drain, args=(proc.stdout,), daemon=True)
        reader.start()

        timed_out = cancelled = False
        try:
            # 每 50ms 醒来一次：看进程是否结束、用户是否取消、是否超时
            while True:
                try:
                    proc.wait(timeout=0.05)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if cancel is not None and cancel.is_set():
                    cancelled = True
                    break
                if time.monotonic() - start > limit:
                    timed_out = True
                    break
        finally:
            # 正常结束、超时、取消、Ctrl-C 都清理整个进程组：测试里遗留的后台孙进程不能活过这次调用。
            _terminate_group(pgid, proc, self.policy.kill_grace)
        reader.join(timeout=2)
        output, truncated = collector.text()
        return ProcessResult(
            argv=tuple(argv),
            exit_code=None if (timed_out or cancelled) else proc.returncode,
            output=self.redact(output),
            truncated=truncated,
            timed_out=timed_out,
            cancelled=cancelled,
            duration=round(time.monotonic() - start, 3),
            pgid=pgid,
        )

    # ---------------- 输出边界 ----------------

    def redact(self, text: str) -> str:
        """把父进程环境里的密钥值从文本中抹掉（文件内容、命令输出、搜索结果都会经过这里）。"""
        for value in self._secret_values:
            text = text.replace(value, "[REDACTED]")
        return text

    def bound(self, text: str, limit: int | None = None) -> tuple[str, bool]:
        """输出边界：先脱敏再限长。"""
        return bound_text(self.redact(text), limit or self.policy.max_output_chars)

    def _looks_secret(self, name: str) -> bool:
        upper = name.upper()
        return any(marker in upper for marker in self.policy.secret_env_markers)


def fingerprint(files: dict[str, str]) -> str:
    """一组文件（路径 → 哈希）的整体指纹，与遍历顺序无关。"""
    digest = hashlib.sha256()
    for rel, value in sorted(files.items()):
        digest.update(f"{rel}\0{value}\n".encode("utf-8"))
    return digest.hexdigest()
