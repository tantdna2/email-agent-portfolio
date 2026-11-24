#!/usr/bin/env python3
"""Replay the daily reply approval flow with local fixtures and fake integrations."""

from base64 import urlsafe_b64encode
from datetime import datetime, timezone
from email.utils import format_datetime
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

from daily_sender_digest import STALE, TEXT, Store


ACCOUNT = "operator@example.com"
SENDER = "maya@example.com"
SCOPE = (ACCOUNT, SENDER, "1001", "1001", "demo")
OPERATOR = {"channel": "telegram", "account": SCOPE[4], "user": SCOPE[2],
            "chat": SCOPE[3], "private": True, "authorized": True}
MESSAGE_ID = "a1"
RECEIVED = datetime(2025, 1, 15, 9, tzinfo=timezone.utc)
SUBJECT = "Workshop slides"
BODY = "Hi, could you confirm you received the workshop slides? Thanks, Maya"
INSTRUCTION = "Make the reply shorter."
INITIAL = "Hi Maya,\n\nThanks for sending the workshop slides. I confirm that I received them."
REVISED = "Hi Maya, thanks for the slides. I received them."


class SampleInbox:
    def profile(self):
        return {"emailAddress": ACCOUNT}

    def request(self, method, path, *, params):
        if (method, path) != ("GET", "messages") or "pageToken" in params:
            raise ValueError("Unexpected inbox request")
        return {"messages": [{"id": MESSAGE_ID}]}

    def get(self, message_id):
        if message_id != MESSAGE_ID:
            raise KeyError(message_id)
        return {
            "id": MESSAGE_ID, "threadId": "fa1", "labelIds": ["INBOX"],
            "internalDate": str(int(RECEIVED.timestamp()) * 1000),
            "payload": {
                "mimeType": "text/plain",
                "headers": [{"name": name, "value": value} for name, value in (
                    ("From", SENDER), ("To", ACCOUNT), ("Subject", SUBJECT),
                    ("Date", format_datetime(RECEIVED)),
                    ("Message-ID", f"<{MESSAGE_ID}@example.com>"),
                )],
                "body": {"data": urlsafe_b64encode(BODY.encode()).decode("ascii")},
            },
        }


def fake_model(email, current=None, instruction=None):
    if email["body"] != BODY:
        raise ValueError("Unexpected source email")
    if current is None:
        return {"summary": "Maya asks for confirmation that the slides arrived.",
                "draft_body": INITIAL}
    if (current, instruction) != (INITIAL, INSTRUCTION):
        raise ValueError("Unexpected revision request")
    return REVISED


def check(condition, message):
    if not condition:
        raise RuntimeError(message)


def replay(store):
    inbox = SampleInbox()
    notifications = []
    outbox = []

    def fake_send(draft, account):
        outbox.append((draft, account))
        return "sent"

    def click(button):
        with store.lock():
            return store.callback({**OPERATOR, "data": button["callback_data"]}, inbox, fake_send)

    def show_count(action, expected):
        print(f"{action}. Simulated sends: {len(outbox)}")
        check(len(outbox) == expected, f"Expected {expected} simulated sends")

    print(f"Sample inbox\nFrom: {SENDER}\nSubject: {SUBJECT}\n\n{BODY}")
    with store.lock():
        report = store.report(inbox, notifications.append, generate=fake_model)
    check(report == {"reported": 1, "failed": 0, "attention": 0}, "Expected one reported draft")
    initial, = (view for view in notifications if view["buttons"])
    old_approve, edit, _ = initial["buttons"][0]
    print(f"\nDraft v1\n{initial['text']}")

    print(f"\nClick {edit['text']} and submit: {INSTRUCTION}")
    click(edit)
    with store.lock():
        session = store.edit_session(OPERATOR)
        revised = store.revise({**OPERATOR, "message_id": "1", "text": INSTRUCTION,
                                "edit_token": session["edit_token"]}, inbox, generate=fake_model)
    check(revised["text"].endswith(REVISED), "Expected the revised draft")
    new_approve, _, _ = revised["buttons"][0]
    print(f"\nDraft v2\n{revised['text']}\n")

    stale = click(old_approve)
    show_count("Click v1 approval after editing", 0)
    check(stale["text"] == STALE, "Expected the old approval to be rejected as stale")
    first = click(new_approve)
    show_count("Click v2 approval", 1)
    check(first["text"] == TEXT["sent"], "Expected the approved draft to be marked sent")
    replayed = click(new_approve)
    show_count("Click v2 approval again", 1)
    check(replayed == first, "Expected the replay to return the existing sent status")
    (sent, account), = outbox
    check((sent["draft_body"], sent["to"], account) == (REVISED, SENDER, ACCOUNT),
          "Expected only the revised reply to reach the simulated outbox")
    print("PASS: stale approval rejected, revised reply sent exactly once in the simulation.")


def main():
    if sys.platform != "linux":
        sys.exit("Run this demo under Linux or WSL to use the daily digest's file locking and state storage.")
    # Use a fresh Linux filesystem directory even when the checkout is on a Windows drive.
    os.environ["EMAIL_AGENT_INITIAL_LOOKBACK_HOURS"] = "24"
    with TemporaryDirectory(prefix="email-agent-demo-", dir="/tmp") as directory:
        store = Store(Path(directory).resolve(), SCOPE, now=lambda: RECEIVED.timestamp() + 60)
        replay(store)


if __name__ == "__main__":
    main()
