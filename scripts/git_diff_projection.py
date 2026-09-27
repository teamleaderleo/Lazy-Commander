"""Faithful bounded projection for standard ``git diff`` unified patches."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path


HUNK_HEADER = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:.*)$"
)
FILE_METADATA = (
    "index ",
    "old mode ",
    "new mode ",
    "deleted file mode ",
    "new file mode ",
    "similarity index ",
    "dissimilarity index ",
    "rename from ",
    "rename to ",
    "copy from ",
    "copy to ",
    "--- ",
    "+++ ",
    "Binary files ",
)


@dataclass(frozen=True)
class DiffUnit:
    header: tuple[str, ...]
    header_lines: tuple[int, ...]
    body: tuple[str, ...]
    body_lines: tuple[int, ...]
    kind: str

    @property
    def rows(self) -> tuple[str, ...]:
        return self.header + self.body

    @property
    def source_lines(self) -> tuple[int, ...]:
        return self.header_lines + self.body_lines


def _is_git_diff(command: str) -> bool:
    from semantic_command_hook import command_argvs

    commands = command_argvs(command)
    if not commands or len(commands) != 1:
        return False
    argv = commands[0]
    if Path(argv[0]).name.lower() != "git":
        return False
    index = 1
    while index < len(argv):
        argument = argv[index]
        if argument in {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}:
            if index + 1 >= len(argv):
                return False
            index += 2
        elif argument in {"-p", "--paginate", "-P", "--no-pager"}:
            index += 1
        elif argument.startswith(
            ("-C", "--git-dir=", "--work-tree=", "--namespace=", "--config-env=")
        ):
            index += 1
        else:
            break
    return argv[index:index + 1] == ("diff",)


def _valid_file_header(header: tuple[str, ...], has_hunks: bool) -> bool:
    if not header or not header[0].startswith("diff --git "):
        return False
    try:
        identity = shlex.split(header[0])
    except ValueError:
        return False
    if len(identity) != 4 or identity[:2] != ["diff", "--git"]:
        return False
    metadata = header[1:]
    if any(row == "GIT binary patch" for row in metadata):
        return False
    if not metadata or any(not row.startswith(FILE_METADATA) for row in metadata):
        return False
    counts = {
        prefix: sum(row.startswith(prefix) for row in metadata)
        for prefix in (
            "old mode ",
            "new mode ",
            "deleted file mode ",
            "new file mode ",
            "rename from ",
            "rename to ",
            "copy from ",
            "copy to ",
            "Binary files ",
        )
    }
    if any(count > 1 for count in counts.values()):
        return False
    if counts["rename from "] and counts["copy from "]:
        return False
    if counts["deleted file mode "] and counts["new file mode "]:
        return False
    if (counts["deleted file mode "] or counts["new file mode "]) and counts["old mode "]:
        return False
    if counts["old mode "] != counts["new mode "]:
        return False
    if counts["rename from "] != counts["rename to "]:
        return False
    if counts["copy from "] != counts["copy to "]:
        return False
    if has_hunks:
        old_file = [index for index, row in enumerate(metadata) if row.startswith("--- ")]
        new_file = [index for index, row in enumerate(metadata) if row.startswith("+++ ")]
        return (
            not counts["Binary files "]
            and len(old_file) == len(new_file) == 1
            and old_file[0] < new_file[0]
        )
    return bool(
        counts["Binary files "]
        or counts["old mode "]
        or counts["deleted file mode "]
        or counts["new file mode "]
        or counts["rename from "]
        or counts["copy from "]
    )


def _valid_hunk(rows: tuple[str, ...]) -> bool:
    match = HUNK_HEADER.fullmatch(rows[0]) if rows else None
    if match is None:
        return False
    old_expected = int(match.group(2) or 1)
    new_expected = int(match.group(4) or 1)
    old_seen = 0
    new_seen = 0
    previous_was_content = False
    for row in rows[1:]:
        if row == r"\ No newline at end of file":
            if not previous_was_content:
                return False
            previous_was_content = False
            continue
        previous_was_content = True
        if row.startswith(" "):
            old_seen += 1
            new_seen += 1
        elif row.startswith("-"):
            old_seen += 1
        elif row.startswith("+"):
            new_seen += 1
        else:
            return False
    return old_seen == old_expected and new_seen == new_expected


def parse_unified_diff(rows: list[str]) -> list[DiffUnit] | None:
    if not rows:
        return []
    starts = [index for index, row in enumerate(rows) if row.startswith("diff --git ")]
    if not starts or starts[0] != 0:
        return None
    starts.append(len(rows))
    units = []
    for section_index in range(len(starts) - 1):
        start, end = starts[section_index:section_index + 2]
        section = rows[start:end]
        hunk_offsets = [index for index, row in enumerate(section) if row.startswith("@@ ")]
        first_hunk = hunk_offsets[0] if hunk_offsets else len(section)
        header = tuple(section[:first_hunk])
        header_lines = tuple(range(start, start + first_hunk))
        if not _valid_file_header(header, bool(hunk_offsets)):
            return None
        if not hunk_offsets:
            units.append(DiffUnit(header, header_lines, (), (), "metadata"))
            continue
        hunk_offsets.append(len(section))
        for hunk_index in range(len(hunk_offsets) - 1):
            hunk_start, hunk_end = hunk_offsets[hunk_index:hunk_index + 2]
            hunk = tuple(section[hunk_start:hunk_end])
            if not _valid_hunk(hunk):
                return None
            units.append(
                DiffUnit(
                    header,
                    header_lines,
                    hunk,
                    tuple(range(start + hunk_start, start + hunk_end)),
                    "hunk",
                )
            )
    return units


def _skip_notice(units: list[DiffUnit], start: int) -> str:
    hunks = sum(unit.kind == "hunk" for unit in units[start:])
    metadata = len(units) - start - hunks
    parts = []
    if hunks:
        parts.append(f"{hunks} {'hunk' if hunks == 1 else 'hunks'}")
    if metadata:
        parts.append(
            f"{metadata} file metadata {'block' if metadata == 1 else 'blocks'}"
        )
    return f"[… {' and '.join(parts)} skipped; use stored expansion for the complete patch …]"


def _join(chunks: list[str]) -> str:
    return "\n".join(chunk for chunk in chunks if chunk)


def _partial_hunk(
    unit: DiffUnit,
    prefix: list[str],
    tail_notice: str,
    limit: int,
) -> tuple[str, set[int]] | None:
    base_rows = [*unit.header, unit.body[0]]
    base_lines = {*unit.header_lines, unit.body_lines[0]}
    body = unit.body[1:]
    shown = []
    for row in body:
        remaining = len(body) - len(shown) - 1
        partial_notice = (
            f"[… partial hunk at the @@ range above; {remaining} "
            f"{'body line' if remaining == 1 else 'body lines'} omitted …]"
        )
        candidate = _join([*prefix, *base_rows, *shown, row, partial_notice, tail_notice])
        if len(candidate) > limit:
            break
        shown.append(row)
    omitted = len(body) - len(shown)
    if omitted == 0:
        return None
    notice = (
        f"[… partial hunk at the @@ range above; {omitted} "
        f"{'body line' if omitted == 1 else 'body lines'} omitted …]"
    )
    rendered = _join([*base_rows, *shown, notice])
    if len(_join([*prefix, rendered, tail_notice])) > limit:
        return None
    shown_lines = base_lines | set(unit.body_lines[1:1 + len(shown)])
    return rendered, shown_lines


def project_git_diff(
    command: str,
    rows: list[str],
    exit_code: int,
    limit: int,
) -> tuple[str, dict[str, int]] | None:
    """Return a faithful patch view, or ``None`` for ambiguous/unsupported output."""
    if exit_code != 0 or not _is_git_diff(command):
        return None
    units = parse_unified_diff(rows)
    if units is None:
        return None
    complete = "\n".join(rows)
    if len(complete) <= limit:
        return complete, {}

    rendered = []
    selected_lines: set[int] = set()
    selected_units = 0
    partial_hunks = 0
    for index, unit in enumerate(units):
        tail_notice = _skip_notice(units, index + 1) if index + 1 < len(units) else ""
        whole = "\n".join(unit.rows)
        if len(_join([*rendered, whole, tail_notice])) <= limit:
            rendered.append(whole)
            selected_lines.update(unit.source_lines)
            selected_units = index + 1
            continue
        if unit.kind == "hunk":
            partial = _partial_hunk(unit, rendered, tail_notice, limit)
            if partial is not None:
                text, source_lines = partial
                rendered.append(text)
                selected_lines.update(source_lines)
                selected_units = index + 1
                partial_hunks = 1
        break

    if selected_units < len(units):
        rendered.append(_skip_notice(units, selected_units))
    payload = _join(rendered)
    if len(payload) > limit:
        raise AssertionError("git diff projection exceeded its character limit")
    skipped_hunks = sum(unit.kind == "hunk" for unit in units[selected_units:])
    omitted = {"lines": len(rows) - len(selected_lines)}
    if skipped_hunks or partial_hunks:
        omitted["hunks"] = skipped_hunks + partial_hunks
    return payload, omitted
