"""离线替身模型：按脚本逐轮返回 tool call，不依赖任何真实模型。

脚本是一个 turn 列表，每个 turn 是：
    {"calls": [{"id": "c1", "name": "read_file", "arguments": {...}}, ...], "thought": "...", "tokens": [in, out]}
也可以是 {"interrupt": true}：模拟用户在等待模型时按下 Ctrl-C（抛 KeyboardInterrupt）。
turn 还可以是函数 f(messages) -> turn，用来根据上一步的观察结果动态决定下一步。

跨进程测试用 JSON 文件描述脚本（ScriptedModel.from_file），并可把每次收到的 prompt
追加写到 log_path，供测试断言“恢复后的模型确实看到了之前的对话”。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from smolagents import Model
from smolagents.models import ChatMessage, ChatMessageToolCall, ChatMessageToolCallFunction, MessageRole
from smolagents.monitoring import TokenUsage

Turn = dict[str, Any] | Callable[[list[ChatMessage]], dict[str, Any]]


class ScriptExhausted(RuntimeError):
    """脚本用完了还被调用：smolagents 会把它包装成 AgentGenerationError，Harness 记为 MODEL_ERROR。"""


def call(name: str, call_id: str, **arguments: Any) -> dict[str, Any]:
    """构造一个 tool call：call("read_file", "c1", path="README.md")。"""
    return {"id": call_id, "name": name, "arguments": arguments}


def turn(*calls: dict[str, Any], thought: str = "", tokens: tuple[int, int] = (120, 30)) -> dict[str, Any]:
    """构造模型的一轮输出：一个或多个 tool call，外加这一轮的 token 用量（预算测试用）。"""
    return {"calls": list(calls), "thought": thought, "tokens": list(tokens)}


def flatten(messages: list[ChatMessage | dict]) -> str:
    """把 smolagents 发给模型的消息列表拍平成纯文本，方便断言“模型看到了什么”。"""
    parts = []
    for msg in messages:
        content = msg.content if isinstance(msg, ChatMessage) else msg.get("content")
        if isinstance(content, list):
            parts.extend(str(block.get("text", "")) for block in content if isinstance(block, dict))
        elif content:
            parts.append(str(content))
    return "\n".join(parts)


class ScriptedModel(Model):
    """实现 smolagents Model 接口的假模型：每次 generate() 弹出脚本里的下一轮。

    requests 记录每次收到的完整消息列表，测试据此检查结构化错误、恢复说明是否真的送到了模型面前。
    """

    def __init__(self, turns: list[Turn], *, model_id: str = "scripted-fake", log_path: Path | None = None):
        super().__init__(model_id=model_id)
        self.turns = list(turns)
        self.requests: list[list[ChatMessage]] = []
        self.log_path = Path(log_path) if log_path else None

    @classmethod
    def from_file(cls, path: Path | str, *, log_path: Path | None = None) -> "ScriptedModel":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(data["turns"], model_id=data.get("model_id", "scripted-fake"), log_path=log_path)

    @property
    def calls_made(self) -> int:
        return len(self.requests)

    def last_prompt(self) -> str:
        return flatten(self.requests[-1]) if self.requests else ""

    def generate(
        self,
        messages: list[ChatMessage],
        stop_sequences: list[str] | None = None,
        response_format: dict[str, str] | None = None,
        tools_to_call_from: list | None = None,
        **kwargs: Any,
    ) -> ChatMessage:
        self.requests.append(messages)
        if self.log_path is not None:  # 跨进程测试：子进程里的 prompt 写到文件，父进程读出来断言
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"request": len(self.requests), "prompt": flatten(messages)}, ensure_ascii=False) + "\n")
        if not self.turns:
            raise ScriptExhausted("the fake model script has no more turns")
        spec = self.turns.pop(0)
        if callable(spec):
            spec = spec(messages)
        if spec.get("interrupt"):  # 模拟用户在等模型回复时按 Ctrl-C
            raise KeyboardInterrupt("scripted Ctrl-C while waiting for the model")
        tokens = spec.get("tokens") or [120, 30]
        tool_calls = [
            ChatMessageToolCall(
                id=c["id"],
                type="function",
                function=ChatMessageToolCallFunction(name=c["name"], arguments=c.get("arguments", {})),
            )
            for c in spec.get("calls", [])
        ]
        return ChatMessage(
            role=MessageRole.ASSISTANT,
            content=spec.get("thought", ""),
            tool_calls=tool_calls or None,
            token_usage=TokenUsage(input_tokens=tokens[0], output_tokens=tokens[1]),
        )
