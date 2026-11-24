import { spawn } from "node:child_process";
import { lstatSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";

const ERROR = "Không thể xử lý. Hãy dùng nút mới nhất hoặc kiểm tra Gmail thủ công; không tự gửi lại.";
const GUIDANCE = "Email Agent đang hoạt động. Để chỉnh sửa bản nháp, hãy bấm ✏️ trên email cần chỉnh.";
const ALLOWED = new Set(["GOG_ACCOUNT", "GOG_KEYRING_PASSWORD", "GOG_KEYRING_BACKEND",
  "GMAIL_AUTHORIZED_USER_FILE", "TARGET_SENDER", "TELEGRAM_APPROVER_USER_ID", "TELEGRAM_TARGET_CHAT_ID",
  "TELEGRAM_ACCOUNT_ID", "EMAIL_AGENT_AUTO_SEND", "EMAIL_AGENT_DAILY_STATE_DIR",
  "EMAIL_AGENT_MONITOR_STATE_DIR",
  "EMAIL_AGENT_INITIAL_LOOKBACK_HOURS", "OPENCLAW_CONFIG_PATH", "OPENCLAW_STATE_DIR", "PATH"]);

export function privateEnvironment(path) {
  if (!isAbsolute(path)) throw new Error("configuration_invalid");
  for (let item = resolve(path);; item = dirname(item)) {
    if (lstatSync(item).isSymbolicLink()) throw new Error("configuration_invalid");
    if (dirname(item) === item) break;
  }
  const info = lstatSync(path);
  if (!info.isFile() || (info.mode & 0o077) !== 0 || info.uid !== process.getuid()) {
    throw new Error("configuration_invalid");
  }
  const env = JSON.parse(readFileSync(path, "utf8"));
  if (!env || Array.isArray(env) || typeof env !== "object"
      || Object.entries(env).some(([key, value]) => !ALLOWED.has(key) || typeof value !== "string")
      || env.EMAIL_AGENT_AUTO_SEND !== "false"
      || !/^[1-9][0-9]*$/.test(env.TELEGRAM_APPROVER_USER_ID ?? "")
      || env.TELEGRAM_TARGET_CHAT_ID !== env.TELEGRAM_APPROVER_USER_ID) {
    throw new Error("configuration_invalid");
  }
  // Never inherit Gateway/bot credentials into Python or the model.
  return { HOME: process.env.HOME, LANG: "C.UTF-8", PATH: process.env.PATH, ...env };
}

export function worker(config, env, event) {
  return new Promise((resolveResult, reject) => {
    if (!isAbsolute(config.pythonPath) || !isAbsolute(config.workerPath)) {
      reject(new Error("configuration_invalid")); return;
    }
    const child = spawn(config.pythonPath, [config.workerPath], {
      env, shell: false, stdio: ["pipe", "pipe", "ignore"],
    });
    const timer = setTimeout(() => { child.kill("SIGKILL"); reject(new Error("worker_timeout")); }, 540000);
    let output = "";
    child.on("error", () => { clearTimeout(timer); reject(new Error("worker_failed")); });
    child.stdin.on("error", () => {});
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", chunk => {
      output += chunk;
      if (output.length > 16384) { child.kill("SIGKILL"); reject(new Error("worker_output_invalid")); }
    });
    child.on("close", code => {
      clearTimeout(timer);
      try {
        if (code !== 0) throw new Error("worker_failed");
        const value = JSON.parse(output);
        if (typeof value.text !== "string" || value.text.length > 4000 || !Array.isArray(value.buttons)
            || (value.edit_token != null && !/^dailydraft:[0-9a-f]{32}$/.test(value.edit_token))
            || value.buttons.length > 1 || value.buttons.some(row => !Array.isArray(row) || row.length > 3
              || row.some(b => typeof b.text !== "string" || b.text.length > 30
                || !/^dailydraft:[0-9a-f]{32}$/.test(b.callback_data)))) throw new Error("worker_output_invalid");
        resolveResult(value);
      } catch { reject(new Error("worker_failed")); }
    });
    child.stdin.end(JSON.stringify(event));
  });
}

function identity(env, user, chat, account) {
  return user === env.TELEGRAM_APPROVER_USER_ID && chat === env.TELEGRAM_TARGET_CHAT_ID
    && account === (env.TELEGRAM_ACCOUNT_ID ?? "default");
}
function eventBase(env) {
  return { channel: "telegram", user: env.TELEGRAM_APPROVER_USER_ID, chat: env.TELEGRAM_TARGET_CHAT_ID,
    account: env.TELEGRAM_ACCOUNT_ID ?? "default", authorized: true, private: true };
}

export function callbackHandler(config, run = worker, load = privateEnvironment) {
  return async ctx => {
    let authorized = false;
    try {
      const env = load(config.environmentFile);
      authorized = ctx.channel === "telegram" && ctx.auth?.isAuthorizedSender === true
        && ctx.isGroup === false && ctx.isForum === false && ctx.threadId == null
        && identity(env, ctx.senderId, ctx.callback?.chatId, ctx.accountId);
      if (!authorized || ctx.callback?.namespace !== "dailydraft"
          || !/^dailydraft:[0-9a-f]{32}$/.test(ctx.callback?.data ?? "")) return { handled: true };
      const response = await run(config, env, { ...eventBase(env), kind: "callback", data: ctx.callback.data });
      // Keep the original review buttons accessible when prompting for an edit.
      // Replayed buttons still go through durable version/status validation.
      await ctx.respond.reply(response);
    } catch {
      if (authorized) { try { await ctx.respond.reply({ text: ERROR }); } catch {} }
    }
    return { handled: true };
  };
}

export function textHandler(config, run = worker, load = privateEnvironment) {
  return async (event, ctx) => {
    if (event.channel !== "telegram" || ctx.channelId !== "telegram") return { handled: false };
    try {
      const env = load(config.environmentFile);
      // Verified against 2026.9.5 message-hook-mappers and Telegram resolver:
      // topics have a composite conversationId; only the bare positive DM ID
      // is accepted. Gateway supplies these identities, never message content.
      if (event.isGroup !== false || !identity(env, ctx.senderId, ctx.conversationId, ctx.accountId)
          || event.senderId !== ctx.senderId || !/^[1-9][0-9]*$/.test(ctx.messageId ?? "")
          || event.messageId !== ctx.messageId) return { handled: true };
      if (typeof event.content !== "string" || !event.content.trim() || event.content.length > 2000) {
        return { handled: true, text: "Yêu cầu chỉnh sửa phải là tin nhắn văn bản ngắn." };
      }
      // Read session state without forwarding ordinary text or touching Gmail.
      // Python revalidates this binding under the same durable pipeline lock.
      const session = await run(config, env, { ...eventBase(env), kind: "edit_session" });
      if (!/^dailydraft:[0-9a-f]{32}$/.test(session.edit_token ?? "")) {
        // Worker-chosen fixed text distinguishes no session, expired session,
        // busy worker or generic failure; message content is never forwarded.
        return { handled: true, text: session.text || GUIDANCE };
      }
      const response = await run(config, env, { ...eventBase(env), kind: "text",
        message_id: ctx.messageId, text: event.content, edit_token: session.edit_token });
      return { handled: true, ...(response.text ? { text: response.text } : {}) };
    } catch {
      // Fail closed, including missing configuration. Never hand editing text
      // to a general agent after a worker error, timeout, or stale target.
      return { handled: true };
    }
  };
}

export default {
  id: "daily-sender-digest",
  name: "Daily sender digest",
  register(api) {
    api.registerInteractiveHandler({ channel: "telegram", namespace: "dailydraft",
      handler: callbackHandler(api.pluginConfig ?? {}) });
    // Native claiming hook; omit host timeout, worker has its own bounded timeout
    // whose rejection is caught above and returns handled:true.
    api.on("before_dispatch", textHandler(api.pluginConfig ?? {}), { priority: 1000 });
  },
};
