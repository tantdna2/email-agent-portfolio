import contextlib
import copy
import importlib.util
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS = Path(__file__).parents[1] / "scripts"
SPEC = importlib.util.spec_from_file_location("send_gmail_reply", SCRIPTS / "send_gmail_reply.py")
MODULE = importlib.util.module_from_spec(SPEC)
with patch.object(sys, "path", [str(SCRIPTS), *sys.path]):
    SPEC.loader.exec_module(MODULE)


def draft(**changes):
    data = {"source_message_id": "abc123", "thread_id": "def456",
            "to": "sender@example.com", "subject": "Re: Meeting",
            "draft_body": "Thank you. What time would suit you?"}
    return data | changes


def source(**headers):
    values = {"From": "Sender <sender@example.com>", "To": "operator@example.com",
              "Cc": "colleague@example.com", "Subject": "Meeting",
              "Message-ID": "<original@example.com>"} | headers
    return {"message": {"id": "abc123", "threadId": "def456", "labelIds": ["INBOX"],
                        "payload": {"headers": [{"name": k, "value": v} for k, v in values.items()]}}}


def completed(payload, code=0, stderr=""):
    return subprocess.CompletedProcess([], code, json.dumps(payload), stderr)


class SendReplyTests(unittest.TestCase):
    def run_cli(self, data=None, flags=(), responses=(), raw=None, env=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        payload = raw if raw is not None else json.dumps(draft() if data is None else data)
        args = ["--sender=sender@example.com", *flags]
        with patch.dict(MODULE.os.environ, env or {}, clear=True), \
             patch.object(sys, "stdin", io.StringIO(payload)), \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
             patch.object(MODULE.subprocess, "run", side_effect=responses) as run:
            code = MODULE.main(args)
        return code, stdout.getvalue(), stderr.getvalue(), run

    def test_default_dry_run_without_confirmation_never_calls_gog(self):
        code, output, error, run = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        self.assertEqual(json.loads(output), {"status": "dry_run", "sent": False,
                                             "source_verified": False, "reply": draft()})
        run.assert_not_called()

    def test_explicit_dry_run_ignores_environment_send_suggestions(self):
        code, output, _, run = self.run_cli(flags=["--dry-run"], env={"SEND": "true"})
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(output)["sent"])
        run.assert_not_called()

    def test_send_requires_account_and_does_not_select_auto(self):
        for flags in (["--send"], ["--send", "--account=auto"], ["--send", "--account=   "]):
            with self.subTest(flags=flags):
                code, _, _, run = self.run_cli(flags=flags)
                self.assertEqual(code, 2)
                run.assert_not_called()

    def test_local_validation_failures_never_call_subprocess_even_with_send(self):
        invalid = [draft(to="other@example.com"), draft(to="a@example.com,b@example.com"),
                   draft(draft_body=" \n"), draft(draft_body=None), draft(subject=""),
                   draft(subject="Re: Meeting\r\nBcc: other@example.com"),
                   draft(draft_body="text\x00"), draft(source_message_id="--help"),
                   draft(thread_id=123), draft(attachments=["private.txt"]),
                   draft(send=True), [], {"to": "sender@example.com"}]
        for value in invalid:
            with self.subTest(value=value):
                code, output, _, run = self.run_cli(value, ["--send", "--account=test-alias"])
                self.assertEqual(code, 6)
                self.assertEqual(output, "")
                run.assert_not_called()

    def test_invalid_and_duplicate_json_never_calls_subprocess(self):
        for raw in ("private malformed JSON", '{"to":"a@example.com","to":"b@example.com"}'):
            with self.subTest(raw=raw):
                code, output, error, run = self.run_cli(raw=raw, flags=["--send"])
                self.assertEqual(code, 5)
                self.assertEqual(output, "")
                self.assertNotIn(raw, error)
                run.assert_not_called()

    def test_obvious_secrets_and_known_environment_values_are_blocked(self):
        examples = ['password=example-only', '"client_secret": "example-only"',
                    'GOG_KEYRING_PASSWORD: example-only', 'Authorization: Bearer example-only',
                    '-----BEGIN PRIVATE KEY-----', 'AIza' + 'x' * 24,
                    'ya29.example-only', 'sk-proj-' + 'x' * 24, 'known-test-value']
        for value in examples:
            for field in ("subject", "draft_body"):
                with self.subTest(value=value, field=field):
                    code, output, error, run = self.run_cli(
                        draft(**{field: value}), ["--send", "--account=test-alias"],
                        env={"GOG_KEYRING_PASSWORD": "known-test-value"})
                    self.assertEqual(code, 6)
                    self.assertEqual(output, "")
                    self.assertNotIn(value, error)
                    run.assert_not_called()

    def test_successful_send_command_uses_only_checked_source_and_stdin(self):
        code, output, error, run = self.run_cli(
            flags=["--send"], env={"GOG_ACCOUNT": "test-alias"},
            responses=[completed(source()), completed({"message_id": "fed321"})])
        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        self.assertEqual(json.loads(output)["status"], "send_command_succeeded")
        self.assertEqual(run.call_count, 2)
        get_call, send_call = run.call_args_list
        self.assertEqual(get_call.args[0], [
            "gog", "--account=test-alias", "--no-input", "--json", "--results-only",
            "--enable-commands-exact=gmail.get", "--readonly", "--gmail-no-send",
            "gmail", "get", "abc123", "--format=metadata",
            "--headers=From,Reply-To,To,Cc,Bcc,Subject,Message-ID"])
        self.assertEqual(send_call.args[0], [
            "gog", "--account=test-alias", "--no-input", "--json", "--results-only",
            "--enable-commands-exact=gmail.reply", "gmail", "reply", "abc123",
            "--to=sender@example.com", "--subject=Re: Meeting", "--body-file=-",
            "--no-quote", "--auto-from-addressed-alias=false", "--signature=false",
            "--remove=colleague@example.com", "--remove=operator@example.com"])
        self.assertEqual(send_call.kwargs["input"], draft()["draft_body"])
        self.assertNotIn(draft()["draft_body"], send_call.args[0])
        self.assertFalse(send_call.kwargs.get("shell", False))

    def test_source_failures_prevent_send(self):
        sources = [source(From="attacker@example.com"),
                   source(**{"Reply-To": "attacker@example.com"}),
                   source(**{"Reply-To": "sender@example.com, attacker@example.com"}),
                   source(To="bad address"), source(Subject="Changed"),
                   source(**{"Message-ID": ""}), {}, []]
        for key, value in [("id", "123abc"), ("threadId", "123def"),
                           ("labelIds", ["DRAFT"]), ("labelIds", None)]:
            item = source()
            item["message"][key] = value
            sources.append(item)
        duplicate = source()
        duplicate["message"]["payload"]["headers"].append({"name": "From", "value": "attacker@example.com"})
        sources.append(duplicate)
        for item in sources:
            with self.subTest(source=item):
                code, output, _, run = self.run_cli(
                    flags=["--send", "--account=test-alias"], responses=[completed(item)])
                self.assertEqual(code, 6)
                self.assertEqual(output, "")
                self.assertEqual(run.call_count, 1)

    def test_not_found_or_auth_failure_does_not_send_or_echo_stderr(self):
        code, _, error, run = self.run_cli(flags=["--send", "--account=test-alias"],
                                         responses=[completed({}, 1, "private diagnostic")])
        self.assertEqual(code, 4)
        self.assertNotIn("private diagnostic", error)
        self.assertEqual(run.call_count, 1)

    def test_invalid_metadata_json_does_not_send(self):
        code, _, _, run = self.run_cli(flags=["--send", "--account=test-alias"],
                                      responses=[subprocess.CompletedProcess([], 0, "invalid", "")])
        self.assertEqual(code, 5)
        self.assertEqual(run.call_count, 1)

    def test_source_header_lists_remove_self_reply_fallback_recipients(self):
        removals, thread = MODULE.validate_source(
            source(To='"Other, Person" <other@example.com>, sender@example.com',
                   Cc="", Bcc="hidden@example.com", **{"Reply-To": "sender@example.com"}),
            draft(thread_id=None), "sender@example.com")
        self.assertEqual(removals, ["hidden@example.com", "other@example.com"])
        self.assertEqual(thread, "def456")

    def test_null_thread_is_verified_from_source(self):
        code, output, _, _ = self.run_cli(draft(thread_id=None), ["--send", "--account=test-alias"],
                                         [completed(source()), completed({})])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["thread_id"], "def456")

    def test_unicode_and_shell_metacharacters_remain_stdin_data(self):
        body = 'Cảm ơn bạn. $(do-not-run) `do-not-run` "quoted"\nNext line.'
        original = draft(draft_body=body)
        snapshot = copy.deepcopy(original)
        code, _, _, run = self.run_cli(original, ["--send", "--account=test-alias"],
                                      [completed(source()), completed({})])
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.kwargs["input"], body)
        self.assertEqual(original, snapshot)

    def test_send_failure_or_timeout_is_not_retried(self):
        for outcome in (completed({}, 1, "private diagnostic"), subprocess.TimeoutExpired("gog", 60)):
            with self.subTest(outcome=outcome):
                code, output, error, run = self.run_cli(
                    flags=["--send", "--account=test-alias"], responses=[completed(source()), outcome])
                self.assertEqual(code, 7)
                self.assertEqual(output, "")
                self.assertNotIn("private diagnostic", error)
                self.assertEqual(run.call_count, 2)

    def test_send_confirmation_cannot_be_abbreviated_or_conflict(self):
        for flags in (["--sen"], ["--send", "--dry-run"]):
            with self.subTest(flags=flags), self.assertRaises(SystemExit) as error:
                self.run_cli(flags=flags)
            self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
