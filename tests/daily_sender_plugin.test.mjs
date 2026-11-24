import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtempSync, writeFileSync, chmodSync, symlinkSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import plugin, { callbackHandler, textHandler, privateEnvironment, worker } from "../deploy/openclaw/daily-sender-digest/index.mjs";

const env = { TELEGRAM_APPROVER_USER_ID: "12345", TELEGRAM_TARGET_CHAT_ID: "12345",
  TELEGRAM_ACCOUNT_ID: "default", EMAIL_AGENT_AUTO_SEND: "false" };
const config = { environmentFile: "/private/example.json" };
const token = "dailydraft:" + "a".repeat(32);
function textEvent() {
  return [{ channel: "telegram", senderId: "12345", isGroup: false, messageId: "11", content: "viết ngắn hơn" },
    { channelId: "telegram", accountId: "default", conversationId: "12345", senderId: "12345", messageId: "11" }];
}
function callback() {
  const replies = [];
  return { channel: "telegram", accountId: "default", senderId: "12345", isGroup: false, isForum: false,
    auth: { isAuthorizedSender: true }, callback: { namespace: "dailydraft", chatId: "12345", data: token },
    respond: { reply: async value => replies.push(value) }, replies };
}

test("registers real native before_dispatch and interactive handler only", () => {
  const hooks = [], interactions = [];
  plugin.register({ pluginConfig: config, on: (...args) => hooks.push(args), registerInteractiveHandler: x => interactions.push(x) });
  assert.equal(hooks[0][0], "before_dispatch");
  assert.equal(hooks[0][2].timeoutMs, undefined);
  assert.equal(interactions[0].namespace, "dailydraft");
});

test("ordinary greeting probes session without forwarding text or invoking Gmail", async () => {
  const events = [];
  const handler = textHandler(config, async (_config, _env, event) => {
    events.push(event); return { text: "", buttons: [] };
  }, () => env);
  const [event, ctx] = textEvent();
  const result = await handler({ ...event, content: "Xin chào" }, ctx);
  assert.equal(result.handled, true);
  assert.match(result.text, /Email Agent đang hoạt động/);
  assert.equal(events.length, 1);
  assert.deepEqual(events[0], { channel: "telegram", account: "default", user: "12345", chat: "12345",
    authorized: true, private: true, kind: "edit_session" });
});

test("valid edit session forwards one instruction with the locked target binding", async () => {
  const events = [];
  const handler = textHandler(config, async (_config, _env, event) => {
    events.push(event);
    return event.kind === "edit_session" ? { text: "", buttons: [], edit_token: token }
      : { text: "Đã chỉnh", buttons: [] };
  }, () => env);
  assert.deepEqual(await handler(...textEvent()), { handled: true, text: "Đã chỉnh" });
  assert.equal(events.length, 2);
  assert.equal(events[1].kind, "text");
  assert.equal(events[1].edit_token, token);
  assert.equal(events[1].text, "viết ngắn hơn");
});

test("expired, busy and failed sessions return the worker's fixed text without forwarding content", async () => {
  for (const text of ["Phiên chỉnh sửa đã hết hạn sau 15 phút và chưa áp dụng tin nhắn này.",
    "Email Agent đang xử lý báo cáo hoặc thao tác khác. Hãy thử lại sau ít phút; chưa áp dụng tin nhắn này.",
    "Không thể xử lý. Hãy dùng nút mới nhất hoặc kiểm tra thủ công; không tự gửi lại."]) {
    const events = [];
    const handler = textHandler(config, async (_config, _env, event) => {
      events.push(event); return { text, buttons: [], edit_token: null };
    }, () => env);
    assert.deepEqual(await handler(...textEvent()), { handled: true, text });
    assert.equal(events.length, 1);
    assert.equal(events[0].kind, "edit_session");
    assert.equal(JSON.stringify(events).includes("viết ngắn hơn"), false);
  }
});

test("exact private account/user/chat enforced, including topic composites", async () => {
  let calls = 0;
  const handler = textHandler(config, async () => { calls++; }, () => env);
  for (const patch of [{ senderId: "67890" }, { accountId: "other" }, { conversationId: "-12345" },
    { conversationId: "12345:topic:1" }, { conversationId: "12345:dm:1" }, { conversationId: "telegram:12345" },
    { conversationId: undefined }, { messageId: undefined }]) {
    const [event, ctx] = textEvent();
    await handler(event, { ...ctx, ...patch });
  }
  for (const patch of [{ isGroup: true }, { isGroup: undefined }, { senderId: "67890" }, { messageId: "12" }]) {
    const [event, ctx] = textEvent();
    await handler({ ...event, ...patch }, ctx);
  }
  assert.equal(calls, 0);
});

test("worker failures and missing configuration never fall through to general agent", async () => {
  const fails = async () => { throw new Error("PRIVATE_MODEL_ERROR"); };
  assert.deepEqual(await textHandler(config, fails, () => env)(...textEvent()), { handled: true });
  assert.deepEqual(await textHandler(config, fails, () => { throw new Error("SECRET"); })(...textEvent()), { handled: true });
});

test("non-Telegram events remain outside plugin", async () => {
  const [event, ctx] = textEvent();
  assert.deepEqual(await textHandler(config)({ ...event, channel: "slack" }, ctx), { handled: false });
});

test("malformed and oversized text cannot invoke worker", async () => {
  let calls = 0;
  const handler = textHandler(config, async () => { calls++; }, () => env);
  for (const content of [null, "", " ", "a".repeat(2001)]) {
    const [event, ctx] = textEvent();
    assert.equal((await handler({ ...event, content }, ctx)).handled, true);
  }
  assert.equal(calls, 0);
});

test("callback forwards only authorized opaque data and replies through native transport", async () => {
  const events = [], ctx = callback();
  const handler = callbackHandler(config, async (_config, _env, event) => {
    events.push(event); return { text: "Hãy nhắn yêu cầu chỉnh sửa cho bản trả lời này.", buttons: [] };
  }, () => env);
  await handler(ctx);
  assert.equal(events.length, 1);
  assert.equal(events[0].data, token);
  assert.equal(ctx.replies.length, 1);
});

test("group/forum/topic unauthorized callbacks are rejected without previews", async () => {
  let calls = 0;
  const handler = callbackHandler(config, async () => { calls++; }, () => env);
  for (const patch of [{ isGroup: true }, { isForum: true }, { threadId: 1 }, { senderId: "67890" },
    { auth: { isAuthorizedSender: false } }, { accountId: "other" }, { channel: "other" },
    { callback: { namespace: "dailydraft", chatId: "-12345", data: token } }]) {
    const ctx = { ...callback(), ...patch };
    await handler(ctx);
    assert.equal(ctx.replies.length, 0);
  }
  assert.equal(calls, 0);
});

test("raw worker exceptions are sanitized for authorized callbacks", async () => {
  const ctx = callback();
  await callbackHandler(config, async () => { throw new Error("SECRET"); }, () => env)(ctx);
  assert.equal(ctx.replies.length, 1);
  assert.equal(JSON.stringify(ctx.replies).includes("SECRET"), false);
});

test("private environment rejects tokens, auto-send, insecure files and symlink ancestors", { skip: process.platform === "win32" }, () => {
  const root = mkdtempSync(join(tmpdir(), "daily-env-"));
  const path = join(root, "env.json");
  try {
    const write = value => { writeFileSync(path, JSON.stringify(value), { mode: 0o600 }); chmodSync(path, 0o600); };
    write(env);
    process.env.TELEGRAM_BOT_TOKEN = "never inherited";
    assert.equal(privateEnvironment(path).TELEGRAM_BOT_TOKEN, undefined);
    for (const extra of [{ TELEGRAM_BOT_TOKEN: "secret" }, { EMAIL_AGENT_AUTO_SEND: "true" },
      { TELEGRAM_TARGET_CHAT_ID: "-12345" }, { UNKNOWN: "value" }]) {
      write({ ...env, ...extra });
      assert.throws(() => privateEnvironment(path));
    }
    write(env);
    chmodSync(path, 0o644);
    assert.throws(() => privateEnvironment(path));
    chmodSync(path, 0o600);
    symlinkSync(path, join(root, "link.json"));
    assert.throws(() => privateEnvironment(join(root, "link.json")));
  } finally {
    delete process.env.TELEGRAM_BOT_TOKEN;
    rmSync(root, { recursive: true });
  }
});

test("worker rejects invalid command paths", async () => {
  await assert.rejects(worker({ pythonPath: "relative", workerPath: "relative" }, env, {}));
});
