#!/usr/bin/env python3
"""
GexLevels GEX-pijplijn — gratis (CBOE) of betaald (Polygon)
────────────────────────────────────────────────────────────────
Haalt de optieketen op, berekent per strike de dealer gamma-exposure en
destilleert daaruit alle GexLevels-niveaus:

  • Gamma Flip            (nul-doorgang van cumulatieve netto GEX)
  • Call Wall / Put Wall  (grootste call- resp. put-side gamma-concentratie)
  • 0DTE-varianten        (alleen contracten die vandaag expireren)
  • Session Ceiling/Floor (verwachte dagrange uit ATM implied volatility)
  • Γ-1 … Γ-10            (gerangschikte overige gamma-concentraties)
  • Correlated 1 … 10     (zelfde berekening op bv. SPY)

Databronnen:
  DATA_SOURCE=cboe     (default) — gratis, ~15 min delayed, geen key nodig
                        endpoint: cdn.cboe.com/api/global/delayed_quotes/options/<TICKER>.json
                        (indexen met underscore: _SPX, _NDX, _VIX)
  DATA_SOURCE=polygon  — realtime, vereist POLYGON_API_KEY (Options-abonnement)

Let op: open interest wordt hoe dan ook maar 1× per dag bijgewerkt (OCC),
dus voor GEX-levels is de gratis delayed feed functioneel vrijwel gelijkwaardig.

Output:
  1. data/*.csv        → Pine Seeds formaat (YYYYMMDDT,open,high,low,close,volume)
  2. paste_string.txt  → kant-en-klare bulk-paste string (fallback voor de indicator)

Omgevingsvariabelen:
  DATA_SOURCE       cboe | polygon        (default: cboe)
  POLYGON_API_KEY   alleen bij polygon
  UNDERLYING        default: QQQ
  CORRELATED        default: SPY
  STRIKE_RANGE_PCT  default: 0.15   (strikes binnen ±15% van spot)
  MAX_DTE           default: 60     (expiraties tot 60 dagen vooruit)
"""

from __future__ import annotations

import os
import re
import sys
import math
import time
import datetime as dt
from collections import defaultdict
from pathlib import Path

import requests

DATA_SOURCE = os.environ.get("DATA_SOURCE", "cboe").lower()
API_KEY     = os.environ.get("POLYGON_API_KEY", "")
UNDERLYING  = os.environ.get("UNDERLYING", "QQQ")
CORRELATED  = os.environ.get("CORRELATED", "SPY")
RANGE_PCT   = float(os.environ.get("STRIKE_RANGE_PCT", "0.15"))
MAX_DTE     = int(os.environ.get("MAX_DTE", "60"))

DATA_DIR = Path(__file__).parent / "data"
UA = {"User-Agent": "Mozilla/5.0 (gex-levels-pipeline; educational use)"}

# Genormaliseerd contract: {"strike","type","exp","gamma","oi","iv"}


# ────────────────────────────── Bron 1: CBOE (gratis) ──────────────────────────────

OCC_RE = re.compile(r"^([A-Z^._]+?)(\d{6})([CP])(\d{8})$")

def parse_occ(symbol: str) -> tuple[dt.date, str, float] | None:
    """'QQQ260620C00700000' → (2026-06-20, 'call', 700.0)"""
    m = OCC_RE.match(symbol.strip())
    if not m:
        return None
    _, ymd, cp, strike = m.groups()
    exp = dt.datetime.strptime(ymd, "%y%m%d").date()
    return exp, ("call" if cp == "C" else "put"), int(strike) / 1000.0


def fetch_cboe(ticker: str) -> tuple[list[dict], float]:
    """Gratis delayed keten van CBOE. Indexen: geef ticker met underscore (bv. _SPX)."""
    url = f"https://cdn.cboe.com/api/global/delayed_quotes/options/{ticker.upper()}.json"
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    payload = r.json()
    d = payload.get("data") or {}
    spot = float(d.get("current_price") or d.get("close") or 0)
    out: list[dict] = []
    for o in d.get("options", []):
        parsed = parse_occ(o.get("option", ""))
        if not parsed:
            continue
        exp, typ, strike = parsed
        gamma = o.get("gamma")
        oi = o.get("open_interest") or 0
        iv = o.get("iv")
        if gamma is None or not oi:
            continue
        out.append({"strike": strike, "type": typ, "exp": exp,
                    "gamma": float(gamma), "oi": float(oi),
                    "iv": float(iv) if iv else None})
    if not spot:
        sys.exit(f"FOUT: geen spotprijs in CBOE-respons voor {ticker}")
    return out, spot


# ────────────────────────────── Bron 2: Polygon (betaald) ──────────────────────────────

def fetch_polygon(ticker: str) -> tuple[list[dict], float]:
    if not API_KEY:
        sys.exit("FOUT: DATA_SOURCE=polygon vereist POLYGON_API_KEY")
    base = "https://api.polygon.io"
    results: list[dict] = []
    spot = float("nan")
    url = f"{base}/v3/snapshot/options/{ticker}"
    params = {"limit": 250, "apiKey": API_KEY}
    while url:
        r = requests.get(url, params=params, timeout=30)
        if r.status_code == 429:
            time.sleep(2)
            continue
        r.raise_for_status()
        payload = r.json()
        for c in payload.get("results", []):
            det, greeks = c.get("details") or {}, c.get("greeks") or {}
            k, gamma = det.get("strike_price"), greeks.get("gamma")
            oi, typ = c.get("open_interest") or 0, det.get("contract_type")
            exp_s = det.get("expiration_date")
            if math.isnan(spot):
                p = (c.get("underlying_asset") or {}).get("price")
                if p:
                    spot = float(p)
            if k is None or gamma is None or not oi or typ not in ("call", "put") or not exp_s:
                continue
            results.append({"strike": float(k), "type": typ,
                            "exp": dt.date.fromisoformat(exp_s),
                            "gamma": float(gamma), "oi": float(oi),
                            "iv": c.get("implied_volatility")})
        url = payload.get("next_url")
        params = {"apiKey": API_KEY}
    if math.isnan(spot):
        r = requests.get(f"{base}/v2/last/trade/{ticker}", params={"apiKey": API_KEY}, timeout=30)
        r.raise_for_status()
        spot = float(r.json()["results"]["p"])
    return results, spot


def fetch_chain(ticker: str) -> tuple[list[dict], float]:
    return fetch_polygon(ticker) if DATA_SOURCE == "polygon" else fetch_cboe(ticker)


# ────────────────────────────── GEX-berekening ──────────────────────────────

def compute_gex(chain: list[dict], spot: float, today: dt.date) -> dict:
    per_strike: dict[float, dict] = defaultdict(lambda: {"call": 0.0, "put": 0.0})
    per_strike_0dte: dict[float, dict] = defaultdict(lambda: {"call": 0.0, "put": 0.0})
    atm_ivs: list[float] = []

    lo, hi = spot * (1 - RANGE_PCT), spot * (1 + RANGE_PCT)
    max_exp = today + dt.timedelta(days=MAX_DTE)

    for c in chain:
        k, exp = c["strike"], c["exp"]
        if not (lo <= k <= hi) or exp < today or exp > max_exp:
            continue
        gex = c["gamma"] * c["oi"] * 100 * spot     # $-gamma per 1%-move
        per_strike[k][c["type"]] += gex
        if exp == today:
            per_strike_0dte[k][c["type"]] += gex
        if c["iv"] and abs(k - spot) / spot <= 0.01 and (exp - today).days <= 5:
            atm_ivs.append(float(c["iv"]))

    def derive(strikes: dict[float, dict]) -> dict:
        if not strikes:
            return {}
        ks = sorted(strikes)
        net = {k: strikes[k]["call"] - strikes[k]["put"] for k in ks}
        call_wall = max(ks, key=lambda k: strikes[k]["call"])
        put_wall  = max(ks, key=lambda k: strikes[k]["put"])
        cum, crossings, prev = 0.0, [], None
        for k in ks:
            cum += net[k]
            if prev is not None and (prev < 0 <= cum or prev > 0 >= cum):
                crossings.append(k)
            prev = cum
        flip = min(crossings, key=lambda k: abs(k - spot)) if crossings else min(ks, key=lambda k: abs(k - spot))
        rest = [k for k in ks if k not in (call_wall, put_wall)]
        ranked = sorted(rest, key=lambda k: abs(net[k]), reverse=True)[:10]
        return {"flip": flip, "call_wall": call_wall, "put_wall": put_wall, "gamma_levels": ranked}

    full, dte0 = derive(per_strike), derive(per_strike_0dte)

    # CBOE geeft IV soms in procenten (bv. 18.5) i.p.v. decimaal (0.185) → normaliseren
    ivs = [v / 100 if v > 3 else v for v in atm_ivs]
    iv = sorted(ivs)[len(ivs) // 2] if ivs else 0.0
    daily_move = spot * iv * math.sqrt(1 / 252) if iv else 0.0
    session = {"ceiling": round(spot + daily_move, 2), "floor": round(spot - daily_move, 2)} if daily_move else {}

    return {"spot": spot, "full": full, "0dte": dte0, "session": session}


# ────────────────────────────── Pine Seeds output ──────────────────────────────

def upsert_csv(name: str, date_key: str, row: list[float]) -> None:
    """Rij van vandaag schrijven/actualiseren in data/<NAME>.csv.
    Formaat: YYYYMMDDT,open,high,low,close,volume  (verifieer tegen Pine Seeds template)."""
    DATA_DIR.mkdir(exist_ok=True)
    path = DATA_DIR / f"{name}.csv"
    rows: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().strip().splitlines():
            if line:
                rows[line.split(",", 1)[0]] = line
    vals = ",".join(f"{v:.2f}" for v in row)
    rows[date_key] = f"{date_key},{vals}"
    path.write_text("\n".join(rows[k] for k in sorted(rows)) + "\n")


def g(lst: list[float], i: int) -> float:
    return float(lst[i]) if i < len(lst) else 0.0


def main() -> None:
    today = dt.date.today()
    date_key = today.strftime("%Y%m%dT")
    version = float(today.strftime("%Y%m%d"))
    pfx = UNDERLYING.upper().lstrip("_")

    print(f"→ Bron: {DATA_SOURCE}")
    print(f"→ Ophalen {UNDERLYING} keten…")
    chain, spot = fetch_chain(UNDERLYING)
    res = compute_gex(chain, spot, today)
    print(f"   spot={spot:.2f}, contracten={len(chain)}")

    print(f"→ Ophalen {CORRELATED} keten…")
    cchain, cspot = fetch_chain(CORRELATED)
    cres = compute_gex(cchain, cspot, today)
    print(f"   spot={cspot:.2f}, contracten={len(cchain)}")

    f, d0, ses = res["full"], res["0dte"], res["session"]
    gl  = f.get("gamma_levels", [])
    cgl = ([cres["full"].get("call_wall", 0), cres["full"].get("put_wall", 0), cres["full"].get("flip", 0)]
           + cres["full"].get("gamma_levels", []))[:10]

    upsert_csv(f"{pfx}_CORE",  date_key, [f.get("flip", 0), f.get("call_wall", 0), f.get("put_wall", 0), d0.get("flip", 0), version])
    upsert_csv(f"{pfx}_INTRA", date_key, [d0.get("call_wall", 0), d0.get("put_wall", 0), ses.get("ceiling", 0), ses.get("floor", 0), version])
    upsert_csv(f"{pfx}_G1_5",  date_key, [g(gl, 0), g(gl, 1), g(gl, 2), g(gl, 3), g(gl, 4)])
    upsert_csv(f"{pfx}_G6_10", date_key, [g(gl, 5), g(gl, 6), g(gl, 7), g(gl, 8), g(gl, 9)])
    upsert_csv(f"{pfx}_C1_5",  date_key, [g(cgl, 0), g(cgl, 1), g(cgl, 2), g(cgl, 3), g(cgl, 4)])
    upsert_csv(f"{pfx}_C6_10", date_key, [g(cgl, 5), g(cgl, 6), g(cgl, 7), g(cgl, 8), g(cgl, 9)])

    parts = []
    def add(key: str, val: float | None):
        if val:
            parts.append(f"{key}:{val:g}")
    add("GF", f.get("flip")); add("CW", f.get("call_wall")); add("PW", f.get("put_wall"))
    add("GF0", d0.get("flip")); add("CW0", d0.get("call_wall")); add("PW0", d0.get("put_wall"))
    add("SC", ses.get("ceiling")); add("SF", ses.get("floor"))
    for i, k in enumerate(gl[:10], 1):
        add(f"G{i}", k)
    for i, k in enumerate(cgl[:10], 1):
        add(f"C{i}", k)
    paste = " ".join(parts)
    (Path(__file__).parent / "paste_string.txt").write_text(paste + "\n")

    print("\n=== PASTE STRING ===")
    print(paste)
    print("\n✓ CSV's geschreven naar ./data — klaar voor Pine Seeds sync")


if __name__ == "__main__":
    main()
