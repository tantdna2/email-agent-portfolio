"""Offline monitoring tests: all Telegram and systemctl commands are mocked."""
import ast
import configparser
from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import check_email_agent_health as health
import notify_daily_report_failure as alert
import run_daily_email_report as runtime

PRIVATE = "PRIVATE-email@example.com-token-body-exception"
ROUTING = {"TELEGRAM_APPROVER_USER_ID": "12345", "TELEGRAM_TARGET_CHAT_ID": "12345",
           "TELEGRAM_ACCOUNT_ID": "default"}


def unit(name):
    config = configparser.ConfigParser(interpolation=None)
    config.read(ROOT / "deploy/systemd" / name, encoding="utf-8")
    return config


def ready_health():
    return json.dumps({"ok": True, "channels": {"telegram": {"accounts": {"default": {
        "accountId": "default", "enabled": True, "configured": True, "running": True, "connected": True}}}}})


class Calls:
    """Split the single subprocess.run mock into health probes and sends."""
    def __init__(self, mock, health):
        self.call_args_list = [c for c in mock.call_args_list if (c.args[0][1] == "health") == health]

    def assert_not_called(self):
        assert not self.call_args_list, self.call_args_list


def alert_main(env, run, *args):
    stdout, stderr = io.StringIO(), io.StringIO()
    # notify_daily_report_failure and agent_reliability share one subprocess module.
    with (patch.dict(os.environ, env, clear=True), patch.object(alert.sys, "argv", ["h", *args]),
          patch.object(alert.subprocess, "run", side_effect=run) as child,
          patch.object(alert.time, "sleep"), patch("agent_reliability.time.sleep"),
          redirect_stdout(stdout), redirect_stderr(stderr)):
        code = alert.main()
    return code, stdout.getvalue(), stderr.getvalue(), Calls(child, False), Calls(child, True)


@unittest.skipUnless(os.name == "posix", "durable alert slot and /proc boot id")
class FailureAlertTests(unittest.TestCase):
    """TASK-019 alert guarantees, restored against the TASK-020 handler."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.monitor = Path(self.temp.name) / "monitor"
        self.daily = Path(self.temp.name) / "daily"
        self.daily.mkdir(mode=0o700)
        self.env = {**ROUTING, "EMAIL_AGENT_MONITOR_STATE_DIR": str(self.monitor),
                    "HOME": "/synthetic/home", "PATH": "/synthetic/bin"}

    @staticmethod
    def fake(send_code=0):
        def run(command, **kwargs):
            if command[:2] == ["openclaw", "health"]:
                return subprocess.CompletedProcess(command, 0, ready_health(), "")
            if isinstance(send_code, Exception):
                raise send_code
            return subprocess.CompletedProcess(command, send_code)
        return run

    def test_fixed_transport_call_and_secret_free_child_environment(self):
        env = {**self.env, "GOG_ACCOUNT": PRIVATE, "TARGET_SENDER": PRIVATE,
               "GMAIL_AUTHORIZED_USER_FILE": PRIVATE, "GOG_KEYRING_PASSWORD": PRIVATE,
               "GOOGLE_APPLICATION_CREDENTIALS": PRIVATE, "TELEGRAM_BOT_TOKEN": PRIVATE,
               "OPENCLAW_CONFIG_PATH": PRIVATE, "OPENCLAW_STATE_DIR": PRIVATE,
               "EMAIL_AGENT_DAILY_STATE_DIR": PRIVATE, "MONITOR_SERVICE_RESULT": PRIVATE}
        code, stdout, stderr, transport, health = alert_main(env, self.fake())
        self.assertEqual(code, 0)
        send = [c for c in transport.call_args_list if c.args[0][:3] == ["openclaw", "message", "send"]]
        self.assertEqual(len(send), 1)
        self.assertEqual(send[0].args[0], ["openclaw", "message", "send", "--channel", "telegram",
                                           "--account", "default", "--target", "12345", "--message",
                                           alert.alert_text(alert.Category.UNKNOWN)])
        for call in transport.call_args_list + health.call_args_list:
            self.assertEqual(call.kwargs["env"], {"HOME": "/synthetic/home", "PATH": "/synthetic/bin",
                                                  "OPENCLAW_LOG_LEVEL": "silent", "OPENCLAW_RAW_STREAM": "0"})
            self.assertEqual(call.kwargs["stdin"], subprocess.DEVNULL)
            self.assertFalse(call.kwargs["check"])
        self.assertEqual(send[0].kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(send[0].kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(send[0].kwargs["timeout"], 60)
        self.assertEqual(json.loads(stdout), {"status": "daily_failure_alert_transport_succeeded"})
        self.assertEqual(stderr, "")
        for sensitive in (PRIVATE, "12345"):
            self.assertNotIn(sensitive, stdout + stderr + alert.ALERT_TEXT)

    def test_nonzero_timeout_and_missing_cli_are_bounded_and_log_safe(self):
        for result in (1, subprocess.TimeoutExpired(PRIVATE, 60, output=PRIVATE, stderr=PRIVATE),
                       FileNotFoundError(PRIVATE), RuntimeError(PRIVATE)):
            with self.subTest(result=type(result).__name__):
                code, stdout, stderr, transport, _ = alert_main(self.env, self.fake(result))
                self.assertEqual(code, 1)
                self.assertEqual(json.loads(stdout), {"status": "daily_failure_alert_delivery_failed"})
                self.assertEqual(stderr, "")
                self.assertNotIn(PRIVATE, stdout + stderr)
                sends = [c for c in transport.call_args_list if c.args[0][1:3] == ["message", "send"]]
                self.assertEqual(len(sends), 3)  # bounded fixed-alert retries only

    def test_missing_or_invalid_private_routing_fails_before_gateway_or_transport(self):
        for changes in ({"TELEGRAM_APPROVER_USER_ID": ""}, {"TELEGRAM_TARGET_CHAT_ID": "-12345"},
                        {"TELEGRAM_TARGET_CHAT_ID": "67890"}, {"TELEGRAM_APPROVER_USER_ID": PRIVATE},
                        {"TELEGRAM_ACCOUNT_ID": "bad/account"}, {"TELEGRAM_ACCOUNT_ID": PRIVATE}):
            with self.subTest(changes=changes):
                code, stdout, _, transport, health = alert_main({**self.env, **changes}, self.fake())
                self.assertEqual(code, 1)
                transport.assert_not_called()
                health.assert_not_called()
                self.assertEqual(json.loads(stdout), {"status": "daily_failure_alert_configuration_invalid"})
                self.assertNotIn(PRIVATE, stdout)

    def test_alert_never_reads_or_writes_daily_email_state(self):
        state = self.daily / "state.json"
        original = b'{"schema":1,"records":{"synthetic":{"status":"send_uncertain"}}}'
        state.write_bytes(original)
        env = {**self.env, "EMAIL_AGENT_DAILY_STATE_DIR": str(self.daily)}
        real_open = open
        def guarded(path, *args, **kwargs):
            if str(self.daily) in str(path):
                raise AssertionError("daily state access forbidden")
            return real_open(path, *args, **kwargs)
        with patch("builtins.open", side_effect=guarded), patch("io.open", side_effect=guarded):
            code, *_ = alert_main(env, self.fake(TimeoutError(PRIVATE)))
        self.assertEqual(code, 1)
        self.assertEqual(state.read_bytes(), original)
        self.assertEqual([p.name for p in self.daily.iterdir()], ["state.json"])
        self.assertEqual(sorted(p.name for p in self.monitor.iterdir()), ["alert.lock", "pending-alert.json"])

    def test_handler_runs_when_gmail_model_and_state_modules_are_unavailable(self):
        # A dependency/import failure in the report must not break monitoring too.
        code = """
import json, os, runpy, subprocess, sys
from unittest.mock import patch
sys.path.insert(0, os.path.dirname(sys.argv[1]))  # as `python script.py` does under systemd
for name in ('daily_sender_digest', 'run_daily_email_report', 'run_email_agent', 'daily_ledger',
             'new_email_pipeline', 'realtime_email_adapters', 'send_gmail_reply',
             'telegram_reply_approval', 'google', 'requests'):
    sys.modules[name] = None
health = json.dumps({"ok": True, "channels": {"telegram": {"accounts": {"default": {
    "configured": True, "enabled": True, "running": True, "connected": True}}}}})
def run(command, **kwargs):
    return subprocess.CompletedProcess(command, 0, health if command[1] == "health" else "", "")
with patch('subprocess.run', side_effect=run) as child:
    try:
        runpy.run_path(sys.argv[1], run_name='__main__')
    except SystemExit as result:
        assert result.code == 0, result.code
    assert [c.args[0][1] for c in child.call_args_list] == ["health", "message"], child.call_args_list
"""
        env = {key: value for key, value in os.environ.items() if key in {"HOME", "PATH"}}
        env.update(ROUTING, EMAIL_AGENT_MONITOR_STATE_DIR=str(self.monitor))
        result = subprocess.run([sys.executable, "-c", code, str(ROOT / "scripts/notify_daily_report_failure.py")],
                                capture_output=True, text=True, env=env, timeout=20, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout), {"status": "daily_failure_alert_transport_succeeded"})


class FailureRoutingTests(unittest.TestCase):
    def test_runner_never_dispatches_failure_alerts_itself(self):
        tree = ast.parse((ROOT / "scripts/run_daily_email_report.py").read_text(encoding="utf-8"))
        imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        imported |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        self.assertNotIn("notify_daily_report_failure", imported)  # systemd OnFailure owns dispatch

    def test_daily_unit_fails_into_onfailure_without_restart_or_success_masking(self):
        config = unit("email-agent.service")
        self.assertEqual(config["Unit"]["OnFailure"], "email-agent-failure-alert.service")
        self.assertEqual(config["Service"]["Type"], "oneshot")
        self.assertEqual(config["Service"]["Restart"], "no")
        self.assertNotIn("SuccessExitStatus", config["Service"])
        self.assertNotIn("RestartForceExitStatus", config["Service"])
        self.assertNotIn("OnSuccess", config["Unit"])
        self.assertNotIn("ExecStartPost", config["Service"])
        for directive in ("Requires", "BindsTo", "PartOf"):
            self.assertNotIn(directive, config["Unit"])

    def test_alert_units_cannot_restart_report_reset_failure_or_recurse(self):
        for name, args in (("email-agent-failure-alert.service", ""),
                           ("email-agent-alert-replay.service", " --replay-only")):
            with self.subTest(name=name):
                config = unit(name)
                self.assertEqual(config["Service"]["Type"], "oneshot")
                self.assertEqual(config["Service"]["Restart"], "no")
                self.assertEqual(config["Service"]["ExecStart"],
                                 "@PYTHON@ @REPO_ROOT@/scripts/notify_daily_report_failure.py" + args)
                self.assertEqual(config["Service"]["EnvironmentFile"],
                                 "-%h/.config/email-agent/email-agent-monitoring.env")
                self.assertIn("EMAIL_AGENT_AUTO_SEND=false", config["Service"]["Environment"])
                self.assertEqual(config["Service"]["TimeoutStartSec"], "400s")
                self.assertEqual(config["Service"]["StandardOutput"], "journal")
                self.assertEqual(config["Service"]["StandardError"], "journal")
                self.assertNotIn("Install", config)
                for directive in ("OnFailure", "OnSuccess", "Requires", "Wants", "BindsTo", "PartOf"):
                    self.assertNotIn(directive, config["Unit"])
                self.assertEqual({key for key in config["Service"] if key.startswith("exec")}, {"execstart"})
                self.assertNotIn("systemctl", config["Service"]["ExecStart"])
        replay = unit("email-agent-alert-replay.timer")
        self.assertEqual(replay["Timer"]["OnStartupSec"], "120s")
        self.assertEqual(replay["Timer"]["Unit"], "email-agent-alert-replay.service")
        self.assertNotIn("OnCalendar", replay["Timer"])
        self.assertNotIn("Persistent", replay["Timer"])
        self.assertEqual(replay["Install"]["WantedBy"], "timers.target")
        timer = unit("email-agent.timer")["Timer"]
        self.assertEqual(timer["OnCalendar"], "*-*-* 07:00:00 Asia/Bangkok")
        self.assertEqual(timer["Persistent"], "true")
        self.assertEqual(timer["OnStartupSec"], "90s")
        self.assertEqual(timer["Unit"], "email-agent.service")


# TASK-019 read-only health checks below remain unchanged.


def healthy_snapshot():
    return {
        health.TIMER: {"LoadState": "loaded", "ActiveState": "active", "SubState": "waiting",
                      "UnitFileState": "enabled", "LastTriggerUSec": "Fri 2026-10-09 07:00:00 +07",
                      "LastTriggerUSecMonotonic": "100"},
        health.SERVICE: {"LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead",
                        "Result": "success", "ExecMainCode": "1", "ExecMainStatus": "0",
                        "ExecMainStartTimestampMonotonic": "110", "ExecMainExitTimestampMonotonic": "150"},
        health.SUBSCRIBER: {"LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead",
                           "UnitFileState": "disabled"},
    }


class HealthCheckTests(unittest.TestCase):
    def assess(self, snapshots):
        return health.assess(*(snapshots[name] for name in (health.TIMER, health.SERVICE, health.SUBSCRIBER)))

    def test_healthy_requires_timer_latest_completed_run_and_disabled_subscriber(self):
        result = self.assess(healthy_snapshot())
        self.assertEqual(result, {"health": "healthy", "timer": {"active": "active", "enabled": "enabled"},
                                  "service": "completed", "last_trigger": "2026-10-09 07:00:00 +07",
                                  "realtime": {"active": "inactive", "enabled": "disabled"}})

    def test_known_failures_are_errors(self):
        cases = [(health.SERVICE, {"ActiveState": "failed", "Result": "exit-code"}),
                 (health.SERVICE, {"Result": "timeout"}),
                 (health.SERVICE, {"ExecMainStatus": "8"}),
                 (health.TIMER, {"ActiveState": "inactive"}),
                 (health.TIMER, {"UnitFileState": "disabled"}),
                 (health.TIMER, {"UnitFileState": "enabled-runtime"}),
                 (health.SUBSCRIBER, {"ActiveState": "active"}),
                 (health.SUBSCRIBER, {"UnitFileState": "enabled"}),
                 (health.SERVICE, {"LoadState": "not-found"})]
        for name, changes in cases:
            with self.subTest(changes=changes):
                snapshots = healthy_snapshot()
                snapshots[name].update(changes)
                self.assertEqual(self.assess(snapshots)["health"], "error")

    def test_running_service_is_observed_but_not_yet_healthy(self):
        for active, substate in (("activating", "start"), ("active", "running")):
            snapshots = healthy_snapshot()
            snapshots[health.SERVICE].update(ActiveState=active, SubState=substate, Result="success")
            result = self.assess(snapshots)
            self.assertEqual(result["service"], "running")
            self.assertEqual(result["health"], "unknown")

    def test_missing_never_run_stale_and_malformed_evidence_are_unknown(self):
        cases = [(health.SERVICE, {"ExecMainCode": "0", "ExecMainStartTimestampMonotonic": "0",
                                   "ExecMainExitTimestampMonotonic": "0"}),
                 (health.SERVICE, {"ExecMainStatus": ""}),
                 (health.SERVICE, {"ExecMainExitTimestampMonotonic": "99"}),
                 (health.SERVICE, {"ExecMainStartTimestampMonotonic": "90"}),
                 (health.TIMER, {"LastTriggerUSec": "n/a", "LastTriggerUSecMonotonic": "0"}),
                 (health.TIMER, {"LastTriggerUSec": "Fri 2026-02-30 07:00:00 +07"}),
                 (health.SUBSCRIBER, {"UnitFileState": ""}),
                 (health.SERVICE, {"LoadState": "", "ActiveState": PRIVATE})]
        for name, changes in cases:
            with self.subTest(changes=changes):
                snapshots = healthy_snapshot()
                snapshots[name].update(changes)
                self.assertEqual(self.assess(snapshots)["health"], "unknown")
        self.assertEqual(health.assess({}, {}, {})["health"], "unknown")

    def test_no_raw_systemd_values_can_leak_into_output(self):
        snapshots = healthy_snapshot()
        for values in snapshots.values():
            for key in list(values):
                values[key] = PRIVATE
            values["Environment"] = PRIVATE
        result = self.assess(snapshots)
        self.assertEqual(result["health"], "unknown")
        self.assertNotIn(PRIVATE, json.dumps(result))

    def test_systemctl_requests_only_fixed_readonly_properties_and_sanitizes_output(self):
        snapshots = healthy_snapshot()
        calls = []
        def show(command, **kwargs):
            calls.append(command)
            self.assertEqual(command[:4], ["systemctl", "--user", "--no-pager", "show"])
            self.assertEqual(command[5], "--property=" + ",".join(health.PROPERTIES[command[4]]))
            self.assertEqual(kwargs["timeout"], 5)
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            self.assertTrue(kwargs["capture_output"])
            self.assertNotIn("GOG_KEYRING_PASSWORD", kwargs["env"])
            text = "\n".join(key + "=" + value for key, value in snapshots[command[4]].items())
            return subprocess.CompletedProcess(command, 0, text + "\nEnvironment=" + PRIVATE, PRIVATE)
        stdout, stderr = io.StringIO(), io.StringIO()
        with (patch.dict(os.environ, {"GOG_KEYRING_PASSWORD": PRIVATE}),
              patch.object(health.subprocess, "run", side_effect=show),
              redirect_stdout(stdout), redirect_stderr(stderr)):
            self.assertEqual(health.main(), 0)
        self.assertEqual([command[4] for command in calls], list(health.PROPERTIES))
        self.assertEqual(json.loads(stdout.getvalue())["health"], "healthy")
        self.assertNotIn(PRIVATE, stdout.getvalue() + stderr.getvalue())

    def test_unavailable_systemd_nonzero_timeout_and_duplicate_properties_are_unknown(self):
        cases = [FileNotFoundError(PRIVATE), subprocess.TimeoutExpired(PRIVATE, 5, output=PRIVATE),
                 subprocess.CompletedProcess([], 1, PRIVATE, PRIVATE),
                 subprocess.CompletedProcess([], 0, "ActiveState=active\nActiveState=inactive", PRIVATE)]
        for result in cases:
            with self.subTest(result=type(result).__name__):
                stdout, stderr = io.StringIO(), io.StringIO()
                with (patch.object(health.subprocess, "run") as show,
                      redirect_stdout(stdout), redirect_stderr(stderr)):
                    if isinstance(result, Exception):
                        show.side_effect = result
                    else:
                        show.return_value = result
                    self.assertEqual(health.main(), 2)
                self.assertEqual(show.call_count, 3)
                self.assertEqual(json.loads(stdout.getvalue())["health"], "unknown")
                self.assertNotIn(PRIVATE, stdout.getvalue() + stderr.getvalue())

    def test_error_exit_code(self):
        snapshots = healthy_snapshot()
        snapshots[health.SERVICE]["ActiveState"] = "failed"
        with patch.object(health, "read_unit", side_effect=lambda name: copy.deepcopy(snapshots[name])), redirect_stdout(io.StringIO()):
            self.assertEqual(health.main(), 1)


if __name__ == "__main__":
    unittest.main()
