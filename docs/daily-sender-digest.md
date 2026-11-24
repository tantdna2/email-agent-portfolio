# Daily sender digest and conversational reply drafts

The daily sender digest runs at 07:00 Asia/Bangkok with the supplied systemd timer. The reliability design covers startup, retries and checkpoint recovery. [Reliability design](task020-reliability-design.md) · [Deployment and recovery](task020-antigravity-runbook.md)

## Discovery and reporting

`read_new_emails_from_sender.py` paginates Gmail `messages.list` with an exact
sender query and read-only `messages.get`. It verifies the actual single From
address after GET, rejects duplicate/ambiguous From headers, and ignores unrelated
senders before decoding their bodies. It never modifies labels, read status,
drafts or messages. Gmail credentials refresh with the readonly scope. The old
latest-only reader is unchanged.

On first state creation, persist `since = run_time - 24 hours` before discovery.
`EMAIL_AGENT_INITIAL_LOOKBACK_HOURS` accepts only canonical integers 1..168,
defaults to 24, and rejects whitespace, signs, leading zeros, decimals and booleans.
No full mailbox history bootstrap occurs. The lower bound is retained after
success; subsequent runs enumerate all matching messages since that bound,
excluding successfully reported message IDs. This deliberately favors correctness
after downtime/late indexing over an advancing date cursor. Search cost grows
with the monitored sender's history since installation. `internalDate` enforces
the bootstrap boundary locally; output sorts oldest to newest, then message ID
for ties. Spam/trash are included; sent messages and Gmail drafts are excluded.

For N emails, the report has one header followed by N independent review messages.
Each initial model invocation receives exactly one source email and returns
exactly `summary` and `draft_body`. No new mail produces the short Vietnamese
no-mail notice and zero model calls. Source binding comes from validated Gmail
metadata and operator configuration, never model output.

The per-message outbox is the pending record in the atomic `state.json` file.
Persist the summary/draft and opaque buttons before Telegram delivery; mark
`reported=true` only after the OpenClaw outbound command succeeds. A failure for
one email does not discard other pending emails. A pending prepared draft is
reused on the next run without regenerating it. Initial generation failures stay
unreported and are eligible on the next run. The process exits nonzero with a
sanitized partial-report status if any item fails.

Telegram has no transaction with the local checkpoint. A crash or timeout after delivery starts can leave the outcome uncertain. The ledger retains the item for manual reconciliation and suppresses automatic replay. Protect the durable state and backups to retain delivery receipts and send fences.

## Native OpenClaw capability gate

The plugin uses these **OpenClaw 2026.9.5** interfaces.

- `OpenClawPluginApi.on("before_dispatch", handler, options)` is a native claiming
  hook. `PluginHookBeforeDispatchEvent` supplies content, sender, message ID and
  group flag; context supplies channel, account and resolved conversation ID.
- In `dispatch-from-config` the hook is awaited before normal model dispatch;
  `{handled:true}` short-circuits that path. Its reply contract is text-only.
- `message-hook-mappers` derives these identity fields from the admitted channel
  context, not user text. The Telegram `resolveTelegramInboundConversation`
  resolver uses a composite conversation ID for topic/thread contexts. Only an
  exact bare positive private chat ID is accepted by this plugin.
- Native `registerInteractiveHandler` provides callback authorization, private /
  group / forum / thread facts, and `respond.reply`. The plugin owns the
  `dailydraft` namespace, including stale buttons.
- Claiming-hook timeout is optional in the installed runner. This plugin omits
  host timeout and catches its bounded worker timeout, returning `handled:true`
  instead of falling through to a general model.

The plugin uses native hooks for text and button handling. The Gateway remains the only Telegram update consumer.

Verify that the Gateway enables the hook for the configured route. OpenClaw
can suppress takeover hooks on restricted admitted session settings, and another
higher-priority claiming plugin can take ownership first. Use a dedicated private
Telegram bot/account for this workflow and no competing inbound claiming plugins.
The Gateway route must admit the native hook; the separate model-only process
must retain tools-deny. If the deployed route cannot satisfy these checks, keep
the timer disabled. Do not substitute a second poller.

## Private plugin setup

Install `deploy/openclaw/daily-sender-digest` using the existing OpenClaw plugin
deployment process; it pins the supported peer version to 2026.9.5.
Configure three absolute paths in its plugin config:

- `environmentFile`: an external 0600 strict-JSON object of string environment
  values, corresponding to the daily env example. Do not point this at the
  systemd shell-style env file; create a JSON companion with the same values.
- `workerPath`: the checkout's `scripts/run_daily_reply_action.py`.
- `pythonPath`: the same Python interpreter used by the daily service.

The operator/user ID and private chat ID must be identical; account must match
the selected Telegram account. Set the Gateway's private sender allowlist to that
operator and disable groups. The callback additionally requires OpenClaw's
`isAuthorizedSender=true`. Ordinary text requires exact channel/account/user/chat
identities from the native hook. Unknown groups, topic conversations, accounts
and senders cannot reach the worker. This dedicated plugin claims Telegram text
fail-closed; without an active edit target it prompts the operator to select Edit.
Use a dedicated Telegram account for this workflow.

`EMAIL_AGENT_AUTO_SEND` must remain `false` in both configurations (`true` or
other explicit values fail). The scope fingerprint binds state to account,
TARGET_SENDER, operator, private chat and Telegram account; changing these requires
manual review, not automatic state reuse/reset. Credentials and state must be
outside the repo, owned by the process user, with directories 0700/files 0600.
Symlink paths and symlink ancestors are rejected. Use a durable Linux filesystem.

The model runs through the tool-free local OpenClaw adapter,
with a separate config/state, `tools.deny=["*"]`, silent logging, no deliver flag,
and a fresh session per invocation. Model subprocesses receive no Gmail or Telegram
environment credentials. The Gateway bot token is not inherited by Python. Review
the separate model instance's session/transcript retention, workspace, plugins,
hooks and provider tracing; the digest does not delete OpenClaw's session storage.

## Review state and safety

Each record contains immutable `source_message_id`, `thread_id`, recipient (`to`)
and reply subject, plus current draft, integer version, status, expiration,
summary/date, reported flag and opaque action tokens. There is no stored source
body, operator instruction or draft conversation history. Terminal reported drafts
and summaries are cleared; binding/ID tombstones remain for deduplication.

| Action / condition | Transition | Effect |
| --- | --- | --- |
| Prepared initial output | awaiting_review, v1 | Summary and exactly one draft |
| Edit | awaiting_edit_instruction | One active target per configured operator/chat |
| Edit another draft | Previous returns to awaiting_review | Target switches; neither draft changes/sends |
| Next authorized private text | revising | Consume Telegram message ID durably, clear active target, invalidate old buttons |
| Successful revision | awaiting_review, v+1 | Exactly one model invocation; only draft body changes |
| Revision failure/crash | awaiting_review | Preserve previous body; no automatic model retry |
| Current Send | send_uncertain, then sent if confirmed | Durable fence before dispatch |
| Skip | skipped | No model or Gmail send; duplicate is harmless |
| Review older than seven days | expired | No send |

Each revision refetches the original email read-only, validates its immutable
binding, and passes source/current draft/operator instruction to one invocation.
Initial generation still requires strict JSON with exactly `summary` and
`draft_body`. Revision uses `tool_free_text`: ONLY the revised email body as plain
text, no JSON, Markdown, commentary or code fences, and no subject/recipient/
signature fields unless already present in the current draft. The operator
instruction is trusted editing input; source email stays untrusted data.

The shared local OpenClaw runner keeps tools-deny, fresh sessions, credential
scrubbing and one invocation per revision. Initial JSON generation and revision
use the same `parse_model_payload` outer parser. It accepts local payloads or a
Gateway-style `status=ok` / `result` wrapper. Optional `meta` / `result.meta` may be
absent, `{}`, or `null`; null means no metadata, not a failure. It imposes no usage,
duration, model identity or other diagnostic metadata schema. Local/result status
may be absent/null/`ok`; a wrapper still requires explicit outer `status=ok`.
Malformed JSON/non-object envelopes and non-object non-null metadata fail closed.
Explicit error/isError/aborted indicators at outer/result/payload and metadata
levels, unsuccessful statuses, media and multiple/missing/non-text payloads are
rejected. These failure indicators cannot be hidden by null metadata elsewhere.
Revision payload text is
never parsed as JSON. `plain(..., 1800)` rejects HTML, code fences, invalid control
characters and secrets; revision validation also rejects JSON-shaped/scalar output,
tilde fences and recognizable English/Vietnamese wrapper commentary such as
"Here is the revised draft:". It never strips or repairs a wrapper. Accepted body
text is preserved verbatim. Commentary recognition is conservative pattern matching,
not a guarantee of semantic correctness; explicit operator review remains required.

Only the draft body is assigned from that text. Source/thread/to/subject remain
immutable. Two edits of v1 produce v2/v3 with two additional invocations and no
automatic model retry. Tokens are rotated and fsynced before model work; old Send
buttons fail closed. A rejected output or model failure preserves the previous
draft/version and returns to `awaiting_review`; duplicate text IDs cannot retry
generation or edit a subsequently selected target.

### Safe revision diagnostics

No state migration or diagnostic history is added. Only a fixed enum is exposed
in the authorized operator's fallback, for example `Mã: R_OUTER_ENVELOPE`.
Codes describe the failed boundary, not private exception details or root cause.
Never collect raw model output, source body, operator instruction or exception
strings to diagnose these codes.

| Code | Failed boundary |
| --- | --- |
| `R_SOURCE` | Read-only source/account fetch or immutable binding validation |
| `R_MODEL_PROCESS` | Local model configuration/preflight/process, nonzero exit or timeout |
| `R_OUTER_ENVELOPE` | Outer JSON/envelope/metadata shape or explicit failure indicators |
| `R_PAYLOAD` | Exactly-one-text payload shape, payload error/abort/media indicators |
| `R_TEXT_CONTRACT` | Revision body or editing-text validation |
| `R_STATE` | Durable state load, fence, checkpoint or recovery |

On failure, clear the active edit target, retain the previous draft/version and
return to `awaiting_review`; a failed final checkpoint also restores the prior
body/version. Old tokens remain invalidated. No model retry and no Gmail send.
Source/state failures before dispatch make zero model attempts; a revision that
reaches the model makes exactly one attempt. Unknown internal exceptions map to a
fixed safe code, never to their string representation. If storage keeps failing,
durable recovery cannot be promised: return `R_STATE` without controls and require
manual inspection. Do not erase/replace state or clear send fences to recover.

The parser accepts omitted or null metadata and rejects explicit error or abort indicators even when other metadata is absent.

Only current **✅ Gửi** on an awaiting-review draft dispatches. Before dispatch,
refetch and validate source ID/thread/From/Reply-To/subject, account and expiry.
The existing `send_approved` adapter invokes `scripts/send_gmail_reply.py` with
explicit `--send`; no generic Gmail sender is added. `send_uncertain` is persisted
using atomic replace and file/directory fsync before that call. Timeout, process
crash, unknown response or failure to persist success never re-arms sending.
Operator must inspect Gmail manually. Duplicate confirmation cannot send twice.

### Operational note: Distinct OAuth surfaces

The daily digest uses two independently managed Google OAuth credentials.

1. **Gmail reader**:
   - Authorized user JSON configured via `GMAIL_AUTHORIZED_USER_FILE`.
   - Used by the daily report runner and revision source validation via `google-auth`.
   - Scope: strictly `gmail.readonly` (`https://www.googleapis.com/auth/gmail.readonly`).
2. **Gmail send boundary**:
   - `gog` CLI file keyring configured via `GOG_KEYRING_PASSWORD` / `GOG_ACCOUNT`.
   - Used only by `send_gmail_reply.py` when an explicit operator Send callback is triggered.
   - Scope preset: `read-send` (`gmail.readonly` + `gmail.send`).

These two credentials must be maintained independently by the operator. Validity of the reader credential does not imply validity of the CLI keyring credential, or vice versa. If `gog` CLI authorization expires or is revoked, the send boundary fails closed at preflight, persisting a terminal `send_uncertain` state without automatic retries.

See [OAuth configuration](oauth-production.md) for scope, token lifetime and credential replacement details.

A process-wide lock serializes report, edit and send state. If a daily batch is
busy, worker requests fail closed with a safe retry/manual-review message. No raw
exception or email content is printed to logs. If revision delivery fails after
state persistence, the updated draft remains durable and old callbacks remain
invalid; operator recovery is required to re-present the current version. Never
use old tokens to authorize a new version or clear a send fence to recover UI.

## Recovery and evidence

For a partial daily report, rerun the daily entrypoint with the same protected
environment/state after fixing the dependency. Successful message IDs are skipped;
prepared records with no Telegram delivery intent can resume without a new model
call. TASK-020 never automatically replays `inflight`/`uncertain` Telegram delivery;
those outcomes require private operator reconciliation with the read-only-by-default
[reconciliation helper](task020-antigravity-runbook.md#reconcile-uncertain-telegram-delivery). An unresolved item never
blocks other emails or later days. A completed Bangkok day
is silent, and later arrivals wait for the next period. Never replace
corrupt state with an empty file. Stop scheduling and reconcile current backups
if state is damaged. Source validation failures require private operator review.

For `send_uncertain`, inspect Gmail Sent and the original thread manually. Do not
retry or clear the fence automatically. No runtime reconciliation tool that can
re-arm dispatch is supplied. Restore/re-present a draft only through an explicitly
reviewed maintenance procedure; do not roll state back across confirmed sends.

Offline tests cover exactly three target messages plus three foreign messages,
three independent summaries/drafts/model calls/review interactions, isolated edit,
send and skip, two revision rounds, stale v1/v2 and one v3 send, deduplication,
bootstrap bounds/downtime, checkpoint failures, uncertain sends, sender/metadata
tampering, authorization, scope mismatch, symlinks and file permissions. Run the [regression suites](../README.md#verification) to verify this checkout.
