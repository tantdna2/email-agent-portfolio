#!/usr/bin/env python3
"""Read the most recent Gmail message from one sender without mutating Gmail."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from typing import Any


EXIT_USAGE = 2
EXIT_NOT_FOUND = 3
EXIT_GOG_FAILURE = 4
EXIT_INVALID_JSON = 5
EXIT_UNEXPECTED_SHAPE = 6

SENDER_PATTERN = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)


class PipelineError(RuntimeError):
    """A safe, user-facing pipeline failure."""

    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def empty_result() -> dict[str, None]:
    """Return the fixed output contract when no matching email exists."""
    return {
        "message_id": None,
        "thread_id": None,
        "from": None,
        "subject": None,
        "date": None,
        "body": None,
    }


def clean_diagnostic(value: str) -> str:
    """Keep useful gog errors while avoiding accidental secret disclosure."""
    value = value.strip()
    value = re.sub(
        r'(?i)(((?:access|refresh)[_-]?token|client[_-]?secret)\s*[=:]\s*["\']?)[^\s,"\']+',
        r"\1[REDACTED]",
        value,
    )
    value = re.sub(r"\bAIza[0-9A-Za-z_-]{20,}\b", "[REDACTED_API_KEY]", value)
    return value[:500]


def run_gog(command: list[str], operation: str) -> Any:
    """Run gog without a shell and parse its JSON-only stdout."""
    try:
        completed = subprocess.run(command, check=False, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise PipelineError("gog was not found on PATH.", EXIT_GOG_FAILURE) from exc

    if completed.returncode != 0:
        detail = clean_diagnostic(completed.stderr)
        suffix = f": {detail}" if detail else ""
        raise PipelineError(
            f"gog {operation} failed with exit code {completed.returncode}{suffix}",
            EXIT_GOG_FAILURE,
        )

    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise PipelineError(
            f"gog {operation} did not return valid JSON.", EXIT_INVALID_JSON
        ) from exc


def first_search_result(payload: Any) -> Mapping[str, Any] | None:
    """Accept gog's list/envelope forms and return the newest result (index 0)."""
    candidates: Any = payload
    if isinstance(payload, Mapping):
        for key in ("messages", "threads", "items", "results", "data"):
            if isinstance(payload.get(key), Sequence) and not isinstance(
                payload.get(key), (str, bytes)
            ):
                candidates = payload[key]
                break
        else:
            if any(key in payload for key in ("id", "messageId", "message_id")):
                candidates = [payload]
            else:
                raise PipelineError(
                    "gog search returned an unsupported JSON shape.", EXIT_UNEXPECTED_SHAPE
                )

    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
        raise PipelineError("gog search returned an unsupported JSON shape.", EXIT_UNEXPECTED_SHAPE)
    if not candidates:
        return None
    if not isinstance(candidates[0], Mapping):
        raise PipelineError("gog search result is not a message object.", EXIT_UNEXPECTED_SHAPE)
    return candidates[0]


def value_from(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def message_id_from(result: Mapping[str, Any]) -> str:
    message_id = value_from(result, "id", "messageId", "message_id")
    if not isinstance(message_id, str) or not message_id:
        raise PipelineError("gog search result does not include a message ID.", EXIT_UNEXPECTED_SHAPE)
    return message_id


def headers_from(message: Mapping[str, Any]) -> dict[str, Any]:
    headers = message.get("headers", {})
    if isinstance(headers, Mapping):
        return {str(key).lower(): value for key, value in headers.items()}
    if isinstance(headers, Sequence) and not isinstance(headers, (str, bytes)):
        normalized: dict[str, Any] = {}
        for header in headers:
            if isinstance(header, Mapping):
                name = value_from(header, "name", "key")
                if isinstance(name, str):
                    normalized[name.lower()] = value_from(header, "value")
        return normalized
    return {}


def normalized_message(
    payload: Any, fallback_message_id: str, fallback_thread_id: Any = None
) -> dict[str, Any]:
    """Reduce gog's sanitized message JSON to the public six-field contract."""
    if not isinstance(payload, Mapping):
        raise PipelineError("gog get returned an unsupported JSON shape.", EXIT_UNEXPECTED_SHAPE)
    message = payload.get("message")
    if not isinstance(message, Mapping):
        message = payload

    headers = headers_from(message)
    body = value_from(message, "body", "text")
    if not isinstance(body, str):
        body = None

    return {
        "message_id": value_from(message, "id", "messageId", "message_id")
        or fallback_message_id,
        "thread_id": value_from(message, "threadId", "thread_id") or fallback_thread_id,
        "from": headers.get("from") or value_from(message, "from"),
        "subject": headers.get("subject") or value_from(message, "subject"),
        "date": headers.get("date") or value_from(message, "date", "internalDate"),
        "body": body,
    }


def gog_base_command(account: str) -> list[str]:
    """Safety flags and the only two command paths this pipeline may execute."""
    return [
        "gog",
        "--readonly",
        "--gmail-no-send",
        "--no-input",
        "--enable-commands-exact",
        "gmail.messages.search,gmail.get",
        "--account",
        account,
        "--json",
        "--results-only",
    ]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Print the newest message from one sender as a six-field JSON object."
    )
    parser.add_argument("sender", help="Exact sender email address, for example alerts@example.com")
    parser.add_argument(
        "--account",
        default=os.environ.get("GOG_ACCOUNT"),
        help="gog account or alias (defaults to GOG_ACCOUNT)",
    )
    args = parser.parse_args(argv)
    if not SENDER_PATTERN.fullmatch(args.sender):
        parser.error("sender must be a single valid email address")
    if not args.account:
        parser.error("--account is required unless GOG_ACCOUNT is set")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    base = gog_base_command(args.account)

    search_payload = run_gog(
        base + ["gmail", "messages", "search", f"from:{args.sender}", "--max", "1"],
        "gmail messages search",
    )
    latest = first_search_result(search_payload)
    if latest is None:
        logging.info("No email found from %s.", args.sender)
        print(json.dumps(empty_result(), ensure_ascii=False))
        return EXIT_NOT_FOUND

    message_id = message_id_from(latest)
    message_payload = run_gog(
        base + ["gmail", "get", message_id, "--sanitize-content"], "gmail get"
    )
    print(
        json.dumps(
            normalized_message(
                message_payload,
                fallback_message_id=message_id,
                fallback_thread_id=value_from(latest, "threadId", "thread_id"),
            ),
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        raise SystemExit(main())
    except PipelineError as exc:
        logging.error("%s", exc)
        raise SystemExit(exc.exit_code)
