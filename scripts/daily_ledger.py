"""Daily checkpoints beside unchanged state.json; caller holds pipeline.lock."""
import re

from agent_reliability import (Category, DeliveryUncertain, Failure, local_day,
                               read_private, write_private)

OPEN = {"inflight", "uncertain"}  # Telegram outcome unknown: never replayed automatically
ATTENTION = {"manual", "uncertain"}  # day statuses with no automatic work remaining
MANUAL_ATTEMPTS = 3  # permanent data/contract failures before manual review
MANUAL_TRANSIENT_DAYS = 3  # distinct Bangkok days of transient model failures
RESOLVED = {"sent", "skipped", "expired"}


def check(condition):
    if not condition:
        raise Failure(Category.STATE)


def reconciliation_mid(key):
    kind, _, mid = key.partition(":")
    return mid if kind in {"draft", "manual"} and re.fullmatch(r"[0-9a-fA-F]+", mid) else None


class Ledger:
    def __init__(self, store):
        self.store = store
        self.value = read_private(store.directory, "daily-ledger.json", {
            "schema": 1, "scope": store.fingerprint, "days": {}, "sources": {}, "deliveries": {}})
        try:
            data = self.value
            check(set(data) == {"schema", "scope", "days", "sources", "deliveries"})
            check(type(data["schema"]) is int and data["schema"] == 1 and data["scope"] == store.fingerprint)
            check(all(isinstance(data[k], dict) for k in ("days", "sources", "deliveries")))
            for date, day in data["days"].items():
                check(re.fullmatch(r"\d{4}-\d{2}-\d{2}", date))
                check(set(day) == {"status", "planned", "items"})
                check(day["status"] in {"pending", "partial", "completed", "manual", "uncertain"})
                check(type(day["planned"]) is bool and isinstance(day["items"], list))
                check(len(set(day["items"])) == len(day["items"]))
                check(all(re.fullmatch(r"[0-9a-fA-F]+", mid) for mid in day["items"]))
            for mid, item in data["sources"].items():
                check(re.fullmatch(r"[0-9a-fA-F]+", mid))
                check(set(item) == {"raw", "email", "attempts", "transient", "category", "manual"})
                check(isinstance(item["raw"], dict))
                check(item["email"] is None or isinstance(item["email"], dict))
                check(type(item["attempts"]) is int and 0 <= item["attempts"] <= MANUAL_ATTEMPTS)
                check(isinstance(item["transient"], list) and len(item["transient"]) <= MANUAL_TRANSIENT_DAYS
                      and len(set(item["transient"])) == len(item["transient"])
                      and all(isinstance(d, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d)
                              for d in item["transient"]))
                check(item["category"] in Category.__members__)
                check(type(item["manual"]) is bool)
            check(all(isinstance(k, str) and v in {"inflight", "sent", "uncertain"}
                      for k, v in data["deliveries"].items()))
        except Exception:
            raise Failure(Category.STATE) from None

    def save(self):
        write_private(self.store.directory, "daily-ledger.json", self.value)

    def today(self):
        date = local_day(self.store.now())
        return date, self.value["days"].setdefault(date, {"status": "pending", "planned": False, "items": []})

    def verify_completed(self, date, day, state):
        check(day["planned"])
        receipts = self.value["deliveries"]
        if not day["items"]:
            check(receipts.get(date + ":none") == "sent")
        else:
            check(receipts.get(date + ":overview") == "sent")
            check(all(receipts.get("draft:" + mid) == "sent"
                      and state["records"].get(mid, {}).get("reported") is True for mid in day["items"]))

    def verify_attention(self, date, day, state):
        # A manual/uncertain day is never COMPLETED; it has no automatic work
        # left. Every item must be delivered, awaiting reconciliation or manual.
        check(day["planned"])
        receipts = self.value["deliveries"]
        head = date + (":overview" if day["items"] else ":none")
        check(receipts.get(head) in {"sent"} | OPEN)
        for mid in day["items"]:
            record = state["records"].get(mid)
            source = self.value["sources"].get(mid)
            check((record is not None and (record["reported"] is True or receipts.get("draft:" + mid) in OPEN))
                  or (record is None and source is not None and source["manual"]
                      and receipts.get("manual:" + mid) in {"sent"} | OPEN))

    def attention_items(self, day, state):
        receipts = self.value["deliveries"]
        return [mid for mid in day["items"]
                if receipts.get("draft:" + mid) in OPEN
                or (mid not in state["records"] and self.value["sources"].get(mid, {}).get("manual"))]

    def reconciliation_items(self):
        return {mid for key, status in self.value["deliveries"].items()
                if status in OPEN and (mid := reconciliation_mid(key))}

    def reconciliation_reminder(self):
        count = sum(1 for key, status in self.value["deliveries"].items()
                    if status in OPEN and reconciliation_mid(key))
        return (f"\nCần đối soát thủ công: {count} thông báo Telegram chưa rõ kết quả (runbook mục 8)."
                if count else "")

    def count_failure(self, mid, kind, date):
        """Record one failed processing; return True once the source needs manual review."""
        source = self.value["sources"][mid]
        if kind == "transient":
            # Model/provider outages count once per Bangkok day: reopening Ubuntu
            # several times during one outage cannot exhaust a valid email.
            if date not in source["transient"]:
                source["transient"] = (source["transient"] + [date])[-MANUAL_TRANSIENT_DAYS:]
            source["category"] = Category.MODEL_UNAVAILABLE.name
        else:
            source["attempts"] = min(MANUAL_ATTEMPTS, source["attempts"] + 1)
            source["category"] = (Category.PARTIAL if kind == "data" else Category.MODEL_UNAVAILABLE).name
        source["manual"] = (source["attempts"] >= MANUAL_ATTEMPTS
                            or len(source["transient"]) >= MANUAL_TRANSIENT_DAYS)
        self.save()
        return source["manual"]

    def minimize(self, state):
        """Drop source snapshots that recovery and reconciliation no longer need.

        Kept: sources without a draft record, manual review, any Telegram key that
        is not confirmed sent, and drafts that are unresolved, failed or
        send_uncertain. Only resolved (sent/skipped/expired), reported drafts with
        a confirmed delivery lose the raw Gmail payload; the ID tombstone stays.
        """
        receipts = self.value["deliveries"]
        for mid, source in self.value["sources"].items():
            record = state["records"].get(mid)
            if (record is not None and record["reported"] is True and record["status"] in RESOLVED
                    and not source["manual"] and receipts.get("draft:" + mid) == "sent"
                    and (source["raw"] or source["email"] is not None)):
                source["raw"], source["email"] = {}, None

    def deliver(self, key, view, notify):
        """Return "sent" or "uncertain"; raise DeliveryUncertain on a new failure."""
        deliveries = self.value["deliveries"]
        status = deliveries.get(key)
        if status == "sent":
            return "sent"
        if status in OPEN:
            if status == "inflight":
                deliveries[key] = "uncertain"  # crash after intent: outcome unknown
                self.save()
            return "uncertain"
        deliveries[key] = "inflight"
        self.save()  # crash fence BEFORE invoking transport
        try:
            notify(view)
        except Exception:
            deliveries[key] = "uncertain"
            self.save()
            raise DeliveryUncertain() from None
        deliveries[key] = "sent"
        self.save()  # crash before this commit => manual reconciliation, no retry
        return "sent"
