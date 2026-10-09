# Binance SMC Scanner

[![Binance SMC Scan](https://github.com/acevod/binance-smc-scanner/actions/workflows/scan.yml/badge.svg)](https://github.com/acevod/binance-smc-scanner/actions/workflows/scan.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![Runner](https://img.shields.io/badge/runner-self--hosted-orange)
![Timeframes](https://img.shields.io/badge/timeframes-15m%20%7C%2030m%20%7C%201h%20%7C%204h-informational)

Scans every Binance USDT-M perpetual futures pair on a timeframe you pick
from Telegram, looking for pairs where, within the last N closed candles,
there's a candle that is the exact candle an active internal Order Block was
built from AND the exact candle of a same-direction HH/HL/LH/LL swing label - plus a
fresh Fair Value Gap, an unbroken local extreme after the BOS/CHoCH, no
contradicting swing label since, the OB sitting in the correct premium/
discount half of the trailing range, and at least one weekly or monthly
Fibonacci level in the right half of the OB zone (upper half for bullish,
lower half for bearish).

## How it works

```
You send /start in Telegram, tap a timeframe button (or type /scan30m etc.)
        |
        v
Cloudflare Worker (receives the Telegram webhook)
        |
        v
Triggers GitHub Actions (repository_dispatch, carrying which timeframe)
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
Make sure `./run.sh` is running in the Termux Ubuntu shell (step 5), then
send `/start` to your bot in Telegram - you'll get a welcome message with
four timeframe buttons to tap. Within ~1-2 minutes of tapping one, results
should land in the chat.

You can also test the workflow directly without Telegram: repo →
Actions → "Binance SMC Scan" → Run workflow → pick a timeframe from the
dropdown (still needs the runner online).

## Commands

- `/start` — shows a welcome message with tappable timeframe buttons
- `/scan15m` — scan the 15m timeframe
- `/scan30m` — scan the 30m timeframe
- `/scan1h` — scan the 1h timeframe
- `/scan4h` — scan the 4h timeframe
- `/scanall` — scan 15m, 30m, 1h and 4h one after another (same as the
  **All TF** button). Each timeframe sends its own result message and
  charts; the timeframe picker comes back once at the end. It takes
  roughly as long as the four scans put together, so `scan.yml`'s job
  timeout is 180 minutes.

There's no bare `/scan` anymore - a timeframe always has to be picked,
either by tapping a button or typing the matching command.

## Tuning

Most parameters live at the top of `scan_binance.py` as env vars with
defaults - override them via `scan.yml`'s `env:` block if needed:
- `SIGNAL_LOOKBACK` (default 50) — how many recent closed candles are
  scanned for a qualifying signal candle.
- `REQUIRE_FRESH_OB` (default `true`) — only keep signals whose OB zone
  has never been touched again since its first breakaway close.
- `MIN_CANDLES_BEYOND_OB` (default 5) — the breakaway close has to be
  at least this many candles old before the signal counts as valid.
- `REQUIRE_FVG` (default `true`) — require a fresh (unfilled) 3-candle
  Fair Value Gap around the OB's breakaway close.
- `REQUIRE_UNBROKEN_SWING` (default `true`) — require a still-unbroken
  local extreme after the formal BOS/CHoCH break.
- `REQUIRE_NO_OPPOSING_SWING` (default `true`) — reject the match if a
  new same-type swing label (LL/HL for bullish, HH/LH for bearish) has
  formed since the breakaway close.
- `REQUIRE_PREMIUM_DISCOUNT` (default `true`) — the whole OB zone must
  sit in the right half of LuxAlgo's trailing swing range: discount
  (bottom to equilibrium) for a bullish OB, premium (equilibrium to top)
  for a bearish OB.
- `REQUIRE_FIB` (default `true`) — at least one Fibonacci level of a
  higher-timeframe candle must sit in the right **half** of the OB zone:
  the upper half (midpoint to top) for a bullish OB, the lower half
  (bottom to midpoint) for a bearish OB. Levels exactly on the midpoint
  or the outer edge count. This is a port of LonesomeTheBlue's
  "Fibonacci levels MTF" indicator: for a bullish HTF candle the level is
  `high - (high - low) * ratio`, for a bearish one
  `low + (high - low) * ratio`. Only checked for pairs that already
  matched, so it costs a couple of tiny extra fetches per candidate.
  - `FIB_TIMEFRAMES` (default `1w,1M`) — the indicator's "Higher Time
    Frame". Weekly **or** monthly: a hit on either one is enough. A pair
    with no closed monthly candle yet (listed < 1 month ago) is judged on
    the weekly alone.
  - `FIB_CANDLE` (default `last`) — `last` = last closed HTF candle,
    `current` = the still-forming one (the indicator's "Current or Last
    HTF Candle").
  - `FIB_LEVELS` (default `0,0.236,0.382,0.5,0.618,0.786,1`) — the
    indicator's enabled-by-default levels; add `-0.382` / `1.236` for the
    extension levels it ships disabled.
  - `scan.yml` sets `FIB_TIMEFRAMES` and `FIB_CANDLE` explicitly in the
    `Run scanner` step - change them there, not just in the script.
  - The fib grids always come from the HTF candle that is "last" **as of
    the scan**, not as of when the OB formed. An OB from a few weeks ago
    is therefore checked against the most recent weekly/monthly range.
- `GENERATE_CHARTS` — set to `"false"` for text-only results (faster).
- `MAX_CONCURRENCY` (default 8) — how many symbols are fetched at once.
- `API_TIMEOUT_MS` (default 30000) — ccxt request timeout; raise it if
  you keep seeing `RequestTimeout` errors.
- `QUOTE` (default `USDT`) — quote/settle asset filter for the pair
  universe.
- `CHART_CANDLES` (default 50) — how many candles are shown in the chart
  image. Chart colors are further down the same config block
  (`CHART_BG_COLOR`, `CHART_UP_COLOR`, etc.) if you want to restyle them.
- `CANDLE_LIMIT` — number of historical candles fetched (needs at least
  ~220 for the 200-period ATR + 50-bar swing warmup; default 500 is
  safe).

**Each timeframe is judged on its own.** `/scan15m`, `/scan30m`, `/scan1h`
and `/scan4h` only look at the picked timeframe (and `/scanall` just runs
them one after another, with no cross-checking between them) - there's no confirmation
on any other timeframe anymore. The `Resolve timeframe` step in `scan.yml`
just passes the picked timeframe through as `TIMEFRAME`.

Pair universe: USD-M futures only (`binanceusdm`, never COIN-M), and
only pairs where both quote and settle currency are USDT (so
USDC-margined pairs are excluded too).

## A note on the "match" definition

> Within the last `SIGNAL_LOOKBACK` candles (default 50), there's a
> candle that is BOTH the exact candle an active (unmitigated) internal
> Order Block was built from AND the exact candle of a same-direction
> swing label (LL/HL for a bullish OB, HH/LH for a bearish OB) — the two
> must land on the same bar, not just nearby. On top of that: the OB's
> breakaway close must form a fresh (unfilled) 3-candle Fair Value Gap,
> the local extreme reached after the BOS/CHoCH break must still be
> unbroken, no new same-type swing label may have formed since the
> break, the whole OB zone must sit in discount (bullish) or premium
> (bearish) of the trailing swing range, and at least one fib level of
> the last closed weekly or monthly candle must fall in the upper half
> of a bullish OB / the lower half of a bearish OB. If more
> than one candle in the window qualifies, the most recent one is used
> as the headline result.

This is a best-effort port of two indicators' behavior — worth
double-checking the first scan's results against your TradingView chart
directly.
