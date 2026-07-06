import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "radi_ct.py"


class RadiCtCliTest(unittest.TestCase):
    def test_help_smoke(self):
        result = subprocess.run(
            [sys.executable, str(CLI), "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertIn("RadiCT Assistant learning-loop CLI", result.stdout)

    def test_draft_from_synthetic_file_smoke(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fixture = tmp_path / "synthetic_input.md"
            fixture.write_text(
                "Описание:\nСинтетический очаг S8 правого легкого уменьшился.\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["RADI_CT_BASE_DIR"] = str(tmp_path)
            result = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "draft",
                    str(fixture),
                    "--area",
                    "ОГК",
                    "--clinical-context",
                    "синтетический пример",
                    "--comparison",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )
            data = json.loads(result.stdout)
            self.assertEqual(data["status"], "draft")
            self.assertTrue(Path(data["path"]).exists())


if __name__ == "__main__":
    unittest.main()
