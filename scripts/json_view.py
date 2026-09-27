#!/usr/bin/env python3
"""Project JSON into bounded structure metadata without scalar value bodies."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any


TYPE_ORDER = ("object", "array", "string", "number", "boolean", "null")


def node_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    raise TypeError("unsupported JSON node")


def bounded_key(value: str, limit: int) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    return value[:limit], True


class Projector:
    def __init__(self, *, max_depth: int, max_keys: int, max_nodes: int, max_key_chars: int):
        self.max_depth = max_depth
        self.max_keys = max_keys
        self.max_nodes = max_nodes
        self.max_key_chars = max_key_chars
        self.nodes = 0

    def project(self, value: Any, depth: int = 0) -> dict[str, Any]:
        kind = node_type(value)
        if self.nodes >= self.max_nodes:
            return {"type": kind, "nodeBudgetTruncated": True}
        self.nodes += 1
        if kind not in {"object", "array"}:
            return {"type": kind}
        if kind == "array":
            counts = Counter(node_type(item) for item in value)
            return {
                "type": "array",
                "count": len(value),
                "itemTypes": {name: counts[name] for name in TYPE_ORDER if counts[name]},
                "valuesEmitted": 0,
            }

        keys = sorted(value)
        result: dict[str, Any] = {"type": "object", "count": len(keys)}
        if depth >= self.max_depth:
            result.update({"keys": [], "omittedKeys": len(keys), "depthTruncated": bool(keys)})
            return result
        entries = []
        for key in keys[: self.max_keys]:
            shown, truncated = bounded_key(key, self.max_key_chars)
            entry = {"key": shown, "node": self.project(value[key], depth + 1)}
            if truncated:
                entry["keyTruncated"] = True
            entries.append(entry)
        result.update(
            {
                "keys": entries,
                "omittedKeys": len(keys) - len(entries),
                "depthTruncated": False,
            }
        )
        return result


def decode_pointer(pointer: str) -> list[str]:
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise ValueError("pointer must be empty or begin with slash")
    return [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]


def resolve_pointer(root: Any, pointer: str) -> Any:
    current = root
    for index, part in enumerate(decode_pointer(pointer)):
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        if isinstance(current, list) and part.isascii() and part.isdigit():
            offset = int(part)
            if offset < len(current):
                current = current[offset]
                continue
        raise LookupError(f"pointer segment {index} does not resolve")
    return current


def base_result(path: Path, source: bytes) -> dict[str, Any]:
    return {
        "schema": "lazy-json-view/v1",
        "path": str(path),
        "sourceBytes": len(source),
        "sourceSha256": hashlib.sha256(source).hexdigest(),
        "rawDocumentEmitted": False,
        "scalarBodiesEmitted": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Show bounded JSON structure or one exact pointer without scalar bodies."
    )
    parser.add_argument("path", type=Path)
    parser.add_argument("--pointer", help="exact RFC 6901 JSON pointer; empty selects root")
    parser.add_argument("--max-depth", type=int, default=3)
    parser.add_argument("--max-keys", type=int, default=30)
    parser.add_argument("--max-nodes", type=int, default=200)
    parser.add_argument("--max-key-chars", type=int, default=80)
    parser.add_argument("--max-bytes", type=int, default=8 * 1024 * 1024)
    args = parser.parse_args()
    if not 0 <= args.max_depth <= 8:
        parser.error("--max-depth must be between 0 and 8")
    if not 1 <= args.max_keys <= 100:
        parser.error("--max-keys must be between 1 and 100")
    if not 1 <= args.max_nodes <= 1000:
        parser.error("--max-nodes must be between 1 and 1000")
    if not 8 <= args.max_key_chars <= 200:
        parser.error("--max-key-chars must be between 8 and 200")
    if not 1024 <= args.max_bytes <= 64 * 1024 * 1024:
        parser.error("--max-bytes must be between 1024 and 67108864")
    if not args.path.is_file():
        parser.error("path must be an existing file")

    source = args.path.read_bytes()
    result = base_result(args.path, source)
    if len(source) > args.max_bytes:
        result.update({"parsed": False, "error": {"type": "source_too_large"}})
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError as error:
        result.update(
            {"parsed": False, "error": {"type": "invalid_utf8", "offset": error.start}}
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2
    try:
        root = json.loads(text)
    except json.JSONDecodeError as error:
        result.update(
            {
                "parsed": False,
                "error": {
                    "type": "invalid_json",
                    "line": error.lineno,
                    "column": error.colno,
                    "offset": error.pos,
                },
            }
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2
    try:
        selected = root if args.pointer is None else resolve_pointer(root, args.pointer)
    except (ValueError, LookupError) as error:
        result.update(
            {
                "parsed": True,
                "mode": "pointer",
                "resolved": False,
                "error": {"type": "invalid_pointer", "detail": str(error)},
            }
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2

    projector = Projector(
        max_depth=args.max_depth,
        max_keys=args.max_keys,
        max_nodes=args.max_nodes,
        max_key_chars=args.max_key_chars,
    )
    result.update(
        {
            "parsed": True,
            "mode": "summary" if args.pointer is None else "pointer",
            "resolved": True,
            "projection": projector.project(selected),
            "projectedNodes": projector.nodes,
            "limits": {
                "maxDepth": args.max_depth,
                "maxKeysPerObject": args.max_keys,
                "maxNodes": args.max_nodes,
                "maxKeyChars": args.max_key_chars,
            },
        }
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
