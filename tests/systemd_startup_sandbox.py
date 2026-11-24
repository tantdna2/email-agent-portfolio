"""CI-only, isolated fake services; never run against the operator's manager.

Exercises real systemd OnStartupSec+OnCalendar+Persistent. Fake clock/Gmail,
private network namespace, unique temporary units, no production configuration.
"""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]


def command(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=30)


def main():
    if os.environ.get("CI") != "true" or os.environ.get("EMAIL_AGENT_SYSTEMD_SANDBOX") != "1":
        raise SystemExit("CI sandbox only")
    if not shutil.which("systemd-analyze"):
        raise SystemExit("systemd-analyze unavailable")
    with tempfile.TemporaryDirectory(prefix="task020-systemd-") as temporary:
        root = Path(temporary)
        render = root / "render"
        render.mkdir()
        names = ["email-agent.service", "email-agent.timer", "email-agent-failure-alert.service",
                 "email-agent-alert-replay.service", "email-agent-alert-replay.timer"]
        for name in names:
            content = (ROOT / "deploy/systemd" / name).read_text()
            (render / name).write_text(content.replace("@REPO_ROOT@", str(ROOT)).replace("@PYTHON@", sys.executable))
        result = command("systemd-analyze", "--user", "verify", *(str(render / name) for name in names))
        if result.stderr:
            print(result.stderr)
        print("unit verification PASS")
        fake = root / "fake.py"
        fake.write_text('''
import contextlib, io, json, os, sys, time
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv[1] + '/scripts')
from daily_sender_digest import Store
from run_daily_email_report import main
root = Path(sys.argv[2])
mode = sys.argv[3]
count = root / 'invocations'
n = int(count.read_text()) if count.exists() else 0
count.write_text(str(n+1))
# Real timer triggers, fake Bangkok clock (1792022400 = 07:00:00 Bangkok).
# before: first invocation at 06:50 (outside the 120s boundary wait), later 07:00.
# boundary: first invocation at 06:59:58 and the fake clock advances with real
# time, so the runner waits 2s for 07:00 instead of exiting NOT_DUE (F-13).
# persistent: catch-up starts after 07:00.
SEVEN = 1792022400
started = time.monotonic()
if mode == 'before':
    base = SEVEN - 600 if time.time() < float(sys.argv[4]) else SEVEN
elif mode == 'boundary':
    base = SEVEN - 2 if n == 0 else SEVEN
else:
    base = SEVEN
def stamp():
    return base + (time.monotonic() - started) if mode == 'boundary' else base
class Gmail:
    def profile(self): return {'emailAddress':'operator@example.com'}
    def request(self, *args, **kwargs): return {'messages':[]}
def notify(view):
    with (root / 'deliveries').open('a') as output: output.write('fixed-no-mail\\n')
scope = ('operator@example.com','sender@example.com','12345','12345','default')
with patch('run_daily_email_report.configuration', return_value=scope), \\
     patch('run_daily_email_report.directory_from_environment', return_value=root/'daily'), \\
     patch('run_daily_email_report.preflight'), \\
     patch('run_daily_email_report.wait_ready'), \\
     patch('run_daily_email_report.gmail_from_environment', return_value=Gmail()), \\
     patch('run_daily_email_report.notify_telegram', side_effect=notify), \\
     patch('run_daily_email_report.time.time', side_effect=stamp), \\
     patch('run_daily_email_report.alert_spool.begin', return_value='fake'), \\
     patch('run_daily_email_report.alert_spool.finish'):
    # Store's default clock is bound at import, so inject it explicitly.
    with patch('run_daily_email_report.Store', side_effect=lambda path, config: Store(path, config, now=stamp)):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main()
        with (root / 'outputs').open('a') as output:
            output.write(json.loads(out.getvalue())['category'] + '\\n')
        raise SystemExit(code)
''')
        for mode in ("before", "boundary", "persistent"):
            prefix = "task020-fake-" + uuid.uuid4().hex
            service, timer = prefix + ".service", prefix + ".timer"
            state = root / mode
            state.mkdir(mode=0o700)
            # calendar future for before-07:00/boundary; missed calendar for catch-up.
            # OnCalendar has whole-second precision: truncate so the fake's
            # before/after decision matches the instant the timer actually fires.
            target = (datetime.now(timezone.utc) + timedelta(
                seconds={"before": 5, "boundary": 3}.get(mode, -2))).replace(microsecond=0)
            calendar = target.strftime("*-*-* %H:%M:%S UTC")
            unit_service = root / service
            unit_timer = root / timer
            unit_service.write_text(f'''[Service]
Type=oneshot
User={os.getuid()}
ExecStart={sys.executable} {fake} {ROOT} {state} {mode} {target.timestamp()}
Environment=EMAIL_AGENT_AUTO_SEND=false
PrivateNetwork=yes
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths={root}
UMask=0077
''')
            unit_timer.write_text(f'''[Timer]
OnStartupSec=1s
OnCalendar={calendar}
Persistent=true
AccuracySec=1us
Unit={service}
''')
            stamp = Path("/var/lib/systemd/timers") / ("stamp-" + timer)
            linked = False
            try:
                command("sudo", "systemctl", "link", "--runtime", str(unit_service), str(unit_timer))
                linked = True
                command("sudo", "touch", str(stamp))
                command("sudo", "touch", "-d", "2 days ago", str(stamp))
                command("sudo", "systemctl", "start", timer)
                time.sleep(8)  # short real timer interval in disposable CI only
                before = int((state / "invocations").read_text())
                outputs = (state / "outputs").read_text().splitlines()
                if mode == "before":
                    assert before >= 2, "startup must leave future calendar trigger armed"
                    assert outputs[0] == "NOT_DUE" and "COMPLETED" in outputs, outputs
                elif mode == "boundary":
                    # The startup run straddling 07:00 owns the report; a calendar
                    # trigger merged into it or run afterwards must not duplicate it.
                    assert outputs[0] == "COMPLETED", outputs
                    assert set(outputs[1:]) <= {"ALREADY_COMPLETED"}, outputs
                else:
                    assert before >= 1, "missed persistent/startup triggers must catch up"
                # Reopening/restarting in the same day must not duplicate output.
                for _ in range(2):
                    command("sudo", "systemctl", "start", service)
                assert (state / "deliveries").read_text().splitlines() == ["fixed-no-mail"]
                result = command("sudo", "systemctl", "show", service, "-p", "Result", "-p", "ExecMainStatus")
                assert "Result=success" in result.stdout and "ExecMainStatus=0" in result.stdout
                print(json.dumps({"scenario": mode, "timer_invocations": before, "deliveries": 1, "result": "PASS"}))
            finally:
                if linked:
                    command("sudo", "systemctl", "stop", timer, service)
                    command("sudo", "systemctl", "clean", "--what=state", timer)
                    command("sudo", "systemctl", "disable", "--runtime", timer, service)
                    command("sudo", "systemctl", "daemon-reload")


if __name__ == "__main__":
    main()
