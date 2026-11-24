#!/usr/bin/env python3
"""Operator reconciliation for uncertain Telegram deliveries (TASK-020 F-04).

Read-only by default. It never invokes Telegram, Gmail or a model and has no
network dependency. A state change requires --apply plus an explicit key and,
for drafts, the exact current version shown by the read-only listing.

  list (default)            print unresolved delivery keys and draft metadata
  --confirm-delivered       operator SAW this exact card in the private chat:
                            record the receipt (draft becomes reported)
  --allow-redelivery        operator verified the card is ABSENT: rotate the
                            draft's tokens (any stray old buttons become stale)
                            and let the next daily run send one fresh card

Drafts must be awaiting review and unexpired for redelivery. Sent, skipped,
failed, expired and send_uncertain records are never re-armed. Exit codes use
the daily contract: 0 ok, 10 usage/config, 17 busy, 18 state/precondition.
"""
import argparse
import json
import os
import re
import sys

from agent_reliability import Category, Failure, stage
from daily_ledger import ATTENTION, OPEN, Ledger, reconciliation_mid
from daily_sender_digest import Store, configuration, directory_from_environment
from new_email_pipeline import require
from read_latest_email_from_sender import PipelineError


def listing(ledger, state):
    rows = []
    for key, status in sorted(ledger.value["deliveries"].items()):
        if status not in OPEN:
            continue
        row = {"key": key, "delivery": status}
        mid = reconciliation_mid(key)
        record = state["records"].get(mid) if mid and key.startswith("draft:") else None
        if record is not None:
            row.update(record_status=record["status"], version=record["version"], reported=record["reported"])
        rows.append(row)
    return rows


def plan(args, ledger, state, now):
    deliveries = ledger.value["deliveries"]
    require(deliveries.get(args.key) in OPEN, "key_not_unresolved")
    mid = reconciliation_mid(args.key)
    changes = {"key": args.key, "action": "confirm-delivered" if args.confirm_delivered else "allow-redelivery"}
    record = state["records"].get(mid) if mid and args.key.startswith("draft:") else None
    if args.key.startswith("draft:"):
        require(record is not None and args.version == record["version"], "version_mismatch")
        if args.allow_redelivery:
            require(record["status"] in {"awaiting_review", "awaiting_edit_instruction"}
                    and now < record["expires"], "draft_not_redeliverable")
    else:
        require(args.version is None, "version_only_for_drafts")
        require(mid is not None or re.fullmatch(r"\d{4}-\d{2}-\d{2}:(?:overview|none)", args.key),
                "key_invalid")
    return changes, mid, record


def affected_days(ledger, key, mid):
    days = ledger.value["days"]
    if mid is not None:
        return [day for day in days.values() if mid in day["items"]]
    date = key.split(":", 1)[0]
    return [days[date]] if date in days else []


def apply(args, store, ledger, state, mid, record):
    deliveries = ledger.value["deliveries"]
    if args.confirm_delivered:
        deliveries[args.key] = "sent"
        if record is not None:
            record["reported"] = True
            store.save(state)
    else:
        if record is not None:
            if state["active_edit"] == mid:
                state["active_edit"] = None
            record["status"] = "awaiting_review"
            store.rotate(record)  # never re-arm the possibly delivered old buttons
            store.save(state)
        del deliveries[args.key]
        # Let a same-day rerun pick the item up; the day is never marked completed here.
        for day in affected_days(ledger, args.key, mid):
            if day["status"] in ATTENTION:
                day["status"] = "partial"
    ledger.save()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--key")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--confirm-delivered", action="store_true")
    action.add_argument("--allow-redelivery", action="store_true")
    parser.add_argument("--version", type=int)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        with stage(Category.CONFIG):
            require(os.name == "posix", "linux_required")
            require(bool(args.key) == (args.confirm_delivered or args.allow_redelivery), "usage")
            require(not args.apply or args.key, "usage")
            store = Store(directory_from_environment(), configuration())
        try:
            lock = store.lock()
            lock.__enter__()
        except PipelineError as error:
            if str(error) == "locked_skip":
                raise Failure(Category.LOCKED) from None
            raise Failure(Category.STATE) from None
        try:
            with stage(Category.STATE):
                ledger = Ledger(store)
                state = store.load()
                if not args.key:
                    print(json.dumps({"unresolved": listing(ledger, state)}, ensure_ascii=False))
                    return 0
                changes, mid, record = plan(args, ledger, state, store.now())
                if not args.apply:
                    print(json.dumps({"dry_run": changes}))
                    return 0
                apply(args, store, ledger, state, mid, record)
                print(json.dumps({"applied": changes}))
                return 0
        finally:
            lock.__exit__(None, None, None)
    except Failure as error:
        print(json.dumps({"code": int(error.category), "category": error.category.name}))
        return int(error.category)
    except Exception:
        print(json.dumps({"code": int(Category.CONFIG), "category": "CONFIG"}))
        return int(Category.CONFIG)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
