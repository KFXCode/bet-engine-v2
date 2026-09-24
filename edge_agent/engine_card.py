"""Edge Agent Bet Card for the Cover1Picks Edge Engine.

Runs after edge_slate.py and grade.py in the daily workflow:

    python -m edge_agent.engine_card            # add --no-research to skip the web research step

Inputs (all written by the engine itself):
    edge_slate.json    the engine's priced games and props
    market_odds.json   10-book game-line odds from the same API call (no extra credits)
    props_cache.json   10-book prop odds from the same API calls (no extra credits)

For every pick the engine would publish (grade.todays_picks, the exact same rule):
    consensus  = sharp-weighted, no-vig probability across books
    final_prob = w * engine_prob + (1 - w) * consensus        (w tuned by the weekly review)
    edge       = final_prob - implied prob of the BEST LEGAL price (Maryland books only)
    BET only if edge >= threshold, edge < cap, enough books, and it is the best bet on that game.
Then the research desk gathers sourced evidence per BET and can put it on HOLD.

Outputs: bet_card.json (for the board / Whop) and reports/bet_card/<date>.md
"""
from __future__ import annotations

import argparse
import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd
import yaml

from . import market as mk
from .slate import add_research, render

N = NormalDist()


def _dec(a):
    a = float(a)
    return 1 + a / 100 if a > 0 else 1 + 100 / abs(a)


def to_decimal_event(ev: dict) -> dict:
    """The engine pulls American odds; the market math works in decimal."""
    ev = copy.deepcopy(ev)
    for b in ev.get("bookmakers", []):
        for m in b.get("markets", []):
            for o in m.get("outcomes", []):
                if o.get("price") is not None:
                    o["price"] = _dec(o["price"])
    return ev


def _norm(n):
    return " ".join((n or "").lower().replace(".", " ").replace("'", "").replace("-", " ").split())


# ---------------------------------------------------------------- consensus ----
def spread_consensus(ev: dict, team: str, line: float, sd: float, weights: dict, bettable: set):
    """Books hang different numbers (-3 vs -3.5), so translate each book's no-vig price
    into the team's expected margin, average that, and re-price at OUR line:
        P(margin + L > 0) = Phi((mu + L) / sd)  =>  mu = sd * Phi^-1(q) - L
    Best legal price is taken only at exactly our line (a different number is a different bet)."""
    mus, ws, best, book, n = [], [], 0.0, None, 0
    for b in ev.get("bookmakers", []):
        for m in b.get("markets", []):
            if m["key"] != "spreads":
                continue
            outs = {o["name"]: o for o in m.get("outcomes", [])}
            if team not in outs or len(outs) != 2 or outs[team].get("point") is None:
                continue
            q = mk.devig([o["price"] for o in outs.values()])[list(outs).index(team)]
            L_b = float(outs[team]["point"])
            mus.append(sd * N.inv_cdf(min(max(q, 1e-4), 1 - 1e-4)) - L_b)
            ws.append(weights.get(b["key"], 1.0)); n += 1
            if abs(L_b - line) < 1e-9 and b["key"] in bettable and outs[team]["price"] > best:
                best, book = outs[team]["price"], b["key"]
    if not mus or not book:
        return None
    mu = float(np.average(mus, weights=ws))
    return {"prob": N.cdf((mu + line) / sd), "n_books": n, "best_price": best, "best_book": book}


def keyed_consensus(ev: dict, key: tuple, weights: dict, bettable: set):
    """Moneylines and props: same market, same player, same line, same side."""
    mkt, desc, point, name = key
    for k, c in mk.consensus(ev, weights, bettable=bettable).items():
        if (k[0] == mkt and _norm(k[1]) == _norm(desc) and k[3].lower() == name.lower()
                and ((k[2] is None and point is None) or (k[2] is not None and point is not None
                                                          and abs(float(k[2]) - float(point)) < 1e-9))):
            return c
    return None


# -------------------------------------------------------------------- card ----
def load_events(root: Path) -> tuple[dict, dict]:
    games, props = {}, {}
    mo = json.loads((root / "market_odds.json").read_text()) if (root / "market_odds.json").exists() else {}
    for sport_key, evs in (mo.get("sports") or {}).items():
        for ev in evs:
            games[(_norm(ev.get("away_team")), _norm(ev.get("home_team")))] = {**to_decimal_event(ev), "sport_key": sport_key}
    pc = json.loads((root / "props_cache.json").read_text()) if (root / "props_cache.json").exists() else {}
    for ck, hit in pc.items():
        d = (hit or {}).get("data")
        if d and d.get("id"):
            props[d["id"]] = to_decimal_event(d)  # newest wins: cache holds one entry per key
    return games, props


def build(root: Path, cfg: dict) -> pd.DataFrame:
    import sys
    sys.path.insert(0, str(root))
    from grade import todays_picks  # the engine's own publish rule

    slate = json.loads((root / "edge_slate.json").read_text())
    games_meta = {(_norm(g["away"]), _norm(g["home"])): g for g in slate.get("games", [])}
    games, props = load_events(root)
    P, M = cfg["current_params"], cfg["market"]
    bettable = set(M["bettable_books"])
    rows = []
    for c in todays_picks(slate):
        model = "sides" if c["market"] in ("Moneyline", "Spread") else "props"
        thr, w = P["thresholds"][model], P["market_blend_w"].get(model, 1.0)
        gkey = (_norm(c["away"]), _norm(c["home"]))
        team = c["home"] if c["selection"] == "home" else c["away"]
        base = {"sport": c["sport"], "game": f"{c['away']} @ {c['home']}", "model": model, "market": c["market"],
                "pick": c["label"], "lane": c.get("lane"), "model_prob": round(c["p"], 4), "engine_price": c["price"],
                "engine_edge": round(c["edge"], 2), "commence": c.get("commence"), "home_team": c["home"],
                "player": c.get("player") or "", "point": c.get("line"), "model_run_at": slate.get("generated_at")}
        if model == "sides":
            ev = games.get(gkey)
            meta = games_meta.get(gkey, {})
            if ev is None:
                rows.append({**base, "decision": "PASS", "reason": "no multi-book odds for this game yet"}); continue
            base.update(event_id=ev["id"], sport_key=ev["sport_key"], sel=team, odds_market="spreads" if c["market"] == "Spread" else "h2h")
            if c["market"] == "Spread":
                q = spread_consensus(ev, team, float(c["line"]), float(meta.get("sdMargin") or 13.5), M["book_weights"], bettable)
            else:
                q = keyed_consensus(ev, ("h2h", "", None, team), M["book_weights"], bettable)
            min_books = M["min_books"]
        else:
            ev = props.get(c.get("event"))
            if ev is None:
                rows.append({**base, "decision": "PASS", "reason": "no multi-book prop odds cached"}); continue
            name = {"over": "Over", "under": "Under", "yes": "Yes"}.get(c["selection"], c["selection"])
            base.update(event_id=ev["id"], sport_key=ev.get("sport_key"), sel=name, odds_market=c["stat_key"])
            q = keyed_consensus(ev, (c["stat_key"], c["player"], c.get("line"), name), M["book_weights"], bettable)
            min_books = M.get("min_books_props", 2)
        if not q or q["n_books"] < min_books:
            rows.append({**base, "decision": "PASS",
                         "reason": f"under {min_books} books at this line, or none of your books offer it"}); continue
        final = w * c["p"] + (1 - w) * q["prob"]
        edge = (final - 1 / q["best_price"]) * 100
        row = {**base, "consensus_prob": round(q["prob"], 4), "final_prob": round(final, 4), "n_books": q["n_books"],
               "best_book": q["best_book"], "best_odds": mk.decimal_to_american(q["best_price"]),
               "edge_pts": round(edge, 2), "ev_per_unit": round(final * q["best_price"] - 1, 4)}
        need = final - thr / 100
        row["floor_odds"] = mk.decimal_to_american(1 / need) if need > 0 else None
        if edge >= P["edge_cap"]:
            row.update(decision="PASS", reason=f"edge {edge:.1f} ≥ {P['edge_cap']} cap: likely model error")
        elif edge < thr:
            row.update(decision="PASS", reason=f"edge {edge:.1f} < {thr} once checked against {q['n_books']} books "
                                               f"(engine said {c['edge']:.1f} vs FanDuel)")
        else:
            row.update(decision="BET", reason="")
        rows.append(row)

    card = pd.DataFrame(rows)
    if card.empty:
        return card
    cap = cfg.get("max_per_game", {"sides": 1, "props": 2})
    bets = card[card["decision"] == "BET"].sort_values("ev_per_unit", ascending=False)
    seen = {}
    for i, r in bets.iterrows():
        k = (r["game"], r["model"])
        seen[k] = seen.get(k, 0) + 1
        if seen[k] > cap.get(r["model"], 1):
            card.loc[i, ["decision", "reason"]] = ["PASS", "correlated with a better bet in this game"]
    card["line_move"] = card.apply(lambda r: fanduel_move(root, r), axis=1)
    card["stake_u"] = np.where(card["decision"] == "BET", cfg.get("staking", {}).get("unit", 1.0), np.nan)
    order = {"BET": 0, "HOLD": 1, "PASS": 2}
    card["_o"] = card["decision"].map(order)
    return card.sort_values(["_o", "ev_per_unit"], ascending=[True, False]).drop(columns="_o").reset_index(drop=True)


def fanduel_move(root: Path, r) -> str | None:
    """Opening vs latest FanDuel no-vig price for this pick, from picks_log.json history."""
    try:
        log = json.loads((root / "picks_log.json").read_text())["picks"]
    except (OSError, ValueError, KeyError):
        return None
    for p in log:
        if p.get("pick") == r["pick"] and f"{p.get('away')} @ {p.get('home')}" == r["game"] and p.get("result") is None:
            h = [x for x in (p.get("history") or []) if x and x[2] is not None]
            if len(h) < 2:
                return "no line history yet"
            nv = lambda a, b: (1 / _dec(a)) / (1 / _dec(a) + 1 / _dec(b))
            p0, p1 = nv(h[0][1], h[0][2]), nv(h[-1][1], h[-1][2])
            d = "toward" if p1 > p0 else "away from"
            return (f"FanDuel moved {abs(p1 - p0) * 100:.1f} pts {d} this side since first published "
                    f"({h[0][1]:+d} → {h[-1][1]:+d}, {len(h)} reads)")
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--config", default="edge_agent/config.yaml")
    ap.add_argument("--no-research", action="store_true")
    a = ap.parse_args()
    root, cfg = Path(a.root), yaml.safe_load(Path(a.config).read_text())
    card = build(root, cfg)
    import os
    R = cfg.get("research", {})
    if not a.no_research and not card.empty and R.get("enabled"):
        if (R.get("model") or os.environ.get("RESEARCH_MODEL")) and os.environ.get("ANTHROPIC_API_KEY"):
            card = add_research(card, cfg)
        else:
            print("research skipped: set research.model (or RESEARCH_MODEL) and the ANTHROPIC_API_KEY secret")
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = root / "reports" / "bet_card"
    out.mkdir(parents=True, exist_ok=True)
    md = render(card, date) if not card.empty else f"# Bet Card, {date}\n\nNo engine picks on the slate."
    (out / f"{date}.md").write_text(md)
    (root / "bet_card.md").write_text(md)
    (root / "bet_card.json").write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rule": "engine probability blended with sharp multi-book consensus; edge vs best legal price",
        "bets": json.loads(card[card["decision"] == "BET"].to_json(orient="records", default_handler=str)) if not card.empty else [],
        "held": json.loads(card[card["decision"] == "HOLD"].to_json(orient="records", default_handler=str)) if not card.empty else [],
        "passed": json.loads(card[card["decision"] == "PASS"][["game", "pick", "reason"]].to_json(orient="records")) if not card.empty else [],
    }, indent=1))
    print(md)


if __name__ == "__main__":
    main()
