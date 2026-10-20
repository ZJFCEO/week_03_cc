"""审计日志：append-only + 哈希链。

一行一条 JSON（JSONL）。每条记录带上一条的哈希 prev_hash 和自身哈希 hash，
改写、删除、插入任何一行都会让 verify() 失败。

本模块只有 append 和只读查询，没有修改或删除接口；追加时用 O_APPEND + flock，
多个进程同时写也能保证 seq 连续、哈希链不分叉。

常用字段：
    run_id / tool_call_id   还原决策链的两个索引
    event                   gate.decision / sandbox.check / approval.* / exec.* / state.transition ...
    actor                   gate / sandbox / completion / harness / executor / human:<name>
    decision                allow / deny / pending / granted / ok / error ...
    rule                    命中的规则，如 sandbox.path_escape、approval.required
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator

GENESIS_HASH = "0" * 64


class AuditIntegrityError(RuntimeError):
    """审计文件被改写、截断或乱序。"""


@dataclass(frozen=True)
class AuditRecord:
    """一条审计记录（不可变）。"""

    seq: int  # 全文件连续递增
    ts: float
    run_id: str
    event: str
    actor: str
    decision: str | None
    rule: str | None
    tool_call_id: str | None
    tool: str | None
    step: int | None
    details: dict[str, Any]
    prev_hash: str  # 上一条记录的 hash，第一条是 64 个 0
    hash: str  # 本条除 hash 外所有字段的 sha256

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def brief(self) -> str:
        """一行可读摘要，CLI 和运行记录里用。"""
        parts = [f"#{self.seq}", self.event, f"actor={self.actor}"]
        if self.decision:
            parts.append(f"decision={self.decision}")
        if self.rule:
            parts.append(f"rule={self.rule}")
        if self.tool_call_id:
            parts.append(f"call={self.tool_call_id}")
        if self.tool:
            parts.append(f"tool={self.tool}")
        if self.step is not None:
            parts.append(f"step={self.step}")
        return " ".join(parts)


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _read_last_line(fd: int) -> bytes | None:
    """从文件尾部往前找最后一行，避免每次追加都读全文件。"""
    size = os.lseek(fd, 0, os.SEEK_END)
    if size == 0:
        return None
    pos, buf = size, b""
    while pos > 0:
        step = min(4096, pos)
        pos -= step
        buf = os.pread(fd, step, pos) + buf
        body = buf.rstrip(b"\n")
        idx = body.rfind(b"\n")
        if idx != -1:
            return body[idx + 1 :]
    return buf.rstrip(b"\n") or None


class AuditLog:
    """审计日志文件的读写入口：只能追加和查询。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        self._lock = threading.Lock()

    def append(
        self,
        *,
        run_id: str,
        event: str,
        actor: str,
        decision: str | None = None,
        rule: str | None = None,
        tool_call_id: str | None = None,
        tool: str | None = None,
        step: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditRecord:
        """追加一条记录并 fsync，返回写入的记录。"""
        # 先过一遍 JSON，保证写入的内容和事后重算哈希时读到的内容一致（Path 等对象转成字符串）。
        clean_details = json.loads(json.dumps(details or {}, ensure_ascii=False, default=str))
        with self._lock:
            fd = os.open(self.path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o644)
            try:
                # 跨进程互斥：seq 和 prev_hash 必须基于文件里真正的最后一行。
                fcntl.flock(fd, fcntl.LOCK_EX)
                last = _read_last_line(fd)
                if last:
                    prev = json.loads(last)
                    seq, prev_hash = prev["seq"] + 1, prev["hash"]
                else:
                    seq, prev_hash = 1, GENESIS_HASH
                body = {
                    "seq": seq,
                    "ts": time.time(),
                    "run_id": run_id,
                    "event": event,
                    "actor": actor,
                    "decision": decision,
                    "rule": rule,
                    "tool_call_id": tool_call_id,
                    "tool": tool,
                    "step": step,
                    "details": clean_details,
                    "prev_hash": prev_hash,
                }
                record = AuditRecord(**body, hash=_digest(body))
                os.write(fd, (_canonical(record.to_dict()) + "\n").encode("utf-8"))
                os.fsync(fd)
                return record
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    # ---- 只读查询 ----

    def _iter(self) -> Iterator[AuditRecord]:
        """逐行读出全部记录；解析不了的行视为文件被破坏。"""
        with self.path.open("r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                if not line.strip():
                    continue
                try:
                    yield AuditRecord(**json.loads(line))
                except (json.JSONDecodeError, TypeError) as exc:
                    raise AuditIntegrityError(f"line {lineno} is not a valid audit record: {exc}") from exc

    def records(
        self,
        *,
        run_id: str | None = None,
        tool_call_id: str | None = None,
        event_prefix: str | None = None,
    ) -> list[AuditRecord]:
        """按 run_id / tool_call_id / 事件前缀过滤，保持写入顺序。"""
        out = []
        for rec in self._iter():
            if run_id is not None and rec.run_id != run_id:
                continue
            if tool_call_id is not None and rec.tool_call_id != tool_call_id:
                continue
            if event_prefix is not None and not rec.event.startswith(event_prefix):
                continue
            out.append(rec)
        return out

    def decision_chain(self, run_id: str, tool_call_id: str) -> list[AuditRecord]:
        """一次工具调用从进入 Gate 到产生结果的全部记录，按 seq 排序。"""
        return self.records(run_id=run_id, tool_call_id=tool_call_id)

    def verify(self) -> int:
        """校验 seq 连续与哈希链完整，返回记录条数；发现篡改抛 AuditIntegrityError。"""
        prev_hash, expected_seq = GENESIS_HASH, 1
        for rec in self._iter():
            body = rec.to_dict()
            stored = body.pop("hash")
            if rec.seq != expected_seq:
                raise AuditIntegrityError(f"seq {rec.seq} found where {expected_seq} was expected")
            if rec.prev_hash != prev_hash:
                raise AuditIntegrityError(f"record #{rec.seq} does not link to the previous record")
            if _digest(body) != stored:
                raise AuditIntegrityError(f"record #{rec.seq} content does not match its hash")
            prev_hash, expected_seq = stored, expected_seq + 1
        return expected_seq - 1
