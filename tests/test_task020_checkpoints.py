"""POSIX checkpoints/concurrency with synthetic mail, fake time and transports."""
from contextlib import redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_daily_sender_digest import daily, Gmail, raw, event, SCOPE, NOW
from test_task020_reliability import Clock
from agent_reliability import Category, Failure, MissingSource, private_lock, read_private, write_private
from daily_ledger import Ledger
import alert_spool
import notify_daily_report_failure as alert


@unittest.skipUnless(os.name == "posix", "Linux permissions, fsync and flock")
class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = daily.Store(self.root / "daily", SCOPE, now=lambda: NOW)
        self.gmail = Gmail()
        self.notices, self.models = [], []

    def generate(self, email):
        self.models.append(email["message_id"])
        return {"summary": "Summary", "draft_body": "Reply"}

    def report(self, notify=None, generate=None):
        with self.store.lock(timeout=120):
            return self.store.report(self.gmail, notify or self.notices.append, generate or self.generate)

    def ledger(self):
        return Ledger(self.store)

    def test_empty_three_boots_same_day_one_notice_zero_models(self):
        for _ in range(3):
            self.store = daily.Store(self.store.directory, SCOPE, now=lambda: NOW)
            self.assertEqual(self.report(), {"reported": 0, "failed": 0, "attention": 0})
        self.assertEqual(len(self.notices), 1)
        self.assertEqual(self.models, [])
        self.assertEqual(self.notices[0]["text"],
                         "Báo cáo Gmail hôm nay: Không có email mới từ người gửi được theo dõi.")

    def test_auth_error_has_no_empty_notice_and_restart_can_recover(self):
        self.gmail.profile = lambda: (_ for _ in ()).throw(Failure(Category.GMAIL_AUTH))
        with self.assertRaises(Failure) as failure:
            self.report()
        self.assertEqual(failure.exception.category, Category.GMAIL_AUTH)
        self.assertEqual(self.notices, [])
        self.gmail = Gmail()
        self.report()
        self.report()
        self.assertEqual(len(self.notices), 1)

    def test_one_email_correct_recipient_and_new_arrival_waits_next_day(self):
        self.gmail = Gmail([raw("a1")])
        self.report()
        record = self.store.load()["records"]["a1"]
        self.assertEqual(record["draft"]["to"], SCOPE[1])
        self.assertEqual(len(self.notices), 2)
        self.gmail = Gmail([raw("a1"), raw("a2", seconds=NOW + 10)])
        self.store.now = lambda: NOW + 20
        self.report()
        self.assertEqual(self.models, ["a1"])
        self.store.now = lambda: NOW + 86400
        self.report()
        self.assertEqual(self.models, ["a1", "a2"])

    def test_model_failure_restart_only_failed_item_and_one_overview(self):
        self.gmail = Gmail([raw("a1"), raw("a2")])
        def first(email):
            if email["message_id"] == "a2":
                raise Failure(Category.MODEL_UNAVAILABLE)
            return self.generate(email)
        self.assertEqual(self.report(generate=first), {"reported": 1, "failed": 1, "attention": 0})
        self.assertEqual(self.report(), {"reported": 1, "failed": 0, "attention": 0})
        self.assertEqual(self.models, ["a1", "a2"])
        self.assertEqual(sum("Có 2 email" in n["text"] for n in self.notices), 1)

    def test_crash_after_transport_before_receipt_never_replays_draft(self):
        self.gmail = Gmail([raw("a1")])
        def crash(view):
            self.notices.append(view)
            if view["buttons"]:
                raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.report(notify=crash)
        self.assertEqual(self.ledger().value["deliveries"]["draft:a1"], "inflight")
        self.assertEqual(self.report(), {"reported": 0, "failed": 1, "attention": 1})
        self.assertEqual(len(self.notices), 2)
        self.assertEqual(self.models, ["a1"])
        self.assertFalse(self.store.load()["records"]["a1"]["reported"])
        self.assertEqual(self.ledger().value["deliveries"]["draft:a1"], "uncertain")

    def test_crash_after_no_mail_transport_does_not_claim_completed(self):
        def crash(view):
            self.notices.append(view)
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.report(notify=crash)
        self.assertEqual(self.report()["failed"], 1)  # uncertain: never resent
        self.assertEqual(len(self.notices), 1)
        self.assertEqual(self.ledger().today()[1]["status"], "uncertain")
        self.assertEqual(self.report(), {"reported": 0, "failed": 0, "attention": 0})
        self.assertEqual(len(self.notices), 1)

    def test_model_output_checkpoint_prevents_regeneration_on_restart(self):
        self.gmail = Gmail([raw("a1")])
        def crash_before_fence(key, view, notify):
            if key.startswith("draft:"):
                raise KeyboardInterrupt()
            original(key, view, notify)
        ledger = self.ledger()
        original = ledger.deliver
        with patch.object(daily, "Ledger", return_value=ledger), patch.object(ledger, "deliver", side_effect=crash_before_fence):
            with self.assertRaises(KeyboardInterrupt):
                self.report()
        self.assertEqual(self.report(), {"reported": 1, "failed": 0, "attention": 0})
        self.assertEqual(self.models, ["a1"])

    def test_poison_email_manual_notice_after_three_attempts_preserves_raw_no_buttons(self):
        bad = raw("a1")
        bad["payload"]["body"]["data"] = "!invalid base64!"
        self.gmail = Gmail([bad, raw("a2")])
        for _ in range(4):
            self.report()
        item = self.ledger().value["sources"]["a1"]
        self.assertEqual(item["attempts"], 3)
        self.assertTrue(item["manual"])
        self.assertEqual(item["raw"], bad)
        self.assertNotIn("a1", self.store.load()["records"])
        notices = [n for n in self.notices if "Cần kiểm tra email này thủ công" in n["text"]]
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["buttons"], [])
        self.assertEqual(self.models, ["a2"])
        self.assertEqual(self.ledger().today()[1]["status"], "manual")

    def test_deleted_source_does_not_block_other_drafts_forever(self):
        self.gmail = Gmail([raw("a1"), raw("a2")])
        get = self.gmail.get
        def missing(mid):
            if mid == "a1":
                raise MissingSource()
            return get(mid)
        self.gmail.get = missing
        for _ in range(4):
            self.report()
        self.assertEqual(self.models, ["a2"])
        source = self.ledger().value["sources"]["a1"]
        self.assertTrue(source["manual"])
        self.assertEqual(source["raw"]["id"], "a1")
        self.assertNotIn("a1", self.store.load()["records"])

    def test_completed_without_delivery_receipt_is_state_failure(self):
        self.report()
        ledger = self.ledger()
        ledger.value["deliveries"].clear()
        ledger.save()
        with self.assertRaises(Failure) as error:
            self.report()
        self.assertEqual(error.exception.category, Category.STATE)

    def test_missing_state_beside_existing_ledger_never_bootstraps_empty_send_fences(self):
        self.report()
        (self.store.directory / "state.json").unlink()
        with self.assertRaises(Failure) as error:
            self.report()
        self.assertEqual(error.exception.category, Category.STATE)
        self.assertFalse((self.store.directory / "state.json").exists())

    def test_persistent_model_contract_error_is_bounded_without_success_tombstone(self):
        self.gmail = Gmail([raw("a1")])
        calls = []
        def invalid(email):
            calls.append(1)
            return {"wrong": "contract"}
        for _ in range(5):
            self.report(generate=invalid)
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.store.load()["records"], {})
        self.assertEqual(self.ledger().value["sources"]["a1"]["email"]["body"], "Only source a1")

    def test_bound_lock_wait_no_inode_deletion(self):
        clock = Clock()
        with self.store.lock():
            inode = (self.store.directory / "pipeline.lock").stat().st_ino
            with self.assertRaises(Failure) as failure:
                with private_lock(self.store.directory, "pipeline.lock", timeout=120,
                                  clock=clock.now, sleep=clock.sleep):
                    self.fail("entered busy pipeline")
        self.assertEqual(failure.exception.category, Category.LOCKED)
        self.assertEqual(clock.value, 120)
        self.assertEqual((self.store.directory / "pipeline.lock").stat().st_ino, inode)

    def test_edit_ttl_missing_metadata_and_target_race_fail_closed(self):
        self.gmail = Gmail([raw("a1"), raw("a2")])
        self.report()
        def edit(mid):
            token = self.store.load()["records"][mid]["actions"]["edit"]
            return self.store.callback(event(data=token), None)
        with self.store.lock():
            edit("a1")
            binding = self.store.edit_session(event())["edit_token"]
            edit("a2")
            response = self.store.revise(event(message_id="1", text="shorter", edit_token=binding), None)
            self.assertEqual(response["text"], daily.GUIDANCE)
            self.assertEqual(self.store.load()["active_edit"], "a2")
            self.store.now = lambda: NOW + 901
            response = self.store.revise(event(message_id="2", text="shorter"), None)
            self.assertEqual(response["text"], daily.EDIT_EXPIRED)
            self.assertEqual(self.store.load()["records"]["a2"]["draft"]["draft_body"], "Reply")
            edit("a1")
            (self.store.directory / "edit-session.json").unlink()
            self.assertIsNone(self.store.edit_session(event())["edit_token"])
        self.assertEqual(self.models, ["a1", "a2"])

    def test_ledger_private_permissions_corruption_and_no_state_schema_change(self):
        self.report()
        path = self.store.directory / "daily-ledger.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(set(self.store.load()), {"schema", "scope", "since", "records", "active_edit", "text_events"})
        original = path.read_bytes()
        path.chmod(0o644)
        with self.assertRaises(Failure):
            self.report()
        path.chmod(0o600)
        path.write_text('{"schema":1,"schema":1}')
        with self.assertRaises(Failure):
            self.report()
        self.assertNotEqual(path.read_bytes(), original)

    def test_recovery_retains_send_uncertain_and_failed_draft_data(self):
        self.gmail = Gmail([raw("a1"), raw("a2")])
        self.report()
        state = self.store.load()
        state["records"]["a1"]["status"] = "send_uncertain"
        state["records"]["a2"]["status"] = "failed"
        self.store.save(state)
        before = copy.deepcopy(state["records"])
        self.store.recover(state)
        self.assertEqual(state["records"], before)


@unittest.skipUnless(os.name == "posix", "durable alert queue")
class AlertSpoolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {"EMAIL_AGENT_MONITOR_STATE_DIR": self.temp.name})
        env.start()
        self.addCleanup(env.stop)

    def run_main(self, *args):
        with patch.object(alert.sys, "argv", ["handler", *args]), redirect_stdout(io.StringIO()):
            return alert.main()

    def test_gateway_never_ready_preserves_queue_then_next_boot_replays_once(self):
        with patch.object(alert_spool, "boot_id", return_value="boot-A"):
            token = alert_spool.begin()
            alert_spool.finish(token, Category.GATEWAY_NOT_READY)
            with patch.object(alert, "notify", return_value="daily_failure_alert_gateway_timeout"):
                self.assertEqual(self.run_main(), 1)
        self.assertEqual(alert_spool.load(Path(self.temp.name))["status"], "pending")
        with (patch.object(alert_spool, "boot_id", return_value="boot-B"),
              patch.object(alert, "notify", return_value="daily_failure_alert_transport_succeeded") as send):
            self.assertEqual(self.run_main("--replay-only"), 0)
            self.assertEqual(self.run_main("--replay-only"), 0)
        send.assert_called_once_with(Category.GATEWAY_NOT_READY, None)

    def test_interrupted_prior_boot_alert_and_same_boot_running_is_silent(self):
        with patch.object(alert_spool, "boot_id", return_value="boot-A"):
            alert_spool.begin()
            with patch.object(alert, "notify") as notify:
                self.run_main("--replay-only")
                notify.assert_not_called()
        with (patch.object(alert_spool, "boot_id", return_value="boot-B"),
              patch.object(alert, "notify", return_value="daily_failure_alert_transport_succeeded") as notify):
            self.run_main("--replay-only")
            notify.assert_called_once_with(Category.UNKNOWN, None)

    def test_alert_killed_mid_delivery_keeps_claim_then_next_boot_delivers(self):
        state = Path(self.temp.name) / "state.json"
        state.write_text('synthetic send_uncertain data')
        with (patch.object(alert_spool, "boot_id", return_value="boot-A"),
              patch.object(alert, "notify", side_effect=KeyboardInterrupt())):
            with self.assertRaises(KeyboardInterrupt):
                self.run_main()
        self.assertEqual(alert_spool.load(Path(self.temp.name))["status"], "sending")
        self.assertEqual(state.read_text(), 'synthetic send_uncertain data')
        with (patch.object(alert_spool, "boot_id", return_value="boot-B"),
              patch.object(alert, "notify", return_value="daily_failure_alert_transport_succeeded") as send):
            self.assertEqual(self.run_main("--replay-only"), 0)
        send.assert_called_once()
        self.assertEqual(alert_spool.load(Path(self.temp.name))["status"], "sent")
