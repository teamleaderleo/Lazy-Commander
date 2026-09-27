#!/usr/bin/env python3
"""Opt-in Codex PreToolUse hook for noisy shell commands."""

from __future__ import annotations

import hashlib
import grp
import json
import os
import pwd
import re
import shlex
import shutil
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA = "lazy-semantic-command/v1"
# Codex currently reports no-prompt sessions as ``dontAsk``. Keep ``never``
# for older clients that used the approval-policy spelling in hook payloads.
ALLOWED_PERMISSION_MODES = {"bypassPermissions", "dontAsk", "never"}
COMMAND_SEPARATORS = {";", ";;", ";&", "&", "&&", "||", "|", "|&", "\n", "(", ")"}
PIPE_SEPARATORS = {"|", "|&"}
REDIRECTIONS = {"<", ">", "<<", "<<-", ">>", "<<<", "<>", "<&", ">&", ">|"}
SHELL_KEYWORDS = {
    "!", "{", "}", "case", "do", "done", "elif", "else", "esac", "fi", "for",
    "function", "if", "in", "select", "then", "until", "while",
}
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.S)
FINITE_SED_PRINT = re.compile(r"\s*\d+(?:\s*,\s*\d+)?p(?:\s*;\s*\d+(?:\s*,\s*\d+)?p)*\s*")
QUOTED_SHELL_METACHARS = frozenset(";&|()<>\n")


def private_group(gid: int) -> bool:
    current = pwd.getpwuid(os.getuid()).pw_name
    try:
        members = set(grp.getgrgid(gid).gr_mem)
    except KeyError:
        return False
    members.update(entry.pw_name for entry in pwd.getpwall() if entry.pw_gid == gid)
    return members <= {current}


def runner_path() -> str | None:
    candidate = shutil.which("lazy-command")
    expected = Path(__file__).with_name("semantic_command.py").resolve()
    try:
        metadata = expected.stat()
    except OSError:
        return None
    if metadata.st_uid != os.getuid():
        return None
    mode = stat.S_IMODE(metadata.st_mode)
    if mode & 0o002:
        return None
    if mode & 0o020 and not private_group(metadata.st_gid):
        return None
    if candidate:
        path = Path(candidate)
        try:
            if path.resolve(strict=True) == expected:
                return str(path.absolute())
        except OSError:
            pass
    return str(expected)


def state_root() -> Path:
    configured = os.environ.get("LAZY_COMMAND_STATE_ROOT")
    root = Path(configured).expanduser() if configured else Path.home() / ".codex" / "state" / "lazy-command"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    return root.resolve()


def digest_json(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _executable_argv(words: list[str]) -> tuple[str, ...] | None:
    """Return one simple command's executable argv after harmless prefixes."""
    index = 0
    while index < len(words):
        if ASSIGNMENT.fullmatch(words[index]):
            index += 1
            continue
        if words[index] in REDIRECTIONS and index + 1 < len(words):
            index += 2
            continue
        if (
            words[index].isdigit()
            and index + 2 < len(words)
            and words[index + 1] in REDIRECTIONS
        ):
            index += 3
            continue
        break
    if index >= len(words):
        return ()

    executable = Path(words[index]).name.lower()
    if executable in SHELL_KEYWORDS:
        return None
    if executable in {"command", "builtin", "exec", "nohup", "time"}:
        index += 1
        while index < len(words) and words[index].startswith("-"):
            index += 1
    elif executable == "env":
        index += 1
        while index < len(words) and (words[index].startswith("-") or ASSIGNMENT.fullmatch(words[index])):
            index += 1
    if index >= len(words):
        return ()
    return tuple(words[index:])


def _safe_shell_text(command: str) -> str | None:
    """Remove comments while rejecting syntax whose quote provenance shlex loses."""
    if any(marker in command for marker in ("$(", "`", "<(", ">(")):
        return None

    output: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(command):
        character = command[index]
        if quote == "'":
            if character == "'":
                quote = None
            elif character in QUOTED_SHELL_METACHARS:
                return None
            output.append(character)
            index += 1
            continue
        if quote == '"':
            if character == '"':
                quote = None
            elif character in QUOTED_SHELL_METACHARS:
                return None
            elif character == "\\" and index + 1 < len(command):
                if command[index + 1] in QUOTED_SHELL_METACHARS:
                    return None
                output.extend((character, command[index + 1]))
                index += 2
                continue
            output.append(character)
            index += 1
            continue

        if character in {"'", '"'}:
            quote = character
            output.append(character)
            index += 1
            continue
        if character == "\\" and index + 1 < len(command):
            if command[index + 1] in QUOTED_SHELL_METACHARS:
                return None
            output.extend((character, command[index + 1]))
            index += 2
            continue
        if character == "#":
            newline = command.find("\n", index)
            if newline < 0:
                break
            output.append("\n")
            index = newline + 1
            continue
        output.append(character)
        index += 1
    return "".join(output)


def _parsed_commands(command: str) -> list[tuple[str | None, tuple[str, ...]]] | None:
    """Parse top-level executable positions, declining shell forms we cannot classify safely."""
    shell_text = _safe_shell_text(command)
    if shell_text is None:
        return None
    lexer = shlex.shlex(shell_text, posix=True, punctuation_chars=";&|()<>\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    if "<<" in tokens or "<<-" in tokens:
        return None

    parsed: list[tuple[str | None, tuple[str, ...]]] = []
    words: list[str] = []
    preceding: str | None = None
    for token in tokens:
        if token not in COMMAND_SEPARATORS:
            words.append(token)
            continue
        if words:
            argv = _executable_argv(words)
            if argv is None:
                return None
            if argv:
                parsed.append((preceding, argv))
            words = []
        preceding = token
    if words:
        argv = _executable_argv(words)
        if argv is None:
            return None
        if argv:
            parsed.append((preceding, argv))
    return parsed


def command_argvs(command: str) -> list[tuple[str, ...]] | None:
    """Return actual shell command invocations without matching quoted labels or comments."""
    parsed = _parsed_commands(command)
    if parsed is None:
        return None
    return [argv for _, argv in parsed]


def _command_name(argv: tuple[str, ...]) -> str:
    return Path(argv[0]).name.lower()


def _is_interactive(argv: tuple[str, ...]) -> bool:
    name = _command_name(argv)
    lowered = [part.lower() for part in argv]
    if name in {"watch", "top", "htop", "less", "more"}:
        return True
    if name == "tail" and any(part in {"-f", "--follow"} or part.startswith("--follow=") for part in lowered[1:]):
        return True
    return name == "npm" and lowered[1:3] == ["run", "dev"]


def _is_content_read(argv: tuple[str, ...]) -> bool:
    lowered = [part.lower() for part in argv]
    if _command_name(argv) == "git" and lowered[1:2] == ["diff"]:
        return not any(part in {"--stat", "--check", "--name-only"} for part in lowered[2:])
    return (
        _command_name(argv) == "gh"
        and lowered[1:3] in (["pr", "view"], ["issue", "view"])
        and "--json" not in lowered[3:]
    )


def _is_noisy(argv: tuple[str, ...]) -> bool:
    name = _command_name(argv)
    lowered = [part.lower() for part in argv]
    if name == "codex":
        return lowered[1:2] == ["exec"]
    if name == "gh":
        return bool(lowered[1:2] and lowered[1] in {"pr", "run", "issue", "workflow"})
    if name == "git":
        return bool(
            lowered[1:2]
            and lowered[1] in {"pull", "fetch", "clone", "switch", "checkout", "merge", "rebase", "status"}
        )
    if name in {"rg", "find", "pytest", "vitest", "jest"}:
        return True
    if re.fullmatch(r"python3?(?:\.\d+)*", name) and lowered[1:3] == ["-m", "unittest"]:
        return True
    if name == "go":
        return lowered[1:2] == ["test"]
    if name == "cargo":
        return bool(lowered[1:2] and lowered[1] in {"test", "check", "build"})
    if name in {"npm", "bun", "pnpm"}:
        return bool(lowered[1:2] and lowered[1] in {"test", "install", "run"})
    return False


def _is_finite_projector(argv: tuple[str, ...]) -> bool:
    """Recognize terminal selectors whose output shape the caller chose explicitly."""
    name = _command_name(argv)
    lowered = [part.lower() for part in argv]
    if name in {"jq", "wc"}:
        return True
    if name == "head":
        return True
    if name == "tail":
        if _is_interactive(argv):
            return False
        values = lowered[1:]
        for index, part in enumerate(values):
            if part in {"-n", "--lines"} and index + 1 < len(values):
                return not values[index + 1].startswith("+")
            if part.startswith("--lines="):
                return not part.removeprefix("--lines=").startswith("+")
        return not any(part.startswith("+") for part in values)
    if name == "sed" and "-n" in lowered[1:]:
        scripts = [part for part in argv[1:] if not part.startswith("-")]
        return any(FINITE_SED_PRINT.fullmatch(script) for script in scripts)
    return False


def _has_native_projection(argv: tuple[str, ...]) -> bool:
    """Recognize a noisy command's own explicit structured or row projection."""
    name = _command_name(argv)
    lowered = [part.lower() for part in argv]
    options = lowered[1:]
    if name == "gh":
        return any(
            part in {"--json", "--jq", "--template", "--limit"}
            or part.startswith(("--json=", "--jq=", "--template=", "--limit="))
            for part in options
        )
    if name == "rg":
        return any(
            part in {
                "-c", "-l", "-m", "--json", "--count", "--count-matches", "--files-with-matches",
                "--files-without-match", "--max-count",
            }
            or part.startswith("--max-count=")
            or re.fullmatch(r"-m\d+", part)
            for part in options
        )
    return False


def _has_explicit_projection(parsed: list[tuple[str | None, tuple[str, ...]]]) -> bool:
    chain_is_noisy = False
    for preceding, argv in parsed:
        if preceding not in PIPE_SEPARATORS:
            chain_is_noisy = False
        if preceding in PIPE_SEPARATORS and chain_is_noisy and _is_finite_projector(argv):
            return True
        if _is_noisy(argv) and _has_native_projection(argv):
            return True
        chain_is_noisy = chain_is_noisy or _is_noisy(argv)
    return False


def should_wrap(payload: dict[str, Any], command: str) -> bool:
    if payload.get("tool_name") != "Bash":
        return False
    if payload.get("permission_mode") not in ALLOWED_PERMISSION_MODES:
        return False
    parsed = _parsed_commands(command)
    if parsed is None or not parsed:
        return False
    commands = [argv for _, argv in parsed]
    if any(_command_name(argv) in {"lc", "lazy-command", "semantic_command.py"} for argv in commands):
        return False
    if any(_is_interactive(argv) for argv in commands):
        return False
    if any(_is_content_read(argv) for argv in commands):
        return False
    if _has_explicit_projection(parsed):
        return False
    return any(_is_noisy(argv) for argv in commands)


def command_cwd(payload: dict[str, Any]) -> str:
    """Return the shell tool's requested cwd, not merely the session cwd."""
    session = payload.get("cwd")
    if not isinstance(session, str) or not session:
        raise SystemExit("semantic-command session cwd unavailable")
    requested = payload.get("tool_input", {}).get("workdir")
    if not isinstance(requested, str) or not requested:
        return session
    candidate = Path(requested).expanduser()
    if not candidate.is_absolute():
        candidate = Path(session) / candidate
    return os.fspath(candidate.resolve())


def store(payload: dict[str, Any], command: str) -> str:
    cwd = command_cwd(payload)
    identity = {
        "session_id": payload.get("session_id"),
        "turn_id": payload.get("turn_id"),
        "tool_use_id": payload.get("tool_use_id"),
        "command": command,
        "cwd": cwd,
    }
    identifier = digest_json(identity)[:16]
    run_dir = state_root() / identifier
    run_dir.mkdir(mode=0o700, exist_ok=True)
    run_dir.chmod(0o700)
    request = {
        "schema": SCHEMA,
        "id": identifier,
        "command": command,
        "cwd": cwd,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "session_id": payload.get("session_id"),
        "turn_id": payload.get("turn_id"),
        "tool_use_id": payload.get("tool_use_id"),
        "max_output_bytes": 64_000_000,
    }
    request["request_sha256"] = digest_json(request)
    path = run_dir / "request.json"
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        ignored = {"created_at", "request_sha256"}
        comparable = {key: value for key, value in existing.items() if key not in ignored}
        candidate = {key: value for key, value in request.items() if key not in ignored}
        if comparable != candidate:
            raise SystemExit("semantic-command request collision")
        return identifier
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(request, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise SystemExit("semantic-command request mode drifted")
    return identifier


def main() -> int:
    payload = json.load(sys.stdin)
    command = payload.get("tool_input", {}).get("command")
    if not isinstance(command, str) or not should_wrap(payload, command):
        return 0
    runner = runner_path()
    if runner is None:
        return 0
    identifier = store(payload, command)
    quoted = shlex.quote(runner)
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": {
                "command": (
                    f"if {quoted} probe {identifier} --cwd \"$PWD\"; "
                    f"then {quoted} run {identifier} --cwd \"$PWD\"; "
                    f"else {quoted} fallback {identifier} --cwd \"$PWD\"; fi"
                )
            },
        }
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
