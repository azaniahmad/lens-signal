#!/usr/bin/env python3
"""
AI Agent Lens - automatic runner.

Every N seconds it:
  1. builds an ai-agent-lens/v1 packet from exchange candles,
  2. POSTs it to the local webhook receiver,
  3. applies AGENT_PROMPT.md decision rules to produce a signal JSON,
  4. saves signals to port/signals/.

Account rules are enforced mechanically from runner_config.json.
Stdlib only.

Usage:
    python port/runner.py --once          # one pass then exit
    python port/runner.py                 # loop forever
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from packet_builder import build  # noqa: E402
import telegram_notify as tn  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CFG = os.path.join(HERE, "runner_config.json")
STATE = os.path.join(HERE, "telegram_state.json")


def load_cfg():
    with open(CFG, encoding="utf-8") as f:
        return json.load(f)


def post(url, token, packet):
    body = json.dumps(packet).encode()
    req = urllib.request.Request(
        f"{url}?token={token}", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=15).read().decode())


def webhook_token(cfg):
    """Receiver token: explicit config value, else env, else gitignored local file.

    Deliberately not stored in tracked files so the repo can be public.
    """
    t = str(cfg.get("webhook", {}).get("token", "")).strip()
    if t:
        return t
    t = os.environ.get("LENS_WEBHOOK_TOKEN", "").strip()
    if t:
        return t
    local = os.path.join(HERE, "webhook_token.txt")
    if os.path.exists(local):
        try:
            with open(local, encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            pass
    return ""


def decide(p, cfg):
    """Mechanical version of AGENT_PROMPT.md decision rules."""
    sc = p["scores"]
    reg = p["regime"]
    tr = p["trade"]
    lev_cap = cfg["max_leverage"]
    acct = cfg["account_usd"]
    risk = cfg["risk_pct"] / 100.0
    min_rr = cfg["min_rr_t2"]

    out = {
        "symbol": p["symbol"], "timeframe": p["timeframe"],
        "packet_time": p["time"], "event": p["event"], "regime": reg,
        "decision": "WAIT", "confidence": 0,
        "entry": None, "stop": None, "target_1": None, "target_2": None,
        "risk_reward_t2": None, "size_units": None, "risk_amount": None,
        "required_leverage": None,
        "order_type": {
            "entry": "LIMIT (maker, zero fee on Lighter, spread ~0.0001%)",
            "stop": "STOP_MARKET (NEVER stop-limit: can fail to fill on a fast wick)",
            "target_1": "LIMIT",
            "target_2": "LIMIT",
        },
        "entry_mode": "place_limit_at_entry -> on fill, set STOP_MARKET at stop + LIMIT at t1/t2. Do not chase; a missed fill is fine. Do not move the stop.",
        "execution_note": "Runner emits signals only. It never places orders — execution stays on the exchange, decided by you.",
        "reasons": [], "concerns": [],
    }

    if reg == "HIGH_VOLATILITY" and cfg.get("block_high_volatility", True):
        out.update(decision="SKIP", confidence=90,
                   concerns=["HIGH_VOLATILITY regime: setups blocked by config."])
        return out

    if p["event"] not in ("LONG_SETUP", "SHORT_SETUP"):
        out.update(confidence=20,
                   concerns=[f"No setup event. Scores L{sc['long']} / S{sc['short']} below threshold {sc['threshold']}."])
        return out

    side = "LONG" if p["event"] == "LONG_SETUP" else "SHORT"
    htf = p["trend"]["htf_bias"]
    if cfg.get("no_against_htf", True):
        if (side == "LONG" and htf == "DOWN") or (side == "SHORT" and htf == "UP"):
            out.update(decision="SKIP", confidence=88,
                       concerns=[f"{side} fights HTF bias {htf}."])
            return out

    if tr["side"] != side or tr["stop"] is None:
        out.update(decision="WAIT", confidence=40, concerns=["No valid trade map in packet."])
        return out

    entry, stop = tr["entry"], tr["stop"]
    t1, t2 = tr["target_1"], tr["target_2"]
    r = abs(entry - stop)
    rr2 = abs(t2 - entry) / r

    if rr2 < min_rr:
        out.update(decision="SKIP", confidence=85, risk_reward_t2=round(rr2, 2),
                   concerns=[f"R:R at T2 {rr2:.2f} below minimum {min_rr}."])
        return out

    # next level blocking path to T1
    lv = p["levels"]
    blocked = None
    if side == "SHORT":
        for name in ("prev_day_low", "vwap"):
            x = lv.get(name)
            if x is not None and t1 < x < entry:
                blocked = f"{name} {x} sits between entry and T1"
                break
    else:
        for name in ("prev_day_high", "vwap"):
            x = lv.get(name)
            if x is not None and entry < x < t1:
                blocked = f"{name} {x} sits between entry and T1"
                break
    if blocked:
        out.update(decision="SKIP", risk_reward_t2=round(rr2, 2),
                   concerns=[f"Next level blocks path to T1: {blocked}."])

    # factors / confidence
    f = sc["long_factors"] if side == "LONG" else sc["short_factors"]
    aligned = sum(1 for v in f.values() if v >= 12)
    weak = [k for k, v in f.items() if v < 12]
    conf = 45 + 6 * aligned
    if weak:
        conf -= 8 * len(weak)

    # position sizing and the leverage reality check
    risk_amt = acct * risk
    units = risk_amt / r
    notional = units * entry
    req_lev = notional / acct

    out.update(
        entry=round(entry, 5), stop=round(stop, 5),
        target_1=round(t1, 5), target_2=round(t2, 5),
        risk_reward_t2=round(rr2, 2),
        size_units=round(units, 8), risk_amount=round(risk_amt, 4),
        required_leverage=round(req_lev, 3),
        confidence=max(0, min(conf, 85)),
        reasons=[
            f"{side}_SETUP fired, score {sc[side.lower()]} >= threshold {sc['threshold']}, regime {reg}.",
            f"R:R at T2 = {rr2:.2f}, R = {round(r, 5)}.",
        ],
        concerns=out["concerns"] + ([f"Weak factors: {', '.join(weak)}."] if weak else []),
    )

    if req_lev > lev_cap:
        out["concerns"].append(
            f"Required leverage {req_lev:.2f}x exceeds cap {lev_cap}x. "
            f"Size reduced to {round(units, 8)} units; DO NOT use 50x on ${acct:.2f}."
        )
        out["decision"] = "SKIP" if req_lev > 10 else out["decision"]
        if out["decision"] == "SKIP" and not blocked:
            out["confidence"] = 80
    return out


def run_once(cfg):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    outdir = os.path.join(ROOT, cfg.get("out_dir", "port/signals"))
    os.makedirs(outdir, exist_ok=True)
    results = []
    for s in cfg["symbols"]:
        try:
            p = build(s["symbol"], s["tf"], s["htf"])
            if cfg["webhook"]["post"]:
                tok = webhook_token(cfg)
                if tok:
                    post(cfg["webhook"]["url"], tok, p)
                else:
                    print(f"[{s['symbol']:10} {s['tf']:4}] webhook token missing (skipped POST)")
            sig = decide(p, cfg)
            results.append(sig)
            fn = os.path.join(outdir, f"{stamp}_{s['symbol']}_{s['tf']}.json")
            with open(fn, "w", encoding="utf-8") as f:
                json.dump({"packet": p, "signal": sig}, f, indent=2)
            print(f"[{s['symbol']:10} {s['tf']:4}] {sig['decision']:5} "
                  f"conf={sig['confidence']:2} "
                  f"L{p['scores']['long']}/S{p['scores']['short']} "
                  f"lev_req={sig['required_leverage']}")
        except Exception as e:
            print(f"[{s['symbol']:10} {s['tf']:4}] ERROR {repr(e)[:90]}")
    with open(os.path.join(outdir, f"{stamp}_summary.json"), "w", encoding="utf-8") as f:
        json.dump({"time": stamp, "config": cfg, "signals": results}, f, indent=2)
    notify(results, cfg, stamp)
    return results


def _load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"sent": [], "runs": 0}


def _save_state(state):
    try:
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass


def notify(results, cfg, stamp):
    """Telegram: alert on new LONG/SHORT, optional periodic status heartbeat."""
    tg = cfg.get("telegram", {})
    if not tg.get("enabled", False):
        return
    token, chat_id = tn.load_credentials()
    if not token or not chat_id:
        tn._log("Telegram enabled but not configured. "
                "Run: python port/telegram_setup.py <BOT_TOKEN>")
        return

    state = _load_state()
    sent = set(state.get("sent", []))

    def fingerprint(s):
        return (f"{s['symbol']}|{s['timeframe']}|{s['decision']}|"
                f"{s['entry']}|{s['stop']}|{s['target_2']}")

    new_signals = []
    for s in results:
        if s["decision"] not in ("LONG", "SHORT"):
            continue
        fp = fingerprint(s)
        if fp not in sent:
            sent.add(fp)
            new_signals.append(s)

    state["sent"] = sorted(sent)[-200:]  # keep the list bounded
    state["runs"] = state.get("runs", 0) + 1
    if new_signals:
        for s in new_signals:
            tn.send(tn.format_signal(s, cfg), token, chat_id)
            print(f"[telegram] {s['decision']} alert sent "
                  f"({s['symbol']} {s['timeframe']})")
        state["last_signal_alert"] = stamp
    _save_state(state)

    # heartbeat: proves the loop is alive without any actionable signal
    if tg.get("notify_on_status", True) and not new_signals:
        every = int(tg.get("status_every_runs", 6))
        if every > 0 and state["runs"] % every == 0:
            tn.send(tn.format_status(results, cfg, stamp), token, chat_id)
            print(f"[telegram] status heartbeat sent (run #{state['runs']})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--config", default=None,
                   help="path to a config file (default: port/runner_config.json)")
    args = ap.parse_args()
    global CFG
    if args.config:
        CFG = args.config
    cfg = load_cfg()
    if args.once:
        run_once(cfg)
        return
    print(f"Runner started: interval={cfg['interval_seconds']}s, "
          f"account=${cfg['account_usd']}, max_lev={cfg['max_leverage']}x, "
          f"telegram={'on' if cfg.get('telegram', {}).get('enabled') else 'off'}")
    while True:
        try:
            run_once(cfg)
        except Exception as e:
            print("loop error", repr(e)[:120])
        time.sleep(cfg["interval_seconds"])


if __name__ == "__main__":
    main()