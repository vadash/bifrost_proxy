"""Tests for sidecar-2 sanitize — empty-thinking-block stripping.

These guard the observable contract: Bedrock's ``thinking: Field required``
400 must no longer trigger for Claude-family requests carrying degenerate
thinking blocks, and no other payload shape may be mutated.
"""

from __future__ import annotations

import copy
import importlib
import unittest

# ``sidecar-2`` is not a valid ``import`` statement identifier (hyphen), so
# load the module via importlib.
_sanitize = importlib.import_module("sidecar-2.sanitize")
mirror_max_tokens = _sanitize.mirror_max_tokens
model_needs_sanitize = _sanitize.model_needs_sanitize
rewrite_reasoning_effort = _sanitize.rewrite_reasoning_effort
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


if __name__ == "__main__":
    unittest.main()
