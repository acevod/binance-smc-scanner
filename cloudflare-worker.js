/**
 * Cloudflare Worker: Telegram -> GitHub Actions relay
 * ----------------------------------------------------
 * Deploy this as a Worker, set it as your Telegram bot's webhook URL.
 *
 * Commands:
 *   /start    - welcome message with tappable timeframe buttons
 *   /scan15m  - scan 15m (confirms against 5m or 30m)
 *   /scan30m  - scan 30m (confirms against 15m or 1h)
 *   /scan1h   - scan 1h  (confirms against 30m or 4h)
 *   /scan4h   - scan 4h  (confirms against 1h or 1D)
 * The inline keyboard buttons under /start trigger the same thing as
 * typing the matching command - use whichever is more convenient.
 *
 * The workflow itself sends the results back to Telegram directly (see
 * scan_binance.py) - this Worker only relays the request to GitHub.
 *
 * Required Worker secrets (wrangler secret put <NAME>, or via the
 * dashboard under Settings -> Variables and Secrets):
 *   TELEGRAM_BOT_TOKEN   - your bot token
 *   TELEGRAM_CHAT_ID     - your chat id (so only you can trigger it)
 *   GITHUB_TOKEN         - a fine-grained PAT with "Contents: write" on the repo
 *   GITHUB_OWNER         - your GitHub username/org
 *   GITHUB_REPO          - the repo name (e.g. "binance-smc-scanner")
 */

const TIMEFRAMES = ["15m", "30m", "1h", "4h"];

const CMD_TO_TF = {
  "/scan15m": "15m",
  "/scan30m": "30m",
  "/scan1h": "1h",
  "/scan4h": "4h",
};

const CALLBACK_TO_TF = {
  "scan_15m": "15m",
  "scan_30m": "30m",
  "scan_1h": "1h",
  "scan_4h": "4h",
};

export default {
  async fetch(request, env) {
    if (request.method !== "POST") {
      return new Response("ok", { status: 200 });
    }

    let update;
    try {
      update = await request.json();
    } catch {
      return new Response("bad request", { status: 400 });
    }

    // --- inline keyboard button tap ---
    if (update.callback_query) {
      const cq = update.callback_query;
      const chatId = cq.message && cq.message.chat && cq.message.chat.id;
      if (!chatId || String(chatId) !== String(env.TELEGRAM_CHAT_ID)) {
        return new Response("ignored", { status: 200 });
      }
      await answerCallbackQuery(env, cq.id);
      const tf = CALLBACK_TO_TF[cq.data];
      if (tf) {
        await startScan(env, tf);
      }
      return new Response("ok", { status: 200 });
    }

    // --- typed message / command ---
    const msg = update.message || update.edited_message;
    const text = (msg && msg.text) || "";
    const chatId = msg && msg.chat && msg.chat.id;

    if (!chatId || String(chatId) !== String(env.TELEGRAM_CHAT_ID)) {
      return new Response("ignored", { status: 200 });
    }

    const cmd = text.trim().toLowerCase();

    if (cmd === "/start") {
      await sendWelcomeMessage(env);
      return new Response("ok", { status: 200 });
    }

    const tf = CMD_TO_TF[cmd];
    if (tf) {
      await startScan(env, tf);
    }

    return new Response("ok", { status: 200 });
  },
};

async function startScan(env, timeframe) {
  await sendTelegramMessage(env, `🔍 Scan ${timeframe} dimulai, tunggu beberapa menit...`);
  await triggerGithubWorkflow(env, timeframe);
}

async function sendWelcomeMessage(env) {
  const url = `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`;
  await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      chat_id: env.TELEGRAM_CHAT_ID,
      text: "👋 Binance SMC Scanner\n\n"
          + "Scans every Binance USDT-M futures pair for a confirmed "
          + "Order Block + swing point setup (SMC/ICT), cross-checked "
          + "against a neighboring timeframe before it counts.\n\n"
          + "Pick a timeframe to scan:",
      reply_markup: {
        inline_keyboard: [
          [
            { text: "15m", callback_data: "scan_15m" },
            { text: "30m", callback_data: "scan_30m" },
          ],
          [
            { text: "1h", callback_data: "scan_1h" },
            { text: "4h", callback_data: "scan_4h" },
          ],
        ],
      },
    }),
  });
}

async function sendTelegramMessage(env, text) {
  const url = `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`;
  await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: env.TELEGRAM_CHAT_ID, text }),
  });
}

async function answerCallbackQuery(env, callbackQueryId) {
  const url = `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/answerCallbackQuery`;
  await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ callback_query_id: callbackQueryId }),
  });
}

async function triggerGithubWorkflow(env, timeframe) {
  const url = `https://api.github.com/repos/${env.GITHUB_OWNER}/${env.GITHUB_REPO}/dispatches`;
  const resp = await fetch(url, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
      "Accept": "application/vnd.github+json",
      "User-Agent": "binance-smc-scanner-worker",
    },
    body: JSON.stringify({
      event_type: "telegram-scan",
      client_payload: { timeframe },
    }),
  });

  if (!resp.ok) {
    const body = await resp.text();
    await sendTelegramMessage(env, `⚠️ Failed to trigger GitHub Actions: ${resp.status} ${body}`);
  }
}
