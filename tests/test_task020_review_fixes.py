"""Regression tests for the Claude Code independent review findings F-01..F-13.

Every Gmail, Telegram, OpenClaw and model interaction is an in-memory fake.
The audit reproduced each failure on ba7081c; these assert the fixed behavior.
"""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from test_daily_sender_digest import daily, Gmail, raw, event, SCOPE, NOW
from agent_reliability import Category, Failure, private_lock
from daily_ledger import Ledger
import alert_spool
import notify_daily_report_failure as alert
import reconcile_daily_delivery as reconcile
import run_daily_email_report as runtime
import run_daily_reply_action as worker_runtime

DAY = 86400
POSIX = os.name == "posix"


def failing_cards(view):
    if view["buttons"]:
        raise RuntimeError("telegram_send_failed")  # notify_telegram require()


@unittest.skipUnless(POSIX, "Linux permissions, fsync and flock")
class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.clock = NOW
        self.store = daily.Store(Path(self.temp.name) / "daily", SCOPE, now=lambda: self.clock)
        self.gmail = Gmail()
        self.notices, self.models = [], []

    def generate(self, email):
        self.models.append(email["message_id"])
        return {"summary": "Summary", "draft_body": "Reply " + email["message_id"]}

    def report(self, notify=None, generate=None):
        with self.store.lock(timeout=1):
            return self.store.report(self.gmail, notify or self.notices.append, generate or self.generate)

    def ledger(self):
        return Ledger(self.store)

    def day(self):
        return self.ledger().today()[1]

    def cards(self):
        return [n for n in self.notices if n["buttons"]]

    def reconcile(self, *args):
        out = io.StringIO()
        with (patch.object(reconcile, "Store", side_effect=lambda *a: self.store),
              patch.object(reconcile, "configuration", return_value=SCOPE),
              patch.object(reconcile, "directory_from_environment", return_value=self.store.directory),
              redirect_stdout(out)):
            code = reconcile.main(list(args))
        return code, json.loads(out.getvalue())


class UncertainDeliveryTests(Base):
    def test_B1_one_failed_card_never_resent_and_later_days_complete(self):
        self.gmail = Gmail([raw("a1")])
        def flaky(view):
            failing_cards(view)
            self.notices.append(view)
        result = self.report(notify=flaky)
        self.assertEqual(result, {"reported": 0, "failed": 1, "attention": 1})
        self.assertEqual(self.ledger().value["deliveries"]["draft:a1"], "uncertain")
        self.assertEqual(self.day()["status"], "uncertain")  # never COMPLETED
        # Same-day reboot: nothing automatic remains; silent, no resend.
        self.assertEqual(self.report(), {"reported": 0, "failed": 0, "attention": 1})
        statuses = []
        for offset in range(1, 4):
            self.clock = NOW + offset * DAY
            self.gmail = Gmail([raw("a1")] + [raw("b%d" % i, seconds=NOW + i * DAY) for i in range(1, offset + 1)])
            self.assertEqual(self.report()["failed"], 0)
            statuses.append(self.day()["status"])
        self.assertEqual(statuses, ["completed"] * 3)
        self.assertEqual(self.models, ["a1", "b1", "b2", "b3"])
        self.assertFalse(any("Reply a1" in card["text"] for card in self.cards()))  # never resent
        self.assertFalse(self.store.load()["records"]["a1"]["reported"])
        headers = [n["text"] for n in self.notices if n["text"].startswith("Báo cáo Gmail")]
        self.assertNotIn("đối soát", headers[0])  # sent before the uncertainty arose
        self.assertEqual(len(headers), 4)
        self.assertTrue(all("Cần đối soát thủ công: 1 thông báo" in text for text in headers[1:]))
        self.clock = NOW
        self.assertEqual(self.day()["status"], "uncertain")

    def test_B1b_lost_ack_and_operator_skip_then_reconcile_receipt(self):
        self.gmail = Gmail([raw("a1")])
        def lost_ack(view):
            self.notices.append(view)
            failing_cards(view)
        self.report(notify=lost_ack)
        token = self.store.load()["records"]["a1"]["actions"]["skip"]
        with self.store.lock():
            self.store.callback(event(data=token), None)
        self.clock = NOW + DAY
        self.assertEqual(self.report()["failed"], 0)
        self.assertEqual(self.day()["status"], "completed")
        code, listing = self.reconcile()
        self.assertEqual(code, 0)
        self.assertEqual(listing["unresolved"], [{"key": "draft:a1", "delivery": "uncertain",
                                                  "record_status": "skipped", "version": 1, "reported": False}])
        self.assertEqual(self.reconcile("--key", "draft:a1", "--confirm-delivered", "--version", "1")[0], 0)
        self.assertEqual(self.ledger().value["deliveries"]["draft:a1"], "uncertain")  # dry run
        self.assertEqual(self.reconcile("--key", "draft:a1", "--confirm-delivered", "--version", "1",
                                        "--apply")[0], 0)
        self.assertEqual(self.ledger().value["deliveries"]["draft:a1"], "sent")
        self.assertTrue(self.store.load()["records"]["a1"]["reported"])
        self.assertEqual(self.reconcile()[1]["unresolved"], [])

    def test_F04_allow_redelivery_rotates_tokens_and_sends_one_fresh_card(self):
        self.gmail = Gmail([raw("a1")])
        def lost_ack(view):
            self.notices.append(view)
            failing_cards(view)
        self.report(notify=lost_ack)
        old = dict(self.store.load()["records"]["a1"]["actions"])
        # Wrong version, or sent/send_uncertain records, are refused.
        self.assertEqual(self.reconcile("--key", "draft:a1", "--allow-redelivery", "--version", "2",
                                        "--apply")[0], int(Category.STATE))
        self.assertEqual(self.reconcile("--key", "draft:a1", "--allow-redelivery", "--version", "1",
                                        "--apply")[0], 0)
        record = self.store.load()["records"]["a1"]
        self.assertTrue(set(old.values()).isdisjoint(record["actions"].values()))
        self.assertNotIn("draft:a1", self.ledger().value["deliveries"])
        self.assertEqual(self.day()["status"], "partial")
        before = len(self.cards())
        self.report()
        self.assertEqual(len(self.cards()), before + 1)
        self.assertEqual(self.cards()[-1]["buttons"][0][0]["callback_data"], record["actions"]["send"])
        sends = []
        with self.store.lock():
            stale = self.store.callback(event(data=old["send"]), None, send=lambda *a: sends.append(a))
        self.assertEqual(stale["text"], daily.STALE)
        self.assertEqual(sends, [])
        self.assertEqual(self.models, ["a1"])

    def test_F04_send_uncertain_record_is_never_rearmed(self):
        self.gmail = Gmail([raw("a1")])
        def lost_ack(view):
            self.notices.append(view)
            failing_cards(view)
        self.report(notify=lost_ack)
        state = self.store.load()
        state["records"]["a1"]["status"] = "send_uncertain"
        self.store.save(state)
        code, _ = self.reconcile("--key", "draft:a1", "--allow-redelivery", "--version", "1", "--apply")
        self.assertEqual(code, int(Category.STATE))
        self.assertEqual(self.ledger().value["deliveries"]["draft:a1"], "uncertain")
        self.assertEqual(self.store.load()["records"]["a1"]["status"], "send_uncertain")

    def test_B2b_manual_notice_failure_never_blocks_newer_emails(self):
        a1 = raw("a1", seconds=NOW - 500)
        self.gmail = Gmail([a1])
        def contract(email):
            return {"wrong": "contract"}
        self.report(generate=contract)
        self.report(generate=contract)
        def fail_manual(view):
            if "thủ công" in view["text"] and "Mã nguồn" in view["text"]:
                raise RuntimeError("telegram_send_failed")
            self.notices.append(view)
        result = self.report(notify=fail_manual, generate=contract)
        self.assertGreater(result["failed"], 0)
        self.assertEqual(self.ledger().value["deliveries"]["manual:a1"], "uncertain")
        for offset in range(1, 4):
            self.clock = NOW + offset * DAY
            self.gmail = Gmail([a1] + [raw("b%d" % i, seconds=NOW + i * DAY) for i in range(1, offset + 1)])
            self.assertEqual(self.report()["failed"], 0)
        self.assertEqual(self.models, ["b1", "b2", "b3"])
        self.assertEqual(len(self.cards()), 3)
        self.assertTrue(all(card["buttons"] for card in self.cards()))
        self.assertNotIn("a1", self.store.load()["records"])  # no Gmail send buttons for a1

    def test_B2_unparseable_source_manual_notice_failure_isolated(self):
        bad = raw("a1", seconds=NOW - 500)
        bad["payload"]["body"]["data"] = "!invalid base64!"
        self.gmail = Gmail([bad, raw("a2")])
        self.report()
        self.report()
        def fail_manual(view):
            if "Mã nguồn" in view["text"]:
                raise RuntimeError("telegram_send_failed")
            self.notices.append(view)
        result = self.report(notify=fail_manual)
        self.assertGreater(result["failed"], 0)
        self.assertEqual(self.day()["status"], "uncertain")
        self.assertEqual(self.report(), {"reported": 0, "failed": 0, "attention": 1})
        self.assertEqual(self.ledger().value["sources"]["a1"]["raw"], bad)  # evidence kept

    def test_B3_overview_uncertain_does_not_block_drafts(self):
        self.gmail = Gmail([raw("a1"), raw("a2")])
        def fail_overview(view):
            if view["text"].startswith("Báo cáo Gmail hôm nay\nCó 2"):
                raise RuntimeError("telegram_send_failed")
            self.notices.append(view)
        result = self.report(notify=fail_overview)
        self.assertEqual(result["reported"], 0)
        self.assertEqual(sorted(self.models), ["a1", "a2"])  # generation continues, checkpointed
        self.assertEqual(self.day()["status"], "partial")
        result = self.report()  # same-day reboot
        self.assertEqual(result["reported"], 2)
        self.assertEqual(len(self.cards()), 2)
        self.assertFalse(any(n["text"].startswith("Báo cáo Gmail hôm nay\nCó") for n in self.notices))
        self.assertEqual(self.day()["status"], "uncertain")
        self.assertEqual(sorted(self.models), ["a1", "a2"])

    def test_gateway_down_mid_batch_defers_without_delivery_intent(self):
        self.gmail = Gmail([raw("a1"), raw("a2"), raw("a3")])
        def down_after_first(view):
            if "Email 2/3" in view["text"]:
                raise RuntimeError("telegram_send_failed")
            if "Email 3/3" in view["text"]:
                self.fail("circuit must stop further deliveries this run")
            self.notices.append(view)
        self.report(notify=down_after_first)
        deliveries = self.ledger().value["deliveries"]
        self.assertEqual(deliveries["draft:a2"], "uncertain")
        self.assertNotIn("draft:a3", deliveries)
        self.report()
        self.assertEqual(self.ledger().value["deliveries"]["draft:a3"], "sent")


class TransientModelTests(Base):
    def test_B4_three_boots_during_outage_do_not_make_valid_email_manual(self):
        self.gmail = Gmail([raw("a1")])
        def down(email):
            raise Failure(Category.MODEL_UNAVAILABLE)
        for _ in range(3):
            self.assertEqual(self.report(generate=down)["failed"], 1)
        self.assertFalse(self.ledger().value["sources"]["a1"]["manual"])
        self.clock = NOW + DAY
        self.report()
        self.assertEqual(self.models, ["a1"])
        self.assertEqual(len(self.cards()), 1)

    def test_transient_failures_on_three_distinct_days_become_manual(self):
        self.gmail = Gmail([raw("a1")])
        def down(email):
            raise RuntimeError("process timeout")
        for offset in range(3):
            self.clock = NOW + offset * DAY
            self.report(generate=down)
        source = self.ledger().value["sources"]["a1"]
        self.assertTrue(source["manual"])
        self.assertEqual(len(source["transient"]), 3)
        self.assertEqual(source["attempts"], 0)
        self.assertEqual(len([n for n in self.notices if "Mã nguồn: a1" in n["text"]]), 1)


class MinimizationTests(Base):
    def test_F09_raw_snapshot_dropped_only_when_resolved_and_delivered(self):
        self.gmail = Gmail([raw("a1"), raw("a2"), raw("a3"), raw("a4")])
        self.report()
        state = self.store.load()
        state["records"]["a1"]["status"] = "sent"
        state["records"]["a2"]["status"] = "send_uncertain"
        state["records"]["a3"]["status"] = "failed"
        self.store.save(state)
        self.clock = NOW + DAY
        self.report()
        sources = self.ledger().value["sources"]
        self.assertEqual((sources["a1"]["raw"], sources["a1"]["email"]), ({}, None))
        for mid in ("a2", "a3", "a4"):  # send_uncertain, failed, awaiting review keep evidence
            self.assertEqual(sources[mid]["raw"]["id"], mid)
            self.assertIsNotNone(sources[mid]["email"])
        self.assertIn("a1", self.store.load()["records"])  # tombstone kept


class EditSessionTests(Base):
    def setUp(self):
        super().setUp()
        self.gmail = Gmail([raw("a1")])
        self.report()

    def test_F12_none_expired_and_busy_are_distinct(self):
        with self.store.lock():
            none = self.store.edit_session(event())
            self.assertEqual((none["session"], none["text"], none["edit_token"]), ("none", daily.GUIDANCE, None))
            token = self.store.load()["records"]["a1"]["actions"]["edit"]
            self.store.callback(event(data=token), None)
            self.assertEqual(self.store.edit_session(event())["session"], "active")
            self.clock = NOW + 900
            expired = self.store.edit_session(event())
            self.assertEqual((expired["session"], expired["text"]), ("expired", daily.EDIT_EXPIRED))
            state = self.store.load()
            self.assertIsNone(state["active_edit"])
            self.assertEqual(state["records"]["a1"]["status"], "awaiting_review")
        env = {"EMAIL_AGENT_DAILY_STATE_DIR": str(self.store.directory)}
        with self.store.lock(), patch.dict(os.environ, env), \
                patch.object(worker_runtime, "configuration", return_value=SCOPE), \
                patch.object(worker_runtime, "Store", side_effect=lambda *a: self.store), \
                patch.object(worker_runtime.sys, "stdin", io.StringIO(json.dumps(event(kind="edit_session")))):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(worker_runtime.main(), 0)
        busy = json.loads(out.getvalue())
        self.assertEqual((busy["text"], busy["session"]), (daily.BUSY, "busy"))
        self.assertNotIn("edit_token", busy)


class AlertSpoolTests(unittest.TestCase):
    def setUp(self):
        if not POSIX:
            self.skipTest("durable alert queue")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {"EMAIL_AGENT_MONITOR_STATE_DIR": self.temp.name})
        env.start()
        self.addCleanup(env.stop)

    def main(self, *args):
        with patch.object(alert.sys, "argv", ["h", *args]), redirect_stdout(io.StringIO()):
            return alert.main()

    def slot(self):
        return alert_spool.load(Path(self.temp.name))

    def test_C1_interrupted_recovery_keeps_original_category(self):
        with patch.object(alert_spool, "boot_id", return_value="A"):
            alert_spool.finish(alert_spool.begin(), Category.GMAIL_AUTH)
            with patch.object(alert, "notify", return_value="daily_failure_alert_gateway_timeout"):
                self.main()
        with patch.object(alert_spool, "boot_id", return_value="B"):
            alert_spool.begin()  # next run starts; WSL closes before finish()
        with (patch.object(alert_spool, "boot_id", return_value="C"),
              patch.object(alert, "notify", return_value="daily_failure_alert_transport_succeeded") as send):
            self.main("--replay-only")
        send.assert_called_once_with(Category.UNKNOWN, Category.GMAIL_AUTH)
        self.assertIn("Lỗi trước chưa báo: GMAIL_AUTH.", alert.alert_text(Category.UNKNOWN, Category.GMAIL_AUTH))

    def test_C1b_new_failure_alert_carries_unsent_previous(self):
        with patch.object(alert_spool, "boot_id", return_value="A"):
            alert_spool.finish(alert_spool.begin(), Category.GMAIL_AUTH)
        with patch.object(alert_spool, "boot_id", return_value="B"):
            alert_spool.finish(alert_spool.begin(), Category.MODEL_UNAVAILABLE)
            with (patch.dict(os.environ, {"MONITOR_EXIT_CODE": "exited", "MONITOR_EXIT_STATUS": "15"}),
                  patch.object(alert, "notify", return_value="daily_failure_alert_transport_succeeded") as send):
                self.main()
        send.assert_called_once_with(Category.MODEL_UNAVAILABLE, Category.GMAIL_AUTH)
        self.assertEqual((self.slot()["status"], self.slot()["previous"]), ("sent", None))

    def test_C2_success_clears_but_retains_previous_as_private_evidence(self):
        with patch.object(alert_spool, "boot_id", return_value="A"):
            alert_spool.finish(alert_spool.begin(), Category.MODEL_UNAVAILABLE)
        with patch.object(alert_spool, "boot_id", return_value="B"):
            alert_spool.finish(alert_spool.begin())
            with patch.object(alert, "notify") as send:
                self.main("--replay-only")
        send.assert_not_called()
        self.assertEqual((self.slot()["status"], self.slot()["previous"]), ("clear", "MODEL_UNAVAILABLE"))

    def test_C3_handler_never_holds_alert_lock_during_gateway_wait_or_send(self):
        with patch.object(alert_spool, "boot_id", return_value="A"):
            alert_spool.finish(alert_spool.begin(), Category.GATEWAY_NOT_READY)
            seen = {}
            def slow_notify(category, previous):
                # A daily run starting while the alert waits must not be LOCKED.
                seen["token"] = alert_spool.begin()
                return "daily_failure_alert_transport_succeeded"
            with patch.object(alert, "notify", side_effect=slow_notify):
                self.assertEqual(self.main("--replay-only"), 0)
        slot = self.slot()
        # The handler's completion did not overwrite the newer running slot.
        self.assertEqual((slot["id"], slot["status"]), (seen["token"], "running"))
        self.assertEqual(slot["previous"], "GATEWAY_NOT_READY")

    def test_concurrent_handlers_one_claim_and_stale_claim_takeover(self):
        with patch.object(alert_spool, "boot_id", return_value="A"):
            alert_spool.finish(alert_spool.begin(), Category.GMAIL_AUTH)
            first = alert_spool.claim(replay=True, now=1000)
            self.assertIsNotNone(first)
            self.assertIsNone(alert_spool.claim(replay=True, now=1001))
            self.assertIsNone(alert_spool.claim(replay=False, now=1001))
            second = alert_spool.claim(replay=True, now=1000 + alert_spool.CLAIM_TTL)
            self.assertIsNotNone(second)
            self.assertFalse(alert_spool.complete(first[0], first[1], True))  # stale owner
            self.assertTrue(alert_spool.complete(second[0], second[1], True))
        self.assertEqual(self.slot()["status"], "sent")

    def test_corrupt_spool_is_not_overwritten(self):
        path = Path(self.temp.name) / "pending-alert.json"
        path.write_text('{"id": "x"}')
        path.chmod(0o600)
        with self.assertRaises(Failure):
            alert_spool.begin()
        self.assertEqual(self.main(), 1)
        self.assertEqual(path.read_text(), '{"id": "x"}')


class RuntimeBoundaryTests(unittest.TestCase):
    def run_main(self, stamp, status="pending", begin=None):
        out, sleeps = io.StringIO(), []
        clock = {"now": stamp}
        def sleep(seconds):
            sleeps.append(seconds)
            clock["now"] += seconds
        with (patch.object(runtime.os, "name", "posix"),
              patch.object(runtime, "configuration", return_value=SCOPE),
              patch.object(runtime, "directory_from_environment"), patch.object(runtime, "Store") as store,
              patch.object(runtime, "Ledger") as ledger, patch.object(runtime, "preflight"),
              patch.object(runtime, "wait_ready"),
              patch.object(runtime.alert_spool, "begin", side_effect=begin, return_value="a" * 32) as begin_call,
              patch.object(runtime.alert_spool, "finish"), patch.object(runtime, "gmail_from_environment"),
              redirect_stdout(out)):
            ledger.return_value.today.return_value = ("date", {"status": status})
            store.return_value.report.return_value = {"reported": 1, "failed": 0, "attention": 0}
            code = runtime.main(clock=lambda: clock["now"], sleep=sleep)
            reported = store.return_value.report.called
        return code, json.loads(out.getvalue()), sleeps, reported, begin_call.called

    @staticmethod
    def bangkok(text):
        from datetime import datetime
        return datetime.fromisoformat(text + "+07:00").timestamp()

    def test_F13_boundary_seconds(self):
        cases = {"06:57:59": (False, []), "06:58:01": (True, [119.0]), "06:59:59": (True, [1.0]),
                 "07:00:00": (True, []), "07:00:01": (True, []), "12:00:00": (True, [])}
        for clock, (reported, sleeps) in cases.items():
            with self.subTest(clock=clock):
                code, output, slept, ran, _ = self.run_main(self.bangkok("2026-10-15T" + clock))
                self.assertEqual(code, 0)
                self.assertEqual(ran, reported)
                self.assertEqual(slept, sleeps)
                self.assertEqual(output["category"], "COMPLETED" if reported else "NOT_DUE")

    def test_attention_day_is_silent_and_never_completed(self):
        code, output, _, ran, began = self.run_main(self.bangkok("2026-10-15T09:00:00"), status="uncertain")
        self.assertEqual((code, output["category"], ran, began), (0, "ATTENTION_PENDING", False, False))

    def test_alert_spool_contention_never_blocks_report(self):
        code, output, _, ran, _ = self.run_main(self.bangkok("2026-10-15T09:00:00"),
                                                begin=Failure(Category.LOCKED))
        self.assertEqual((code, ran), (0, True))
        self.assertEqual(output, {"code": 0, "category": "COMPLETED", "alert_spool": "unavailable"})

    def test_fast_failure_is_bounded_no_retry_loop(self):
        # Each activation is one bounded attempt; systemd Restart=no and no
        # OnFailure loop. Persistent catch-up + OnStartupSec can at most add one.
        runs = 0
        for _ in range(2):
            with (patch.object(runtime.os, "name", "posix"),
                  patch.object(runtime, "configuration", side_effect=RuntimeError("PRIVATE")),
                  redirect_stdout(io.StringIO()) as out):
                code = runtime.main(clock=lambda: self.bangkok("2026-10-15T09:00:00"), sleep=self.fail)
            runs += 1
            self.assertEqual(code, int(Category.CONFIG))
            self.assertNotIn("PRIVATE", out.getvalue())
        self.assertEqual(runs, 2)


if __name__ == "__main__":
    unittest.main()
