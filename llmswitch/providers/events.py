"""Building Anthropic Messages responses, for providers that translate from another protocol.

A translating provider describes its answer as a sequence of content blocks: open a
block, feed it deltas, close it. Two writers take that description. ``MessageStream``
turns it into the server-sent events a streaming client expects; ``MessageBuilder``
folds it into the content list of a complete message. Feeding both from the same
code keeps streamed and non-streamed answers identical block for block.
"""

from __future__ import annotations

import json
import secrets
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import httpx


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(12)}"


def sse(event: str, payload: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


def stream_error(message: str, err_type: str = "api_error") -> bytes:
    """The event an Anthropic stream carries when it fails after it has started."""
    return sse("error", {"type": "error", "error": {"type": err_type, "message": message}})


def anthropic_message(
    message_id: str,
    model: str,
    content: list[dict[str, Any]],
    stop_reason: str | None,
    usage: dict[str, int],
) -> dict[str, Any]:
    return {
        "id": message_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": usage,
    }


async def iter_sse_json(resp: httpx.Response) -> AsyncIterator[Any]:
    """The JSON payload of every ``data:`` line of an upstream event stream, up to ``[DONE]``."""
    async for line in resp.aiter_lines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            return
        try:
            yield json.loads(data)
        except ValueError:
            continue


class BlockWriter(Protocol):
    """Where a translator sends content blocks. Every call returns the events to emit, if any."""

    open_type: str | None

    def open(self, block: dict[str, Any]) -> list[bytes]: ...

    def delta(self, delta: dict[str, Any]) -> list[bytes]: ...

    def close(self) -> list[bytes]: ...


class MessageStream:
    """The Anthropic streaming event sequence, built one content block at a time.

    Blocks are numbered in the order they open, and each one is closed before the next
    one opens, which is the order Anthropic clients expect.
    """

    def __init__(self, model: str, message_id: str | None = None) -> None:
        self.model = model
        self.message_id = message_id or new_id("msg")
        self.opened = 0  # blocks opened so far; the open one, if any, is number opened - 1
        self.open_type: str | None = None

    @property
    def index(self) -> int:
        return self.opened - 1

    def start(self) -> bytes:
        message = anthropic_message(self.message_id, self.model, [], None, {"input_tokens": 0, "output_tokens": 0})
        return sse("message_start", {"type": "message_start", "message": message})

    def open(self, block: dict[str, Any]) -> list[bytes]:
        events = self.close()
        self.open_type = block["type"]
        events.append(
            sse("content_block_start", {"type": "content_block_start", "index": self.opened, "content_block": block})
        )
        self.opened += 1
        return events

    def delta(self, delta: dict[str, Any], index: int | None = None) -> list[bytes]:
        idx = self.index if index is None else index
        return [sse("content_block_delta", {"type": "content_block_delta", "index": idx, "delta": delta})]

    def close(self) -> list[bytes]:
        if self.open_type is None:
            return []
        self.open_type = None
        return [sse("content_block_stop", {"type": "content_block_stop", "index": self.index})]

    def finish(self, stop_reason: str, usage: dict[str, int]) -> list[bytes]:
        events = self.close()
        events.append(
            sse(
                "message_delta",
                {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None}, "usage": usage},
            )
        )
        events.append(sse("message_stop", {"type": "message_stop"}))
        return events


class MessageBuilder:
    """The same block sequence as ``MessageStream``, folded into a message's ``content`` list."""

    def __init__(self) -> None:
        self.content: list[dict[str, Any]] = []
        self.open_type: str | None = None
        self._json: list[str] = []

    def open(self, block: dict[str, Any]) -> list[bytes]:
        self.close()
        self.content.append(dict(block))
        self.open_type = block["type"]
        return []

    def delta(self, delta: dict[str, Any]) -> list[bytes]:
        block = self.content[-1]
        kind = delta.get("type")
        if kind == "text_delta":
            block["text"] = block.get("text", "") + delta.get("text", "")
        elif kind == "thinking_delta":
            block["thinking"] = block.get("thinking", "") + delta.get("thinking", "")
        elif kind == "signature_delta":
            block["signature"] = delta.get("signature", "")
        elif kind == "input_json_delta":
            self._json.append(delta.get("partial_json", ""))
        return []

    def close(self) -> list[bytes]:
        if self.open_type == "tool_use" and self._json:
            self.content[-1]["input"] = json.loads("".join(self._json))
        self.open_type = None
        self._json = []
        return []
