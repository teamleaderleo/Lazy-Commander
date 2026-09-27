"""Compact a complete JSON document without rewriting any JSON tokens."""

from __future__ import annotations

import json


_JSON_WHITESPACE = " \t\r\n"


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def validate_json(text: str) -> None:
    """Validate standard JSON without using the parsed value for rendering."""
    json.loads(text, parse_constant=_reject_constant)


def compact_json_layout(text: str, limit: int) -> str | None:
    """Return a smaller complete JSON layout when ``text`` exceeds ``limit``.

    JSON parsing validates the document only. The returned text is built directly
    from the source, so duplicate keys and the spelling of every token survive.
    """
    if limit < 0 or len(text) <= limit:
        return None
    try:
        validate_json(text)
    except (RecursionError, ValueError):
        return None

    compact: list[str] = []
    readable: list[str] = []
    depth = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            compact.append(character)
            readable.append(character)
            if len(compact) > limit:
                return None
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character in _JSON_WHITESPACE:
            continue

        compact.append(character)
        readable.append(character)
        if len(compact) > limit:
            return None
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
        elif character in "]}":
            depth -= 1
        elif character == "," and depth == 1:
            readable.append("\n")

    compact_text = "".join(compact)
    if len(compact_text) > limit:
        return None
    readable_text = "".join(readable)
    if compact_text.startswith(("[", "{")) and len(readable_text) <= limit:
        return readable_text
    return compact_text
