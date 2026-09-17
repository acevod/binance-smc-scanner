# Binance SMC Scanner

Scans every Binance USDT-M perpetual futures pair on the 30m timeframe,
looking for pairs where, within the last N closed candles, there's a
candle that overlaps an active internal Order Block AND has a
same-direction HH/HL/LH/LL swing label on the exact same candle.

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
scan_binance.py runs on YOUR OWN self-hosted runner
(not GitHub's servers - Binance blocks those, see step 5)
        |
        v
Results (list + charts) are sent back to Telegram
```

## Setup

### 1. Create a GitHub repo
Push this folder to a new repo. **Private is fine** — GitHub Actions runs
normally on private repos, and personal accounts get free Actions minutes
for private repos too. The Worker and Telegram bot don't care about repo
visibility either, since access is via a personal access token, not the
repo being public.

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
tokens) scoped to **this repo only**, with:
- **Contents** → Read and write (this is what the `repository_dispatch`
  endpoint actually checks, not "Actions" as you'd expect)
- **Metadata** → Read-only (usually auto-selected)

### 5. Set up the self-hosted runner (on your phone, via Termux)

**Why self-hosted?** Binance blocks requests coming from cloud/datacenter
IP ranges (including GitHub's own runners, which live on Azure) with a
`451 restricted location` error. Running the job from your phone's home
internet connection avoids that.

The official GitHub Actions runner doesn't run cleanly directly inside
Termux (Android's C library isn't fully compatible with it). The
reliable fix: run a real Ubuntu environment inside Termux with
`proot-distro`, and run the runner inside that.

```bash
# 1. In Termux, install proot-distro and a Ubuntu environment
pkg update && pkg install -y proot-distro
proot-distro install ubuntu
proot-distro login ubuntu

# You're now inside Ubuntu (glibc, just like a real Linux server).
# Everything below runs INSIDE this Ubuntu shell.

# 2. Install what the runner and the scan script need
apt update && apt install -y curl git python3 python3-pip tar

# 3. Create a folder for the runner and download it
mkdir actions-runner && cd actions-runner
# Get the EXACT download command (with correct version + checksum) from:
# your repo -> Settings -> Actions -> Runners -> New self-hosted runner
# -> choose Linux -> ARM64 (most phones are ARM64). It looks like this:
curl -o actions-runner-linux-arm64.tar.gz -L https://github.com/actions/runner/releases/download/vX.X.X/actions-runner-linux-arm64-X.X.X.tar.gz
tar xzf ./actions-runner-linux-arm64.tar.gz

# 4. Configure it (GitHub's Runners page gives you this exact command
#    with your own token filled in):
./config.sh --url https://github.com/<owner>/<repo> --token <TOKEN_FROM_GITHUB>

# 5. Start the runner - it needs to be running whenever you want /scan
#    to actually execute (jobs just queue up if it's offline)
./run.sh
```

Keep Termux from getting killed in the background while `./run.sh` runs:
```bash
termux-wake-lock
```
Also turn off battery optimization for Termux in Android Settings, and
consider installing **Termux:Boot** so the runner can auto-start.

The Python dependencies (`pip3 install -r requirements.txt`) get
installed automatically by the workflow's own "Install dependencies"
step the first time it runs — no need to install them manually.

### 6. Deploy the Cloudflare Worker

**Option A — via the dashboard (easiest, no CLI needed):**
1. Go to dash.cloudflare.com → **Workers & Pages** → **Create** →
   **Create Worker**.
2. Give it a name (e.g. `binance-scanner-relay`) → **Deploy** (this
   deploys the default "Hello World" code, that's fine for now).
3. Click **Edit code**, delete everything, paste in the contents of
   `cloudflare-worker.js` from this repo → **Save and deploy**.
4. Back on the Worker's page, go to **Settings** → **Variables and
   Secrets**, and add these five, one by one (use type **Secret**, not
   plain Text):
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `GITHUB_TOKEN` (the PAT from step 4)
   - `GITHUB_OWNER` (your GitHub username, no `@`)
   - `GITHUB_REPO` (just the repo name, not the full URL)
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

### 7. Point the Telegram webhook at the Worker
Open this URL in a browser (works fine from your phone):
```
https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://binance-scanner-relay.<your-subdomain>.workers.dev
```
A successful response looks like `{"ok":true,"result":true,...}`.

### 8. Test it
Make sure `./run.sh` is running in the Termux Ubuntu shell (step 5),
then send `/scan` to your bot in Telegram. Within ~1-2 minutes, results
should land in the chat.

You can also test the workflow directly without Telegram: repo →
Actions → "Binance SMC Scan" → Run workflow (still needs the runner
online).

## Tuning

All parameters live at the top of `scan_binance.py`:
- `SIGNAL_LOOKBACK` (default 20) — how many recent closed candles are
  scanned for a qualifying signal candle.
- `GENERATE_CHARTS` — set to `"false"` for text-only results (faster).
- `REQUIRE_FRESH_OB` (default `true`) — only keep signals whose OB zone
  has never been touched again since its first breakaway close.
- `MIN_CANDLES_BEYOND_OB` (default 5) — the breakaway close has to be
  at least this many candles old before the signal counts as valid.
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
> swing label (LL/HL for a bullish OB, HH/LH for a bearish OB) from the
> HH.pine logic — the two must land on the same bar, not just nearby.
> If more than one candle in the window qualifies, the most recent one
> is used as the headline result.

This is a best-effort port of your two indicators' behavior — worth
double-checking the first scan's results against your TradingView chart
directly. If something looks off, send the pair + candle time and it can
be recalibrated.
