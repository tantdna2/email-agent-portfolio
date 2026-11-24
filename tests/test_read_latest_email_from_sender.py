import importlib.util
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "read_latest_email_from_sender.py"
SPEC = importlib.util.spec_from_file_location("read_latest_email", SCRIPT_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class ParseGogJsonTests(unittest.TestCase):
    def test_extracts_first_message_from_envelope(self):
        result = MODULE.first_search_result(
            {"messages": [{"id": "message-newest"}, {"id": "message-older"}]}
        )
        self.assertEqual(MODULE.message_id_from(result), "message-newest")

    def test_returns_none_when_search_is_empty(self):
        self.assertIsNone(MODULE.first_search_result([]))
        self.assertIsNone(MODULE.first_search_result({"items": []}))

    def test_normalizes_sanitized_message_to_exact_contract(self):
        parsed = MODULE.normalized_message(
            {
                "id": "message-1",
                "threadId": "thread-1",
                "headers": {
                    "from": "Sender <sender@example.com>",
                    "subject": "Status update",
                    "date": "Mon, 22 Sep 2026 10:00:00 +0700",
                },
                "body": "The sanitized body.",
                "attachments": [{"filename": "ignored.txt"}],
            },
            fallback_message_id="fallback-message",
        )
        self.assertEqual(
            parsed,
            {
                "message_id": "message-1",
                "thread_id": "thread-1",
                "from": "Sender <sender@example.com>",
                "subject": "Status update",
                "date": "Mon, 22 Sep 2026 10:00:00 +0700",
                "body": "The sanitized body.",
            },
        )

    def test_uses_nested_message_and_header_list(self):
        parsed = MODULE.normalized_message(
            {
                "message": {
                    "headers": [
                        {"name": "From", "value": "sender@example.com"},
                        {"name": "Subject", "value": "Hello"},
                        {"name": "Date", "value": "2026-09-22"},
                    ],
                    "body": "Hello world",
                }
            },
            fallback_message_id="message-fallback",
            fallback_thread_id="thread-fallback",
        )
        self.assertEqual(parsed["message_id"], "message-fallback")
        self.assertEqual(parsed["thread_id"], "thread-fallback")
        self.assertEqual(parsed["from"], "sender@example.com")
        self.assertEqual(parsed["body"], "Hello world")

    def test_extracts_first_message_from_threads_envelope(self):
        result = MODULE.first_search_result(
            {"threads": [{"id": "thread-msg-1"}, {"id": "thread-msg-2"}]}
        )
        self.assertEqual(MODULE.message_id_from(result), "thread-msg-1")

    def test_clean_diagnostic_redacts_secrets(self):
        cleaned = MODULE.clean_diagnostic(
            'error: access_token=secret123 client_secret: "shh" AIzaSyD1234567890abcdef12345'
        )
        self.assertNotIn("secret123", cleaned)
        self.assertNotIn("shh", cleaned)
        self.assertNotIn("AIzaSyD1234567890abcdef12345", cleaned)
        self.assertIn("access_token=[REDACTED]", cleaned)
        self.assertIn("[REDACTED_API_KEY]", cleaned)


if __name__ == "__main__":
    unittest.main()
