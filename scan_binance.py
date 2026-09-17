"""
Binance Futures SMC Scanner
============================
Scans all Binance USDT-M perpetual futures pairs on the 30m timeframe and
flags pairs where the LATEST CLOSED candle sits inside an active
(unmitigated) internal Order Block AND has a recent HH/HL/LH/LL swing label
matching that OB's direction.

This is an independent re-implementation (in Python) of the calculation
logic found in two TradingView Pine Script indicators the user owns:
  1. LuxAlgo "Smart Money Concepts" -> internal structure / internal OB only
  2. A standard pivot-based HH/HL/LH/LL swing labeler

Nothing here is copy-pasted Pine source -- it's a from-scratch numeric
port of the *behavior*, written in Python/pandas/numpy, for the user's own
personal scanning tool.

ENV VARS (all optional except none are required to just print to stdout):
  TELEGRAM_BOT_TOKEN   - bot token, if set + TELEGRAM_CHAT_ID -> sends results
  TELEGRAM_CHAT_ID     - chat id to send results to
  GENERATE_CHARTS      - "true"/"false" (default "true") -> render PNG charts
                          for matched pairs and send them as photos
  TIMEFRAME            - default "30m"
  CANDLE_LIMIT         - how many candles to fetch per symbol, default 500
  SIGNAL_LOOKBACK      - how many recent closed candles to scan for a
                          qualifying signal candle, default 10
  REQUIRE_FRESH_OB     - "true"/"false" (default "false") -> if true,
                          drop matches whose OB zone has already been
                          retested since its breakout confirmation
  MAX_CONCURRENCY      - concurrent symbol fetches, default 8
  QUOTE                - quote asset filter, default "USDT"
"""

import asyncio
import io
import os
import sys
import time
import traceback
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import requests

try:
    import ccxt.async_support as ccxt
except ImportError:
    print("Install deps first: pip install -r requirements.txt")
    raise

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
TIMEFRAME       = os.environ.get("TIMEFRAME", "30m")
CANDLE_LIMIT    = int(os.environ.get("CANDLE_LIMIT", "500"))
SIGNAL_LOOKBACK = int(os.environ.get("SIGNAL_LOOKBACK", "15"))  # how many recent closed candles to scan
REQUIRE_FRESH_OB = os.environ.get("REQUIRE_FRESH_OB", "false").lower() == "true"  # only keep untested OBs
MAX_CONCURRENCY = int(os.environ.get("MAX_CONCURRENCY", "8"))
QUOTE           = os.environ.get("QUOTE", "USDT")
GENERATE_CHARTS = os.environ.get("GENERATE_CHARTS", "true").lower() == "true"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID")

# --- Chart appearance - edit these to restyle the PNG charts ---
CHART_CANDLES           = int(os.environ.get("CHART_CANDLES", "50"))
# Dark mode preset (TradingView-style dark theme). Swap CHART_BG_COLOR /
# CHART_TITLE_COLOR back to "#ffffff" / "#131722" for light mode.
CHART_BG_COLOR           = "#131722"   # chart background (figure + plot area)
CHART_UP_COLOR           = "#26a69a"   # bullish candle color
CHART_DOWN_COLOR         = "#ef5350"   # bearish candle color
CHART_OB_BULLISH_COLOR   = "#26a69a"   # bullish OB zone (box + border lines)
CHART_OB_BEARISH_COLOR   = "#ef5350"   # bearish OB zone (box + border lines)
CHART_OB_ZONE_ALPHA      = 0.15        # OB box fill opacity (0-1)
CHART_BOS_CHOCH_COLOR    = "#9598a1"   # BOS/CHoCH dashed line + label
CHART_SIGNAL_LINE_COLOR  = "#ffca28"   # vertical dotted line on the signal candle
CHART_TITLE_COLOR        = "#d1d4dc"   # title text color

# --- fixed indicator defaults (match the user's TradingView settings) ---
INTERNAL_LEN = 5      # LuxAlgo internal structure length (fixed in the script)
SWING_LEN    = 50     # LuxAlgo main "swing" length (default), only used so
                       # we know when an internal pivot == the main swing
                       # pivot (in which case LuxAlgo does NOT treat it as
                       # "internal" and skips it -- extraCondition check)
ATR_LEN      = 200
HV_MULT      = 2.0    # "high volatility bar" filter multiplier for OB source

PIVOT_LB = 5           # HH.pine pivot lookback left
PIVOT_RB = 5           # HH.pine pivot lookback right


# ---------------------------------------------------------------------------
# INDICATOR MATH
# ---------------------------------------------------------------------------
def rma(values: np.ndarray, length: int) -> np.ndarray:
    """Wilder's smoothing, matches Pine's ta.rma / ta.atr."""
    n = len(values)
    out = np.full(n, np.nan)
    if n < length:
        return out
    out[length - 1] = values[:length].mean()
    for i in range(length, n):
        out[i] = (out[i - 1] * (length - 1) + values[i]) / length
    return out


def compute_atr(high, low, close, length=ATR_LEN):
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]
    tr = np.maximum.reduce([
        high - low,
        np.abs(high - prev_close),
        np.abs(low - prev_close),
    ])
    return rma(tr, length)


@dataclass
class Pivot:
    level: float = float("nan")
    last_level: float = float("nan")
    crossed: bool = False
    bar_index: int = -1


@dataclass
class OrderBlock:
    bar_high: float
    bar_low: float
    bar_index: int
    bias: int   # +1 bullish, -1 bearish
    pivot_bar_index: int = -1   # bar the broken structure pivot came from
    break_bar_index: int = -1   # bar where the break (BOS/CHoCH) confirmed
    break_level: float = float("nan")  # price level that got broken
    tag: str = "BOS"            # "BOS" or "CHoCH"


def compute_legs(high: np.ndarray, low: np.ndarray, size: int):
    """
    Re-implementation of LuxAlgo's leg()/getCurrentStructure() pivot
    detection. Returns a dict bar_index -> ('high'|'low', price) for every
    bar where a new pivot is confirmed (i.e. the bar where the transition
    is detected, `size` bars after the actual pivot bar).
    """
    n = len(high)
    events = {}  # detection_bar_index -> (pivot_bar_index, 'high'/'low', price)
    cur_leg = 0  # 0 = bearish leg, 1 = bullish leg
    for i in range(size, n):
        window_high = high[i - size + 1:i + 1].max()
        window_low = low[i - size + 1:i + 1].min()
        new_leg_high = high[i - size] > window_high
        new_leg_low = low[i - size] < window_low

        new_leg = cur_leg
        if new_leg_high:
            new_leg = 0
        elif new_leg_low:
            new_leg = 1

        if new_leg != cur_leg:
            pivot_bar = i - size
            if new_leg == 1:
                events[i] = (pivot_bar, "low", low[pivot_bar])
            else:
                events[i] = (pivot_bar, "high", high[pivot_bar])
        cur_leg = new_leg
    return events


def compute_internal_order_blocks(df: pd.DataFrame):
    """
    Full per-bar replay of LuxAlgo's internal structure + internal Order
    Block store/mitigate logic (Mode=Historical, Confluence Filter=off,
    Order Block Filter=ATR, Order Block Mitigation=High/Low - i.e. the
    library defaults, matching the user's settings).

    Returns: (final_active_obs, obs_by_bar)
      final_active_obs - list of OrderBlock still ACTIVE as of the last bar
      obs_by_bar        - dict bar_index -> list of OrderBlock active as of
                           THAT bar (snapshot), so callers can check "was
                           there an active OB at candle i" for any i, not
                           just the last one.
    """
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    close = df["close"].to_numpy()
    n = len(df)

    atr = compute_atr(high, low, close, ATR_LEN)
    high_vol_bar = (high - low) >= (HV_MULT * atr)
    # parsed high/low: swapped on high-volatility bars (anomaly filter)
    parsed_high = np.where(high_vol_bar, low, high)
    parsed_low = np.where(high_vol_bar, high, low)

    internal_events = compute_legs(high, low, INTERNAL_LEN)
    swing_events = compute_legs(high, low, SWING_LEN)

    internal_high, internal_low = Pivot(), Pivot()
    swing_high_level, swing_low_level = float("nan"), float("nan")
    internal_trend_bias = 0  # 0 neutral, 1 bullish, -1 bearish

    order_blocks: list[OrderBlock] = []
    obs_by_bar: dict[int, list] = {}

    start = max(INTERNAL_LEN, SWING_LEN) + 1
    for i in range(start, n):
        if i in swing_events:
            _, kind, price = swing_events[i]
            if kind == "high":
                swing_high_level = price
            else:
                swing_low_level = price

        if i in internal_events:
            pivot_bar, kind, price = internal_events[i]
            if kind == "high":
                internal_high.last_level = internal_high.level
                internal_high.level = price
                internal_high.crossed = False
                internal_high.bar_index = pivot_bar
            else:
                internal_low.last_level = internal_low.level
                internal_low.level = price
                internal_low.crossed = False
                internal_low.bar_index = pivot_bar

        # --- bullish crossover of internalHigh -> BOS/CHoCH + store OB ---
        if (not np.isnan(internal_high.level) and not internal_high.crossed
                and close[i - 1] <= internal_high.level < close[i]
                and internal_high.level != swing_high_level):
            tag = "CHoCH" if internal_trend_bias == -1 else "BOS"
            internal_high.crossed = True
            internal_trend_bias = 1
            pb = internal_high.bar_index
            if i > pb:
                seg = parsed_low[pb:i]
                idx = pb + int(np.argmin(seg))
                order_blocks.insert(0, OrderBlock(
                    bar_high=parsed_high[idx], bar_low=parsed_low[idx],
                    bar_index=idx, bias=1, pivot_bar_index=pb,
                    break_bar_index=i, break_level=internal_high.level, tag=tag))

        # --- bearish crossunder of internalLow -> BOS/CHoCH + store OB ---
        if (not np.isnan(internal_low.level) and not internal_low.crossed
                and close[i - 1] >= internal_low.level > close[i]
                and internal_low.level != swing_low_level):
            tag = "CHoCH" if internal_trend_bias == 1 else "BOS"
            internal_low.crossed = True
            internal_trend_bias = -1
            pb = internal_low.bar_index
            if i > pb:
                seg = parsed_high[pb:i]
                idx = pb + int(np.argmax(seg))
                order_blocks.insert(0, OrderBlock(
                    bar_high=parsed_high[idx], bar_low=parsed_low[idx],
                    bar_index=idx, bias=-1, pivot_bar_index=pb,
                    break_bar_index=i, break_level=internal_low.level, tag=tag))

        # --- mitigation check every bar (Order Block Mitigation = High/Low) ---
        still_active = []
        for ob in order_blocks:
            mitigated = ((ob.bias == -1 and high[i] > ob.bar_high) or
                         (ob.bias == 1 and low[i] < ob.bar_low))
            if not mitigated:
                still_active.append(ob)
        order_blocks = still_active[:100]
        obs_by_bar[i] = list(order_blocks)

    return order_blocks, obs_by_bar


def compute_swing_labels(df: pd.DataFrame, lb=PIVOT_LB, rb=PIVOT_RB):
    """
    Port of the HH.pine pivot-based HH/HL/LH/LL classifier.
    Returns list of dicts: {bar_index, confirmed_at, label, price}
    'confirmed_at' = bar_index + rb (when the label actually becomes known,
    matching the indicator's lag).
    """
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    n = len(high)

    raw = []  # (bar_index, 'H'/'L', price)
    for j in range(lb, n - rb):
        left_h, right_h = high[j - lb:j], high[j + 1:j + rb + 1]
        if high[j] > left_h.max() and high[j] > right_h.max():
            raw.append((j, "H", high[j]))
            continue
        left_l, right_l = low[j - lb:j], low[j + 1:j + rb + 1]
        if low[j] < left_l.min() and low[j] < right_l.min():
            raw.append((j, "L", low[j]))

    raw.sort(key=lambda x: x[0])

    # dedupe consecutive same-type pivots, keep the more extreme one
    zz = []
    for bar_index, kind, price in raw:
        if zz and zz[-1][1] == kind:
            prev_bar, prev_kind, prev_price = zz[-1]
            if (kind == "H" and price >= prev_price) or (kind == "L" and price <= prev_price):
                zz[-1] = (bar_index, kind, price)
            # else: drop the new (less extreme) one
        else:
            zz.append((bar_index, kind, price))

    labels = []
    for k in range(len(zz)):
        bar_index, kind, a = zz[k]
        b = zz[k - 1][2] if k - 1 >= 0 else None
        c = zz[k - 2][2] if k - 2 >= 0 else None
        d = zz[k - 3][2] if k - 3 >= 0 else None
        e = zz[k - 4][2] if k - 4 >= 0 else None
        label = None
        if kind == "H" and b is not None and c is not None and d is not None:
            if a > b and a > c and c > b and c > d:
                label = "HH"
            elif (a <= c and (b < c and b < d and d < c and (e is None or d < e))) or (a > b and a < c and b > d):
                label = "LH"
        elif kind == "L" and b is not None and c is not None and d is not None:
            if a < b and a < c and c < b and c < d:
                label = "LL"
            elif (a >= c and (b > c and b > d and d > c and (e is None or d > e))) or (a < b and a > c and b < d):
                label = "HL"
        if label:
            labels.append({"bar_index": bar_index, "confirmed_at": bar_index + rb,
                            "label": label, "price": a})
    return labels


# ---------------------------------------------------------------------------
# MATCHING
# ---------------------------------------------------------------------------
def is_ob_fresh(high: np.ndarray, low: np.ndarray, ob: "OrderBlock", last_i: int) -> bool:
    """
    "Fresh" = since the candle that confirmed the break (ob.break_bar_index),
    price has NOT come back and touched the OB zone [bar_low, bar_high]
    again. A bullish OB is touched if a later candle's low dips back down
    into the zone; a bearish OB is touched if a later candle's high pokes
    back up into the zone.
    """
    start = ob.break_bar_index + 1
    if start > last_i:
        return True  # no candles yet since the break - nothing could have touched it
    if ob.bias == 1:
        return not bool((low[start:last_i + 1] <= ob.bar_high).any())
    else:
        return not bool((high[start:last_i + 1] >= ob.bar_low).any())


def evaluate_symbol(df: pd.DataFrame):
    """
    Looks at every internal OB that is still ACTIVE (unmitigated) as of
    the last closed candle. A match requires that OB's own candle (the
    exact candle it was built from) to ALSO be the exact candle of a
    same-direction swing label (LL/HL for a bullish OB, HH/LH for a
    bearish OB) - not just nearby, the same bar_index - and that candle
    must fall within the last SIGNAL_LOOKBACK closed candles (default 15).

    Each match also carries "fresh": True/False - whether the OB zone has
    been left untouched since its breakout confirmation candle (see
    is_ob_fresh). If REQUIRE_FRESH_OB is set, non-fresh matches are
    dropped entirely instead of just being labeled.
    """
    n = len(df)
    last_i = n - 1  # last CLOSED candle (caller must have already dropped
                     # the currently-forming candle)

    final_obs, _ = compute_internal_order_blocks(df)
    swings = compute_swing_labels(df)
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()

    swing_labels_by_bar = {}
    for s in swings:
        swing_labels_by_bar.setdefault(s["bar_index"], set()).add(s["label"])

    start_i = max(0, last_i - SIGNAL_LOOKBACK + 1)
    all_signals = []

    for ob in final_obs:
        if not (start_i <= ob.bar_index <= last_i):
            continue
        labels_here = swing_labels_by_bar.get(ob.bar_index, set())
        # A bullish OB candle is picked as the LOWEST point in its leg
        # (argmin parsedLow), so it naturally lands on a LOW-type swing
        # pivot: LL or HL. A bearish OB candle is the HIGHEST point in
        # its leg (argmax parsedHigh), landing on a HIGH-type pivot:
        # HH or LH.
        matched_bias = None
        matched_labels = None
        if ob.bias == 1 and labels_here & {"LL", "HL"}:
            matched_bias, matched_labels = "bullish", sorted(labels_here & {"LL", "HL"})
        elif ob.bias == -1 and labels_here & {"HH", "LH"}:
            matched_bias, matched_labels = "bearish", sorted(labels_here & {"HH", "LH"})

        if matched_bias is None:
            continue

        fresh = is_ob_fresh(high, low, ob, last_i)
        if REQUIRE_FRESH_OB and not fresh:
            continue

        all_signals.append({"bar_index": ob.bar_index, "bias": matched_bias, "ob": ob,
                             "matched_labels": matched_labels, "fresh": fresh})

    if not all_signals:
        return None

    all_signals.sort(key=lambda s: s["bar_index"])
    best = all_signals[-1]  # most recent qualifying candle in the window
    best["bars_ago"] = last_i - best["bar_index"]
    best["all_signals"] = all_signals
    return best


# ---------------------------------------------------------------------------
# DATA FETCHING
# ---------------------------------------------------------------------------
async def fetch_symbols(exchange):
    """
    exchange is ccxt.binanceusdm -> this ALREADY only contains USD-M
    futures (never COIN-M; that's a separate ccxt class, binancecoinm,
    which we never touch). On top of that we explicitly require:
      - swap (perpetual, not dated futures)
      - linear contract (USD-margined, not inverse)
      - quote == USDT AND settle == USDT (excludes USDC-margined pairs)
      - active (still tradeable)
    """
    markets = await exchange.load_markets()
    symbols = [
        m["symbol"] for m in markets.values()
        if m.get("swap") and m.get("linear") and m.get("active")
        and m.get("quote") == QUOTE and m.get("settle") == QUOTE
    ]
    return sorted(symbols)


async def fetch_and_evaluate(exchange, symbol, sem):
    async with sem:
        for attempt in range(3):
            try:
                ohlcv = await exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=CANDLE_LIMIT)
                break
            except Exception:
                if attempt == 2:
                    return None
                await asyncio.sleep(1.5 * (attempt + 1))
        if len(ohlcv) < max(SWING_LEN, ATR_LEN) + 20:
            return None
        df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
        df = df.iloc[:-1]  # drop currently-forming candle
        try:
            match = evaluate_symbol(df)
        except Exception:
            traceback.print_exc()
            return None
        if match:
            match["symbol"] = symbol
            match["df"] = df
            match["last_price"] = df["close"].iloc[-1]
            match["last_time"] = pd.to_datetime(df["ts"].iloc[-1], unit="ms", utc=True)
        return match


async def run_scan():
    exchange = ccxt.binanceusdm({"enableRateLimit": True})
    try:
        symbols = await fetch_symbols(exchange)
        print(f"Scanning {len(symbols)} {QUOTE} perpetual pairs on {TIMEFRAME}...")
        sem = asyncio.Semaphore(MAX_CONCURRENCY)
        tasks = [fetch_and_evaluate(exchange, s, sem) for s in symbols]
        results = []
        for coro in asyncio.as_completed(tasks):
            r = await coro
            if r:
                results.append(r)
        return results
    finally:
        await exchange.close()


# ---------------------------------------------------------------------------
# CHARTING
# ---------------------------------------------------------------------------
def render_chart(match) -> bytes | None:
    try:
        import mplfinance as mpf
    except ImportError:
        return None

    df = match["df"].copy()
    df["date"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.set_index("date")
    plot_df = df.tail(CHART_CANDLES)[["open", "high", "low", "close", "volume"]]
    window_start_idx = len(df) - len(plot_df)  # original df index of plot_df's first row

    ob = match["ob"]
    ob_color = CHART_OB_BULLISH_COLOR if ob.bias == 1 else CHART_OB_BEARISH_COLOR
    ob_time = df.index[ob.bar_index] if ob.bar_index < len(df) else plot_df.index[0]

    mc = mpf.make_marketcolors(up=CHART_UP_COLOR, down=CHART_DOWN_COLOR,
                                edge="inherit", wick="inherit")
    style = mpf.make_mpf_style(marketcolors=mc, facecolor=CHART_BG_COLOR,
                                figcolor=CHART_BG_COLOR, gridcolor=CHART_BG_COLOR)

    ago_txt = "latest candle" if match["bars_ago"] == 0 else f"{match['bars_ago']} candles ago"
    fresh_txt = " - FRESH OB" if match["fresh"] else " - retested OB"
    fig, axlist = mpf.plot(
        plot_df, type="candle", volume=False, style=style,
        returnfig=True, figsize=(9, 6),
    )
    ax = axlist[0]
    ax.set_title(f"{match['symbol']}  ({match['bias'].upper()} - signal {ago_txt}{fresh_txt})",
                  color=CHART_TITLE_COLOR, fontsize=13, fontweight="bold", pad=14)
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_xlabel("")
    ax.set_ylabel("")
    for spine in ax.spines.values():
        spine.set_visible(False)

    # --- OB zone (shaded box + dashed top/bottom lines) ---
    if ob_time in plot_df.index:
        x0 = plot_df.index.get_loc(ob_time)
        ax.axhspan(ob.bar_low, ob.bar_high, xmin=max(x0 - 0.5, 0) / len(plot_df),
                   xmax=1.0, color=ob_color, alpha=CHART_OB_ZONE_ALPHA)
        ax.axhline(ob.bar_high, color=ob_color, lw=0.8, ls="--")
        ax.axhline(ob.bar_low, color=ob_color, lw=0.8, ls="--")

    # --- BOS/CHoCH break line: horizontal dashed line from the old pivot
    # to the candle where price broke through it, plus a small label ---
    if ob.pivot_bar_index >= window_start_idx and ob.break_bar_index >= window_start_idx:
        x_pivot = ob.pivot_bar_index - window_start_idx
        x_break = ob.break_bar_index - window_start_idx
        if 0 <= x_pivot < len(plot_df) and 0 <= x_break < len(plot_df):
            ax.plot([x_pivot, x_break], [ob.break_level, ob.break_level],
                     color=CHART_BOS_CHOCH_COLOR, lw=1.0, ls="--")
            ax.text((x_pivot + x_break) / 2, ob.break_level, ob.tag,
                     color=CHART_BOS_CHOCH_COLOR, fontsize=8, ha="center",
                     va="bottom" if ob.bias == 1 else "top")

    # --- vertical marker on the matched signal candle ---
    signal_time = df.index[match["bar_index"]] if match["bar_index"] < len(df) else None
    if signal_time in plot_df.index:
        xs = plot_df.index.get_loc(signal_time)
        ax.axvline(xs, color=CHART_SIGNAL_LINE_COLOR, lw=1.2, ls=":")

    fig.patch.set_facecolor(CHART_BG_COLOR)
    ax.set_facecolor(CHART_BG_COLOR)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor=CHART_BG_COLOR)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# TELEGRAM OUTPUT
# ---------------------------------------------------------------------------
def send_telegram_message(text):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                              "parse_mode": "HTML"}, timeout=20)


def send_telegram_photo(png_bytes, caption):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    files = {"photo": ("chart.png", png_bytes)}
    requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
                  files=files, timeout=30)


def format_result_line(m):
    arrow = "🟢" if m["bias"] == "bullish" else "🔴"
    ago = "latest candle" if m["bars_ago"] == 0 else f"{m['bars_ago']} candles ago"
    fresh_tag = " 🆕fresh" if m["fresh"] else ""
    return (f"{arrow} <b>{m['symbol']}</b> - {m['bias']} ({ago}){fresh_tag} | "
            f"OB {m['ob'].bar_low:.4f}-{m['ob'].bar_high:.4f} | "
            f"swing: {','.join(m['matched_labels'])} | "
            f"price now {m['last_price']:.4f}")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    t0 = time.time()
    results = asyncio.run(run_scan())
    elapsed = time.time() - t0

    if not results:
        msg = f"✅ Scan complete ({elapsed:.0f}s). No matching pairs right now."
        print(msg)
        send_telegram_message(msg)
        return

    results.sort(key=lambda m: m["symbol"])
    lines = [f"✅ Scan complete ({elapsed:.0f}s) - {len(results)} matching pair(s):\n"]
    for m in results:
        lines.append(format_result_line(m))
    text = "\n".join(lines)
    print(text)
    send_telegram_message(text)

    if GENERATE_CHARTS:
        for m in results:
            png = render_chart(m)
            if png:
                send_telegram_photo(png, f"{m['symbol']} - {m['bias']}")
            time.sleep(1.1)  # stay under Telegram's ~1 msg/sec rate limit


if __name__ == "__main__":
    main()
