#!/usr/bin/env python3
"""Prepare untrusted email JSON for an LLM to draft a reply; never access Gmail."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from typing import Any

from read_latest_email_from_sender import (
    EXIT_INVALID_JSON,
    EXIT_NOT_FOUND,
    EXIT_UNEXPECTED_SHAPE,
    SENDER_PATTERN,
    PipelineError,
    empty_result,
)


def prepare_context(email: Any, sender: str) -> dict[str, Any]:
    """Keep source facts separate from the empty draft and trusted recipient."""
    if not SENDER_PATTERN.fullmatch(sender):
        raise PipelineError("sender must be a single valid email address", 2)
    fields = empty_result()
    if not isinstance(email, Mapping) or not fields.keys() <= email.keys():
        raise PipelineError("Expected the reader's six-field JSON object.", EXIT_UNEXPECTED_SHAPE)
    if any(email[key] is not None and not isinstance(email[key], str) for key in fields):
        raise PipelineError("Email fields must be strings or null.", EXIT_UNEXPECTED_SHAPE)
    if all(email[key] is None for key in fields):
        raise PipelineError("No matching email; no reply context prepared.", EXIT_NOT_FOUND)
    if not email["message_id"] or not email["message_id"].strip():
        raise PipelineError("Source message ID is missing.", EXIT_UNEXPECTED_SHAPE)
    if not email["body"] or not email["body"].strip():
        raise PipelineError("Email body is unavailable; cannot ground a reply.", EXIT_UNEXPECTED_SHAPE)

    # Normalize header whitespace without altering the original source data.
    subject = " ".join((email["subject"] or "").split())
    if not subject:
        subject = "Re: (no subject)"
    elif not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    return {
        "source_message_id": email["message_id"],
        "thread_id": email["thread_id"],
        # Never let an untrusted From/Reply-To header redirect the reply.
        "to": sender,
        "subject": subject,
        "draft_body": None,
        "untrusted_email": {key: email[key] for key in ("from", "subject", "date", "body")},
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read pipeline JSON from stdin and prepare context for a text-only LLM reply."
    )
    parser.add_argument("sender", help="The same user-specified sender passed to the reader")
    args = parser.parse_args(argv)
    if not SENDER_PATTERN.fullmatch(args.sender):
        parser.error("sender must be a single valid email address")
    try:
        email = json.load(sys.stdin)
        context = prepare_context(email, args.sender)
    except (json.JSONDecodeError, UnicodeError):
        print("Input is not valid email JSON.", file=sys.stderr)
        return EXIT_INVALID_JSON
    except PipelineError as exc:
        print(str(exc), file=sys.stderr)
        return exc.exit_code
    print(json.dumps(context, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
