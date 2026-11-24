import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).parents[1] / "scripts"
SPEC = importlib.util.spec_from_file_location("run_email_agent", SCRIPTS / "run_email_agent.py")
MODULE = importlib.util.module_from_spec(SPEC)
with patch.object(sys, "path", [str(SCRIPTS), *sys.path]):
    SPEC.loader.exec_module(MODULE)

EMAIL = {"message_id": "abc123", "thread_id": "def456", "from": "sender@example.com",
         "subject": "Meeting", "date": None, "body": "Private email body"}
DRAFT = {"source_message_id": "abc123", "thread_id": "def456", "to": "sender@example.com",
         "subject": "Re: Meeting", "draft_body": "What time suits you?"}
SOURCE = {"message": {"id": "abc123", "threadId": "def456", "payload": {"headers": [
    {"name": "From", "value": "sender@example.com"},
    {"name": "To", "value": "operator@example.com"},
    {"name": "Subject", "value": "Meeting"},
    {"name": "Message-ID", "value": "<original@example.com>"},
]}}}
ENV = {"GOG_ACCOUNT": "test-alias", "TARGET_SENDER": "sender@example.com"}
HELP = "--agent <id> --local --json --message-file <path> --session-id <id> --timeout <seconds>"


def completed(value, code=0):
    return subprocess.CompletedProcess([], code, value, "PRIVATE-SECRET-DIAGNOSTIC")


@unittest.skipUnless(os.name == "posix", "Runtime targets Linux/WSL and flock/fsync")
class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "email-agent"
        self.directory.mkdir()
        self.env_patch = patch.dict(os.environ, ENV, clear=True)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def state(self):
        return MODULE.load_state(self.directory, ENV["GOG_ACCOUNT"])

    def seed(self, status):
        data = self.state()
        data["messages"]["abc123"] = status
        MODULE.save_state(self.directory, data)

    def run_pipeline(self, *, auto=True, email=EMAIL, draft=DRAFT, commands=None):
        if commands is None:
            commands = [json.dumps(SOURCE), "{}"]
        with patch.object(MODULE, "read_latest", return_value=email) as reader, \
                patch.object(MODULE, "draft_reply", return_value=draft) as model, \
                patch.object(MODULE.sender_api, "run_command", side_effect=commands) as gog:
            result = MODULE.pipeline(ENV["GOG_ACCOUNT"], ENV["TARGET_SENDER"], auto, self.directory)
        return result, reader, model, gog

    def test_new_message_continues_and_success_persists_processed(self):
        result, _, model, gog = self.run_pipeline()
        self.assertEqual(result, "send_command_succeeded_processed")
        model.assert_called_once()
        self.assertEqual(gog.call_count, 2)
        self.assertTrue(gog.call_args.kwargs["sending"])
        self.assertEqual(self.state()["messages"], {"abc123": "processed"})
        self.assertEqual((self.directory / "state.json").stat().st_mode & 0o777, 0o600)
        raw = (self.directory / "state.json").read_text()
        for private in (EMAIL["body"], DRAFT["draft_body"], ENV["GOG_ACCOUNT"], DRAFT["subject"]):
            self.assertNotIn(private, raw)

    def test_processed_skips_model_and_send_even_if_body_missing(self):
        self.seed("processed")
        result, _, model, gog = self.run_pipeline(email=EMAIL | {"body": None})
        self.assertEqual(result, "processed_skip")
        model.assert_not_called()
        gog.assert_not_called()

    def test_no_email_skips_model_and_send(self):
        result, _, model, gog = self.run_pipeline(email=None)
        self.assertEqual(result, "no_email_skip")
        model.assert_not_called()
        gog.assert_not_called()

    def test_dry_run_validates_without_sending_or_marking_processed(self):
        result, _, model, gog = self.run_pipeline(auto=False)
        self.assertEqual(result, "dry_run_validated_no_send")
        model.assert_called_once()
        gog.assert_not_called()
        self.assertEqual(self.state()["messages"], {})

    def test_only_exact_true_enables_sending(self):
        for flag in (None, "", "1", "TRUE", "false", "true"):
            with self.subTest(flag=flag), patch.dict(os.environ, {}):
                if flag is not None:
                    os.environ["EMAIL_AGENT_AUTO_SEND"] = flag
                with patch.object(MODULE, "pipeline", return_value="test") as pipeline, \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(MODULE.main(), 0)
                self.assertEqual(pipeline.call_args.args[2], flag == "true")

    def test_uncertain_is_durable_before_dispatch_and_processed_only_after_success(self):
        def command(argv, body=None, sending=False):
            if not sending:
                self.assertEqual(self.state()["messages"], {})
                return json.dumps(SOURCE)
            self.assertEqual(self.state()["messages"], {"abc123": "uncertain"})
            return "{}"
        self.run_pipeline(commands=command)
        self.assertEqual(self.state()["messages"], {"abc123": "processed"})

    def test_failed_send_timeout_or_crash_never_retries(self):
        for outcome in (subprocess.TimeoutExpired("gog PRIVATE-SECRET", 60),
                        MODULE.PipelineError("PRIVATE-SECRET", 7), KeyboardInterrupt()):
            with self.subTest(outcome=type(outcome).__name__):
                path = self.directory / "state.json"
                if path.exists():
                    path.unlink()
                with self.assertRaises((MODULE.PipelineError, KeyboardInterrupt)):
                    self.run_pipeline(commands=[json.dumps(SOURCE), outcome])
                self.assertEqual(self.state()["messages"], {"abc123": "uncertain"})
                with patch.object(MODULE, "read_latest") as reader, \
                        patch.object(MODULE, "draft_reply") as model, \
                        patch.object(MODULE.sender_api, "run_command") as gog, \
                        self.assertRaises(MODULE.PipelineError) as error:
                    MODULE.pipeline(ENV["GOG_ACCOUNT"], ENV["TARGET_SENDER"], True, self.directory)
                self.assertEqual(error.exception.exit_code, 7)
                reader.assert_not_called()
                model.assert_not_called()
                gog.assert_not_called()

    def test_failed_intent_write_prevents_send(self):
        with patch.object(MODULE, "save_state", side_effect=OSError("private")), \
                patch.object(MODULE.sender_api, "run_command", return_value=json.dumps(SOURCE)) as gog, \
                patch.object(MODULE, "read_latest", return_value=EMAIL), \
                patch.object(MODULE, "draft_reply", return_value=DRAFT), self.assertRaises(OSError):
            MODULE.pipeline(ENV["GOG_ACCOUNT"], ENV["TARGET_SENDER"], True, self.directory)
        self.assertEqual(gog.call_count, 1)

    def test_failed_processed_write_keeps_uncertain_fence(self):
        real_save = MODULE.save_state
        def save(directory, state):
            if "processed" in state["messages"].values():
                raise OSError("disk full")
            real_save(directory, state)
        with patch.object(MODULE, "save_state", side_effect=save), self.assertRaises(MODULE.PipelineError):
            self.run_pipeline()
        self.assertEqual(self.state()["messages"], {"abc123": "uncertain"})

    def test_malformed_state_fails_before_reader_or_model(self):
        valid = self.state()
        invalid = ["", "PRIVATE-SECRET", "[]", "{}", '{"version":1,"version":1}',
                   json.dumps(valid | {"version": True}),
                   json.dumps(valid | {"messages": {"abc123": "sending"}}),
                   json.dumps(valid | {"messages": {"not-id": "processed"}}),
                   json.dumps(valid | {"account_sha256": "different-account"})]
        for raw in invalid:
            with self.subTest(raw=raw):
                (self.directory / "state.json").write_text(raw)
                with patch.object(MODULE, "read_latest") as reader, self.assertRaises(MODULE.PipelineError) as error:
                    MODULE.pipeline(ENV["GOG_ACCOUNT"], ENV["TARGET_SENDER"], True, self.directory)
                self.assertEqual(error.exception.exit_code, 8)
                reader.assert_not_called()
                self.assertEqual((self.directory / "state.json").read_text(), raw)

    def test_two_real_processes_cannot_hold_lock_and_crash_releases_it(self):
        script = (
            "import sys; from pathlib import Path; "
            "sys.path.insert(0, sys.argv[1]); import run_email_agent as m; "
            "lock=m.pipeline_lock(Path(sys.argv[2])); lock.__enter__(); "
            "print('ready', flush=True); sys.stdin.read()"
        )
        process = subprocess.Popen([sys.executable, "-c", script, str(SCRIPTS), str(self.directory)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), "ready")
            with patch.object(MODULE, "read_latest") as reader, self.assertRaises(MODULE.PipelineError) as error:
                MODULE.pipeline(ENV["GOG_ACCOUNT"], ENV["TARGET_SENDER"], True, self.directory)
            self.assertEqual(str(error.exception), "locked_skip")
            reader.assert_not_called()
        finally:
            process.kill()
            process.communicate(timeout=5)
        with MODULE.pipeline_lock(self.directory):
            pass

    def test_model_cannot_change_source_recipient_subject_or_thread(self):
        for key, value in (("source_message_id", "111aaa"), ("thread_id", None),
                           ("to", "other@example.com"), ("subject", "Re: Other"), ("draft_body", "")):
            with self.subTest(key=key), self.assertRaises(MODULE.PipelineError):
                self.run_pipeline(draft=DRAFT | {key: value}, commands=[])
        self.assertEqual(self.state()["messages"], {})

    def test_source_validation_failure_does_not_record_or_send(self):
        with self.assertRaises(MODULE.PipelineError):
            self.run_pipeline(commands=["{}"])
        self.assertEqual(self.state()["messages"], {})

    def test_secret_and_body_never_reach_logs_on_child_error(self):
        markers = [EMAIL["body"], "PRIVATE-SECRET-DIAGNOSTIC", "password-example", "oauth-example", "model-example"]
        with patch.dict(os.environ, {"GOG_KEYRING_PASSWORD": markers[2], "OAUTH_TOKEN": markers[3],
                                     "MODEL_SECRET": markers[4]}), \
                patch.object(MODULE, "state_directory", return_value=self.directory), \
                patch.object(MODULE.subprocess, "run", return_value=completed(" ".join(markers), 1)), \
                contextlib.redirect_stdout(io.StringIO()) as output, \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(MODULE.main(), 4)
        for marker in markers:
            self.assertNotIn(marker, output.getvalue() + errors.getvalue())

    def test_state_default_xdg_and_repo_rejection(self):
        with patch.object(Path, "home", return_value=Path(self.temp.name)):
            self.assertEqual(MODULE.state_directory(), Path(self.temp.name) / ".local/state/email-agent")
        with patch.dict(os.environ, {"XDG_STATE_HOME": self.temp.name}):
            self.assertEqual(MODULE.state_directory(), self.directory)
        for bad in ("relative", str(MODULE.REPO)):
            with patch.dict(os.environ, {"XDG_STATE_HOME": bad}), self.assertRaises(MODULE.PipelineError):
                MODULE.state_directory()

    def test_reader_no_match_contract_and_invalid_output(self):
        with patch.object(MODULE, "captured", return_value=completed(json.dumps(MODULE.empty_result()), 3)):
            self.assertIsNone(MODULE.read_latest("test-alias", "sender@example.com"))
        with patch.object(MODULE, "captured", return_value=completed(json.dumps(EMAIL), 3)), \
                self.assertRaises(MODULE.PipelineError):
            MODULE.read_latest("test-alias", "sender@example.com")

    def test_actual_sender_adapter_timeout_is_uncertain_without_retry(self):
        with patch.object(MODULE, "read_latest", return_value=EMAIL), \
                patch.object(MODULE, "draft_reply", return_value=DRAFT), \
                patch.object(MODULE.sender_api.subprocess, "run", side_effect=[
                    completed(json.dumps(SOURCE)), subprocess.TimeoutExpired("gog", 60)]) as run, \
                self.assertRaises(MODULE.PipelineError):
            MODULE.pipeline(ENV["GOG_ACCOUNT"], ENV["TARGET_SENDER"], True, self.directory)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(self.state()["messages"], {"abc123": "uncertain"})

    def test_openclaw_help_precedes_model_and_private_prompt_is_not_argv(self):
        context = MODULE.prepare_context(EMAIL, ENV["TARGET_SENDER"])
        def run(command, **kwargs):
            if command[-1] == "--help":
                return completed(HELP)
            self.assertNotIn("--deliver", command)
            self.assertIn("--local", command)
            self.assertEqual(command[command.index("--agent") + 1], "email-agent")
            self.assertNotIn(EMAIL["body"], " ".join(command))
            with open(command[-1], encoding="utf-8") as stream:
                self.assertIn(EMAIL["body"], stream.read())
            # A launcher child with no inherited descriptors can still open
            # the parent's anonymous prompt file after a runtime respawn.
            child = subprocess.run([
                sys.executable, "-c", "import sys; print(len(open(sys.argv[1]).read()))", command[-1],
            ], capture_output=True, text=True, check=True)
            self.assertGreater(int(child.stdout), len(EMAIL["body"]))
            return completed(json.dumps({"payloads": [{"text": json.dumps(DRAFT)}]}))
        with patch.object(MODULE, "model_environment", return_value={}), \
                patch.object(MODULE, "captured", side_effect=run) as child:
            self.assertEqual(MODULE.draft_reply(context), DRAFT)
        self.assertEqual(child.call_count, 2)
        self.assertEqual(child.call_args_list[0].args[0], ["openclaw", "agent", "--help"])

    def test_unsupported_cli_or_malformed_draft_never_falls_back(self):
        context = MODULE.prepare_context(EMAIL, ENV["TARGET_SENDER"])
        cases = [[completed("--agent --message")], [completed(HELP), completed("bad JSON")],
                 [completed(HELP), completed(json.dumps({"payloads": []}))],
                 [completed(HELP), completed(json.dumps({"payloads": [{"text": "```json {} ```"}]}))],
                 [completed(HELP), completed(json.dumps({"status": "error", "result": {"payloads": []}}))]]
        for responses in cases:
            with self.subTest(count=len(responses)), patch.object(MODULE, "model_environment", return_value={}), \
                    patch.object(MODULE, "captured", side_effect=responses) as child, \
                    self.assertRaises((MODULE.PipelineError, ValueError)):
                MODULE.draft_reply(context)
            self.assertEqual(child.call_count, len(responses))

    def test_model_requires_tools_disabled_and_silent_logs(self):
        config = Path(self.temp.name) / "openclaw.json"
        safe = {"tools": {"deny": ["*"]}, "logging": {"level": "silent", "consoleLevel": "silent"}}
        with patch.dict(os.environ, {"OPENCLAW_CONFIG_PATH": str(config),
                                     "OPENCLAW_STATE_DIR": str(Path(self.temp.name) / "openclaw"),
                                     "GOG_KEYRING_PASSWORD": "private-value"}):
            for value in ({}, safe | {"tools": {"deny": ["exec"]}}, safe | {"logging": {"level": "debug"}}):
                config.write_text(json.dumps(value))
                with self.assertRaises(MODULE.PipelineError):
                    MODULE.model_environment()
            config.write_text(json.dumps(safe))
            env = MODULE.model_environment()
            self.assertNotIn("GOG_KEYRING_PASSWORD", env)
            self.assertNotIn("GOG_ACCOUNT", env)


    def test_reviewed_draft_overrides_model_and_unlinks_on_success(self):
        reviewed_file = self.directory / "reviewed_draft.json"
        reviewed_file.write_text(json.dumps(DRAFT | {"draft_body": "Custom reviewed body"}))
        result, _, model, gog = self.run_pipeline()
        self.assertEqual(result, "send_command_succeeded_processed")
        model.assert_not_called()
        self.assertEqual(gog.call_count, 2)
        self.assertEqual(gog.call_args_list[1].args[1], "Custom reviewed body")
        self.assertFalse(reviewed_file.exists())


if __name__ == "__main__":
    unittest.main()
