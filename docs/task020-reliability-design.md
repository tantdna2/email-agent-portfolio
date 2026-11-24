# Daily digest reliability design

This design covers the daily scheduler, dependency readiness, delivery checkpoints and failure alerts. [Deployment and recovery](task020-antigravity-runbook.md)

## Error contract and bounds

The daily journal contains only controlled `code`/`category` values. Child output,
exception strings, routing IDs, source IDs and email contents never enter it.

| Code | Category | Meaning |
| --- | --- | --- |
| 0 | COMPLETED / ALREADY_COMPLETED / NOT_DUE / ATTENTION_PENDING | Safe successful/no-op exit (ATTENTION_PENDING: no automatic work left; the run that found it already failed) |
| 8 | UNKNOWN | Unclassified exception; retained compatibility |
| 10 | CONFIG | Required settings, account binding or safe model config invalid |
| 11 | GMAIL_AUTH | HTTP 401 / OAuth RefreshError (including invalid_grant) |
| 12 | GMAIL_UNAVAILABLE | Read API/network/response failure |
| 13 | GATEWAY_NOT_READY | Gateway API readiness deadline |
| 14 | TELEGRAM_SEND | Selected account probe unavailable or failed/ambiguous delivery |
| 15 | MODEL_UNAVAILABLE | Initial inference unavailable or model contract failed |
| 16 | PARTIAL | Mixed batch or invalid source; details stay in private ledger |
| 17 | LOCKED | Pipeline lock wait exhausted |
| 18 | STATE | Checkpoint, permission or filesystem failure |

No conflict with legacy scripts: this is the daily entrypoint's contract.
Revision diagnostics retain TASK-019's `R_*` codes and one attempt per request.

Readiness polls `openclaw health --json --verbose --timeout 10000` for at most
180 seconds, including each child timeout (15s). The RPC timeout stays above the
Gateway's fixed 7s collection cap. The CLI connects with least-privilege
`operator.read`; for such callers the Gateway deletes successful per-account
`probe` objects and keeps only redacted failures (review F-01; collector.ts
`buildHealthAccountRecord`, server-methods/health.ts `includeSensitive`,
OpenClaw's own health.snapshot test expects `probe` undefined). Readiness
therefore requires, for `channels.telegram.accounts[configuredAccount]`:
`configured === true`, `enabled` absent or true, `running === true`,
`connected` absent or true, no `healthState` (set by the collector whenever
channel-health-policy evaluates the account unhealthy), `lifecycle` absent or
`ready`, and a `probe`, if present, with `ok === true`. Admin scope is neither
requested nor needed. The timeout record (no `configured`) and any unknown shape
fail closed. Gateway up but account not ready is TELEGRAM_SEND; no Gateway is
GATEWAY_NOT_READY. Readiness is not proof that a later delivery succeeds; the
delivery checkpoints below own that outcome.

Verified pinned primary sources (2026.9.5):

- [health command: direct HealthSummary JSON, verbose probe](https://github.com/openclaw/openclaw/blob/v2026.9.5/src/commands/health.ts)
- [HealthSummary and per-account map](https://github.com/openclaw/openclaw/blob/v2026.9.5/src/gateway/health/types.ts)
- [collector: configured accounts and probe results](https://github.com/openclaw/openclaw/blob/v2026.9.5/src/gateway/health/collector.ts)
- [Telegram probeAccount adapter](https://github.com/openclaw/openclaw/blob/v2026.9.5/extensions/telegram/src/channel.ts)
- [health RPC audience: includeSensitive only for operator.admin](https://github.com/openclaw/openclaw/blob/v2026.9.5/src/gateway/server-methods/health.ts)
- [channel health policy and healthState](https://github.com/openclaw/openclaw/blob/v2026.9.5/src/gateway/channel-health-policy.ts)
- [CLI JSON failure envelope](https://github.com/openclaw/openclaw/blob/v2026.9.5/src/cli/failure-output.ts)

Gmail GET: maximum three attempts, 20s request timeout, 1+3s backoff; OAuth
refresh transport has a separate 20s timeout. Retryable OAuth server failures
are availability errors. No retries for 401, non-retryable RefreshError,
non-transient status, malformed response or POST. No Gmail-send code changed.
Google AuthorizedSession can perform its own bounded authentication refresh.
These are transport timeouts, not a claim of a hard wall-clock deadline across
all pages, DNS resolution and OAuth internals.

Initial summary+draft: maximum three model processes. Each has 30s help and
330s process limits; backoff 2+5s, maximum 1,087s per initial operation excluding
local validation/storage. Retry only the verified no-output envelope that
OpenClaw v2026.9.5 `agent --local --json` prints for the upstream
`400 [400]: {"error":{"type":"server_error","message":"Upstream request failed:
Model is unavailable."}}` (review F-02): exit code 1 and stdout exactly
`{"ok":false,"error":{"type":"cli_error","message":"LLM request failed: provider
rejected the request schema or tool payload."}}` with no payload. The failover
error is thrown (assistant-failure.ts), entry.ts writes formatCliJsonFailure,
error-text.ts renders the schema-rejection copy. Limitation: the copy masks the
upstream cause and safe config keeps `logging.level=silent`, so this envelope
also covers genuine schema rejections; a bounded retry is an operator-approved
trade-off, not proof that the failure was transient. Other exits, timeouts, CLI
errors, extra keys or any payload are not retried. Revision keeps one attempt;
Gmail send never retries. Process/timeout failures are `ModelUnavailable`
(transient); output that exists but violates the contract is
`ModelContractError` (bounded, see manual review).

The first useful inference checks model availability. A running local router, a model list or a healthy Gateway only checks that component.

## Schedule and checkpoints

Single daily timer: `07:00 Asia/Bangkok`, `Persistent=true`, `OnStartupSec=90s`.
Before 07:00 the startup invocation exits 0 before constructing Store/Gmail;
the calendar timer remains armed until 07:00. systemd merges a calendar trigger
into a start job that is still activating, so a startup run beginning within
120s before 07:00 waits for the boundary and performs the report itself instead
of exiting NOT_DUE and swallowing it (review F-13; 06:59:59 sleeps 1s, 07:00:00
and later run immediately, 06:57:59 exits NOT_DUE). Every activation is one
bounded attempt with Restart=no and no OnFailure loop; a fast failure can be
repeated at most once more in the same boot by the other trigger. After 07:00 a startup or missed
calendar activation runs recovery. The manager may coalesce simultaneous events
or invoke the service again after it exits. The ledger, not event counts,
provides deduplication. Completed days are silent, including later arrivals;
those arrivals belong to the next reporting period.

[systemd v255 semantics](https://github.com/systemd/systemd/blob/v255/man/systemd.timer.xml)
define OnStartupSec relative to manager startup and immediate expiration when
already past. CI exercises real combined timers with temporary fake services,
a fake Bangkok clock and PrivateNetwork, including a missed persistent stamp.
Verify real WSL startup and shutdown behavior separately for a WSL deployment.

`state.json` schema 1 is unchanged. The same `pipeline.lock` inode serializes
report, callbacks and edit metadata. Daily lock waits at most 120s; interactive
workers keep the existing nonblocking fail-closed behavior. No lock is unlinked.
Separate `daily-ledger.json` and `edit-session.json` use owner-only checks,
symlink refusal, atomic replacement, file fsync, directory fsync and 0600 mode.

The ledger stores Bangkok days, frozen batch IDs, private raw/normalized source
snapshots, bounded failure counters, manual-review status and Telegram delivery
states. Sources are checkpointed during the successful Gmail query; no inference
starts until a complete batch plan is durable. The existing bootstrap `since`
anchor and old state records are preserved. Partial batches resume from their
frozen IDs; new arrivals wait for the next period.

Each overview, no-mail notice, per-email draft and manual notice has a stable
delivery key. Write `inflight` before invoking Telegram, then `sent` after a
successful command. A durable sent receipt repairs a missing state reported flag
without resending or regenerating. A crash/exception during delivery becomes
`uncertain`; automatic recovery never replays it. Telegram
offers no transaction with these files: exactly-once is NOT promised.

Isolation (review F-03/F-04/F-05): every email is processed independently and
only STATE failures abort a batch. After the first failed delivery in a run, the
remaining deliveries of that run are deferred WITHOUT recording intent (a
circuit), so a Gateway outage cannot turn the whole batch into uncertain items;
generation continues and is checkpointed. An uncertain overview or manual notice
never blocks drafts. Uncertain draft/manual keys are excluded from every later
batch plan, so they never block other emails or later days. The day on which the
uncertainty arose is recorded as `uncertain` and is never COMPLETED; later days
complete normally and their overview/no-mail text carries a count-only reminder
("Cần đối soát thủ công: N ..."). A rerun of a `manual`/`uncertain` day has no
automatic work left and exits 0 ATTENTION_PENDING silently; the run that found
the problem already exited non-zero for the alert.

Reconciliation is manual and explicit: `scripts/reconcile_daily_delivery.py`
lists unresolved keys (read-only default; never invokes Telegram, Gmail or a
model). With `--apply`, an exact key and, for drafts, the exact current version:
`--confirm-delivered` records a card the operator saw; `--allow-redelivery`
(card verified absent) rotates the draft's tokens so any stray old buttons are
STALE, then lets the next run send one fresh card. Sent, skipped, failed, expired
and send_uncertain records are never re-armed; callbacks are never re-armed.

Manual review (review F-06): permanent failures (invalid/unparseable/deleted
source, model output violating the contract) count up to three attempts.
Transient failures (model process/provider/timeout) count once per distinct
Bangkok day; three distinct days also lead to manual review. Reopening Ubuntu
several times during one outage cannot exhaust a valid email. Manual review
sends one notice without Gmail action buttons; it is not processed-success.

Data minimization (review F-09): the raw Gmail snapshot and normalized email of
a source are dropped only when its draft is resolved (sent/skipped/expired),
reported, delivery-confirmed and not under manual review; the ID tombstone
remains. Sources without a draft, manual review, unconfirmed Telegram keys and
failed/send_uncertain or unresolved drafts keep their evidence. The ledger
schema is 1 and includes a bounded `transient` day list.
Definitive per-message 404 retains its source ID (and any earlier snapshot) for
manual review. Missing state.json beside existing sidecars fails closed; it cannot
bootstrap away old send fences. Missing frozen-batch data also fails STATE.
`send_uncertain` and pre-dispatch `failed` drafts are no longer scrubbed by recover;
data already scrubbed by older releases cannot be reconstructed by this patch.

## Routing and failure alerts

Normal authorized private text first asks a worker for session metadata only.
Without a valid session, return the worker's fixed text and claim the message:
no session (guidance), expired session (closed, draft back to review),
busy worker/lock contention (try again later) or generic failure (review F-12). Content
never goes to the general agent. With a valid 15-minute session, pass the bound
edit token; Python rechecks it under pipeline.lock before Gmail/model IO. A target
switch, missing metadata, corrupt state, timeout or expired session fails closed.
Existing callbacks, approval checks, revision diagnostics and send fence remain.
The former test requiring all text to be an instruction was replaced with
separate greeting/session/edit tests; broad-agent fallthrough remains forbidden.

The alert handler depends only on stdlib and two stdlib-only safety modules;
it imports no Gmail, model or email processing modules. It waits up to 180s,
then tries only its fixed alert up to three times (60s each, 2+5s backoff).
400s service timeout covers short lock sections + 180 + 180 + 7 plus storage.
No OnFailure/restart loop is added; original service failure is never reset.

`pending-alert.json` contains only controlled values: run id, boot id, status
(running/pending/sending/sent/clear), category, `previous` (earliest undelivered
category) and a delivery claim (owner, boot, time). `alert.lock` is held only for
short read/modify/write sections, never while waiting for the Gateway or
invoking Telegram (review F-07). A handler claims the slot, sends unlocked, then
completes only if the slot still has the same id and owner, so a concurrent run
or handler is never overwritten; a claim older than 450s or from another boot is
stale and may be taken over. Daily entry marks a run before external work; spool
failure or contention never blocks the report (OnFailure still runs). Completion
clears the slot, failure makes it pending. A startup replay timer at 120s retries
pending alerts or interrupted prior-boot runs; a current-boot active run is not
mistaken for an interruption. A new run carries the earliest undelivered
category into `previous` (review F-08); the next alert text includes it ("Lỗi
trước chưa báo: ..."). A successful run clears the slot but retains `previous`
as private evidence. Fixed alerts can duplicate after lost Telegram
acknowledgments; they cannot send Gmail. If Gateway never becomes ready, journal
and the pending slot remain. If WSL stops, no delivery guarantee is possible.
Journal retention across boots must be configured operationally. Corrupt/unwritable
alert storage leaves only journal evidence and requires operator attention.

`TimeoutStartSec=infinity` already existed at the verified base and is retained,
not newly enabled. A batch of N valid independent initial turns may take
N * 1,087s plus Gmail/Telegram/checkpoint overhead; a fixed 45-minute cap would
truncate a valid backlog. Every external operation/retry is bounded, Restart=no.
Operator must review this existing setting during deployment; a future global
budget would require resumable batching, not arbitrary cancellation.

## Offline audit coverage

| Requested cases | Evidence |
| --- | --- |
| 1–4: empty/day dedup, one/multiple independent replies | CheckpointTests; DigestTests three-email end-to-end |
| 5–8: OAuth/network/unavailable model/invalid output | ReadRetryTests; ModelRetryTests; RevisionAdapterTests |
| 9–11: Telegram checkpoints and readiness deadlines | CheckpointTests crash tests; ReadinessTests |
| 12–16: early/late startup, repeated boot, failure/recovery | fake-clock RuntimeTests; CheckpointTests; real systemd sandbox |
| 17–20: greetings, edit TTL, identity and target race | Node daily plugin tests; CheckpointTests; existing authorization regressions |
| 21–22: duplicate Send and send_uncertain | unchanged DigestTests send fence/timeout/checkpoint regressions |
| 23–24: alert delayed readiness/timeout/restart | AlertTests; AlertSpoolTests; IsolationTests |
| 25: persistent invalid/deleted source and bad model contract | CheckpointTests bounded manual-review cases |
| Review F-01..F-13 | test_task020_review_fixes (B1, B1b, B2, B2b, B3, B4, C1-C3, reconciliation, minimization, F-12, F-13); public-health and real OpenClaw envelope fixtures; restored TASK-019 FailureAlertTests/FailureRoutingTests |
| 26–28: safe diagnostics and TASK-019/full regressions | RuntimeTests, IsolationTests, retained health/revision tests and CI |

Windows runs deliberately skip Linux permissions/flock/proc-fd tests. GitHub
Actions runs the complete Python and Node suites on Linux with fake services.
The timer sandbox verifies rendered units and observes real service activations;
before-07:00 calendar waiting and missed-calendar catch-up each produce exactly
one fake notice despite additional same-day starts. No real Gmail, Telegram,
OpenClaw or model calls are made.
