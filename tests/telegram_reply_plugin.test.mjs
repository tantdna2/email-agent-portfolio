import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtempSync, writeFileSync, chmodSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import plugin, { createHandler, privateEnvironment, worker } from "../deploy/openclaw/email-reply-approval/index.mjs";

const env = { TELEGRAM_APPROVER_USER_ID: "123456", TELEGRAM_TARGET_CHAT_ID: "123456",
  EMAIL_AGENT_AUTO_SEND: "false", EMAIL_AGENT_REPLY_APPROVAL: "true" };
const config = { environmentFile: "/private/runtime.json", workerPath: "/private/worker.py", pythonPath: "/usr/bin/python3" };
function context() {
  const edits = [], replies = [];
  return { channel: "telegram", auth: { isAuthorizedSender: true }, isGroup: false, isForum: false,
    senderId: "123456", accountId: "default", callback: { namespace: "emailreply",
      chatId: "123456", data: "emailreply:" + "a".repeat(32), messageText: "PRIVATE EMAIL BODY" },
    respond: { editMessage: async r => edits.push(r), reply: async r => replies.push(r) }, edits, replies };
}

test("native namespace is registered even without private config", () => {
  let registration;
  plugin.register({ registerInteractiveHandler: r => { registration = r; } });
  assert.equal(registration.namespace, "emailreply");
  assert.equal(registration.channel, "telegram");
  assert.equal(typeof registration.handler, "function");
});

test("authorized callback uses metadata only and returns handled without submitText", async () => {
  let calls = 0;
  const ctx = context();
  const handler = createHandler(config, async (c, e, event) => {
    calls++;
    assert.equal(event.data, ctx.callback.data);
    assert.equal(event.authorized, true);
    assert.equal(JSON.stringify(event).includes("PRIVATE EMAIL BODY"), false);
    return { text: "Preview", buttons: [] };
  }, () => env);
  assert.deepEqual(await handler(ctx), { handled: true });
  assert.equal(calls, 1);
  assert.deepEqual(ctx.edits, [{ text: "Preview", buttons: [] }]);
});

test("user chat group topic channel account and gateway auth mismatches fail closed", async () => {
  let calls = 0;
  const handler = createHandler(config, async () => { calls++; }, () => env);
  for (const override of [{ senderId: "654321" }, { callback: { chatId: "654321" } },
    { isGroup: true }, { isForum: true }, { threadId: 1 }, { channel: "slack" },
    { accountId: "other" }, { auth: { isAuthorizedSender: false } }, { isGroup: undefined }]) {
    const ctx = Object.assign(context(), override);
    assert.deepEqual(await handler(ctx), { handled: true });
    assert.equal(ctx.edits.length + ctx.replies.length, 0);
  }
  assert.equal(calls, 0);
});

test("malformed namespace payload never falls through to agent", async () => {
  let calls = 0;
  const handler = createHandler(config, async () => { calls++; }, () => env);
  for (const data of ["emailreply:confirm", "emailreply:../secret", "other:" + "a".repeat(32)]) {
    const ctx = context(); ctx.callback.data = data;
    assert.deepEqual(await handler(ctx), { handled: true });
  }
  assert.equal(calls, 0);
});

test("missing configuration is handled silently without fallback", async () => {
  const ctx = context();
  const handler = createHandler(config, () => assert.fail(), () => { throw new Error("SECRET"); });
  assert.deepEqual(await handler(ctx), { handled: true });
  assert.equal(ctx.edits.length + ctx.replies.length, 0);
});

test("worker failure never retries, logs secrets, or claims no send", async () => {
  let calls = 0;
  const ctx = context();
  const handler = createHandler(config, async () => { calls++; throw new Error("SECRET BODY REPLY TOKEN"); }, () => env);
  assert.deepEqual(await handler(ctx), { handled: true });
  assert.equal(calls, 1);
  assert.equal(ctx.replies.length, 1);
  assert.match(ctx.replies[0].text, /thủ công/);
  assert.equal(JSON.stringify(ctx.replies).includes("SECRET"), false);
});

test("Telegram edit failure does not repeat worker or send", async () => {
  let calls = 0;
  const ctx = context(); ctx.respond.editMessage = async () => { throw new Error("private reply"); };
  const handler = createHandler(config, async () => { calls++; return { text: "PRIVATE", buttons: [] }; }, () => env);
  assert.deepEqual(await handler(ctx), { handled: true });
  assert.equal(calls, 1);
  assert.equal(ctx.replies.length, 1);
});

test("private environment validates permissions and never inherits bot credentials", { skip: process.platform === "win32" }, () => {
  const dir = mkdtempSync(join(tmpdir(), "email-plugin-test-"));
  const path = join(dir, "environment.json");
  const previous = process.env.TELEGRAM_BOT_TOKEN;
  try {
    writeFileSync(path, JSON.stringify(env), { mode: 0o600 });
    process.env.TELEGRAM_BOT_TOKEN = "FAKE SECRET";
    assert.equal(privateEnvironment(path).TELEGRAM_BOT_TOKEN, undefined);
    chmodSync(path, 0o644);
    assert.throws(() => privateEnvironment(path));
    chmodSync(path, 0o600);
    writeFileSync(path, JSON.stringify({ ...env, TELEGRAM_BOT_TOKEN: "FAKE SECRET" }));
    assert.throws(() => privateEnvironment(path));
  } finally {
    if (previous === undefined) delete process.env.TELEGRAM_BOT_TOKEN;
    else process.env.TELEGRAM_BOT_TOKEN = previous;
    rmSync(dir, { recursive: true });
  }
});

test("worker rejects relative executable paths without executing", async () => {
  await assert.rejects(worker({ pythonPath: "python", workerPath: "worker.py" }, {}, {}));
});
