"""Tests for Feishu adapter outbound markdown payload construction.

Originally reproduced hermes-agent issue #52786 (table downgrade bug).
After the unified routing refactor (2026-07-05), the send path no longer
produces ``post`` — it routes via feishu_card: ≤150 chars → ``text``,
>150 chars → ``interactive`` card. These tests now guard the unified
routing behaviour: content is never lost, tables in interactive cards
render as native Feishu table elements.
"""

from __future__ import annotations

import json

from tests.gateway._plugin_adapter_loader import load_plugin_adapter

_adapter = load_plugin_adapter("feishu")


def _call_build_outbound_payload(content: str) -> tuple[str, str]:
    """Invoke ``_build_outbound_payload`` on a bare adapter instance.

    Note: post routing was removed in the unified routing refactor
    (2026-07-05); _build_outbound_payload now delegates to
    _build_outbound_payloads → feishu_card.build_outbound_payloads.
    """
    inst = object.__new__(_adapter.FeishuAdapter)
    return inst._build_outbound_payload(content)


def test_short_table_routes_to_text_with_content_preserved():
    """A short table (≤150 chars) routes to ``text`` via feishu_card, and
    the table data must be preserved in the plain-text output."""
    content = (
        "| col A | col B |\n"
        "| ----- | ----- |\n"
        "| 1     | 2     |"
    )
    msg_type, payload_str = _call_build_outbound_payload(content)
    assert msg_type == "text", f"expected 'text' for short content, got {msg_type!r}"
    payload = json.loads(payload_str)
    text = payload.get("text", "")
    assert "col A" in text, f"table header lost in text output: {text!r}"
    assert "1" in text, f"table data lost in text output: {text!r}"


def test_plain_text_without_markdown_still_uses_text():
    """A message with no markdown must route to plain text."""
    msg_type, _ = _call_build_outbound_payload("just a plain sentence with no markup")
    assert msg_type == "text"


def test_short_heading_routes_to_text_with_heading_preserved():
    """A short heading (≤150 chars) routes to ``text``, heading text preserved."""
    msg_type, payload_str = _call_build_outbound_payload("# hello world\n")
    assert msg_type == "text", f"expected 'text', got {msg_type!r}"
    payload = json.loads(payload_str)
    assert "hello world" in payload.get("text", "")


def test_mixed_table_and_prose_routes_correctly():
    """A message mixing a table with surrounding prose must route correctly
    (≤150 chars → text, >150 → interactive) without losing content."""
    content = (
        "Here is the data:\n\n"
        "| col A | col B |\n"
        "| ----- | ----- |\n"
        "| 1     | 2     |\n\n"
        "Let me know."
    )
    msg_type, payload_str = _call_build_outbound_payload(content)
    # Content is ~70 chars → text routing
    assert msg_type == "text", f"expected 'text' for ~70-char content, got {msg_type!r}"
    payload = json.loads(payload_str)
    text = payload.get("text", "")
    assert "Here is the data" in text, "leading prose was lost"
    assert "col A" in text, "table header was lost"
    assert "Let me know" in text, "trailing prose was lost"
