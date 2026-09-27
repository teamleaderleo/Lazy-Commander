import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "SKILL.md"


class SkillDocumentationTest(unittest.TestCase):
    def test_always_loaded_skill_stays_bounded(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        self.assertLessEqual(len(text.encode("utf-8")), 3_600)
        self.assertLessEqual(len(re.findall(r"\S+", text)), 500)

    def test_route_catalogue_is_not_always_loaded(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        self.assertNotIn("| Need | Use |", text)
        self.assertIn("references/mechanism-routing.md", text)

    def test_hot_instructions_use_shortcut_not_legacy_shell(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn("lc 'COMMAND'", text)
        self.assertNotIn("lazy-command shell --cwd", text)

    def test_local_markdown_links_resolve(self) -> None:
        text = SKILL.read_text(encoding="utf-8")
        targets = re.findall(r"\[[^]]+\]\(([^)]+\.md)\)", text)
        self.assertTrue(targets)
        for target in targets:
            with self.subTest(target=target):
                self.assertTrue((ROOT / target).is_file())


if __name__ == "__main__":
    unittest.main()
