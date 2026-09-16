# Binance SMC Scanner

Scans every Binance USDT-M perpetual futures pair on the 30m timeframe,
looking for pairs where, within the last N closed candles, there's a
candle that overlaps an active internal Order Block AND has a
same-direction HH/HL/LH/LL swing label nearby.

## How it works

```
You send /scan in Telegram
        |
        v
Cloudflare Worker (receives the Telegram webhook)
        |
        v
Triggers GitHub Actions (repository_dispatch)
        |
        v
scan_binance.py runs on GitHub's server, scans every pair
        |
        v
Results (list + charts) are sent back to Telegram
```

## Setup

### 1. Create a GitHub repo
Push this folder to a new repo. **Private is fine** — GitHub Actions runs
normally on private repos, and personal accounts get free Actions minutes
(~2,000/month) for private repos too. The Worker and Telegram bot don't
care about repo visibility either, since access is via a personal access
token, not the repo being public.

### 2. Create a Telegram bot
- Message `@BotFather` → `/newbot` → save the **bot token**.
- Send any message to your new bot, then open
  `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser to find
  your **chat id** (the number in `"chat":{"id": ...}`).

### 3. GitHub Secrets
Repo → Settings → Secrets and variables → Actions, add:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

### 4. GitHub Personal Access Token (for the Worker to use)
Create a fine-grained PAT (Settings → Developer settings → Fine-grained
tokens) scoped to **this repo only**, with **Actions: Read and write**
permission.

### 5. Deploy the Cloudflare Worker

**Option A — via the dashboard (easiest, no CLI needed):**
1. Go to dash.cloudflare.com → **Workers & Pages** → **Create** →
   **Create Worker**.
2. Give it a name (e.g. `binance-scanner-relay`) → **Deploy** (this
   deploys the default "Hello World" code, that's fine for now).
3. Click **Edit code**, delete everything, paste in the contents of
   `cloudflare-worker.js` from this repo → **Save and deploy**.
4. Back on the Worker's page, go to **Settings** → **Variables and
   Secrets**, and add these five, one by one (use type **Secret**, not
   plain Text, especially for the tokens):
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `GITHUB_TOKEN`
   - `GITHUB_OWNER` (your GitHub username)
   - `GITHUB_REPO` (this repo's name)
5. Redeploy after adding the secrets.
6. Your Worker URL is shown at the top of the page, something like
   `https://binance-scanner-relay.<your-subdomain>.workers.dev`.

**Option B — via the CLI (wrangler):**
```bash
npm install -g wrangler
wrangler init binance-scanner-relay   # choose "Hello World Worker"
# replace src/index.js with cloudflare-worker.js from this repo
wrangler secret put TELEGRAM_BOT_TOKEN
wrangler secret put TELEGRAM_CHAT_ID
wrangler secret put GITHUB_TOKEN
wrangler secret put GITHUB_OWNER
wrangler secret put GITHUB_REPO
wrangler deploy
```

### 6. Point the Telegram webhook at the Worker
```bash
curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://binance-scanner-relay.<your-subdomain>.workers.dev"
```

### 7. Test it
Send `/scan` to your bot in Telegram. Within ~1-2 minutes, GitHub Actions
should run and the results should land in the chat.

You can also test manually without Telegram: repo → Actions →
"Binance SMC Scan" → Run workflow.

## Tuning

All parameters live at the top of `scan_binance.py`:
- `SIGNAL_LOOKBACK` (default 20) — how many recent closed candles are
  scanned for a qualifying signal candle.
- `GENERATE_CHARTS` — set to `"false"` for text-only results (faster).
- `CANDLE_LIMIT` — number of historical candles fetched (needs at least
  ~220 for the 200-period ATR + 50-bar swing warmup; default 500 is
  safe).

Pair universe: USD-M futures only (`binanceusdm`, never COIN-M), and
only pairs where both quote and settle currency are USDT (so
USDC-margined pairs are excluded too).

## A note on the "match" definition

> Within the last `SIGNAL_LOOKBACK` candles (default 20), there's a
> candle that is BOTH the exact candle an active (unmitigated) internal
> Order Block was built from AND the exact candle of a same-direction
> swing label (HH/HL for a bullish OB, LH/LL for a bearish OB) from the
> HH.pine logic — the two must land on the same bar, not just nearby.
> If more than one candle in the window qualifies, the most recent one
> is used as the headline result.

This is a best-effort port of your two indicators' behavior — worth
double-checking the first scan's results against your TradingView chart
directly. If something looks off, send the pair + candle time and it can
be recalibrated.
