# Gmail label suggestion and Telegram approval

The optional label workflow suggests one of three configured Gmail labels and applies it only after explicit approval. The writer uses a dedicated credential scoped to `gmail.modify`. [Realtime architecture](realtime-email-architecture.md)

## Operator flow

The existing notification text is unchanged. Independent opt-ins add
`[💬 Gợi ý trả lời] [🏷 Phân loại]`. Label approval works with reply approval off.
Notification creation only allocates an opaque action; it never calls the label
model or opens the writer credential. Keep `EMAIL_AGENT_SUMMARY_MODE=none` to
avoid the existing optional TASK-012 summary model on arrival.

Only pressing **🏷 Phân loại** re-fetches the source with the readonly reader and
invokes the existing OpenClaw local model path once. It receives untrusted email
data, a configured list, and a strict output contract: `{"choice":0}` where the
value is integer 0/1/2 or null. Duplicate keys, extra fields, booleans, floats,
names, IDs, Markdown and other invalid outputs fail closed. Code maps the index
to the configured list; the model cannot create a name or choose a Gmail ID.

The operator sees the recommendation and all three configured names, including
when AI returns none. Selecting any name shows the subject and chosen name in
**Xem trước — chưa gắn nhãn**. Only **✅ Gắn nhãn** can reach the writer.
Cancel, selection, suggestion, stale buttons and unauthorized callbacks never
write Gmail. A stale selection cannot change an existing preview.

## Private configuration and separate credentials

The subscriber and label worker must use the same private JSON allowlist:
`EMAIL_AGENT_LABEL_ALLOWLIST_JSON=["Công việc","Hóa đơn","Khách hàng"]`.
Exactly three distinct nonempty names are required. No names are business rules
in code or hard-coded in the model prompt; the prompt uses this configuration.
The ordered list is part of the durable scope fingerprint. Changing the list,
account, operator, chat or bot invalidates existing actions; do not delete state
to work around a mismatch. Retain tombstones when migrating configuration.

- Reader: existing `GMAIL_AUTHORIZED_USER_FILE`; its readonly scope boundary is
  unchanged and still rejects `gmail.modify`.
- Writer: **separate** `GMAIL_LABEL_AUTHORIZED_USER_FILE`, outside the checkout,
  owned by the runtime user, exactly 0600, with symlinks (including parent paths)
  rejected. It accepts `gmail.modify` plus optional identity scopes, not send,
  full-mail or other Gmail scopes. Refresh requests only `gmail.modify`.
- Writer credential is opened only after authorized preview confirmation and
  the durable uncertainty fence. The subscriber, reply worker and classification
  path do not load it. Never put the writer path into the reply environment or
  subscriber environment; only the label plugin's private environment gets it.
- Model uses the existing separate `OPENCLAW_CONFIG_PATH` / `OPENCLAW_STATE_DIR`
  with `tools.deny=["*"]`, silent console/file logging and `--local`. Gmail,
  Google, Pub/Sub, Telegram and gog credential environment variables are removed
  from the model child. No `--deliver`, tool use or model-driven Gmail/Telegram
  dispatch. Protect and audit OpenClaw's own session storage separately, as in
  TASK-013; raw body is never written to the label interaction store.

`gmail.modify` is a broader OAuth grant than this adapter's allowed operations.
The adapter exposes only preflight and single-message add. It reads profile,
labels and the minimal source message, then optionally POSTs that message's
`modify` endpoint. There is no generic request API or send/draft/trash/delete,
archive, mark-read, thread modification, label creation/rename/deletion operation.
The only write body is `{"addLabelIds":["<resolved USER ID>"],"removeLabelIds":[]}`.
HTTP redirects and automatic authorization replay are disabled for the POST.

Provision and authorize the writer credential separately from the read-only credential. Keep both outside Git.

## Native plugin configuration

Use the existing OpenClaw plugin installation mechanism for
`deploy/openclaw/email-label-approval`, its `openclaw.plugin.json` manifest and
`package.json` declaring `openclaw.extensions=["./index.mjs"]` for OpenClaw
`2026.9.5`.
Configure the plugin entry `email-label-approval` with absolute Linux
`pythonPath`, `workerPath` (`scripts/run_telegram_label_action.py`) and
`environmentFile`. Copy `environment.example.json` outside Git, fill private
values, and protect it with 0600 permissions. It is JSON, not a shell env file.
Use the same external XDG state root as the subscriber; fill the blank path
fields. The example is deliberately incomplete and cannot authorize callbacks.

Set `EMAIL_AGENT_LABEL_APPROVAL=true` only in the private subscriber and label
worker configurations after setup is reviewed. Keep `EMAIL_AGENT_AUTO_SEND=false`.
Do not change the 07:00 workflow, enable the subscriber or perform consent as
part of implementation validation.

The plugin registers native `registerInteractiveHandler` for Telegram namespace
**emaillabel**, even when its private config is unavailable. Callback data is
`emaillabel:<32 lowercase hex>` from 128 random bits; no source, subject, name,
Gmail label ID or credential is encoded. Reply retains its own `emailreply`
namespace and unchanged private environment allowlist. Neither worker processes
the other's namespace. There is no second getUpdates poller, webhook or text
command fallback.

The handler requires Telegram, gateway-authorized sender, exact operator ID,
exact private chat ID, exact bot account, explicit non-group/non-forum context,
and no topic/thread. Unauthorized callbacks return handled without a worker,
response, model or credential load. Config errors also remain handled. Worker
stdout is a private response pipe; stderr/errors are suppressed and never copied
to gateway logs. Telegram edit failure does not re-run the worker.

## Persistence and recovery

State lives outside Git under the realtime root's `label-interactions/` directory,
0700 with 0600 state/lock files. Atomic replacement, file and directory fsync,
and the existing Linux flock serialize notifications and callbacks.

`offered → classifying → choices → preview → label_uncertain → labeled`

TTL is 24 hours. `classifying` is persisted before source fetch/model invocation;
a crash becomes terminal failed on the next callback, never a second model turn.
Before loading writer credentials, confirmation purges temporary data and
persists/fsyncs terminal `label_uncertain`. Preflight rechecks the private list,
account, exact source/message thread binding, existence, non-DRAFT status, and
exactly one matching USER name/ID. System matches, duplicate names or IDs,
missing labels and preview/selection mismatches fail closed. The preview label
is checked against the selection before crossing the fence.

If the source already has the selected label, terminal `already_labeled` avoids
POST. Otherwise one POST adds one label and removes none. Success requires the
response to contain the same source/thread and the added label. Timeout, crash,
HTTP error, malformed response or failure to persist success leaves terminal
`label_uncertain` and never retries automatically. Preflight failures become
`label_failed`; expiration/cancellation also terminate the action.

All terminal records purge recommendation, selection, preview and subject/source
detail, retaining the source record key, expiry, status and opaque action
tombstones. A later notification cannot re-arm the interaction. Do not remove
uncertainty records or manually replay a callback: reconcile the exact message
and current labels in Gmail first. Manual recovery must preserve tombstones.

## Offline verification

Run full suites in Linux/WSL:

```sh
python3 -m unittest discover -s tests -v
node --test tests/*.test.mjs
git diff --check
```

Tests use fake sources, fake HTTP/model/Telegram adapters and temporary private
state/credential fixtures. They cover authorization, independent notification
flags, strict model schema, manual override/none, credential isolation, source
and preview binding, runtime USER resolution, exact write body/endpoints,
fsync/crash/timeout recovery, duplicate prevention, permissions, privacy and the
complete TASK-012/TASK-013 regression suites. No live services are called.
