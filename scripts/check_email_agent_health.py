#!/usr/bin/env python3
"""Read-only user-systemd snapshot. No credentials, application imports or writes."""
from datetime import datetime
import json
import os
import re
import subprocess


TIMER = "email-agent.timer"
SERVICE = "email-agent.service"
SUBSCRIBER = "email-agent-subscriber.service"
COMMON = ("LoadState", "ActiveState", "SubState", "UnitFileState")
PROPERTIES = {
    TIMER: COMMON + ("LastTriggerUSec", "LastTriggerUSecMonotonic"),
    SERVICE: COMMON + ("Result", "ExecMainCode", "ExecMainStatus",
                       "ExecMainStartTimestampMonotonic", "ExecMainExitTimestampMonotonic"),
    SUBSCRIBER: COMMON,
}
ACTIVE_STATES = {"active", "inactive", "failed", "activating", "deactivating", "reloading"}
FILE_STATES = {"enabled", "enabled-runtime", "disabled", "masked", "masked-runtime",
               "static", "indirect", "generated", "transient", "alias", "linked", "linked-runtime"}
FAILURE_RESULTS = {"exit-code", "signal", "core-dump", "timeout", "watchdog",
                   "start-limit-hit", "resources", "protocol", "oom-kill"}


def read_unit(unit):
    env = {key: value for key, value in os.environ.items()
           if key in {"HOME", "PATH", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"}}
    env.update(LC_ALL="C", TZ="Asia/Bangkok", SYSTEMD_COLORS="0")
    command = ["systemctl", "--user", "--no-pager", "show", unit,
               "--property=" + ",".join(PROPERTIES[unit])]
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, encoding="utf-8", env=env, timeout=5, check=False)
        if result.returncode != 0:
            return {}
        values = {}
        for line in result.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator and key in PROPERTIES[unit]:
                if key in values:
                    return {}  # Ambiguous output is not evidence.
                values[key] = value
        return values
    except Exception:
        return {}


def number(values, key):
    value = values.get(key, "")
    return int(value) if re.fullmatch(r"[0-9]{1,20}", value) else None


def observed(values, key, allowed):
    value = values.get(key)
    return value if value in allowed else "unknown"


def last_trigger(values):
    value = values.get("LastTriggerUSec", "")
    match = re.fullmatch(
        r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) "
        r"(UTC|GMT|[+-]\d{2}(?::?\d{2})?)", value)
    if match:
        try:
            datetime.strptime(match[1], "%Y-%m-%d %H:%M:%S")
            return match[1] + " " + match[2]
        except ValueError:
            pass
    return "unknown"


def service_state(values):
    if values.get("LoadState") != "loaded":
        return "unknown"
    active = values.get("ActiveState")
    result = values.get("Result")
    if active == "failed":
        return "failed"
    if ((active == "activating" and values.get("SubState") == "start")
            or (active == "active" and values.get("SubState") == "running")):
        return "running"
    if result in FAILURE_RESULTS:
        return "failed"
    start = number(values, "ExecMainStartTimestampMonotonic")
    end = number(values, "ExecMainExitTimestampMonotonic")
    code = number(values, "ExecMainCode")
    status = number(values, "ExecMainStatus")
    if active == "inactive" and code in {1, 2, 3} and status not in {None, 0}:
        return "failed"
    # A never-run oneshot also has inactive/Result=success. Require actual exit evidence.
    if (active == "inactive" and values.get("SubState") == "dead" and result == "success"
            and code == 1 and status == 0 and start is not None and end is not None
            and 0 < start <= end):
        return "completed"
    return "unknown"


def assess(timer, service, subscriber):
    timer_active = observed(timer, "ActiveState", ACTIVE_STATES)
    timer_enabled = observed(timer, "UnitFileState", FILE_STATES)
    realtime_active = observed(subscriber, "ActiveState", ACTIVE_STATES)
    realtime_enabled = observed(subscriber, "UnitFileState", FILE_STATES)
    state = service_state(service)
    trigger = last_trigger(timer)
    trigger_mono = number(timer, "LastTriggerUSecMonotonic")
    start = number(service, "ExecMainStartTimestampMonotonic")
    matches_timer = (trigger != "unknown" and trigger_mono is not None and start is not None
                     and 0 < trigger_mono <= start)
    timer_ok = (timer.get("LoadState") == "loaded" and timer_active == "active"
                and timer.get("SubState") == "waiting" and timer_enabled == "enabled")
    realtime_ok = (subscriber.get("LoadState") == "loaded" and realtime_active == "inactive"
                   and realtime_enabled == "disabled")
    error = (state == "failed"
             or any(v.get("LoadState") in {"not-found", "error", "bad-setting"}
                    for v in (timer, service, subscriber))
             or timer_active in {"inactive", "failed"}
             or timer_enabled in {"disabled", "masked", "masked-runtime", "enabled-runtime"}
             or realtime_active in {"active", "activating", "reloading", "failed"}
             or realtime_enabled in {"enabled", "enabled-runtime"})
    health = ("error" if error else "healthy" if timer_ok and realtime_ok
              and state == "completed" and matches_timer else "unknown")
    return {"health": health, "timer": {"active": timer_active, "enabled": timer_enabled},
            "service": state, "last_trigger": trigger,
            "realtime": {"active": realtime_active, "enabled": realtime_enabled}}


def main():
    result = assess(read_unit(TIMER), read_unit(SERVICE), read_unit(SUBSCRIBER))
    print(json.dumps(result), flush=True)
    return {"healthy": 0, "error": 1, "unknown": 2}[result["health"]]


if __name__ == "__main__":
    raise SystemExit(main())
