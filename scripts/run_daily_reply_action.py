#!/usr/bin/env python3
"""Private native OpenClaw plugin worker. stdin only, sanitized stdout only."""
from contextlib import ExitStack
import json
import os
import sys

from daily_sender_digest import BUSY, Store, configuration, directory_from_environment, gmail_from_environment
from new_email_pipeline import require
from read_latest_email_from_sender import PipelineError
from run_daily_email_report import notify_telegram
from run_email_agent import parse_json
from revision_diagnostics import RevisionFailure, safe_revision_code


class LazyGmail:
    """Skip, Edit and stale callbacks need neither credentials nor Gmail IO."""
    def __init__(self):
        self.client = None

    def _client(self):
        if self.client is None:
            self.client = gmail_from_environment()
        return self.client

    def profile(self):
        return self._client().profile()

    def get(self, message_id):
        return self._client().get(message_id)


def main():
    try:
        require(os.name == "posix", "linux_required")
        raw = sys.stdin.read(16385)
        require(len(raw) <= 16384, "event_invalid")
        event = parse_json(raw)
        store = Store(directory_from_environment(), configuration())
        store.authorized(event)
        try:
            lock = store.lock()
            lock.__enter__()
        except PipelineError as error:
            if str(error) != "locked_skip":
                raise
            # Busy is distinct from "no session": nothing was read or applied.
            print(json.dumps({"text": BUSY, "buttons": [], "session": "busy"}, ensure_ascii=False))
            return 0
        with ExitStack() as held:
            held.push(lock)
            if event.get("kind") == "edit_session":
                response = store.edit_session(event)
            elif event.get("kind") == "callback":
                response = store.callback(event, LazyGmail())
            elif event.get("kind") == "text":
                require(isinstance(event.get("edit_token"), str), "edit_session_required")
                response = store.revise(event, LazyGmail())
                # Buttons go through the existing OpenClaw outbound CLI because
                # before_dispatch's native reply contract is text-only.
                notify_telegram(response)
                response = {"text": "", "buttons": []}
            else:
                require(False, "event_invalid")
        print(json.dumps(response, ensure_ascii=False))
        return 0
    except RevisionFailure as error:
        print(json.dumps({"text": "Không thể xử lý chỉnh sửa. Mã: " + safe_revision_code(error)
                          + ". Cần kiểm tra thủ công; không tự thử lại.", "buttons": []}, ensure_ascii=False))
        return 0
    except Exception:
        # A broken pipe/timeout must not disclose model/email/credential errors.
        print(json.dumps({"text": "Không thể xử lý. Hãy dùng nút mới nhất hoặc kiểm tra thủ công; không tự gửi lại.",
                          "buttons": []}, ensure_ascii=False))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
