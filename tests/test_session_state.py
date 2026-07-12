"""Tests for session-safe case state and prepare/save-draft workflow."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.session_state import SessionStateStore


class SessionStateStoreTest(unittest.TestCase):
    def test_set_and_get_active_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStateStore(base_dir=Path(tmp))
            store.set_active_case("sess-1", "case-001", state="awaiting_feedback", task="conclusion", rag_status="used")
            entry = store.get_active_case("sess-1")
            self.assertIsNotNone(entry)
            self.assertEqual(entry["case_id"], "case-001")
            self.assertEqual(entry["state"], "awaiting_feedback")
            self.assertEqual(entry["rag_status"], "used")

    def test_no_cross_session_contamination(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStateStore(base_dir=Path(tmp))
            store.set_active_case("sess-1", "case-001")
            store.set_active_case("sess-2", "case-002")
            self.assertEqual(store.get_active_case("sess-1")["case_id"], "case-001")
            self.assertEqual(store.get_active_case("sess-2")["case_id"], "case-002")

    def test_clear_active_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStateStore(base_dir=Path(tmp))
            store.set_active_case("sess-1", "case-001")
            store.clear("sess-1")
            self.assertIsNone(store.get_active_case("sess-1"))

    def test_state_file_contains_no_medical_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStateStore(base_dir=Path(tmp))
            store.set_active_case("sess-1", "case-001")
            content = store.state_file.read_text(encoding="utf-8")
            # Only JSON structural keys, no descriptions/conclusions
            self.assertNotIn("Описание:", content)
            self.assertNotIn("Заключение:", content)
            self.assertNotIn("анамнез", content)


class PrepareAndSaveDraftTest(unittest.TestCase):
    def test_prepare_endpoint_returns_structured_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["RADI_CT_BASE_DIR"] = tmp
            client = TestClient(app)
            with patch("src.ohs.resolve_ohs_command", return_value=None):
                response = client.post(
                    "/api/prepare",
                    json={
                        "input_text": "Синтетическое описание очага S8 правого легкого.",
                        "task": "conclusion",
                        "area": ["ОГК"],
                    },
                )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertIn("rag_status", data)
            self.assertIn("normalized", data)
            self.assertIn("prompt", data)
            self.assertIn(data["rag_status"], {"used", "no_hits", "unavailable", "error"})

    def test_save_draft_creates_case_from_prepared(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["RADI_CT_BASE_DIR"] = tmp
            client = TestClient(app)
            prepared = {
                "input_text": "Синтетическое описание без идентификаторов.",
                "task": "conclusion",
                "area": ["ОГК"],
                "clinical_context": "",
                "comparison": False,
                "references_used": [],
            }
            response = client.post(
                "/api/save-draft",
                json={
                    "prepared": prepared,
                    "assistant_draft": "Синтетическое заключение.",
                    "references_used": [],
                },
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertIn("case_id", data)
            self.assertTrue(data["case_id"])
            self.assertEqual(data["draft"], "Синтетическое заключение.")

    def test_save_draft_requires_assistant_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["RADI_CT_BASE_DIR"] = tmp
            client = TestClient(app)
            response = client.post(
                "/api/save-draft",
                json={
                    "prepared": {"input_text": "test"},
                    "assistant_draft": "",
                },
            )
            self.assertEqual(response.status_code, 400)

    def test_session_state_api_set_and_get(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["RADI_CT_BASE_DIR"] = tmp
            client = TestClient(app)
            # Set
            response = client.post(
                "/api/session/state",
                json={
                    "session_id": "test-sess-1",
                    "case_id": "case-001",
                    "state": "awaiting_feedback",
                    "task": "conclusion",
                    "rag_status": "used",
                },
            )
            self.assertEqual(response.status_code, 200)
            # Get
            response = client.get("/api/session/state/test-sess-1")
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["case_id"], "case-001")

    def test_session_state_api_404_for_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["RADI_CT_BASE_DIR"] = tmp
            client = TestClient(app)
            response = client.get("/api/session/state/nonexistent")
            self.assertEqual(response.status_code, 404)

    def test_prepare_save_draft_accept_e2e(self):
        """End-to-end: prepare → save-draft → accept with structured outcome."""
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["RADI_CT_BASE_DIR"] = tmp
            os.environ["RADI_CT_AUTO_REINDEX"] = "0"
            client = TestClient(app)

            # Step 1: prepare
            with patch("src.ohs.resolve_ohs_command", return_value=None):
                prepare_resp = client.post(
                    "/api/prepare",
                    json={
                        "input_text": "Синтетическое описание без идентификаторов.",
                        "task": "conclusion",
                        "area": ["ОГК"],
                    },
                )
            self.assertEqual(prepare_resp.status_code, 200)
            prepared_data = prepare_resp.json()

            # Step 2: save-draft
            save_resp = client.post(
                "/api/save-draft",
                json={
                    "prepared": {
                        "input_text": "Синтетическое описание без идентификаторов.",
                        "task": "conclusion",
                        "area": ["ОГК"],
                        "clinical_context": "",
                        "comparison": False,
                        "references_used": prepared_data.get("references_used", []),
                    },
                    "assistant_draft": "Синтетическое заключение без идентификаторов.",
                    "references_used": prepared_data.get("references_used", []),
                },
            )
            self.assertEqual(save_resp.status_code, 200)
            case_id = save_resp.json()["case_id"]
            self.assertTrue(case_id)

            # Step 3: accept with save_as_reference
            accept_resp = client.post(
                f"/api/accept/{case_id}",
                json={"save_as_reference": True},
            )
            self.assertEqual(accept_resp.status_code, 200)
            accept_data = accept_resp.json()
            self.assertTrue(accept_data["saved_as_reference"])
            ref = accept_data["reference"]
            self.assertTrue(ref["saved"])
            self.assertEqual(ref["reference_id"], case_id)


# Import app lazily to avoid env issues at import time
from src.main import app  # noqa: E402