# GexLevels — Installation

Zo werkt de keten:

    CBOE delayed options-feed (gratis, geen key)
        →  gex_pipeline.py  (GitHub Action, elke 30 min tijdens US-uren)
        →  paste_string.txt (één regel per underlying: GLD + QQQ)
        →  GexLevels.pine op TradingView (één copy-paste, alle charts)

De indicator herkent zelf op welk symbool hij staat: op NQ/MNQ toont hij de
QQQ-levels (met SPY als λ-levels), op GC/MGC de GLD-levels — allemaal uit
dezelfde paste-string. Optioneel kan de data later via Pine Seeds automatisch
binnenkomen (zie onderaan); tot die tijd is het één copy-paste per dag.


## Installatie

### Stap 1 — GitHub-repo

1. Maak een GitHub-repo aan (publiek is alleen nodig voor de latere
   Pine Seeds-optie; voor de paste-workflow mag privé ook).
2. Zet deze bestanden erin:
   - `gex_pipeline.py`
   - `.github/workflows/update-levels.yml`
3. Test: tabblad *Actions* → workflow "Update GEX levels" → *Run workflow*.
   Na afloop staan er in de repo:
   - `paste_string.txt` — één regel GLD, één regel QQQ (de indicator kiest zelf)
   - `data/GexLevels_live.pine` — het complete indicatorscript met de verse
     paste al ingevuld (handig als je liever één blok code plakt)
   - `data/*.csv` — Pine Seeds-formaat (voor de latere auto-optie)
   - `history/*.json` — dagelijkse snapshots met clusters en migratie

De workflow draait automatisch elke 30 min tijdens de US-sessie plus een
ochtend-refresh (nieuwe open interest + verse 0DTE). Cron-tijden zijn UTC en
afgestemd op US-zomertijd; in de winter schuift de sessie één uur.

### Stap 2 — Indicator

1. Zet `GexLevels.pine` in de Pine Editor → Save → Add to chart.
2. Open Instellingen → veld **"Levels plakken"** → plak de volledige inhoud
   van `paste_string.txt` (beide regels tegelijk is prima).
3. Klaar. Dezelfde paste werkt op al je charts: NQ pakt de QQQ-regel,
   GC pakt de GLD-regel. Rechtsboven staat de datadatum ("GexLevels:
   2026-07-20") zodat je altijd ziet hoe vers de levels zijn.

Er is bewust vrijwel geen instellingenvenster: alleen het paste-veld, een
strike-grid-toggle met kleur, en de datadatum-toggle. Al het overige
(kleuren, zonebreedte, welke symbolen bij welke chart horen via `symMap`,
gridstap van 0,5 strike) staat in het CONFIG-blok bovenin de code.


## Wat de pipeline berekent

- **GEX per strike** — dealer gamma-exposure: gamma × open interest × 100 ×
  spot² × 1% (calls +, puts −), strikes binnen ±15% van spot, expiraties tot
  60 dagen (instelbaar via env-vars).
- **Gamma Flip** — standaard de *profielmethode*: het dealer-gammaprofiel
  wordt via Black-Scholes herberekend over een reeks hypothetische spots; de
  flip is waar dat profiel van teken wisselt. In een negatief regime ligt de
  flip daardoor typisch bóven de prijs. (`FLIP_METHOD=cumulative` geeft de
  oude nul-doorgang van cumulatieve netto GEX.)
- **Call/Put Wall** — de strike met de grootste call- resp. put-side exposure,
  plus secondaries in het historie-snapshot.
- **0DTE-levels** — zelfde berekening op alleen de contracten die vandaag
  expireren (flip, walls).
- **Session Ceiling/Floor** — verwachte dagrange: 85% van de ATM-straddle van
  de dichtstbijzijnde expiratie (`EM_FACTOR`); valt terug op het IV-model
  (spot × ATM-IV × √(1/252)) als er geen bruikbare straddle-quotes zijn.
- **Γ-1…Γ-10** — overige strikes gerangschikt op totale gamma-concentratie
  (`GAMMA_RANK=total`; `net` geeft de oude netto-ranking).
- **λ-1…λ-10** — kernlevels van het correlated symbool (SPY bij QQQ), door de
  indicator zelf omgerekend naar de chart.
- **[+] [++] [+++] tiers** — confluentie-score per strike: basis is een grote
  netto-concentratie; extra punten voor 0DTE-prominentie, alignment met een
  correlated kernlevel, hoog optievolume en lidmaatschap van een sterk
  multi-strike cluster.
- **Historie & migratie** — elk run-moment schrijft een JSON-snapshot
  (profiel, clusters, regime); de console toont de verschuiving van flip en
  walls t.o.v. de vorige sessie en waarschuwt bij een regime-wissel.

### Databron en versheid

CBOE delayed quotes (`cdn.cboe.com/api/global/delayed_quotes/options/…`) —
gratis, ~15 min vertraagd, geen key. Omdat open interest hoe dan ook maar
1× per dag wordt bijgewerkt (OCC), is dit voor GEX-levels functioneel vrijwel
gelijkwaardig aan realtime. Het endpoint is publiek maar niet officieel
gedocumenteerd; poll het niet vaker dan elke 15–30 min. Indexen vragen een
underscore-prefix (`_SPX`, `_NDX`). In het weekend levert de feed de
vrijdagdata — de datum rechtsboven op de chart maakt dat zichtbaar.

### Strike→chart-conversie (het anker)

QQQ- en GLD-strikes moeten worden omgerekend naar NQ- resp. GC-prijzen. De
paste bevat daarvoor `PC`/`CPC`: de officiële vorige-dagclose van de
underlying. De indicator deelt de vorige dagclose van het chartsymbool door
die waarde — twee afgeronde sessieprijzen van hetzelfde moment. Dat anker is
volledig deterministisch: identiek op elke timeframe, bij elke herlaad, op
elk uur van de dag. Oudere pastes zonder `PC` vallen terug op het
`SPOT`/`TS`-anker (spotprijs + meetmoment).

### Velden in de paste-string

`GF CW PW` kernlevels · `GF0 CW0 PW0` 0DTE · `SC SF` session range ·
`G1–G10` gamma-levels · `C1–C10` correlated · `MK` tier-markers
(`715+++|710+`) · `SPOT/CSPOT` spot-anker (fallback) · `PC/CPC`
prev-close-anker · `SYM/CSYM` symboolnamen voor de labels · `TS` meetmoment
(epoch) · `DT` datadatum. Ontbrekende velden worden simpelweg niet getekend —
een handmatige mini-paste als `GF:711 CW:720 PW:700` werkt dus ook (alleen op
een QQQ/NQ-chart betrouwbaar, want zonder `SPOT`/`PC` gebruikt de conversie
de geconfigureerde standaardsymbolen).

### Omgevingsvariabelen (workflow)

`UNDERLYING` (QQQ) · `CORRELATED` (SPY; komma-lijst of leeg) ·
`STRIKE_RANGE_PCT` (0.15) · `MAX_DTE` (60) · `FLIP_METHOD` (profile) ·
`GAMMA_RANK` (total) · `EM_FACTOR` (0.85) · `SPOT_DELAY_MIN` (15, alleen
voor het fallback-anker). De meegeleverde workflow draait twee stappen:
eerst GLD (zonder correlated), dan QQQ+SPY; de paste-regels worden per
underlying samengevoegd in één `paste_string.txt`. Extra markt toevoegen =
extra workflow-stap + regel in `symMap` in de indicator.


## Optioneel — automatisch via Pine Seeds  ⚠️ vereist goedkeuring TradingView

De pipeline schrijft de CSV's al in Pine Seeds-formaat. Wil je van de
dagelijkse copy-paste af: volg het onboarding-proces in de officiële
docs-repo (https://github.com/tradingview-pine-seeds/docs — repo-structuur,
naamgeving en het exacte CSV-datumformaat zijn dáár leidend; de repo moet
publiek zijn). Na goedkeuring zet je in het CONFIG-blok `autoMode = true` en
vul je `seedSrc` in als `seed_<githubuser>_<repo>`. Let op: Pine Seeds is
géén tick-feed — TradingView synct de repo een beperkt aantal keer per dag.


## Gebruik op de chart

Walls en flips zijn zones, geen laserlijnen: omkeringen komen geregeld een
fractie vóór het level (front-running) en in een negatief gamma-regime breken
walls ook gewoon. Gebruik de zones als gebieden waar je alert bent en laat je
eigen signalen (liquidity, SMT) de entry bepalen. De zonebreedte is instelbaar
via `zoneHalf` in het CONFIG-blok.

Disclaimer: alleen voor educatieve doeleinden, geen financieel advies.
