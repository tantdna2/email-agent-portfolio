"""Private, bounded failure handoff; no email, model or send-path dependencies.

pending-alert.json holds ONE slot with controlled values only:
  id        run/alert identity (random hex)
  boot      Linux boot identity of the writer
  status    running | pending | sending | sent | clear
  category  Category name of this failure
  previous  earliest unsent Category name carried over from an older slot, or null
  claim     null, or {"owner", "boot", "at"} while one handler is delivering

alert.lock is held only for these short read/modify/write sections, never
while waiting for the Gateway or invoking Telegram. A claim identifies the
delivering handler; completion only writes if the slot still carries that
same id and owner, so a concurrent run or handler is never overwritten.
"""
import os
from pathlib import Path
import re
import time
import uuid

from agent_reliability import Category, Failure, private_lock, private_path, read_private, write_private, stage

LOCK_WAIT = 5
CLAIM_TTL = 450  # > alert service TimeoutStartSec (400s): a stale claim belongs to a dead handler
STATUSES = {"running", "pending", "sending", "sent", "clear"}
UNSENT = {"pending", "sending"}


def directory():
    with stage(Category.STATE):
        return _directory()


def _directory():
    path = Path(os.environ.get("EMAIL_AGENT_MONITOR_STATE_DIR",
                str(Path.home() / ".local/state/email-agent-monitoring"))).expanduser()
    if (not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents))
            or path.resolve().is_relative_to(Path(__file__).resolve().parents[1])):
        raise Failure(Category.STATE)
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    return private_path(path, directory=True)


def boot_id():
    # Linux boot identity only; no host identifiers are logged or transmitted.
    with stage(Category.STATE):
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()


def _hex(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) is not None


def load(root):
    value = read_private(root, "pending-alert.json", None)
    if value is None:
        return None
    claim = value.get("claim") if isinstance(value, dict) else None
    if (not isinstance(value, dict)
            or set(value) != {"id", "boot", "status", "category", "previous", "claim"}
            or not _hex(value["id"]) or not isinstance(value["boot"], str)
            or value["status"] not in STATUSES
            or value["category"] not in Category.__members__
            or not (value["previous"] is None or value["previous"] in Category.__members__)
            or not (claim is None or isinstance(claim, dict) and set(claim) == {"owner", "boot", "at"}
                    and _hex(claim["owner"]) and isinstance(claim["boot"], str)
                    and type(claim["at"]) is int)
            or (value["status"] == "sending") != (claim is not None)):
        raise Failure(Category.STATE)
    return value


def _save(root, value):
    write_private(root, "pending-alert.json", value)


def carried(value):
    """Earliest failure in `value` that was never delivered, else None."""
    if value is None:
        return None
    if value["status"] in UNSENT:
        return value["previous"] or value["category"]
    if value["status"] == "running":
        # An unfinished run is an interruption (WSL stop, kill) unless the
        # caller is that same run; it keeps any older undelivered cause.
        return value["previous"] or Category.UNKNOWN.name
    return None  # sent, or cleared by a later successful report


def begin():
    root = directory()
    with private_lock(root, "alert.lock", timeout=LOCK_WAIT):
        previous = carried(load(root))  # corruption must not be silently overwritten
        token = uuid.uuid4().hex
        _save(root, {"id": token, "boot": boot_id(), "status": "running",
                     "category": Category.UNKNOWN.name, "previous": previous, "claim": None})
        return token


def finish(token, category=None):
    """Success clears (the report itself reached the operator); failure becomes pending.

    `previous` is retained in both cases, so a recovered run keeps the evidence
    of the earlier undelivered failure in the private slot.
    """
    root = directory()
    with private_lock(root, "alert.lock", timeout=LOCK_WAIT):
        value = load(root)
        if value and value["id"] == token and value["status"] == "running":
            value.update(status="pending" if category else "clear",
                         category=category.name if category else Category.UNKNOWN.name)
            _save(root, value)


def _stale(value, now, boot):
    claim = value["claim"]
    return claim is not None and (claim["boot"] != boot or not 0 <= now - claim["at"] < CLAIM_TTL)


def claim(replay=False, category=Category.UNKNOWN, now=None):
    """Atomically select and claim the slot to deliver; return (id, owner, category, previous) or None."""
    now = int(time.time() if now is None else now)
    root = directory()
    with private_lock(root, "alert.lock", timeout=LOCK_WAIT):
        value = load(root)
        boot = boot_id()
        if value and value["status"] == "sending" and not _stale(value, now, boot):
            return None  # another live handler owns delivery
        if replay:
            if not value:
                return None
            if value["status"] == "running" and value["boot"] != boot:
                value.update(status="pending", category=Category.UNKNOWN.name)  # interrupted run
            if value["status"] not in UNSENT:
                return None
        elif not value or value["status"] not in UNSENT:
            if value and value["status"] == "running" and value["boot"] == boot:
                previous = value["previous"]  # same failed run, killed before finish()
            else:
                previous = carried(value)
            value = {"id": uuid.uuid4().hex, "boot": boot, "status": "pending",
                     "category": category.name, "previous": previous, "claim": None}
        owner = uuid.uuid4().hex
        value.update(status="sending", claim={"owner": owner, "boot": boot, "at": now})
        _save(root, value)
        return value["id"], owner, Category[value["category"]], (
            Category[value["previous"]] if value["previous"] else None)


def complete(alert_id, owner, delivered):
    """Record the outcome only if this handler still owns the same slot."""
    root = directory()
    with private_lock(root, "alert.lock", timeout=LOCK_WAIT):
        value = load(root)
        if (value and value["id"] == alert_id and value["status"] == "sending"
                and value["claim"]["owner"] == owner):
            value.update(status="sent" if delivered else "pending", claim=None)
            if delivered:
                value["previous"] = None
            _save(root, value)
            return True
        return False
