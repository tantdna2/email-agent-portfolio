"""Transport-independent new-mail queue and durable notification outbox (Linux)."""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import tempfile
import time

from read_latest_email_from_sender import headers_from
from run_email_agent import (fail, outside_repo, parse_json, pipeline_lock,
                             save_state, state_directory, sync_directory)
from send_gmail_reply import ID_PATTERN, SECRET_PATTERN


def require(condition, status="event_invalid"):
    if not condition:
        fail(status, 8)


def valid_id(value):
    return isinstance(value, str) and bool(ID_PATTERN.fullmatch(value))


def valid_history(value):
    if type(value) is int and value > 0:
        return True
    return isinstance(value, str) and bool(re.fullmatch(r"[1-9][0-9]*", value))


@dataclass(frozen=True)
class NewEmailEvent:
    message_id: str
    history_id: str | int | None = None

    def __post_init__(self):
        require(valid_id(self.message_id))
        require(self.history_id is None or valid_history(self.history_id))


def decode_notification(data, account):
    """StreamingPull SDK has already base64-decoded PubsubMessage.data."""
    require(isinstance(data, bytes) and len(data) <= 16384)
    value = parse_json(data.decode("utf-8"))
    require(isinstance(value, dict))
    require(isinstance(value.get("emailAddress"), str)
            and value["emailAddress"].casefold() == account.casefold())
    require(valid_history(value.get("historyId")))
    return str(value["historyId"])


def realtime_directory():
    return outside_repo(state_directory() / "realtime")


class Store:
    """Caller holds pipeline_lock. State contains identifiers/statuses only."""

    def __init__(self, directory, account):
        self.directory = outside_repo(Path(directory))
        self.account = account
        self.outbox = self.directory / "outbox"

    def load(self):
        path = self.directory / "state.json"
        scope = hashlib.sha256(self.account.casefold().encode()).hexdigest()
        require(not path.is_symlink(), "state_invalid_manual_review")
        if not path.exists():
            return dict(version=1, account_sha256=scope, history_id=None,
                        watch_expiration=None, messages={})
        try:
            state = parse_json(path.read_text(encoding="utf-8"))
            require(isinstance(state, dict) and set(state) == {
                "version", "account_sha256", "history_id", "watch_expiration", "messages"})
            require(type(state["version"]) is int and state["version"] == 1
                    and state["account_sha256"] == scope)
            require(state["history_id"] is None or valid_history(state["history_id"]))
            require(state["watch_expiration"] is None or valid_history(state["watch_expiration"]))
            require(isinstance(state["messages"], dict))
            require(all(valid_id(key) and value in ("pending", "retryable", "processed", "ignored")
                        for key, value in state["messages"].items()))
            require(state["history_id"] is not None or not state["messages"])
            return state
        except Exception:
            fail("state_invalid_manual_review", 8)

    def save(self, state):
        save_state(self.directory, state)

    def payload_exists(self, message_id):
        require(not self.outbox.is_symlink(), "outbox_invalid_manual_review")
        path = self.outbox / (message_id + ".json")
        require(not path.is_symlink(), "outbox_invalid_manual_review")
        if not path.exists():
            return False
        value = parse_json(path.read_text(encoding="utf-8"))
        validate_payload(value, message_id)
        # Also complete a directory fsync that may have failed after replace.
        sync_directory(self.outbox)
        return True

    def publish(self, payload):
        """One immutable local payload per message; never print it to journal."""
        message_id = payload["message_id"]
        validate_payload(payload, message_id)
        if self.payload_exists(message_id):
            return
        self.outbox.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self.outbox, 0o700)
        sync_directory(self.directory)
        fd, name = tempfile.mkstemp(prefix=".payload-", dir=self.outbox)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.outbox / (message_id + ".json"))
            sync_directory(self.outbox)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def notification_sent(self, message_id):
        path = self.outbox / (message_id + ".notified")
        require(not path.is_symlink(), "outbox_invalid_manual_review")
        if path.exists():
            sync_directory(self.outbox)
            return True
        return False

    def begin_notification(self, message_id):
        # A crash/timeout after this fence is an uncertain external send. Never
        # automatically resend until the operator reconciles Telegram delivery.
        self.notification_marker(message_id, ".notifying")

    def mark_notification_sent(self, message_id):
        self.notification_marker(message_id, ".notified")

    def notification_marker(self, message_id, suffix):
        require(valid_id(message_id) and not self.outbox.is_symlink(), "outbox_invalid_manual_review")
        path = self.outbox / (message_id + suffix)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        sync_directory(self.outbox)


def validate_payload(payload, message_id):
    require(isinstance(payload, dict) and set(payload) in (
        {"event", "message_id", "from", "subject", "summary"},
        {"event", "message_id", "from", "subject", "date", "summary"},
    ), "payload_invalid")
    require(payload["event"] == "new_email" and valid_id(message_id)
            and payload["message_id"] == message_id, "payload_invalid")
    require(all(isinstance(value, str) for value in payload.values()), "payload_invalid")
    require(len(payload["summary"]) <= 2000, "payload_invalid")


def redact(value, secrets=()):
    """Heuristic redaction plus exact matching of known runtime credentials."""
    known = list(secrets) + [v for k, v in os.environ.items() if v and
                            re.search(r"(?i)(PASSWORD|PASSWD|TOKEN|SECRET|API_KEY)$", k)]
    for secret in sorted(set(known), key=len, reverse=True):
        if secret:
            value = value.replace(secret, "[REDACTED]")
    value = SECRET_PATTERN.sub("[REDACTED]", value)
    value = re.sub(r"\b[0-9]{6,}:[A-Za-z0-9_-]{20,}\b", "[REDACTED]", value)
    return value


def normalize_email(message, expected_id):
    require(isinstance(message, dict) and message.get("id") == expected_id, "gmail_response_invalid")
    labels = message.get("labelIds")
    require(isinstance(labels, list) and all(isinstance(x, str) for x in labels),
            "gmail_labels_invalid")
    if {"SENT", "DRAFT"}.intersection(labels):
        return None
    require(valid_id(message.get("threadId")), "gmail_response_invalid")
    payload = message.get("payload")
    require(isinstance(payload, dict), "gmail_response_invalid")
    headers = headers_from(payload)
    require(all(isinstance(value, str) for value in headers.values()), "gmail_headers_invalid")

    class PlainHTML(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.text, self.hidden = [], 0

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style"):
                self.hidden += 1
            if tag in ("br", "p", "div", "li") and not self.hidden:
                self.text.append("\n")

        def handle_endtag(self, tag):
            if tag in ("script", "style") and self.hidden:
                self.hidden -= 1

        def handle_data(self, data):
            if not self.hidden:
                self.text.append(data)

    def text_parts(part):
        require(isinstance(part, dict), "gmail_mime_invalid")
        # Never fetch attachments or follow HTML links. Prefer plain text.
        if part.get("filename"):
            return []
        parts = part.get("parts", [])
        require(isinstance(parts, list), "gmail_mime_invalid")
        found = []
        for child in parts:
            found.extend(text_parts(child))
        if part.get("mimeType") in ("text/plain", "text/html"):
            body = part.get("body", {})
            require(isinstance(body, dict), "gmail_mime_invalid")
            data = body.get("data", "")
            require(isinstance(data, str), "gmail_mime_invalid")
            raw = base64.b64decode(data + "=" * (-len(data) % 4), altchars=b"-_", validate=True)
            text = raw.decode("utf-8", errors="replace")
            if part["mimeType"] == "text/html":
                parser = PlainHTML()
                parser.feed(text)
                text = "".join(parser.text).strip()
            found.append((part["mimeType"], text))
        return found

    parts = text_parts(payload)
    plain = [text for kind, text in parts if kind == "text/plain"]
    body = "\n".join(plain or [text for _, text in parts])
    return {"message_id": expected_id, "thread_id": message["threadId"],
            "from": headers.get("from", ""), "to": headers.get("to", ""),
            "subject": headers.get("subject", ""), "date": headers.get("date", ""),
            "body": body}


class Pipeline:
    def __init__(self, store, gmail, summarize=None, secrets=(), notify=None):
        self.store, self.gmail = store, gmail
        self.summarize = summarize
        self.secrets = secrets
        self.notify = notify

    def enqueue(self, event):
        """Backend-neutral boundary; direct message events do not move cursor."""
        state = self.store.load()
        require(state["history_id"] is not None, "watch_registration_required")
        state["messages"].setdefault(event.message_id, "pending")
        self.store.save(state)

    def discover(self, notification_history=None):
        state = self.store.load()
        start = state["history_id"]
        require(start is not None, "watch_registration_required")
        if notification_history is not None:
            require(valid_history(notification_history))
            if int(notification_history) <= int(start):
                return
        # All pages must succeed before cursor and queue commit together.
        page_token = None
        seen_tokens = set()
        while True:
            page = self.gmail.history(start, page_token)
            require(isinstance(page, dict) and valid_history(page.get("historyId")), "history_invalid")
            records = page.get("history", [])
            require(isinstance(records, list), "history_invalid")
            for record in records:
                require(isinstance(record, dict) and valid_history(record.get("id")), "history_invalid")
                added = record.get("messagesAdded", [])
                require(isinstance(added, list), "history_invalid")
                for item in added:
                    require(isinstance(item, dict) and isinstance(item.get("message"), dict), "history_invalid")
                    event = NewEmailEvent(item["message"].get("id"), record["id"])
                    state["messages"].setdefault(event.message_id, "pending")
            page_token = page.get("nextPageToken")
            if not page_token:
                require(int(page["historyId"]) >= int(start), "history_invalid")
                state["history_id"] = page["historyId"]
                self.store.save(state)
                return
            require(isinstance(page_token, str) and page_token not in seen_tokens, "history_invalid")
            seen_tokens.add(page_token)

    def renew(self, topic):
        state = self.store.load()
        profile = self.gmail.profile()
        require(isinstance(profile, dict) and isinstance(profile.get("emailAddress"), str)
                and profile["emailAddress"].casefold() == self.store.account.casefold(), "account_mismatch")
        require(valid_history(profile.get("historyId")), "history_invalid")
        # Persist a baseline BEFORE watch, closing the registration/crash race.
        if state["history_id"] is None:
            state["history_id"] = profile["historyId"]
            self.store.save(state)
        response = self.gmail.watch(topic)
        require(isinstance(response, dict) and valid_history(response.get("historyId"))
                and valid_history(response.get("expiration")), "watch_response_invalid")
        require(int(response["expiration"]) > int(time.time() * 1000), "watch_expired")
        state["watch_expiration"] = response["expiration"]
        self.store.save(state)
        # Never replace an existing cursor with the watch response. Daily catchup
        # also recovers dropped Pub/Sub notifications without Gmail polling.
        self.discover()

    def process_pending(self, message_ids=None):
        state = self.store.load()
        retried = 0
        for message_id, status in list(state["messages"].items()):
            if message_ids is not None and message_id not in message_ids:
                continue
            if status not in ("pending", "retryable"):
                continue
            try:
                if self.store.payload_exists(message_id):
                    payload = parse_json((self.store.outbox / (message_id + ".json")).read_text(encoding="utf-8"))
                else:
                    email = normalize_email(self.gmail.get(message_id), message_id)
                    if email is None:
                        state["messages"][message_id] = "ignored"
                    else:
                        safe_email = {key: redact(value, self.secrets) for key, value in email.items()}
                        summary = self.summarize(safe_email) if self.summarize else ""
                        require(isinstance(summary, str) and len(summary) <= 2000, "summary_invalid")
                        payload = {"event": "new_email", "message_id": message_id,
                                   "from": safe_email["from"][:1000], "subject": safe_email["subject"][:1000],
                                   "date": safe_email["date"][:1000],
                                   "summary": redact(summary, self.secrets)}
                        self.store.publish(payload)
                if state["messages"][message_id] != "ignored":
                    if self.notify and not self.store.notification_sent(message_id):
                        self.store.begin_notification(message_id)
                        self.notify(payload)
                        self.store.mark_notification_sent(message_id)
                    state["messages"][message_id] = "processed"
            except Exception:
                # No exception text (it can contain a message body or credential).
                state["messages"][message_id] = "retryable"
                retried += 1
            self.store.save(state)
        return retried
