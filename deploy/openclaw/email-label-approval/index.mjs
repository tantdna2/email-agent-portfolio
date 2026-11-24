import { spawn } from "node:child_process";
import { readFileSync, lstatSync } from "node:fs";
import { isAbsolute, dirname } from "node:path";

const ERROR = "Không thể xử lý yêu cầu. Nếu vừa xác nhận gắn nhãn, kiểm tra Gmail thủ công; không tự thử lại.";
const ALLOWED = new Set(["GOG_ACCOUNT", "GMAIL_AUTHORIZED_USER_FILE", "GMAIL_LABEL_AUTHORIZED_USER_FILE",
  "TELEGRAM_APPROVER_USER_ID", "TELEGRAM_TARGET_CHAT_ID", "TELEGRAM_ACCOUNT_ID",
  "EMAIL_AGENT_AUTO_SEND", "EMAIL_AGENT_LABEL_APPROVAL", "EMAIL_AGENT_LABEL_ALLOWLIST_JSON",
  "OPENCLAW_CONFIG_PATH", "OPENCLAW_STATE_DIR", "XDG_STATE_HOME", "PATH"]);

export function privateEnvironment(path) {
  if (typeof path !== "string" || !isAbsolute(path)) throw new Error("configuration_invalid");
  for (let part = path; ; part = dirname(part)) {
    if (lstatSync(part).isSymbolicLink()) throw new Error("configuration_invalid");
    if (dirname(part) === part) break;
  }
  const stat = lstatSync(path);
  if (!stat.isFile() || (stat.mode & 0o777) !== 0o600 || stat.uid !== process.getuid()) {
    throw new Error("configuration_invalid");
  }
  const config = JSON.parse(readFileSync(path, "utf8"));
  if (!config || Array.isArray(config) || typeof config !== "object"
      || Object.entries(config).some(([key, value]) => !ALLOWED.has(key) || typeof value !== "string")
      || config.EMAIL_AGENT_AUTO_SEND !== "false" || config.EMAIL_AGENT_LABEL_APPROVAL !== "true"
      || !/^[1-9][0-9]*$/.test(config.TELEGRAM_APPROVER_USER_ID ?? "")
      || config.TELEGRAM_TARGET_CHAT_ID !== config.TELEGRAM_APPROVER_USER_ID) {
    throw new Error("configuration_invalid");
  }
  // Only the label worker receives the writer path. No gateway bot or gog secrets.
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
    const timer = setTimeout(() => { child.kill("SIGKILL"); reject(new Error("worker_timeout")); }, 660000);
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
                || !/^emaillabel:[0-9a-f]{32}$/.test(button.callback_data)))) {
          throw new Error("worker_output_invalid");
        }
        resolve(result);
      } catch { reject(new Error("worker_output_invalid")); }
    });
    child.stdin.end(JSON.stringify(event));
  });
}

function authorized(ctx, env) {
  return ctx.channel === "telegram" && ctx.auth?.isAuthorizedSender === true
    && ctx.isGroup === false && ctx.isForum === false && ctx.threadId == null
    && ctx.senderId === env.TELEGRAM_APPROVER_USER_ID
    && ctx.callback?.chatId === env.TELEGRAM_TARGET_CHAT_ID
    && ctx.accountId === (env.TELEGRAM_ACCOUNT_ID ?? "default")
    && ctx.callback?.namespace === "emaillabel"
    && /^emaillabel:[0-9a-f]{32}$/.test(ctx.callback?.data ?? "");
}

export function createHandler(config, run = worker, load = privateEnvironment) {
  return async ctx => {
    let env;
    try {
      env = load(config.environmentFile);
      if (!authorized(ctx, env)) return { handled: true };
      const response = await run(config, env, {
        channel: "telegram", account: ctx.accountId, user: ctx.senderId,
        chat: ctx.callback.chatId, authorized: true, private: true, data: ctx.callback.data,
      });
      await ctx.respond.editMessage(response);
    } catch {
      // Never leak exception contents or retry a worker after Telegram edit failure.
      if (env && authorized(ctx, env)) {
        try { await ctx.respond.reply({ text: ERROR }); } catch { /* No fallback. */ }
      }
    }
    return { handled: true };
  };
}

export default {
  id: "email-label-approval",
  name: "Email label approval",
  register(api) {
    // Own the namespace even with broken/missing configuration; no agent fallback.
    api.registerInteractiveHandler({ channel: "telegram", namespace: "emaillabel",
      handler: createHandler(api.pluginConfig ?? {}) });
  },
};
