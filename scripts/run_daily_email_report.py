#!/usr/bin/env python3
"""TASK-016 daily runtime. No Gmail send path; callbacks own explicit approval."""
import json
import os
import time

from daily_sender_digest import Store, configuration, directory_from_environment, gmail_from_environment
from new_email_pipeline import require
from run_email_agent import captured
from agent_reliability import (Category, Failure, category_of, due, seconds_until_due,
                               stage, wait_ready)
from daily_ledger import ATTENTION, Ledger
import alert_spool


def preflight():
    from daily_sender_digest import secure_path
    from realtime_email_adapters import load_authorized_user_file
    from run_email_agent import model_environment
    scope = configuration()
    secure_path(os.environ.get("GMAIL_AUTHORIZED_USER_FILE", ""))
    load_authorized_user_file()
    for key in ("OPENCLAW_CONFIG_PATH", "OPENCLAW_STATE_DIR"):
        secure_path(os.environ.get(key, ""), directory=key.endswith("DIR"))
    model_environment()  # validate deny-all tools; no model invocation
    return scope


def notify_telegram(view):
    _, _, _, chat, bot = configuration()
    # OpenClaw owns outbound transport and credentials. No second receiver.
    env = {key: value for key, value in os.environ.items()
           if key in {"HOME", "PATH", "LANG", "LC_ALL", "XDG_CONFIG_HOME"}}
    env.update(OPENCLAW_LOG_LEVEL="silent", OPENCLAW_RAW_STREAM="0")
    command = ["openclaw", "message", "send", "--channel", "telegram", "--account", bot,
               "--target", chat, "--message", view["text"]]
    if view["buttons"]:
        blocks = [{"type": "buttons", "buttons": [
            {"label": b["text"], "action": {"type": "callback", "value": b["callback_data"]}}
            for b in row]} for row in view["buttons"]]
        command += ["--presentation", json.dumps({"blocks": blocks}, ensure_ascii=False)]
    result = captured(command, timeout=60, env=env)
    require(result.returncode == 0, "telegram_send_failed")


# systemd merges a calendar trigger into a start job that is still activating.
# A startup run that begins just before 07:00 therefore owns the 07:00 report:
# it waits for the boundary instead of exiting NOT_DUE and swallowing it.
BOUNDARY_WAIT = 120


def emit(category, spool):
    value = {"code": 0 if isinstance(category, str) else int(category),
             "category": category if isinstance(category, str) else category.name}
    if spool:
        value["alert_spool"] = "unavailable"  # report continues; journal keeps the evidence
    print(json.dumps(value))


def main(clock=None, sleep=None):
    clock, sleep = clock or time.time, sleep or time.sleep
    token, spool_failed = None, False
    try:
        with stage(Category.CONFIG):
            require(os.name == "posix", "linux_required")
        if not due(clock()):
            wait = seconds_until_due(clock())
            if wait > BOUNDARY_WAIT:
                emit("NOT_DUE", False)
                return 0
            sleep(wait)
            if not due(clock()):
                emit("NOT_DUE", False)
                return 0
        with stage(Category.CONFIG):
            scope = configuration()
        with stage(Category.STATE):
            store = Store(directory_from_environment(), scope)
        with stage(Category.STATE):
            lock = store.lock(timeout=120)
        with lock:
            ledger = Ledger(store)
            date, day = ledger.today()
            if day["status"] == "completed" or day["status"] in ATTENTION:
                # No automatic work remains; the run that found the problem
                # already exited non-zero. Silent, never COMPLETED for attention.
                with stage(Category.STATE):
                    state = store.load()
                    store.recover(state)
                    if day["status"] == "completed":
                        ledger.verify_completed(date, day, state)
                    else:
                        ledger.verify_attention(date, day, state)
                emit("ALREADY_COMPLETED" if day["status"] == "completed" else "ATTENTION_PENDING", False)
                return 0
            try:
                token = alert_spool.begin()
            except Exception:
                # The alert handoff is evidence, not a gate: OnFailure still runs.
                spool_failed = True
            with stage(Category.CONFIG):
                preflight()
            wait_ready(scope[4])
            with stage(Category.CONFIG):
                gmail = gmail_from_environment()
            result = store.report(gmail, notify_telegram)
            if result["failed"]:
                raise Failure(store.report_category or Category.PARTIAL)
            if token:
                try:
                    alert_spool.finish(token)
                except Exception:
                    spool_failed = True
        emit("ATTENTION_PENDING" if result.get("attention") else "COMPLETED", spool_failed)
        return 0
    except Exception as error:
        category = category_of(error)
        if token:
            try:
                alert_spool.finish(token, category)
            except Exception:
                spool_failed = True
        emit(category, spool_failed)
        return int(category)


if __name__ == "__main__":
    raise SystemExit(main())
