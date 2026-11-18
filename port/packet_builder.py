#!/usr/bin/env python3
"""
AI Agent Lens - exchange data port.

Rebuilds the exact same `ai-agent-lens/v1` packet from exchange OHLCV candles,
so the SAME webhook receiver + SAME AGENT_PROMPT.md work without TradingView.

Follows docs/SPECIFICATION.md. Stdlib only.

Usage:
    python port/packet_builder.py --symbol BTC_USDT --tf 1h --post
    python port/packet_builder.py --symbol ETH_USDT --tf 4h
"""
import argparse
import json
import math
import os
import urllib.request
from datetime import datetime, timezone

SCHEMA = "ai-agent-lens/v1"
GATE = "https://api.gateio.ws/api/v4/futures/usdt"


# ---------------------------------------------------------------- data layer
def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode())


def get_candles(symbol, tf, limit=400):
    """Return list of {t,o,h,l,c,v} oldest->newest from Gate.io futures."""
    raw = fetch(f"{GATE}/candlesticks?contract={symbol}&interval={tf}&limit={limit}")
    out = []
    for r in raw:
        o, h, l, c, v = (float(r[k]) for k in ("o", "h", "l", "c", "v"))
        if all(math.isfinite(x) for x in (o, h, l, c, v)):
            out.append({"t": int(r["t"]), "o": o, "h": h, "l": l, "c": c, "v": v})
    return out


# --------------------------------------------------------------- indicators
def ema(vals, n):
    if len(vals) < n:
        return [None] * len(vals)
    k = 2.0 / (n + 1)
    out = [None] * (n - 1) + [sum(vals[:n]) / n]
    for v in vals[n:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rma(vals, n):
    """Wilder smoothing (ta.rsi / ta.atr / ta.dmi)."""
    if len(vals) < n:
        return [None] * len(vals)
    out = [None] * (n - 1) + [sum(vals[:n]) / n]
    for v in vals[n:]:
        out.append((out[-1] * (n - 1) + v) / n)
    return out


def rsi(closes, n=14):
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    ag, al = rma(gains, n), rma(losses, n)
    out = [None]
    for g, l in zip(ag, al):
        if g is None or l is None:
            out.append(None)
        elif l == 0:
            out.append(100.0 if g > 0 else 50.0)
        else:
            out.append(100 - 100 / (1 + g / l))
    return out


def macd(closes):
    f, s = ema(closes, 12), ema(closes, 26)
    line = [None if a is None or b is None else a - b for a, b in zip(f, s)]
    valid = [x for x in line if x is not None]
    sig = ema(valid, 9)
    sig_full = [None] * (len(line) - len(valid)) + sig
    hist = [None if a is None or b is None else a - b for a, b in zip(line, sig_full)]
    return hist


def atr(bars, n=14):
    tr = []
    for i, b in enumerate(bars):
        if i == 0:
            tr.append(b["h"] - b["l"])
        else:
            pc = bars[i - 1]["c"]
            tr.append(max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc)))
    return rma(tr, n)


def dmi(bars, n=14):
    """Wilder DMI/ADX. Uses RAW true range (not smoothed ATR) in the denominator."""
    plus, minus, tr = [], [], []
    for i in range(1, len(bars)):
        up = bars[i]["h"] - bars[i - 1]["h"]
        dn = bars[i - 1]["l"] - bars[i]["l"]
        plus.append(up if up > dn and up > 0 else 0.0)
        minus.append(dn if dn > up and dn > 0 else 0.0)
        pc = bars[i - 1]["c"]
        tr.append(max(bars[i]["h"] - bars[i]["l"], abs(bars[i]["h"] - pc), abs(bars[i]["l"] - pc)))
    sp, sm, st = rma(plus, n), rma(minus, n), rma(tr, n)
    di_p, di_m, dx = [], [], []
    for p, m, t in zip(sp, sm, st):
        di_p.append(100 * p / t if t else 0.0)
        di_m.append(100 * m / t if t else 0.0)
        den = di_p[-1] + di_m[-1]
        dx.append(100 * abs(di_p[-1] - di_m[-1]) / den if den else 0.0)
    adx = rma(dx, n)
    pad = [None] * (len(bars) - len(adx))
    return pad + di_p, pad + di_m, pad + adx


def percentrank(vals, n=100):
    out = [None] * len(vals)
    for i in range(max(n - 1, 0), len(vals)):
        win = vals[i - n + 1:i + 1]
        cur = vals[i]
        if cur is None:
            continue
        valid = [x for x in win if x is not None]
        out[i] = 100.0 * sum(1 for x in valid if x < cur) / len(valid)
    return out


def sma(vals, n):
    return [None] * (n - 1) + [sum(vals[i - n + 1:i + 1]) / n for i in range(n - 1, len(vals))]


def vwap_session(bars):
    """UTC-day anchored VWAP (ta.vwap hlc3, session anchored)."""
    out = []
    cum_pv = cum_v = 0.0
    day = None
    for b in bars:
        d = datetime.fromtimestamp(b["t"], timezone.utc).date()
        if day is None or d != day:
            cum_pv = cum_v = 0.0
            day = d
        tp = (b["h"] + b["l"] + b["c"]) / 3
        cum_pv += tp * b["v"]
        cum_v += b["v"]
        out.append(cum_pv / cum_v if cum_v else None)
    return out


# ------------------------------------------------------------- candle shape
def pattern(b, prev, a):
    o, h, l, c = b["o"], b["h"], b["l"], b["c"]
    rng = h - l
    if rng == 0:
        return "FLAT"
    po, pc = (prev["o"], prev["c"]) if prev else (None, None)
    body = abs(c - o)
    uw, lw = h - max(o, c), min(o, c) - l
    if prev and body and abs(po - pc) > 0:
        if c > o and pc < po and c >= po and o <= pc:
            return "BULLISH_ENGULFING"
        if c < o and pc > po and c <= po and o >= pc:
            return "BEARISH_ENGULFING"
    if body <= 0.10 * rng:
        return "DOJI"
    if lw >= 2 * body and uw <= 0.15 * rng:
        return "HAMMER"
    if uw >= 2 * body and lw <= 0.15 * rng:
        return "SHOOTING_STAR"
    if body >= 0.90 * rng:
        return "BULLISH_MARUBOZU" if c > o else "BEARISH_MARUBOZU"
    if prev:
        if h < prev["h"] and l > prev["l"]:
            return "INSIDE_BAR"
        if h > prev["h"] and l < prev["l"]:
            return "OUTSIDE_BAR"
    if a and rng >= 1.8 * a:
        return "WIDE_RANGE_BULL" if c > o else "WIDE_RANGE_BEAR"
    return "NONE"


def candle(b, prev, a):
    o, h, l, c = b["o"], b["h"], b["l"], b["c"]
    rng = h - l
    body = abs(c - o)
    pct = lambda x: round(100 * x / rng, 1) if rng else 0.0
    return {
        "open": o, "high": h, "low": l, "close": c, "volume": b["v"],
        "direction": "bull" if c > o else "bear" if c < o else "flat",
        "body_pct": pct(body),
        "upper_wick_pct": pct(h - max(o, c)),
        "lower_wick_pct": pct(min(o, c) - l),
        "range_atr": round(rng / a, 2) if a else None,
        "close_position_pct": pct(c - l),
        "gap_pct": round((o - prev["c"]) / prev["c"] * 100, 3) if prev else None,
        "closed_above_prev_high": bool(prev and c > prev["h"]),
        "closed_below_prev_low": bool(prev and c < prev["l"]),
        "pattern": pattern(b, prev, a),
    }


# ---------------------------------------------------------------- structure
def pivots(bars, k=5):
    ph, pl = [], []
    for i in range(k, len(bars) - k):
        h, l = bars[i]["h"], bars[i]["l"]
        if all(h > bars[i + j]["h"] and h > bars[i - j]["h"] for j in range(1, k + 1)):
            ph.append((i, h))
        if all(l < bars[i + j]["l"] and l < bars[i - j]["l"] for j in range(1, k + 1)):
            pl.append((i, l))
    return ph, pl


# --------------------------------------------------------------- assemble
def build(symbol, tf, htf):
    bars = get_candles(symbol, tf)
    ht = get_candles(symbol, htf, 400)
    daily = get_candles(symbol, "1d", 10)
    if len(bars) < 210 or len(ht) < 60 or len(daily) < 2:
        raise RuntimeError("not enough candles")

    c = [b["c"] for b in bars]
    v = [b["v"] for b in bars]
    e20, e50, e200 = ema(c, 20), ema(c, 50), ema(c, 200)
    R = rsi(c, 14)
    H = macd(c)
    A = atr(bars, 14)
    dp, dm, adx = dmi(bars, 14)
    AP = percentrank(A, 100)
    RV = [None if s is None or s == 0 else x / s for x, s in zip(v, sma(v, 20))]
    VW = vwap_session(bars)

    hc = [b["c"] for b in ht]
    he = ema(hc, 50)

    # HTF bias uses CLOSED htf bar [1]
    htf_close, htf_ema = hc[-2], he[-2]
    htf_bias = "UP" if htf_close > htf_ema else "DOWN" if htf_close < htf_ema else "FLAT"

    i = len(bars) - 1
    a = A[i]
    slope = e200[i] - e200[i - 5] if e200[i] and e200[i - 5] else 0

    # regime (first match wins)
    if AP[i] is not None and AP[i] >= 90:
        regime = "HIGH_VOLATILITY"
    elif c[i] > e200[i] and slope > 0 and adx[i] >= 20:
        regime = "TREND_UP"
    elif c[i] < e200[i] and slope < 0 and adx[i] >= 20:
        regime = "TREND_DOWN"
    else:
        regime = "RANGE"

    bull = c[i] > bars[i]["o"]
    # long score
    trend_l = 7 * (e20[i] > e50[i]) + 7 * (e50[i] > e200[i]) + 6 * (c[i] > e20[i])
    mom_l = (10 if (R[i] > 50 and R[i] < 75) else 4 if R[i] >= 75 else 0)
    mom_l += 5 * (H[i] > 0) + 5 * (H[i] > H[i - 1])
    st_l = 10 * (dp[i] > dm[i]) + 10 * (adx[i] >= 20)
    vol_l = 20 if (bull and RV[i] >= 1.5) else 12 if (bull and RV[i] >= 1.0) else 6 if bull else 0
    htf_l = 20 if htf_bias == "UP" else 10 if htf_bias in ("FLAT", "N/A") else 0
    long_score = trend_l + mom_l + st_l + vol_l + htf_l

    trend_s = 7 * (e20[i] < e50[i]) + 7 * (e50[i] < e200[i]) + 6 * (c[i] < e20[i])
    mom_s = (10 if (R[i] < 50 and R[i] > 25) else 4 if R[i] <= 25 else 0)
    mom_s += 5 * (H[i] < 0) + 5 * (H[i] < H[i - 1])
    st_s = 10 * (dm[i] > dp[i]) + 10 * (adx[i] >= 20)
    vol_s = 20 if (not bull and RV[i] >= 1.5) else 12 if (not bull and RV[i] >= 1.0) else 6 if not bull else 0
    htf_s = 20 if htf_bias == "DOWN" else 10 if htf_bias in ("FLAT", "N/A") else 0
    short_score = trend_s + mom_s + st_s + vol_s + htf_s

    # previous-bar scores to find threshold cross
    def score_at(j):
        t = 7 * (e20[j] > e50[j]) + 7 * (e50[j] > e200[j]) + 6 * (c[j] > e20[j])
        m = (10 if (R[j] > 50 and R[j] < 75) else 4 if R[j] >= 75 else 0) + 5 * (H[j] > 0) + 5 * (H[j] > H[j - 1])
        s = 10 * (dp[j] > dm[j]) + 10 * (adx[j] >= 20)
        bl = c[j] > bars[j]["o"]
        vo = 20 if (bl and RV[j] >= 1.5) else 12 if (bl and RV[j] >= 1.0) else 6 if bl else 0
        return t + m + s + vo + (20 if htf_bias == "UP" else 10)

    prev_long = score_at(i - 1)
    event = "NONE"
    if long_score >= 70 and prev_long < 70 and regime not in ("TREND_DOWN", "HIGH_VOLATILITY"):
        event = "LONG_SETUP"
    elif short_score >= 70 and regime not in ("TREND_UP", "HIGH_VOLATILITY"):
        event = "SHORT_SETUP"

    # structure
    ph, pl = pivots(bars, 5)
    swing_h = ph[-1][1] if ph else None
    swing_l = pl[-1][1] if pl else None
    swing_ph2 = ph[-2][1] if len(ph) > 1 else None
    swing_pl2 = pl[-2][1] if len(pl) > 1 else None
    if swing_h and swing_ph2 and swing_l and swing_pl2:
        sbias = "BULLISH" if (swing_h > swing_ph2 and swing_l > swing_pl2) else "BEARISH" if (swing_h < swing_ph2 and swing_l < swing_pl2) else "MIXED"
    else:
        sbias = "MIXED"
    sev = "NONE"
    if swing_h and c[i - 1] <= swing_h < c[i]:
        sev = "BREAK_UP"
    elif swing_l and c[i - 1] >= swing_l > c[i]:
        sev = "BREAK_DOWN"

    # trade / risk map
    trade = {"side": "NONE", "status": "NONE", "entry": None, "stop": None,
             "target_1": None, "target_2": None, "bars_since_entry": None}
    side = "LONG" if event == "LONG_SETUP" else "SHORT" if event == "SHORT_SETUP" else None
    if side and a:
        entry = c[i]
        stop = entry - 1.5 * a if side == "LONG" else entry + 1.5 * a
        r = abs(entry - stop)
        t1 = entry + 1.5 * r if side == "LONG" else entry - 1.5 * r
        t2 = entry + 3 * r if side == "LONG" else entry - 3 * r
        trade = {"side": side, "status": "ACTIVE", "entry": round(entry, 5),
                 "stop": round(stop, 5), "target_1": round(t1, 5), "target_2": round(t2, 5),
                 "bars_since_entry": 0}

    recent = []
    for j in range(max(0, i - 4), i + 1):
        recent.append({
            "bars_ago": i - j, "o": bars[j]["o"], "h": bars[j]["h"],
            "l": bars[j]["l"], "c": bars[j]["c"], "v": bars[j]["v"],
            "dir": "bull" if bars[j]["c"] > bars[j]["o"] else "bear",
            "body_pct": candle(bars[j], bars[j - 1] if j else None, A[j])["body_pct"],
            "range_atr": round((bars[j]["h"] - bars[j]["l"]) / A[j], 2) if A[j] else None,
            "pattern": pattern(bars[j], bars[j - 1] if j else None, A[j]),
        })

    return {
        "schema": SCHEMA,
        "event": event,
        "symbol": f"GATE:{symbol}",
        "timeframe": tf,
        "time": datetime.fromtimestamp(bars[i]["t"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "state_code": f"LENS|{symbol}|{tf}|{regime}|L{long_score}|S{short_score}|{trade['side']}:{trade['status']}",
        "candle": candle(bars[i], bars[i - 1], a),
        "recent_candles": recent,
        "regime": regime,
        "trend": {
            "ema_fast": round(e20[i], 5), "ema_mid": round(e50[i], 5), "ema_slow": round(e200[i], 5),
            "stack": "BULLISH" if e20[i] > e50[i] > e200[i] else "BEARISH" if e20[i] < e50[i] < e200[i] else "MIXED",
            "slow_slope": "RISING" if slope > 0 else "FALLING" if slope < 0 else "FLAT",
            "htf": htf, "htf_bias": htf_bias,
        },
        "momentum": {"rsi": round(R[i], 1), "macd_hist": round(H[i], 5), "macd_hist_rising": H[i] > H[i - 1]},
        "strength": {"adx": round(adx[i], 1), "di_plus": round(dp[i], 1), "di_minus": round(dm[i], 1)},
        "volatility": {"atr": round(a, 5), "atr_percentile": round(AP[i], 0) if AP[i] is not None else None},
        "volume": {"relative": round(RV[i], 2) if RV[i] is not None else None},
        "structure": {"bias": sbias, "event": sev, "last_swing_high": round(swing_h, 5) if swing_h else None,
                      "last_swing_low": round(swing_l, 5) if swing_l else None},
        "levels": {"prev_day_high": daily[-2]["h"], "prev_day_low": daily[-2]["l"],
                   "vwap": round(VW[i], 5) if VW[i] else None},
        "scores": {"long": long_score, "short": short_score, "threshold": 70,
                   "long_factors": {"trend": trend_l, "momentum": mom_l, "strength": st_l, "volume": vol_l, "htf": htf_l},
                   "short_factors": {"trend": trend_s, "momentum": mom_s, "strength": st_s, "volume": vol_s, "htf": htf_s}},
        "trade": trade,
        "last_setup": {"side": trade["side"], "bars_ago": trade["bars_since_entry"]},
        "_source": "gateio-futures (exchange data port, no TradingView)",
    }


def resolve_webhook_token(arg, args_dict=None):
    """Token comes from the CLI flag, then env, then port/webhook_token.txt.

    Never stored in tracked config, so this repo can be public safely.
    Returns "" when not configured.
    """
    if arg:
        return arg
    env = os.environ.get("LENS_WEBHOOK_TOKEN", "").strip()
    if env:
        return env
    local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webhook_token.txt")
    if os.path.exists(local):
        try:
            with open(local, encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            pass
    return ""


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="BTC_USDT")
    p.add_argument("--tf", default="1h")
    p.add_argument("--htf", default="4h")
    p.add_argument("--post", action="store_true", help="POST packet to local webhook receiver")
    p.add_argument("--url", default="http://localhost:8080/webhook")
    p.add_argument("--token", default=None,
                   help="receiver token; or env LENS_WEBHOOK_TOKEN; or port/webhook_token.txt")
    p.add_argument("--out", default=None)
    args = p.parse_args()

    packet = build(args.symbol, args.tf, args.htf)
    body = json.dumps(packet, indent=2)
    if args.out:
        open(args.out, "w", encoding="utf-8").write(body)
    if args.post:
        token = resolve_webhook_token(args.token)
        if not token:
            print("POST: skipped - no receiver token. Set env LENS_WEBHOOK_TOKEN "
                  "or create port/webhook_token.txt (gitignored).")
        else:
            req = urllib.request.Request(
                f"{args.url}?token={token}", data=body.encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            print("POST:", urllib.request.urlopen(req, timeout=15).read().decode())
    print("=" * 60)
    print(f"PACKET  {packet['symbol']} {packet['timeframe']}  {packet['time']}")
    print(f"EVENT   {packet['event']} | REGIME {packet['regime']} | SCORES L{packet['scores']['long']} S{packet['scores']['short']}")
    print(f"PRICE   {packet['candle']['close']} | RSI {packet['momentum']['rsi']} | ADX {packet['strength']['adx']}")
    print(f"TRADE   {packet['trade']}")
    print(f"CANDLE  {packet['candle']['pattern']} close_pos={packet['candle']['close_position_pct']}% range_atr={packet['candle']['range_atr']}")
    print(f"STRUCT  {packet['structure']}")
    if not args.out and not args.post:
        print("\n--packet-json--\n" + body)


if __name__ == "__main__":
    main()