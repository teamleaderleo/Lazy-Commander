#!/usr/bin/env python3
"""Validate and execute exact repository-owned observation profiles."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any


SCHEMA = "lazy-owner-observation-profiles/v1"
RECEIPT_SCHEMA = "lazy-owner-observation-receipt/v1"
PROFILE_FILENAME = Path(".lazy/observation-profiles.json")
MAX_PROFILE_BYTES = 1_000_000
MAX_CHILD_OUTPUT_BYTES = 64_000
MAX_RESULT_BYTES = 1_000_000
SAFE_ID = re.compile(r"^[a-z][a-z0-9-]{0,79}$")
SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/@+-]{0,239}$")
GIT_OID = re.compile(r"^[0-9a-f]{40,64}$")


class ProfileError(RuntimeError):
    pass


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProfileError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def read_regular(path: Path, maximum: int, context: str) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
            raise ProfileError(f"{context} must be one regular file no larger than {maximum} bytes")
        payload = os.read(descriptor, maximum + 1)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if len(payload) > maximum:
        raise ProfileError(f"{context} exceeds its byte bound")
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise ProfileError(f"{context} changed while it was read")
    return payload


def parse_json(payload: bytes, context: str) -> Any:
    try:
        return json.loads(payload.decode("utf-8"), object_pairs_hook=unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProfileError(f"{context} must be UTF-8 JSON") from error


def require_keys(value: dict[str, Any], allowed: set[str], context: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ProfileError(f"{context} has unknown keys: {', '.join(sorted(unknown))}")


def repository_slug(root: Path) -> str:
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or result.stderr:
        raise ProfileError("repository origin cannot be verified")
    origin = result.stdout.strip().removesuffix(".git")
    match = re.search(r"(?:github\.com[:/])([^/]+/[^/]+)$", origin)
    if not match:
        raise ProfileError("repository origin is not a bounded GitHub repository")
    return match.group(1)


def load_profiles(path: Path) -> tuple[Path, bytes, dict[str, Any]]:
    resolved = path.expanduser().resolve()
    if resolved.name != PROFILE_FILENAME.name or resolved.parent.name != PROFILE_FILENAME.parent.name:
        raise ProfileError("profile file must be repository-root .lazy/observation-profiles.json")
    root = resolved.parent.parent.resolve()
    top = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if top.returncode != 0 or top.stderr or Path(top.stdout.strip()).resolve() != root:
        raise ProfileError("profile file is not bound to its repository root")
    payload = read_regular(resolved, MAX_PROFILE_BYTES, "profile file")
    raw = parse_json(payload, "profile file")
    if not isinstance(raw, dict):
        raise ProfileError("profile file must be a JSON object")
    require_keys(raw, {"schema", "repository", "profiles"}, "profile file")
    if raw.get("schema") != SCHEMA:
        raise ProfileError(f"profile schema must be {SCHEMA}")
    repository = raw.get("repository")
    if not isinstance(repository, str) or repository_slug(root).lower() != repository.lower():
        raise ProfileError("profile repository does not match the exact Git origin")
    profiles = raw.get("profiles")
    if not isinstance(profiles, list) or not profiles or len(profiles) > 64:
        raise ProfileError("profiles must contain 1 to 64 rows")
    return root, payload, raw


def parse_parameters(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ProfileError("each parameter must be NAME=VALUE")
        name, item = value.split("=", 1)
        if not SAFE_ID.fullmatch(name) or not item or len(item) > 4096:
            raise ProfileError("parameter name or value is invalid")
        if name in result:
            raise ProfileError(f"duplicate parameter {name!r}")
        result[name] = item
    return result


def beneath(value: Path, root: Path) -> bool:
    try:
        value.relative_to(root)
        return True
    except ValueError:
        return False


def validate_parameter(spec: dict[str, Any], value: str, repository_root: Path) -> str:
    kind = spec["kind"]
    if kind == "git-oid":
        if not GIT_OID.fullmatch(value):
            raise ProfileError(f"parameter {spec['name']} must be an immutable Git object ID")
        return value
    if kind == "safe-ref":
        if not SAFE_REF.fullmatch(value):
            raise ProfileError(f"parameter {spec['name']} must be a bounded opaque reference")
        return value
    if kind == "sha256":
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ProfileError(f"parameter {spec['name']} must be a lowercase SHA-256")
        return value
    if kind == "enum":
        choices = spec.get("values")
        if not isinstance(choices, list) or not choices or any(not isinstance(item, str) for item in choices):
            raise ProfileError(f"parameter {spec['name']} has invalid enum values")
        if value not in choices:
            raise ProfileError(f"parameter {spec['name']} is outside its closed enum")
        return value
    if kind == "absolute-file":
        candidate = Path(value).expanduser().resolve()
        roots = spec.get("roots")
        if not isinstance(roots, list) or not roots:
            raise ProfileError(f"parameter {spec['name']} lacks allowed roots")
        allowed: list[Path] = []
        for item in roots:
            if item == "{repository}":
                allowed.append(repository_root)
            elif isinstance(item, str) and Path(item).is_absolute():
                allowed.append(Path(item).resolve())
            else:
                raise ProfileError(f"parameter {spec['name']} has an invalid allowed root")
        if not any(beneath(candidate, root) for root in allowed):
            raise ProfileError(f"parameter {spec['name']} is outside its allowed roots")
        payload = read_regular(candidate, MAX_RESULT_BYTES, f"parameter {spec['name']}")
        if not payload:
            raise ProfileError(f"parameter {spec['name']} is empty")
        return str(candidate)
    raise ProfileError(f"parameter {spec['name']} has unsupported kind {kind!r}")


def select_profile(raw: dict[str, Any], profile_id: str) -> dict[str, Any]:
    matches = [row for row in raw["profiles"] if isinstance(row, dict) and row.get("id") == profile_id]
    if len(matches) != 1:
        raise ProfileError("profile ID is missing or duplicated")
    profile = matches[0]
    require_keys(profile, {"id", "entrypoint", "sourceSha256", "parameters", "output"}, "profile")
    if not SAFE_ID.fullmatch(profile_id):
        raise ProfileError("profile ID is invalid")
    return profile


def compile_invocation(
    *,
    profile_path: Path,
    profile_id: str,
    parameter_values: list[str],
    output_dir: Path,
    expected_profile_sha256: str | None = None,
) -> dict[str, Any]:
    root, profile_payload, raw = load_profiles(profile_path)
    profile = select_profile(raw, profile_id)
    entrypoint_text = profile.get("entrypoint")
    if not isinstance(entrypoint_text, str):
        raise ProfileError("profile entrypoint must be a relative path")
    relative = Path(entrypoint_text)
    if relative.is_absolute() or ".." in relative.parts or relative.parts[:1] != ("tools",):
        raise ProfileError("profile entrypoint must be a tools-relative path without traversal")
    entrypoint = (root / relative).resolve()
    if not beneath(entrypoint, root):
        raise ProfileError("profile entrypoint escapes the repository root")
    source = read_regular(entrypoint, MAX_RESULT_BYTES, "profile source")
    source_sha = profile.get("sourceSha256")
    if not isinstance(source_sha, str) or source_sha != sha256(source):
        raise ProfileError("profile source hash does not match the checked source")
    specs = profile.get("parameters")
    if not isinstance(specs, list) or len(specs) > 32:
        raise ProfileError("profile parameters must be a bounded list")
    supplied = parse_parameters(parameter_values)
    argv = [sys.executable, str(entrypoint)]
    seen: set[str] = set()
    for index, spec in enumerate(specs):
        if not isinstance(spec, dict):
            raise ProfileError(f"parameter spec {index} must be an object")
        require_keys(spec, {"name", "flag", "kind", "values", "roots"}, f"parameter spec {index}")
        name = spec.get("name")
        flag = spec.get("flag")
        if not isinstance(name, str) or not SAFE_ID.fullmatch(name) or name in seen:
            raise ProfileError(f"parameter spec {index} has an invalid or duplicate name")
        if not isinstance(flag, str) or not re.fullmatch(r"--[a-z][a-z0-9-]{0,79}", flag):
            raise ProfileError(f"parameter spec {index} has an invalid flag")
        if name not in supplied:
            raise ProfileError(f"required parameter {name!r} is missing")
        seen.add(name)
        argv.extend([flag, validate_parameter(spec, supplied[name], root)])
    unknown = set(supplied) - seen
    if unknown:
        raise ProfileError(f"unknown parameters: {', '.join(sorted(unknown))}")
    output = profile.get("output")
    if not isinstance(output, dict):
        raise ProfileError("profile output must be an object")
    require_keys(output, {"flag", "filename"}, "profile output")
    output_flag = output.get("flag")
    filename = output.get("filename")
    if not isinstance(output_flag, str) or not re.fullmatch(r"--[a-z][a-z0-9-]{0,79}", output_flag):
        raise ProfileError("profile output flag is invalid")
    if not isinstance(filename, str) or not re.fullmatch(r"[a-z][a-z0-9._-]{0,119}", filename):
        raise ProfileError("profile output filename is invalid")
    resolved_output = output_dir.expanduser().resolve()
    if resolved_output.exists():
        raise ProfileError("profile output directory already exists")
    if not resolved_output.parent.is_dir():
        raise ProfileError("profile output parent does not exist")
    result_path = resolved_output / filename
    argv.extend([output_flag, str(result_path)])
    profile_sha = sha256(profile_payload)
    if expected_profile_sha256 is not None:
        if not re.fullmatch(r"[0-9a-f]{64}", expected_profile_sha256):
            raise ProfileError("expected profile hash must be a lowercase SHA-256")
        if profile_sha != expected_profile_sha256:
            raise ProfileError("profile file drifted from the enqueued identity")
    invocation = {
        "schema": "lazy-owner-observation-invocation/v1",
        "profileId": profile_id,
        "profileSha256": profile_sha,
        "repository": raw["repository"],
        "repositoryRootSha256": sha256(str(root).encode("utf-8")),
        "sourceSha256": source_sha,
        "commandSha256": sha256(canonical(argv)),
        "cwd": str(root),
        "argv": argv,
        "outputDir": str(resolved_output),
        "resultPath": str(result_path),
        "observationOnly": True,
    }
    return invocation


def execute(invocation: dict[str, Any]) -> dict[str, Any]:
    output_dir = Path(invocation["outputDir"])
    output_dir.mkdir(mode=0o700)
    output_dir.chmod(0o700)
    process = subprocess.run(
        invocation["argv"],
        cwd=invocation["cwd"],
        check=False,
        capture_output=True,
    )
    if len(process.stdout) > MAX_CHILD_OUTPUT_BYTES or len(process.stderr) > MAX_CHILD_OUTPUT_BYTES:
        raise ProfileError("owner observation diagnostics exceeded the hard bound")
    if process.returncode != 0:
        raise ProfileError("owner observation failed")
    if process.stderr:
        raise ProfileError("owner observation emitted an unexpected diagnostic")
    result_path = Path(invocation["resultPath"])
    result = read_regular(result_path, MAX_RESULT_BYTES, "owner observation result")
    if stat.S_IMODE(result_path.stat().st_mode) != 0o600:
        raise ProfileError("owner observation result is not mode 0600")
    return {
        "schema": RECEIPT_SCHEMA,
        "profileId": invocation["profileId"],
        "profileSha256": invocation["profileSha256"],
        "sourceSha256": invocation["sourceSha256"],
        "commandSha256": invocation["commandSha256"],
        "resultSha256": sha256(result),
        "resultBytes": len(result),
        "exitCode": process.returncode,
        "rawContentEmitted": False,
    }


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    commands = value.add_subparsers(dest="command", required=True)
    for name in ("check", "run"):
        command = commands.add_parser(name)
        command.add_argument("--profiles", type=Path, required=True)
        command.add_argument("--profile", required=True)
        command.add_argument("--output-dir", type=Path, required=True)
        command.add_argument("--param", action="append", default=[])
        if name == "run":
            command.add_argument("--expected-profile-sha256", required=True)
    return value


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    args = parser().parse_args(argv)
    try:
        invocation = compile_invocation(
            profile_path=args.profiles,
            profile_id=args.profile,
            parameter_values=args.param,
            output_dir=args.output_dir,
            expected_profile_sha256=(
                args.expected_profile_sha256 if args.command == "run" else None
            ),
        )
        if args.command == "check":
            receipt = {
                "schema": "lazy-owner-observation-check/v1",
                "profileId": invocation["profileId"],
                "profileSha256": invocation["profileSha256"],
                "sourceSha256": invocation["sourceSha256"],
                "commandSha256": invocation["commandSha256"],
                "observationOnly": True,
                "rawContentEmitted": False,
            }
        else:
            receipt = execute(invocation)
    except (OSError, ProfileError) as error:
        raise SystemExit(str(error)) from error
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
