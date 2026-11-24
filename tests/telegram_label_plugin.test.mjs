import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtempSync, readFileSync, statSync, writeFileSync, chmodSync, rmSync, symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import plugin, { createHandler, privateEnvironment, worker } from "../deploy/openclaw/email-label-approval/index.mjs";
import replyPlugin, { createHandler as replyHandler, privateEnvironment as replyEnvironment } from "../deploy/openclaw/email-reply-approval/index.mjs";

const env = { TELEGRAM_APPROVER_USER_ID: "123456", TELEGRAM_TARGET_CHAT_ID: "123456",
  EMAIL_AGENT_AUTO_SEND: "false", EMAIL_AGENT_LABEL_APPROVAL: "true" };
const config = { environmentFile: "/private/fixture.json", workerPath: "/private/worker.py", pythonPath: "/usr/bin/python3" };
function context() {
  const edits = [], replies = [];
  return { channel: "telegram", auth: { isAuthorizedSender: true }, isGroup: false, isForum: false,
    senderId: "123456", accountId: "default", callback: { namespace: "emaillabel",
      chatId: "123456", data: "emaillabel:" + "a".repeat(32), messageText: "FAKE PRIVATE BODY" },
    respond: { editMessage: async r => edits.push(r), reply: async r => replies.push(r) }, edits, replies };
}

test("label package declares the existing ESM entrypoint for the pinned OpenClaw version", () => {
  const root = new URL("../deploy/openclaw/email-label-approval/", import.meta.url);
  const pkg = JSON.parse(readFileSync(new URL("package.json", root), "utf8"));
  const manifest = JSON.parse(readFileSync(new URL("openclaw.plugin.json", root), "utf8"));
  assert.equal(pkg.name, "email-label-approval");
  assert.equal(pkg.name, manifest.id);
  assert.equal(pkg.version, "0.1.0");
  assert.equal(pkg.type, "module");
  assert.equal(pkg.private, true);
  assert.deepEqual(pkg.peerDependencies, { openclaw: "2026.9.5" });
  assert.deepEqual(pkg.openclaw.extensions, ["./index.mjs"]);
  assert.ok(statSync(new URL(pkg.openclaw.extensions[0], root)).isFile());
});

test("label namespace claimed even with no configuration", async () => {
  let registration;
  plugin.register({ registerInteractiveHandler: r => { registration = r; } });
  assert.equal(registration.namespace, "emaillabel");
  assert.equal(registration.channel, "telegram");
  const ctx = context();
  assert.deepEqual(await registration.handler(ctx), { handled: true });
  assert.equal(ctx.edits.length + ctx.replies.length, 0);
});

test("authorized label callback only passes trusted metadata to worker", async () => {
  let calls = 0;
  const ctx = context();
  const handler = createHandler(config, async (c, e, event) => {
    calls++;
    assert.deepEqual(event, { channel: "telegram", account: "default", user: "123456", chat: "123456",
      authorized: true, private: true, data: ctx.callback.data });
    return { text: "Preview", buttons: [] };
  }, () => env);
  assert.deepEqual(await handler(ctx), { handled: true });
  assert.equal(calls, 1);
  assert.equal(ctx.edits.length, 1);
});

test("unauthorized channel sender chat bot group forum topic never starts worker", async () => {
  let calls = 0;
  const handler = createHandler(config, async () => { calls++; }, () => env);
  for (const override of [{ channel: "slack" }, { senderId: "999" }, { accountId: "other" },
    { isGroup: true }, { isGroup: undefined }, { isForum: true }, { isForum: undefined },
    { threadId: 1 }, { threadId: "0" }, { callback: { chatId: "999" } },
    { auth: { isAuthorizedSender: false } }]) {
    const ctx = Object.assign(context(), override);
    assert.deepEqual(await handler(ctx), { handled: true });
    assert.equal(ctx.edits.length + ctx.replies.length, 0);
  }
  assert.equal(calls, 0);
});

test("invalid configuration and malformed callbacks fail closed", async () => {
  const broken = createHandler(config, () => assert.fail(), () => { throw new Error("FAKE OAUTH TOKEN"); });
  const ctx = context();
  assert.deepEqual(await broken(ctx), { handled: true });
  assert.equal(ctx.edits.length + ctx.replies.length, 0);
  const handler = createHandler(config, () => assert.fail(), () => env);
  for (const data of ["emaillabel:confirm", "emaillabel:../secret", "emaillabel:" + "a".repeat(31)]) {
    const item = context(); item.callback.data = data;
    assert.deepEqual(await handler(item), { handled: true });
  }
});

test("reply plugin does not execute label callbacks and label plugin does not execute reply callbacks", async () => {
  let replyRegistration, labelRegistration;
  replyPlugin.register({ registerInteractiveHandler: r => { replyRegistration = r; } });
  plugin.register({ registerInteractiveHandler: r => { labelRegistration = r; } });
  assert.notEqual(replyRegistration.namespace, labelRegistration.namespace);
  const reply = replyHandler(config, () => assert.fail(), () => env);
  assert.deepEqual(await reply(context()), { handled: true });
  const ctx = context(); ctx.callback.namespace = "emailreply"; ctx.callback.data = "emailreply:" + "b".repeat(32);
  assert.deepEqual(await createHandler(config, () => assert.fail(), () => env)(ctx), { handled: true });
  assert.equal(ctx.edits.length + ctx.replies.length, 0);
});

test("worker failure and Telegram edit failure never retry or leak details", async () => {
  for (const workerFails of [true, false]) {
    let calls = 0;
    const ctx = context();
    ctx.respond.editMessage = async () => { throw new Error("FAKE PRIVATE BODY"); };
    const handler = createHandler(config, async () => {
      calls++;
      if (workerFails) throw new Error("FAKE OAUTH TOKEN");
      return { text: "Preview", buttons: [] };
    }, () => env);
    assert.deepEqual(await handler(ctx), { handled: true });
    assert.equal(calls, 1);
    assert.equal(ctx.replies.length, 1);
    assert.match(ctx.replies[0].text, /thủ công/);
    assert.doesNotMatch(JSON.stringify(ctx.replies), /FAKE/);
  }
});

test("private writer environment is isolated from reply worker and gateway secrets", { skip: process.platform === "win32" }, () => {
  const dir = mkdtempSync(join(tmpdir(), "label-plugin-test-"));
  const path = join(dir, "environment.json");
  const previous = process.env.TELEGRAM_BOT_TOKEN;
  try {
    writeFileSync(path, JSON.stringify({ ...env, GMAIL_LABEL_AUTHORIZED_USER_FILE: "/private/fake-writer.json" }), { mode: 0o600 });
    process.env.TELEGRAM_BOT_TOKEN = "FAKE TOKEN";
    assert.equal(privateEnvironment(path).TELEGRAM_BOT_TOKEN, undefined);
    assert.equal(privateEnvironment(path).GMAIL_LABEL_AUTHORIZED_USER_FILE, "/private/fake-writer.json");
    assert.throws(() => replyEnvironment(path));
    chmodSync(path, 0o644);
    assert.throws(() => privateEnvironment(path));
    chmodSync(path, 0o600);
    const link = join(dir, "link.json"); symlinkSync(path, link);
    assert.throws(() => privateEnvironment(link));
    for (const extra of [{ TELEGRAM_BOT_TOKEN: "FAKE" }, { GOG_KEYRING_PASSWORD: "FAKE" },
      { EMAIL_AGENT_AUTO_SEND: "true" }, { TELEGRAM_TARGET_CHAT_ID: "-123456" }]) {
      writeFileSync(path, JSON.stringify({ ...env, ...extra }));
      assert.throws(() => privateEnvironment(path));
    }
  } finally {
    if (previous === undefined) delete process.env.TELEGRAM_BOT_TOKEN;
    else process.env.TELEGRAM_BOT_TOKEN = previous;
    rmSync(dir, { recursive: true });
  }
});

test("worker rejects relative executables without running", async () => {
  await assert.rejects(worker({ pythonPath: "python", workerPath: "worker.py" }, {}, {}));
});

test("worker rejects cross-namespace output, oversized output and errors with fake children", async () => {
  for (const output of [JSON.stringify({ text: "Preview", buttons: [[{ text: "Go", callback_data: "emailreply:" + "a".repeat(32) }]] }),
    "not json", "x".repeat(17000)]) {
    const fakeConfig = { pythonPath: process.execPath, workerPath: "--eval" };
    // Use a temporary mock worker: no Gmail, model, Telegram or runtime config.
    const dir = mkdtempSync(join(tmpdir(), "label-worker-test-"));
    try {
      const script = join(dir, "fixture.cjs");
      writeFileSync(script, `process.stdin.resume(); process.stdin.on('end', () => process.stdout.write(${JSON.stringify(output)}));`);
      await assert.rejects(worker({ ...fakeConfig, workerPath: script }, {}, {}));
    } finally { rmSync(dir, { recursive: true }); }
  }
});
