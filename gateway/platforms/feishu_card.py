"""
Feishu card payload builder — pure functions, no adapter state.

Three-tier routing: short→text, medium→card, long→card with header.
Thresholds are configurable via environment variables (units: characters
via len(), NOT bytes):
  FEISHU_CARD_LENGTH_THRESHOLD (default 150)
  FEISHU_HEADER_LENGTH_THRESHOLD (default 300)

Layout:
  1. Config & Routing  — constants + sanitization helpers + table/KPI
                         parsers (_parse_markdown_table / _parse_kpi_items /
                         _build_kpi_column_set) + extractors
                         (title/subtitle/tags/template) + build_outbound_payloads
  2. Rendering         — strip_markdown + _build_elements_from_content
                         + truncation helpers (_truncate_to_bytes /
                           _truncate_element_content / _truncate_table_rows /
                           _truncate_oversized_group) + _split_card_elements
                         + _build_card_json + build_card_payloads
                         + _maybe_split_cards
"""

from __future__ import annotations

import json
import os
import re
import logging

_logger = logging.getLogger("feishu_card")


def _json_len(obj) -> int:
    """UTF-8 byte size of *obj*'s compact JSON serialization."""
    return len(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


# ═══════════════════════════════════════════
# 1. Config & Routing — constants + build_outbound_payloads
# ═══════════════════════════════════════════

_DEFAULT_MAX_CARD_BYTES = 28 * 1024
_MAX_TABLES_PER_CARD = 5
_MAX_TITLE_CHARS = 30


def _env_int(key: str, default: int) -> int:
    """Read an int from env, falling back to *default* on parse failure."""
    try:
        return int(os.environ.get(key, str(default)))
    except ValueError:
        _logger.warning("Invalid %s, using default %d", key, default)
        return default


# Routing thresholds are measured in CHARACTERS (len(content)), not UTF-8
# bytes — 150 chars of Chinese text is 450 bytes. All card-size budgets
# below are byte-based.
_HEADER_LENGTH_THRESHOLD = _env_int("FEISHU_HEADER_LENGTH_THRESHOLD", 300)
CARD_LENGTH_THRESHOLD = _env_int("FEISHU_CARD_LENGTH_THRESHOLD", 150)
_DEFAULT_ELEMENT_MARGIN = "4px 0px 4px 0px"
_HEADING_MARGIN = "10px 0px 6px 0px"
_TABLE_MARGIN = "8px 0px 12px 0px"
_HR_MARGIN = "4px 0px 4px 0px"
_CALLOUT_MARGIN = "8px 0px 8px 0px"
_TRUNCATE_SAFETY_MARGIN = 50
_TRUNCATE_MIN_CONTENT_BYTES = 256

# KPI indicator-card trigger: a heading starting with one of these emojis
# followed by a "- **label**: value" list (2-4 items) becomes a native
# column_set; single items or 5+ items fall back to a plain heading.
_KPI_HEADING_EMOJIS = ("📊", "📈", "📉")

# Shared truncation notices (avoid duplicated string literals across functions)
_CONTENT_TRUNCATION_NOTICE = "\n\n⚠️ [内容过长，已截断显示]"

_FENCE_MARKER = r"(?:```|~~~)"
_FENCE_RE = re.compile(rf"^\s*{_FENCE_MARKER}")
_FENCE_PATTERN = rf"{_FENCE_MARKER}[\s\S]*?{_FENCE_MARKER}"
_FENCE_SPLIT_RE = re.compile(f"({_FENCE_PATTERN})")
_FENCE_INNER_RE = re.compile(r"(?:```|~~~)[^\n]*\n(.*?)(?:```|~~~)", re.DOTALL)
_HR_RE = re.compile(r"^(?:-{3,}|\*{3,}|_{3,})$", re.MULTILINE)
# Markdown table separator row: | --- | :---: | ---: |
# {0,20} limits to 21 columns (1 leading + 20 trailing). Tables wider
# than 21 cols fail this match → _parse_markdown_table returns None →
# degrades to plain text.
_TABLE_SEP_RE = re.compile(r"^\|[\s:]*-+[\s:]*\|([\s:]*-+[\s:]*\|){0,20}$")
# KPI item: "- **label**: value" (conservative — both parts required)
_KPI_ITEM_RE = re.compile(r"^-\s+\*\*(.+?)\*\*:\s*(.+)$")

# Template color mapping based on title keywords (used by _select_template
# for card header color). Iteration order = priority (blue last = fallback).
_TEMPLATE_COLORS = {
    "red": ["错误", "error", "失败", "fail", "异常", "exception", "bug", "告警",
            "崩溃", "crash", "超时", "timeout"],
    "yellow": ["警告", "warning", "注意", "caution", "alert", "风险", "预警",
               "待处理", "pending", "等待", "waiting", "检查"],
    "green": ["成功", "success", "通过", "验收", "批准", "approved",
              "在线", "online", "正常", "normal", "健康", "health", "恢复",
              "restore", "验证", "verify", "巡检", "ok"],
    "orange": ["进度", "progress", "执行", "跟踪", "track", "部署", "deploy",
               "更新", "update", "变更", "change", "发布", "备份", "backup",
               "清理", "cleanup", "维护", "maintenance", "压缩", "compact",
               "回收", "recycle"],
    "purple": ["审计", "audit", "审查", "review", "代码检查", "测试", "test",
               "分析", "analysis", "统计", "statistics", "数据", "data",
               "扫描", "索引", "index", "同步", "sync",
               "文档", "documentation", "wiki"],
    "blue": ["信息", "info", "提示", "notice", "报告", "report", "通知", "notify",
             "摘要", "summary", "概览", "overview", "日报", "daily",
             "周报", "weekly", "月报", "monthly", "资讯", "news", "快报"],
}

# Internal marker: heading elements carry this data_type so _build_card_json
# can strip it before serialization and build_card_payloads can dedup the
# title heading from the card body.
_DATA_TYPE_HEADING = "heading"

# Short English keywords that need word-boundary matching in _select_template
# to avoid false positives (e.g. "ok" in "broken", "bug" in "debug").
_WORD_BOUNDARY_KW = {"ok", "bug", "fail", "error", "online", "track",
                     "data", "info", "sync", "test", "news", "index"}

# Emoji → color mapping for blockquote text_tag (used by
# _build_elements_from_content). Distinct from _TEMPLATE_COLORS which
# governs the header template color.
_EMOJI_COLOR_MAP = {
    "⚠️": "orange", "❌": "red", "✅": "green", "ℹ️": "blue",
    "🔴": "red", "🟡": "yellow", "🟢": "green",
    "💡": "blue", "🔥": "red", "⭐": "yellow",
}

# Emoji → icon token mapping for blockquote callout div.
# All 5 tokens verified against the official icon library (2026-07-18).
# L1 = exact icon exists; L2 = semantic approximation.
_EMOJI_ICON_MAP = {
    "⚠️": "warning_outlined",   # L1
    "❌": "close_outlined",     # L1
    "✅": "done_outlined",      # L1
    "ℹ️": "info_outlined",      # L1
    "🔴": "close_outlined",     # L2 (severity → close icon)
    "🟡": "warning_outlined",   # L2 (caution → warning icon)
    "🟢": "done_outlined",      # L2 (ok → done icon)
    "💡": "info_outlined",      # L2 (tip → info icon)
    "🔥": "warning_outlined",   # L2 (urgent → warning icon)
    "⭐": "info_outlined",      # L2 (highlight → info icon)
}

def _match_emoji_icon(text: str) -> tuple[str, str, str] | None:
    """Match a leading emoji and return (emoji, color, icon_token).

    Scans _EMOJI_COLOR_MAP for a prefix match on *text*. Returns None when
    no mapped emoji is found. Used by both the blockquote callout and the
    card header icon to avoid duplicating the lookup loop.
    """
    for emoji, color in _EMOJI_COLOR_MAP.items():
        if text.startswith(emoji):
            # _EMOJI_ICON_MAP and _EMOJI_COLOR_MAP have identical key sets,
            # so direct indexing is safe. A KeyError here is a fail-fast
            # signal that the two maps fell out of sync.
            return emoji, color, _EMOJI_ICON_MAP[emoji]
    return None


_TABLE_PIPE_SPLIT_RE = re.compile(r"\s*\|\s*")

# Precompiled patterns for strip_markdown hot path
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_BOLD_DBL_RE = re.compile(r"\*\*(.+?)\*\*")
_BOLD_TRI_RE = re.compile(r"___(.+?)___")
_BOLD_UND_RE = re.compile(r"__(.+?)__")
# * must be adjacent to non-space on both sides — prevents math
# expressions like "2 * 3 * 4" from being misread as italic.
_ITALIC_RE = re.compile(r'(?<!\*)\*(?!\s)(?!\*)(.+?)(?<!\s)(?<!\*)\*(?!\*)')
_STRIKE_RE = re.compile(r"~~([^~\n]+)~~")
_UNDERLINE_TAG_RE = re.compile(r"<u>([\s\S]*?)</u>")
_HEADING_STRIP_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_LIST_BULLET_RE = re.compile(r"^[\s]*[-*]\s+", re.MULTILINE)
_PIPE_REPLACE_RE = re.compile(r"\|")
_SEP_ROW_CLEAN_RE = re.compile(r"^[\s]*[-:\s]+$", re.MULTILINE)
_LINK_EXPAND_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BLOCKQUOTE_STRIP_RE = re.compile(r"^>\s*", re.MULTILINE)
_WS_COMPRESS_RE = re.compile(r"[^\S\n]+")  # compress runs of non-newline whitespace
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
# Precompiled patterns for _build_elements_from_content
_HEADING_NORMALIZE_RE = re.compile(r"^#{1,6}\s+(.+)", re.MULTILINE)
_H3_MATCH_RE = re.compile(r"^###\s+([^\n]+)")

# Precompiled patterns for content sanitization
_BOX_DRAWING_RE = re.compile(r"[═║╔╗╚╝╠╣╦╩╬─│┌┐└┘├┤┬┴┼]+")
_MULTI_HR_RE = re.compile(r"(?:^|\n)(?:[-*_]{3,}\s*\n){2,}", re.MULTILINE)
# Non-code fence markers that agents sometimes wrap plain text in
_NON_CODE_FENCE_RE = re.compile(r"```(?:plain_text|text|plaintext|markdown|md)\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)

# Tag colors for header text_tag_list
_TAG_COLORS = ["blue", "green", "orange", "red", "purple", "neutral"]


def _sanitize_content(content: str) -> str:
    """Clean up content that would render poorly in Feishu cards.

    - Remove box-drawing characters (═║╔╗╚╝ etc.) that render as garbled text
    - Collapse multiple consecutive --- into a single one
    - Strip non-code fence markers (```plain_text, ```text, etc.) that pollute rendering

    Note: heading level normalization is handled by _build_elements_from_content
    (_HEADING_NORMALIZE_RE), so it is NOT done here to avoid duplicate work.
    """
    # 1) Strip non-code fence markers (```plain_text, ```text, etc.)
    #    These are NOT real code blocks — agents wrap plain text in them
    content = _NON_CODE_FENCE_RE.sub(r"\1", content)

    # 2) Remove box-drawing characters (replace with space to preserve word boundaries)
    content = _BOX_DRAWING_RE.sub(" ", content)

    # 3) Collapse multiple consecutive HR lines into a single ---
    #    Pattern: 2+ lines of ---/*** /___  separated by newlines
    content = _MULTI_HR_RE.sub("\n\n---\n\n", content)

    return content


def _parse_markdown_table(text: str) -> dict | None:
    """Parse a markdown pipe table into a native Feishu table element.

    Returns a ``{"tag": "table", ...}`` dict or None if *text* is not a
    valid table (fewer than 2 rows, no separator row).
    """
    lines = text.strip().split("\n")
    if len(lines) < 2:
        return None
    # Must start and end with |
    if not lines[0].strip().startswith("|"):
        return None
    if not _TABLE_SEP_RE.match(lines[1].strip()):
        return None

    def _split_row(row: str) -> list[str]:
        cells = _TABLE_PIPE_SPLIT_RE.split(row.strip())
        # Remove leading/trailing empty from the | borders
        if cells and cells[0] == "":
            cells = cells[1:]
        if cells and cells[-1] == "":
            cells = cells[:-1]
        # Cells are plain text — strip any residual markdown formatting.
        return [strip_markdown(c) for c in cells]

    headers = _split_row(lines[0])
    data_rows = [_split_row(r) for r in lines[2:] if r.strip()]
    if not headers:
        return None

    columns = []
    for i, h in enumerate(headers):
        columns.append({
            "name": f"col_{i}",
            "display_name": h,
            "data_type": "text",
        })
    rows = []
    for dr in data_rows:
        row = {}
        for i, col in enumerate(columns):
            row[col["name"]] = dr[i] if i < len(dr) else ""
        rows.append(row)

    return {
        "tag": "table",
        # element_id is assigned by the caller (_build_elements_from_content)
        "page_size": min(len(rows), 5) if rows else 5,
        "row_height": "low",
        "header_style": {
            "text_align": "left",
            "text_size": "normal",
            "background_style": "none",  # official default
            "text_color": "grey",
            "bold": True,
            "lines": 1,
        },
        "columns": columns,
        "rows": rows,
        "margin": _TABLE_MARGIN,
    }


def _parse_kpi_items(paragraph: str) -> list[tuple[str, str]] | None:
    """Parse a ``- **label**: value`` list into (label, value) pairs.

    Returns None unless EVERY non-empty line matches (conservative: a mixed
    list never becomes a KPI card).
    """
    items = []
    for line in paragraph.split("\n"):
        line = line.strip()
        if not line:
            continue
        m = _KPI_ITEM_RE.match(line)
        if not m:
            return None
        items.append((m.group(1).strip(), m.group(2).strip()))
    return items or None


def _build_kpi_column_set(items: list[tuple[str, str]], cs_idx: int) -> dict:
    """Build a bisect column_set KPI card (label + centered value per column)."""
    return {
        "tag": "column_set",
        "element_id": f"cs_{cs_idx}",
        "flex_mode": "bisect",
        "horizontal_spacing": "default",
        "background_style": "grey",
        "margin": _TABLE_MARGIN,
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "elements": [
                    {"tag": "markdown", "text_align": "center",
                     "content": f"**{label}**\n{value}", "margin": "0px"}
                ],
            }
            for label, value in items
        ],
    }


def _extract_title(content: str) -> str:
    """Extract first heading from content, excluding code blocks."""
    # Split by code fences to avoid matching headings inside code
    parts = _FENCE_SPLIT_RE.split(content)
    for part in parts:
        if part.strip().startswith(("```", "~~~")):
            continue  # Skip code blocks
        # Look for heading in this part
        for line in part.split("\n"):
            match = _HEADING_NORMALIZE_RE.match(line.strip())
            if match:
                title = strip_markdown(match.group(1).strip())
                # Truncate to 30 chars for Feishu header limit
                if len(title) > _MAX_TITLE_CHARS:
                    title = title[:_MAX_TITLE_CHARS - 3] + "..."
                return title
    return ""


def _extract_subtitle(content: str) -> str:
    """Extract subtitle from a ## heading that appears after the title heading."""
    in_code = False
    found_title = False
    for line in content.split("\n"):
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            in_code = not in_code
            continue
        if in_code:
            continue
        # Extract subtitle: ## heading after the title heading.
        # Note: startswith("## ") already excludes "### " (the 3rd char
        # must be a space, so "### " doesn't match).
        if found_title and stripped.startswith("## "):
            sub = strip_markdown(stripped[3:].strip())
            return sub[:60]
        if _HEADING_NORMALIZE_RE.match(stripped):
            found_title = True
    return ""


def _extract_tags(content: str) -> list[str]:
    """Extract tags from > [tag1] [tag2] blockquote near title area."""
    for line in content.split("\n")[:15]:
        stripped = line.strip()
        if stripped.startswith("> [") and "]" in stripped:
            tags = re.findall(r"\[(\S+?)\]", stripped)
            if tags:
                return [t.strip() for t in tags[:3]]
    return []


def _select_template(title: str, content: str = "") -> str:
    """Select template color based on title keywords, with content fallback."""
    content = content or ""

    def _kw_match(kw: str, text_lower: str) -> bool:
        if kw in _WORD_BOUNDARY_KW:
            return re.search(r"\b" + re.escape(kw) + r"\b", text_lower) is not None
        return kw in text_lower

    def _match(text: str) -> str | None:
        """Return first matching color for keywords in *text*, preserving
        _TEMPLATE_COLORS iteration order as priority (blue last)."""
        text_lower = text.lower()
        for color, keywords in _TEMPLATE_COLORS.items():
            if any(_kw_match(kw, text_lower) for kw in keywords):
                return color
        return None

    # 1. Try title keywords; 2. Fallback to first 500 chars of content
    return _match(title) or (content and _match(content[:500])) or "blue"



def build_outbound_payloads(content: str) -> list[tuple[str, str]]:
    """Three-tier routing: ≤150→text, >150→card, >300→card with header.

    Returns a list of (msg_type, raw_content) tuples:
    - text: raw_content is plain-text string (caller wraps in JSON)
    - interactive: raw_content is a card JSON string (ready for API)
    """
    if len(content) <= CARD_LENGTH_THRESHOLD:
        # Sanitize like the card path — box-drawing chars and non-code
        # fences would otherwise leak into short text messages.
        return [("text", strip_markdown(_sanitize_content(content)))]
    force_default_title = len(content) > _HEADER_LENGTH_THRESHOLD
    # Only extract subtitle/tags when a header will actually be rendered
    # (force_default_title=True). Below _HEADER_LENGTH_THRESHOLD no header
    # is added, so these would be computed but never used.
    if force_default_title:
        subtitle = _extract_subtitle(content)
        tags = _extract_tags(content)
    else:
        subtitle, tags = "", None
    cjs = build_card_payloads(content, force_default_title=force_default_title,
                              subtitle=subtitle, tags=tags)
    _logger.debug("Route → %s (len=%d, cards=%d, subtitle=%s, tags=%s)",
                  "card+header" if force_default_title else "card",
                  len(content), len(cjs),
                  subtitle or "-", tags or "-")
    return [("interactive", cj) for cj in cjs]


# ═══════════════════════════════════════════
# 2. Rendering — strip_markdown + internal helpers + build_card_payloads + _maybe_split_cards
# ═══════════════════════════════════════════


def strip_markdown(text: str) -> str:
    """Convert markdown to readable plain text for Feishu text messages.

    Processing order matters: code fences first (inner content preserved),
    then inline formatting, headings, lists, tables, links, blockquotes.
    Final step compresses inline whitespace to single spaces while
    preserving newlines for multi-line structure.
    """
    # 0) Normalise Windows line endings so downstream MULTILINE regexes
    # anchor correctly (\\r before \\n breaks ^/$ boundaries).
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # 1) Code fences — keep inner content, remove fence markers
    text = _FENCE_INNER_RE.sub(lambda m: m.group(1).strip("\n"), text)

    # 2) Inline code — backtick-wrapped
    text = _INLINE_CODE_RE.sub(r"\1", text)

    # 3) Bold — **text**, ___text___ or __text__
    text = _BOLD_DBL_RE.sub(r"\1", text)
    text = _BOLD_TRI_RE.sub(r"\1", text)
    text = _BOLD_UND_RE.sub(r"\1", text)

    # 3.5) Italic — *text* only.
    # Underscore _italic_ is intentionally NOT processed: in plain-text
    # output the underscores belong to snake_case identifiers (file_name,
    # my_var) far more often than to italic emphasis.  Stripping them
    # corrupts code identifiers; leaving them is harmless because Feishu
    # text messages do not render _italic_ anyway.
    text = _ITALIC_RE.sub(r"\1", text)

    # 4) Strikethrough — ~~text~~ → text
    text = _STRIKE_RE.sub(r"\1", text)

    # 5) Underline — <u>text</u> → text
    text = _UNDERLINE_TAG_RE.sub(r"\1", text)

    # 6) Headings — # heading → heading
    text = _HEADING_STRIP_RE.sub("", text)

    # 7) Lists — - item / * item → • item
    text = _LIST_BULLET_RE.sub("• ", text)

    # 8) Tables — | col1 | col2 | → col1  col2
    text = _PIPE_REPLACE_RE.sub(" ", text)
    # Clean up separator rows (--- or :---: or ---:)
    text = _SEP_ROW_CLEAN_RE.sub("", text)

    # 9) Links — [text](url) → text (url)
    text = _LINK_EXPAND_RE.sub(r"\1 (\2)", text)

    # 10) Blockquotes — > text → text
    text = _BLOCKQUOTE_STRIP_RE.sub("", text)

    # 10.5) Horizontal rules
    text = _HR_RE.sub("", text)

    # 11) Compress inline whitespace (spaces/tabs) to single space, but
    #     preserve newlines so multi-line plain-text messages keep their
    #     structure. Then clean up 3+ consecutive newlines to max 2.
    text = _WS_COMPRESS_RE.sub(" ", text)
    text = _MULTI_NEWLINE_RE.sub("\n\n", text).strip()

    return text


def _segment_lines(lines: list[str]) -> list[list[str]]:
    """Segment lines into blocks, keeping code fences atomic."""
    blocks = []
    current = []
    in_fence = False
    for line in lines:
        stripped = line.strip()
        is_fence = bool(_FENCE_RE.match(stripped))
        if is_fence and not in_fence:
            if current:
                blocks.append(current)
            current = [line]
            in_fence = True
        elif is_fence and in_fence:
            current.append(line)
            blocks.append(current)
            current = []
            in_fence = False
        elif not stripped and not in_fence:
            if current:
                blocks.append(current)
                current = []
        else:
            current.append(line)
    if current:
        blocks.append(current)
    return blocks


def _split_heading_and_table(block_lines: list[str]) -> list[list[str]]:
    """If a block contains a heading followed by a table, split them.

    Handles the common case: ``# Title\\n| col |...`` with no blank line.
    """
    for i, line in enumerate(block_lines):
        stripped = line.strip()
        # Check if this line is a heading
        if _HEADING_NORMALIZE_RE.match(stripped):
            # Check if the NEXT line starts a table
            if i + 1 < len(block_lines) and block_lines[i + 1].strip().startswith("|"):
                return [block_lines[:i + 1], block_lines[i + 1:]]
    return [block_lines]


def _normalize_heading(match: re.Match) -> str:
    """Normalize heading to h3+bold, stripping existing wrapping bold markers."""
    text = match.group(1).strip()
    # Strip wrapping **...** to avoid nesting: **Title** → Title
    if text.startswith("**") and text.endswith("**") and len(text) >= 4:
        text = text[2:-2].strip()
    # Strip wrapping __text__ (underscore bold)
    elif text.startswith("__") and text.endswith("__") and len(text) >= 4:
        text = text[2:-2].strip()
    return f"### {text}"


def _build_elements_from_content(content: str) -> list[dict]:
    """Build card elements from content string."""
    # 1) Protect code blocks from heading normalization
    parts = _FENCE_SPLIT_RE.split(content)
    for i in range(len(parts)):
        if parts[i].strip().startswith(("```", "~~~")):
            continue  # fenced block → preserve as-is
        parts[i] = _HEADING_NORMALIZE_RE.sub(_normalize_heading, parts[i])
    body = "".join(parts)

    # 2) Segment into logical blocks, then split heading+table combos
    lines = body.split("\n")
    blocks = _segment_lines(lines)
    expanded = []
    for block in blocks:
        expanded.extend(_split_heading_and_table(block))
    paragraphs = ["\n".join(b) for b in expanded]

    elements = []
    md_idx = 0
    hr_idx = 0
    tbl_idx = 0
    cs_idx = 0
    first_heading = True
    skip_next = False

    for i, p in enumerate(paragraphs):
        if skip_next:
            skip_next = False
            continue
        p_stripped = p.strip()
        if not p_stripped:
            continue

        # --- horizontal rule (---, ***, ___) ---
        if _HR_RE.match(p_stripped):
            elements.append({"tag": "hr", "element_id": f"hr_{hr_idx}", "margin": _HR_MARGIN})
            hr_idx += 1
            continue

        # --- markdown pipe table → native Feishu table component ---
        native_tbl = _parse_markdown_table(p_stripped)
        if native_tbl:
            native_tbl["element_id"] = f"tbl_{tbl_idx}"
            elements.append(native_tbl)
            tbl_idx += 1
            # element_id prefixes (el_/tbl_/hr_) are namespace-isolated —
            # tables don't consume the el_ counter.
            continue

        # --- heading (### text) → markdown bold (heading component not supported by Feishu API) ---
        heading_match = _H3_MATCH_RE.match(p_stripped)
        if heading_match:
            heading_text = heading_match.group(1).strip()
            # KPI indicator card: metric-emoji heading + "- **label**: value"
            # list (2-4 items) → native column_set. Conservative on purpose:
            # plain lists or non-metric emojis never trigger this. The list
            # may sit on the same paragraph (no blank line) or the next one.
            kpi_items = None
            kpi_from_next_para = False
            if heading_text[:1] in _KPI_HEADING_EMOJIS:
                rest_of_para = p_stripped[heading_match.end():].strip()
                if rest_of_para:
                    kpi_items = _parse_kpi_items(rest_of_para)
                if not kpi_items and i + 1 < len(paragraphs):
                    kpi_items = _parse_kpi_items(paragraphs[i + 1])
                    kpi_from_next_para = kpi_items is not None
            if kpi_items and 2 <= len(kpi_items) <= 4:
                elements.append(_build_kpi_column_set(kpi_items, cs_idx))
                cs_idx += 1
                first_heading = False
                skip_next = kpi_from_next_para
                continue
            # First heading has no top margin
            margin = "0px 0px 6px 0px" if first_heading else _HEADING_MARGIN
            first_heading = False
            elements.append({
                "tag": "markdown",
                "element_id": f"el_{md_idx}",
                "content": f"**{heading_text}**",
                "margin": margin,
                "data_type": _DATA_TYPE_HEADING,  # marker for title dedup & HR filtering
            })
            md_idx += 1
            # If paragraph has trailing content after heading, emit as markdown
            remaining = p_stripped[heading_match.end():].strip()
            if remaining:
                elements.append({"tag": "markdown", "element_id": f"el_{md_idx}", "content": remaining, "margin": _DEFAULT_ELEMENT_MARGIN})
                md_idx += 1
            continue

        # --- blockquote with emoji → div+icon callout (v2 native) ---
        if p_stripped.startswith(">"):
            # Strip "> " prefix from every line (handles multi-line blockquotes).
            # Only strip ">" when followed by space or at line end — prevents
            # eating legitimate ">" in content like ">5度" or "a>=b".
            bq_lines = []
            for line in p_stripped.split("\n"):
                s = line
                while s.startswith("> "):
                    s = s[2:]
                if s == ">":
                    s = ""
                bq_lines.append(s.strip())
            bq_content = "\n".join(bq_lines).strip()
            _ei = _match_emoji_icon(bq_content)
            if _ei:
                emoji, color, icon_token = _ei
                rest = bq_content[len(emoji):].strip()
                elements.append({
                    "tag": "div",
                    "element_id": f"el_{md_idx}",
                    "icon": {
                        "tag": "standard_icon",
                        "token": icon_token,
                        "color": color,
                    },
                    "text": {
                        "tag": "lark_md",
                        "content": rest or emoji,
                    },
                    "margin": _CALLOUT_MARGIN,
                })
                md_idx += 1
            else:
                # Non-emoji blockquote: output as plain markdown (no visual
                # prefix). The old ▎ vertical-bar prefix was removed because
                # it looked heavy on multi-line quotes.
                elements.append({"tag": "markdown", "element_id": f"el_{md_idx}", "content": bq_content, "margin": _DEFAULT_ELEMENT_MARGIN})
                md_idx += 1
            continue

        elements.append({"tag": "markdown", "element_id": f"el_{md_idx}", "content": p, "margin": _DEFAULT_ELEMENT_MARGIN})
        md_idx += 1

    if not elements:
        elements = [{"tag": "markdown", "element_id": "el_0", "content": body, "margin": _DEFAULT_ELEMENT_MARGIN}]

    # Filter out redundant HR elements: those immediately before a heading
    # or a table (both already have their own top margin for visual
    # separation) and any trailing HR at the end of the card.
    def _is_heading_el(el: dict) -> bool:
        return el.get("data_type") == _DATA_TYPE_HEADING

    def _is_block_el(el: dict) -> bool:
        """Heading or table — both carry top margin, so an HR right before
        them is redundant."""
        return _is_heading_el(el) or _is_table(el)

    filtered = []
    for i, el in enumerate(elements):
        if el["tag"] == "hr":
            next_el = elements[i + 1] if i + 1 < len(elements) else None
            if next_el is None or _is_block_el(next_el):
                continue
        filtered.append(el)

    return filtered


def _is_table(el: dict) -> bool:
    """Check if a card element is a native Feishu table component."""
    return el.get("tag") == "table"


def _truncate_to_bytes(text: str, max_bytes: int) -> str:
    """Truncate text to fit within max_bytes when UTF-8 encoded."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    # Truncate at character boundaries
    truncated = encoded[:max_bytes]
    # Walk back to avoid splitting a multi-byte character.
    # Start from len(truncated) to try the full slice first — if the
    # truncation point happens to land on a valid UTF-8 boundary, we
    # return the entire max_bytes worth of content.
    for cut in range(len(truncated), max(0, len(truncated) - 4), -1):
        try:
            return truncated[:cut].decode("utf-8")
        except UnicodeDecodeError:
            continue
    return ""


def _truncate_element_content(el: dict, max_content_bytes: int, notice: str) -> dict:
    """Truncate a single element's text content to fit within a byte budget.

    Handles three element shapes:
    - markdown: content lives in el["content"]
    - div (callout): content lives in el["text"]["content"]
    - table: returns el unchanged (caller must handle row-truncation)

    Returns a *new* dict (shallow copy with modified content) so the original
    element is never mutated.
    """
    el = el.copy()  # shallow copy — safe because we only replace leaf strings

    if el.get("tag") == "table":
        return el  # tables have no text content to truncate; use row-truncation instead
    if el.get("tag") == "div" and "text" in el:
        original = el["text"].get("content", "")
    else:
        original = el.get("content", "")

    truncated = _truncate_to_bytes(original, max_content_bytes)
    if truncated == original:
        # Nothing was actually cut — return the element unchanged so a
        # caller truncating a group never *grows* it by appending the notice.
        return el
    if el.get("tag") == "div" and "text" in el:
        el["text"] = {**el["text"], "content": truncated + notice}
    else:
        el["content"] = truncated + notice
    return el


def _make_table_truncation_notice(seq: int) -> dict:
    """Build a table-truncation notice with a unique element_id.

    *seq* is the caller's per-card counter — multiple truncated tables in
    the same card must not share an element_id (Feishu uses element_id to
    target elements for card updates).
    """
    return {
        "tag": "markdown",
        "element_id": f"trunc_tbl_{seq}",
        "content": "⚠️ [表格内容过长，无法显示]",
        "margin": _DEFAULT_ELEMENT_MARGIN,
    }


def _truncate_table_rows(
    el: dict, byte_tester, max_bytes: int
) -> dict | None:
    """Binary-search the maximum number of table rows that fit within max_bytes.

    Args:
        el: The table element dict (shallow-copied internally).
        byte_tester: Callable[[int], int] — given a row count, returns the
            byte size of the card JSON with that many rows.
        max_bytes: Maximum allowed byte size.

    Returns the truncated element (copy with reduced rows), or None if even
    one row exceeds the limit (caller should use
    _make_table_truncation_notice(seq)).
    """
    rows = list(el.get("rows", []))
    lo, hi = 0, len(rows)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if byte_tester(mid) <= max_bytes:
            lo = mid
        else:
            hi = mid - 1
    if lo > 0:
        result = el.copy()
        result["rows"] = rows[:lo]
        result["page_size"] = min(lo, 5)
        return result
    return None


def _truncate_oversized_group(
    group: list[dict], max_bytes: int, build_fn
) -> list[dict]:
    """Truncate content so build_fn(group) fits within *max_bytes*.

    Each round shrinks the current largest element (by serialized size):
    tables lose rows (binary search via _truncate_table_rows), everything
    else loses text content (_truncate_element_content). Rounds repeat
    until the group fits or nothing shrinks anymore (every element at its
    floor). *build_fn* takes the candidate group and returns its serialized
    byte size — the caller supplies it so sibling elements and header
    fields stay in the budget.

    Returns a new group; the input is not mutated.
    """
    group = list(group)
    trunc_notice_seq = 0
    for _ in range(len(group)):
        if build_fn(group) <= max_bytes:
            break
        largest_idx = max(
            range(len(group)),
            key=lambda j: _json_len(group[j]),
        )
        el = group[largest_idx]
        if _is_table(el):
            result = _truncate_table_rows(
                el,
                byte_tester=lambda n: build_fn(
                    group[:largest_idx] + [{**el, "rows": el.get("rows", [])[:n]}]
                    + group[largest_idx + 1:],
                ),
                max_bytes=max_bytes,
            )
            if result is None:
                trunc_notice_seq += 1
                group[largest_idx] = _make_table_truncation_notice(trunc_notice_seq)
            else:
                group[largest_idx] = result
            continue
        notice = _CONTENT_TRUNCATION_NOTICE
        notice_bytes = len(notice.encode("utf-8"))
        el_cleared = el.copy()
        if el_cleared.get("tag") == "div" and "text" in el_cleared:
            el_cleared["text"] = {**el_cleared["text"], "content": ""}
        else:
            el_cleared["content"] = ""
        overhead = build_fn(
            group[:largest_idx] + [el_cleared] + group[largest_idx + 1:],
        )
        max_content = max(
            max_bytes - overhead - notice_bytes - _TRUNCATE_SAFETY_MARGIN,
            _TRUNCATE_MIN_CONTENT_BYTES,
        )
        before = _json_len(el)
        group[largest_idx] = _truncate_element_content(el, max_content, notice)
        after = _json_len(group[largest_idx])
        if after >= before:
            # Largest element is already at its floor — further rounds would
            # only re-pick it (or smaller ones) with no gain. Stop.
            break
    return group


def _split_card_elements(elements: list[dict], header_budget: int = 0) -> list[list[dict]]:
    """Split elements into groups fitting _MAX_CARD_BYTES.

    Elements are never split mid-element; oversized single elements are
    truncated via _truncate_oversized_group (tables lose rows, other
    elements lose text content). The caller (_maybe_split_cards) adds
    headers and page indicators, so we reserve a header budget via
    header_budget to avoid post-split overflow.
    """
    # Effective per-card limit after reserving space for header+page indicators
    max_bytes = _DEFAULT_MAX_CARD_BYTES - header_budget

    # Incremental size estimation (O(n)) instead of re-serializing the whole
    # card on every element (O(n^2) — 50 elements used to trigger ~1275 full
    # serializations). We approximate each group's size as a fixed card-frame
    # overhead plus the sum of per-element JSON byte sizes (+2 for the ", "
    # list separator json.dumps inserts between array elements). This is a
    # conservative upper bound (elements still carry their data_type marker
    # here, which _build_card_json strips), so a group sized by this
    # estimate never exceeds the limit once serialized. The post-processing
    # pass below re-serializes single-element groups to catch any residual
    # oversize; multi-element overflow is caught by _maybe_split_cards'
    # safety net.
    el_sizes = [
        _json_len(el) + 2 for el in elements
    ]
    card_overhead = len(_build_card_json([]).encode("utf-8"))

    groups: list[list[dict]] = []
    current_group: list[dict] = []
    current_bytes = card_overhead
    table_count = 0

    for el, el_size in zip(elements, el_sizes):
        is_tbl = _is_table(el)
        size_ok = current_bytes + el_size <= max_bytes
        tables_ok = not (is_tbl and table_count >= _MAX_TABLES_PER_CARD)
        if size_ok and tables_ok:
            current_group.append(el)
            current_bytes += el_size
            if is_tbl:
                table_count += 1
        else:
            # When a table would exceed the per-card limit, split to a new card
            # instead of degrading it to a code block.
            if current_group:
                groups.append(current_group)
            current_group = [el]
            current_bytes = card_overhead + el_size
            table_count = 1 if is_tbl else 0

    if current_group:
        groups.append(current_group)

    # Post-process: detect oversized single-element groups that cannot be
    # split further and truncate their content so the card JSON fits.
    for i, group in enumerate(groups):
        if len(group) == 1 and len(_build_card_json(group).encode("utf-8")) > max_bytes:
            # NOTE: g is the lambda parameter (not a loop var); the helper is
            # invoked synchronously, so captured values stay stable.
            groups[i] = _truncate_oversized_group(
                group, max_bytes,
                lambda g: len(_build_card_json(g).encode("utf-8")),
            )

    return groups


def _build_card_json(elements: list[dict], title: str = "", subtitle: str = "", tags: list[str] | None = None, template: str = "blue", icon: dict | None = None) -> str:
    """Build a complete card JSON string (schema 2.0, with optional header).

    Args:
        elements: List of card element dicts
        title: Main header title
        subtitle: Optional subtitle below title
        tags: Optional list of tags (max 3) shown after title
        template: Header color template
        icon: Optional header icon (standard_icon dict) derived from a
            leading emoji in the title
    """
    if not elements:
        elements = [{"tag": "markdown", "content": "", "element_id": "ph_0"}]

    # Strip internal-only fields (data_type, etc.) before serialization so
    # they don't leak into the Feishu API payload.
    clean_elements = []
    for el in elements:
        clean_el = {k: v for k, v in el.items() if not k.startswith("_") and k != "data_type"}
        clean_elements.append(clean_el)
    elements = clean_elements

    card = {
        "schema": "2.0",
        "config": {
            "width_mode": "fill",
            "update_multi": True,  # explicit shared-card declaration (2.0 default)
        },
        "body": {
            "direction": "vertical",
            "padding": "12px 8px 12px 8px",  # 4-value format (top right bottom left)
            "horizontal_align": "left",
            "vertical_spacing": "3px",
            "vertical_align": "top",  # official default
            "elements": elements,
        },
    }

    # Add header if title is provided
    if title:
        header = {
            "title": {
                "tag": "plain_text",
                "content": title
            },
            "template": template
        }

        # Header icon (title leading emoji → standard_icon token)
        if icon:
            header["icon"] = icon

        # Subtitle
        if subtitle:
            header["subtitle"] = {
                "tag": "plain_text",
                "content": subtitle
            }

        # Tags (max 3). Defensive truncation: the sole production source
        # (_extract_tags) already caps at 3, but build_card_payloads is a
        # public API and direct callers may pass longer lists.
        if tags:
            if len(tags) > 3:
                _logger.warning("tags list has %d items, truncating to 3", len(tags))
                tags = tags[:3]
            header["text_tag_list"] = [
                {
                    "tag": "text_tag",
                    "text": {"tag": "plain_text", "content": t},
                    "color": _TAG_COLORS[i % len(_TAG_COLORS)]
                }
                for i, t in enumerate(tags)
            ]

        card["header"] = header

    return json.dumps(card, ensure_ascii=False)


def build_card_payloads(content: str, force_default_title: bool = False, subtitle: str = "", tags: list[str] | None = None) -> list[str]:
    """Build one or more Feishu interactive card (JSON 2.0 schema) payloads.

    If the content fits in a single card, returns [card_json].
    If it exceeds _MAX_CARD_BYTES, splits into multiple card JSONs.
    force_default_title: if True and no heading is auto-detected, inject a
                default title so the card gets a header with template color.
                If a heading IS detected, a header is always added regardless.
    subtitle: Optional subtitle below title
    tags: Optional list of tags (max 3) shown after title
    """
    # Sanitize content before processing
    content = _sanitize_content(content)

    elements = _build_elements_from_content(content)
    # Only add a card header when force_default_title is True (i.e. content
    # length > _HEADER_LENGTH_THRESHOLD). Below that threshold the heading
    # text still renders as bold inside the card body, just without the
    # colored header bar — keeping short cards visually clean.
    if force_default_title:
        title = _extract_title(content)
        template = _select_template(title, content)
        icon = None
        if not title:
            title = "📋 内容"
        else:
            # Header icon: mapped leading emoji (via _EMOJI_COLOR_MAP) in the
            # title → standard_icon token; unmapped emojis (e.g. 📊) stay in
            # the title. The mapped emoji is stripped to avoid double display;
            # a title that was ONLY the emoji keeps its original text.
            _ei = _match_emoji_icon(title)
            if _ei:
                emoji, _color, _token = _ei
                icon = {
                    "tag": "standard_icon",
                    "token": _token,
                    "color": _color,
                }
                title = title[len(emoji):].strip() or title
            # Dedup: remove first heading element from body since it's in header.
            # Use data_type marker instead of fragile content/margin string matching.
            for i, el in enumerate(elements):
                if el.get("data_type") == _DATA_TYPE_HEADING:
                    del elements[i]
                    break
            # Dedup: the subtitle heading is shown in the header too — remove
            # it from the body. Match strictly when the subtitle is intact
            # (avoids "每日" matching "每日详细分析"); fall back to a prefix
            # match only when the subtitle was truncated at 60 chars.
            if subtitle:
                # Dedup: compare strip_markdown-normalized text on both sides
                # so that a heading with inline markdown (e.g. "**每日 **运营**
                # 汇报**") still matches the subtitle ("每日 运营 汇报", already
                # strip_markdown'd by _extract_subtitle).  Prefix match is used
                # when the subtitle was truncated at 60 chars.
                _sub_norm = subtitle
                _sub_truncated = len(subtitle) >= 60
                for i, el in enumerate(elements):
                    if el.get("data_type") != _DATA_TYPE_HEADING:
                        continue
                    _el_norm = strip_markdown(el.get("content", ""))
                    if _el_norm == _sub_norm or (_sub_truncated and _el_norm.startswith(_sub_norm)):
                        del elements[i]
                        break
    else:
        title = ""
        template = "blue"
        icon = None
    return _maybe_split_cards(elements, title, subtitle, tags, template, icon)


def _maybe_split_cards(elements: list[dict], title: str = "", subtitle: str = "", tags: list[str] | None = None, template: str = "blue", icon: dict | None = None) -> list[str]:
    """Split oversized cards and inject page indicators."""
    card_json = _build_card_json(elements, title, subtitle, tags, template, icon)
    table_count = sum(1 for el in elements if _is_table(el))
    if len(card_json.encode("utf-8")) <= _DEFAULT_MAX_CARD_BYTES and table_count <= _MAX_TABLES_PER_CARD:
        return [card_json]

    # H2 fix: reserve byte budget for header (first card only) and page
    # indicators (every card) so post-split cards don't exceed the limit.
    # Measure the actual header size from a probe card.
    header_probe = _build_card_json([], title, subtitle, tags, template, icon)
    header_overhead = len(header_probe.encode("utf-8")) - len(_build_card_json([]).encode("utf-8"))
    # Measure actual page indicator element size (was hardcoded 60, actual ≈ 79B)
    page_indicator_probe = {"tag": "markdown", "content": "📋 **(99/99)**", "margin": "0px 0px 4px 0px"}
    page_indicator_bytes = _json_len(page_indicator_probe)
    header_budget = header_overhead + page_indicator_bytes

    groups = _split_card_elements(elements, header_budget=header_budget)
    n = len(groups)
    cards = []
    for idx, group in enumerate(groups):
        if n > 1:
            group = [{"tag": "markdown", "content": f"📋 **({idx + 1}/{n})**", "margin": "0px 0px 4px 0px"}] + group
        # Only add header to first card
        card_title = title if idx == 0 else ""
        card_subtitle = subtitle if idx == 0 else ""
        card_tags = tags if idx == 0 else None
        card_template = template if idx == 0 else "blue"
        card_icon = icon if idx == 0 else None
        card_json_i = _build_card_json(group, card_title, card_subtitle, card_tags, card_template, card_icon)
        # Safety net: if the card still exceeds the limit (e.g. header was
        # larger than estimated), truncate the largest element.
        if len(card_json_i.encode("utf-8")) > _DEFAULT_MAX_CARD_BYTES:
            _logger.warning("Card %d/%d exceeds limit after split (%d > %d), truncating",
                            idx + 1, n, len(card_json_i.encode("utf-8")), _DEFAULT_MAX_CARD_BYTES)
            # NOTE: card_title/subtitle/tags/template are captured per round
            # and read synchronously inside the helper — safe by construction.
            group = _truncate_oversized_group(
                group, _DEFAULT_MAX_CARD_BYTES,
                lambda g: len(_build_card_json(
                    g, card_title, card_subtitle, card_tags, card_template, card_icon,
                ).encode("utf-8")),
            )
            card_json_i = _build_card_json(group, card_title, card_subtitle, card_tags, card_template, card_icon)
            if len(card_json_i.encode("utf-8")) > _DEFAULT_MAX_CARD_BYTES:
                # Theoretically unreachable (element frame overhead is far
                # below the card budget). Defensive last resort: emit a
                # minimal truncation card instead of an oversized one.
                _logger.error("Card %d/%d still exceeds limit after truncation (%d), "
                              "degrading to truncation notice",
                              idx + 1, n, len(card_json_i.encode("utf-8")))
                card_json_i = _build_card_json(
                    [{"tag": "markdown", "element_id": "fatal_trunc",
                      "content": "⚠️ [内容过长，无法完整显示]",
                      "margin": _DEFAULT_ELEMENT_MARGIN}],
                    card_title, card_subtitle, card_tags, card_template, card_icon,
                )
        cards.append(card_json_i)
    return cards

