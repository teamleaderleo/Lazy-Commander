#!/usr/bin/env python3
"""Durable low-friction broker for admitted Codex tasks and exact wakes.

Edit map:
- persistence/schema -> database
- admission/immediate run -> run_admission, run_main
- defer/lease/settle -> defer_main, claim_main, execute_main, settle_main
- routing/settlement -> routing_run, route_main, settle_route_main, settle_worker_main
- portfolio adapters -> lane_budget_main, control_service_main, campaign_controller_main
- wake/worker/inspection -> drain_due, tick_main, worker_main, status_main, show_main
- CLI wiring -> parser, main
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import socket
import sqlite3
import stat
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from action_admission import (  # noqa: E402
    REQUEST_SCHEMA,
    decide_request,
    sha256_json,
    validate_request,
)
from offload_router import (  # noqa: E402
    plan_control_services,
    plan_lane_budget,
    route_task,
    settle_carrier_effect,
    settle_worker_execution,
)
from campaign_controller import (  # noqa: E402
    compile_controller as compile_campaign_controller,
    load_snapshot as load_campaign_snapshot,
    run as run_campaign_controller,
)


TASK_STATUSES = {
    "queued",
    "leased",
    "running",
    "complete",
    "failed",
    "attention",
    "ambiguous",
}
EXECUTABLE_ROUTES = {"direct", "bounded-command", "bounded-read"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def parse_at(value: str) -> datetime:
    rendered = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(rendered)
    except ValueError as error:
        raise argparse.ArgumentTypeError("at must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("at must include a timezone")
    return parsed.astimezone(timezone.utc)


def parse_duration(value: str) -> timedelta:
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if len(value) < 2 or value[-1] not in units:
        raise argparse.ArgumentTypeError("duration must end in s, m, h, or d")
    try:
        amount = float(value[:-1])
    except ValueError as error:
        raise argparse.ArgumentTypeError("duration amount must be numeric") from error
    seconds = amount * units[value[-1]]
    if seconds < 0 or seconds > 3650 * 86400:
        raise argparse.ArgumentTypeError("duration must be between 0 and 3650 days")
    return timedelta(seconds=seconds)


def private_file(path: Path):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(descriptor, "w", encoding="utf-8")


def write_private(path: Path, value: Any) -> None:
    text = value if isinstance(value, str) else json.dumps(value, indent=2, sort_keys=True) + "\n"
    with private_file(path) as output:
        output.write(text)
        output.flush()
        os.fsync(output.fileno())
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise SystemExit(f"private artifact mode drifted: {path}")


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def state_root(args: argparse.Namespace) -> Path:
    root = args.state_root.expanduser().resolve()
    ensure_dir(root)
    ensure_dir(root / "tasks")
    ensure_dir(root / "runs")
    return root


def database(root: Path) -> sqlite3.Connection:
    path = root / "broker.sqlite3"
    connection = sqlite3.connect(path, timeout=10, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=10000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            eligible_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            status TEXT NOT NULL,
            request_sha256 TEXT NOT NULL,
            request_relpath TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            lease_owner_sha256 TEXT,
            lease_token_sha256 TEXT,
            lease_expires_at TEXT,
            last_route TEXT,
            last_exit_code INTEGER,
            CHECK (status IN ('queued','leased','running','complete','failed','attention','ambiguous'))
        );
        CREATE TABLE IF NOT EXISTS events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL REFERENCES tasks(id),
            at TEXT NOT NULL,
            kind TEXT NOT NULL,
            detail_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS tasks_due
            ON tasks(status, eligible_at, created_at);
        CREATE TABLE IF NOT EXISTS campaign_budgets (
            budget_ref TEXT PRIMARY KEY,
            generation INTEGER NOT NULL,
            limit_tokens INTEGER NOT NULL,
            reserved_tokens INTEGER NOT NULL,
            consumed_tokens INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS campaign_acceptances (
            transition_id TEXT PRIMARY KEY,
            campaign_ref TEXT NOT NULL,
            campaign_generation INTEGER NOT NULL,
            owner_fingerprint TEXT NOT NULL,
            view_fingerprint TEXT NOT NULL,
            budget_ref TEXT NOT NULL REFERENCES campaign_budgets(budget_ref),
            budget_generation_before INTEGER NOT NULL,
            budget_generation_after INTEGER NOT NULL,
            budget_limit INTEGER NOT NULL,
            budget_reserved_before INTEGER NOT NULL,
            budget_consumed_before INTEGER NOT NULL,
            reservation_tokens INTEGER NOT NULL,
            accepted_at TEXT NOT NULL
        );
        """
    )
    for candidate in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        if candidate.exists():
            candidate.chmod(0o600)
    return connection


def notify_worker(root: Path) -> None:
    """Wake a live worker; durable SQLite state remains authoritative."""
    path = root / "wake.sock"
    if not path.exists():
        return
    notifier = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        notifier.sendto(b"1", str(path))
    except OSError:
        pass
    finally:
        notifier.close()


def event(
    connection: sqlite3.Connection,
    task_id: str,
    kind: str,
    detail: dict[str, Any] | None = None,
    *,
    at: datetime | None = None,
) -> None:
    connection.execute(
        "INSERT INTO events(task_id, at, kind, detail_json) VALUES (?, ?, ?, ?)",
        (
            task_id,
            iso(at or utc_now()),
            kind,
            json.dumps(detail or {}, sort_keys=True, separators=(",", ":")),
        ),
    )


def command_argv(values: list[str]) -> list[str]:
    command = list(values)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        raise SystemExit("a command argv is required after --")
    return command


def command_request(
    *,
    cwd: Path,
    argv: list[str],
    observation_only: bool,
) -> dict[str, Any]:
    resolved = cwd.expanduser().resolve()
    if not resolved.is_dir():
        raise SystemExit("cwd does not exist or is not a directory")
    return validate_request(
        {
            "schema": REQUEST_SCHEMA,
            "kind": "command",
            "argv": argv,
            "cwd": str(resolved),
            "scope": {"observation_only": observation_only},
        }
    )


def run_admission(request_path: Path, output_dir: Path) -> tuple[int, dict[str, Any], dict[str, Any]]:
    process = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_DIR / "action_admission.py"),
            "run-command",
            "--request",
            str(request_path),
            "--output-dir",
            str(output_dir),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    receipt_path = output_dir / "admission-receipt.json"
    decision_path = output_dir / "decision.json"
    if not receipt_path.is_file() or not decision_path.is_file():
        raise SystemExit("admission failed before producing private decision artifacts")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    decision = json.loads(decision_path.read_text(encoding="utf-8"))
    if process.stderr:
        raise SystemExit("admission emitted an unexpected diagnostic")
    return process.returncode, receipt, decision


def run_main(args: argparse.Namespace) -> int:
    root = state_root(args)
    request = command_request(
        cwd=args.cwd,
        argv=command_argv(args.argv),
        observation_only=args.observation_only,
    )
    run_id = f"{utc_now().strftime('%Y%m%dT%H%M%SZ')}-{sha256_json(request)[:12]}-{uuid.uuid4().hex[:8]}"
    run_dir = (args.output_dir.expanduser().resolve() if args.output_dir else root / "runs" / run_id)
    if run_dir.exists():
        raise SystemExit("refusing to overwrite broker run artifacts")
    ensure_dir(run_dir)
    request_path = run_dir / "request.json"
    write_private(request_path, request)
    exit_code, receipt, decision = run_admission(request_path, run_dir / "execution")
    broker_receipt = {
        "schema": "lazy-broker-run-receipt/v1",
        "run_id": run_id,
        "request_sha256": sha256_json(request),
        "route": receipt["route"],
        "executed": receipt["executed"],
        "exit_code": receipt["exit_code"],
        "decision_sha256": receipt["decision_sha256"],
        "raw_content_emitted": False,
    }
    write_private(run_dir / "receipt.json", broker_receipt)
    view = run_dir / "execution" / "run" / "view.txt"
    print(
        json.dumps(
            {
                "run_id": run_id,
                "route": receipt["route"],
                "executed": receipt["executed"],
                "exit_code": receipt["exit_code"],
                "view": str(view) if view.is_file() else None,
                "next": decision.get("next"),
            },
            sort_keys=True,
        )
    )
    return exit_code


def eligible_time(args: argparse.Namespace) -> datetime:
    if args.at is not None:
        return args.at
    return utc_now() + args.after


def defer_main(args: argparse.Namespace) -> int:
    root = state_root(args)
    request = command_request(
        cwd=args.cwd,
        argv=command_argv(args.argv),
        observation_only=True,
    )
    decision = decide_request(request)
    if decision["route"] not in EXECUTABLE_ROUTES:
        raise SystemExit(
            f"deferred observation is not mechanically executable: {decision['route']}"
        )
    task_id = uuid.uuid4().hex
    task_dir = root / "tasks" / task_id
    ensure_dir(task_dir)
    request_path = task_dir / "request.json"
    write_private(request_path, request)
    now = utc_now()
    eligible = eligible_time(args)
    receipt = {
        "schema": "lazy-broker-enqueue-receipt/v1",
        "task_id": task_id,
        "request_sha256": sha256_json(request),
        "eligible_at": iso(eligible),
        "admission_route": decision["route"],
        "authority": "observation-only",
        "raw_content_emitted": False,
    }
    write_private(task_dir / "enqueue-receipt.json", receipt)
    connection = database(root)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """INSERT INTO tasks(
                id, created_at, eligible_at, updated_at, status,
                request_sha256, request_relpath, last_route
            ) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?)""",
            (
                task_id,
                iso(now),
                iso(eligible),
                iso(now),
                receipt["request_sha256"],
                str(request_path.relative_to(root)),
                decision["route"],
            ),
        )
        event(connection, task_id, "queued", {"eligible_at": iso(eligible), "route": decision["route"]}, at=now)
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()
    notify_worker(root)
    print(json.dumps({"task_id": task_id, "eligible_at": iso(eligible), "route": decision["route"]}, sort_keys=True))
    return 0


def reclaim_expired(connection: sqlite3.Connection, now: datetime) -> int:
    rows = list(
        connection.execute(
            "SELECT id FROM tasks WHERE status IN ('leased','running') AND lease_expires_at <= ?",
            (iso(now),),
        )
    )
    for row in rows:
        connection.execute(
            """UPDATE tasks SET status='queued', updated_at=?, lease_owner_sha256=NULL,
               lease_token_sha256=NULL, lease_expires_at=NULL WHERE id=?""",
            (iso(now), row["id"]),
        )
        event(connection, row["id"], "lease-expired-requeued", at=now)
    return len(rows)


def due_main(args: argparse.Namespace) -> int:
    root = state_root(args)
    connection = database(root)
    now = utc_now()
    try:
        connection.execute("BEGIN IMMEDIATE")
        reclaimed = reclaim_expired(connection, now)
        rows = list(
            connection.execute(
                """SELECT id, eligible_at, attempts, last_route FROM tasks
                   WHERE status='queued' AND eligible_at <= ?
                   ORDER BY eligible_at, created_at LIMIT ?""",
                (iso(now), args.limit),
            )
        )
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()
    print(
        json.dumps(
            {
                "now": iso(now),
                "reclaimed_expired_leases": reclaimed,
                "due": [dict(row) for row in rows],
            },
            sort_keys=True,
        )
    )
    return 0


def claim_main(args: argparse.Namespace) -> int:
    root = state_root(args)
    connection = database(root)
    now = utc_now()
    token = secrets.token_urlsafe(32)
    token_sha = hashlib.sha256(token.encode("utf-8")).hexdigest()
    owner_sha = hashlib.sha256(args.worker.encode("utf-8")).hexdigest()
    expires = now + timedelta(seconds=args.lease_seconds)
    try:
        connection.execute("BEGIN IMMEDIATE")
        reclaim_expired(connection, now)
        row = connection.execute(
            """SELECT id, attempts FROM tasks WHERE status='queued' AND eligible_at <= ?
               ORDER BY eligible_at, created_at LIMIT 1""",
            (iso(now),),
        ).fetchone()
        if row is None:
            connection.execute("COMMIT")
            print(json.dumps({"claimed": False, "now": iso(now)}, sort_keys=True))
            return 0
        attempt = int(row["attempts"]) + 1
        connection.execute(
            """UPDATE tasks SET status='leased', attempts=?, updated_at=?,
               lease_owner_sha256=?, lease_token_sha256=?, lease_expires_at=? WHERE id=?""",
            (attempt, iso(now), owner_sha, token_sha, iso(expires), row["id"]),
        )
        event(connection, row["id"], "leased", {"attempt": attempt, "expires_at": iso(expires)}, at=now)
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()
    claim_id = uuid.uuid4().hex
    claim_path = root / "tasks" / row["id"] / f"claim-{claim_id}.json"
    write_private(
        claim_path,
        {
            "schema": "lazy-broker-claim/v1",
            "task_id": row["id"],
            "attempt": attempt,
            "lease_token": token,
            "expires_at": iso(expires),
        },
    )
    print(json.dumps({"claimed": True, "task_id": row["id"], "claim": str(claim_path), "expires_at": iso(expires)}, sort_keys=True))
    return 0


def load_claim(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size > 20_000:
        raise SystemExit("claim is missing or exceeds its bound")
    try:
        claim = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise SystemExit("claim is not valid JSON") from error
    if set(claim) != {"schema", "task_id", "attempt", "lease_token", "expires_at"}:
        raise SystemExit("claim keys do not match the contract")
    if claim["schema"] != "lazy-broker-claim/v1":
        raise SystemExit("claim schema is not supported")
    return claim


def execute_main(args: argparse.Namespace) -> int:
    root = state_root(args)
    claim = load_claim(args.claim.expanduser().resolve())
    token_sha = hashlib.sha256(claim["lease_token"].encode("utf-8")).hexdigest()
    connection = database(root)
    now = utc_now()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM tasks WHERE id=?", (claim["task_id"],)).fetchone()
        if row is None:
            raise SystemExit("claim task does not exist")
        if row["status"] != "leased" or row["lease_token_sha256"] != token_sha:
            raise SystemExit("claim is stale or does not own the task")
        if row["lease_expires_at"] <= iso(now):
            raise SystemExit("claim lease has expired")
        if int(row["attempts"]) != int(claim["attempt"]):
            raise SystemExit("claim attempt does not match current state")
        connection.execute("UPDATE tasks SET status='running', updated_at=? WHERE id=?", (iso(now), row["id"]))
        event(connection, row["id"], "execution-started", {"attempt": row["attempts"]}, at=now)
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        connection.close()
        raise

    request_path = root / row["request_relpath"]
    request = validate_request(json.loads(request_path.read_text(encoding="utf-8")))
    if sha256_json(request) != row["request_sha256"]:
        connection.close()
        raise SystemExit("task request hash does not match durable state")
    attempt_dir = root / "tasks" / row["id"] / f"attempt-{row['attempts']}"
    try:
        exit_code, receipt, decision = run_admission(request_path, attempt_dir)
    except BaseException:
        failed_at = utc_now()
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute(
            "SELECT status, lease_token_sha256 FROM tasks WHERE id=?", (row["id"],)
        ).fetchone()
        if current and current["status"] == "running" and current["lease_token_sha256"] == token_sha:
            connection.execute(
                """UPDATE tasks SET status='attention', updated_at=?,
                   lease_owner_sha256=NULL, lease_token_sha256=NULL,
                   lease_expires_at=NULL WHERE id=?""",
                (iso(failed_at), row["id"]),
            )
            event(connection, row["id"], "execution-error", at=failed_at)
            connection.execute("COMMIT")
        else:
            connection.execute("ROLLBACK")
        connection.close()
        raise
    finished = utc_now()
    if receipt["executed"] and receipt["exit_code"] == 0:
        status = "complete"
    elif receipt["executed"]:
        status = "failed"
    elif receipt["route"] == "exact-wake" and decision.get("next", {}).get("wake_at"):
        status = "queued"
    elif receipt["route"] == "reconcile-effect":
        status = "ambiguous"
    else:
        status = "attention"
    connection.execute("BEGIN IMMEDIATE")
    current = connection.execute("SELECT status, lease_token_sha256 FROM tasks WHERE id=?", (row["id"],)).fetchone()
    if current["status"] != "running" or current["lease_token_sha256"] != token_sha:
        connection.execute("ROLLBACK")
        connection.close()
        raise SystemExit("task ownership changed during execution")
    eligible_at = decision.get("next", {}).get("wake_at") if status == "queued" else row["eligible_at"]
    connection.execute(
        """UPDATE tasks SET status=?, eligible_at=?, updated_at=?, last_route=?,
           last_exit_code=?, lease_owner_sha256=NULL, lease_token_sha256=NULL,
           lease_expires_at=NULL WHERE id=?""",
        (status, eligible_at, iso(finished), receipt["route"], receipt["exit_code"], row["id"]),
    )
    event(
        connection,
        row["id"],
        "execution-settled",
        {"status": status, "route": receipt["route"], "exit_code": receipt["exit_code"]},
        at=finished,
    )
    connection.execute("COMMIT")
    connection.close()
    broker_receipt = {
        "schema": "lazy-broker-execution-receipt/v1",
        "task_id": row["id"],
        "attempt": row["attempts"],
        "status": status,
        "route": receipt["route"],
        "exit_code": receipt["exit_code"],
        "decision_sha256": receipt["decision_sha256"],
        "raw_content_emitted": False,
    }
    write_private(root / "tasks" / row["id"] / f"execution-receipt-{row['attempts']}.json", broker_receipt)
    view = attempt_dir / "run" / "view.txt"
    print(json.dumps({**broker_receipt, "view": str(view) if view.is_file() else None}, sort_keys=True))
    return exit_code


def settle_main(args: argparse.Namespace) -> int:
    root = state_root(args)
    connection = database(root)
    now = utc_now()
    target = {
        "complete": "complete",
        "ambiguous": "ambiguous",
        "verified-no-effect": "queued",
        "requeue": "queued",
    }[args.outcome]
    eligible = now + args.after if target == "queued" else now
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT status FROM tasks WHERE id=?", (args.id,)).fetchone()
        if row is None:
            raise SystemExit("task does not exist")
        if row["status"] in {"leased", "running"}:
            raise SystemExit("cannot operator-settle an actively leased task")
        connection.execute(
            """UPDATE tasks SET status=?, eligible_at=?, updated_at=?,
               lease_owner_sha256=NULL, lease_token_sha256=NULL, lease_expires_at=NULL
               WHERE id=?""",
            (target, iso(eligible), iso(now), args.id),
        )
        event(connection, args.id, "operator-settled", {"outcome": args.outcome, "status": target}, at=now)
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()
    if target == "queued":
        notify_worker(root)
    print(json.dumps({"task_id": args.id, "status": target, "eligible_at": iso(eligible)}, sort_keys=True))
    return 0


def observation_main(args: argparse.Namespace) -> int:
    root = state_root(args)
    scope = {
        key: value
        for key, value in {
            "owner_query_available": args.owner_query,
            "semantic_delta_available": args.semantic_delta,
            "selector_bounded": args.selector_bounded,
            "visual_semantics": args.visual_semantics,
            "expected_payload_chars": args.expected_chars,
            "expected_items": args.expected_items,
        }.items()
        if value not in (False, None)
    }
    request = validate_request(
        {
            "schema": REQUEST_SCHEMA,
            "kind": f"{args.kind}-observation",
            "operation": args.operation,
            "scope": scope,
        }
    )
    decision = decide_request(request)
    run_id = f"{utc_now().strftime('%Y%m%dT%H%M%SZ')}-{sha256_json(request)[:12]}-{uuid.uuid4().hex[:8]}"
    run_dir = root / "runs" / run_id
    ensure_dir(run_dir)
    write_private(run_dir / "request.json", request)
    write_private(run_dir / "decision.json", decision)
    receipt = {
        "schema": "lazy-broker-observation-receipt/v1",
        "run_id": run_id,
        "request_sha256": sha256_json(request),
        "decision_sha256": sha256_json(decision),
        "route": decision["route"],
        "raw_content_emitted": False,
    }
    write_private(run_dir / "receipt.json", receipt)
    print(
        json.dumps(
            {
                "run_id": run_id,
                "route": decision["route"],
                "original_action_executable": decision["original_action_executable"],
                "next": decision["next"],
                "decision": str(run_dir / "decision.json"),
            },
            sort_keys=True,
        )
    )
    return 0


def routing_run(root: Path, prefix: str, request: dict[str, Any], decision: dict[str, Any]) -> str:
    run_id = f"{utc_now().strftime('%Y%m%dT%H%M%SZ')}-{prefix}-{sha256_json(request)[:12]}-{uuid.uuid4().hex[:8]}"
    run_dir = root / "runs" / run_id
    ensure_dir(run_dir)
    write_private(run_dir / "request.json", request)
    write_private(run_dir / "decision.json", decision)
    write_private(
        run_dir / "receipt.json",
        {
            "schema": "lazy-routing-receipt/v1",
            "run_id": run_id,
            "request_sha256": sha256_json(request),
            "decision_sha256": sha256_json(decision),
            "route": decision.get("route") or decision.get("outcome"),
            "raw_content_emitted": False,
        },
    )
    return run_id


def route_main(args: argparse.Namespace) -> int:
    root = state_root(args)
    request = {
        "schema": "lazy-offload-request/v1",
        "taskRef": args.task_ref,
        "kind": args.kind,
        "urgency": args.urgency,
        "deterministicObservation": args.deterministic_observation,
        "ownerQueryAvailable": args.owner_query,
        "requiresNovelJudgment": args.novel_judgment,
        "requiresRepositoryMutation": args.repository_mutation,
        "requiresLiveBrowser": args.live_browser,
        "consequentialEffect": args.consequential_effect,
        "carrierContextAdvantage": args.carrier_context,
        "retainedCarrierContinuation": args.retained_carrier_continuation,
        "parallelizable": args.parallelizable,
        "containsSensitiveMaterial": args.sensitive,
        "expectedOutputChars": args.expected_chars,
    }
    decision = route_task(request)
    run_id = routing_run(root, "route", request, decision)
    print(
        json.dumps(
            {
                "run_id": run_id,
                "task_ref": decision["taskRef"],
                "route": decision["route"],
                "wake_on": decision["wakeOn"],
                "browser_message_requires_action_time_confirmation": decision[
                    "browserMessageRequiresActionTimeConfirmation"
                ],
                "campaign_summary": {
                    "ref": f"route:{run_id}",
                    "generation": 1,
                    "route": decision["route"],
                    "fingerprint": decision["requestSha256"],
                    "authorizesWork": False,
                    "authorizesEffects": False,
                    "authorizesDispatch": False,
                },
                "decision": str(root / "runs" / run_id / "decision.json"),
            },
            sort_keys=True,
        )
    )
    return 0


def load_bounded_json(path: Path) -> Any:
    resolved = path.expanduser().resolve()
    if not resolved.is_file() or resolved.stat().st_size > 1_000_000:
        raise SystemExit("JSON input is missing or exceeds the 1 MB bound")
    try:
        return json.loads(resolved.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise SystemExit("JSON input is invalid") from error


def lane_budget_main(args: argparse.Namespace) -> int:
    root = state_root(args)
    request = load_bounded_json(args.inventory)
    decision = plan_lane_budget(request, args.desired_new)
    run_id = routing_run(root, "lanes", request, decision)
    print(
        json.dumps(
            {
                "run_id": run_id,
                "outcome": decision["outcome"],
                "admit_now": decision["admitNow"],
                "responsive": decision["responsive"],
                "retained": decision["retained"],
                "requested_lifecycle_actions": decision["requestedLifecycleActions"],
                "campaign_summary": {
                    "ref": f"lane-plan:{run_id}",
                    "generation": 1,
                    "route": None,
                    "status": "ready" if decision["admitNow"] else "blocked",
                    "fingerprint": sha256_json(decision),
                    "authorizesWork": False,
                    "authorizesEffects": False,
                    "authorizesDispatch": False,
                },
                "decision": str(root / "runs" / run_id / "decision.json"),
            },
            sort_keys=True,
        )
    )
    return 0


def control_service_main(args: argparse.Namespace) -> int:
    root = state_root(args)
    request = load_bounded_json(args.inventory)
    decision = plan_control_services(request, args.freshness_seconds)
    run_id = routing_run(root, "controls", request, decision)
    print(
        json.dumps(
            {
                "run_id": run_id,
                "outcome": decision["outcome"],
                "active_services": decision["activeServices"],
                "healthy_services": decision["healthyServices"],
                "control_surface_count": decision["controlSurfaceCount"],
                "new_window_recommended": decision["newWindowRecommended"],
                "requested_lifecycle_actions": decision["requestedLifecycleActions"],
                "campaign_summary": {
                    "ref": f"control-plan:{run_id}",
                    "generation": 1,
                    "route": None,
                    "status": (
                        "ready"
                        if decision["healthyServices"] > 0
                        and not decision["newWindowRecommended"]
                        else "stale"
                    ),
                    "fingerprint": sha256_json(decision),
                    "authorizesWork": False,
                    "authorizesEffects": False,
                    "authorizesDispatch": False,
                },
                "decision": str(root / "runs" / run_id / "decision.json"),
            },
            sort_keys=True,
        )
    )
    return 0


def settle_route_main(args: argparse.Namespace) -> int:
    root = state_root(args)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,239}", args.run_id):
        raise SystemExit("run-id is not a bounded routing identifier")
    run_dir = root / "runs" / args.run_id
    decision_path = run_dir / "decision.json"
    if not decision_path.is_file():
        raise SystemExit("routing run does not exist")
    settlement_path = run_dir / "settlement.json"
    if settlement_path.exists():
        raise SystemExit("carrier effect is already settled")
    decision = load_bounded_json(decision_path)
    try:
        settlement = settle_carrier_effect(
            decision,
            args.outcome,
            args.conversation_ref,
        )
    except ValueError as error:
        raise SystemExit(str(error)) from error
    settlement["settledAt"] = iso(utc_now())
    write_private(settlement_path, settlement)
    print(
        json.dumps(
            {
                "run_id": args.run_id,
                "outcome": settlement["outcome"],
                "effect_confirmed": settlement["effectConfirmed"],
                "retry_authorized": settlement["retryAuthorized"],
                "requires_reconciliation": settlement["requiresReconciliation"],
                "next": settlement["next"],
                "settlement": str(settlement_path),
            },
            sort_keys=True,
        )
    )
    return 0


def settle_worker_main(args: argparse.Namespace) -> int:
    root = state_root(args)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,239}", args.run_id):
        raise SystemExit("run-id is not a bounded routing identifier")
    run_dir = root / "runs" / args.run_id
    decision_path = run_dir / "decision.json"
    if not decision_path.is_file():
        raise SystemExit("routing run does not exist")
    settlement_path = run_dir / "worker-settlement.json"
    if settlement_path.exists():
        raise SystemExit("worker execution is already settled")
    decision = load_bounded_json(decision_path)
    try:
        settlement = settle_worker_execution(
            decision,
            args.outcome,
            args.execution_ref,
            args.artifact_sha256,
            args.artifact_bytes,
        )
    except ValueError as error:
        raise SystemExit(str(error)) from error
    settlement["settledAt"] = iso(utc_now())
    write_private(settlement_path, settlement)
    print(json.dumps({
        "run_id": args.run_id,
        "outcome": settlement["outcome"],
        "requires_reconciliation": settlement["requiresReconciliation"],
        "retry_authorized": settlement["retryAuthorized"],
        "next": settlement["next"],
        "settlement": str(settlement_path),
    }, sort_keys=True))
    return 0


def drain_due(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    root = state_root(args)
    completed: list[dict[str, Any]] = []
    worker_exit = 0
    for _ in range(args.limit):
        claimed = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--state-root",
                str(root),
                "claim",
                "--worker",
                args.worker,
                "--lease-seconds",
                str(args.lease_seconds),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if claimed.returncode != 0:
            worker_exit = claimed.returncode
            break
        claim_projection = json.loads(claimed.stdout)
        if not claim_projection["claimed"]:
            break
        executed = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--state-root",
                str(root),
                "execute",
                "--claim",
                claim_projection["claim"],
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if executed.stdout:
            try:
                result = json.loads(executed.stdout)
                completed.append(
                    {
                        "task_id": result.get("task_id"),
                        "attempt": result.get("attempt"),
                        "status": result.get("status", "attention"),
                        "route": result.get("route"),
                        "exit_code": result.get("exit_code"),
                    }
                )
            except json.JSONDecodeError:
                completed.append(
                    {"task_id": claim_projection["task_id"], "status": "attention"}
                )
        if executed.returncode != 0:
            worker_exit = executed.returncode
            break
    return {
        "worker": hashlib.sha256(args.worker.encode("utf-8")).hexdigest()[:12],
        "processed": len(completed),
        "tasks": completed,
        "raw_content_emitted": False,
    }, worker_exit


def tick_main(args: argparse.Namespace) -> int:
    report, exit_code = drain_due(args)
    print(json.dumps(report, sort_keys=True))
    return exit_code


def next_deadline(root: Path) -> datetime | None:
    connection = database(root)
    try:
        value = connection.execute(
            "SELECT MIN(eligible_at) FROM tasks WHERE status='queued'"
        ).fetchone()[0]
    finally:
        connection.close()
    return parse_at(value) if value else None


def worker_main(args: argparse.Namespace) -> int:
    if args.once:
        return tick_main(args)
    root = state_root(args)
    lock_path = root / "worker.lock"
    lock_descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_descriptor)
        raise SystemExit("another Lazy Commander worker owns this state root")
    socket_path = root / "wake.sock"
    if socket_path.exists():
        if not stat.S_ISSOCK(socket_path.stat().st_mode):
            raise SystemExit("worker wake path exists and is not a socket")
        socket_path.unlink()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    listener.bind(str(socket_path))
    socket_path.chmod(0o600)
    try:
        while True:
            report, exit_code = drain_due(args)
            if report["processed"] or exit_code:
                print(json.dumps(report, sort_keys=True), flush=True)
            deadline = next_deadline(root)
            timeout = None if deadline is None else max(
                0.0, (deadline - utc_now()).total_seconds()
            )
            if exit_code:
                timeout = 30.0 if timeout is None else max(timeout, 30.0)
            listener.settimeout(timeout)
            try:
                listener.recv(1)
            except socket.timeout:
                pass
    finally:
        listener.close()
        if socket_path.exists():
            socket_path.unlink()
        os.close(lock_descriptor)


def status_main(args: argparse.Namespace) -> int:
    root = state_root(args)
    connection = database(root)
    now = iso(utc_now())
    try:
        counts = {
            row["status"]: row["count"]
            for row in connection.execute(
                "SELECT status, COUNT(*) AS count FROM tasks GROUP BY status"
            )
        }
        next_eligible = connection.execute(
            "SELECT MIN(eligible_at) FROM tasks WHERE status='queued'"
        ).fetchone()[0]
        due = connection.execute(
            "SELECT COUNT(*) FROM tasks WHERE status='queued' AND eligible_at <= ?",
            (now,),
        ).fetchone()[0]
        attention = [
            row["id"]
            for row in connection.execute(
                """SELECT id FROM tasks WHERE status IN ('failed','attention','ambiguous')
                   ORDER BY updated_at DESC LIMIT 20"""
            )
        ]
        campaign_acceptance_count = connection.execute(
            "SELECT COUNT(*) FROM campaign_acceptances"
        ).fetchone()[0]
        campaign_budgets = [
            {
                "budget_ref": row["budget_ref"],
                "generation": row["generation"],
                "limit_tokens": row["limit_tokens"],
                "reserved_tokens": row["reserved_tokens"],
                "consumed_tokens": row["consumed_tokens"],
            }
            for row in connection.execute(
                "SELECT budget_ref, generation, limit_tokens, reserved_tokens, consumed_tokens FROM campaign_budgets ORDER BY budget_ref LIMIT 20"
            )
        ]
    finally:
        connection.close()
    print(
        json.dumps(
            {
                "now": now,
                "counts": counts,
                "due": due,
                "next_eligible_at": next_eligible,
                "attention": attention,
                "attention_truncated": sum(
                    counts.get(name, 0) for name in ("failed", "attention", "ambiguous")
                ) > len(attention),
                "worker_socket": (root / "wake.sock").exists(),
                "campaign_acceptances": campaign_acceptance_count,
                "campaign_budgets": campaign_budgets,
                "raw_content_emitted": False,
            },
            sort_keys=True,
        )
    )
    return 0


def show_main(args: argparse.Namespace) -> int:
    root = state_root(args)
    connection = database(root)
    try:
        row = connection.execute(
            """SELECT id, created_at, eligible_at, updated_at, status, request_sha256,
               attempts, lease_expires_at, last_route, last_exit_code FROM tasks WHERE id=?""",
            (args.id,),
        ).fetchone()
        if row is None:
            raise SystemExit("task does not exist")
        events = list(
            connection.execute(
                "SELECT sequence, at, kind, detail_json FROM events WHERE task_id=? ORDER BY sequence",
                (args.id,),
            )
        )
    finally:
        connection.close()
    print(
        json.dumps(
            {
                "task": dict(row),
                "events": [
                    {"sequence": event_row["sequence"], "at": event_row["at"], "kind": event_row["kind"], "detail": json.loads(event_row["detail_json"])}
                    for event_row in events
                ],
            },
            sort_keys=True,
        )
    )
    return 0


def campaign_controller_main(args: argparse.Namespace) -> int:
    output_dir = args.output_dir
    if output_dir is None:
        root = state_root(args)
        view = compile_campaign_controller(load_campaign_snapshot(args.input))
        output_dir = root / "campaigns" / view["decisionFingerprint"]
    receipt = run_campaign_controller(args.input, output_dir)
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


def load_campaign_view(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file() or resolved.stat().st_size > 1_000_000:
        raise SystemExit("campaign view is missing or exceeds the 1 MB bound")
    try:
        view = json.loads(resolved.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SystemExit("campaign view must be valid UTF-8 JSON") from error
    if not isinstance(view, dict) or view.get("schema") != "lazy-campaign-controller-view/v1":
        raise SystemExit("campaign view schema is unsupported")
    proposal = view.get("acceptanceProposal")
    proposal_keys = set(proposal) if isinstance(proposal, dict) else set()
    base_proposal_keys = {
        "transitionId", "ownerRef", "evaluatedOwnerGeneration",
        "evaluatedOwnerFingerprint", "requestedCodexReservationTokens",
        "authorizesWork", "authorizesEffects", "authorizesDispatch",
    }
    if proposal_keys not in {frozenset(base_proposal_keys), frozenset(base_proposal_keys | {"evaluatedCursorVectorFingerprint"})}:
        raise SystemExit("campaign acceptance proposal is missing or malformed")
    if any(proposal[key] is not False for key in ("authorizesWork", "authorizesEffects", "authorizesDispatch")):
        raise SystemExit("campaign acceptance proposal must grant no authority")
    if proposal["transitionId"] != view.get("decisionFingerprint"):
        raise SystemExit("campaign transition identity does not bind the decision")
    if proposal["ownerRef"] != view.get("campaignRef") or proposal["evaluatedOwnerGeneration"] != view.get("campaignGeneration"):
        raise SystemExit("campaign owner version does not bind the view")
    if proposal["evaluatedOwnerFingerprint"] != view.get("snapshotSha256"):
        raise SystemExit("campaign owner fingerprint does not bind the snapshot")
    requested = proposal["requestedCodexReservationTokens"]
    if isinstance(requested, bool) or not isinstance(requested, int) or requested < 0:
        raise SystemExit("campaign reservation must be a non-negative integer")
    return view


def accept_campaign_main(args: argparse.Namespace) -> int:
    """Atomically journal one proposal and reserve Codex budget; execute nothing."""

    root = state_root(args)
    view = load_campaign_view(args.view)
    proposal = view["acceptanceProposal"]
    if args.owner_generation != proposal["evaluatedOwnerGeneration"]:
        raise SystemExit("owner generation changed before campaign acceptance")
    if args.owner_fingerprint != proposal["evaluatedOwnerFingerprint"]:
        raise SystemExit("owner fingerprint changed before campaign acceptance")
    counters = (args.budget_limit, args.budget_reserved, args.budget_consumed)
    if any(value < 0 for value in counters) or args.budget_generation < 1:
        raise SystemExit("budget generation and counters are outside their bounds")
    if args.budget_reserved + args.budget_consumed > args.budget_limit:
        raise SystemExit("budget reserved plus consumed exceeds limit")

    transition_id = proposal["transitionId"]
    view_fingerprint = sha256_json(view)
    requested = proposal["requestedCodexReservationTokens"]
    connection = database(root)
    now = iso(utc_now())
    try:
        connection.execute("BEGIN IMMEDIATE")
        replay = connection.execute(
            "SELECT * FROM campaign_acceptances WHERE transition_id=?",
            (transition_id,),
        ).fetchone()
        if replay is not None:
            exact = (
                replay["campaign_ref"] == proposal["ownerRef"]
                and replay["campaign_generation"] == args.owner_generation
                and replay["owner_fingerprint"] == args.owner_fingerprint
                and replay["view_fingerprint"] == view_fingerprint
                and replay["budget_ref"] == args.budget_ref
                and replay["budget_generation_before"] == args.budget_generation
                and replay["budget_limit"] == args.budget_limit
                and replay["budget_reserved_before"] == args.budget_reserved
                and replay["budget_consumed_before"] == args.budget_consumed
                and replay["reservation_tokens"] == requested
            )
            if not exact:
                raise SystemExit("campaign acceptance replay conflicts with the durable ledger")
            connection.commit()
            outcome = "replay"
            generation_after = replay["budget_generation_after"]
            reserved_after = replay["budget_reserved_before"] + requested
        else:
            budget = connection.execute(
                "SELECT * FROM campaign_budgets WHERE budget_ref=?",
                (args.budget_ref,),
            ).fetchone()
            if budget is None:
                connection.execute(
                    "INSERT INTO campaign_budgets VALUES (?, ?, ?, ?, ?, ?)",
                    (args.budget_ref, args.budget_generation, args.budget_limit, args.budget_reserved, args.budget_consumed, now),
                )
            elif (
                budget["generation"] != args.budget_generation
                or budget["limit_tokens"] != args.budget_limit
                or budget["reserved_tokens"] != args.budget_reserved
                or budget["consumed_tokens"] != args.budget_consumed
            ):
                raise SystemExit("budget generation or counters changed before campaign acceptance")
            if args.budget_reserved + args.budget_consumed + requested > args.budget_limit:
                connection.rollback()
                print(json.dumps({
                    "outcome": "budget-blocked",
                    "transition_id": transition_id,
                    "requested_tokens": requested,
                    "authorizesWork": False,
                    "authorizesEffects": False,
                    "authorizesDispatch": False,
                }, sort_keys=True, separators=(",", ":")))
                return 3
            generation_after = args.budget_generation + 1
            reserved_after = args.budget_reserved + requested
            changed = connection.execute(
                "UPDATE campaign_budgets SET generation=?, reserved_tokens=?, updated_at=? WHERE budget_ref=? AND generation=? AND reserved_tokens=? AND consumed_tokens=?",
                (generation_after, reserved_after, now, args.budget_ref, args.budget_generation, args.budget_reserved, args.budget_consumed),
            ).rowcount
            if changed != 1:
                raise SystemExit("budget compare-and-swap failed")
            connection.execute(
                "INSERT INTO campaign_acceptances VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    transition_id, proposal["ownerRef"], args.owner_generation,
                    args.owner_fingerprint, view_fingerprint, args.budget_ref,
                    args.budget_generation, generation_after, args.budget_limit,
                    args.budget_reserved, args.budget_consumed, requested, now,
                ),
            )
            connection.commit()
            outcome = "accepted"
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()

    print(json.dumps({
        "schema": "lazy-campaign-acceptance-receipt/v1",
        "outcome": outcome,
        "transition_id": transition_id,
        "owner_ref": proposal["ownerRef"],
        "owner_generation": args.owner_generation,
        "owner_fingerprint": args.owner_fingerprint,
        "budget_ref": args.budget_ref,
        "budget_generation_before": args.budget_generation,
        "budget_generation_after": generation_after,
        "reserved_tokens_after": reserved_after,
        "requested_tokens": requested,
        "executesWork": False,
        "authorizesWork": False,
        "authorizesEffects": False,
        "authorizesDispatch": False,
    }, sort_keys=True, separators=(",", ":")))
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument(
        "--state-root",
        type=Path,
        default=Path(os.environ.get("LAZY_COMMANDER_STATE_ROOT", Path.home() / ".codex" / "state" / "lazy-commander")),
    )
    commands = root.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--cwd", type=Path, default=Path.cwd())
    run.add_argument("--output-dir", type=Path)
    run.add_argument("--observation-only", action="store_true")
    run.add_argument("argv", nargs=argparse.REMAINDER)
    defer = commands.add_parser("defer")
    timing = defer.add_mutually_exclusive_group(required=True)
    timing.add_argument("--after", type=parse_duration)
    timing.add_argument("--at", type=parse_at)
    defer.add_argument("--cwd", type=Path, default=Path.cwd())
    defer.add_argument("argv", nargs=argparse.REMAINDER)
    due = commands.add_parser("due")
    due.add_argument("--limit", type=int, default=20)
    claim = commands.add_parser("claim")
    claim.add_argument("--worker", required=True)
    claim.add_argument("--lease-seconds", type=int, default=300)
    execute = commands.add_parser("execute")
    execute.add_argument("--claim", type=Path, required=True)
    settle = commands.add_parser("settle")
    settle.add_argument("--id", required=True)
    settle.add_argument("--outcome", choices=("complete", "ambiguous", "verified-no-effect", "requeue"), required=True)
    settle.add_argument("--after", type=parse_duration, default=timedelta(0))
    show = commands.add_parser("show")
    show.add_argument("--id", required=True)
    observe = commands.add_parser("observe")
    observe.add_argument("--kind", choices=("browser", "tool"), default="browser")
    observe.add_argument("--operation", required=True)
    observe.add_argument("--owner-query", action="store_true")
    observe.add_argument("--semantic-delta", action="store_true")
    observe.add_argument("--selector-bounded", action="store_true")
    observe.add_argument("--visual-semantics", action="store_true")
    observe.add_argument("--expected-chars", type=int)
    observe.add_argument("--expected-items", type=int)
    tick = commands.add_parser("tick")
    tick.add_argument("--worker", required=True)
    tick.add_argument("--limit", type=int, default=20)
    tick.add_argument("--lease-seconds", type=int, default=900)
    worker = commands.add_parser("worker")
    worker.add_argument("--worker", required=True)
    worker.add_argument("--limit", type=int, default=20)
    worker.add_argument("--lease-seconds", type=int, default=900)
    worker.add_argument("--once", action="store_true")
    commands.add_parser("status")
    route = commands.add_parser("route")
    route.add_argument("--task-ref", required=True)
    route.add_argument("--kind", choices=("coding", "research", "browser", "coordination", "verification"), required=True)
    route.add_argument("--urgency", choices=("interactive", "background"), default="interactive")
    route.add_argument("--deterministic-observation", action="store_true")
    route.add_argument("--owner-query", action="store_true")
    route.add_argument("--novel-judgment", action="store_true")
    route.add_argument("--repository-mutation", action="store_true")
    route.add_argument("--live-browser", action="store_true")
    route.add_argument("--consequential-effect", action="store_true")
    route.add_argument("--carrier-context", action="store_true")
    route.add_argument("--retained-carrier-continuation", action="store_true")
    route.add_argument("--parallelizable", action="store_true")
    route.add_argument("--sensitive", action="store_true")
    route.add_argument("--expected-chars", type=int, default=0)
    lanes = commands.add_parser("plan-lanes")
    lanes.add_argument("--inventory", type=Path, required=True)
    lanes.add_argument("--desired-new", type=int, default=1)
    controls = commands.add_parser("plan-controls")
    controls.add_argument("--inventory", type=Path, required=True)
    controls.add_argument("--freshness-seconds", type=int, default=300)
    campaign = commands.add_parser("campaign")
    campaign.add_argument("--input", type=Path, required=True)
    campaign.add_argument("--output-dir", type=Path)
    accept_campaign = commands.add_parser("accept-campaign")
    accept_campaign.add_argument("--view", type=Path, required=True)
    accept_campaign.add_argument("--owner-generation", type=int, required=True)
    accept_campaign.add_argument("--owner-fingerprint", required=True)
    accept_campaign.add_argument("--budget-ref", required=True)
    accept_campaign.add_argument("--budget-generation", type=int, required=True)
    accept_campaign.add_argument("--budget-limit", type=int, required=True)
    accept_campaign.add_argument("--budget-reserved", type=int, required=True)
    accept_campaign.add_argument("--budget-consumed", type=int, required=True)
    settle_route = commands.add_parser("settle-route")
    settle_route.add_argument("--run-id", required=True)
    settle_route.add_argument(
        "--outcome",
        choices=("committed", "verified-no-effect", "ambiguous"),
        required=True,
    )
    settle_route.add_argument("--conversation-ref")
    settle_worker = commands.add_parser("settle-worker")
    settle_worker.add_argument("--run-id", required=True)
    settle_worker.add_argument("--outcome", choices=("committed", "failed", "ambiguous"), required=True)
    settle_worker.add_argument("--execution-ref", required=True)
    settle_worker.add_argument("--artifact-sha256")
    settle_worker.add_argument("--artifact-bytes", type=int)
    return root


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    args = parser().parse_args(argv)
    if args.command == "run":
        return run_main(args)
    if args.command == "defer":
        return defer_main(args)
    if args.command == "due":
        return due_main(args)
    if args.command == "claim":
        if args.lease_seconds < 10 or args.lease_seconds > 86400:
            raise SystemExit("lease-seconds must be between 10 and 86400")
        return claim_main(args)
    if args.command == "execute":
        return execute_main(args)
    if args.command == "settle":
        return settle_main(args)
    if args.command == "observe":
        return observation_main(args)
    if args.command in {"tick", "worker"}:
        if args.limit < 1 or args.limit > 1000:
            raise SystemExit("limit must be between 1 and 1000")
        if args.lease_seconds < 10 or args.lease_seconds > 86400:
            raise SystemExit("lease-seconds must be between 10 and 86400")
        return tick_main(args) if args.command == "tick" else worker_main(args)
    if args.command == "status":
        return status_main(args)
    if args.command == "route":
        return route_main(args)
    if args.command == "plan-lanes":
        return lane_budget_main(args)
    if args.command == "plan-controls":
        return control_service_main(args)
    if args.command == "campaign":
        return campaign_controller_main(args)
    if args.command == "accept-campaign":
        return accept_campaign_main(args)
    if args.command == "settle-route":
        return settle_route_main(args)
    if args.command == "settle-worker":
        return settle_worker_main(args)
    return show_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
