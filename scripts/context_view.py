#!/usr/bin/env python3
"""Project one Markdown file into a bounded table of contents or exact section."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def headings(lines: list[str]) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    fenced = False
    for number, line in enumerate(lines, 1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        match = None if fenced else HEADING.match(line)
        if match:
            found.append(
                {"level": len(match.group(1)), "title": match.group(2), "line": number}
            )
    return found


def exact_section(
    lines: list[str], found: list[dict[str, object]], title: str
) -> tuple[int, int, str]:
    matches = [item for item in found if item["title"].casefold() == title.casefold()]
    if len(matches) != 1:
        raise ValueError(f"section must match exactly once; found {len(matches)}")
    selected = matches[0]
    start = int(selected["line"]) - 1
    level = int(selected["level"])
    end = len(lines)
    for item in found:
        candidate = int(item["line"]) - 1
        if candidate > start and int(item["level"]) <= level:
            end = candidate
            break
    return start + 1, end, "".join(lines[start:end])


def bounded(text: str, limit: int) -> tuple[str, bool, int]:
    if len(text) <= limit:
        return text, False, 0
    return text[:limit], True, len(text) - limit


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Show Markdown headings or one exact bounded section."
    )
    parser.add_argument("path", type=Path)
    parser.add_argument("--section", help="case-insensitive exact heading title")
    parser.add_argument("--max-chars", type=int, default=6000)
    parser.add_argument("--max-headings", type=int, default=100)
    args = parser.parse_args()
    if not 256 <= args.max_chars <= 12000:
        parser.error("--max-chars must be between 256 and 12000")
    if not 1 <= args.max_headings <= 200:
        parser.error("--max-headings must be between 1 and 200")
    if not args.path.is_file():
        parser.error("path must be an existing file")

    text = args.path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    found = headings(lines)
    result: dict[str, object] = {
        "schema": "lazy-context-view/v1",
        "path": str(args.path),
        "sourceChars": len(text),
        "sourceLines": len(lines),
        "rawDocumentEmitted": False,
    }
    if args.section is None:
        shown = found[: args.max_headings]
        omitted = len(found) - len(shown)
        result.update(
            {
                "mode": "toc",
                "headings": shown,
                "totalHeadings": len(found),
                "omittedHeadings": omitted,
                "truncated": omitted > 0,
                "omittedBody": True,
            }
        )
    else:
        try:
            start, end, content = exact_section(lines, found, args.section)
        except ValueError as error:
            parser.error(str(error))
        content, truncated, omitted = bounded(content, args.max_chars)
        result.update(
            {
                "mode": "section",
                "section": args.section,
                "startLine": start,
                "endLine": end,
                "content": content,
                "truncated": truncated,
                "omittedChars": omitted,
            }
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
