import { spawn } from "node:child_process";
import { readFileSync, lstatSync } from "node:fs";
import { isAbsolute } from "node:path";

const ERROR = "Không thể xử lý yêu cầu. Nếu vừa xác nhận gửi, kiểm tra Gmail thủ công; không tự thử gửi lại.";
const ALLOWED = new Set(["GOG_ACCOUNT", "GOG_KEYRING_PASSWORD", "GOG_KEYRING_BACKEND",
  "GMAIL_AUTHORIZED_USER_FILE", "TELEGRAM_APPROVER_USER_ID", "TELEGRAM_TARGET_CHAT_ID",
  "TELEGRAM_ACCOUNT_ID", "EMAIL_AGENT_AUTO_SEND", "EMAIL_AGENT_REPLY_APPROVAL",
  "OPENCLAW_CONFIG_PATH", "OPENCLAW_STATE_DIR", "XDG_STATE_HOME", "PATH"]);

export function privateEnvironment(path) {
  if (!isAbsolute(path)) throw new Error("configuration_invalid");
  const stat = lstatSync(path);
  if (!stat.isFile() || stat.isSymbolicLink() || (stat.mode & 0o077) !== 0
      || stat.uid !== process.getuid()) throw new Error("configuration_invalid");
  const config = JSON.parse(readFileSync(path, "utf8"));
  if (!config || Array.isArray(config) || typeof config !== "object"
      || Object.entries(config).some(([key, value]) => !ALLOWED.has(key) || typeof value !== "string")
      || config.EMAIL_AGENT_AUTO_SEND !== "false" || config.EMAIL_AGENT_REPLY_APPROVAL !== "true"
      || !/^[1-9][0-9]*$/.test(config.TELEGRAM_APPROVER_USER_ID ?? "")
      || config.TELEGRAM_TARGET_CHAT_ID !== config.TELEGRAM_APPROVER_USER_ID) {
    throw new Error("configuration_invalid");
  }
  // Gateway credentials (especially the bot token) are not inherited by workers.
  return { HOME: process.env.HOME, LANG: "C.UTF-8", PATH: process.env.PATH, ...config };
}

export function worker(config, env, event) {
  return new Promise((resolve, reject) => {
    if (!isAbsolute(config.pythonPath) || !isAbsolute(config.workerPath)) {
      reject(new Error("configuration_invalid")); return;
    }
    const child = spawn(config.pythonPath, [config.workerPath], {
      env, shell: false, stdio: ["pipe", "pipe", "ignore"],
    });
    let output = "";
    const timer = setTimeout(() => { child.kill("SIGKILL"); reject(new Error("worker_timeout")); }, 540000);
    child.on("error", () => { clearTimeout(timer); reject(new Error("worker_failed")); });
    child.stdin.on("error", () => {});
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", chunk => {
      output += chunk;
      if (output.length > 16384) { child.kill("SIGKILL"); reject(new Error("worker_output_invalid")); }
    });
    child.on("close", code => {
      clearTimeout(timer);
      if (code !== 0) { reject(new Error("worker_failed")); return; }
      try {
        const result = JSON.parse(output);
        if (typeof result.text !== "string" || result.text.length > 4000 || !Array.isArray(result.buttons)
            || result.buttons.length > 2 || result.buttons.some(row => !Array.isArray(row) || row.length > 3
              || row.some(button => typeof button.text !== "string" || button.text.length > 30
                || !/^emailreply:[0-9a-f]{32}$/.test(button.callback_data)))) {
          throw new Error("worker_output_invalid");
        }
        resolve(result);
      } catch { reject(new Error("worker_output_invalid")); }
    });
    child.stdin.end(JSON.stringify(event));
  });
}

export function createHandler(config, run = worker, load = privateEnvironment) {
  return async ctx => {
    try {
      const env = load(config.environmentFile);
      if (ctx.channel !== "telegram" || ctx.auth?.isAuthorizedSender !== true
          || ctx.isGroup !== false || ctx.isForum !== false || ctx.threadId != null
          || ctx.senderId !== env.TELEGRAM_APPROVER_USER_ID
          || ctx.callback?.chatId !== env.TELEGRAM_TARGET_CHAT_ID
          || ctx.accountId !== (env.TELEGRAM_ACCOUNT_ID ?? "default")
          || ctx.callback?.namespace !== "emailreply"
          || !/^emailreply:[0-9a-f]{32}$/.test(ctx.callback?.data ?? "")) {
        return { handled: true }; // Never expose preview to an unauthorized chat.
      }
      const response = await run(config, env, {
        channel: "telegram", account: ctx.accountId, user: ctx.senderId,
        chat: ctx.callback.chatId, authorized: true, private: true, data: ctx.callback.data,
      });
      await ctx.respond.editMessage(response);
    } catch {
      // Never throw child output/errors into the gateway journal or agent fallback.
      // No retries, submitText, model run, or Telegram polling here.
      try {
        // An invalid config cannot establish authorization, so do not reply then.
        const env = load(config.environmentFile);
        if (ctx.auth?.isAuthorizedSender === true && ctx.isGroup === false && ctx.isForum === false
            && ctx.senderId === env.TELEGRAM_APPROVER_USER_ID
            && ctx.callback?.chatId === env.TELEGRAM_TARGET_CHAT_ID
            && ctx.accountId === (env.TELEGRAM_ACCOUNT_ID ?? "default")) {
          await ctx.respond.reply({ text: ERROR });
        }
      } catch { /* Sanitized silence; durable state remains authoritative. */ }
    }
    return { handled: true };
  };
}

export default {
  id: "email-reply-approval",
  name: "Email reply approval",
  register(api) {
    // Register even when private configuration is missing: own the namespace
    // and fail closed, rather than handing stale buttons to a general agent.
    api.registerInteractiveHandler({ channel: "telegram", namespace: "emailreply",
      handler: createHandler(api.pluginConfig ?? {}) });
  },
};
