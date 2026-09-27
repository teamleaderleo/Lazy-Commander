from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "context_view.py"


def run_view(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(path), *args],
        text=True,
        capture_output=True,
        check=False,
    )


def test_toc_hides_body_and_ignores_fenced_headings(tmp_path: Path) -> None:
    doc = tmp_path / "long.md"
    doc.write_text("# One\nsecret\n```\n## Fake\n```\n## Two\nbody\n", encoding="utf-8")

    result = run_view(doc)

    assert result.returncode == 0
    view = json.loads(result.stdout)
    assert [item["title"] for item in view["headings"]] == ["One", "Two"]
    assert view["totalHeadings"] == 2
    assert view["omittedHeadings"] == 0
    assert view["truncated"] is False
    assert view["rawDocumentEmitted"] is False
    assert "secret" not in result.stdout


def test_toc_caps_heading_count_and_reports_omissions(tmp_path: Path) -> None:
    doc = tmp_path / "generated.md"
    doc.write_text("".join(f"## Heading {number}\nbody\n" for number in range(250)), encoding="utf-8")

    result = run_view(doc, "--max-headings", "25")

    assert result.returncode == 0
    view = json.loads(result.stdout)
    assert len(view["headings"]) == 25
    assert view["totalHeadings"] == 250
    assert view["omittedHeadings"] == 225
    assert view["truncated"] is True
    assert [item["title"] for item in view["headings"]] == [
        f"Heading {number}" for number in range(25)
    ]
    assert "body" not in result.stdout


def test_exact_section_stops_at_peer_and_reports_truncation(tmp_path: Path) -> None:
    doc = tmp_path / "long.md"
    doc.write_text("# One\n## Wanted\n" + "x" * 400 + "\n## Next\nnope\n", encoding="utf-8")

    result = run_view(doc, "--section", "wanted", "--max-chars", "256")

    assert result.returncode == 0
    view = json.loads(result.stdout)
    assert view["mode"] == "section"
    assert view["truncated"] is True
    assert view["omittedChars"] > 0
    assert "nope" not in view["content"]


def test_ambiguous_section_refuses(tmp_path: Path) -> None:
    doc = tmp_path / "duplicate.md"
    doc.write_text("# Same\na\n# Same\nb\n", encoding="utf-8")

    result = run_view(doc, "--section", "Same")

    assert result.returncode != 0
    assert "found 2" in result.stderr


def test_heading_limit_has_closed_range(tmp_path: Path) -> None:
    doc = tmp_path / "one.md"
    doc.write_text("# One\n", encoding="utf-8")

    assert run_view(doc, "--max-headings", "0").returncode != 0
    assert run_view(doc, "--max-headings", "201").returncode != 0
