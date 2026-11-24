#!/usr/bin/env python3
"""Watch registration/renewal and Pub/Sub StreamingPull; stdout = statuses only."""
import argparse
from concurrent.futures import TimeoutError
import json
import logging
import os
import re
import threading

from new_email_pipeline import (Pipeline, Store, decode_notification, pipeline_lock,
                                realtime_directory, require)
from realtime_email_adapters import gmail_from_environment, notify_telegram, summarize_openclaw, telegram_target
from read_latest_email_from_sender import PipelineError, SENDER_PATTERN


def status(name):
    print(json.dumps({"status": name}), flush=True)


def configuration():
    account = os.environ.get("GOG_ACCOUNT", "")
    project = os.environ.get("GCP_PROJECT_ID", "")
    require(bool(SENDER_PATTERN.fullmatch(account)), "account_email_required")
    require(bool(re.fullmatch(r"[a-z][a-z0-9-]{4,61}[a-z0-9]", project)), "project_required")
    resources = []
    for env, kind in (("GMAIL_PUBSUB_TOPIC", "topics"), ("GMAIL_PUBSUB_SUBSCRIPTION", "subscriptions")):
        value = os.environ.get(env, "")
        prefix = f"projects/{project}/{kind}/"
        require(value.startswith(prefix) and bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9._~+%-]{2,254}",
                                                             value[len(prefix):])), "pubsub_config_invalid")
        resources.append(value)
    mode = os.environ.get("EMAIL_AGENT_SUMMARY_MODE", "none")
    require(mode in ("none", "openclaw"), "summary_mode_invalid")
    return account, resources[0], resources[1], mode


def receive(message, pipeline, mutex):
    try:
        history = decode_notification(message.data, pipeline.store.account)
        with mutex, pipeline_lock(pipeline.store.directory):
            pipeline.discover(history)
        # ACK means durable work accepted, not model success or Telegram delivery.
        message.ack()
        status("event_queued")
    except Exception:
        message.nack()
        status("event_retryable")


def subscribe(pipeline, subscription):
    from google.cloud import pubsub_v1

    mutex = threading.Lock()
    # One callback at a time; the SDK extends the ack lease during history work.
    with pubsub_v1.SubscriberClient() as client:
        future = client.subscribe(subscription,
                                  callback=lambda message: receive(message, pipeline, mutex),
                                  flow_control=pubsub_v1.types.FlowControl(max_messages=1))
        try:
            while True:
                with mutex, pipeline_lock(pipeline.store.directory):
                    pending = [key for key, value in pipeline.store.load()["messages"].items()
                               if value in ("pending", "retryable")]
                retries = 0
                for message_id in pending:
                    # Release locks between messages so callbacks/renewal can
                    # progress even while a large backlog is being summarized.
                    with mutex, pipeline_lock(pipeline.store.directory):
                        retries += pipeline.process_pending({message_id})
                status("work_retryable" if retries else "work_checked")
                try:
                    # Only retry our durable pending work, never poll Gmail for new mail.
                    future.result(timeout=30)
                    return
                except TimeoutError:
                    pass
        finally:
            future.cancel()
            future.result(timeout=60)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("register-watch", "renew-watch", "subscribe", "retry-pending"))
    args = parser.parse_args(argv)
    # Third-party exceptions/debug logs may contain credentials. Emit only our statuses.
    logging.disable(logging.CRITICAL)
    try:
        require(os.name == "posix", "linux_wsl_required")
        account, topic, subscription, mode = configuration()
        if args.command in ("subscribe", "retry-pending"):
            telegram_target()
        gmail, secrets = gmail_from_environment()
        pipeline = Pipeline(Store(realtime_directory(), account), gmail,
                            summarize_openclaw if mode == "openclaw" else None, secrets,
                            notify=notify_telegram)
        with pipeline_lock(pipeline.store.directory):
            # Corrupt state stops before any Gmail request.
            pipeline.store.load()
            profile = gmail.profile()
            require(isinstance(profile, dict) and isinstance(profile.get("emailAddress"), str)
                    and profile["emailAddress"].casefold() == account.casefold(), "account_mismatch")
            if args.command in ("register-watch", "renew-watch"):
                pipeline.renew(topic)
                status("watch_renewed")
                return 0
            if args.command == "retry-pending":
                retries = pipeline.process_pending()
                status("work_retryable" if retries else "work_checked")
                return 4 if retries else 0
            # Catch up after restart even if the last push was lost.
            pipeline.discover()
        subscribe(pipeline, subscription)
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        safe = {"state_invalid_manual_review", "history_expired_manual_resync_required",
                "watch_registration_required", "account_mismatch", "linux_wsl_required"}
        status(str(exc) if isinstance(exc, PipelineError) and str(exc) in safe else "realtime_failed")
        return 8


if __name__ == "__main__":
    raise SystemExit(main())
