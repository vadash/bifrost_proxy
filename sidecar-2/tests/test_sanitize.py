"""Tests for sidecar-2 sanitize — the three Claude-family body rewriters.

These guard the observable contract: empty-thinking-block stripping,
``reasoning_effort`` -> Bedrock-native thinking shape, and
``max_completion_tokens`` -> ``max_tokens`` mirroring must fire exactly when
intended, and no other payload shape may be mutated.
"""

from __future__ import annotations

import copy
import importlib
import json
import unittest

# ``sidecar-2`` is not a valid ``import`` statement identifier (hyphen), so
# load the module via importlib.
_sanitize = importlib.import_module("sidecar-2.sanitize")
cap_max_tokens = _sanitize.cap_max_tokens
completion_to_sse = _sanitize.completion_to_sse
downgrade_stream = _sanitize.downgrade_stream
mirror_max_tokens = _sanitize.mirror_max_tokens
model_needs_sanitize = _sanitize.model_needs_sanitize
model_needs_stream_downgrade = _sanitize.model_needs_stream_downgrade
rewrite_reasoning_effort = _sanitize.rewrite_reasoning_effort
sanitize_request_body = _sanitize.sanitize_request_body
strip_empty_thinking = _sanitize.strip_empty_thinking


def _msg(role, content):
    return {"role": role, "content": content}


def _thinking(text, **extra):
    return {"type": "thinking", "thinking": text, **extra}


def _text(t):
    return {"type": "text", "text": t}


class TestModelNeedsSanitize(unittest.TestCase):
    """``model_needs_sanitize`` — case-insensitive claude/sonnet/opus match."""

    def test_claude_family_matches(self):
        for m in (
            "claude-opus-4-8",
            "claude-sonnet-4-6",
            "anthropic/claude-3-7-sonnet",
            "bedrock/us.anthropic.claude-opus-4-1",
            "OPUS",
            "SoNnEt-2025",
        ):
            with self.subTest(model=m):
                self.assertTrue(model_needs_sanitize(m))

    def test_non_claude_passes(self):
        for m in ("z-ai/glm-5.2", "gpt-5", "gemini-3-pro", "deepseek-v4", "glm"):
            with self.subTest(model=m):
                self.assertFalse(model_needs_sanitize(m))

    def test_non_string_never_matches(self):
        for m in (None, 42, [], {}):
            with self.subTest(model=m):
                self.assertFalse(model_needs_sanitize(m))


class TestSanitizeClaudeRequest(unittest.TestCase):
    """``strip_empty_thinking`` — strip empty thinking, keep everything else."""

    def test_strips_empty_thinking_keeps_signature(self):
        """The exact failure from the 400 log: ``thinking: ""`` + signature."""
        body = {
            "model": "claude-opus-4-8",
            "messages": [
                _msg("user", [_text("hi")]),
                _msg(
                    "assistant",
                    [
                        _thinking("", signature="EpEECokBCBAYAipA"),
                        _text("answer"),
                        {"type": "tool_use", "id": "t1", "name": "_read", "input": {}},
                    ],
                ),
            ],
        }
        removed = strip_empty_thinking(body)
        self.assertEqual(removed, 1)
        blocks = body["messages"][1]["content"]
        self.assertEqual([b["type"] for b in blocks], ["text", "tool_use"])

    def test_strips_missing_and_whitespace_thinking(self):
        body = {
            "messages": [
                _msg("assistant", [
                    {"type": "thinking"},                    # missing field
                    _thinking("   "),                        # whitespace-only
                    _thinking(None),                         # null
                    _text("kept"),
                ]),
            ],
        }
        removed = strip_empty_thinking(body)
        self.assertEqual(removed, 3)
        self.assertEqual(body["messages"][0]["content"], [_text("kept")])

    def test_keeps_thinking_with_real_text(self):
        body = {
            "messages": [
                _msg("assistant", [
                    _thinking("let me think about this", signature="sig"),
                    _text("answer"),
                ]),
            ],
        }
        removed = strip_empty_thinking(body)
        self.assertEqual(removed, 0)
        self.assertEqual(len(body["messages"][0]["content"]), 2)

    def test_drops_assistant_message_left_with_empty_content(self):
        """content: [] would itself be a 400 — drop the whole turn."""
        body = {
            "messages": [
                _msg("user", [_text("hi")]),
                _msg("assistant", [_thinking("")]),
                _msg("user", [_text("again")]),
            ],
        }
        removed = strip_empty_thinking(body)
        self.assertEqual(removed, 1)
        self.assertEqual([m["role"] for m in body["messages"]], ["user", "user"])

    def test_clean_body_untouched_and_reports_zero(self):
        body = {
            "model": "claude-opus-4-8",
            "messages": [
                _msg("user", [_text("hi")]),
                _msg("assistant", [_text("hello"), _thinking("real reasoning")]),
                _msg("user", [
                    {"type": "tool_result", "tool_use_id": "t1",
                     "content": [_text("ok")], "is_error": False},
                ]),
            ],
            "thinking": {"type": "enabled", "budget_tokens": 10000},
        }
        before = copy.deepcopy(body)
        removed = strip_empty_thinking(body)
        self.assertEqual(removed, 0)
        self.assertEqual(body, before)  # zero mutation on clean payloads

    def test_user_messages_never_touched(self):
        body = {"messages": [_msg("user", [_thinking("")])]}
        removed = strip_empty_thinking(body)
        self.assertEqual(removed, 0)
        self.assertEqual(body["messages"][0]["content"], [_thinking("")])

    def test_non_list_inputs_noop(self):
        self.assertEqual(strip_empty_thinking({}), 0)
        self.assertEqual(strip_empty_thinking({"messages": None}), 0)
        self.assertEqual(strip_empty_thinking({"messages": "nope"}), 0)
        # string-content assistant messages are legal and pass through
        body = {"messages": [_msg("assistant", "plain string content")]}
        self.assertEqual(strip_empty_thinking(body), 0)
        self.assertEqual(body["messages"][0]["content"], "plain string content")

    def test_interleaved_role_alternation_preserved(self):
        """Stripping must not create adjacent same-role turns (API rejects those)."""
        body = {
            "messages": [
                _msg("user", [_text("1")]),
                _msg("assistant", [_thinking(""), _text("a")]),
                _msg("user", [_text("2")]),
                _msg("assistant", [_thinking("")]),          # drops entirely
                _msg("user", [_text("3")]),
                _msg("assistant", [_thinking(""), _text("b")]),
            ],
        }
        removed = strip_empty_thinking(body)
        self.assertEqual(removed, 3)
        roles = [m["role"] for m in body["messages"]]
        self.assertEqual(roles, ["user", "assistant", "user", "user", "assistant"])
        # remaining assistants kept their text
        self.assertEqual(body["messages"][1]["content"], [_text("a")])
        self.assertEqual(body["messages"][4]["content"], [_text("b")])


class TestRewriteReasoningEffort(unittest.TestCase):
    """``rewrite_reasoning_effort`` — OpenAI -> Bedrock-native shape."""

    def test_rewrites_medium_to_adaptive_plus_effort(self):
        """The exact failure from the 400 log: reasoning_effort=medium, no thinking."""
        body = {
            "model": "agentrouter-01/claude-opus-4-8",
            "messages": [{"role": "user", "content": "hi"}],
            "reasoning_effort": "medium",
            "max_completion_tokens": 64000,
        }
        self.assertTrue(rewrite_reasoning_effort(body))
        self.assertNotIn("reasoning_effort", body)
        self.assertEqual(body["thinking"], {"adaptive": True})
        self.assertEqual(body["output_config"], {"effort": "medium"})
        # untouched fields stay untouched
        self.assertEqual(body["max_completion_tokens"], 64000)

    def test_preserves_existing_output_config_keys(self):
        body = {"reasoning_effort": "high", "output_config": {"foo": "bar"}}
        self.assertTrue(rewrite_reasoning_effort(body))
        self.assertEqual(body["output_config"], {"foo": "bar", "effort": "high"})

    def test_skips_when_explicit_thinking_present(self):
        """Anthropic-format request with explicit thinking is left alone."""
        body = {
            "reasoning_effort": "medium",
            "thinking": {"type": "enabled", "budget_tokens": 10000},
        }
        before = copy.deepcopy(body)
        self.assertFalse(rewrite_reasoning_effort(body))
        self.assertEqual(body, before)

    def test_skips_when_no_reasoning_effort(self):
        body = {"model": "claude-opus-4-8", "messages": []}
        before = copy.deepcopy(body)
        self.assertFalse(rewrite_reasoning_effort(body))
        self.assertEqual(body, before)

    def test_skips_non_string_effort(self):
        for bad in (None, 5, True, ["medium"]):
            body = {"reasoning_effort": bad}
            with self.subTest(value=bad):
                self.assertFalse(rewrite_reasoning_effort(body))
                self.assertEqual(body["reasoning_effort"], bad)

    def test_skips_when_explicit_thinking_empty_dict(self):
        """An explicit empty ``thinking: {}`` is a deliberate client config -> forward as-is."""
        body = {
            "model": "claude-sonnet-4",
            "reasoning_effort": "high",
            "thinking": {},
        }
        before = copy.deepcopy(body)
        self.assertFalse(rewrite_reasoning_effort(body))
        self.assertEqual(body, before)
        self.assertIn("reasoning_effort", body)

    def test_still_skips_when_explicit_thinking_nonempty(self):
        """Non-empty explicit thinking dict is likewise left untouched (locks both gate sides)."""
        body = {
            "model": "claude-sonnet-4",
            "reasoning_effort": "high",
            "thinking": {"adaptive": True},
        }
        before = copy.deepcopy(body)
        self.assertFalse(rewrite_reasoning_effort(body))
        self.assertEqual(body, before)


class TestInjectBedrockMaxTokens(unittest.TestCase):
    """``mirror_max_tokens`` — mirror max_completion_tokens -> max_tokens."""

    def test_mirrors_value(self):
        """The 8192-cap failure: client sent 64000, Bedrock needs max_tokens."""
        body = {"max_completion_tokens": 64000}
        self.assertTrue(mirror_max_tokens(body))
        self.assertEqual(body["max_tokens"], 64000)
        # original field preserved — agentrouter may still consult it
        self.assertEqual(body["max_completion_tokens"], 64000)

    def test_skips_when_max_tokens_already_set(self):
        """Anthropic-format request with explicit max_tokens is left alone."""
        body = {"max_completion_tokens": 64000, "max_tokens": 4096}
        before = copy.deepcopy(body)
        self.assertFalse(mirror_max_tokens(body))
        self.assertEqual(body, before)

    def test_skips_when_no_max_completion_tokens(self):
        body = {"model": "claude-opus-4-8", "messages": []}
        before = copy.deepcopy(body)
        self.assertFalse(mirror_max_tokens(body))
        self.assertEqual(body, before)

    def test_skips_non_int_values(self):
        for bad in (None, "64000", 64000.0, True):
            body = {"max_completion_tokens": bad}
            with self.subTest(value=bad):
                self.assertFalse(mirror_max_tokens(body))
                self.assertNotIn("max_tokens", body)


class TestCapMaxTokens(unittest.TestCase):
    """``cap_max_tokens`` — unconditional ceiling on the output-token budget.

    Both ``max_tokens`` (Anthropic) and ``max_completion_tokens`` (OpenAI) are
    forced to ``_MAX_TOKENS_CAP`` whenever the value is missing or exceeds the
    cap. Applies to every request regardless of model/provider.
    """

    def test_sets_both_when_missing(self):
        body = {"model": "gpt-5", "messages": []}
        self.assertTrue(cap_max_tokens(body))
        self.assertEqual(body["max_tokens"], 16000)
        self.assertEqual(body["max_completion_tokens"], 16000)

    def test_clamps_oversize_to_cap(self):
        # The pathological case: client asked for 64000 output tokens.
        body = {"model": "claude-opus-4-8", "max_tokens": 64000}
        self.assertTrue(cap_max_tokens(body))
        self.assertEqual(body["max_tokens"], 16000)
        # Missing OpenAI field is also filled to the cap.
        self.assertEqual(body["max_completion_tokens"], 16000)

    def test_clamps_openai_oversize_only(self):
        body = {"model": "gpt-5", "max_completion_tokens": 100000}
        self.assertTrue(cap_max_tokens(body))
        self.assertEqual(body["max_completion_tokens"], 16000)
        # Missing Anthropic field filled to cap.
        self.assertEqual(body["max_tokens"], 16000)

    def test_preserves_below_cap_values(self):
        body = {"max_tokens": 4096, "max_completion_tokens": 8192}
        before = copy.deepcopy(body)
        self.assertFalse(cap_max_tokens(body))
        self.assertEqual(body, before)

    def test_preserves_one_below_one_missing(self):
        # An explicit below-cap value stays; only the missing field is filled.
        body = {"max_tokens": 4096}
        self.assertTrue(cap_max_tokens(body))
        self.assertEqual(body["max_tokens"], 4096)
        self.assertEqual(body["max_completion_tokens"], 16000)

    def test_clamps_above_cap_keeps_field_below_cap(self):
        body = {"max_tokens": 64000, "max_completion_tokens": 4096}
        self.assertTrue(cap_max_tokens(body))
        self.assertEqual(body["max_tokens"], 16000)
        self.assertEqual(body["max_completion_tokens"], 4096)

    def test_non_int_values_treated_as_missing(self):
        for bad in (None, "16000", 16000.0, True):
            body = {"max_tokens": bad, "model": "glm-5.2"}
            with self.subTest(value=bad):
                self.assertTrue(cap_max_tokens(body))
                self.assertEqual(body["max_tokens"], 16000)
                self.assertEqual(body["max_completion_tokens"], 16000)

    def test_exactly_at_cap_is_left_untouched(self):
        body = {"max_tokens": 16000, "max_completion_tokens": 16000}
        before = copy.deepcopy(body)
        self.assertFalse(cap_max_tokens(body))
        self.assertEqual(body, before)

    def test_not_a_dict_is_noop(self):
        for bad in (None, "raw-body", 42, [], b"x"):
            self.assertFalse(cap_max_tokens(bad))


class TestSanitizeRequestBody(unittest.TestCase):
    """``sanitize_request_body`` — orchestration of all three rewriters.

    Pins the ``None``-vs-``bytes`` contract the proxy relies on: a body
    unchanged by any rewrite stays byte-verbatim in the caller (returns
    ``None``), and a body touched by any rewrite round-trips through
    ``json.loads`` to the same shape, re-encoded.
    """

    def test_clean_claude_body_returns_none(self):
        # No empty thinking, no reasoning_effort, max_tokens already set.
        body = {
            "model": "claude-opus-4-8",
            "messages": [
                _msg("user", [_text("hi")]),
            ],
            "max_tokens": 1024,
        }
        self.assertIsNone(sanitize_request_body(body))
        # Body is untouched (no mutation when returning None).
        self.assertEqual(body["model"], "claude-opus-4-8")
        self.assertEqual(body["messages"], [_msg("user", [_text("hi")])])
        self.assertEqual(body["max_tokens"], 1024)

    def test_empty_thinking_block_returns_bytes(self):
        body = {
            "model": "claude-opus-4-8",
            "messages": [
                _msg("assistant", [_thinking(""), _text("answer")]),
                _msg("user", [_text("again")]),
            ],
        }
        out = sanitize_request_body(body)
        self.assertIsNotNone(out)
        decoded = json.loads(out)
        # The empty thinking block was dropped, text block preserved.
        self.assertEqual(decoded["model"], "claude-opus-4-8")
        self.assertEqual(
            decoded["messages"],
            [
                _msg("assistant", [_text("answer")]),
                _msg("user", [_text("again")]),
            ],
        )

    def test_reasoning_effort_rewrite_returns_bytes(self):
        body = {
            "model": "claude-opus-4-8",
            "messages": [_msg("user", [_text("hi")])],
            "reasoning_effort": "high",
        }
        out = sanitize_request_body(body)
        self.assertIsNotNone(out)
        decoded = json.loads(out)
        self.assertNotIn("reasoning_effort", decoded)
        self.assertEqual(decoded["thinking"], {"adaptive": True})
        self.assertEqual(decoded["output_config"], {"effort": "high"})

    def test_max_completion_tokens_mirrored_returns_bytes(self):
        body = {
            "model": "claude-opus-4-8",
            "messages": [_msg("user", [_text("hi")])],
            "max_completion_tokens": 4096,
        }
        out = sanitize_request_body(body)
        self.assertIsNotNone(out)
        decoded = json.loads(out)
        self.assertEqual(decoded["max_tokens"], 4096)
        self.assertEqual(decoded["max_completion_tokens"], 4096)

    def test_non_claude_model_returns_none_unchanged(self):
        # The model gate fires first regardless of body content; an OpenAI
        # body with reasoning_effort + max_completion_tokens is forwarded
        # verbatim (passthrough is upstream's problem, not the sidecar's).
        body = {
            "model": "gpt-5",
            "messages": [_msg("user", [_text("hi")])],
            "reasoning_effort": "high",
            "max_completion_tokens": 8192,
        }
        self.assertIsNone(sanitize_request_body(body))
        self.assertEqual(body["model"], "gpt-5")
        self.assertEqual(body["reasoning_effort"], "high")
        self.assertNotIn("max_tokens", body)

    def test_dict_without_model_returns_none(self):
        # A parsed dict with no ``model`` key can't match the sanitize gate;
        # the orchestrator must short-circuit to ``None`` rather than raise.
        self.assertIsNone(sanitize_request_body({}))
        self.assertIsNone(sanitize_request_body({"messages": []}))


class TestStreamDowngrade(unittest.TestCase):
    """``downgrade_stream`` — vision-exp stream:true forced to false.

    Guards the workaround for the vision-exp preview server's streaming
    tool-call parser bug (intermittently double-wraps accumulated tool-call
    arguments as ``{"arguments": {...}}``); non-streaming responses are
    clean, so the proxy downgrades and replays SSE.
    """

    def test_vision_exp_stream_downgraded(self):
        for m in ("DeepSeek-V4-Flash-Vision-Exp",
                  "amd0/DeepSeek-V4-Flash-Vision-Exp",
                  "deepseek-v4-flash-vision-exp"):
            with self.subTest(model=m):
                body = {"model": m, "stream": True,
                        "stream_options": {"include_usage": True},
                        "messages": [_msg("user", "hi")]}
                self.assertTrue(downgrade_stream(body))
                self.assertFalse(body["stream"])
                self.assertNotIn("stream_options", body)

    def test_other_models_untouched(self):
        # DeepSeek-V4-Flash-0731 (the fixed sibling) must keep streaming.
        for m in ("DeepSeek-V4-Flash-0731", "deepseek-v4", "gpt-5",
                  "claude-opus-4-8", "z-ai/glm-5.2"):
            with self.subTest(model=m):
                body = {"model": m, "stream": True}
                self.assertFalse(downgrade_stream(body))
                self.assertTrue(body["stream"])

    def test_non_stream_noop(self):
        for body in ({"model": "DeepSeek-V4-Flash-Vision-Exp"},
                     {"model": "DeepSeek-V4-Flash-Vision-Exp", "stream": False},
                     {}):
            with self.subTest(body=body):
                self.assertFalse(downgrade_stream(body))

    def test_model_gate(self):
        self.assertTrue(model_needs_stream_downgrade("AMD0/DeepSeek-V4-Flash-Vision-Exp"))
        self.assertFalse(model_needs_stream_downgrade("DeepSeek-V4-Flash-0731"))
        self.assertFalse(model_needs_stream_downgrade(None))


class TestCompletionToSse(unittest.TestCase):
    """``completion_to_sse`` — buffered completion replayed as OpenAI SSE."""

    @staticmethod
    def _events(sse):
        return [json.loads(line[len("data: "):])
                for line in sse.decode().split("\n\n")
                if line.startswith("data: ") and line != "data: [DONE]"]

    def _completion(self, message, **extra):
        return json.dumps({"id": "c1", "object": "chat.completion",
                           "created": 1, "model": "m",
                           "choices": [{"index": 0, "message": message,
                                        "finish_reason": "tool_calls"}], **extra}).encode()

    def test_tool_calls_replayed_flat_and_complete(self):
        completion = self._completion({
            "role": "assistant", "content": None,
            "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "get_file",
                              "arguments": "{\"filePath\": \"/etc/os-release\"}"}},
                {"id": "call_2", "type": "function",
                 "function": {"name": "get_file",
                              "arguments": "{\"filePath\": \"/etc/hostname\"}"}},
            ],
        })
        events = self._events(completion_to_sse(completion, include_usage=False))
        # role+content, one chunk per tool_call, finish chunk.
        self.assertEqual(len(events), 4)
        self.assertEqual(events[0]["choices"][0]["delta"],
                         {"role": "assistant", "content": ""})
        args = [e["choices"][0]["delta"]["tool_calls"][0] for e in events[1:3]]
        self.assertEqual([a["id"] for a in args], ["call_1", "call_2"])
        self.assertEqual([a["index"] for a in args], [0, 1])
        # The replayed arguments are the client-visible contract: each
        # tool_call must carry its complete, flat, valid-JSON argument
        # string — never the server's double-wrapped shape.
        for a in args:
            self.assertEqual(a["type"], "function")
            parsed = json.loads(a["function"]["arguments"])
            self.assertIn("filePath", parsed)
            self.assertNotIn("arguments", parsed)
        self.assertEqual(events[3]["choices"][0]["finish_reason"], "tool_calls")
        self.assertTrue(events[-1]["choices"][0]["delta"] == {})

    def test_reasoning_and_content_order(self):
        completion = self._completion({"role": "assistant",
                                       "reasoning_content": "thinking",
                                       "content": "answer"})
        events = self._events(completion_to_sse(completion, include_usage=False))
        self.assertEqual(events[0]["choices"][0]["delta"]["reasoning_content"], "thinking")
        self.assertEqual(events[1]["choices"][0]["delta"]["content"], "answer")

    def test_usage_chunk_gated_on_include_usage(self):
        completion = self._completion({"role": "assistant", "content": "x"},
                                      usage={"total_tokens": 7})
        with_usage = self._events(completion_to_sse(completion, include_usage=True))
        self.assertEqual(with_usage[-1]["choices"], [])
        self.assertEqual(with_usage[-1]["usage"]["total_tokens"], 7)
        without_usage = self._events(completion_to_sse(completion, include_usage=False))
        self.assertNotIn("usage", without_usage[-1])

    def test_wrapped_arguments_unwrapped(self):
        # The vision-exp server double-wraps: sole-key and spurious-key
        # variants. Replay must hand the client flat arguments.
        completion = self._completion({
            "role": "assistant", "content": None,
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "read",
                 "arguments": "{\"arguments\": {\"path\": \"/x\", \"i\": \"y\"}}"}},
                {"id": "c2", "type": "function", "function": {"name": "edit",
                 "arguments": "{\"path\": \"/x\", \"arguments\": {\"oldText\": \"a\"}}"}},
            ],
        })
        events = self._events(completion_to_sse(completion, include_usage=False))
        a1 = json.loads(events[1]["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"])
        a2 = json.loads(events[2]["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"])
        self.assertEqual(a1, {"path": "/x", "i": "y"})
        self.assertEqual(a2, {"path": "/x", "oldText": "a"})

    def test_clean_arguments_verbatim(self):
        completion = self._completion({
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "c1", "type": "function", "function": {
                "name": "read", "arguments": "{\"path\": \"/x\"}"}}],
        })
        events = self._events(completion_to_sse(completion, include_usage=False))
        raw = events[1]["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"]
        self.assertEqual(json.loads(raw), {"path": "/x"})

    def test_garbage_returns_none(self):
        self.assertIsNone(completion_to_sse(b"not json", include_usage=False))
        self.assertIsNone(completion_to_sse(b"{}", include_usage=False))
        self.assertIsNone(completion_to_sse(b'{"choices": []}', include_usage=False))

    def test_done_sentinel_always_last(self):
        sse = completion_to_sse(self._completion({"role": "assistant", "content": "x"}),
                                include_usage=False)
        self.assertTrue(sse.decode().endswith("data: [DONE]\n\n"))


if __name__ == "__main__":
    unittest.main()
