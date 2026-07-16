# GexLevels — Installation

Zo werkt de keten:

    Polygon API  →  gex_pipeline.py (GitHub Action, elke 30 min)
                 →  CSV's in jouw GitHub-repo (Pine Seeds formaat)
                 →  TradingView synchroniseert de repo als custom datafeed
                 →  GexLevels.pine leest de levels via request.seed()

Belangrijk om te weten: Pine Seeds is géén tick-feed. TradingView haalt de
repo-data een beperkt aantal keer per dag op (in de praktijk tot ~5 updates
per handelsdag worden doorgezet). Vergelijkbare commerciële indicatoren werken om diezelfde reden ook met
dagelijkse snapshot-updates in plaats van echte realtime streaming.


## Stap 1 — Databron kiezen

**Gratis (standaard): CBOE delayed data.** De pipeline gebruikt het publieke
endpoint `cdn.cboe.com/api/global/delayed_quotes/options/QQQ.json`, dat per
contract gamma, IV, open interest en volume levert — zonder API-key, ~15 min
vertraagd. Omdat open interest hoe dan ook maar 1× per dag wordt bijgewerkt
(OCC), is dit voor GEX-levels functioneel vrijwel gelijkwaardig aan realtime.
Kanttekening: het endpoint is publiek maar niet officieel gedocumenteerd, dus
het formaat kan ooit wijzigen. Poll het niet vaker dan elke ~15–30 min.
Indexen vragen een underscore-prefix: `_SPX`, `_NDX`, `_VIX`.

**Optioneel (realtime): Polygon.io.** Zet in de workflow `DATA_SOURCE: polygon`
en voeg het repo-secret `POLYGON_API_KEY` toe (Options-abonnement vereist).

## Stap 2 — GitHub-repo

1. Maak een nieuwe **publieke** GitHub-repo aan (Pine Seeds vereist publiek).
2. Zet deze bestanden erin:
   - `gex_pipeline.py`
   - `.github/workflows/update-levels.yml`  (het meegeleverde update-levels.yml)
   - een lege map `data/`
3. (Alleen bij Polygon) *Settings → Secrets and variables → Actions* →
   secret `POLYGON_API_KEY` toevoegen. Bij CBOE is géén secret nodig.
4. Test: tabblad *Actions* → workflow "Update GEX levels" → *Run workflow*.
   Na afloop moeten er CSV's in `data/` staan (QQQ_CORE.csv, QQQ_INTRA.csv,
   QQQ_G1_5.csv, QQQ_G6_10.csv, QQQ_C1_5.csv, QQQ_C6_10.csv) plus
   `paste_string.txt`.


## Stap 3 — Pine Seeds activeren  ⚠️ vereist goedkeuring van TradingView

Pine Seeds is een officieel TradingView-programma; je repo moet worden
aangemeld voordat de data in TradingView beschikbaar is:

1. Lees de actuele instructies in de officiële docs-repo:
   https://github.com/tradingview-pine-seeds/docs
2. Volg daar het onboarding-proces (repo-structuur, naamgeving en aanmelding
   verlopen via hun template en instructies — controleer daar ook het exacte
   CSV-datumformaat; de pipeline schrijft `YYYYMMDDT,open,high,low,close,volume`
   conform hun template, maar hun spec is leidend).
3. Na goedkeuring zijn je symbolen in TradingView beschikbaar als
   `SEED_<githubuser>_<repo>:<SYMBOOL>` en in Pine via
   `request.seed("seed_<githubuser>_<repo>", "QQQ_CORE", …)`.


## Stap 4 — Indicator instellen

1. Zet `GexLevels.pine` in de Pine Editor → Save → Add to chart.
2. Instellingen → groep "⓪ Automatische data":
   - vink **"Levels automatisch via Pine Seeds"** aan
   - vul de seed source in: `seed_<jouwgithubnaam>_<reponaam>`
   - prefix: `QQQ`
3. Klaar. De levels verversen zodra TradingView je repo opnieuw synct; de
   datum rechtsboven ("GexLevels replay: …") komt automatisch uit de data.

Zolang stap 3 nog niet is goedgekeurd, laat je de auto-modus uit en gebruik je
de inhoud van `paste_string.txt` (wordt bij elke run automatisch gegenereerd)
in het bulk-paste veld — dat is één copy-paste per dag.


## Wat de pipeline berekent

- **GEX per strike** = gamma × open interest × 100 × spot (calls +, puts −)
- **Gamma Flip** = nul-doorgang van de cumulatieve netto GEX, dichtst bij spot
- **Call/Put Wall** = strike met de grootste call- resp. put-side exposure
- **0DTE-levels** = zelfde berekening, maar alleen contracten die vandaag expireren
- **Session Ceiling/Floor** = spot ± spot × ATM-IV × √(1/252) (verwachte dagrange)
- **Γ-1…Γ-10** = overige strikes gerangschikt op |netto GEX|
- **Correlated 1…10** = kernlevels van het correlated symbool (default SPY);
  de indicator rekent die zelf om via de live chart/SPY-ratio

Disclaimer: alleen voor educatieve doeleinden, geen financieel advies.
