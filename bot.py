"""Hyperliquid copy-trading PAPER bot. Usage: python bot.py [refresh|poll|report]"""
import json, os, sys, time
from datetime import datetime, timezone
import requests

INFO = "https://api.hyperliquid.xyz/info"
LEADERBOARD = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
STATE_FILE = "state.json"
START_CASH = 10_000.0
TOP_N = 5
POSITION_FRACTION = 0.10   # each copied position = 10% of current equity (1x, no leverage)
MIN_ACCOUNT = 100_000      # ignore small accounts
MAX_ACCOUNT = 20_000_000   # whales/vaults are not realistically copyable
MAX_MONTH_ROI = 3.0        # >300%/month is almost always a data glitch
MAX_VLM_RATIO = 50         # monthly volume / account value; above this = likely market maker/HFT


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load():
    if os.path.exists(STATE_FILE):
        return json.load(open(STATE_FILE))
    return {"traders": [], "snapshots": {}, "cash": START_CASH, "positions": {},
            "trades": [], "realized_pnl": 0.0, "updated": None}


def save(s):
    s["updated"] = now()
    json.dump(s, open(STATE_FILE, "w"), indent=2)


def post(payload):
    r = requests.post(INFO, json=payload, timeout=20)
    r.raise_for_status()
    return r.json()


def perf(row, window):
    for name, p in row["windowPerformances"]:
        if name == window:
            return {k: float(v) for k, v in p.items()}
    return {"pnl": 0, "roi": 0, "vlm": 0}


# ---------- Job A: pick traders ----------
def refresh():
    s = load()
    rows = requests.get(LEADERBOARD, timeout=60).json()["leaderboardRows"]
    scored = []
    for r in rows:
        av = float(r["accountValue"])
        if av < MIN_ACCOUNT or av > MAX_ACCOUNT:
            continue
        d, w, m, a = (perf(r, k) for k in ("day", "week", "month", "allTime"))
        if not (a["pnl"] > 0 and m["pnl"] > 0 and w["pnl"] >= 0):
            continue                      # must be profitable across timeframes
        if m["roi"] <= 0 or m["roi"] > MAX_MONTH_ROI or a["roi"] > 50:
            continue
        if m["vlm"] / av > MAX_VLM_RATIO:
            continue                      # skip HFT / market makers (can't copy at 5-min lag)
        scored.append((m["roi"] + a["roi"] * 0.25, r["ethAddress"], av, m["roi"]))
    scored.sort(reverse=True)
    s["traders"] = [{"address": a, "account_value": av, "month_roi": roi}
                    for _, a, av, roi in scored[:TOP_N]]
    # drop snapshots of traders no longer followed (their paper positions stay until closed by poll)
    save(s)
    print("Following:", [t["address"] for t in s["traders"]])


# ---------- Job B: poll + paper trade ----------
def positions_of(addr):
    st = post({"type": "clearinghouseState", "user": addr})
    out = {}
    for ap in st["assetPositions"]:
        p = ap["position"]
        szi = float(p["szi"])
        if szi != 0:
            out[p["coin"]] = szi
    return out


def equity(s, mids):
    eq = s["cash"]
    for pos in s["positions"].values():
        px = float(mids.get(pos["coin"], pos["entry_px"]))
        eq += pos["notional"] + pos["qty"] * (px - pos["entry_px"])
    return eq


def close_pos(s, key, px, reason):
    pos = s["positions"].pop(key)
    pnl = pos["qty"] * (px - pos["entry_px"])
    s["cash"] += pos["notional"] + pnl
    s["realized_pnl"] += pnl
    s["trades"].append({"t": now(), "action": "SELL" if pos["qty"] > 0 else "COVER",
                        "coin": pos["coin"], "px": px, "pnl": round(pnl, 2), "trader": pos["trader"], "why": reason})


def open_pos(s, key, coin, szi, px, addr, mids):
    notional = min(equity(s, mids) * POSITION_FRACTION, s["cash"])
    if notional < 10:
        return
    qty = notional / px * (1 if szi > 0 else -1)
    s["cash"] -= notional
    s["positions"][key] = {"coin": coin, "qty": qty, "entry_px": px, "notional": notional, "trader": addr}
    s["trades"].append({"t": now(), "action": "BUY" if szi > 0 else "SHORT",
                        "coin": coin, "px": px, "notional": round(notional, 2), "trader": addr})


def poll():
    s = load()
    if not s["traders"]:
        print("No traders yet. Run refresh first."); return
    mids = post({"type": "allMids"})
    for t in s["traders"]:
        addr = t["address"]
        try:
            cur = positions_of(addr)
        except Exception as e:
            print("skip", addr, e); continue
        print(addr[:10], "open positions:", len(cur))
        prev = s["snapshots"].get(addr)
        s["snapshots"][addr] = cur
        if prev is None:
            continue                      # first sight: record only, don't copy old positions
        for coin in set(prev) | set(cur):
            key = f"{addr}:{coin}"
            old, new = prev.get(coin, 0), cur.get(coin, 0)
            px = float(mids.get(coin, 0))
            if px <= 0:
                continue
            flipped = old and new and (old > 0) != (new > 0)
            if (old and not new) or flipped:
                if key in s["positions"]:
                    close_pos(s, key, px, "trader closed" if not flipped else "trader flipped")
            if (new and not old) or flipped:
                open_pos(s, key, coin, new, px, addr, mids)
    s["trades"] = s["trades"][-500:]
    if not s.get("history"):
        record_equity(s, equity(s, mids))
    save(s)
    print("Equity:", round(equity(s, mids), 2), "open:", len(s["positions"]))


def record_equity(s, eq):
    h = s.setdefault("history", [])
    h.append({"t": now(), "equity": round(eq, 2)})
    s["history"] = h[-3000:]   # ~2 months at one point per 30 min


# ---------- Job C: report ----------
def report():
    s = load()
    mids = post({"type": "allMids"})
    eq = equity(s, mids)
    record_equity(s, eq)
    save(s)
    cutoff = time.time() - 86400
    recent = [t for t in s["trades"] if datetime.fromisoformat(t["t"]).timestamp() > cutoff]
    lines = [f"*Copy-trade paper portfolio*",
             f"Equity: ${eq:,.2f} ({(eq / START_CASH - 1) * 100:+.2f}% since start)",
             f"Realized PnL: ${s['realized_pnl']:,.2f} | Cash: ${s['cash']:,.2f}",
             f"Open positions: {len(s['positions'])} | Trades last 24h: {len(recent)}"]
    for p in s["positions"].values():
        px = float(mids.get(p["coin"], p["entry_px"]))
        lines.append(f"• {'LONG' if p['qty'] > 0 else 'SHORT'} {p['coin']}: {p['qty'] * (px - p['entry_px']):+.2f} USD")
    text = "\n".join(lines)
    print(text)
    hook = os.environ.get("SLACK_WEBHOOK_URL")
    if hook:
        requests.post(hook, json={"text": text}, timeout=20)


if __name__ == "__main__":
    {"refresh": refresh, "poll": poll, "report": report}[sys.argv[1]]()
