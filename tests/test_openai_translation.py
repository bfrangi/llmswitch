from __future__ import annotations

import json

from hub.providers.openai import StreamTranslator, anthropic_to_openai, openai_to_anthropic

REQUEST = {
    "model": "x",
    "max_tokens": 100,
    "temperature": 0.2,
    "stop_sequences": ["END"],
    "system": [
        {"type": "text", "text": "sys A", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "sys B"},
    ],
    "metadata": {"user_id": "u1"},
    "thinking": {"type": "adaptive"},
    "context_management": {"edits": []},
    "tools": [
        {
            "name": "read",
            "description": "read a file",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
            "strict": True,
        },
        {"type": "web_search_20260209", "name": "web_search"},
    ],
    "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},
    "messages": [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "look"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
            ],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "hmm", "signature": "s"},
                {"type": "text", "text": "reading"},
                {"type": "tool_use", "id": "toolu_1", "name": "read", "input": {"path": "a.py"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": [{"type": "text", "text": "print(1)"}]},
                {"type": "text", "text": "and now?"},
            ],
        },
    ],
}


def test_request_translation() -> None:
    out = anthropic_to_openai(REQUEST, "upstream-model")
    assert out["model"] == "upstream-model"
    assert out["max_tokens"] == 100 and out["temperature"] == 0.2 and out["stop"] == ["END"] and out["user"] == "u1"
    assert "thinking" not in out and "context_management" not in out and "stream" not in out
    msgs = out["messages"]
    assert msgs[0] == {"role": "system", "content": "sys A\n\nsys B"}
    assert msgs[1]["role"] == "user" and msgs[1]["content"][0] == {"type": "text", "text": "look"}
    assert msgs[1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert msgs[2]["role"] == "assistant" and msgs[2]["content"] == "reading"
    assert msgs[2]["tool_calls"] == [
        {"id": "toolu_1", "type": "function", "function": {"name": "read", "arguments": '{"path": "a.py"}'}}
    ]
    assert msgs[3] == {"role": "tool", "tool_call_id": "toolu_1", "content": "print(1)"}
    assert msgs[4] == {"role": "user", "content": "and now?"}
    assert out["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "read",
                "description": "read a file",
                "parameters": REQUEST["tools"][0]["input_schema"],
            },
        }
    ]
    assert out["tool_choice"] == "auto" and out["parallel_tool_calls"] is False


def test_tool_choice_variants() -> None:
    base = {"messages": [{"role": "user", "content": "x"}], "tools": [{"name": "t", "input_schema": {}}]}
    assert anthropic_to_openai({**base, "tool_choice": {"type": "any"}}, "m")["tool_choice"] == "required"
    assert anthropic_to_openai({**base, "tool_choice": {"type": "tool", "name": "t"}}, "m")["tool_choice"] == {
        "type": "function",
        "function": {"name": "t"},
    }
    assert anthropic_to_openai({**base, "tool_choice": {"type": "none"}}, "m")["tool_choice"] == "none"
    assert "tool_choice" not in anthropic_to_openai({"messages": [], "tool_choice": {"type": "any"}}, "m")


def test_stream_flag_adds_usage_option() -> None:
    out = anthropic_to_openai({"messages": [], "stream": True}, "m")
    assert out["stream"] is True and out["stream_options"] == {"include_usage": True}


def test_response_translation() -> None:
    data = {
        "id": "chatcmpl-9",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": "I will read it",
                    "reasoning_content": "thinking...",
                    "tool_calls": [
                        {"id": "call_1", "function": {"name": "read", "arguments": '{"path": "a.py"}'}},
                        {"function": {"name": "noop", "arguments": "not json"}},
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4},
    }
    msg = openai_to_anthropic(data, "hub/model")
    assert msg["id"] == "chatcmpl-9" and msg["model"] == "hub/model" and msg["stop_reason"] == "tool_use"
    assert msg["content"][0] == {"type": "thinking", "thinking": "thinking...", "signature": ""}
    assert msg["content"][1] == {"type": "text", "text": "I will read it"}
    assert msg["content"][2] == {"type": "tool_use", "id": "call_1", "name": "read", "input": {"path": "a.py"}}
    assert msg["content"][3]["input"] == {"_raw_arguments": "not json"} and msg["content"][3]["id"].startswith("toolu_")
    assert msg["usage"] == {"input_tokens": 3, "output_tokens": 4}
    assert (
        openai_to_anthropic({"choices": [{"finish_reason": "length", "message": {"content": "x"}}]}, "m")["stop_reason"]
        == "max_tokens"
    )


def events_of(chunks: list[bytes]) -> list[dict]:
    return [
        json.loads(line[5:]) for chunk in chunks for line in chunk.decode().splitlines() if line.startswith("data:")
    ]


def test_stream_translator_text_then_argless_tool() -> None:
    tr = StreamTranslator("m")
    out = [tr.start()]
    out += tr.feed({"choices": [{"delta": {"reasoning_content": "let me"}}]})
    out += tr.feed({"choices": [{"delta": {"content": "Hi"}}]})
    out += tr.feed({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "ls"}}]}}]})
    out += tr.feed(
        {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 1, "completion_tokens": 2}}
    )
    out += tr.finish()
    ev = events_of(out)
    kinds = [(e["type"], e.get("index")) for e in ev]
    assert kinds == [
        ("message_start", None),
        ("content_block_start", 0),
        ("content_block_delta", 0),
        ("content_block_stop", 0),
        ("content_block_start", 1),
        ("content_block_delta", 1),
        ("content_block_stop", 1),
        ("content_block_start", 2),
        ("content_block_delta", 2),
        ("content_block_stop", 2),
        ("message_delta", None),
        ("message_stop", None),
    ]
    assert ev[1]["content_block"]["type"] == "thinking" and ev[2]["delta"]["thinking"] == "let me"
    assert ev[8]["delta"] == {"type": "input_json_delta", "partial_json": "{}"}, (
        "tool call without arguments still yields valid JSON"
    )
    assert ev[10]["delta"]["stop_reason"] == "tool_use" and ev[10]["usage"] == {"input_tokens": 1, "output_tokens": 2}
    for chunk in out:
        assert chunk.startswith(b"event: ") and chunk.endswith(b"\n\n")
