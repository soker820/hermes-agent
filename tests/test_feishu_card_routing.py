"""Tests for feishu_card.py pure functions.

Covers: build_outbound_payloads (routing), strip_markdown,
_build_card_json, _split_card_elements, _maybe_split_cards.

Run: /path/to/venv/bin/python -m pytest tests/test_feishu_card_routing.py -q
"""

import pytest
import sys
import os
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gateway.platforms.feishu_card as fc
from gateway.platforms.feishu_card import (
    build_outbound_payloads, strip_markdown,
    _split_card_elements, _build_card_json,
    _MAX_TABLES_PER_CARD, _DEFAULT_ELEMENT_MARGIN, _DEFAULT_MAX_CARD_BYTES,
    _is_table, _select_template,
)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _md_el(content: str) -> dict:
    """Build a markdown element dict with the default margin."""
    return {"tag": "markdown", "element_id": "el_x", "content": content,
            "margin": _DEFAULT_ELEMENT_MARGIN}


def _table_el(i: int) -> dict:
    """Build a native Feishu table element for split-limit testing."""
    return {"tag": "table", "element_id": f"tbl_{i}", "columns": [], "rows": [],
            "margin": _DEFAULT_ELEMENT_MARGIN}


# ─── build_outbound_payloads (routing) ───────────────────────────────────────

def test_routing_short_goes_text():
    payloads = build_outbound_payloads("short")
    assert payloads[0][0] == "text"


def test_routing_long_goes_card():
    payloads = build_outbound_payloads("x" * 151)
    assert payloads[0][0] == "interactive"


def test_routing_exactly_100_goes_text():
    payloads = build_outbound_payloads("x" * 100)
    assert payloads[0][0] == "text"



def test_routing_card_schema():
    payloads = build_outbound_payloads("x" * 151)
    card = json.loads(payloads[0][1])
    assert card["schema"] == "2.0"


def test_routing_text_strips_markdown():
    payloads = build_outbound_payloads("**bold** word")
    assert payloads[0][0] == "text"
    assert "**" not in payloads[0][1]


def test_routing_text_path_sanitized():
    """Short text path also sanitizes box-drawing chars and non-code fences."""
    content = "═ ═ ═\n```text\ninner\n```\n" + "x" * 20
    payloads = build_outbound_payloads(content)
    assert payloads[0][0] == "text"
    assert "═" not in payloads[0][1]
    assert "```" not in payloads[0][1]
    assert "inner" in payloads[0][1]


# ─── strip_markdown ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("input_md, expected", [
    ("```python\ncode block\n```", "code block"),
    ("**bold text**", "bold text"),
    ("___bold triple___", "bold triple"),
    ("__bold double__", "bold double"),
    ("# Heading", "Heading"),
    ("- item one", "• item one"),
    ("| col1 | col2 |", "col1 col2"),
    ("[click here](https://example.com)", "click here (https://example.com)"),
    ("> quoted text", "quoted text"),
    ("*italic text*", "italic text"),
    ("`inline code`", "inline code"),
    ("multi   spaces   here", "multi spaces here"),
    ("~~deleted~~", "deleted"),
    ("<u>underlined</u>", "underlined"),
    ("---", ""),
    ("***", ""),
    ("___", ""),
])
def test_strip_markdown_cases(input_md, expected):
    assert strip_markdown(input_md) == expected


# ─── _build_card_json ────────────────────────────────────────────────────────

def test_build_card_json_no_header():
    card = json.loads(_build_card_json([_md_el("text")]))
    assert card["schema"] == "2.0"
    assert "header" not in card
    assert len(card["body"]["elements"]) == 1


def test_build_card_json_empty_elements():
    card = json.loads(_build_card_json([]))
    assert card["body"]["elements"][0]["content"] == ""


def test_build_card_json_preserves_elements():
    card = json.loads(_build_card_json([_md_el("hello"), _md_el("world")]))
    assert card["body"]["elements"][0]["content"] == "hello"
    assert card["body"]["elements"][1]["content"] == "world"


def test_build_card_json_body_padding():
    card = json.loads(_build_card_json([_md_el("x")]))
    assert card["body"]["padding"] == "12px 8px 12px 8px"


# ─── _split_card_elements ───────────────────────────────────────────────────

def test_split_small_content_single_group():
    groups = _split_card_elements([_md_el("short")])
    assert len(groups) == 1
    assert len(groups[0]) == 1


def test_split_empty_elements():
    assert _split_card_elements([]) == []


def test_split_oversized_single_element_truncated():
    big = _md_el("x" * (2 * _DEFAULT_MAX_CARD_BYTES))
    groups = _split_card_elements([big])
    assert len(groups) == 1
    assert fc._CONTENT_TRUNCATION_NOTICE in groups[0][0]["content"]
    assert len(_build_card_json(groups[0]).encode("utf-8")) <= _DEFAULT_MAX_CARD_BYTES


def test_split_multiple_elements_fit_one_group():
    groups = _split_card_elements([_md_el("a"), _md_el("b"), _md_el("c")])
    assert len(groups) == 1
    assert len(groups[0]) == 3


def test_split_respects_table_limit():
    els = [_table_el(i) for i in range(_MAX_TABLES_PER_CARD + 2)]
    groups = _split_card_elements(els)
    assert len(groups) >= 2
    for g in groups:
        assert sum(1 for el in g if _is_table(el)) <= _MAX_TABLES_PER_CARD


# ─── Card body defaults ─────────────────────────────────────────────────────

def test_card_body_uses_default_padding():
    card_json = fc._build_card_json([_md_el("x")])
    card = json.loads(card_json)
    assert card["body"]["padding"] == "12px 8px 12px 8px"
    assert "horizontal_spacing" not in card["body"]  # removed: no-op in vertical layout
    assert card["body"]["horizontal_align"] == "left"
    assert card["body"]["vertical_spacing"] == "3px"
    assert card["body"]["vertical_align"] == "top"


def test_card_json_schema_version():
    card_json = fc._build_card_json([_md_el("x")])
    card = json.loads(card_json)
    assert card["schema"] == "2.0"
    assert "header" not in card  # no templates → no header


# ─── _select_template ───────────────────────────────────────────────────────

def test_select_template_red_from_title():
    assert _select_template("发生错误") == "red"


def test_select_template_green_from_title():
    assert _select_template("任务成功完成") == "green"


def test_select_template_content_fallback():
    """Title has no keywords, but content contains 'error' → should select red."""
    assert _select_template("", "An error occurred in the system") == "red"


def test_select_template_default_blue():
    """No keywords in title or content → default blue."""
    assert _select_template("普通标题", "普通内容") == "blue"


# ─── Native table component ──────────────────────────────────────────────────

def test_parse_markdown_table_valid():
    """Markdown pipe table → native Feishu table element."""
    md = "| 指标 | 目标 |\n|------|------|\n| CPU | 80% |"
    tbl = fc._parse_markdown_table(md)
    assert tbl is not None
    assert tbl["tag"] == "table"
    assert len(tbl["columns"]) == 2
    assert tbl["columns"][0]["display_name"] == "指标"
    assert tbl["columns"][1]["display_name"] == "目标"
    assert len(tbl["rows"]) == 1
    assert tbl["rows"][0]["col_0"] == "CPU"
    assert tbl["rows"][0]["col_1"] == "80%"


def test_parse_markdown_table_not_table():
    """Non-table text → None."""
    assert fc._parse_markdown_table("just text") is None
    assert fc._parse_markdown_table("| no separator\n| no sep") is None
    assert fc._parse_markdown_table("") is None


def test_build_elements_native_table():
    """Full pipeline: markdown table content → element with tag 'table'."""
    content = "### 📊 报告\n\n| 指标 | 值 |\n|-----|-----|\n| A | 1 |\n| B | 2 |"
    els = fc._build_elements_from_content(content)
    table_els = [e for e in els if e["tag"] == "table"]
    assert len(table_els) == 1
    assert len(table_els[0]["columns"]) == 2
    assert len(table_els[0]["rows"]) == 2


def test_is_table_native():
    """_is_table detects native Feishu table components."""
    native = {"tag": "table", "columns": [], "rows": []}
    md_el = _md_el("| h |\n|---|\n| v |")
    plain = _md_el("not a table")
    assert _is_table(native) is True
    # After pipeline refactor, markdown elements are never tables
    assert _is_table(md_el) is False
    assert _is_table(plain) is False


# ─── Blockquote emoji → div+icon callout ─────────────────────────────────────

def test_build_elements_blockquote_emoji():
    """Blockquote with emoji → div+standard_icon callout (v2 native)."""
    content = "> ⚠️ 注意：内存偏高"
    els = fc._build_elements_from_content(content)
    assert len(els) == 1
    assert els[0]["tag"] == "div"
    assert els[0]["icon"]["token"] == "warning_outlined"
    assert els[0]["icon"]["color"] == "orange"
    assert "注意：内存偏高" in els[0]["text"]["content"]


def test_build_elements_blockquote_no_emoji():
    """Blockquote without emoji → plain markdown (no visual prefix)."""
    content = "> 普通引用文本"
    els = fc._build_elements_from_content(content)
    assert len(els) == 1
    assert els[0]["tag"] == "markdown"
    assert "普通引用文本" in els[0]["content"]
    # No ▎ vertical-bar prefix (removed 2026-07-18)
    assert "▎" not in els[0]["content"]


def test_build_elements_blockquote_variants():
    """All mapped emojis produce correct div+icon colors and tokens."""
    for emoji, color in fc._EMOJI_COLOR_MAP.items():
        content = f"> {emoji} message"
        els = fc._build_elements_from_content(content)
        assert els[0]["tag"] == "div", f"Expected div for {emoji}"
        assert els[0]["icon"]["color"] == color, \
            f"Wrong color for emoji {emoji}"
        assert els[0]["icon"]["token"] == fc._EMOJI_ICON_MAP[emoji], \
            f"Wrong icon token for emoji {emoji}"
        assert els[0]["icon"]["token"] in (
            "warning_outlined", "close_outlined", "done_outlined", "info_outlined"
        ), f"Unknown icon token for emoji {emoji}: {els[0]['icon']['token']}"


def test_hr_before_table_filtered():
    """HR immediately before a table is filtered (table has its own margin)."""
    content = "段落\n\n---\n\n| a | b |\n|---|---|\n| 1 | 2 |"
    els = fc._build_elements_from_content(content)
    tags = [e["tag"] for e in els]
    assert "hr" not in tags
    assert tags == ["markdown", "table"]


def test_hr_between_paragraphs_preserved():
    """HR between two paragraphs is kept (only heading/table/trailing HR filtered)."""
    content = "段落一\n\n---\n\n段落二"
    els = fc._build_elements_from_content(content)
    tags = [e["tag"] for e in els]
    assert tags == ["markdown", "hr", "markdown"]


# ─── End-to-end routing with new features ────────────────────────────────────

def test_full_pipeline_table_and_blockquote():
    """Full routing: table + blockquote in a single card."""
    content = (
        "### 📊 监控报告\n\n这是一段足够长的描述文字以确保内容超过路由阈值。"
        "下面是关键指标的监控数据表格和异常告警信息。\n\n"
        "| 服务 | 状态 |\n|------|------|\n| API | 正常 |\n| DB | 异常 |\n\n"
        "> ❌ DB连接超时，需立即处理\n\n"
        "这是详情说明文字。建议检查数据库连接池配置和网络延迟。"
    )
    payloads = build_outbound_payloads(content)
    assert payloads[0][0] == "interactive"
    card = json.loads(payloads[0][1])
    tags = [e["tag"] for e in card["body"]["elements"]]
    assert "table" in tags
    # The blockquote should be a div+icon callout (red close_outlined)
    div_els = [e for e in card["body"]["elements"] if e.get("tag") == "div"]
    assert len(div_els) == 1
    assert div_els[0]["icon"]["color"] == "red"
    assert div_els[0]["icon"]["token"] == "close_outlined"
    assert "DB连接超时" in div_els[0]["text"]["content"]


# ─── Subtitle and tags ──────────────────────────────────────────────────────

def test_build_card_json_with_subtitle():
    """Subtitle appears in header when provided."""
    card = json.loads(fc._build_card_json(
        [_md_el("content")],
        title="Main Title",
        subtitle="Subtitle Text"
    ))
    assert "header" in card
    assert card["header"]["subtitle"]["content"] == "Subtitle Text"


def test_build_card_json_without_subtitle():
    """No subtitle key when not provided."""
    card = json.loads(fc._build_card_json(
        [_md_el("content")],
        title="Main Title"
    ))
    assert "header" in card
    assert "subtitle" not in card["header"]


def test_build_card_json_with_tags():
    """Tags appear as text_tag_list in header."""
    card = json.loads(fc._build_card_json(
        [_md_el("content")],
        title="Main Title",
        tags=["tag1", "tag2"]
    ))
    assert "header" in card
    assert len(card["header"]["text_tag_list"]) == 2
    assert card["header"]["text_tag_list"][0]["text"]["content"] == "tag1"
    assert card["header"]["text_tag_list"][1]["text"]["content"] == "tag2"


def test_build_card_json_tags_truncation():
    """Tags beyond 3 are truncated with warning."""
    card = json.loads(fc._build_card_json(
        [_md_el("content")],
        title="Main Title",
        tags=["t1", "t2", "t3", "t4", "t5"]
    ))
    assert len(card["header"]["text_tag_list"]) == 3


def test_build_card_json_tags_color_cycling():
    """Tag colors cycle through _TAG_COLORS."""
    card = json.loads(fc._build_card_json(
        [_md_el("content")],
        title="Main Title",
        tags=["t1", "t2", "t3"]
    ))
    colors = [t["color"] for t in card["header"]["text_tag_list"]]
    assert colors == ["blue", "green", "orange"]


def test_build_card_json_no_header_when_no_title():
    """Subtitle and tags are ignored when no title is provided."""
    card = json.loads(fc._build_card_json(
        [_md_el("content")],
        subtitle="ignored",
        tags=["ignored"]
    ))
    assert "header" not in card


# ─── Non-code fence stripping ───────────────────────────────────────────────

def test_sanitize_strips_plain_text_fence():
    """```plain_text fences are stripped, content preserved."""
    content = "before\n```plain_text\ninner content\n```\nafter"
    result = fc._sanitize_content(content)
    assert "```plain_text" not in result
    assert "inner content" in result
    assert "before" in result
    assert "after" in result


def test_sanitize_strips_text_fence():
    """```text fences are stripped."""
    content = "```text\nsome text\n```"
    result = fc._sanitize_content(content)
    assert "```text" not in result
    assert "some text" in result


def test_sanitize_preserves_python_fence():
    """Real code fences (```python) are preserved."""
    content = "```python\ndef hello():\n    pass\n```"
    result = fc._sanitize_content(content)
    assert "```python" in result


def test_sanitize_preserves_bash_fence():
    """Real code fences (```bash) are preserved."""
    content = "```bash\necho hello\n```"
    result = fc._sanitize_content(content)
    assert "```bash" in result


def test_sanitize_strips_markdown_fence():
    """```markdown fences are stripped."""
    content = "```markdown\n# Title\n```"
    result = fc._sanitize_content(content)
    assert "```markdown" not in result
    assert "# Title" in result


# ─── _maybe_split_cards ─────────────────────────────────────────────────────

def test_maybe_split_single_small_card():
    """Small content → single card, no page indicator."""
    cards = fc._maybe_split_cards([_md_el("hello"), _md_el("world")])
    assert len(cards) == 1
    card = json.loads(cards[0])
    assert card["schema"] == "2.0"
    assert len(card["body"]["elements"]) == 2


def test_maybe_split_oversized_content_splits():
    """Content that exceeds one card → multiple cards, each within limit."""
    big = _md_el("x" * (2 * _DEFAULT_MAX_CARD_BYTES))
    cards = fc._maybe_split_cards([big, big])
    assert len(cards) >= 2
    for c in cards:
        assert len(c.encode("utf-8")) <= _DEFAULT_MAX_CARD_BYTES


def test_maybe_split_page_indicator_and_header_first_only():
    """Multi-card: page indicator prepended, header only on the first card."""
    big = _md_el("x" * (2 * _DEFAULT_MAX_CARD_BYTES))
    cards = fc._maybe_split_cards([big, big, big], title="测试标题")
    assert len(cards) > 1
    first = json.loads(cards[0])
    second = json.loads(cards[1])
    assert "📋" in first["body"]["elements"][0]["content"]
    assert first["header"]["title"]["content"] == "测试标题"
    assert "header" not in second


def test_maybe_split_oversized_single_element_truncated():
    """Single element larger than a card → truncated, still fits."""
    big = _md_el("x" * (5 * _DEFAULT_MAX_CARD_BYTES))
    cards = fc._maybe_split_cards([big])
    assert len(cards) == 1
    assert fc._CONTENT_TRUNCATION_NOTICE in json.loads(cards[0])["body"]["elements"][0]["content"]
    assert len(cards[0].encode("utf-8")) <= _DEFAULT_MAX_CARD_BYTES


def test_maybe_split_respects_table_limit():
    """More than _MAX_TABLES_PER_CARD tables → split so no card exceeds it."""
    els = [_table_el(i) for i in range(_MAX_TABLES_PER_CARD + 2)]
    cards = fc._maybe_split_cards(els)
    assert len(cards) >= 2
    for c in cards:
        card = json.loads(c)
        tbl_count = sum(1 for el in card["body"]["elements"] if el["tag"] == "table")
        assert tbl_count <= _MAX_TABLES_PER_CARD


def test_truncate_oversized_group_loops_until_fit():
    """Multiple oversized elements → loop keeps truncating until the group fits."""
    els = [_md_el("x" * 20000) for _ in range(3)]
    build_fn = lambda g: len(fc._build_card_json(g).encode("utf-8"))
    result = fc._truncate_oversized_group(els, _DEFAULT_MAX_CARD_BYTES, build_fn)
    assert build_fn(result) <= _DEFAULT_MAX_CARD_BYTES


def test_maybe_split_with_subtitle_and_tags():
    """Header with subtitle+tags → split cards still fit within the limit."""
    big = _md_el("x" * (2 * _DEFAULT_MAX_CARD_BYTES))
    cards = fc._maybe_split_cards(
        [big, big], title="标题", subtitle="副标题", tags=["t1", "t2", "t3"]
    )
    assert len(cards) >= 2
    for c in cards:
        assert len(c.encode("utf-8")) <= _DEFAULT_MAX_CARD_BYTES


def test_maybe_split_oversized_div_truncated():
    """Oversized div callout → text.content truncated via the div path."""
    big_div = {
        "tag": "div", "element_id": "el_x",
        "icon": {"tag": "standard_icon", "token": "warning_outlined", "color": "orange"},
        "text": {"tag": "lark_md", "content": "x" * (2 * _DEFAULT_MAX_CARD_BYTES)},
        "margin": _DEFAULT_ELEMENT_MARGIN,
    }
    cards = fc._maybe_split_cards([big_div])
    assert len(cards) == 1
    assert len(cards[0].encode("utf-8")) <= _DEFAULT_MAX_CARD_BYTES
    el = json.loads(cards[0])["body"]["elements"][0]
    assert el["tag"] == "div"
    assert fc._CONTENT_TRUNCATION_NOTICE in el["text"]["content"]


# ─── Subtitle dedup + header icon ───────────────────────────────────────────

def test_subtitle_heading_deduped_from_body():
    """Subtitle shown in the header must not be duplicated in the body."""
    content = "# 系统巡检报告\n\n## 每日例行\n\n" + "正文内容" * 80
    card = json.loads(build_outbound_payloads(content)[0][1])
    assert card["header"]["subtitle"]["content"] == "每日例行"
    body_texts = [el.get("content", "") for el in card["body"]["elements"]]
    assert not any("**每日例行**" in t for t in body_texts)


def test_header_icon_from_emoji_title():
    """Leading emoji in the title → header icon, emoji stripped from title."""
    content = "# ✅ 任务完成\n\n" + "详细内容" * 80
    card = json.loads(build_outbound_payloads(content)[0][1])
    assert card["header"]["title"]["content"] == "任务完成"
    assert card["header"]["icon"]["token"] == "done_outlined"
    assert card["header"]["icon"]["color"] == "green"


def test_header_icon_not_added_without_mapped_emoji():
    """Unmapped emoji (e.g. 📊) stays in the title, no icon added."""
    content = "# 📊 普通标题\n\n" + "详细内容" * 80
    card = json.loads(build_outbound_payloads(content)[0][1])
    assert card["header"]["title"]["content"] == "📊 普通标题"
    assert "icon" not in card["header"]


# ─── KPI column_set ─────────────────────────────────────────────────────────

def test_kpi_column_set_built():
    """Metric-emoji heading + key:value list (2-4 items) → native column_set."""
    content = "### 📊 指标\n- **CPU**: 80%\n- **内存**: 60%\n\n正文内容"
    els = fc._build_elements_from_content(content)
    cs = [el for el in els if el["tag"] == "column_set"]
    assert len(cs) == 1
    assert cs[0]["flex_mode"] == "bisect"
    assert len(cs[0]["columns"]) == 2
    col_contents = [c["elements"][0]["content"] for c in cs[0]["columns"]]
    assert "**CPU**\n80%" in col_contents
    # list items must not also appear as plain markdown
    assert not any("CPU" in el.get("content", "") and el["tag"] != "column_set"
                   for el in els)


def test_kpi_not_triggered_plain_list():
    """Plain (non key:value) list after metric heading → no column_set."""
    content = "### 📊 指标\n- 普通项甲\n- 普通项乙"
    els = fc._build_elements_from_content(content)
    assert not any(el["tag"] == "column_set" for el in els)


def test_kpi_not_triggered_single_item():
    """Single key:value item → no column_set (needs 2+)."""
    content = "### 📊 指标\n- **CPU**: 80%"
    els = fc._build_elements_from_content(content)
    assert not any(el["tag"] == "column_set" for el in els)


def test_kpi_not_triggered_without_metric_emoji():
    """Non-metric heading + key:value list → no column_set."""
    content = "### 常规标题\n- **CPU**: 80%\n- **内存**: 60%"
    els = fc._build_elements_from_content(content)
    assert not any(el["tag"] == "column_set" for el in els)


def test_kpi_triggered_from_next_paragraph():
    """List separated by a blank line still triggers column_set."""
    content = "### 📊 指标\n\n- **CPU**: 80%\n- **内存**: 60%\n\n正文内容"
    els = fc._build_elements_from_content(content)
    cs = [el for el in els if el["tag"] == "column_set"]
    assert len(cs) == 1
    # the list paragraph must not also be emitted as plain markdown
    assert not any("CPU" in el.get("content", "") and el["tag"] != "column_set"
                   for el in els)


def test_kpi_boundary_four_triggered_five_not():
    """4 items → column_set; 5 items → plain heading (too crowded)."""
    four = "### 📊 指标\n- **A**: 1\n- **B**: 2\n- **C**: 3\n- **D**: 4"
    els4 = fc._build_elements_from_content(four)
    assert any(el["tag"] == "column_set" for el in els4)

    five = "### 📊 指标\n- **A**: 1\n- **B**: 2\n- **C**: 3\n- **D**: 4\n- **E**: 5"
    els5 = fc._build_elements_from_content(five)
    assert not any(el["tag"] == "column_set" for el in els5)


def test_callout_div_uses_calout_margin():
    """Emoji blockquote callout carries the dedicated callout margin."""
    content = "> ⚠️ 内存偏高"
    els = fc._build_elements_from_content(content)
    assert els[0]["tag"] == "div"
    assert els[0]["margin"] == fc._CALLOUT_MARGIN


def test_subtitle_prefix_match_does_not_overshoot():
    """'每日' subtitle must not dedup a different '每日详细分析' heading."""
    content = ("# 主报告\n\n## 每日\n\n### 每日详细分析\n\n" + "正文内容" * 80)
    card = json.loads(build_outbound_payloads(content)[0][1])
    assert card["header"]["subtitle"]["content"] == "每日"
    body_texts = [el.get("content", "") for el in card["body"]["elements"]]
    assert any("**每日详细分析**" in t for t in body_texts)  # kept
    assert not any("**每日**" == t.strip() for t in body_texts)  # deduped


def test_subtitle_truncated_60_still_deduped():
    """Subtitle truncated at 60 chars is still removed from the body."""
    long_subtitle = "这是一个非常长的副标题" * 10  # 100 chars > 60
    content = f"# 主报告\n\n## {long_subtitle}\n\n" + "正文内容" * 80
    card = json.loads(build_outbound_payloads(content)[0][1])
    sub = card["header"]["subtitle"]["content"]
    assert len(sub) == 60  # truncated by _extract_subtitle
    body_texts = [el.get("content", "") for el in card["body"]["elements"]]
    assert not any(t.startswith(f"**{sub}") for t in body_texts)  # deduped
