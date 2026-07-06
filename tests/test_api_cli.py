import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import radi_ct_api


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


class RadiCtApiCliTest(unittest.TestCase):
    def test_health_uses_configured_api_url(self):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            return FakeResponse({"status": "ok", "version": "test"})

        with patch("urllib.request.urlopen", fake_urlopen), patch("sys.stdout", new=io.StringIO()) as stdout:
            radi_ct_api.main(["--api-url", "http://127.0.0.1:9999", "health"])

        self.assertEqual(captured["url"], "http://127.0.0.1:9999/api/health")
        self.assertEqual(captured["timeout"], 120)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "ok")

    def test_draft_sends_input_and_assistant_draft_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "input.md"
            draft_path = Path(tmp) / "draft.md"
            input_path.write_text("Описание: синтетическое описание.", encoding="utf-8")
            draft_path.write_text("Синтетическое заключение.", encoding="utf-8")
            captured = {}

            def fake_urlopen(request, timeout):
                captured["url"] = request.full_url
                captured["method"] = request.get_method()
                captured["payload"] = json.loads(request.data.decode("utf-8"))
                return FakeResponse({"case_id": "case-1", "draft": "Синтетическое заключение."})

            with patch("urllib.request.urlopen", fake_urlopen), patch("sys.stdout", new=io.StringIO()) as stdout:
                radi_ct_api.main(
                    [
                        "--api-url",
                        "http://api.test",
                        "draft",
                        str(input_path),
                        "--assistant-draft",
                        str(draft_path),
                        "--area",
                        "ОГК",
                        "--clinical-context",
                        "синтетический пример",
                        "--comparison",
                    ]
                )

            self.assertEqual(captured["url"], "http://api.test/api/draft")
            self.assertEqual(captured["method"], "POST")
            self.assertEqual(captured["payload"]["input_text"], "Описание: синтетическое описание.")
            self.assertEqual(captured["payload"]["assistant_draft"], "Синтетическое заключение.")
            self.assertEqual(captured["payload"]["area"], ["ОГК"])
            self.assertTrue(captured["payload"]["comparison"])
            self.assertEqual(json.loads(stdout.getvalue())["case_id"], "case-1")

    def test_cases_status_query(self):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            return FakeResponse([])

        with patch("urllib.request.urlopen", fake_urlopen), patch("sys.stdout", new=io.StringIO()):
            radi_ct_api.main(["--api-url", "http://api.test", "cases", "--status", "corrected"])

        self.assertEqual(captured["url"], "http://api.test/api/cases?status=corrected")


if __name__ == "__main__":
    unittest.main()
