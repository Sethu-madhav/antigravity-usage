try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    tomllib = None
import unittest
from pathlib import Path

import agy_usage

ROOT = Path(__file__).resolve().parent.parent


class TestPackagingMetadata(unittest.TestCase):
    def setUp(self):
        if tomllib is not None:
            text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
            self.config = tomllib.loads(text)
            self.project = self.config["project"]
        else:
            self.config = None
            self.project = None

    def test_setup_py_removed(self):
        self.assertFalse((ROOT / "setup.py").exists())

    def test_license_file_present(self):
        license_path = ROOT / "LICENSE"
        self.assertTrue(license_path.is_file())
        text = license_path.read_text(encoding="utf-8")
        self.assertTrue(text.strip())
        self.assertIn("MIT License", text)
        self.assertIn("Permission is hereby granted, free of charge", text)

    @unittest.skipIf(tomllib is None, "tomllib requires Python 3.11+")
    def test_version_is_dynamic(self):
        self.assertIn("version", self.project["dynamic"])
        self.assertNotIn("version", self.project)
        self.assertEqual(
            self.config["tool"]["setuptools"]["dynamic"]["version"],
            {"attr": "agy_usage.__version__"},
        )

    @unittest.skipIf(tomllib is None, "tomllib requires Python 3.11+")
    def test_license_is_spdx_expression(self):
        self.assertEqual(self.project["license"], "MIT")
        for classifier in self.project.get("classifiers", []):
            self.assertFalse(classifier.startswith("License ::"), classifier)

    @unittest.skipIf(tomllib is None, "tomllib requires Python 3.11+")
    def test_console_script_entry_point(self):
        self.assertEqual(
            self.project["scripts"]["agy-usage"], "agy_usage.cli:main"
        )

    def test_package_version_is_semver(self):
        self.assertTrue(agy_usage.__version__)
        self.assertRegex(agy_usage.__version__, r"^\d+\.\d+\.\d+")


if __name__ == "__main__":
    unittest.main()
