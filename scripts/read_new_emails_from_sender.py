#!/usr/bin/env python3
"""Paginated, read-only exact-sender reader; oldest internalDate then ID first."""
import os
import re
import time

from new_email_pipeline import normalize_email, require, valid_id
from read_latest_email_from_sender import empty_result
import send_gmail_reply as sender_api
from agent_reliability import MissingSource


def lookback_hours(value=None):
    value = os.environ.get("EMAIL_AGENT_INITIAL_LOOKBACK_HOURS", "24") if value is None else value
    require(isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value)
            and 1 <= int(value) <= 168, "lookback_invalid")
    return int(value)


def source_email(raw, message_id, sender):
    require(isinstance(raw, dict) and raw.get("id") == message_id, "source_invalid")
    headers = raw.get("payload", {}).get("headers")
    require(isinstance(headers, list), "source_invalid")
    from_values = [h.get("value") for h in headers
                   if isinstance(h, dict) and str(h.get("name", "")).lower() == "from"]
    require(len(from_values) == 1, "source_invalid")
    # Do not decode the body or invoke the model for an unrelated sender.
    if sender_api.mailbox_addresses(from_values[0]) != [sender]:
        return None
    email = normalize_email(raw, message_id)
    if email is None:
        return None
    return {key: email[key] for key in empty_result()}


def read_new_emails(gmail, sender, since, reported=(), until=None, on_source=None, on_error=None):
    require(isinstance(sender, str) and sender_api.SENDER_PATTERN.fullmatch(sender), "sender_invalid")
    require(type(since) is int and since > 0, "checkpoint_invalid")
    until = int(time.time()) if until is None else until
    require(type(until) is int and until >= since, "checkpoint_invalid")
    # The bootstrap lower bound never advances: downtime and late indexing cannot
    # move an unreported message out of the search window. Dedup is per message.
    params = {"q": f"from:{sender} after:{since - 1} before:{until + 1}",
              "maxResults": 500, "includeSpamTrash": "true"}
    pages, ids, ordered = set(), set(), []
    reported = set(reported)
    while True:
        page = gmail.request("GET", "messages", params=dict(params))
        require(isinstance(page, dict), "reader_page_invalid")
        messages = page.get("messages", [])
        require(isinstance(messages, list), "reader_page_invalid")
        for item in messages:
            require(isinstance(item, dict) and valid_id(item.get("id")), "reader_id_invalid")
            message_id = item["id"]
            if message_id in ids or message_id in reported:
                continue
            ids.add(message_id)
            try:
                raw = gmail.get(message_id)
            except MissingSource:
                if on_error is None:
                    raise
                on_error(message_id, {"id": message_id, "read_status": "not_found"})
                continue
            try:
                email = source_email(raw, message_id, sender)
                if email is None:
                    continue
                stamp = raw.get("internalDate")
                require(isinstance(stamp, str) and re.fullmatch(r"[0-9]+", stamp), "reader_date_invalid")
                stamp = int(stamp)
            except Exception:
                if on_error is None:
                    raise
                on_error(message_id, raw)
                continue
            if since * 1000 <= stamp <= until * 1000:
                if on_source:
                    on_source(message_id, raw, email)
                ordered.append((stamp, message_id, email))
        token = page.get("nextPageToken")
        if token is None:
            break
        require(isinstance(token, str) and token and token not in pages, "reader_page_invalid")
        pages.add(token)
        params["pageToken"] = token
    return [email for _, _, email in sorted(ordered)]
