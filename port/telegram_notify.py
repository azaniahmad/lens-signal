#!/usr/bin/env python3
"""
AI Agent Lens - Telegram notifier.

Sends LONG/SHORT signals (and optional periodic status heartbeats) to a
Telegram chat via the Bot API. Stdlib only.

Token / chat id resolution order:
  1. environment  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
  2. port/telegram_config.json  ("bot_token", "chat_id")
  3. port/telegram_token.txt    (token only, one line)

Never raises into the runner: a failed notification must not stop the
signal loop. Every failure is logged to port/telegram.log instead.
"""
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "telegram.log")
CFG_JSON = os.path.join(HERE, "telegram_config.json")
TOKEN_TXT = os.path.join(HERE, "telegram_token.txt")


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{ts} {msg}\n")
    except Exception:
        pass


def load_credentials():
    """Return (token, chat_id). Empty string when not configured."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    cfg = {}
    if os.path.exists(CFG_JSON):
        try:
            with open(CFG_JSON, encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
    if not token:
        token = str(cfg.get("bot_token", "")).strip()
    if not chat_id:
        chat_id = str(cfg.get("chat_id", "")).strip()
    if not token and os.path.exists(TOKEN_TXT):
        try:
            with open(TOKEN_TXT, encoding="utf-8") as f:
                token = f.read().strip()
        except Exception:
            pass
    return token, chat_id


def api_call(token, method, payload=None, timeout=15):
    """Call a Telegram Bot API method. Returns parsed JSON or raises."""
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(payload).encode() if payload else None
    req = urllib.request.Request(url, data=data, method="POST" if payload else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


import urllib.parse  # noqa: E402  (used by api_call)


def send(text, token=None, chat_id=None, silent=False):
    """Send a message. Returns True on success, False on failure."""
    if not text:
        return False
    if token is None or chat_id is None:
        token, chat_id = load_credentials()
    if not token or not chat_id:
        if not silent:
            _log("SKIP send: telegram not configured (missing bot_token or chat_id)")
        return False
    try:
        res = api_call(token, "sendMessage", {
            "chat_id": chat_id,
            "text": text[:4096],
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        })
        if not res.get("ok"):
            _log(f"FAIL send: {res}")
            return False
        _log(f"OK sent ({len(text)} chars) to {chat_id}")
        return True
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        _log(f"FAIL http {e.code}: {body}")
        return False
    except Exception as e:
        _log(f"FAIL exception: {repr(e)[:200]}")
        return False


def get_me(token=None):
    if token is None:
        token, _ = load_credentials()
    return api_call(token, "getMe")


def discover_chat_id(token):
    """Scan recent updates and return candidate chat ids (most recent first)."""
    res = api_call(token, "getUpdates", {"limit": 100, "timeout": 0})
    found = []
    for upd in res.get("result", []):
        for key in ("message", "edited_message", "channel_post", "my_chat_member"):
            m = upd.get(key)
            if m and m.get("chat"):
                cid = str(m["chat"]["id"])
                title = (m["chat"].get("title") or m["chat"].get("username")
                         or m["chat"].get("first_name") or "")
                if cid not in [c[0] for c in found]:
                    found.append((cid, title))
    return found


# ------------------------------------------------------------- formatting
def _fmt_num(x, nd=2):
    if x is None:
        return "n/a"
    try:
        return f"{float(x):,.{nd}f}"
    except Exception:
        return str(x)


def format_signal(sig, cfg):
    """Format one LONG/SHORT signal as Telegram HTML."""
    side = "LONG" if sig.get("event") == "LONG_SETUP" else "SHORT"
    icon = "\U0001F7E2" if side == "LONG" else "\U0001F534"
    sym = sig["symbol"].replace("GATE:", "")
    acct = cfg.get("account_usd", 0)
    risk_pct = cfg.get("risk_pct", 1)

    lines = [
        f"{icon} <b>{side} SIGNAL - {sym} {sig['timeframe']}</b>",
        "",
        f"Confidence: <b>{sig.get('confidence')}</b>   "
        f"Regime: {sig.get('regime')}   Event: {sig.get('event')}",
        f"Packet time: {sig.get('packet_time')}",
        "",
        f"Entry  (LIMIT):       <b>{_fmt_num(sig.get('entry'), 2)}</b>",
        f"Stop   (STOP_MARKET): <b>{_fmt_num(sig.get('stop'), 2)}</b>",
        f"TP1    (LIMIT):       <b>{_fmt_num(sig.get('target_1'), 2)}</b>",
        f"TP2    (LIMIT):       <b>{_fmt_num(sig.get('target_2'), 2)}</b>",
        f"R:R at TP2: <b>{sig.get('risk_reward_t2')}</b>",
        "",
        f"Size: <b>{sig.get('size_units')}</b> {sym.split('_')[0]}",
        f"Risk: ${sig.get('risk_amount')} ({risk_pct}% of ${acct})",
        f"Required leverage: <b>{sig.get('required_leverage')}x</b> "
        f"(cap {cfg.get('max_leverage')}x)",
        "",
    ]
    reasons = sig.get("reasons") or []
    if reasons:
        lines.append("<b>Reasons</b>")
        lines += [f"- {r}" for r in reasons]
        lines.append("")
    concerns = sig.get("concerns") or []
    if concerns:
        lines.append("<b>Concerns</b>")
        lines += [f"- {c}" for c in concerns]
        lines.append("")
    lines += [
        "<b>Execution</b>",
        "1. Place LIMIT order at entry",
        "2. On fill: STOP_MARKET at stop",
        "3. LIMIT at TP1 and TP2",
        "",
        "<i>Do not chase a missed fill. Do not move the stop. "
        "Do not add to the position.</i>",
    ]
    return "\n".join(lines)


def format_status(signals, cfg, stamp):
    """Periodic heartbeat: one line per chart + overall verdict."""
    lines = [f"\U0001F4CA <b>AI Agent Lens status</b> - {stamp} UTC", ""]
    actionable = []
    for s in signals:
        d = s.get("decision")
        mark = {"LONG": "\U0001F7E2", "SHORT": "\U0001F534"}.get(d, "⚪")
        lines.append(
            f"{mark} {s['symbol'].replace('GATE:', '')} {s['timeframe']} - "
            f"<b>{d}</b> (conf {s.get('confidence')})"
        )
        if d in ("LONG", "SHORT"):
            actionable.append(s)
    lines.append("")
    if actionable:
        lines.append(f"⚡ <b>{len(actionable)} actionable signal(s)</b> - see above.")
    else:
        lines.append("No LONG/SHORT. No orders to place.")
    lines.append("")
    lines.append(f"Account ${cfg.get('account_usd')} | risk {cfg.get('risk_pct')}% | "
                 f"max leverage {cfg.get('max_leverage')}x | every {cfg.get('interval_seconds')}s")
    return "\n".join(lines)


if __name__ == "__main__":
    token, chat_id = load_credentials()
    print("token:", "set" if token else "MISSING")
    print("chat_id:", chat_id or "MISSING")
