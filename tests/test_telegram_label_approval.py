import contextlib
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import telegram_label_approval as core
import gmail_label_writer as writer
import realtime_email_adapters as adapters
import run_telegram_label_action as cli
from run_email_agent import pipeline_lock, parse_json
from test_telegram_reply_approval import MESSAGE, BODY, SCOPE

LABELS = ["Công việc", "Hóa đơn", "Khách hàng"]
ENV = {"GOG_ACCOUNT": SCOPE[0], "TELEGRAM_APPROVER_USER_ID": SCOPE[1],
       "TELEGRAM_TARGET_CHAT_ID": SCOPE[2], "EMAIL_AGENT_AUTO_SEND": "false",
       "EMAIL_AGENT_LABEL_APPROVAL": "true", "EMAIL_AGENT_LABEL_ALLOWLIST_JSON": json.dumps(LABELS)}


@unittest.skipUnless(os.name == "posix", "Linux fsync/flock boundary")
class InteractionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name) / "labels"
        self.clock = Mock(return_value=1000)
        self.store = core.Interactions(self.directory, SCOPE, LABELS, self.clock)
        lock = pipeline_lock(self.directory)
        lock.__enter__()
        self.addCleanup(lock.__exit__, None, None, None)
        env = patch.dict(os.environ, ENV, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.gmail = Mock()
        self.gmail.profile.return_value = {"emailAddress": SCOPE[0]}
        self.gmail.get.return_value = copy.deepcopy(MESSAGE)
        self.model = Mock(return_value=0)
        self.writer = Mock()
        self.writer.prepare.return_value = ("Label_fixture", False)
        self.factory = Mock(return_value=self.writer)
        self.token = self.store.offer("abc123")

    def record(self):
        return self.store.load()["records"]["abc123"]

    def event(self, token):
        return {"channel": "telegram", "account": "default", "user": SCOPE[1], "chat": SCOPE[2],
                "authorized": True, "private": True, "data": token}

    def click(self, token):
        return self.store.handle(self.event(token), SCOPE, self.gmail, self.model, self.factory)

    def preview(self, choice=0):
        self.click(self.token)
        return self.click(self.record()["actions"][f"pick{choice}"])

    def confirm(self):
        self.preview()
        return self.click(self.record()["actions"]["confirm"])

    def assert_purged(self):
        for key in ("source", "recommendation", "selection", "preview"):
            self.assertIsNone(self.record()[key])

    def test_notification_no_model_reader_or_writer_and_opaque_id(self):
        self.assertRegex(self.token, r"^emaillabel:[0-9a-f]{32}$")
        self.assertEqual(self.token, self.store.offer("abc123"))
        self.assertEqual(self.gmail.mock_calls, [])
        self.model.assert_not_called()
        self.factory.assert_not_called()

    def test_exactly_three_choices_one_model_call_and_manual_override(self):
        view = self.click(self.token)
        self.assertEqual([x["text"] for x in view["buttons"][0]], LABELS)
        self.assertIn(LABELS[0], view["text"])
        self.click(self.token)
        preview = self.click(self.record()["actions"]["pick2"])
        self.assertIn(LABELS[2], preview["text"])
        self.model.assert_called_once()
        self.gmail.get.assert_called_once_with("abc123")
        self.factory.assert_not_called()

    def test_none_still_permits_manual_selection(self):
        self.model.return_value = None
        view = self.click(self.token)
        self.assertIn("none", view["text"])
        self.click(self.record()["actions"]["pick1"])
        self.assertEqual(self.record()["preview"]["label"], LABELS[1])
        self.factory.assert_not_called()

    def test_all_callback_data_is_opaque(self):
        self.preview()
        for token in self.record()["actions"].values():
            self.assertRegex(token, r"^emaillabel:[0-9a-f]{32}$")
            self.assertLess(len(token.encode()), 64)
            for value in [BODY, "abc123", "def456", "Label_fixture", *LABELS]:
                self.assertNotIn(value, token)

    def test_unauthorized_callback_loads_no_credentials_or_model(self):
        for overrides in ({"channel": "slack"}, {"user": "999"}, {"chat": "-123"},
                          {"account": "other"}, {"authorized": False}, {"private": False}):
            with self.subTest(overrides=overrides), self.assertRaises(Exception):
                self.store.handle(self.event(self.token) | overrides, SCOPE, self.gmail, self.model, self.factory)
        self.assertEqual(self.gmail.mock_calls, [])
        self.model.assert_not_called()
        self.factory.assert_not_called()

    def test_selection_only_previews_and_stale_pick_cannot_change_preview(self):
        view = self.preview(1)
        self.assertIn("chưa gắn nhãn", view["text"])
        self.assertEqual(view, self.click(self.record()["actions"]["pick2"]))
        self.factory.assert_not_called()

    def test_only_confirm_loads_writer_after_durable_purged_fence(self):
        def factory():
            self.assertEqual(self.record()["status"], "label_uncertain")
            self.assert_purged()
            return self.writer
        self.factory.side_effect = factory
        self.confirm()
        self.factory.assert_called_once()
        self.writer.prepare.assert_called_once_with("abc123", "def456", LABELS[0], SCOPE[0])
        self.writer.add.assert_called_once_with("abc123", "def456", "Label_fixture")
        self.assertEqual(self.record()["status"], "labeled")
        self.assert_purged()

    def test_duplicate_confirm_after_restart_does_not_write_twice(self):
        self.confirm()
        token = self.record()["actions"]["confirm"]
        self.store = core.Interactions(self.directory, SCOPE, LABELS, self.clock)
        self.click(token)
        self.store.offer("abc123")
        self.click(self.token)
        self.writer.add.assert_called_once()
        self.factory.assert_called_once()

    def test_preview_survives_restart(self):
        view = self.preview(2)
        self.store = core.Interactions(self.directory, SCOPE, LABELS, self.clock)
        self.assertEqual(self.click(self.token), view)
        self.click(self.record()["actions"]["confirm"])
        self.writer.prepare.assert_called_once_with("abc123", "def456", LABELS[2], SCOPE[0])

    def test_cancel_purges_and_never_writes(self):
        self.preview()
        self.click(self.record()["actions"]["cancel"])
        self.click(self.record()["actions"]["confirm"])
        self.assertEqual(self.record()["status"], "cancelled")
        self.assert_purged()
        self.factory.assert_not_called()

    def test_expired_never_loads_writer(self):
        self.preview()
        self.clock.return_value = 1000 + core.TTL
        self.click(self.record()["actions"]["confirm"])
        self.assertEqual(self.record()["status"], "expired")
        self.factory.assert_not_called()
        self.assert_purged()

    def test_expiry_during_preflight_blocks_post(self):
        def prepare(*args):
            self.clock.return_value = 1000 + core.TTL
            return "Label_fixture", False
        self.writer.prepare.side_effect = prepare
        self.confirm()
        self.assertEqual(self.record()["status"], "expired")
        self.writer.add.assert_not_called()

    def test_crash_during_model_never_calls_model_again(self):
        self.model.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.click(self.token)
        self.store = core.Interactions(self.directory, SCOPE, LABELS, self.clock)
        self.click(self.token)
        self.assertEqual(self.record()["status"], "failed")
        self.model.assert_called_once()
        self.assert_purged()

    def test_fsync_failure_before_model_blocks_model(self):
        with patch.object(core, "save_state", side_effect=OSError()), self.assertRaises(OSError):
            self.click(self.token)
        self.model.assert_not_called()

    def test_fsync_failure_before_write_blocks_even_credentials(self):
        self.preview()
        token = self.record()["actions"]["confirm"]
        with patch.object(core, "save_state", side_effect=OSError()), self.assertRaises(OSError):
            self.click(token)
        self.factory.assert_not_called()
        self.writer.add.assert_not_called()

    def test_crash_before_dispatch_leaves_terminal_fence(self):
        self.factory.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.confirm()
        self.click(self.record()["actions"]["confirm"])
        self.assertEqual(self.record()["status"], "label_uncertain")
        self.factory.assert_called_once()
        self.writer.add.assert_not_called()

    def test_timeout_ambiguous_write_never_retries(self):
        self.writer.add.side_effect = TimeoutError("FAKE OAUTH TOKEN " + BODY)
        self.confirm()
        self.click(self.record()["actions"]["confirm"])
        self.assertEqual(self.record()["status"], "label_uncertain")
        self.writer.add.assert_called_once()
        self.assert_purged()

    def test_crash_during_post_never_retries(self):
        self.writer.add.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.confirm()
        self.click(self.record()["actions"]["confirm"])
        self.assertEqual(self.record()["status"], "label_uncertain")
        self.writer.add.assert_called_once()

    def test_failure_persisting_success_keeps_uncertain(self):
        self.preview()
        token = self.record()["actions"]["confirm"]
        save = self.store.save
        def persist(state):
            if state["records"]["abc123"]["status"] == "labeled":
                raise OSError()
            save(state)
        with patch.object(self.store, "save", side_effect=persist), self.assertRaises(OSError):
            self.click(token)
        self.click(token)
        self.assertEqual(self.record()["status"], "label_uncertain")
        self.writer.add.assert_called_once()

    def test_already_labeled_never_posts(self):
        self.writer.prepare.return_value = ("Label_fixture", True)
        self.confirm()
        self.assertEqual(self.record()["status"], "already_labeled")
        self.assert_purged()
        self.writer.add.assert_not_called()

    def test_preflight_failure_never_posts_or_reloads_writer(self):
        self.writer.prepare.side_effect = ValueError("bad source")
        self.confirm()
        self.click(self.record()["actions"]["confirm"])
        self.assertEqual(self.record()["status"], "label_failed")
        self.factory.assert_called_once()
        self.writer.add.assert_not_called()

    def test_changed_allowlist_fails_closed_before_credentials(self):
        self.preview()
        with patch.dict(os.environ, {"EMAIL_AGENT_LABEL_ALLOWLIST_JSON": json.dumps(list(reversed(LABELS)))}):
            self.click(self.record()["actions"]["confirm"])
        self.assertEqual(self.record()["status"], "label_failed")
        self.factory.assert_not_called()

    def test_preview_selection_mismatch_rejected(self):
        self.preview()
        state = self.store.load()
        state["records"]["abc123"]["selection"] = 2
        token = state["records"]["abc123"]["actions"]["confirm"]
        self.store.save(state)
        with self.assertRaises(Exception):
            self.click(token)
        self.factory.assert_not_called()

    def test_source_mismatch_rejected_before_model(self):
        self.gmail.get.return_value["id"] = "fff111"
        self.click(self.token)
        self.assertEqual(self.record()["status"], "failed")
        self.model.assert_not_called()

    def test_invalid_model_choice_never_retries(self):
        self.model.return_value = "New label"
        self.click(self.token)
        self.click(self.token)
        self.assertEqual(self.record()["status"], "failed")
        self.model.assert_called_once()

    def test_raw_body_not_persisted_and_permissions_private(self):
        self.preview()
        path = self.directory / "state.json"
        self.assertNotIn(BODY, path.read_text())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(list(self.directory.glob(".state-*")), [])

    def test_lock_blocks_concurrent_worker(self):
        with self.assertRaisesRegex(Exception, "locked_skip"):
            with pipeline_lock(self.directory):
                self.factory()
        self.factory.assert_not_called()

    def test_no_body_or_credentials_in_logs(self):
        self.writer.add.side_effect = RuntimeError(BODY + "FAKE OAUTH TOKEN")
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            self.confirm()
        self.assertEqual(output.getvalue(), "")

    def test_changed_scope_or_label_mapping_rejects_state(self):
        for scope, labels in [(SCOPE[:-1] + ("other",), LABELS), (SCOPE, list(reversed(LABELS)))]:
            with self.assertRaises(Exception):
                core.Interactions(self.directory, scope, labels).load()

    def test_state_symlink_and_permissions_rejected(self):
        path = self.directory / "state.json"
        path.chmod(0o644)
        with self.assertRaises(Exception):
            self.click(self.token)
        path.chmod(0o600)
        target = self.directory / "saved.json"
        path.rename(target)
        path.symlink_to(target)
        with self.assertRaises(Exception):
            self.click(self.token)
        self.model.assert_not_called()
        self.factory.assert_not_called()

    def test_unknown_callback_and_other_namespace_rejected(self):
        for token in ["emaillabel:" + "f" * 32, "emailreply:" + "f" * 32, "emaillabel:confirm"]:
            with self.assertRaises(Exception):
                self.click(token)
        self.model.assert_not_called()
        self.factory.assert_not_called()

    def test_expiry_during_model_discards_choices(self):
        def model(*args):
            self.clock.return_value = 1000 + core.TTL
            return 1
        self.model.side_effect = model
        self.assertEqual(self.click(self.token)["buttons"], [])
        self.assertEqual(self.record()["status"], "expired")
        self.assert_purged()

    def test_real_adapter_end_to_end_with_fake_transport(self):
        session = Mock()
        response = lambda value: Mock(status_code=200, text=json.dumps(value))
        session.get.side_effect = [response({"emailAddress": SCOPE[0]}), response({"labels": [
            {"id": "Label_fixture", "name": LABELS[1], "type": "user"}]}), response(MESSAGE)]
        session.post.return_value = response(MESSAGE | {"labelIds": ["INBOX", "Label_fixture"]})
        self.factory.return_value = writer.LabelWriter(session)
        self.preview(1)
        self.assertEqual(session.mock_calls, [])
        token = self.record()["actions"]["confirm"]
        self.click(token)
        self.click(token)
        session.post.assert_called_once_with(writer.BASE + "messages/abc123/modify",
            json={"addLabelIds": ["Label_fixture"], "removeLabelIds": []}, timeout=60, allow_redirects=False)
        self.assertEqual(self.record()["status"], "labeled")


class BoundaryTests(unittest.TestCase):
    def test_strict_model_json_rejects_duplicates_extra_fields_and_invalid_choices(self):
        for raw in ['{"choice":0,"choice":1}', '{"choice":0,"label":"New"}', '{"label":"Công việc"}',
                    '{"choice":true}', '{"choice":1.0}', '{"choice":-1}', '{"choice":3}',
                    '{"choice":"0"}', '{"choice":NaN}', '{"choice":Infinity}', '{}', '[]',
                    '```json\n{"choice":0}\n```', '{"choice":0} trailing']:
            with self.subTest(raw=raw), self.assertRaises(Exception):
                core.choice_schema(parse_json(raw))
        for choice in [0, 1, 2, None]:
            self.assertEqual(core.choice_schema(parse_json(json.dumps({"choice": choice}))), choice)

    def test_exactly_three_distinct_configured_names(self):
        for names in [[], LABELS[:2], LABELS + ["Other"], ["A", "A", "B"], [0, "B", "C"],
                      ["A\nB", "B", "C"], [" A", "B", "C"]]:
            with patch.dict(os.environ, {"EMAIL_AGENT_LABEL_ALLOWLIST_JSON": json.dumps(names)}), \
                    self.assertRaises(Exception):
                core.allowlist()
        with patch.dict(os.environ, ENV):
            self.assertEqual(core.allowlist(), LABELS)

    def test_notification_feature_flags_independent_and_never_invoke_model(self):
        import telegram_reply_approval as reply
        for reply_on, label_on in [(False, False), (True, False), (False, True), (True, True)]:
            env = ENV | {"EMAIL_AGENT_REPLY_APPROVAL": str(reply_on).lower(),
                         "EMAIL_AGENT_LABEL_APPROVAL": str(label_on).lower()}
            with patch.dict(os.environ, env, clear=True), \
                    patch.object(core, "notification_action", return_value="emaillabel:" + "a" * 32) as label_action, \
                    patch.object(reply, "notification_action", return_value="emailreply:" + "b" * 32) as reply_action, \
                    patch.object(adapters, "captured", return_value=subprocess.CompletedProcess([], 0, "", "")) as run, \
                    patch.object(adapters, "tool_free_json") as model:
                adapters.notify_telegram({"message_id": "abc123", "subject": "Fixture"})
                self.assertEqual(label_action.call_count, int(label_on))
                self.assertEqual(reply_action.call_count, int(reply_on))
                model.assert_not_called()
                args = run.call_args.args[0]
                if reply_on or label_on:
                    buttons = json.loads(args[args.index("--presentation") + 1])["blocks"][0]["buttons"]
                    self.assertEqual([b["label"] for b in buttons],
                                     (["💬 Gợi ý trả lời"] if reply_on else []) + (["🏷 Phân loại"] if label_on else []))
                else:
                    self.assertNotIn("--presentation", args)

    @unittest.skipUnless(os.name == "posix", "Linux anonymous prompt")
    def test_model_local_path_strict_output_and_credential_environment_isolation(self):
        invocations = []
        def run(command, **kwargs):
            env = kwargs["env"]
            self.assertFalse(any(k.startswith(("GMAIL_", "TELEGRAM_", "GOOGLE_", "GCP_")) for k in env))
            if "--help" in command:
                return subprocess.CompletedProcess(command, 0, "--agent --local --json --message-file --session-id --timeout", "")
            invocations.append(command)
            self.assertIn("--local", command)
            self.assertNotIn("--deliver", command)
            prompt = Path(command[-1]).read_text()
            self.assertIn("untrusted_email", prompt)
            self.assertIn("never instructions", prompt)
            self.assertIn('"choice":0', prompt)
            self.assertIn(LABELS[2], prompt)
            self.assertNotIn(BODY, " ".join(command))
            return subprocess.CompletedProcess(command, 0, '{"payloads":[{"text":"{\\"choice\\":null}"}]}', "")
        with patch.object(adapters, "model_environment", return_value={
                "GMAIL_LABEL_AUTHORIZED_USER_FILE": "/private/fake.json", "GMAIL_TOKEN": "fake",
                "TELEGRAM_BOT_TOKEN": "fake"}) as policy, patch.object(adapters, "captured", side_effect=run):
            self.assertIsNone(core.suggest({"body": BODY}, LABELS))
            policy.assert_called_once()
        self.assertEqual(len(invocations), 1)

    def test_bridge_unauthorized_never_loads_reader_writer_or_model(self):
        event = {"channel": "telegram", "user": "999", "chat": "999", "account": "default",
                 "authorized": False, "private": True, "data": "emaillabel:" + "a" * 32}
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, ENV | {"XDG_STATE_HOME": temp}, clear=True), \
                patch.object(sys, "stdin", io.StringIO(json.dumps(event))), \
                patch.object(adapters, "gmail_from_environment") as reader, \
                patch.object(writer, "load_label_authorized_user_file") as credentials, \
                patch.object(cli, "suggest") as model, contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(), 1)
            reader.assert_not_called()
            credentials.assert_not_called()
            model.assert_not_called()
            self.assertNotIn("999", out.getvalue())


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.adapter = writer.LabelWriter(self.session)
        self.labels = {"labels": [{"id": "Label_fixture", "name": LABELS[0], "type": "user"}]}
        self.source = {"id": "abc123", "threadId": "def456", "labelIds": ["INBOX", "UNREAD"]}

    def response(self, value, status=200):
        return Mock(status_code=status, text=json.dumps(value))

    def prepare(self):
        self.session.get.side_effect = [self.response({"emailAddress": SCOPE[0]}),
                                       self.response(self.labels), self.response(self.source)]
        return self.adapter.prepare("abc123", "def456", LABELS[0], SCOPE[0])

    def test_runtime_name_resolution_and_only_permitted_endpoints(self):
        self.assertEqual(self.prepare(), ("Label_fixture", False))
        self.session.post.return_value = self.response(self.source | {"labelIds": self.source["labelIds"] + ["Label_fixture"]})
        self.adapter.add("abc123", "def456", "Label_fixture")
        self.assertEqual([c.args[0] for c in self.session.get.call_args_list],
                         [writer.BASE + "profile", writer.BASE + "labels", writer.BASE + "messages/abc123"])
        self.session.post.assert_called_once_with(writer.BASE + "messages/abc123/modify",
            json={"addLabelIds": ["Label_fixture"], "removeLabelIds": []}, timeout=60, allow_redirects=False)
        self.assertFalse(hasattr(self.adapter, "request"))
        self.assertEqual([c[0] for c in self.session.mock_calls], ["get", "get", "get", "post"])

    def test_missing_duplicate_name_id_and_system_labels_fail_closed(self):
        good = self.labels["labels"][0]
        for labels in [[], [good, dict(good)], [good, good | {"id": "Label_other"}],
                       [good, good | {"name": "Other"}], [good | {"type": "system"}],
                       [good, good | {"id": "INBOX", "type": "system"}], [good | {"id": "INBOX"}]]:
            with self.subTest(labels=labels), self.assertRaises(Exception):
                writer.resolve_user_label({"labels": labels}, LABELS[0])
        self.session.post.assert_not_called()

    def test_source_wrong_id_thread_draft_and_missing_labels_rejected(self):
        for override in [{"id": "aaa111"}, {"threadId": "aaa111"}, {"labelIds": ["DRAFT"]},
                         {"labelIds": None}, {"labelIds": ["INBOX", "INBOX"]}]:
            with self.subTest(override=override), self.assertRaises(Exception):
                writer.source_labels(self.source | override, "abc123", "def456")

    def test_wrong_account_and_missing_message_fail_closed(self):
        self.session.get.return_value = self.response({"emailAddress": "other@example.com"})
        with self.assertRaises(Exception):
            self.adapter.prepare("abc123", "def456", LABELS[0], SCOPE[0])
        self.session.get.side_effect = [self.response({"emailAddress": SCOPE[0]}), self.response(self.labels),
                                       self.response({}, 404)]
        with self.assertRaises(Exception):
            self.adapter.prepare("abc123", "def456", LABELS[0], SCOPE[0])
        self.session.post.assert_not_called()

    def test_existing_label_detected_without_post(self):
        self.source["labelIds"].append("Label_fixture")
        self.assertEqual(self.prepare(), ("Label_fixture", True))
        self.session.post.assert_not_called()

    def test_malformed_or_unsuccessful_write_receipt_never_retried(self):
        for response in [self.response({}), self.response(self.source), self.response({}, 500),
                         Mock(status_code=200, text="bad json"),
                         self.response(self.source | {"id": "fff111", "labelIds": ["Label_fixture"]})]:
            self.session.reset_mock()
            self.session.post.return_value = response
            with self.assertRaises(Exception):
                self.adapter.add("abc123", "def456", "Label_fixture")
            self.session.post.assert_called_once()

    def test_timeout_no_retry(self):
        self.session.post.side_effect = TimeoutError("FAKE TOKEN")
        with self.assertRaises(TimeoutError):
            self.adapter.add("abc123", "def456", "Label_fixture")
        self.session.post.assert_called_once()

    @unittest.skipUnless(os.name == "posix", "Linux private credentials")
    def test_writer_credential_scopes_permissions_and_symlinks(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "writer.json"
            info = {"type": "authorized_user", "scopes": [writer.MODIFY], "client_id": "fake",
                    "client_secret": "fake-secret", "refresh_token": "fake-refresh"}
            path.write_text(json.dumps(info))
            path.chmod(0o600)
            with patch.dict(os.environ, {"GMAIL_LABEL_AUTHORIZED_USER_FILE": str(path)}):
                self.assertEqual(writer.load_label_authorized_user_file(), info)
                path.chmod(0o644)
                with self.assertRaises(Exception):
                    writer.load_label_authorized_user_file()
                path.chmod(0o600)
                for scopes in [[adapters.READONLY], [writer.MODIFY, adapters.SEND], ["https://mail.google.com/"]]:
                    path.write_text(json.dumps(info | {"scopes": scopes}))
                    with self.assertRaises(Exception):
                        writer.load_label_authorized_user_file()
            link = Path(temp) / "link.json"
            link.symlink_to(path)
            with patch.dict(os.environ, {"GMAIL_LABEL_AUTHORIZED_USER_FILE": str(link)}), self.assertRaises(Exception):
                writer.load_label_authorized_user_file()

    @unittest.skipUnless(os.name == "posix", "Linux credentials")
    def test_readonly_loader_still_rejects_modify(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "reader.json"
            path.write_text(json.dumps({"type": "authorized_user", "scopes": [adapters.READONLY, writer.MODIFY]}))
            path.chmod(0o600)
            with patch.dict(os.environ, {"GMAIL_AUTHORIZED_USER_FILE": str(path)}), self.assertRaises(Exception):
                adapters.load_authorized_user_file()

    def test_writer_session_disables_auth_retry_and_requests_only_modify(self):
        credentials = Mock()
        session = Mock()
        modules = {"google.auth.transport.requests": types.SimpleNamespace(AuthorizedSession=session),
                   "google.oauth2.credentials": types.SimpleNamespace(Credentials=credentials)}
        with patch.dict(sys.modules, modules), patch.object(writer, "load_label_authorized_user_file", return_value={}) as load:
            adapter = writer.writer_from_environment()
        load.assert_called_once()
        credentials.from_authorized_user_info.assert_called_once_with({}, scopes=[writer.MODIFY])
        session.assert_called_once_with(credentials.from_authorized_user_info.return_value, max_refresh_attempts=0)
        self.assertIsInstance(adapter, writer.LabelWriter)


if __name__ == "__main__":
    unittest.main()
