#!/usr/bin/env python3
"""Validate a reviewed five-field reply; dry-run unless --send is explicit.

CLI verified against gog v0.40.0 (a3065f53). No OAuth setup or retries.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from email import policy
from email.parser import HeaderParser

from read_latest_email_from_sender import PipelineError, SENDER_PATTERN


FIELDS = {"source_message_id", "thread_id", "to", "subject", "draft_body"}
ID_PATTERN = re.compile(r"[0-9a-fA-F]+")
SECRET_PATTERN = re.compile(
    r"(?i)(?:"
    r"(?:password|passwd|pwd|gog[_ -]?keyring[_ -]?password|"
    r"(?:access|refresh)[_ -]?token|client[_ -]?secret|api[_ -]?key)"
    r"[\"']?\s*[:=]\s*[\"']?\S+|"
    r"\bBearer\s+\S+|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    r"\bAIza[\w-]{20,}|\bya29\.[\w.-]+|\bsk-(?:proj-)?[\w-]{16,}|"
    r"\bgh[pousr]_[A-Za-z0-9]{20,}|\beyJ[\w-]+\.eyJ[\w-]+\.[\w-]+)"
)


def fail(message, code=6):
    raise PipelineError(message, code)


def no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            fail("Duplicate JSON keys are not accepted.", 5)
        result[key] = value
    return result


def has_controls(text, multiline=False):
    allowed = "\n\r\t" if multiline else ""
    return any((ord(c) < 32 or 127 <= ord(c) <= 159) and c not in allowed for c in text)


def validate_draft(draft, sender):
    if not isinstance(sender, str) or not SENDER_PATTERN.fullmatch(sender):
        fail("Expected one valid operator-specified sender.", 2)
    if not isinstance(draft, dict) or set(draft) != FIELDS:
        fail("Expected exactly the five fields from gmail-reply-drafter.")
    for key in FIELDS - {"thread_id"}:
        if not isinstance(draft[key], str) or not draft[key].strip():
            fail(f"{key} must be a nonempty string.")
    for key in ("source_message_id", "thread_id"):
        if key == "thread_id" and draft[key] is None:
            continue
        if not isinstance(draft[key], str) or not ID_PATTERN.fullmatch(draft[key]):
            fail(f"{key} must be a Gmail hexadecimal ID.")
    if not SENDER_PATTERN.fullmatch(draft["to"]) or draft["to"] != sender:
        fail("Recipient must exactly match the operator-specified sender.")
    if has_controls(draft["subject"]) or draft["subject"] != draft["subject"].strip():
        fail("Subject must be a single nonempty header without outer whitespace.")
    if has_controls(draft["draft_body"], multiline=True):
        fail("Reply body contains unsupported control characters.")
    values = [value for value in draft.values() if isinstance(value, str)]
    known_secrets = [value for key, value in os.environ.items() if value and
                     re.search(r"(?i)(PASSWORD|PASSWD|TOKEN|SECRET|API_KEY)$", key)]
    if any(SECRET_PATTERN.search(value) or any(secret in value for secret in known_secrets)
           for value in values):
        fail("Possible secret in reply data; review and remove it before proceeding.")


def base_command(account, command):
    return ["gog", f"--account={account}", "--no-input", "--json", "--results-only",
            f"--enable-commands-exact={command}"]


def get_command(account, message_id):
    return base_command(account, "gmail.get") + [
        "--readonly", "--gmail-no-send", "gmail", "get", message_id,
        "--format=metadata", "--headers=From,Reply-To,To,Cc,Bcc,Subject,Message-ID",
    ]


def mailbox_addresses(value):
    if not value:
        return []
    if not isinstance(value, str) or has_controls(value):
        fail("Unsupported source address header.")
    try:
        header = HeaderParser(policy=policy.default).parsestr(f"To: {value}\n\n")["To"]
        addresses = [address.addr_spec for address in header.addresses]
        if header.defects or not addresses or any(
            not SENDER_PATTERN.fullmatch(address) for address in addresses
        ) or any(group.display_name is not None for group in header.groups):
            fail("Ambiguous source address header.")
        return addresses
    except (ValueError, IndexError, AttributeError):
        fail("Malformed source address header.")


def validate_source(payload, draft, sender):
    """Use raw metadata so duplicate headers cannot hide a recipient redirect."""
    if not isinstance(payload, dict):
        fail("Unsupported Gmail metadata response.")
    message = payload.get("message", payload)
    if not isinstance(message, dict) or message.get("id") != draft["source_message_id"]:
        fail("Source message was not verified.")
    thread = message.get("threadId")
    if not isinstance(thread, str) or not ID_PATTERN.fullmatch(thread):
        fail("Source thread was not verified.")
    if draft["thread_id"] is not None and draft["thread_id"] != thread:
        fail("Source thread does not match the reviewed draft.")
    labels = message.get("labelIds", [])
    if not isinstance(labels, list) or any(not isinstance(label, str) for label in labels):
        fail("Malformed source labels.")
    if "DRAFT" in labels:
        fail("Cannot reply to an unsent Gmail Draft.")
    part = message.get("payload")
    if not isinstance(part, dict) or not isinstance(part.get("headers"), list):
        fail("Raw source headers are required.")
    headers = {}
    relevant = {"from", "reply-to", "to", "cc", "bcc", "subject", "message-id"}
    for item in part["headers"]:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            fail("Malformed source headers.")
        name = item["name"].lower()
        if name not in relevant:
            continue
        value = item.get("value")
        if name in headers or not isinstance(value, str):
            fail("Duplicate or malformed source headers.")
        headers[name] = value
    if not headers.get("message-id", "").strip() or has_controls(headers["message-id"]):
        fail("Source has no valid Message-ID header.")
    if mailbox_addresses(headers.get("from", "")) != [sender]:
        fail("Source From must match the operator-specified sender.")
    if headers.get("reply-to") and mailbox_addresses(headers["reply-to"]) != [sender]:
        fail("Source Reply-To redirects away from the approved sender.")
    # A reply to one's own email can inherit original To recipients in gog.
    # Remove every other address, even in that fallback, before dispatch.
    remove = set()
    for name in ("to", "cc", "bcc"):
        remove.update(address.lower() for address in mailbox_addresses(headers.get(name, ""))
                      if address.lower() != sender.lower())
    original_subject = headers.get("subject", "").strip()
    expected_subject = original_subject if original_subject.lower().startswith("re:") else f"Re: {original_subject}"
    if not original_subject or draft["subject"] != expected_subject:
        fail("Subject differs from the source reply subject; refusing a new thread.")
    return sorted(remove), thread


def reply_command(account, draft, remove):
    # No shell, attachments, quotes, signatures, reply-all, or free recipient flags.
    return base_command(account, "gmail.reply") + [
        "gmail", "reply", draft["source_message_id"], f"--to={draft['to']}",
        f"--subject={draft['subject']}", "--body-file=-", "--no-quote",
        "--auto-from-addressed-alias=false", "--signature=false",
    ] + [f"--remove={address}" for address in remove]


def run_command(command, body=None, sending=False):
    try:
        result = subprocess.run(command, input=body, capture_output=True, text=True,
                                encoding="utf-8", check=False, timeout=60)
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        fail("Reply outcome unknown; do not retry automatically." if sending
             else "Unable to read source metadata; nothing sent.", 7 if sending else 4)
    if result.returncode != 0:
        # Never echo gog stderr: it can include message text or authentication data.
        fail("Reply command failed; inspect outcome before any manual retry." if sending
             else "Source lookup failed; nothing sent. Check existing authorization.",
             7 if sending else 4)
    return result.stdout


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--sender", required=True, help="Trusted sender approved by the operator")
    parser.add_argument("--account", default=os.environ.get("GOG_ACCOUNT"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--send", action="store_true", help="Explicitly authorize this reviewed reply")
    mode.add_argument("--dry-run", action="store_true", help="Local validation only (default)")
    args = parser.parse_args(argv)
    try:
        draft = json.load(sys.stdin, object_pairs_hook=no_duplicate_keys)
        validate_draft(draft, args.sender)
        if not args.send:
            print(json.dumps({"status": "dry_run", "sent": False,
                              "source_verified": False, "reply": draft}, ensure_ascii=False))
            return 0
        if (not args.account or not args.account.strip()
                or args.account != args.account.strip()
                or args.account.lower() == "auto" or has_controls(args.account)):
            fail("An explicit account/alias or GOG_ACCOUNT is required for --send.", 2)
        raw = run_command(get_command(args.account, draft["source_message_id"]))
        source = json.loads(raw, object_pairs_hook=no_duplicate_keys)
        remove, thread = validate_source(source, draft, args.sender)
        run_command(reply_command(args.account, draft, remove), draft["draft_body"], sending=True)
        print(json.dumps({"status": "send_command_succeeded", "source_message_id": draft["source_message_id"],
                          "thread_id": thread, "to": draft["to"]}))
        return 0
    except (json.JSONDecodeError, UnicodeError):
        print("Invalid JSON; nothing sent.", file=sys.stderr)
        return 5
    except PipelineError as exc:
        print(str(exc), file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
