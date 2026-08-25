# TARKARB

Flea → trader arbitrage scanner for Escape from Tarkov: finds items listed on the
flea market below their best trader sell price (buy on flea, vendor to trader,
pocket the spread — flea buyers pay no fee and traders take non-FIR items).

**Live board:** https://twr-chris.github.io/tarkarb/

## What it shows

- **CONVEYOR** — items whose flea price is pinned to the vendor price (24h avg
  within 5%), with a deep order book (30+ offers) and full-value units. These are
  standing fountains: new under-vendor listings appear continuously. Ranked by
  **camping yield** = OPEN% (share of ~2h snapshots over 30 days where the floor
  sat below vendor) × average dip depth = expected ₽ per glance at the filter.
- **STRUCTURAL / WINDOW** — one-off dips below vendor value; usually already
  sniped by the time you see them.
- **frac:** flags — traders pay only remaining value on multi-use keys, med HP
  pools, food/fuel units, and durability gear; a cheap listing may be a partial
  unit. Single-use and unlimited-use items can't be partial and carry no flag.

## Run it

- **Web:** open the live board above (or `index.html` via any static server —
  it fetches data client-side).
- **CLI:** `python tarkov_arb.py` (Python 3.10+, stdlib only). PVE economy by
  default; `--mode regular` for PVP, `--safe-only`, `--min-spread`, `--min-pct`,
  `--csv out.csv`.

## Data

All market data comes from the community-run [tarkov.dev](https://tarkov.dev)
JSON API (`json.tarkov.dev`, ~5 min freshness). Massive thanks to
[The Hideout](https://github.com/the-hideout) for maintaining it.
(The project's GraphQL API has been down since 2026-07-21; the JSON flat files
are what the tarkov.dev site itself runs on.)

Not affiliated with Battlestate Games. Prices are only as fresh as the upstream
scanners; verify in-game before spending your rubles.
