# Changelog

All notable changes to this project are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
versioning follows [Semantic Versioning](https://semver.org/).

## [1.1.0] - 2026-10-08

### Added
- **Fibonacci confirmation** (`REQUIRE_FIB`, default `true`): at least one
  fib level must sit inside the OB zone (edges included). Port of
  LonesomeTheBlue's "Fibonacci levels MTF" indicator, using
  Higher Time Frame = `1w` and Current or Last HTF Candle = `last`.
  - Bullish HTF candle: `high - (high - low) * ratio`; bearish:
    `low + (high - low) * ratio`.
  - New settings: `FIB_TIMEFRAME` (default `1w`), `FIB_CANDLE`
    (`last` / `current`, default `last`), `FIB_LEVELS` (default
    `0,0.236,0.382,0.5,0.618,0.786,1`).
  - Only checked for pairs that already passed every other filter (one
    extra small fetch per candidate), and applied per signal, so an older
    qualifying OB can still be picked if the newest one has no fib in it.
  - Pairs without a closed weekly candle yet (new listings) are skipped.
- Fib levels that hit the OB are shown in the Telegram result line
  (e.g. `fib 0.618@1.2345`) and drawn as dotted orange lines on the chart
  (`CHART_FIB_COLOR`).
- README: documented `MAX_CONCURRENCY`, `API_TIMEOUT_MS` and `QUOTE`.

### Removed
- **Multi-timeframe confirmation.** A scan on a timeframe is now judged on
  that timeframe only; no second timeframe has to agree.
  - Removed `CONFIRM_TIMEFRAMES` and `CONFIRM_MODE` from
    `scan_binance.py`, and the per-timeframe pairing table and
    `CONFIRM_MODE: "any"` from `scan.yml` (the "Resolve timeframe" step now
    only validates and passes `TIMEFRAME` through).
  - Removed the "synced with ..." tag from Telegram results.
  - Expect more matches than before, since the neighbor-TF check no longer
    filters them (the fib filter partly compensates).

### Changed
- `cloudflare-worker.js`: removed the "confirms against ..." notes from the
  command list comment and reworded the `/start` welcome message to
  mention the weekly fib. No logic change; redeploying the Worker is
  optional.
- README: intro now says "exact candle" for the OB/swing match (it
  previously said "overlaps", which contradicted the match definition),
  updated the commands, tuning and match-definition sections.

## [1.0.0]

Initial release.

- Scans all Binance USDT-M perpetual pairs (USDT quote and settle) on a
  timeframe picked from Telegram (`/scan15m`, `/scan30m`, `/scan1h`,
  `/scan4h`).
- Match = the exact candle of an active internal Order Block (LuxAlgo SMC
  port) that is also the exact candle of a same-direction HH/HL/LH/LL swing
  label, within the last `SIGNAL_LOOKBACK` closed candles.
- Filters: fresh OB, minimum candles beyond the OB, fresh 3-candle FVG,
  unbroken local extreme after BOS/CHoCH, no opposing swing label since the
  breakaway, OB in discount (bullish) / premium (bearish).
- Multi-timeframe confirmation on a neighboring timeframe (`any` of two).
- Results and PNG charts sent to Telegram; Cloudflare Worker relay and
  GitHub Actions self-hosted runner.
