"""Standard-library-only safety primitives; never import Gmail/application code."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from enum import IntEnum
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import time


class Category(IntEnum):
    UNKNOWN = 8
    CONFIG = 10
    GMAIL_AUTH = 11
    GMAIL_UNAVAILABLE = 12
    GATEWAY_NOT_READY = 13
    TELEGRAM_SEND = 14
    MODEL_UNAVAILABLE = 15
    PARTIAL = 16
    LOCKED = 17
    STATE = 18


class Failure(RuntimeError):
    def __init__(self, category):
        self.category = Category(category)
        super().__init__(self.category.name)


class MissingSource(Failure):
    """Definitive GET-message 404, distinct from transient Gmail unavailability."""
    def __init__(self):
        super().__init__(Category.PARTIAL)


class ModelUnavailable(Failure):
    """No usable model output (process, timeout, provider); may be transient."""
    def __init__(self):
        super().__init__(Category.MODEL_UNAVAILABLE)


class ModelContractError(Failure):
    """Output exists but violates the strict contract; bounded, never retried in-run."""
    def __init__(self):
        super().__init__(Category.MODEL_UNAVAILABLE)


class DeliveryUncertain(Failure):
    """Telegram outcome unknown; never replayed without operator reconciliation."""
    def __init__(self):
        super().__init__(Category.TELEGRAM_SEND)


def category_of(error, fallback=Category.UNKNOWN):
    return error.category if isinstance(error, Failure) else fallback


@contextmanager
def stage(category):
    try:
        yield
    except Failure:
        raise
    except Exception:
        raise Failure(category) from None


BANGKOK = timezone(timedelta(hours=7), "Asia/Bangkok")  # no DST; no host tz dependency


def local_day(stamp):
    return datetime.fromtimestamp(stamp, BANGKOK).date().isoformat()


def due(stamp):
    return datetime.fromtimestamp(stamp, BANGKOK).hour >= 7


def seconds_until_due(stamp):
    """Seconds until today's 07:00 Bangkok; 0 when already due."""
    moment = datetime.fromtimestamp(stamp, BANGKOK)
    if moment.hour >= 7:
        return 0.0
    return (moment.replace(hour=7, minute=0, second=0, microsecond=0) - moment).total_seconds()


def transport_environment():
    env = {k: v for k, v in os.environ.items()
           if k in {"HOME", "PATH", "LANG", "LC_ALL", "XDG_CONFIG_HOME"}}
    env.update(OPENCLAW_LOG_LEVEL="silent", OPENCLAW_RAW_STREAM="0")
    return env


def telegram_ready(value, account):
    # v2026.9.5 healthCommand writes HealthSummary directly (no result wrapper).
    # The CLI connects with least-privilege operator.read, so the Gateway strips
    # successful per-account probes (gateway/health/collector.ts) and keeps only
    # redacted probe failures. Readiness therefore uses public runtime fields:
    # configured/enabled/running/connected plus the collector's healthState,
    # which is set whenever channel-health-policy evaluates the account as
    # unhealthy. A timeout record has no `configured` and fails closed. This is
    # outbound-account readiness, not proof that a later delivery succeeds.
    if not isinstance(value, dict) or value.get("ok") is not True:
        return False
    channels = value.get("channels")
    telegram = channels.get("telegram") if isinstance(channels, dict) else None
    accounts = telegram.get("accounts") if isinstance(telegram, dict) else None
    selected = accounts.get(account) if isinstance(accounts, dict) else None
    if not isinstance(selected, dict):
        return False
    probe = selected.get("probe")
    return (selected.get("configured") is True
            and selected.get("enabled", True) is True
            and selected.get("running") is True
            and selected.get("connected", True) is True
            and selected.get("healthState") is None
            and selected.get("lifecycle") in (None, "ready")
            and (probe is None or isinstance(probe, dict) and probe.get("ok") is True))


HEALTH_RPC_TIMEOUT_MS = 10000  # Gateway caps collection at 7s; keep the RPC above it.


def wait_ready(account, *, timeout=180, clock=None, sleep=None, run=None):
    clock, sleep, run = clock or time.monotonic, sleep or time.sleep, run or subprocess.run
    deadline = clock() + timeout
    category = Category.GATEWAY_NOT_READY
    while clock() < deadline:
        remaining = deadline - clock()
        category = Category.GATEWAY_NOT_READY
        try:
            result = run(["openclaw", "health", "--json", "--verbose", "--timeout",
                          str(HEALTH_RPC_TIMEOUT_MS)],
                         stdin=subprocess.DEVNULL, capture_output=True, text=True,
                         encoding="utf-8", env=transport_environment(),
                         timeout=min(15, remaining), check=False)
            if result.returncode == 0:
                value = json.loads(result.stdout)
                if telegram_ready(value, account):
                    return
                if isinstance(value, dict) and value.get("ok") is True and isinstance(value.get("channels"), dict):
                    category = Category.TELEGRAM_SEND
        except Exception:
            pass  # bounded wait; never print child output or exception
        remaining = deadline - clock()
        if remaining > 0:
            sleep(min(5, remaining))
    raise Failure(category)


def private_path(path, *, directory=False):
    path = Path(path).expanduser()
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise Failure(Category.STATE)
    info = path.stat()
    if (info.st_uid != os.getuid() or info.st_mode & 0o077
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
        raise Failure(Category.STATE)
    return path


def read_private(directory, name, default):
    with stage(Category.STATE):
        private_path(directory, directory=True)
        path = Path(directory) / name
        if path.is_symlink():
            raise Failure(Category.STATE)
        if not path.exists():
            return default
        private_path(path)
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise Failure(Category.STATE)
                result[key] = value
            return result
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)


def write_private(directory, name, value):
    with stage(Category.STATE):
        private_path(directory, directory=True)
        path = Path(directory) / name
        if path.exists() or path.is_symlink():
            private_path(path)
        fd, temporary = tempfile.mkstemp(prefix=".checkpoint-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(value, stream, ensure_ascii=True, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


@contextmanager
def private_lock(directory, name, *, timeout=0, clock=None, sleep=None):
    import fcntl
    clock, sleep = clock or time.monotonic, sleep or time.sleep
    with stage(Category.STATE):
        private_path(directory, directory=True)
        path = Path(directory) / name
        if path.exists() or path.is_symlink():
            private_path(path)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        deadline = clock() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if clock() >= deadline:
                    raise Failure(Category.LOCKED) from None
                sleep(min(0.25, deadline - clock()))
            except OSError:
                raise Failure(Category.STATE) from None
        yield
    finally:
        os.close(fd)  # never unlink a lock inode
