"""Tests for subagent summary budgeting (PR #9126).

delegate_task caps subagent summaries against the parent's remaining context
headroom (split across the batch) before they enter the parent's context, and
spills the full text to disk so nothing is lost. This guards the
compression/429 death spiral that batch fan-out could trigger by returning N
full summaries verbatim into the parent.
"""

import os
import tempfile

import pytest

import tools.delegate_tool as dt


class _FakeCompressor:
    def __init__(self, context_length, max_tokens, last_prompt_tokens=0):
        self.context_length = context_length
        self.max_tokens = max_tokens
        # Patch 070: the budget reads last_prompt_tokens first.
        # When 0/missing it falls back to last_real_prompt_tokens, then to
        # the parent's session_prompt_tokens.  Tests that pass a non-zero
        # value here exercise the primary code path; tests that leave it 0
        # exercise the fallback chain.
        self.last_prompt_tokens = last_prompt_tokens


class _FakeParent:
    def __init__(self, context_length, used_tokens, max_tokens, last_prompt_tokens=0):
        self.context_compressor = _FakeCompressor(context_length, max_tokens, last_prompt_tokens)
        self.session_prompt_tokens = used_tokens


def test_small_summaries_pass_through_untouched():
    parent = _FakeParent(context_length=200_000, used_tokens=10_000, max_tokens=8_000)
    results = [
        {"task_index": 0, "summary": "short result A", "status": "completed"},
        {"task_index": 1, "summary": "short result B", "status": "completed"},
    ]
    dt._apply_summary_budget(results, parent)
    assert results[0]["summary"] == "short result A"
    assert "summary_truncated" not in results[0]
    assert "summary_truncated" not in results[1]


def test_batch_overflow_trimmed_and_spilled_losslessly(monkeypatch):
    # Isolate spill directory to a temp HERMES_HOME.
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setenv("HERMES_HOME", os.path.join(td, ".hermes"))
        # Distinct head + tail markers so we can prove the tail survives.
        big = "HEAD_MARKER\n" + ("X" * 50_000) + "\nTAIL_MARKER"
        # Parent nearly full (120k/131k) → tiny headroom → aggressive trim.
        parent = _FakeParent(context_length=131_000, used_tokens=120_000, max_tokens=8_000)
        results = [
            {"task_index": i, "summary": big, "status": "completed"} for i in range(5)
        ]
        dt._apply_summary_budget(results, parent)
        for r in results:
            assert r["summary_truncated"] is True
            assert len(r["summary"]) < len(big)
            # Head+tail window: both ends survive in-context.
            assert "HEAD_MARKER" in r["summary"]
            assert "TAIL_MARKER" in r["summary"]
            path = r.get("summary_full_path")
            assert path and os.path.exists(path)
            # The spill file holds the FULL original text — nothing is lost.
            with open(path, encoding="utf-8") as fh:
                assert fh.read() == big
            # The footer points the parent at the full version with an offset.
            assert "read_file" in r["summary"]
            assert "offset=" in r["summary"]
            # Spilled into the delegation cache (mounted into remote backends).
            assert os.path.join("cache", "delegation") in path


def test_empty_results_is_noop():
    # No summaries → nothing to do, must not raise.
    dt._apply_summary_budget([], _FakeParent(131_000, 1_000, 8_000))
    dt._apply_summary_budget(
        [{"task_index": 0, "status": "failed", "summary": None}],
        _FakeParent(131_000, 1_000, 8_000),
    )


def test_last_prompt_tokens_preferred_over_session_accumulator():
    """Patch 070: last_prompt_tokens (current context) must override
    session_prompt_tokens (lifetime accumulator).

    The bug: session_prompt_tokens only ever grows, so on a long session
    headroom was permanently negative → every summary starved to the
    2000-char floor even when the live context was nearly empty.
    """
    # session_prompt_tokens=999_999 (huge, would starve), but
    # last_prompt_tokens=5_000 (real current usage, lots of headroom).
    parent = _FakeParent(
        context_length=200_000,
        used_tokens=999_999,  # stale accumulator
        max_tokens=8_000,
        last_prompt_tokens=5_000,  # real current usage
    )
    big = "HEAD_MARKER\n" + ("Y" * 20_000) + "\nTAIL_MARKER"
    results = [{"task_index": 0, "summary": big, "status": "completed"}]
    dt._apply_summary_budget(results, parent)
    # With last_prompt_tokens=5_000, headroom is large (~187k tokens × 0.1
    # ÷ 1 summary × 4 chars/token ≈ 74k chars) → 20k summary should NOT be
    # truncated. If the code used session_prompt_tokens (999_999), headroom
    # would be negative and the summary would hit the 2000-char floor.
    assert "summary_truncated" not in results[0], (
        "summary was truncated despite low last_prompt_tokens — the budget "
        "is using session_prompt_tokens (accumulator) instead of "
        "last_prompt_tokens (current context). Patch 070 regression."
    )
