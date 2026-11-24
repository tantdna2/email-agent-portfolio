"""Offline failure/readiness tests. Every subprocess/network/model call is fake."""
from contextlib import nullcontext, redirect_stdout
from datetime import datetime
import ast
import configparser
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import agent_reliability as safe
import realtime_email_adapters as adapters
import run_daily_email_report as runtime
import notify_daily_report_failure as alert
from test_daily_sender_digest import OPENCLAW_PROVIDER_REJECTED_STDOUT


class Clock:
    def __init__(self):
        self.value = 0
    def now(self):
        return self.value
    def sleep(self, seconds):
        self.value += seconds


def ready(account="default"):
    # Public HealthSummary for an operator.read caller (OpenClaw v2026.9.5):
    # successful probes are stripped, runtime fields remain.
    return {"ok": True, "channels": {"telegram": {"accounts": {
        account: {"accountId": account, "enabled": True, "configured": True, "running": True,
                  "connected": True, "lastError": None, "mode": "polling", "lastProbeAt": 1}}}}}


class ReadinessTests(unittest.TestCase):
    def test_late_gateway_and_correct_account_probe(self):
        clock = Clock()
        run = Mock(side_effect=[subprocess.CompletedProcess([], 1, "private"),
                               subprocess.CompletedProcess([], 0, json.dumps(ready("other"))),
                               subprocess.CompletedProcess([], 0, json.dumps(ready()))])
        safe.wait_ready("default", clock=clock.now, sleep=clock.sleep, run=run)
        self.assertEqual(clock.value, 10)
        self.assertEqual(run.call_count, 3)
        self.assertIn("--verbose", run.call_args.args[0])
        self.assertNotIn("OPENCLAW_CONFIG_PATH", run.call_args.kwargs["env"])

    def test_never_ready_timeout_includes_process_duration_and_no_raw_log(self):
        clock = Clock()
        def run(*args, **kwargs):
            clock.sleep(kwargs["timeout"])
            raise subprocess.TimeoutExpired("PRIVATE-TOKEN", kwargs["timeout"])
        with self.assertRaises(safe.Failure) as error:
            safe.wait_ready("default", clock=clock.now, sleep=clock.sleep, run=run)
        self.assertEqual(clock.value, 180)
        self.assertEqual(error.exception.category, safe.Category.GATEWAY_NOT_READY)
        self.assertNotIn("PRIVATE", str(error.exception))

    def test_unknown_schemas_and_top_level_ok_fail_closed(self):
        for value in ({"ok": True}, {"result": ready()}, ready("wrong"), None, [], {"ok": "true"}):
            self.assertFalse(safe.telegram_ready(value, "default"))
        self.assertTrue(safe.telegram_ready(ready(), "default"))
        for change in ({"probe": {"ok": False, "error": "redacted"}}, {"running": False},
                       {"connected": False}, {"enabled": False}, {"configured": False},
                       {"healthState": "disconnected"}, {"healthState": "not-running"},
                       {"lifecycle": "starting"}, {"running": "true"}):
            value = ready()
            value["channels"]["telegram"]["accounts"]["default"].update(change)
            self.assertFalse(safe.telegram_ready(value, "default"), change)
        timeout_record = {"ok": True, "channels": {"telegram": {"accounts": {"default": {
            "accountId": "default", "lastError": "health collection timed out after 7000ms",
            "probe": {"ok": False, "timedOut": True}}}}}}
        self.assertFalse(safe.telegram_ready(timeout_record, "default"))

    def test_bangkok_schedule_ignores_host_timezone(self):
        def stamp(text):
            return datetime.fromisoformat(text).timestamp()
        self.assertFalse(safe.due(stamp("2026-10-15T06:59:59+07:00")))
        self.assertTrue(safe.due(stamp("2026-10-15T07:00:00+07:00")))
        self.assertTrue(safe.due(stamp("2026-10-15T12:00:00+07:00")))
        self.assertEqual(safe.local_day(stamp("2026-10-14T18:00:00+00:00")), "2026-10-15")

    def test_gateway_up_but_selected_channel_unready_has_telegram_category(self):
        clock = Clock()
        value = ready()
        value["channels"]["telegram"]["accounts"]["default"]["running"] = False
        run = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(value)))
        with self.assertRaises(safe.Failure) as error:
            safe.wait_ready("default", clock=clock.now, sleep=clock.sleep, run=run)
        self.assertEqual(error.exception.category, safe.Category.TELEGRAM_SEND)
        self.assertEqual(clock.value, 180)


class ReadRetryTests(unittest.TestCase):
    def test_temporary_get_retries_bounded_and_watch_never_retries(self):
        for method, expected in (("GET", 3), ("POST", 1)):
            session = Mock()
            session.request.side_effect = ConnectionError("PRIVATE")
            with patch.object(adapters.time, "sleep") as sleep, self.assertRaises(safe.Failure) as error:
                adapters.Gmail(session).request(method, "profile")
            self.assertEqual(session.request.call_count, expected)
            self.assertEqual(sleep.call_count, expected - 1)
            self.assertEqual(error.exception.category, safe.Category.GMAIL_UNAVAILABLE)

    def test_401_and_invalid_grant_never_return_empty_or_retry(self):
        refresh = type("RefreshError", (Exception,), {"__module__": "google.auth.exceptions"})
        for failure in (Mock(status_code=401), refresh("invalid_grant PRIVATE")):
            session = Mock()
            if isinstance(failure, Exception):
                session.request.side_effect = failure
            else:
                session.request.return_value = failure
            with self.assertRaises(safe.Failure) as error:
                adapters.Gmail(session).profile()
            self.assertEqual(error.exception.category, safe.Category.GMAIL_AUTH)
            self.assertEqual(session.request.call_count, 1)

    def test_message_404_is_definitive_source_failure_not_an_empty_mailbox(self):
        session = Mock()
        session.request.return_value = Mock(status_code=404)
        with self.assertRaises(safe.MissingSource):
            adapters.Gmail(session).get("a1")
        self.assertEqual(session.request.call_count, 1)

    def test_retryable_oauth_server_error_is_not_invalid_grant(self):
        refresh = type("RefreshError", (Exception,), {"__module__": "google.auth.exceptions", "retryable": True})
        session = Mock()
        session.request.side_effect = [refresh("PRIVATE"), Mock(status_code=200, text="{}")]
        with patch.object(adapters.time, "sleep"):
            self.assertEqual(adapters.Gmail(session).profile(), {})
        self.assertEqual(session.request.call_count, 2)

    def test_transient_status_then_valid_read(self):
        session = Mock()
        session.request.side_effect = [Mock(status_code=503), Mock(status_code=429),
                                       Mock(status_code=200, text='{"messages":[]}')]
        with patch.object(adapters.time, "sleep"):
            self.assertEqual(adapters.Gmail(session).request("GET", "messages"), {"messages": []})
        self.assertEqual(session.request.call_count, 3)


class ModelRetryTests(unittest.TestCase):
    def test_400_unavailable_retries_three_then_fails(self):
        with (patch.object(adapters, "_run_tool_free_process", side_effect=adapters.RetryableModelFailure()) as run,
              patch.object(adapters.time, "sleep") as sleep, self.assertRaises(safe.Failure)):
            adapters.initial_json_with_retry({}, "rules")
        self.assertEqual(run.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2, 5])

    def test_recovers_without_extra_model_readiness_inference(self):
        value = {"summary": "ok", "draft_body": "Reply"}
        raw = json.dumps({"payloads": [{"text": json.dumps(value)}]})
        with (patch.object(adapters, "_run_tool_free_process",
                           side_effect=[adapters.RetryableModelFailure(), raw]) as run,
              patch.object(adapters.time, "sleep")):
            self.assertEqual(adapters.initial_json_with_retry({}, "rules"), value)
        self.assertEqual(run.call_count, 2)

    def test_non_retryable_process_failure_is_transient_not_retried(self):
        with (patch.object(adapters, "_run_tool_free_process", side_effect=RuntimeError("PRIVATE")) as run,
              patch.object(adapters.time, "sleep"), self.assertRaises(safe.ModelUnavailable) as error):
            adapters.initial_json_with_retry({}, "rules")
        run.assert_called_once()
        self.assertNotIsInstance(error.exception, adapters.RetryableModelFailure)
        self.assertNotIn("PRIVATE", str(error.exception))

    def test_invalid_output_and_revision_do_not_retry(self):
        for raw in ('{}', 'invalid', '{"payloads":[{"text":"not JSON"}]}'):
            with (patch.object(adapters, "_run_tool_free_process", return_value=raw) as run,
                  self.assertRaises(safe.ModelContractError)):
                adapters.initial_json_with_retry({}, "rules")
            run.assert_called_once()
        with (patch.object(adapters, "_run_tool_free_process", side_effect=adapters.RetryableModelFailure()) as run,
              self.assertRaises(Exception)):
            adapters.tool_free_text({}, "rules")
        run.assert_called_once()

    def test_only_the_verified_openclaw_envelope_is_retryable(self):
        real = OPENCLAW_PROVIDER_REJECTED_STDOUT
        self.assertTrue(adapters.provider_rejected_without_output(1, real))
        self.assertTrue(adapters.provider_rejected_without_output(1, json.dumps(json.loads(real))))
        rejected = json.loads(real)
        audit_raw = ('400 [400]: {"error":{"type":"server_error",'
                     '"message":"Upstream request failed: Model is unavailable."}}')
        for returncode, stdout in (
                (0, real), (2, real), (1, ""), (1, "invalid"),
                # The audit raw text and older fabricated forms are not what OpenClaw prints.
                (1, audit_raw), (1, "400 Model is unavailable"),
                (1, '{"payloads":[{"text":"400 Model is unavailable","isError":true}]}'),
                (1, json.dumps({**rejected, "payloads": [{"text": "{}"}]})),
                (1, json.dumps({"ok": False, "error": {"type": "gateway_transport_error",
                                                       "message": rejected["error"]["message"]}})),
                (1, json.dumps({"ok": False, "error": {"type": "cli_error",
                                                       "message": "LLM request failed: other"}})),
                (1, json.dumps({"ok": False, "error": {**rejected["error"], "extra": 1}}))):
            self.assertFalse(adapters.provider_rejected_without_output(returncode, stdout), stdout)


class AlertTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"TELEGRAM_APPROVER_USER_ID": "12345",
                    "TELEGRAM_TARGET_CHAT_ID": "12345", "TELEGRAM_ACCOUNT_ID": "default",
                    "GMAIL_AUTHORIZED_USER_FILE": "PRIVATE", "TELEGRAM_BOT_TOKEN": "PRIVATE"}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_waits_then_fixed_short_alert_with_sanitized_environment(self):
        with (patch.object(alert, "wait_ready") as ready_call,
              patch.object(alert.subprocess, "run", return_value=Mock(returncode=0)) as run):
            self.assertEqual(alert.notify(), "daily_failure_alert_transport_succeeded")
        ready_call.assert_called_once_with("default")
        run.assert_called_once()
        self.assertNotIn("07:00", alert.ALERT_TEXT)
        self.assertNotIn("PRIVATE", str(run.call_args))
        self.assertEqual(run.call_args.kwargs["stdout"], subprocess.DEVNULL)

    def test_alert_retries_bounded_but_gateway_timeout_does_not_send(self):
        with (patch.object(alert, "wait_ready"), patch.object(alert.time, "sleep"),
              patch.object(alert.subprocess, "run", side_effect=TimeoutError("PRIVATE")) as run):
            self.assertEqual(alert.notify(), "daily_failure_alert_delivery_failed")
        self.assertEqual(run.call_count, 3)
        with (patch.object(alert, "wait_ready", side_effect=safe.Failure(safe.Category.GATEWAY_NOT_READY)),
              patch.object(alert.subprocess, "run") as run):
            self.assertEqual(alert.notify(), "daily_failure_alert_gateway_timeout")
        run.assert_not_called()

    def test_invalid_routing_and_unknown_origin_are_safe(self):
        with patch.dict(os.environ, {"TELEGRAM_TARGET_CHAT_ID": "-12345"}), patch.object(alert, "wait_ready") as wait:
            self.assertEqual(alert.notify(), "daily_failure_alert_configuration_invalid")
        wait.assert_not_called()
        with patch.dict(os.environ, {"MONITOR_EXIT_CODE": "exited", "MONITOR_EXIT_STATUS": "PRIVATE"}):
            self.assertEqual(alert.origin_category(), safe.Category.UNKNOWN)
        with patch.dict(os.environ, {"MONITOR_EXIT_CODE": "exited", "MONITOR_EXIT_STATUS": "11"}):
            self.assertEqual(alert.origin_category(), safe.Category.GMAIL_AUTH)


class RuntimeTests(unittest.TestCase):
    def run_main(self, failure=None, report=None, completed=False):
        out = io.StringIO()
        with (patch.object(runtime.os, "name", "posix"), patch.object(runtime, "due", return_value=True),
              patch.object(runtime, "configuration", return_value=("a", "b", "1", "1", "default")),
              patch.object(runtime, "directory_from_environment"), patch.object(runtime, "Store") as store,
              patch.object(runtime, "Ledger") as ledger, patch.object(runtime, "preflight"),
              patch.object(runtime, "wait_ready"), patch.object(runtime.alert_spool, "begin", return_value="run"),
              patch.object(runtime.alert_spool, "finish"), patch.object(runtime, "gmail_from_environment") as gmail,
              redirect_stdout(out)):
            ledger.return_value.today.return_value = ("date", {"status": "completed" if completed else "pending"})
            store.return_value.report.side_effect = failure
            store.return_value.report.return_value = report or {"reported": 1, "failed": 0}
            store.return_value.report_category = safe.Category.PARTIAL
            code = runtime.main()
            if completed:
                gmail.assert_not_called()
        return code, out.getvalue()

    def test_completed_and_already_completed_are_success(self):
        self.assertEqual(self.run_main()[0], 0)
        self.assertIn("ALREADY_COMPLETED", self.run_main(completed=True)[1])

    def test_categories_and_journal_are_bounded(self):
        for category in safe.Category:
            code, output = self.run_main(failure=safe.Failure(category))
            self.assertEqual(code, int(category))
            self.assertEqual(json.loads(output), {"code": int(category), "category": category.name})
        code, output = self.run_main(failure=RuntimeError("PRIVATE-email-token"))
        self.assertEqual(code, 8)
        self.assertNotIn("PRIVATE", output)
        self.assertEqual(self.run_main(report={"reported": 1, "failed": 1})[0], 16)

    def test_before_seven_does_not_even_construct_gmail_or_store(self):
        with (patch.object(runtime.os, "name", "posix"), patch.object(runtime, "due", return_value=False),
              patch.object(runtime, "Store") as store, patch.object(runtime, "gmail_from_environment") as gmail,
              redirect_stdout(io.StringIO())):
            self.assertEqual(runtime.main(), 0)
        store.assert_not_called()
        gmail.assert_not_called()


class IsolationTests(unittest.TestCase):
    def test_alert_dependency_tree_contains_only_stdlib_safety_modules(self):
        root = Path(__file__).resolve().parents[1]
        allowed = set(sys.stdlib_module_names) | {"agent_reliability", "alert_spool"}
        for name in ("notify_daily_report_failure.py", "agent_reliability.py", "alert_spool.py",
                     "check_email_agent_health.py"):
            tree = ast.parse((root / "scripts" / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    self.assertTrue(all(alias.name.split(".")[0] in allowed for alias in node.names))
                elif isinstance(node, ast.ImportFrom):
                    self.assertIn(node.module.split(".")[0], allowed)

    def test_monitoring_imports_without_gmail_or_model_dependencies(self):
        code = '''
import sys
sys.path.insert(0, sys.argv[1])
for name in ('daily_sender_digest','run_daily_email_report','run_email_agent',
             'new_email_pipeline','realtime_email_adapters','send_gmail_reply',
             'telegram_reply_approval','google','requests'):
    sys.modules[name] = None
import notify_daily_report_failure, check_email_agent_health
'''
        result = subprocess.run([sys.executable, "-c", code, str(Path(runtime.__file__).parent)],
                                capture_output=True, text=True, timeout=10, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout + result.stderr, "")

    def test_systemd_failure_routing_and_timeouts_do_not_loop_or_mask_errors(self):
        root = Path(__file__).resolve().parents[1] / "deploy/systemd"
        def unit(name):
            value = configparser.ConfigParser(interpolation=None)
            value.read(root / name)
            return value
        daily = unit("email-agent.service")
        self.assertEqual(daily["Unit"]["OnFailure"], "email-agent-failure-alert.service")
        self.assertNotIn("SuccessExitStatus", daily["Service"])
        self.assertEqual(daily["Service"]["TimeoutStartSec"], "infinity")
        for name in ("email-agent-failure-alert.service", "email-agent-alert-replay.service"):
            value = unit(name)
            self.assertNotIn("OnFailure", value["Unit"])
            self.assertEqual(value["Service"]["Restart"], "no")
            self.assertEqual(value["Service"]["TimeoutStartSec"], "400s")
            self.assertNotIn("ExecStartPost", value["Service"])
            self.assertNotIn("reset-failed", value["Service"]["ExecStart"])
        timer = unit("email-agent.timer")["Timer"]
        self.assertEqual(timer["OnCalendar"], "*-*-* 07:00:00 Asia/Bangkok")
        self.assertEqual(timer["Persistent"], "true")
        self.assertEqual(timer["OnStartupSec"], "90s")
