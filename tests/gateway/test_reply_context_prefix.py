"""Tests for gateway.platforms.base.build_reply_context_prefix.

Covers the three branches of reply-context injection:
  1. Interactive card (reply_to_raw_content set) — full card payload injected
  2. Plain text reply — [Replying to: "..."] prefix
  3. Own-message reply — [Replying to your previous message: "..."] prefix

Also verifies the cap on large card payloads and the no-context fallback.
"""
from types import SimpleNamespace

import pytest

from gateway.platforms.base import (
    REPLY_RAW_CONTENT_CAP,
    build_reply_context_prefix,
)


def _event(**kw):
    """Build a minimal event-like object for the helper."""
    defaults = {
        "reply_to_message_id": "msg-123",
        "reply_to_text": None,
        "reply_to_raw_content": None,
        "reply_to_is_own_message": False,
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


class TestCardInjection:
    def test_raw_content_injects_full_card(self):
        ev = _event(reply_to_raw_content='{"header":{"title":"Test"}}')
        result = build_reply_context_prefix(ev)
        assert result.startswith("[The user is replying to a card message.")
        assert "Card content:\n" in result
        assert '{"header":{"title":"Test"}}' in result
        assert result.endswith("]\n\n")

    def test_raw_content_truncates_above_cap(self):
        big = "X" * (REPLY_RAW_CONTENT_CAP + 500)
        ev = _event(reply_to_raw_content=big)
        result = build_reply_context_prefix(ev)
        assert "...[truncated]" in result
        # Payload portion = cap + truncation marker, not the full oversized blob
        body = result.split("Card content:\n", 1)[1]
        assert len(body) < REPLY_RAW_CONTENT_CAP + 100

    def test_raw_content_at_cap_not_truncated(self):
        exact = "Y" * REPLY_RAW_CONTENT_CAP
        ev = _event(reply_to_raw_content=exact)
        result = build_reply_context_prefix(ev)
        assert "...[truncated]" not in result

    def test_raw_content_prefers_card_over_text(self):
        """When both raw_content and text exist, card wins."""
        ev = _event(
            reply_to_raw_content='{"card":true}',
            reply_to_text="plain text snippet",
        )
        result = build_reply_context_prefix(ev)
        assert "replying to a card message" in result
        assert "plain text snippet" not in result


class TestTextFallback:
    def test_plain_text_reply(self):
        ev = _event(reply_to_text="User said something here")
        result = build_reply_context_prefix(ev)
        assert result == '[Replying to: "User said something here"]\n\n'

    def test_own_message_reply(self):
        ev = _event(
            reply_to_text="Bot's earlier message",
            reply_to_is_own_message=True,
        )
        result = build_reply_context_prefix(ev)
        assert result == '[Replying to your previous message: "Bot\'s earlier message"]\n\n'

    def test_long_text_truncated_to_500(self):
        long_text = "A" * 800
        ev = _event(reply_to_text=long_text)
        result = build_reply_context_prefix(ev)
        # Only first 500 chars appear in the snippet
        assert "A" * 500 in result
        assert "A" * 501 not in result.split('"')[1]


class TestNoContext:
    def test_no_reply_fields_returns_empty(self):
        ev = _event(
            reply_to_message_id=None,
            reply_to_text=None,
            reply_to_raw_content=None,
        )
        assert build_reply_context_prefix(ev) == ""

    def test_empty_strings_return_empty(self):
        ev = _event(reply_to_text="", reply_to_raw_content="")
        assert build_reply_context_prefix(ev) == ""


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
