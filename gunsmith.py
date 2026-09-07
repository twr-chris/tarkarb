#!/usr/bin/env python3
"""
gunsmith.py - TARKARB weapon-build lab: compatibility tree + stat model +
Pareto goal-seek optimizer for Escape from Tarkov guns (AR-family first).

Data: json.tarkov.dev flat files (same source as tarkov_arb.py). The base
weapon item IS the chassis (lower receiver): everything else hangs off its
`properties.slots` tree - slot -> allowedItems -> those items' slots -> ...
Compatibility is static (patch-frequency); prices are dynamic (~5 min).

Stat model (validated against every in-game preset via --validate):
  ergonomics  = weapon.ergonomics + sum(mod.ergonomics)
  recoil      = weapon.recoilVertical|Horizontal * (1 + sum(mod.recoilModifier))
  weight      = sum(item.weight)

Optimizer: tree DP carrying a Pareto front of (ergo, recoilSum, cost, weight)
per subtree, pruning dominated builds (quantized) with a goal-aware beam cap.
Mods with no upside for the goal (sights, lasers) prune to "empty" naturally.

Usage:
  python gunsmith.py --validate                       # stat model vs oracle
  python gunsmith.py --family m4a1                    # what is an M4? (overlap)
  python gunsmith.py --tree m4a1 --depth 2            # socket tree
  python gunsmith.py --build m4a1 --goal recoil --budget 400000
  python gunsmith.py --build m4a1 --goal ergo --flea-only
"""

import argparse
import json
import os
import sys
import time
import urllib.request

API = "https://json.tarkov.dev"
UA = "twr-arb-scan/0.5 (personal project)"
CACHE_TTL = 1800


def cache_dir() -> str:
    d = os.path.join(os.environ.get("TEMP", "."), "claude", "gunsmith")
    os.makedirs(d, exist_ok=True)
    return d


def get_json(path: str) -> dict:
    fn = os.path.join(cache_dir(), path.strip("/").replace("/", "_") + ".json")
    if os.path.exists(fn) and os.path.getmtime(fn) > time.time() - CACHE_TTL:
        return json.load(open(fn, encoding="utf-8"))
    req = urllib.request.Request(API + path, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=90) as r:
        data = json.load(r)
    json.dump(data, open(fn, "w", encoding="utf-8"))
    return data


class DB:
    def __init__(self, mode: str):
        self.mode = mode
        self.items = get_json(f"/{mode}/items")["data"]["items"]
        self.tr = get_json(f"/{mode}/items_{'en'}")["data"]

    def name(self, iid: str) -> str:
        it = self.items.get(iid)
        if not it:
            return iid
        return self.tr.get(it["name"], it.get("normalizedName", iid))

    def props(self, iid: str) -> dict:
        return (self.items.get(iid) or {}).get("properties") or {}

    def find_weapon(self, query: str) -> str:
        q = query.lower()
        weapons = [i for i in self.items.values()
                   if (i.get("properties") or {}).get("propertiesType") == "ItemPropertiesWeapon"]
        exact = [i for i in weapons if q == i.get("normalizedName")]
        subs = [i for i in weapons if q in (i.get("normalizedName") or "")
                or q in self.name(i["id"]).lower()]
        pick = (exact or subs)
        if not pick:
            sys.exit(f"no weapon matches '{query}'")
        if len(pick) > 1 and not exact:
            print(f"note: '{query}' matched {len(pick)} weapons; using {self.name(pick[0]['id'])}")
        return pick[0]["id"]


# ---------------------------------------------------------------- stat model

def mod_stats(props: dict) -> tuple[float, float]:
    """(ergonomics delta, recoil modifier fraction) for a mod's properties."""
    return props.get("ergonomics") or 0, props.get("recoilModifier") or 0


def compute_build_stats(db: DB, base_id: str, part_ids: list[str]) -> dict:
    bp = db.props(base_id)
    ergo = bp.get("ergonomics") or 0
    rsum = 0.0
    weight = (db.items.get(base_id) or {}).get("weight") or 0
    for pid in part_ids:
        e, r = mod_stats(db.props(pid))
        ergo += e
        rsum += r
        weight += (db.items.get(pid) or {}).get("weight") or 0
    return {
        "ergo": ergo,
        "recoilV": (bp.get("recoilVertical") or 0) * (1 + rsum),
        "recoilH": (bp.get("recoilHorizontal") or 0) * (1 + rsum),
        "recoilSum": rsum,
        "weight": weight,
    }


def validate(db: DB) -> None:
    """Check the stat model against every preset's own oracle stats."""
    presets = [i for i in db.items.values()
               if (i.get("properties") or {}).get("propertiesType") == "ItemPropertiesPreset"]
    n = passed = 0
    errs = []
    for pr in presets:
        pp = pr["properties"]
        base = pp.get("baseItem")
        if not base or base not in db.items:
            continue
        if db.props(base).get("propertiesType") != "ItemPropertiesWeapon":
            continue
        parts = []
        for ci in pr.get("containsItems") or []:
            iid = ci["item"] if isinstance(ci["item"], str) else ci["item"].get("id")
            if not iid or iid == base:
                continue
            # loaded ammo ships in containsItems (count = mag load); build-screen
            # stats exclude it, so we do too - and no stat mod appears twice
            if db.props(iid).get("propertiesType") == "ItemPropertiesAmmo":
                continue
            parts.append(iid)
        got = compute_build_stats(db, base, parts)
        exp_e, exp_v, exp_h = pp.get("ergonomics"), pp.get("recoilVertical"), pp.get("recoilHorizontal")
        if exp_e is None or exp_v is None:
            continue
        n += 1
        de = abs(round(got["ergo"]) - exp_e)
        dv = abs(round(got["recoilV"]) - exp_v)
        dh = abs(round(got["recoilH"]) - (exp_h or 0))
        if de <= 1 and dv <= 1 and dh <= 1:
            passed += 1
        else:
            errs.append((de + dv + dh, db.name(pr["id"]), de, dv, dh,
                         round(got["ergo"]), exp_e, round(got["recoilV"]), exp_v))
    print(f"validated {n} presets: {passed} pass ({100*passed/max(n,1):.1f}%) "
          f"within +/-1 on ergo, recoilV, recoilH")
    for _, name, de, dv, dh, ge, ee, gv, ev in sorted(errs, reverse=True)[:10]:
        print(f"  MISS {name[:52]:<52} ergo {ge} vs {ee} | recoilV {gv} vs {ev}")


# ------------------------------------------------------------------- family

def reachable_parts(db: DB, root_id: str) -> set[str]:
    seen, stack = set(), [root_id]
    while stack:
        iid = stack.pop()
        for slot in db.props(iid).get("slots") or []:
            for cand in (slot.get("filters") or {}).get("allowedItems") or []:
                if cand not in seen:
                    seen.add(cand)
                    stack.append(cand)
    return seen


def family(db: DB, gun_id: str) -> None:
    mine = reachable_parts(db, gun_id)
    print(f"{db.name(gun_id)}: {len(mine)} reachable parts. Closest relatives (Jaccard):")
    rows = []
    for it in db.items.values():
        if (it.get("properties") or {}).get("propertiesType") != "ItemPropertiesWeapon":
            continue
        if it["id"] == gun_id:
            continue
        theirs = reachable_parts(db, it["id"])
        j = len(mine & theirs) / max(len(mine | theirs), 1)
        rows.append((j, db.name(it["id"]), len(theirs)))
    rows.sort(reverse=True)
    for j, name, cnt in rows[:12]:
        print(f"  {j*100:5.1f}%  {name[:52]:<52} ({cnt} parts)")


# --------------------------------------------------------------------- tree

def print_tree(db: DB, iid: str, depth: int, max_depth: int, seen: tuple) -> None:
    for slot in db.props(iid).get("slots") or []:
        allowed = (slot.get("filters") or {}).get("allowedItems") or []
        req = "*" if slot.get("required") else " "
        print("  " * depth + f"{req}{slot.get('nameId','?')} [{len(allowed)} options]")
        if depth >= max_depth:
            continue
        # expand each distinct child that itself has slots (structure carriers)
        shown = set()
        for cand in allowed:
            if db.props(cand).get("slots") and cand not in seen and cand not in shown:
                shown.add(cand)
                print("  " * (depth + 1) + f"- {db.name(cand)[:60]}")
                print_tree(db, cand, depth + 2, max_depth, seen + (cand,))
        if shown:
            leafs = len(allowed) - len(shown)
            if leafs:
                print("  " * (depth + 1) + f"- (+{leafs} leaf options)")


# ---------------------------------------------------------------- optimizer

BEAM = 500
Q_ERGO, Q_REC, Q_COST, Q_WT = 1.0, 0.005, 2500, 0.1


def price_of(db: DB, iid: str, flea_only: bool, max_ll: int) -> tuple[int, str] | None:
    it = db.items.get(iid) or {}
    types = {t.lower() for t in it.get("types") or []}
    best = None
    flea = it.get("lastLowPrice") or 0
    if flea and "noflea" not in types:
        best = (flea, "Flea")
    if not flea_only:
        for o in it.get("buyFromTrader") or []:
            if o.get("taskUnlock"):
                continue
            if (o.get("minTraderLevel") or 1) > max_ll:
                continue
            p = o.get("priceRUB") or 0
            if p and (best is None or p < best[0]):
                best = (p, f"trader LL{o.get('minTraderLevel')}")
    return best


def scalarize(goal: str):
    if goal == "ergo":
        return lambda o: (o[0], -o[1], -o[2])            # ergo up, recoil down, cost down
    if goal == "recoil":
        return lambda o: (-o[1], o[0], -o[2])            # recoil down, ergo up, cost down
    return lambda o: (o[0] - 250 * o[1] - o[2] / 50000,)  # balanced blend


def optimize(db: DB, gun_id: str, goal: str, budget: int | None,
             flea_only: bool, max_ll: int) -> None:
    score = scalarize(goal)
    memo: dict[str, list] = {}
    unavailable = set()

    def options_for(iid: str, stack: frozenset):
        """Pareto options for taking item iid incl. its whole subtree.
        Option = (ergo, recoilSum, cost, weight, builddict, conflictset)"""
        if iid in memo and iid not in stack:
            return memo[iid]
        it = db.items.get(iid)
        if not it:
            return []
        pr = price_of(db, iid, flea_only, max_ll)
        if pr is None:
            unavailable.add(iid)
            return []
        cost0, src = pr
        p = db.props(iid)
        e0, r0 = mod_stats(p)
        w0 = it.get("weight") or 0
        conf0 = frozenset(it.get("conflictingItems") or [])
        acc = [(e0, r0, cost0, w0, {"__src": src}, conf0)]
        if iid in stack:                       # cycle guard: bare item only
            return acc
        for slot in p.get("slots") or []:
            allowed = (slot.get("filters") or {}).get("allowedItems") or []
            slot_opts = [] if slot.get("required") else [None]
            for cand in allowed:
                slot_opts.extend((cand, o) for o in options_for(cand, stack | {iid}))
            if slot.get("required") and not slot_opts:
                return []                      # unbuildable
            acc = merge(acc, slot.get("nameId", slot.get("id", "?")), slot_opts)
            if not acc:
                return []
        if iid not in stack:
            memo[iid] = acc
        return acc

    def merge(acc, slot_name, slot_opts):
        out = []
        for e, r, c, w, b, cf in acc:
            for so in slot_opts:
                if so is None:
                    out.append((e, r, c, w, b, cf))
                    continue
                cand, (e2, r2, c2, w2, b2, cf2) = so
                if budget is not None and c + c2 > budget:
                    continue
                if set(collect_ids(cand, b2)) & cf:   # incoming parts forbidden by build
                    continue
                if cf2 & set(collect_ids(None, b)):   # current parts forbidden by incoming
                    continue
                nb = dict(b)
                nb[slot_name] = (cand, b2)
                out.append((e + e2, r + r2, c + c2, w + w2, nb, cf | cf2))
        return prune(out)

    def collect_ids(root, b):
        ids = [root] if root else []
        for k, v in b.items():
            if k == "__src":
                continue
            cid, cb = v
            ids.extend(collect_ids(cid, cb))
        return ids

    def prune(opts):
        # quantized Pareto prune, then goal-aware beam cap
        best = {}
        for o in opts:
            key = (round(o[0] / Q_ERGO), round(o[1] / Q_REC),
                   round(o[2] / Q_COST), round(o[3] / Q_WT))
            cur = best.get(key)
            if cur is None or score(o[:4]) > score(cur[:4]):
                best[key] = o
        opts = list(best.values())
        keep = []
        for o in sorted(opts, key=lambda o: score(o[:4]), reverse=True):
            if not any(dominates(k, o) for k in keep[:80]):
                keep.append(o)
            if len(keep) >= BEAM:
                break
        return keep

    def dominates(a, b):
        return (a[0] >= b[0] and a[1] <= b[1] and a[2] <= b[2] and a[3] <= b[3]
                and (a[0] > b[0] or a[1] < b[1] or a[2] < b[2] or a[3] < b[3])
                and a[5] <= b[5])

    # --- run: the gun itself (price the bare gun; slots merged like a mod)
    gun = db.items[gun_id]
    gp = db.props(gun_id)
    pr = price_of(db, gun_id, flea_only, max_ll) or (0, "unpriced")
    acc = [(0.0, 0.0, pr[0], gun.get("weight") or 0, {"__src": pr[1]},
            frozenset(gun.get("conflictingItems") or []))]
    for slot in gp.get("slots") or []:
        allowed = (slot.get("filters") or {}).get("allowedItems") or []
        slot_opts = [] if slot.get("required") else [None]
        for cand in allowed:
            slot_opts.extend((cand, o) for o in options_for(cand, frozenset({gun_id})))
        if slot.get("required") and not slot_opts:
            sys.exit(f"required slot {slot.get('nameId')} has no available parts "
                     f"under current filters")
        acc = merge(acc, slot.get("nameId", "?"), slot_opts)

    if not acc:
        sys.exit("no feasible build under constraints")
    bestopt = max(acc, key=lambda o: score(o[:4]))
    e, r, c, w, b, _ = bestopt
    base_e = gp.get("ergonomics") or 0
    print(f"\n== {db.name(gun_id)} | goal={goal}"
          f"{f' | budget {budget:,}' if budget else ''}"
          f"{' | flea-only' if flea_only else f' | traders <= LL{max_ll}'} ==")
    print(f"ergonomics {base_e + e:.0f}   recoil V {(gp.get('recoilVertical') or 0)*(1+r):.0f} "
          f"/ H {(gp.get('recoilHorizontal') or 0)*(1+r):.0f}   "
          f"weight {w:.2f} kg   cost {c:,.0f} RUB")
    print(f"\n{db.name(gun_id)}  [{b.get('__src')}]")
    print_build(db, b, 1)
    if unavailable:
        print(f"\n({len(unavailable)} parts excluded as unbuyable under filters)")


def print_build(db: DB, b: dict, depth: int) -> None:
    for k, v in b.items():
        if k == "__src":
            continue
        cid, cb = v
        e, r = mod_stats(db.props(cid))
        stat = " ".join(s for s in (
            f"ergo{e:+.0f}" if e else "", f"recoil{r*100:+.0f}%" if r else "") if s)
        print("  " * depth + f"{k}: {db.name(cid)[:56]}"
              f"  [{cb.get('__src','?')}]  {stat}")
        print_build(db, cb, depth + 1)


def main() -> None:
    ap = argparse.ArgumentParser(description="TARKARB gunsmith lab")
    ap.add_argument("--mode", default="pve", choices=["regular", "pve", "pvp-season"])
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--family", metavar="GUN")
    ap.add_argument("--tree", metavar="GUN")
    ap.add_argument("--depth", type=int, default=2)
    ap.add_argument("--build", metavar="GUN")
    ap.add_argument("--goal", default="recoil", choices=["ergo", "recoil", "balanced"])
    ap.add_argument("--budget", type=int)
    ap.add_argument("--flea-only", action="store_true")
    ap.add_argument("--max-level", type=int, default=4, help="max trader loyalty level")
    args = ap.parse_args()

    db = DB(args.mode)
    if args.validate:
        validate(db)
    if args.family:
        family(db, db.find_weapon(args.family))
    if args.tree:
        gid = db.find_weapon(args.tree)
        print(db.name(gid))
        print_tree(db, gid, 1, args.depth, (gid,))
    if args.build:
        optimize(db, db.find_weapon(args.build), args.goal, args.budget,
                 args.flea_only, args.max_level)
    if not any((args.validate, args.family, args.tree, args.build)):
        ap.print_help()


if __name__ == "__main__":
    main()
