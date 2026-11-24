# Portfolio status

The daily sender digest reads new messages from a configured Gmail sender, prepares a summary and draft for each, and presents them for review in a private Telegram chat. A revision invalidates earlier approval buttons. An explicit approval of the current version can cross the Gmail send boundary once. The workflow records uncertain sends for manual reconciliation.

| Area | What is included |
| --- | --- |
| Daily digest | Linux user timers, Gmail read-only discovery, a tool-free OpenClaw drafting adapter, versioned review, delivery checkpoints and failure alerts. [Daily digest](daily-sender-digest.md) · [Systemd templates](../deploy/systemd/README.md) |
| Optional realtime integrations | Gmail Watch/Pub/Sub notifications and separate approval paths for replies and labels. [Realtime architecture](realtime-email-architecture.md) · [Reply approval](telegram-reply-approval.md) · [Label approval](gmail-label-approval.md) |
| Offline demo | A fictional email, a canned edit and an in-memory outbox. The original approval, revised approval and replay produce simulated send counts of 0, 1 and 1. [Quick start](../README.md#offline-quick-start) · [HTTP demo](../README.md#run-the-http-demo-with-docker) |
| Draft evaluation | 60 archived model responses for 30 synthetic held-out emails, with locked blind grades and an offline reporting command. [Evaluation archive](../evals/results/heldout-a3160ef/README.md) |
| Verification | Python and Node regression tests use synthetic fixtures and fake services. The CI workflow also renders and checks systemd units and exercises startup in a disposable sandbox. [Regression commands](../README.md#verification) · [CI workflow](../.github/workflows/task020-offline.yml) |
