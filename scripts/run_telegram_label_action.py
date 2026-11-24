#!/usr/bin/env python3
"""Private plugin pipe. Never log email data or exception/credential contents."""
import json
import logging
import os
import sys

from new_email_pipeline import realtime_directory, redact, require
from run_email_agent import parse_json, pipeline_lock
from telegram_label_approval import Interactions, allowlist, configuration, suggest


def main():
    logging.disable(logging.CRITICAL)
    try:
        require(os.name == "posix", "linux_required")
        require(os.environ.get("EMAIL_AGENT_LABEL_APPROVAL") == "true", "label_approval_disabled")
        scope = configuration()
        labels = allowlist()
        event = parse_json(sys.stdin.read(4097))
        class LazyReader:
            client = None
            secrets = ()

            def load(self):
                if self.client is None:
                    from realtime_email_adapters import gmail_from_environment
                    self.client, self.secrets = gmail_from_environment()
                return self.client

            def profile(self):
                return self.load().profile()

            def get(self, message_id):
                return self.load().get(message_id)

        gmail = LazyReader()
        def model(email, names):
            safe = {key: redact(value, gmail.secrets) for key, value in email.items()}
            return suggest(safe, names)

        store = Interactions(realtime_directory() / "label-interactions", scope, labels)
        with pipeline_lock(store.directory):
            response = store.handle(event, scope, gmail, model=model)
        print(json.dumps(response, ensure_ascii=False))
        return 0
    except Exception:
        print(json.dumps({"text": "Không thể xử lý yêu cầu. Nếu vừa xác nhận gắn nhãn, kiểm tra Gmail thủ công; không tự thử lại.",
                          "buttons": []}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
