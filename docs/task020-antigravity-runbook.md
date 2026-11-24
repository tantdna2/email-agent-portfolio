# Daily digest deployment and recovery

The daily digest, OpenClaw Telegram plugin and systemd templates share a runtime user, interpreter and durable state. Review changes to the components together and verify them in a disposable Linux environment before putting a mailbox on the schedule. [Systemd templates](../deploy/systemd/README.md) · [Reliability design](task020-reliability-design.md)

## Deployment checks

1. Configure the [Gmail reader and send credentials](oauth-production.md) independently. Keep `EMAIL_AGENT_AUTO_SEND=false`. Keep durable state and credential files on a Linux filesystem outside the checkout with owner-only access. Back up the current state before changing components.
2. Configure one private OpenClaw Telegram account with one allowed operator. Set the subscriber and other report schedulers aside for the daily-only configuration. Confirm the `dailydraft` callback namespace and `before_dispatch` hook belong to the daily plugin. Use a separate tool-denied profile for drafting. [Plugin setup](daily-sender-digest.md#private-plugin-setup)
3. Render the systemd user units for this checkout and interpreter. Give monitoring its own minimal environment for Telegram routing and spool state. Run `systemd-analyze --user verify`, the offline suites and the systemd startup sandbox. [Systemd configuration](../deploy/systemd/README.md#units-and-configuration) · [Offline checks](../README.md#verification)
4. Check Gateway readiness and that the selected Telegram account is running. Check the route and auth settings without logging private details. The first scheduled draft generation exercises the model path for that environment. [Readiness contract](task020-reliability-design.md#error-contract-and-bounds)
5. Review the next 07:00 Asia/Bangkok trigger before enabling the daily and alert replay timers. With a persistent timer, a missed calendar run can fire immediately. Review the journal's fixed statuses and check that a completed reporting day stays complete across user manager restarts. [Schedule and checkpoints](task020-reliability-design.md#schedule-and-checkpoints)

## Reconcile uncertain Telegram delivery

Run `python3 scripts/reconcile_daily_delivery.py` as the service user with the daily environment loaded and no daily worker running. The default invocation lists unresolved delivery keys and can include message identifiers. Inspect this output privately.

- If the matching card is present, use `--key draft:<id> --version <v> --confirm-delivered` to preview recording a receipt. Add `--apply` after checking the key and draft version.
- If the card is missing, use `--key draft:<id> --version <v> --allow-redelivery` to preview making one future delivery eligible. Add `--apply` after checking the key and version. Old controls become stale. The next daily run can send a fresh card.
- Overview, no-mail and manual-review notices use the same two actions without a draft version.

The helper only updates local checkpoint state. Review items that exhaust the processing attempts in the operator interface. Keep the ledger and its source tombstones. [Delivery checkpoints](task020-reliability-design.md#schedule-and-checkpoints)

## Reconcile uncertain Gmail sends

For a `send_uncertain` draft, inspect Gmail Sent and the original thread manually. Retain the send fence even after a failed credential preflight or during rollback. See the [daily digest recovery notes](daily-sender-digest.md#recovery-and-evidence).

## Rollback

Stop the timer and quiesce the approval workers before switching source, plugin and units together. Retain and protect the current state, ledger, edit-session data and alert spool. Review compatibility before restarting the timer, especially if the older release lacks the current ledger format. Use current send fences and delivery receipts when resolving any pending records.
