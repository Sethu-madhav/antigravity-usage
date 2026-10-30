import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class TestGitignoreHygiene(unittest.TestCase):
    def setUp(self):
        gitignore_path = ROOT / ".gitignore"
        self.assertTrue(gitignore_path.is_file(), ".gitignore must exist")
        self.lines = gitignore_path.read_text(encoding="utf-8").splitlines()

    def test_tool_cache_entries_present(self):
        expected = [
            ".pytest_cache/",
            ".coverage",
            ".mypy_cache/",
            ".ruff_cache/",
        ]
        for entry in expected:
            with self.subTest(entry=entry):
                self.assertIn(entry, self.lines)

    def test_preexisting_entries_preserved(self):
        expected = [
            "__pycache__/",
            ".venv",
            ".DS_Store",
        ]
        for entry in expected:
            with self.subTest(entry=entry):
                self.assertIn(entry, self.lines)


if __name__ == "__main__":
    unittest.main()
