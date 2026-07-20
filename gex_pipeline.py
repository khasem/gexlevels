#!/usr/bin/env python3
"""
GexLevels GEX-pijplijn — gratis CBOE-data
────────────────────────────────────────────────────────────────
Haalt de optieketen op, berekent per strike de dealer gamma-exposure en
destilleert daaruit alle GexLevels-niveaus:

  • Gamma Flip            (profielmethode: tekenwissel van totale dealer-gamma
                           over hypothetische spots; FLIP_METHOD=cumulative voor
                           de oude nul-doorgang van cumulatieve netto GEX)
  • Call Wall / Put Wall  (grootste call- resp. put-side gamma-concentratie)
  • 0DTE-varianten        (alleen contracten die vandaag expireren)
  • Session Ceiling/Floor (verwachte dagrange uit ATM implied volatility)
  • Γ-1 … Γ-10            (gerangschikte overige gamma-concentraties)
  • Correlated 1 … 10     (zelfde berekening op bv. SPY)

Databron: CBOE delayed quotes — gratis, ~15 min vertraagd, geen key nodig.
  endpoint: cdn.cboe.com/api/global/delayed_quotes/options/<TICKER>.json
  (indexen met underscore: _SPX, _NDX, _VIX)

Let op: open interest wordt hoe dan ook maar 1× per dag bijgewerkt (OCC),
dus voor GEX-levels is de gratis delayed feed functioneel vrijwel gelijkwaardig.

Output:
  1. data/*.csv        → Pine Seeds formaat (YYYYMMDDT,open,high,low,close,volume)
  2. paste_string.txt  → kant-en-klare bulk-paste string (fallback voor de indicator)

Omgevingsvariabelen:
  UNDERLYING        default: QQQ
  CORRELATED        default: SPY    (komma-gescheiden lijst mogelijk)
  STRIKE_RANGE_PCT  default: 0.15   (strikes binnen ±15% van spot)
  MAX_DTE           default: 60     (expiraties tot 60 dagen vooruit)
  FLIP_METHOD       default: profile     (of: cumulative)
  GAMMA_RANK        default: total       (of: net)
  EM_FACTOR         default: 0.85        (expected-move-fractie van de ATM-straddle)
  SPOT_DELAY_MIN    default: 15          (feedvertraging-correctie voor het TS-anker)
  DATA_SOURCE       default: cboe        (enige ondersteunde bron)
"""

from __future__ import annotations

import os
import re
import sys
import math
import time
import json
import datetime as dt
from collections import defaultdict
from pathlib import Path

import requests

CALC_VERSION = "1.3.2"
EM_FACTOR   = float(os.environ.get("EM_FACTOR", "0.85"))  # expected move ≈ 85% van de ATM-straddle (publieke benadering)
FLIP_METHOD = os.environ.get("FLIP_METHOD", "profile")     # "profile" (gamma-profiel vs spot) of "cumulative" (oude methode)
GAMMA_RANK  = os.environ.get("GAMMA_RANK", "total")        # "total" (call+put gamma) of "net" (|call−put|, oude methode)
UNDERLYING  = os.environ.get("UNDERLYING", "QQQ")
CORRELATED  = os.environ.get("CORRELATED", "SPY")          # komma-gescheiden lijst mogelijk, bv. "SPY,IWM"
CORR_LIST   = [s.strip() for s in CORRELATED.split(",") if s.strip()]
RANGE_PCT   = float(os.environ.get("STRIKE_RANGE_PCT", "0.15"))
MAX_DTE     = int(os.environ.get("MAX_DTE", "60"))
SPOT_DELAY_MIN = float(os.environ.get("SPOT_DELAY_MIN", "15"))  # CBOE delayed ≈ 15 min

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


def parse_cboe_ts(payload: dict) -> int | None:
    """Tijdstempel van de CBOE-quote zelf (Eastern Time) → epoch-seconden.
    Cruciaal buiten markturen: de spot in de payload is dan bv. de vrijdagclose,
    en het TS-anker moet naar dát moment wijzen — niet naar 'nu'."""
    from zoneinfo import ZoneInfo
    raw = payload.get("timestamp") or (payload.get("data") or {}).get("last_trade_time")
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            naive = dt.datetime.strptime(str(raw).strip(), fmt)
        except ValueError:
            continue
        return int(naive.replace(tzinfo=ZoneInfo("America/New_York")).timestamp())
    return None


def fetch_cboe(ticker: str) -> tuple[list[dict], float, int | None]:
    """Gratis delayed keten van CBOE. Indexen: geef ticker met underscore (bv. _SPX)."""
    url = f"https://cdn.cboe.com/api/global/delayed_quotes/options/{ticker.upper()}.json"
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    payload = r.json()
    quote_ts = parse_cboe_ts(payload)
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
                    "iv": float(iv) if iv else None,
                    "vol": float(o.get("volume") or 0),
                    "bid": float(o.get("bid") or 0), "ask": float(o.get("ask") or 0)})
    if not spot:
        sys.exit(f"FOUT: geen spotprijs in CBOE-respons voor {ticker}")
    return out, spot, quote_ts


def fetch_chain(ticker: str) -> tuple[list[dict], float, int | None]:
    src = os.environ.get("DATA_SOURCE", "cboe").lower()
    if src != "cboe":
        sys.exit(f"FOUT: DATA_SOURCE '{src}' wordt niet ondersteund — alleen 'cboe' is geïmplementeerd.")
    return fetch_cboe(ticker)


# ────────────────────────────── GEX-berekening ──────────────────────────────

def bs_gamma(S: float, K: float, T: float, iv: float) -> float:
    """Black-Scholes gamma (r≈0, q≈0) — voldoende voor de tekenwissel van het profiel."""
    if S <= 0 or K <= 0 or T <= 0 or iv <= 0:
        return 0.0
    st = iv * math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * iv * iv * T) / st
    return math.exp(-0.5 * d1 * d1) / (math.sqrt(2 * math.pi) * S * st)


def profile_flip(contracts: list[dict], lo: float, hi: float,
                 today: dt.date, spot: float) -> float | None:
    """Gamma Flip via het gamma-profiel: totale netto dealer-gamma herberekend op
    hypothetische spotprijzen (gamma schuift mee met de onderliggende). De flip is
    de spotprijs waar het totaal van teken wisselt — in een negatief regime ligt die
    structureel bóven de markt ("boven X wordt dealer-gamma weer positief").
    Dit is de methode die commerciële GEX-aanbieders doorgaans hanteren; de
    cumulatief-over-strikes-methode blijft beschikbaar als fallback."""
    def _iv(v: float) -> float:
        return v / 100 if v > 3 else v          # CBOE geeft IV soms in %
    legs = []
    for c in contracts:
        if not c.get("iv"):
            continue
        iv = _iv(float(c["iv"]))
        if iv <= 0:
            continue
        T = (max((c["exp"] - today).days, 0) + 0.5) / 365.0   # +0,5 dag: 0DTE houdt intraday-gamma
        legs.append((c["strike"], 1.0 if c["type"] == "call" else -1.0, c["oi"], iv, T))
    if not legs:
        return None
    n = 240
    prev_tot, prev_s, crossings = None, None, []
    for i in range(n + 1):
        s = lo + (hi - lo) * i / n
        tot = sum(sign * oi * bs_gamma(s, k, t, iv) for k, sign, oi, iv, t in legs)
        if prev_tot is not None and (prev_tot < 0 <= tot or prev_tot > 0 >= tot) and tot != prev_tot:
            crossings.append(prev_s + (0 - prev_tot) / (tot - prev_tot) * (s - prev_s))
        prev_tot, prev_s = tot, s
    if not crossings:
        return None
    return round(min(crossings, key=lambda x: abs(x - spot)), 2)


def compute_gex(chain: list[dict], spot: float, today: dt.date) -> dict:
    per_strike: dict[float, dict] = defaultdict(lambda: {"call": 0.0, "put": 0.0})
    per_strike_0dte: dict[float, dict] = defaultdict(lambda: {"call": 0.0, "put": 0.0})
    filt: list[dict] = []          # gefilterde contracten (voor het flip-profiel)
    filt0: list[dict] = []         # idem, alleen 0DTE
    vol_map: dict[float, float] = {}
    atm_ivs: list[float] = []
    straddle: dict[tuple, float] = {}          # (exp, strike, type) → mid-prijs
    nearest_exp: dt.date | None = None

    lo, hi = spot * (1 - RANGE_PCT), spot * (1 + RANGE_PCT)
    max_exp = today + dt.timedelta(days=MAX_DTE)

    for c in chain:
        k, exp = c["strike"], c["exp"]
        if not (lo <= k <= hi) or exp < today or exp > max_exp:
            continue
        # Notionele GEX: gamma × OI × multiplier(100) × spot² × 1%  ($-gamma per 1%-move)
        gex = c["gamma"] * c["oi"] * 100 * spot * spot * 0.01
        per_strike[k][c["type"]] += gex
        filt.append(c)
        vol_map[k] = vol_map.get(k, 0.0) + c.get("vol", 0.0)
        if exp == today:
            per_strike_0dte[k][c["type"]] += gex
            filt0.append(c)
        if c["iv"] and abs(k - spot) / spot <= 0.01 and (exp - today).days <= 5:
            atm_ivs.append(float(c["iv"]))
        # ATM-straddle verzamelen (dichtstbijzijnde expiratie, strikes binnen 2% van spot)
        bid, ask = c.get("bid", 0), c.get("ask", 0)
        if bid > 0 and ask > 0 and abs(k - spot) / spot <= 0.02:
            if nearest_exp is None or exp < nearest_exp:
                nearest_exp = exp
            straddle[(exp, k, c["type"])] = (bid + ask) / 2

    def derive(strikes: dict[float, dict]) -> dict:
        if not strikes:
            return {}
        ks = sorted(strikes)
        net = {k: strikes[k]["call"] - strikes[k]["put"] for k in ks}
        call_wall = max(ks, key=lambda k: strikes[k]["call"])
        put_wall  = max(ks, key=lambda k: strikes[k]["put"])
        # Gamma Flip: exacte nul-doorgang van cumulatieve netto GEX via lineaire
        # interpolatie tussen strikes; de doorgang die het dichtst bij spot ligt wint.
        cum, crossings = 0.0, []
        prev_cum, prev_k = None, None
        for k in ks:
            cum += net[k]
            if prev_cum is not None and (prev_cum < 0 <= cum or prev_cum > 0 >= cum) and cum != prev_cum:
                frac = (0 - prev_cum) / (cum - prev_cum)
                crossings.append(prev_k + frac * (k - prev_k))
            prev_cum, prev_k = cum, k
        flip = round(min(crossings, key=lambda x: abs(x - spot)), 2) if crossings else min(ks, key=lambda k: abs(k - spot))
        rest = [k for k in ks if k not in (call_wall, put_wall)]
        # Γ-ranking: "total" = call+put gamma (strikes met grote posities aan
        # béíde kanten blijven zichtbaar — bij netto vallen die tegen elkaar weg),
        # "net" = |call−put| (oude methode)
        if GAMMA_RANK == "total":
            ranked = sorted(rest, key=lambda k: strikes[k]["call"] + strikes[k]["put"], reverse=True)[:10]
        else:
            ranked = sorted(rest, key=lambda k: abs(net[k]), reverse=True)[:10]
        # Secondary walls: op-één-na-grootste concentratie, buiten de directe buurt van de primary
        gaps = [b - a for a, b in zip(ks, ks[1:]) if b - a > 0]
        step = sorted(gaps)[len(gaps) // 2] if gaps else 1.0
        def _secondary(side: str, primary: float):
            for k in sorted(ks, key=lambda kk: strikes[kk][side], reverse=True):
                if abs(k - primary) > step * 2:
                    return k
            return None
        return {"flip": flip, "call_wall": call_wall, "put_wall": put_wall, "gamma_levels": ranked,
                "call_wall_2": _secondary("call", call_wall), "put_wall_2": _secondary("put", put_wall)}

    full, dte0 = derive(per_strike), derive(per_strike_0dte)

    # Gamma Flip vervangen door de profielmethode (tenzij FLIP_METHOD=cumulative
    # of het profiel geen doorgang oplevert — dan blijft de cumulatieve flip staan)
    flip_used = "cumulative"
    if FLIP_METHOD == "profile":
        pf = profile_flip(filt, lo, hi, today, spot)
        if pf is not None and full:
            full["flip"] = pf
            flip_used = "profile"
        pf0 = profile_flip(filt0, lo, hi, today, spot)
        if pf0 is not None and dte0:
            dte0["flip"] = pf0

    # Session Range — voorkeursmodel: expected move ≈ EM_FACTOR × ATM-straddle
    # van de dichtstbijzijnde expiratie (publieke standaardbenadering).
    daily_move = 0.0
    if nearest_exp is not None:
        pairs = [k for k in {kk for (e, kk, _t) in straddle if e == nearest_exp}
                 if (nearest_exp, k, "call") in straddle and (nearest_exp, k, "put") in straddle]
        if pairs:
            atm_k = min(pairs, key=lambda k: abs(k - spot))
            daily_move = EM_FACTOR * (straddle[(nearest_exp, atm_k, "call")] + straddle[(nearest_exp, atm_k, "put")])
    if not daily_move:
        # Fallback: IV-model (CBOE geeft IV soms in % i.p.v. decimaal → normaliseren)
        ivs = [v / 100 if v > 3 else v for v in atm_ivs]
        iv = sorted(ivs)[len(ivs) // 2] if ivs else 0.0
        daily_move = spot * iv * math.sqrt(1 / 252) if iv else 0.0
    session = {"ceiling": round(spot + daily_move, 2), "floor": round(spot - daily_move, 2)} if daily_move else {}

    profile = {k: {"call": round(v["call"], 0), "put": round(v["put"], 0),
                   "net": round(v["call"] - v["put"], 0)} for k, v in per_strike.items()}
    net_full = {k: v["net"] for k, v in profile.items()}
    net_total = sum(net_full.values())
    regime = "POSITIEF (+GEX, mean-reversion)" if net_total > 0 else "NEGATIEF (-GEX, momentum)"
    return {"spot": spot, "full": full, "0dte": dte0, "session": session,
            "net": net_full, "vol": vol_map, "net_total": net_total, "regime": regime,
            "profile": profile, "em_expiration": nearest_exp.isoformat() if nearest_exp else None,
            "flip_method": flip_used}


# ────────────────────────────── Live-script generator ──────────────────────────────

def emit_live_script(paste: str, today: dt.date) -> bool:
    """Schrijft data/GexLevels_live.pine: het volledige indicator-script met de
    verse paste-string en datum al ingevuld — klaar om integraal te plakken."""
    tpl_path = Path(__file__).parent / "GexLevels.pine"
    if not tpl_path.exists():
        return False
    src = tpl_path.read_text()
    out, done_p = [], False
    for line in src.splitlines():
        if not done_p and line.strip().startswith("pasteCode"):
            line = f'pasteCode = "{paste}"   // {today.isoformat()}'
            done_p = True
        out.append(line)
    if not done_p:
        return False
    dest_dir = Path(__file__).parent / "data"
    dest_dir.mkdir(exist_ok=True)
    dest = dest_dir / "GexLevels_live.pine"
    dest.write_text("\n".join(out) + "\n")
    return True


# ────────────────────────────── Gamma-clusters & migratie ──────────────────────────────

def find_clusters(net: dict[float, float], top_n: int = 8) -> list[dict]:
    """Groepeert aangrenzende, significante strikes met gelijk teken tot gamma-clusters.

    Significantie: |netto GEX| ≥ 25% van het maximum. Aangrenzend: gat ≤ 1,5× strike-stap.
    Sterkte is relatief t.o.v. het zwaarste cluster (100%)."""
    if not net:
        return []
    ks = sorted(net)
    gaps = [b - a for a, b in zip(ks, ks[1:]) if b - a > 0]
    step = sorted(gaps)[len(gaps) // 2] if gaps else 1.0
    mx = max(abs(v) for v in net.values()) or 1.0
    sig = [k for k in ks if abs(net[k]) >= 0.25 * mx]

    groups, cur = [], []
    for k in sig:
        if cur and (k - cur[-1] > step * 1.5 or (net[k] > 0) != (net[cur[-1]] > 0)):
            groups.append(cur)
            cur = []
        cur.append(k)
    if cur:
        groups.append(cur)

    out = []
    for c in groups:
        tot = sum(abs(net[k]) for k in c)
        center = sum(k * abs(net[k]) for k in c) / tot
        out.append({"lo": c[0], "hi": c[-1], "width": len(c), "center": round(center, 2),
                    "sign": "call" if net[max(c, key=lambda k: abs(net[k]))] > 0 else "put",
                    "total_gex": round(tot, 0)})
    if out:
        mxt = max(o["total_gex"] for o in out)
        for o in out:
            o["strength_pct"] = round(100 * o["total_gex"] / mxt, 1)
    out.sort(key=lambda o: -o["total_gex"])
    return out[:top_n]


def compute_migration(hist_dir: Path, pfx: str, today: dt.date, snapshot: dict):
    """Vergelijkt de huidige positionering met het meest recente eerdere snapshot."""
    prev_files = sorted(f for f in hist_dir.glob(f"{pfx}_*.json")
                        if f.stem.split("_", 1)[1] < today.isoformat())
    if not prev_files:
        return None
    prev = json.loads(prev_files[-1].read_text())

    def diff(now, old):
        if now is None or old is None:
            return None
        return {"prev": old, "now": now, "delta": round(now - old, 2)}

    pl, nl = prev.get("levels", {}), snapshot["levels"]
    return {
        "vs_date": prev.get("date"),
        "spot": diff(snapshot["spot"], prev.get("spot")),
        "net_total_gex": diff(snapshot["net_total_gex"], prev.get("net_total_gex")),
        "regime_changed": prev.get("regime") != snapshot.get("regime"),
        "gamma_flip": diff(nl.get("gamma_flip"), pl.get("gamma_flip")),
        "call_wall": diff(nl.get("call_wall"), pl.get("call_wall")),
        "put_wall": diff(nl.get("put_wall"), pl.get("put_wall")),
    }


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

    print("→ Bron: CBOE (delayed)")
    print(f"→ Ophalen {UNDERLYING} keten…")
    chain, spot, quote_ts = fetch_chain(UNDERLYING)
    res = compute_gex(chain, spot, today)
    print(f"   spot={spot:.2f}, contracten={len(chain)}")

    corr_results = []
    for csym in CORR_LIST:
        print(f"→ Ophalen {csym} keten…")
        cchain, cspot, _cts = fetch_chain(csym)
        corr_results.append((csym, compute_gex(cchain, cspot, today)))
        print(f"   spot={cspot:.2f}, contracten={len(cchain)}")
    cres = corr_results[0][1]   # eerste correlated asset voedt de λ-levels op de chart

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
    # ── Tier-systeem [+] [++] [+++]: confluentie-score per strike ──
    # Basis (kandidaat): |netto GEX| ≥ 35% van het maximum.
    # +1 punt per onafhankelijke bevestiging:
    #   • strike is óók prominent in de 0DTE-keten (walls/flip/top-ranked)
    #   • strike ligt vlak bij een omgerekend correlated (SPY) kernlevel
    #   • optievolume op de strike ≥ 60% van het maximale strike-volume
    net = res.get("net", {})
    clusters: list[dict] = []
    if net:
        mx = max(abs(v) for v in net.values()) or 1.0

        dte0_set = set()
        d0d = res["0dte"]
        for kk in [d0d.get("call_wall"), d0d.get("put_wall"), d0d.get("flip")]:
            if kk is not None:
                dte0_set.add(kk)
        dte0_set.update(d0d.get("gamma_levels", [])[:5])

        # correlated kernlevels van ALLE opgegeven assets omrekenen via spot-ratio
        corr_conv = []
        for _csym, _cr in corr_results:
            if not _cr.get("spot"):
                continue
            _ratio = res["spot"] / _cr["spot"]
            _cf = _cr["full"]
            _keys = ([_cf.get("call_wall"), _cf.get("put_wall"), _cf.get("flip")]
                     + _cf.get("gamma_levels", [])[:5])
            corr_conv.extend(k * _ratio for k in _keys if k)
        tol = res["spot"] * 0.0025   # ±0,25% telt als alignment

        # sterke multi-strike clusters (voor cluster-lidmaatschap in de score;
        # dezelfde lijst wordt verderop hergebruikt voor het historie-snapshot)
        clusters = find_clusters(net)
        strong_clusters = [c for c in clusters if c["strength_pct"] >= 50 and c["width"] >= 2]

        vol_map = res.get("vol", {})
        vmax = max(vol_map.values()) if vol_map else 0.0

        marks = []
        for k, v in sorted(net.items(), key=lambda kv: -abs(kv[1])):
            if abs(v) / mx < 0.35:
                continue
            score = 1
            if any(abs(k - z) < 0.01 for z in dte0_set):
                score += 1
            if any(abs(k - z) <= tol for z in corr_conv):
                score += 1
            if vmax and vol_map.get(k, 0.0) >= 0.60 * vmax:
                score += 1
            if any(c["lo"] <= k <= c["hi"] for c in strong_clusters):
                score += 1
            marks.append(f"{k:g}" + "+" * min(score, 3))
            if len(marks) >= 12:
                break
        if marks:
            parts.append("MK:" + "|".join(marks))

    # Spot-ankers: hiermee verankert de indicator de strike→chart-conversie.
    # TS = het moment waarop de spot echt is vastgelegd. Voorkeur: het tijdstempel
    # uit de CBOE-payload zelf — buiten markturen (avond/weekend) wijst dat naar
    # de laatste sessie, zodat de indicator niet een live NQ-prijs door een
    # bevroren QQQ-spot deelt. Fallback: nu − feedvertraging.
    now = int(time.time())
    if quote_ts and now - 6 * 86400 <= quote_ts <= now + 300:
        ts = quote_ts
        ts_src = "CBOE-quote"
    else:
        ts = now - int(SPOT_DELAY_MIN * 60)
        ts_src = "klok−vertraging (geen bruikbaar quote-tijdstempel)"
    parts.append(f"SPOT:{spot:.2f}")
    if corr_results and corr_results[0][1].get("spot"):
        parts.append(f"CSPOT:{corr_results[0][1]['spot']:.2f}")
    parts.append(f"TS:{ts}")
    print(f"→ TS-anker: {dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(timespec='seconds')} UTC  [{ts_src}]")

    paste = " ".join(parts)
    (Path(__file__).parent / "paste_string.txt").write_text(paste + "\n")

    # Dagelijkse historie-snapshot (t.b.v. vergelijking met eerdere sessies)
    hist_dir = Path(__file__).parent / "history"
    hist_dir.mkdir(exist_ok=True)
    snapshot = {
        "date": today.isoformat(),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "underlying": UNDERLYING, "spot": round(spot, 2),
        "data_source": "CBOE",
        "calculation_version": CALC_VERSION,
        "quote_timestamp": quote_ts,
        "flip_method": res.get("flip_method"),
        "gamma_rank": GAMMA_RANK,
        "expiration_used": res.get("em_expiration"),
        "strike_range_pct": RANGE_PCT, "max_dte": MAX_DTE,
        "regime": res["regime"], "net_total_gex": round(res["net_total"], 0),
        "correlated_symbols": CORR_LIST,
        "levels": {"gamma_flip": f.get("flip"), "call_wall": f.get("call_wall"),
                   "put_wall": f.get("put_wall"),
                   "call_wall_secondary": f.get("call_wall_2"),
                   "put_wall_secondary": f.get("put_wall_2"),
                   "gamma_levels": gl,
                   "0dte": {"flip": d0.get("flip"), "call_wall": d0.get("call_wall"),
                            "put_wall": d0.get("put_wall")},
                   "session": ses, "correlated": cgl},
        "clusters": clusters,
        "gamma_profile": {f"{k:g}": v for k, v in sorted(res.get("profile", {}).items())},
    }
    snapshot["migration"] = compute_migration(hist_dir, pfx, today, snapshot)
    (hist_dir / f"{pfx}_{today.isoformat()}.json").write_text(json.dumps(snapshot, indent=1))

    # Console-samenvatting: clusters + migratie
    top_c = snapshot["clusters"][:3]
    if top_c:
        print("\n→ Gamma-clusters (top 3):")
        for c in top_c:
            print(f"   {c['sign']:<4} {c['lo']:g}–{c['hi']:g}  center {c['center']:g}  sterkte {c['strength_pct']:g}%")
    mig = snapshot["migration"]
    if mig:
        print(f"\n→ Gamma Migration t.o.v. {mig['vs_date']}:")
        for naam, key in [("Gamma Flip", "gamma_flip"), ("Call Wall", "call_wall"), ("Put Wall", "put_wall")]:
            d = mig.get(key)
            if d:
                print(f"   {naam}: {d['prev']:g} → {d['now']:g}  ({d['delta']:+g})")
        dn = mig.get("net_total_gex")
        if dn:
            print(f"   Netto GEX: ${dn['prev'] / 1e9:,.2f} mld → ${dn['now'] / 1e9:,.2f} mld")
        if mig.get("regime_changed"):
            print("   ⚠ REGIME-WISSEL sinds vorige sessie!")

    print(f"\n→ Dealer-regime: {res['regime']}  |  netto GEX ≈ ${res['net_total'] / 1e9:,.2f} mld per 1%-move")
    print(f"→ Gamma Flip-methode: {res.get('flip_method')}  |  flip = {f.get('flip')}")
    print("\n=== PASTE STRING ===")
    print(paste)
    if emit_live_script(paste, today):
        print("✓ Kant-en-klaar script: ./data/GexLevels_live.pine")
    print("\n✓ CSV's geschreven naar ./data — klaar voor Pine Seeds sync")
    print("✓ Historie-snapshot geschreven naar ./history")


if __name__ == "__main__":
    main()
