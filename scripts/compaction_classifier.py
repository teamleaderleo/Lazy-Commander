#!/usr/bin/env python3
"""Run the closed compaction-boundary classifier on a bounded state card."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from compaction_advisor import validate_classification
from compaction_owner_checkpoint import BOUNDARY_EVIDENCE


CARD_KEYS = {
    "schema",
    "checkpointFresh",
    "durableCheckpointReady",
    "nextActionPresent",
    "effectState",
    "goalActive",
    "boundaryEvidence",
}
CLASSIFIER_DISABLED_FEATURES = (
    "apps",
    "plugins",
    "shell_tool",
    "image_generation",
)
TOKEN_USAGE_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)


def build_card(
    checkpoint: dict[str, Any],
    *,
    checkpoint_fresh: bool,
    goal_status: str | None,
) -> dict[str, Any]:
    card = {
        "schema": "lazy-compaction-classifier-card/v1",
        "checkpointFresh": checkpoint_fresh,
        "durableCheckpointReady": checkpoint["durableCheckpointReady"],
        "nextActionPresent": checkpoint["nextActionPresent"],
        "effectState": checkpoint["effectState"],
        "goalActive": goal_status == "active",
        "boundaryEvidence": sorted(checkpoint.get("boundaryEvidence", [])),
    }
    return validate_card(card)


def validate_card(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != CARD_KEYS:
        raise ValueError("classifier card keys do not match the contract")
    if raw["schema"] != "lazy-compaction-classifier-card/v1":
        raise ValueError("classifier card schema is unsupported")
    for key in ("checkpointFresh", "durableCheckpointReady", "nextActionPresent", "goalActive"):
        if not isinstance(raw[key], bool):
            raise ValueError(f"{key} must be boolean")
    if raw["effectState"] not in {"none", "settled", "issued", "ambiguous"}:
        raise ValueError("effectState is unsupported")
    evidence = raw["boundaryEvidence"]
    if (
        not isinstance(evidence, list)
        or len(evidence) > 8
        or evidence != sorted(set(evidence))
        or any(item not in BOUNDARY_EVIDENCE for item in evidence)
    ):
        raise ValueError("boundaryEvidence must be a bounded enum list")
    return dict(raw)


def normalize_classification(
    card: dict[str, Any],
    raw_classification: Any,
) -> dict[str, str]:
    classification = validate_classification(raw_classification)
    if classification["decision"] == "keep":
        return classification
    ready = (
        card["checkpointFresh"]
        and card["durableCheckpointReady"]
        and card["nextActionPresent"]
    )
    return (
        {"decision": "now", "reason": "stable-boundary"}
        if ready
        else {"decision": "soon", "reason": "checkpoint-first"}
    )


def _token_usage(log_path: Path) -> dict[str, int] | None:
    usage = None
    with log_path.open("rb") as source:
        for raw in source:
            if len(raw) > 64 * 1024:
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            candidate = event.get("usage") if event.get("type") == "turn.completed" else None
            if not isinstance(candidate, dict):
                continue
            if all(isinstance(candidate.get(key), int) and candidate[key] >= 0 for key in TOKEN_USAGE_KEYS):
                usage = {key: candidate[key] for key in TOKEN_USAGE_KEYS}
    return usage


def run_codex_classifier(
    real_codex: str,
    state_root: Path,
    raw_card: Any,
    *,
    model: str = "gpt-5.6-luna",
    timeout_seconds: float = 60,
) -> tuple[dict[str, str], dict[str, Any]]:
    card = validate_card(raw_card)
    jobs = state_root / "classifier-jobs"
    jobs.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(jobs, 0o700)
    schema = Path(__file__).resolve().parents[1] / "references" / "compaction-classifier.schema.json"
    prompt = (
        "Decide whether to compact this Codex root thread before its next expensive turn. "
        "Use only the supplied closed card. now means a stable semantic boundary; "
        "soon means checkpoint-first; keep means the same active phase. Return only the schema.\n"
        + json.dumps(card, sort_keys=True, separators=(",", ":"))
    )
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix=".job-", dir=jobs) as directory:
        job = Path(directory)
        os.chmod(job, 0o700)
        output = job / "result.json"
        log = job / "codex.log"
        env = dict(os.environ)
        env.pop("CODEX_CLI_PATH", None)
        env["RUST_LOG"] = "error"
        command = [
            real_codex,
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "-c",
            "skills.include_instructions=false",
            *(
                value
                for feature in CLASSIFIER_DISABLED_FEATURES
                for value in ("--disable", feature)
            ),
            "-m",
            model,
            "--json",
            "--output-schema",
            str(schema),
            "--output-last-message",
            str(output),
            "-",
        ]
        with log.open("wb") as transcript:
            completed = subprocess.run(
                command,
                input=prompt.encode(),
                stdout=transcript,
                stderr=subprocess.STDOUT,
                cwd=job,
                env=env,
                timeout=timeout_seconds,
                check=False,
            )
        elapsed_ms = round((time.monotonic() - started) * 1000)
        if completed.returncode != 0 or not output.exists() or output.stat().st_size > 2048:
            raise RuntimeError("closed classifier process failed")
        model_classification = validate_classification(
            json.loads(output.read_text(encoding="utf-8"))
        )
        classification = normalize_classification(card, model_classification)
        token_usage = _token_usage(log)
        metrics = {
            "model": model,
            "elapsedMs": elapsed_ms,
            "reportedTokensUsed": (
                token_usage["input_tokens"] + token_usage["output_tokens"]
                if token_usage is not None
                else None
            ),
            "tokenUsage": token_usage,
            "modelClassification": model_classification,
            "classificationNormalized": classification != model_classification,
            "cardSha256": hashlib.sha256(
                json.dumps(card, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "rawConversationProvided": False,
        }
    return classification, metrics
