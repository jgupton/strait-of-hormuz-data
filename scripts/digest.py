#!/usr/bin/env python3
"""
Morning digest for the strait-of-hormuz-data fork.
Runs after refresh.sh in the same GitHub Action.

Reads:  data/*.csv|json   (already refreshed upstream mirror)
Pulls:  NASA FIRMS thermal anomalies (free key), yfinance prices + options chains
Writes: docs/data/brief.json   (dashboard payload)
        docs/data/options-history.csv  (appended daily, enables deltas)
Alerts: optional Twilio SMS when any trigger fires (secrets, skipped if absent)

Not investment advice. This reports data.
"""

import csv
import io
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = ROOT / "docs" / "data"
OUT.mkdir(parents=True, exist_ok=True)

NOW = datetime.now(timezone.utc)

# ---------------------------------------------------------------- watchlist
# Oil-linked + tanker equities with liquid options.
TICKERS = ["USO", "XLE", "XOM", "FRO", "STNG", "TNK", "INSW", "NAT"]

# Unusual-activity thresholds
VOL_OI_RATIO = 1.5      # today's contract volume vs open interest
PC_SHIFT = 0.35         # put/call ratio move vs trailing average
FIRMS_BOX = "54.0,24.5,58.5,27.5"   # west,south,east,north — Strait of Hormuz


def log(msg):
    print(f"[digest] {msg}", flush=True)


def safe(fn, label, default=None):
    try:
        return fn()
    except Exception as e:
        log(f"WARN {label} failed: {e}")
        return default


# ---------------------------------------------------------------- repo data
def load_status():
    p = DATA / "status.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text())


def load_transits():
    p = DATA / "transits.csv"
    if not p.exists():
        return []
    with p.open() as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k in ("n_total", "n_tanker", "n_cargo"):
            try:
                r[k] = float(r.get(k) or 0)
            except ValueError:
                r[k] = 0.0
    return rows


def transit_deltas(rows):
    if len(rows) < 8:
        return None
    last = rows[-1]
    prev7 = rows[-8:-1]
    avg7 = sum(r["n_total"] for r in prev7) / 7.0
    ma7 = sum(r["n_total"] for r in rows[-7:]) / 7.0
    return {
        "date": last.get("date"),
        "total": last["n_total"],
        "tanker": last["n_tanker"],
        "avg7_prior": round(avg7, 1),
        "delta_vs_avg7": round(last["n_total"] - avg7, 1),
        "ma7": round(ma7, 1),
        # Kalshi/Polymarket reopening contracts resolve on 7d MA vs 60
        "ma7_vs_60_threshold": round(ma7 - 60.0, 1),
    }


def recent_events():
    p = DATA / "events.csv"
    if not p.exists():
        return []
    with p.open() as f:
        rows = list(csv.DictReader(f))
    cutoff = NOW - timedelta(hours=72)
    out = []
    for r in rows:
        try:
            t = datetime.fromisoformat(r["occurred_at_iso"].replace("Z", "+00:00"))
        except Exception:
            continue
        if t >= cutoff:
            out.append({
                "time": t.isoformat(),
                "type": r.get("type", ""),
                "severity": r.get("severity", ""),
                "title": r.get("title", ""),
                "source": r.get("source_name", ""),
                "url": r.get("source_url", ""),
            })
    out.sort(key=lambda x: x["time"], reverse=True)
    return out[:12]


def staleness_check(status):
    """Upstream mirror can fail silently — flag if status.json is old."""
    ts = None
    for key in ("as_of", "updated_at", "timestamp", "generated_at"):
        if key in status:
            ts = status[key]
            break
    if not ts:
        return {"stale": None, "note": "no timestamp field found in status.json"}
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        age_h = (NOW - t).total_seconds() / 3600.0
        return {"stale": age_h > 36, "age_hours": round(age_h, 1)}
    except Exception:
        return {"stale": None, "note": f"unparseable timestamp: {ts}"}


# ---------------------------------------------------------------- FIRMS
def firms_thermal():
    """Overnight thermal anomalies in the Hormuz bounding box (last 2 days)."""
    key = os.environ.get("FIRMS_MAP_KEY")
    if not key:
        return {"available": False, "note": "set FIRMS_MAP_KEY secret to enable"}
    url = (f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/"
           f"{key}/VIIRS_SNPP_NRT/{FIRMS_BOX}/2")
    with urllib.request.urlopen(url, timeout=30) as r:
        text = r.read().decode()
    rows = list(csv.DictReader(io.StringIO(text)))
    # crude offshore filter: anything not hugging the coasts of the box.
    # real version: point-in-polygon against a land mask.
    detections = [{
        "lat": row.get("latitude"), "lon": row.get("longitude"),
        "date": row.get("acq_date"), "time": row.get("acq_time"),
        "confidence": row.get("confidence"), "frp": row.get("frp"),
    } for row in rows]
    return {"available": True, "count_48h": len(detections),
            "detections": detections[:20]}


# ---------------------------------------------------------------- markets
def market_pull():
    """Prices + options flow via yfinance. Needs pip install yfinance."""
    import yfinance as yf

    prices = {}
    for sym, label in [("BZ=F", "brent"), ("CL=F", "wti")]:
        t = safe(lambda s=sym: yf.Ticker(s).history(period="5d"), f"price {sym}")
        if t is not None and len(t) >= 2:
            last, prev = float(t["Close"].iloc[-1]), float(t["Close"].iloc[-2])
            prices[label] = {"last": round(last, 2),
                             "chg_pct": round(100 * (last / prev - 1), 2)}

    flow = []
    for sym in TICKERS:
        row = safe(lambda s=sym: options_snapshot(yf, s), f"options {sym}")
        if row:
            flow.append(row)
    return prices, flow


def options_snapshot(yf, sym):
    tk = yf.Ticker(sym)
    hist = tk.history(period="2d")
    spot = float(hist["Close"].iloc[-1]) if len(hist) else None

    expiries = tk.options[:3]  # near-dated is where event positioning shows
    call_vol = put_vol = call_oi = put_oi = 0
    unusual = []
    for exp in expiries:
        ch = tk.option_chain(exp)
        for kind, df in (("C", ch.calls), ("P", ch.puts)):
            vol = df["volume"].fillna(0)
            oi = df["openInterest"].fillna(0)
            if kind == "C":
                call_vol += int(vol.sum()); call_oi += int(oi.sum())
            else:
                put_vol += int(vol.sum()); put_oi += int(oi.sum())
            # unusual: fresh volume swamping existing OI on a real position
            hot = df[(vol > 200) & (vol > VOL_OI_RATIO * oi.clip(lower=1))]
            for _, r in hot.iterrows():
                unusual.append({
                    "expiry": exp, "type": kind,
                    "strike": float(r["strike"]),
                    "vol": int(r["volume"]),
                    "oi": int(r["openInterest"] or 0),
                    "vol_oi": round(float(r["volume"]) /
                                    max(float(r["openInterest"] or 0), 1), 1),
                    "iv": round(float(r.get("impliedVolatility") or 0), 3),
                })
    unusual.sort(key=lambda x: x["vol_oi"], reverse=True)
    pc = round(put_vol / max(call_vol, 1), 2)
    return {"symbol": sym, "spot": round(spot, 2) if spot else None,
            "call_vol": call_vol, "put_vol": put_vol, "pc_ratio": pc,
            "unusual": unusual[:5]}


def append_history_and_deltas(flow):
    """Persist today's per-ticker summary; compute P/C shift vs trailing 5d."""
    hp = OUT / "options-history.csv"
    fieldnames = ["date", "symbol", "call_vol", "put_vol", "pc_ratio"]
    existing = []
    if hp.exists():
        with hp.open() as f:
            existing = list(csv.DictReader(f))
    today = NOW.strftime("%Y-%m-%d")
    existing = [r for r in existing if r["date"] != today]  # idempotent re-runs
    for row in flow:
        existing.append({"date": today, "symbol": row["symbol"],
                         "call_vol": row["call_vol"], "put_vol": row["put_vol"],
                         "pc_ratio": row["pc_ratio"]})
    with hp.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader(); w.writerows(existing)

    for row in flow:
        hist = [float(r["pc_ratio"]) for r in existing
                if r["symbol"] == row["symbol"] and r["date"] != today]
        if len(hist) >= 3:
            avg = sum(hist[-5:]) / len(hist[-5:])
            row["pc_avg5"] = round(avg, 2)
            row["pc_shift"] = round(row["pc_ratio"] - avg, 2)
            row["pc_flag"] = abs(row["pc_shift"]) >= PC_SHIFT
        else:
            row["pc_avg5"] = None; row["pc_shift"] = None; row["pc_flag"] = False
    return flow


# ---------------------------------------------------------------- alerts
def build_alerts(transits, events, firms, flow, stale):
    alerts = []
    if stale.get("stale"):
        alerts.append(f"UPSTREAM STALE: status.json is {stale['age_hours']}h old")
    if transits and abs(transits["delta_vs_avg7"]) >= 8:
        alerts.append(f"TRANSITS {transits['delta_vs_avg7']:+.0f} vs 7d avg "
                      f"({transits['total']:.0f} on {transits['date']})")
    sev = [e for e in events if e["type"] in ("strike", "closure")]
    if sev:
        alerts.append(f"{len(sev)} strike/closure event(s) in last 72h: "
                      f"{sev[0]['title']}")
    if firms.get("available") and firms.get("count_48h", 0) > 3:
        alerts.append(f"FIRMS: {firms['count_48h']} thermal anomalies in "
                      f"Hormuz box (48h)")
    for row in flow:
        if row.get("pc_flag"):
            alerts.append(f"{row['symbol']} P/C {row['pc_ratio']} "
                          f"({row['pc_shift']:+.2f} vs 5d avg)")
        for u in row.get("unusual", [])[:1]:
            if u["vol_oi"] >= 3:
                alerts.append(f"{row['symbol']} {u['expiry']} {u['strike']}"
                              f"{u['type']}: vol {u['vol']} = "
                              f"{u['vol_oi']}x OI")
    return alerts


def send_sms(alerts):
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    tok = os.environ.get("TWILIO_AUTH_TOKEN")
    frm = os.environ.get("TWILIO_FROM")
    to = os.environ.get("ALERT_PHONE")
    if not all([sid, tok, frm, to]) or not alerts:
        return
    body = "HORMUZ/WATCH " + NOW.strftime("%b %d") + "\n" + "\n".join(alerts[:6])
    import base64
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    data = urllib.parse.urlencode({"From": frm, "To": to, "Body": body}).encode()
    req = urllib.request.Request(url, data=data)
    auth = base64.b64encode(f"{sid}:{tok}".encode()).decode()
    req.add_header("Authorization", f"Basic {auth}")
    urllib.request.urlopen(req, timeout=30)
    log(f"SMS sent ({len(alerts)} alerts)")


# ---------------------------------------------------------------- main
def main():
    status = safe(load_status, "status.json", {})
    stale = safe(lambda: staleness_check(status), "staleness", {})
    transit_rows = safe(load_transits, "transits.csv", [])
    transits = safe(lambda: transit_deltas(transit_rows), "transit deltas")
    events = safe(recent_events, "events.csv", [])
    firms = safe(firms_thermal, "FIRMS", {"available": False})
    prices, flow = safe(market_pull, "markets", ({}, []))
    flow = safe(lambda: append_history_and_deltas(flow), "options history", flow)
    alerts = build_alerts(transits or {}, events, firms, flow, stale or {})

    brief = {
        "generated_at": NOW.isoformat(),
        "stale": stale,
        "alerts": alerts,
        "transits": transits,
        "prices": prices,
        "options_flow": flow,
        "events": events,
        "firms": {k: v for k, v in firms.items() if k != "detections"} | {
            "detections": firms.get("detections", [])[:8]},
        "disclaimer": "Data reporting only. Not investment advice.",
    }
    (OUT / "brief.json").write_text(json.dumps(brief, indent=2))
    log(f"brief.json written — {len(alerts)} alert(s)")
    safe(lambda: send_sms(alerts), "twilio")


if __name__ == "__main__":
    import urllib.parse  # used in send_sms
    sys.exit(main())
