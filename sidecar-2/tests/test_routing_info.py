"""Tests for sidecar-2 routing_info.extract_provider.

Fixtures under ``fixtures/`` were captured verbatim from the live Bifrost
(v1.6.4) on 127.0.0.1:8080:

* ``chat_completions_stream.sse``     — /v1/chat/completions stream; provider
  rides the penultimate (usage-carrying) chunk's top-level
  ``extra_fields.routing_info``.
* ``responses_stream_completed.sse``  — /v1/responses stream; provider rides
  the terminal ``response.completed`` event's top-level ``extra_fields``.
* ``responses_stream_fallback.sse``   — /v1/responses stream forced onto
  nvidia-2 with fallbacks [nvidia-1]; actually served by nvidia-1.
* ``chat_completions_stream_truncated.sse`` — same chat stream with the
  usage chunk + [DONE] cut off (client disconnect shape) -> no provider.

The contract: a completed response always yields its serving provider; a
stream that never reaches its terminal event yields None (a normal outcome,
NOT an error).
"""

from __future__ import annotations

import importlib
import os
import unittest

_routing_info = importlib.import_module("sidecar-2.routing_info")
extract_provider = _routing_info.extract_provider

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _load(name: str) -> bytes:
    with open(os.path.join(FIXTURES, name), "rb") as f:
        return f.read()


class TestExtractProviderStream(unittest.TestCase):
    def test_chat_completions_stream(self) -> None:
        body = _load("chat_completions_stream.sse")
        self.assertEqual(extract_provider(body, is_stream=True), "nvidia-1")

    def test_responses_stream_completed(self) -> None:
        body = _load("responses_stream_completed.sse")
        self.assertEqual(extract_provider(body, is_stream=True), "nvidia-1")

    def test_responses_stream_fallback_served_by_fallback(self) -> None:
        body = _load("responses_stream_fallback.sse")
        self.assertEqual(extract_provider(body, is_stream=True), "nvidia-1")

    def test_truncated_stream_returns_none(self) -> None:
        body = _load("chat_completions_stream_truncated.sse")
        self.assertIsNone(extract_provider(body, is_stream=True))

    def test_garbage_returns_none(self) -> None:
        self.assertIsNone(extract_provider(b"not json\n\nat all", is_stream=True))
        self.assertIsNone(extract_provider(b"", is_stream=True))


class TestExtractProviderNonStream(unittest.TestCase):
    def test_top_level_extra_fields(self) -> None:
        body = (
            b'{"id":"x","extra_fields":{"routing_info":{"provider":"nvidia-4"}}}'
        )
        self.assertEqual(extract_provider(body, is_stream=False), "nvidia-4")

    def test_missing_or_empty_provider_returns_none(self) -> None:
        self.assertIsNone(extract_provider(b"{}", is_stream=False))
        self.assertIsNone(
            extract_provider(
                b'{"extra_fields":{"routing_info":{"provider":""}}}',
                is_stream=False,
            )
        )
        self.assertIsNone(extract_provider(b"not json", is_stream=False))


if __name__ == "__main__":
    unittest.main()
