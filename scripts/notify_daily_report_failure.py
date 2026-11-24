#!/usr/bin/env python3
"""Failure/replay handler: stdlib-only dependencies, no Gmail/application imports."""
import json
import os
import re
import subprocess
import sys
import time

from agent_reliability import Category, transport_environment, wait_ready
import alert_spool


ALERT_TEXT = ("Email Agent: Báo cáo Gmail chưa hoàn tất. Kiểm tra systemd journal và checkpoint; "
              "không tự gửi lại email khi kết quả gửi chưa rõ.")
TRANSPORT_TIMEOUT = 60


def origin_category():
    try:
        if os.environ.get("MONITOR_EXIT_CODE") == "exited":
            return Category(int(os.environ.get("MONITOR_EXIT_STATUS", "")))
    except (ValueError, TypeError):
        pass
    return Category.UNKNOWN


def alert_text(category, previous=None):
    text = ALERT_TEXT + " Mã: " + category.name + "."
    if previous is not None and previous != category:
        text += " Lỗi trước chưa báo: " + previous.name + "."
    return text


def notify(category=Category.UNKNOWN, previous=None):
    user = os.environ.get("TELEGRAM_APPROVER_USER_ID", "")
    chat = os.environ.get("TELEGRAM_TARGET_CHAT_ID", "")
    account = os.environ.get("TELEGRAM_ACCOUNT_ID", "default")
    if (not re.fullmatch(r"[1-9][0-9]*", user) or chat != user
            or not re.fullmatch(r"[a-zA-Z0-9_-]+", account)):
        return "daily_failure_alert_configuration_invalid"
    try:
        wait_ready(account)
    except Exception:
        return "daily_failure_alert_gateway_timeout"
    command = ["openclaw", "message", "send", "--channel", "telegram", "--account", account,
               "--target", chat, "--message", alert_text(category, previous)]
    # Retries apply only to the fixed operational alert. Lost acknowledgments may
    # duplicate that alert. Reports and Gmail send do not use this loop.
    for attempt in range(3):
        try:
            result = subprocess.run(command, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    env=transport_environment(), timeout=TRANSPORT_TIMEOUT, check=False)
            if result.returncode == 0:
                return "daily_failure_alert_transport_succeeded"
        except Exception:
            pass
        if attempt < 2:
            time.sleep((2, 5)[attempt])
    return "daily_failure_alert_delivery_failed"


def main():
    try:
        # Short claim under alert.lock; the Gateway wait and Telegram transport
        # run WITHOUT the lock, so daily runs and other handlers never block on it.
        claimed = alert_spool.claim("--replay-only" in sys.argv, origin_category())
        if claimed is None:
            status = "daily_failure_alert_nothing_pending"
        else:
            alert_id, owner, category, previous = claimed
            status = notify(category, previous)
            alert_spool.complete(alert_id, owner, status == "daily_failure_alert_transport_succeeded")
    except Exception:
        status = "daily_failure_alert_state_failed"
    print(json.dumps({"status": status}), flush=True)
    return 0 if status in {"daily_failure_alert_transport_succeeded", "daily_failure_alert_nothing_pending"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
