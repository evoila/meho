# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Bidirectional ADF-Markdown Converter.

Converts between Atlassian Document Format (ADF) and Markdown.
Shared between Jira (Phase 42) and Confluence (Phase 43).

Hand-rolled converter (no PyPI dependency) with full control over
both directions. Supports headers, bold/italic/strikethrough, code
(inline + fenced), bullet/numbered lists, links, tables, blockquotes,
and horizontal rules.

ADF spec: https://developer.atlassian.com/cloud/jira/platform/apis/document/structure/
"""

import re
from typing import Any

# ---------------------------------------------------------------------------
# ADF -> Markdown (READ direction: agent reads clean markdown)
# ---------------------------------------------------------------------------


def adf_to_markdown(adf: dict | None) -> str:
    """
    Convert Atlassian Document Format to markdown text.

    Walks the ADF document tree and converts each block node to
    its markdown equivalent. Unknown node types are rendered as
    plain text when possible, skipped silently otherwise.

    Args:
        adf: ADF document dict with type="doc" and content array

    Returns:
        Markdown string
    """
    if not adf or not isinstance(adf, dict):
        return ""
    if adf.get("type") != "doc":
        return ""

    content = adf.get("content", [])
    if not content:
        return ""

    blocks = []
    for node in content:
        block = _convert_block_node(node)
        if block is not None:
            blocks.append(block)

    return "\n\n".join(blocks)


def _convert_block_node(node: dict, indent_level: int = 0) -> str | None:
    """Convert a single ADF block node to markdown."""
    node_type = node.get("type", "")

    if node_type == "paragraph":
        return _convert_paragraph(node)
    elif node_type == "heading":
        return _convert_heading(node)
    elif node_type == "codeBlock":
        return _convert_code_block(node)
    elif node_type == "bulletList":
        return _convert_bullet_list(node, indent_level)
    elif node_type == "orderedList":
        return _convert_ordered_list(node, indent_level)
    elif node_type == "blockquote":
        return _convert_blockquote(node)
    elif node_type == "table":
        return _convert_table(node)
    elif node_type == "rule":
        return "---"
    elif node_type == "mediaSingle" or node_type == "mediaGroup":
        return None  # Skip media nodes silently
    elif node_type == "panel":
        return _convert_panel(node)
    elif node_type in ("extension", "bodiedExtension"):
        return _convert_extension(node)
    else:
        # Unknown block type -- try to extract text content
        text = _extract_inline_text(node.get("content", []))
        return text if text else None


def _convert_paragraph(node: dict) -> str:
    """Convert paragraph node to inline text."""
    return _extract_inline_text(node.get("content", []))


def _convert_heading(node: dict) -> str:
    """Convert heading node to markdown heading."""
    level = node.get("attrs", {}).get("level", 1)
    text = _extract_inline_text(node.get("content", []))
    return f"{'#' * level} {text}"


def _convert_code_block(node: dict) -> str:
    """Convert codeBlock node to fenced code block."""
    language = node.get("attrs", {}).get("language", "")
    content = node.get("content", [])
    code_text = "".join(n.get("text", "") for n in content if n.get("type") == "text")
    return f"```{language}\n{code_text}\n```"


def _convert_bullet_list(node: dict, indent_level: int = 0) -> str:
    """Convert bulletList node to markdown bullet list."""
    items = []
    indent = "  " * indent_level
    for item in node.get("content", []):
        if item.get("type") == "listItem":
            item_lines = _convert_list_item(item, indent_level)
            items.append(f"{indent}- {item_lines}")
    return "\n".join(items)


def _convert_ordered_list(node: dict, indent_level: int = 0) -> str:
    """Convert orderedList node to markdown numbered list."""
    items = []
    indent = "  " * indent_level
    for idx, item in enumerate(node.get("content", []), start=1):
        if item.get("type") == "listItem":
            item_lines = _convert_list_item(item, indent_level)
            items.append(f"{indent}{idx}. {item_lines}")
    return "\n".join(items)


def _convert_list_item(item: dict, indent_level: int) -> str:
    """Convert a listItem's content, handling nested lists."""
    parts = []
    for child in item.get("content", []):
        child_type = child.get("type", "")
        if child_type == "paragraph":
            parts.append(_convert_paragraph(child))
        elif child_type == "bulletList":
            parts.append("\n" + _convert_bullet_list(child, indent_level + 1))
        elif child_type == "orderedList":
            parts.append("\n" + _convert_ordered_list(child, indent_level + 1))
        else:
            block = _convert_block_node(child, indent_level + 1)
            if block:
                parts.append(block)
    return "".join(parts)


def _convert_blockquote(node: dict) -> str:
    """Convert blockquote node to markdown blockquote."""
    inner_blocks = []
    for child in node.get("content", []):
        block = _convert_block_node(child)
        if block is not None:
            inner_blocks.append(block)
    inner_text = "\n\n".join(inner_blocks)
    # Prefix each line with >
    lines = inner_text.split("\n")
    return "\n".join(f"> {line}" for line in lines)


def _convert_table(node: dict) -> str:
    """
    Convert table node to GitHub-flavored markdown table.

    First row treated as header, followed by separator row.
    """
    rows = node.get("content", [])
    if not rows:
        return ""

    md_rows = []
    for row in rows:
        cells = []
        for cell in row.get("content", []):
            # cell is tableHeader or tableCell
            cell_content = []
            for child in cell.get("content", []):
                block = _convert_block_node(child)
                if block:
                    cell_content.append(block)
            cells.append(" ".join(cell_content))
        md_rows.append("| " + " | ".join(cells) + " |")

    if len(md_rows) >= 1:
        # Insert separator after first row (header)
        num_cols = md_rows[0].count("|") - 1
        separator = "| " + " | ".join(["---"] * num_cols) + " |"
        md_rows.insert(1, separator)

    return "\n".join(md_rows)


def _convert_panel(node: dict) -> str:
    """Convert panel node (info/warning/etc.) to blockquote."""
    inner_blocks = []
    for child in node.get("content", []):
        block = _convert_block_node(child)
        if block is not None:
            inner_blocks.append(block)
    inner_text = "\n\n".join(inner_blocks)
    lines = inner_text.split("\n")
    return "\n".join(f"> {line}" for line in lines)


def _convert_extension(node: dict) -> str | None:
    """
    Convert Confluence extension/bodiedExtension nodes (macros) to markdown.

    Handles common Confluence macros:
    - code -> fenced code block with language
    - info/warning/note/tip -> blockquote with prefix
    - expand -> <details> collapsible section
    - Unknown macros -> [Macro: extensionKey] placeholder
    """
    attrs = node.get("attrs", {})
    extension_key = attrs.get("extensionKey", "")
    macro_params = attrs.get("parameters", {}).get("macroParams", {})

    if extension_key == "code":
        language = macro_params.get("language", {}).get("value", "")
        code_text = _extract_extension_body_text(node)
        return f"```{language}\n{code_text}\n```"
    elif extension_key in ("info", "warning", "note", "tip"):
        prefix_map = {
            "info": "INFO",
            "warning": "WARNING",
            "note": "NOTE",
            "tip": "TIP",
        }
        prefix = prefix_map.get(extension_key, extension_key.upper())
        body_text = _extract_extension_body_text(node)
        lines = body_text.split("\n")
        return "\n".join(
            f"> **{prefix}:** {line}" if i == 0 else f"> {line}" for i, line in enumerate(lines)
        )
    elif extension_key == "expand":
        title = macro_params.get("title", {}).get("value", "Details")
        body_text = _extract_extension_body_text(node)
        return f"<details>\n<summary>{title}</summary>\n\n{body_text}\n</details>"
    else:
        return f"[Macro: {extension_key}]"


def _extract_extension_body_text(node: dict) -> str:
    """
    Recursively extract text from an extension node's content array.

    bodiedExtension nodes have a content array with standard ADF block nodes.
    Non-bodied extension nodes may have parameters but no content.
    """
    content = node.get("content", [])
    if not content:
        return ""

    blocks = []
    for child in content:
        block = _convert_block_node(child)
        if block is not None:
            blocks.append(block)

    return "\n\n".join(blocks)


def _extract_inline_text(content: list[dict]) -> str:
    """Extract and format inline nodes as markdown text."""
    if not content:
        return ""

    parts = []
    for node in content:
        node_type = node.get("type", "")

        if node_type == "text":
            text = node.get("text", "")
            marks = node.get("marks", [])
            text = _apply_marks(text, marks)
            parts.append(text)
        elif node_type == "hardBreak":
            parts.append("\n")
        elif node_type == "inlineCard":
            url = node.get("attrs", {}).get("url", "")
            parts.append(f"[{url}]({url})")
        elif node_type == "mention":
            text = node.get("attrs", {}).get("text", "@user")
            parts.append(text)
        elif node_type == "emoji":
            short_name = node.get("attrs", {}).get("shortName", "")
            parts.append(short_name)
        elif node_type == "status":
            status_text = node.get("attrs", {}).get("text", "")
            parts.append(f"[STATUS: {status_text}]")
        else:
            # Unknown inline -- try to get text
            text = node.get("text", "")
            if text:
                parts.append(text)

    return "".join(parts)


def _apply_marks(text: str, marks: list[dict]) -> str:
    """Apply ADF marks (bold, italic, code, link, strike) to text."""
    if not marks:
        return text

    for mark in marks:
        mark_type = mark.get("type", "")
        if mark_type == "strong":
            text = f"**{text}**"
        elif mark_type == "em":
            text = f"*{text}*"
        elif mark_type == "code":
            text = f"`{text}`"
        elif mark_type == "strike":
            text = f"~~{text}~~"
        elif mark_type == "link":
            href = mark.get("attrs", {}).get("href", "")
            text = f"[{text}]({href})"

    return text


# ---------------------------------------------------------------------------
# Markdown -> ADF (WRITE direction: agent writes markdown, API needs ADF)
# ---------------------------------------------------------------------------


def markdown_to_adf(markdown: str) -> dict:
    """
    Convert markdown text to Atlassian Document Format.

    Parses markdown block by block (headings, lists, code blocks,
    blockquotes, tables, horizontal rules, paragraphs) and converts
    each to the corresponding ADF node. Inline formatting (bold,
    italic, code, links, strikethrough) is parsed within text blocks.

    Args:
        markdown: Markdown text

    Returns:
        ADF document dict with version=1, type="doc"
    """
    if not markdown or not markdown.strip():
        return {"version": 1, "type": "doc", "content": []}

    lines = markdown.split("\n")
    blocks = _parse_blocks(lines)

    # Filter out empty paragraphs
    content = [b for b in blocks if b is not None]

    return {"version": 1, "type": "doc", "content": content}


def _parse_blocks(lines: list[str]) -> list[dict]:  # NOSONAR (cognitive complexity)
    """Parse markdown lines into ADF block nodes."""
    blocks: list[dict] = []
    i = 0

    while i < len(lines):
        line = lines[i]

        # Empty line -- skip
        if not line.strip():
            i += 1
            continue

        # Fenced code block
        if line.strip().startswith("```"):
            block, i = _parse_code_block(lines, i)
            blocks.append(block)
            continue

        # Heading
        heading_match = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading_match:
            level = len(heading_match.group(1))
            text = heading_match.group(2)
            blocks.append(
                {
                    "type": "heading",
                    "attrs": {"level": level},
                    "content": _parse_inline(text),
                }
            )
            i += 1
            continue

        # Horizontal rule
        if re.match(r"^(\-{3,}|\*{3,}|_{3,})$", line.strip()):
            blocks.append({"type": "rule"})
            i += 1
            continue

        # Bullet list
        if re.match(r"^(\s*)([-*])\s+", line):
            block, i = _parse_bullet_list(lines, i)
            blocks.append(block)
            continue

        # Ordered list
        if re.match(r"^(\s*)\d+\.\s+", line):
            block, i = _parse_ordered_list(lines, i)
            blocks.append(block)
            continue

        # Blockquote
        if line.startswith("> ") or line == ">":
            block, i = _parse_blockquote(lines, i)
            blocks.append(block)
            continue

        # Table
        if "|" in line and re.match(r"^\|.*\|$", line.strip()):
            block, i = _parse_table(lines, i)
            if block:
                blocks.append(block)
            continue

        # Regular paragraph
        block, i = _parse_paragraph(lines, i)
        if block:
            blocks.append(block)
        continue

    return blocks


def _parse_code_block(lines: list[str], start: int) -> tuple:
    """Parse a fenced code block starting at `start`."""
    first_line = lines[start].strip()
    # Extract language from opening fence
    language = first_line[3:].strip()  # After ```

    code_lines = []
    i = start + 1
    while i < len(lines):
        if lines[i].strip() == "```":
            i += 1
            break
        code_lines.append(lines[i])
        i += 1

    code_text = "\n".join(code_lines)
    node: dict[str, Any] = {
        "type": "codeBlock",
        "attrs": {},
        "content": [{"type": "text", "text": code_text}] if code_text else [],
    }
    if language:
        node["attrs"]["language"] = language

    return node, i


def _parse_bullet_list(lines: list[str], start: int) -> tuple:
    """Parse a bullet list starting at `start`."""
    items: list[dict[str, Any]] = []
    i = start

    while i < len(lines):
        match = re.match(r"^(\s*)([-*])\s+(.*)$", lines[i])
        if not match:
            break

        indent = len(match.group(1))
        if indent > 0 and items:
            # Nested list -- handled by parent
            break

        text = match.group(3)
        items.append(
            {
                "type": "listItem",
                "content": [
                    {
                        "type": "paragraph",
                        "content": _parse_inline(text),
                    }
                ],
            }
        )
        i += 1

        # Check for nested list items
        nested_items = []
        while i < len(lines):
            nested_match = re.match(r"^(\s+)([-*])\s+(.*)$", lines[i])
            if nested_match and len(nested_match.group(1)) >= 2:
                nested_items.append(lines[i])
                i += 1
            else:
                break

        if nested_items:
            nested_block, _ = _parse_bullet_list(
                [re.sub(r"^\s{2}", "", line) for line in nested_items],
                0,
            )
            items[-1]["content"].append(nested_block)

    return {"type": "bulletList", "content": items}, i


def _parse_ordered_list(lines: list[str], start: int) -> tuple:
    """Parse an ordered list starting at `start`."""
    items: list[dict[str, Any]] = []
    i = start

    while i < len(lines):
        match = re.match(r"^(\s*)\d+\.\s+(.*)$", lines[i])
        if not match:
            break

        indent = len(match.group(1))
        if indent > 0 and items:
            break

        text = match.group(2)
        items.append(
            {
                "type": "listItem",
                "content": [
                    {
                        "type": "paragraph",
                        "content": _parse_inline(text),
                    }
                ],
            }
        )
        i += 1

        # Check for nested list items
        nested_items = []
        while i < len(lines):
            nested_match = re.match(r"^(\s+)\d+\.\s+(.*)$", lines[i])
            if nested_match and len(nested_match.group(1)) >= 2:
                nested_items.append(lines[i])
                i += 1
            else:
                break

        if nested_items:
            nested_block, _ = _parse_ordered_list(
                [re.sub(r"^\s{2}", "", line) for line in nested_items],
                0,
            )
            items[-1]["content"].append(nested_block)

    return {"type": "orderedList", "content": items}, i


def _parse_blockquote(lines: list[str], start: int) -> tuple:
    """Parse a blockquote starting at `start`."""
    quote_lines = []
    i = start

    while i < len(lines):
        if lines[i].startswith("> "):
            quote_lines.append(lines[i][2:])
            i += 1
        elif lines[i] == ">":
            quote_lines.append("")
            i += 1
        else:
            break

    # Parse inner content as blocks
    inner_blocks = _parse_blocks(quote_lines)

    return {"type": "blockquote", "content": inner_blocks}, i


def _parse_table(lines: list[str], start: int) -> tuple:
    """Parse a GFM table starting at `start`."""
    table_lines = []
    i = start

    while i < len(lines) and "|" in lines[i]:
        table_lines.append(lines[i].strip())
        i += 1

    if len(table_lines) < 2:
        # Not a real table, treat as paragraph
        block, _ = _parse_paragraph(lines, start)
        return block, i

    # Check for separator row (second line should be |---|---|)
    if not re.match(r"^\|[\s\-:|]+\|$", table_lines[1]):
        block, _ = _parse_paragraph(lines, start)
        return block, i

    rows = []

    # Header row
    header_cells = _split_table_row(table_lines[0])
    header_row = {
        "type": "tableRow",
        "content": [
            {
                "type": "tableHeader",
                "attrs": {},
                "content": [{"type": "paragraph", "content": _parse_inline(cell.strip())}],
            }
            for cell in header_cells
        ],
    }
    rows.append(header_row)

    # Data rows (skip separator at index 1)
    for row_line in table_lines[2:]:
        data_cells = _split_table_row(row_line)
        data_row = {
            "type": "tableRow",
            "content": [
                {
                    "type": "tableCell",
                    "attrs": {},
                    "content": [{"type": "paragraph", "content": _parse_inline(cell.strip())}],
                }
                for cell in data_cells
            ],
        }
        rows.append(data_row)

    return {
        "type": "table",
        "attrs": {"isNumberColumnEnabled": False, "layout": "default"},
        "content": rows,
    }, i


def _split_table_row(line: str) -> list[str]:
    """Split a table row into cells, stripping outer pipes."""
    # Remove leading/trailing pipes
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return stripped.split("|")


def _parse_paragraph(lines: list[str], start: int) -> tuple:  # NOSONAR (cognitive complexity)
    """Parse a paragraph (consecutive non-empty, non-special lines)."""
    para_lines = []
    i = start

    while i < len(lines):
        line = lines[i]
        # Stop at empty line or special block start
        if not line.strip():
            i += 1
            break
        if line.strip().startswith("```"):
            break
        if re.match(r"^#{1,6}\s+", line):
            break
        if re.match(r"^(\-{3,}|\*{3,}|_{3,})$", line.strip()):
            break
        if re.match(r"^(\s*)[-*]\s+", line):
            break
        if re.match(r"^(\s*)\d+\.\s+", line):
            break
        if line.startswith("> ") or line == ">":
            break
        if "|" in line and re.match(r"^\|.*\|$", line.strip()):
            break

        para_lines.append(line)
        i += 1

    if not para_lines:
        return None, i

    text = " ".join(para_lines)
    inline_content = _parse_inline(text)

    if not inline_content:
        return None, i

    return {"type": "paragraph", "content": inline_content}, i


def _parse_inline(text: str) -> list[dict]:  # NOSONAR (cognitive complexity)
    """
    Parse inline markdown formatting into ADF inline nodes.

    Handles: **bold**, *italic*, `code`, [text](url), ~~strike~~
    Marks are applied as ADF marks on text nodes.
    """
    if not text:
        return []

    nodes: list[dict] = []
    # Regex to find inline patterns
    # Order matters: ** before *, ~~ is distinct
    pattern = re.compile(
        r"(?P<code>`[^`]+`)"  # inline code
        r"|(?P<bold_italic>\*\*\*[^*]+\*\*\*)"  # bold italic
        r"|(?P<bold>\*\*[^*]+\*\*)"  # bold
        r"|(?P<italic>\*[^*]+\*)"  # italic
        r"|(?P<strike>~~[^~]+~~)"  # strikethrough
        r"|(?P<link>\[[^\]]+\]\([^)]+\))"  # link
    )

    last_end = 0
    for match in pattern.finditer(text):
        # Add any text before this match
        if match.start() > last_end:
            plain = text[last_end : match.start()]
            if plain:
                nodes.append({"type": "text", "text": plain})

        if match.group("code"):
            code_text = match.group("code")[1:-1]  # Strip backticks
            nodes.append(
                {
                    "type": "text",
                    "text": code_text,
                    "marks": [{"type": "code"}],
                }
            )
        elif match.group("bold_italic"):
            inner = match.group("bold_italic")[3:-3]
            nodes.append(
                {
                    "type": "text",
                    "text": inner,
                    "marks": [{"type": "strong"}, {"type": "em"}],
                }
            )
        elif match.group("bold"):
            inner = match.group("bold")[2:-2]
            nodes.append(
                {
                    "type": "text",
                    "text": inner,
                    "marks": [{"type": "strong"}],
                }
            )
        elif match.group("italic"):
            inner = match.group("italic")[1:-1]
            nodes.append(
                {
                    "type": "text",
                    "text": inner,
                    "marks": [{"type": "em"}],
                }
            )
        elif match.group("strike"):
            inner = match.group("strike")[2:-2]
            nodes.append(
                {
                    "type": "text",
                    "text": inner,
                    "marks": [{"type": "strike"}],
                }
            )
        elif match.group("link"):
            link_match = re.match(r"\[([^\]]+)\]\(([^)]+)\)", match.group("link"))
            if link_match:
                link_text = link_match.group(1)
                href = link_match.group(2)
                nodes.append(
                    {
                        "type": "text",
                        "text": link_text,
                        "marks": [{"type": "link", "attrs": {"href": href}}],
                    }
                )

        last_end = match.end()

    # Add any remaining text after last match
    if last_end < len(text):
        remaining = text[last_end:]
        if remaining:
            nodes.append({"type": "text", "text": remaining})

    # If no formatting found, return the whole text as a single node
    if not nodes and text:
        nodes.append({"type": "text", "text": text})

    return nodes


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


def _self_test() -> bool:
    """Quick roundtrip sanity check."""
    md = "# Hello\n\nThis is **bold** and *italic*.\n\n- Item 1\n- Item 2\n\n```python\nprint('hi')\n```"
    adf = markdown_to_adf(md)
    back = adf_to_markdown(adf)
    # Verify key elements survived roundtrip
    return "# Hello" in back and "**bold**" in back and "- Item" in back
