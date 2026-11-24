#!/usr/bin/env python3
"""Private stdin/stdout bridge for the OpenClaw interactive plugin; never journal stdout."""
import json
import logging
import os
import sys

from new_email_pipeline import realtime_directory, require
from run_email_agent import parse_json, pipeline_lock
from telegram_reply_approval import Interactions, configuration, suggest


def main():
    logging.disable(logging.CRITICAL)
    try:
        require(os.name == "posix", "linux_required")
        require(os.environ.get("EMAIL_AGENT_REPLY_APPROVAL") == "true", "reply_approval_disabled")
        scope = configuration()
        event = parse_json(sys.stdin.read(4097))
        # Credentials/API clients are not even loaded for unauthenticated callbacks.
        class LazyGmail:
            client = None

            def load(self):
                if self.client is None:
                    from realtime_email_adapters import gmail_from_environment
                    self.client, self.secrets = gmail_from_environment()
                return self.client

            def get(self, message_id):
                return self.load().get(message_id)

            def profile(self):
                return self.load().profile()

        gmail = LazyGmail()
        def model(email):
            from new_email_pipeline import redact
            safe = {key: redact(value, gmail.secrets) for key, value in email.items()}
            values = suggest(safe)
            require(all(redact(value, gmail.secrets) == value for value in values), "reply_secret_rejected")
            return values

        store = Interactions(realtime_directory() / "interactions", scope)
        with pipeline_lock(store.directory):
            response = store.handle(event, scope, gmail, model=model)
        # This pipe contains the preview. Parent must capture it, never log it.
        print(json.dumps(response, ensure_ascii=False))
        return 0
    except Exception:
        # May have crossed the send fence: never assert nothing was sent here.
        print(json.dumps({"text": "Không thể xử lý yêu cầu. Nếu vừa xác nhận gửi, kiểm tra Gmail thủ công; không tự thử gửi lại.",
                          "buttons": []}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
