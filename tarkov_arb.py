#!/usr/bin/env python3
"""
tarkov_arb.py - flea -> trader arbitrage scanner for Escape from Tarkov.

Finds items whose flea market price sits below the best trader sell price
(the "blank RFID card" class: buy on flea, vendor to trader, pocket the spread).

Data: tarkov.dev community JSON API (json.tarkov.dev - free, keyless, ~5 min
freshness; the same flat files the tarkov.dev site itself runs on).
NOTE: the project's GraphQL API (api.tarkov.dev/graphql) has been hard-down
since 2026-07-21 ("GraphQL server unavailable", the-hideout/tarkov-api#474),
so this script uses the JSON dumps instead. json.tarkov.dev 403s requests
with no/default-library User-Agent, hence the custom UA below.

Buying on flea has no buyer-side fee, and traders accept non-FIR items,
so profit per flip = best trader priceRUB - flea buy price. No other math.

Tiers (the TRADER column names who pays the price shown, all tiers):
  CONVEYOR   - the flea price is pinned to the vendor price (24h avg within
               5% of best trader pay), the floor dips below it, and the book
               is deep (30+ offers, full-value items only). A standing
               fountain - new under-vendor listings appear continuously
               (blank-RFID / Red Rebel class). Camp these filters in-game.
               Conveyors are scored by camping yield: OPEN% (share of ~2h
               market snapshots over the last 30 days where the floor sat
               below vendor) x the average dip depth when open = YIELD, the
               expected rubles per glance at the filter. Sorted by YIELD;
               it rewards fast-refilling items over rare big spreads.
  STRUCTURAL - avg24hPrice < best trader price. The item sits below vendor
               value on average all day, but thinner/less pinned than a
               conveyor. Camp its filter.
  WINDOW     - low24hPrice < best trader price <= avg24hPrice. It dipped
               below vendor at least once in the last 24h. History by the
               time you read this; snipeable only with luck.

Fractional-value guard: traders only pay for what is left of an item - keys/
keycards with N uses, med kits with an HP pool, food/fuel with units, and
durability gear (armor/helmets/guns) all vendor at remaining value, so a cheap
flea listing may be a partially-used unit worth far less than the full trader
price shown here. Such items are flagged "frac: <why>"; single-use and
unlimited-use items cannot exist in a partial state and carry no flag. Pass
--safe-only to drop flagged rows entirely. Items with 0 flea offers at the
last scan are flagged too (spread exists on paper, nothing to buy right now).

Usage:
  python3 tarkov_arb.py                     # PVE economy (default), sane defaults
  python3 tarkov_arb.py --safe-only         # only full-value-guaranteed items
  python3 tarkov_arb.py --mode regular      # PVP economy (separate market)
  python3 tarkov_arb.py --min-spread 10000 --min-pct 3 --csv arb.csv
"""

import argparse
import csv
import json
import sys
import urllib.error
import urllib.request

API_BASE = "https://json.tarkov.dev"
UA = "twr-arb-scan/0.5 (personal project)"


def get_json(path: str, fatal: bool = True) -> dict | None:
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        headers={"Accept": "application/json", "User-Agent": UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if fatal:
            sys.exit(f"GET {path} failed: HTTP {e.code} {e.read().decode()[:200]}")
        return None
    except Exception:
        if fatal:
            raise
        return None


def fetch_items(mode: str, lang: str = "en") -> list[dict]:
    """Fetch items, name translations, and trader names; return flat item dicts.

    Translated fields hold lookup keys ("<id> Name", "<id> Nickname"); the
    companion "<path>_<lang>" file maps those keys to display strings.
    """
    raw = get_json(f"/{mode}/items")
    names = get_json(f"/{mode}/items_{lang}")["data"]
    traders = get_json(f"/{mode}/traders")["data"]
    trader_names = get_json(f"/{mode}/traders_{lang}")["data"]
    tname = {tid: trader_names.get(t["name"], t.get("normalizedName", tid))
             for tid, t in traders.items()}

    items = []
    for it in raw["data"]["items"].values():
        items.append({
            "id": it["id"],
            "name": names.get(it["name"], it.get("normalizedName") or it["name"]),
            "shortName": names.get(it["shortName"], it["shortName"]),
            "normalizedName": it.get("normalizedName") or "",
            "types": it.get("types") or [],
            "properties": it.get("properties") or {},
            "lastLowPrice": it.get("lastLowPrice"),
            "avg24hPrice": it.get("avg24hPrice"),
            "low24hPrice": it.get("low24hPrice"),
            "changeLast48hPercent": it.get("changeLast48hPercent"),
            "lastOfferCount": it.get("lastOfferCount"),
            "sellFor": [
                {"priceRUB": o.get("priceRUB") or 0,
                 "vendor": {"name": tname.get(o.get("trader"), o.get("trader") or "")}}
                for o in it.get("sellToTrader") or []
            ],
        })
    return items


def best_trader(item: dict) -> tuple[str, int] | None:
    """Highest trader sell price in RUB (Peacekeeper USD already normalized)."""
    best = None
    for offer in item.get("sellFor") or []:
        vendor = (offer.get("vendor") or {}).get("name", "")
        if vendor == "Flea Market":
            continue
        price = offer.get("priceRUB") or 0
        if best is None or price > best[1]:
            best = (vendor, price)
    return best


def usage_risk(item: dict) -> str | None:
    """Why a cheap flea listing may vendor below the full trader price.

    Traders pay fractionally for partially-consumed items (uses/HP/units
    remaining) and for damaged durability gear. Single-use and unlimited-use
    items cannot exist in a partial state. Melee weapons never degrade.
    Returns a short reason string, or None when full value is guaranteed.
    """
    p = item.get("properties") or {}
    pt = p.get("propertiesType") or ""
    uses = p.get("uses") or 0
    if pt == "ItemPropertiesKey":
        return f"{uses} uses" if uses > 1 else None
    if pt == "ItemPropertiesMedKit":
        hp = p.get("hitpoints") or 0
        return f"{hp} HP pool" if hp > 1 else None
    if pt in ("ItemPropertiesMedicalItem", "ItemPropertiesPainkiller",
              "ItemPropertiesSurgicalKit"):
        return f"{uses} uses" if uses > 1 else None
    if pt in ("ItemPropertiesFoodDrink", "ItemPropertiesResource"):
        units = p.get("units") or 0
        return f"{units} units" if units > 1 else None
    if pt in ("ItemPropertiesArmor", "ItemPropertiesChestRig", "ItemPropertiesHelmet",
              "ItemPropertiesGlasses", "ItemPropertiesArmorAttachment"):
        return "durability" if (p.get("durability") or 0) > 0 else None
    if pt in ("ItemPropertiesWeapon", "ItemPropertiesPreset"):
        return "durability"
    if "gun" in (t.lower() for t in item.get("types") or []):
        return "durability"  # gun presets sometimes carry no weapon properties
    if not pt and "repair-kit" in item.get("normalizedName", ""):
        return "resource pool"  # repair kits: API exposes no resource field
    return None


def scan(items: list[dict], min_spread: int, min_pct: float,
         safe_only: bool = False) -> list[dict]:
    rows = []
    for it in items:
        low24 = it.get("low24hPrice") or 0
        avg24 = it.get("avg24hPrice") or 0
        last_low = it.get("lastLowPrice") or 0
        if not low24 and not last_low:
            continue  # no flea presence
        bt = best_trader(it)
        if not bt:
            continue
        trader, tprice = bt

        # Trigger on the 24h low: "did an arb window open today?"
        floor = min(p for p in (low24, last_low) if p) or 0
        spread_best = tprice - floor
        if floor <= 0 or spread_best < min_spread:
            continue
        if (spread_best / floor) * 100 < min_pct:
            continue

        risk = usage_risk(it)
        if safe_only and risk:
            continue
        offers = it.get("lastOfferCount")
        # CONVEYOR: flea avg pinned to the vendor price, deep book, fungible
        # (full-value) units. The under-vendor floor regenerates continuously.
        if (avg24 and abs(avg24 - tprice) / tprice <= 0.05
                and (offers or 0) >= 30 and not risk):
            tier = "CONVEYOR"
        elif avg24 and avg24 < tprice:
            tier = "STRUCTURAL"
        else:
            tier = "WINDOW"
        flags = []
        if risk:
            flags.append(f"frac: {risk}")
        if offers == 0:
            flags.append("no offers")

        rows.append({
            "tier": tier,
            "id": it["id"],
            "name": it["name"],
            "short": it.get("shortName", ""),
            "trader": trader,
            "trader_price": tprice,
            "flea_last_low": last_low,
            "flea_low_24h": low24,
            "flea_avg_24h": avg24,
            "spread_now": tprice - last_low if last_low else None,
            "spread_at_24h_low": spread_best,
            "spread_vs_avg": tprice - avg24 if avg24 else None,
            "trend_48h_pct": it.get("changeLast48hPercent"),
            "offers_last_scan": offers,
            "fractional_risk": risk or "",
            "open_pct": None,          # filled by score_conveyors
            "avg_dip": None,
            "yield_per_look": None,
            "flags": "; ".join(flags),
        })

    sort_rows(rows)
    return rows


def sort_rows(rows: list[dict]) -> None:
    # Conveyors first (by camping yield, else floor spread), then structural
    # (by persistent edge), then windows (nearest-to-structural first)
    rank = {"CONVEYOR": 0, "STRUCTURAL": 1, "WINDOW": 2}

    def key(r):
        if r["tier"] == "CONVEYOR":
            second = (r["yield_per_look"] if r["yield_per_look"] is not None
                      else r["spread_at_24h_low"] / 1000)  # unscored sink low
        else:
            second = r["spread_vs_avg"] or r["spread_at_24h_low"]
        return (rank[r["tier"]], -second)

    rows.sort(key=key)


def score_conveyors(rows: list[dict], mode: str) -> None:
    """Score CONVEYOR rows by camping yield: expected rubles per glance.

    yield/look = P(a below-vendor listing is standing when you look) x the
    average depth of the dip when one is, measured over the last ~30 days of
    ~2h market snapshots from /{mode}/prices/{id}. Then re-sorts the rows.
    """
    conv = [r for r in rows if r["tier"] == "CONVEYOR"]
    if not conv:
        return
    print(f"Scoring {len(conv)} conveyor items via 30-day price history...")
    for r in conv:
        hist = get_json(f"/{mode}/prices/{r['id']}", fatal=False)
        seq = (hist or {}).get("data") or []
        seq = [e for e in seq if e.get("timestamp") and e.get("priceMin")]
        if not seq:
            continue
        cutoff = seq[-1]["timestamp"] - 30 * 86400 * 1000
        recent = [e for e in seq if e["timestamp"] >= cutoff]
        dips = [r["trader_price"] - e["priceMin"] for e in recent
                if e["priceMin"] < r["trader_price"]]
        if not recent:
            continue
        r["open_pct"] = round(100 * len(dips) / len(recent), 1)
        r["avg_dip"] = round(sum(dips) / len(dips)) if dips else 0
        r["yield_per_look"] = round((r["open_pct"] / 100) * r["avg_dip"])
    sort_rows(rows)


def print_table(rows: list[dict]) -> None:
    if not rows:
        print("No arbitrage candidates above thresholds. Lower --min-spread/--min-pct.")
        return
    hdr = (f"{'TIER':<11} {'ITEM':<42} {'TRADER':<12} {'PAYS':>10} "
           f"{'FLEA NOW':>10} {'24H LOW':>10} {'SPREAD@LOW':>11} {'OFFERS':>6} "
           f"{'OPEN%':>5} {'YIELD':>7}  FLAGS")
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        offers = "" if r["offers_last_scan"] is None else f"{r['offers_last_scan']:,}"
        openp = "" if r["open_pct"] is None else f"{r['open_pct']:.0f}%"
        yld = "" if r["yield_per_look"] is None else f"{r['yield_per_look']:,}"
        print(f"{r['tier']:<11} {r['name'][:42]:<42} {r['trader']:<12} "
              f"{r['trader_price']:>10,} {r['flea_last_low'] or 0:>10,} "
              f"{r['flea_low_24h'] or 0:>10,} {r['spread_at_24h_low']:>11,} "
              f"{offers:>6} {openp:>5} {yld:>7}  {r['flags']}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Tarkov flea->trader arbitrage scanner")
    ap.add_argument("--mode", choices=["regular", "pve", "pvp-season"], default="pve",
                    help="Economy to scan: pve (default), regular = main PVP, "
                         "pvp-season = seasonal PVP wipe")
    ap.add_argument("--min-spread", type=int, default=5000,
                    help="Minimum rubles per flip at the 24h low (default 5000)")
    ap.add_argument("--min-pct", type=float, default=2.0,
                    help="Minimum spread as %% of flea price (default 2.0)")
    ap.add_argument("--safe-only", action="store_true",
                    help="Drop items whose trader value scales with remaining "
                         "uses/HP/units/durability (multi-use keys, meds, fuel, "
                         "armor, guns); keep only guaranteed-full-value items")
    ap.add_argument("--csv", metavar="PATH", help="Also write full results to CSV")
    args = ap.parse_args()

    items = fetch_items(args.mode)
    rows = scan(items, args.min_spread, args.min_pct, args.safe_only)
    score_conveyors(rows, args.mode)
    print_table(rows)
    print(f"\n{len(rows)} candidates across {len(items)} items "
          f"({args.mode} economy{', safe-only' if args.safe_only else ''}, "
          f"json.tarkov.dev data, ~5 min stale).")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else [])
            w.writeheader()
            w.writerows(rows)
        print(f"CSV written to {args.csv}")


if __name__ == "__main__":
    main()
