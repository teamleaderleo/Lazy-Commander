#!/usr/bin/env python3
"""Persist a browser/tool result privately and emit a deterministic control view.

The default view contains no result bodies.  A caller may expose one exact string
only by naming the consumer and supplying that consumer's explicit character
budget; this module intentionally has no universal content ceiling.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
from collections import Counter
from pathlib import Path
from typing import Any


INPUT_SCHEMA = "lazy-browser-result-input/v1"
VIEW_SCHEMA = "lazy-browser-result-view/v1"
ACTION_CLASSES = {"observation", "effect"}
OUTCOMES = {"success", "error", "timeout"}
SETTLEMENTS = {"not-applicable", "verified-no-effect", "committed", "ambiguous"}
TOP_KEYS = {
    "schema", "operationId", "attemptId", "actionClass", "outcome",
    "settlement", "receipt", "result",
}
PROMPT_KEY = re.compile(r"prompt|instruction|query", re.I)
URL_KEY = re.compile(r"url|uri|href|location", re.I)
BINARY_KEY = re.compile(r"screenshot|image|audio|video|blob|binary|base64|data", re.I)
TRANSPORT_KEY = re.compile(r"header|cookie|status|request|response|tool|call|trace", re.I)


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def bounded_identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError(f"{name} must be a non-empty string of at most 256 characters")
    return value


def validate(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("input must be a JSON object")
    unknown = set(raw) - TOP_KEYS
    if unknown:
        raise ValueError(f"unknown input keys: {', '.join(sorted(unknown))}")
    if raw.get("schema") != INPUT_SCHEMA:
        raise ValueError(f"schema must be {INPUT_SCHEMA}")
    action_class = raw.get("actionClass")
    outcome = raw.get("outcome")
    settlement = raw.get("settlement")
    if action_class not in ACTION_CLASSES:
        raise ValueError("actionClass is not supported")
    if outcome not in OUTCOMES:
        raise ValueError("outcome is not supported")
    if settlement not in SETTLEMENTS:
        raise ValueError("settlement is not supported")
    receipt = raw.get("receipt")
    if receipt is not None and not isinstance(receipt, dict):
        raise ValueError("receipt must be an object when present")
    if action_class == "observation":
        if settlement != "not-applicable":
            raise ValueError("observation settlement must be not-applicable")
    else:
        if settlement == "not-applicable":
            raise ValueError("effect settlement cannot be not-applicable")
        if settlement in {"committed", "verified-no-effect"}:
            if not receipt or not receipt.get("authority") or not receipt.get("receiptId"):
                raise ValueError("authoritative effect settlement requires receipt authority and receiptId")
            bounded_identifier(receipt["authority"], "receipt.authority")
            bounded_identifier(receipt["receiptId"], "receipt.receiptId")
        if outcome == "timeout" and settlement not in {"ambiguous", "committed", "verified-no-effect"}:
            raise ValueError("timed-out effect must remain ambiguous or have authoritative settlement")
    return raw


def string_category(key: str | None) -> str:
    name = key or ""
    if PROMPT_KEY.search(name):
        return "prompt"
    if URL_KEY.search(name):
        return "url"
    if BINARY_KEY.search(name):
        return "binary"
    if TRANSPORT_KEY.search(name):
        return "transport"
    return "semantic"


def measure(value: Any, *, key: str | None = None, stats: Counter[str] | None = None) -> Counter[str]:
    stats = stats if stats is not None else Counter()
    stats["nodes"] += 1
    if isinstance(value, dict):
        stats["objects"] += 1
        stats["keys"] += len(value)
        for child_key, child in value.items():
            measure(child, key=child_key, stats=stats)
    elif isinstance(value, list):
        stats["arrays"] += 1
        stats["items"] += len(value)
        for child in value:
            measure(child, key=key, stats=stats)
    elif isinstance(value, str):
        category = string_category(key)
        stats["strings"] += 1
        stats[f"{category}Strings"] += 1
        stats[f"{category}Chars"] += len(value)
    elif value is None:
        stats["nulls"] += 1
    elif isinstance(value, bool):
        stats["booleans"] += 1
    elif isinstance(value, (int, float)):
        stats["numbers"] += 1
    else:
        raise ValueError("result contains a non-JSON value")
    return stats


def decode_pointer(pointer: str) -> list[str]:
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise ValueError("content pointer must be empty or start with slash")
    return [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]


def resolve_pointer(root: Any, pointer: str) -> Any:
    current = root
    for part in decode_pointer(pointer):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isascii() and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            raise ValueError("content pointer does not resolve")
    if not isinstance(current, str):
        raise ValueError("content pointer must resolve to a string")
    return current


def effect_view(raw: dict[str, Any]) -> dict[str, Any]:
    settlement = raw["settlement"]
    receipt = raw.get("receipt")
    authoritative = settlement in {"committed", "verified-no-effect"}
    return {
        "settlement": settlement,
        "authoritativelySettled": authoritative,
        "reconciliationRequired": raw["actionClass"] == "effect" and settlement == "ambiguous",
        "retryAuthorized": False,
        "receipt": None if not authoritative else {
            "authority": receipt["authority"],
            "receiptId": receipt["receiptId"],
        },
    }


def build_view(raw: dict[str, Any], *, consumer_id: str | None, pointer: str | None,
               max_content_chars: int | None) -> dict[str, Any]:
    source = canonical_bytes(raw)
    stats = measure(raw["result"])
    view: dict[str, Any] = {
        "schema": VIEW_SCHEMA,
        "operationId": raw["operationId"],
        "attemptId": raw["attemptId"],
        "actionClass": raw["actionClass"],
        "outcome": raw["outcome"],
        "sourceBytes": len(source),
        "sourceSha256": hashlib.sha256(source).hexdigest(),
        "resultShape": {key: stats[key] for key in (
            "nodes", "objects", "arrays", "keys", "items", "strings",
            "numbers", "booleans", "nulls",
        )},
        "stringClasses": {category: {
            "strings": stats[f"{category}Strings"],
            "chars": stats[f"{category}Chars"],
        } for category in ("semantic", "prompt", "url", "binary", "transport")},
        "rawResultPersistedPrivately": True,
        "resultBodiesEmitted": False,
        "contentBudget": None,
        "effect": effect_view(raw),
    }
    if consumer_id is not None:
        selected = resolve_pointer(raw["result"], pointer or "")
        emitted = selected[:max_content_chars]
        view["resultBodiesEmitted"] = True
        view["contentBudget"] = {
            "consumerId": consumer_id,
            "pointer": pointer or "",
            "maxChars": max_content_chars,
            "selectedChars": len(emitted),
            "omittedChars": len(selected) - len(emitted),
        }
        view["content"] = emitted
    return view


def private_write(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise RuntimeError(f"private artifact mode drifted: {path}")


def persist(output_dir: Path, raw: dict[str, Any], view: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output_dir, 0o700)
    artifacts = {
        "raw.json": canonical_bytes(raw) + b"\n",
        "view.json": json.dumps(view, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n",
    }
    for name, data in artifacts.items():
        path = output_dir / name
        if path.exists():
            if path.read_bytes() != data:
                raise RuntimeError(f"replay conflict: {path}")
            if stat.S_IMODE(path.stat().st_mode) != 0o600:
                raise RuntimeError(f"private artifact mode drifted: {path}")
        else:
            private_write(path, data)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--consumer-id")
    parser.add_argument("--content-pointer")
    parser.add_argument("--max-content-chars", type=int)
    args = parser.parse_args()
    options = (args.consumer_id, args.content_pointer, args.max_content_chars)
    if any(value is not None for value in options) and not all(value is not None for value in options):
        parser.error("content exposure requires --consumer-id, --content-pointer, and --max-content-chars together")
    if args.consumer_id is not None:
        bounded_identifier(args.consumer_id, "consumerId")
        if not 1 <= args.max_content_chars <= 10_000_000:
            parser.error("--max-content-chars must be between 1 and 10000000")
    try:
        raw = validate(json.loads(args.input.read_text(encoding="utf-8")))
        view = build_view(raw, consumer_id=args.consumer_id, pointer=args.content_pointer,
                          max_content_chars=args.max_content_chars)
        persist(args.output_dir, raw, view)
    except (OSError, json.JSONDecodeError, ValueError, RuntimeError) as error:
        parser.error(str(error))
    print(json.dumps(view, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
