"""Market layer: sharpen probabilities with the whole market, legally.

Source: The Odds API (licensed aggregator, https://the-odds-api.com). No scraping of sportsbook
sites -- that breaks their terms of service. API key goes in the ODDS_API_KEY secret, never in code.

What it does:
  1. snapshot  -- saves every book's prices for a sport to data/odds_snapshots/ (our own line history)
  2. consensus -- de-vigs each book, weights sharp books heavier -> one "market truth" probability
  3. best price -- the best number available across books (line shopping = free CLV)
  4. closing   -- last snapshot before kickoff = closing line, used to grade CLV automatically

CLI:
  python -m edge_agent.market snapshot --sport basketball_nba --markets h2h,spreads,totals
  python -m edge_agent.market fill-closing        # writes closing odds into the ledger
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import requests
import yaml

BASE = "https://api.the-odds-api.com/v4"
SPORTS = {"NFL": "americanfootball_nfl", "NCAAF": "americanfootball_ncaaf",
          "NBA": "basketball_nba", "NCAAB": "basketball_ncaab"}


# ---------- fetching ----------
def fetch_odds(sport_key: str, markets: str, regions: str, api_key: str | None = None) -> tuple[list, dict]:
    api_key = api_key or os.environ["ODDS_API_KEY"]
    r = requests.get(f"{BASE}/sports/{sport_key}/odds", timeout=30, params={
        "apiKey": api_key, "regions": regions, "markets": markets, "oddsFormat": "decimal", "dateFormat": "iso"})
    r.raise_for_status()
    quota = {k: r.headers.get(k) for k in ("x-requests-remaining", "x-requests-used", "x-requests-last")}
    return r.json(), quota


def fetch_event_props(sport_key: str, event_id: str, markets: str, regions: str, api_key: str | None = None):
    """Player props are per-event on The Odds API (e.g. markets=player_points,player_rebounds)."""
    api_key = api_key or os.environ["ODDS_API_KEY"]
    r = requests.get(f"{BASE}/sports/{sport_key}/events/{event_id}/odds", timeout=30, params={
        "apiKey": api_key, "regions": regions, "markets": markets, "oddsFormat": "decimal", "dateFormat": "iso"})
    r.raise_for_status()
    return r.json(), {k: r.headers.get(k) for k in ("x-requests-remaining", "x-requests-used")}


def save_snapshot(events: list, sport_key: str, out_dir: str = "data/odds_snapshots") -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    p = Path(out_dir) / sport_key / f"{ts}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"taken_at": ts, "events": events}))
    return p


# ---------- math ----------
def _group_key(market_key: str, o: dict):
    """Outcomes that form one two-way (or n-way) market inside a single book."""
    desc = o.get("description") or ""          # player name for props
    pt = o.get("point")
    if market_key == "spreads" and pt is not None:
        pt = abs(pt)                            # Team A -3.5 pairs with Team B +3.5
    return (market_key, desc, pt)


def devig(prices: list[float]) -> list[float]:
    """Multiplicative de-vig: p_i = (1/d_i) / sum(1/d_j)."""
    inv = np.array([1.0 / p for p in prices])
    return list(inv / inv.sum())


def consensus(event: dict, weights: dict, default_w: float = 1.0, bettable: set | None = None) -> dict:
    """-> {(market, desc, point_of_outcome, outcome_name): {prob, n_books, best_price, best_book}}"""
    acc: dict = {}
    for bk in event.get("bookmakers", []):
        w = weights.get(bk["key"], default_w)
        for m in bk.get("markets", []):
            groups: dict = {}
            for o in m.get("outcomes", []):
                groups.setdefault(_group_key(m["key"], o), []).append(o)
            for g, outs in groups.items():
                if len(outs) < 2:
                    continue                    # can't de-vig a one-sided line
                probs = devig([o["price"] for o in outs])
                for o, p in zip(outs, probs):
                    k = (m["key"], o.get("description") or "", o.get("point"), o["name"])
                    a = acc.setdefault(k, {"wsum": 0.0, "psum": 0.0, "n": 0, "best": 0.0, "book": None})
                    a["wsum"] += w; a["psum"] += w * p; a["n"] += 1
                    if (bettable is None or bk["key"] in bettable) and o["price"] > a["best"]:
                        a["best"], a["book"] = o["price"], bk["key"]
    return {k: {"prob": a["psum"] / a["wsum"], "n_books": a["n"], "best_price": a["best"], "best_book": a["book"]}
            for k, a in acc.items() if a["wsum"] > 0 and a["book"]}


def sharpen(model_prob: float, market_prob: float, w: float) -> float:
    """Final probability = w * model + (1 - w) * sharp consensus. w is what the review agent tunes."""
    return w * model_prob + (1 - w) * market_prob


def decimal_to_american(d: float) -> int:
    return round((d - 1) * 100) if d >= 2 else round(-100 / (d - 1))


def price_pick(event: dict, market: str, name: str, model_prob: float, w: float, weights: dict,
               point=None, description: str = "", min_books: int = 3, bettable: set | None = None) -> dict | None:
    """Everything the engine needs to decide on one pick, against the best price on the board."""
    # Consensus uses ALL books (sharp offshore books included); best price only from books you can legally use.
    c = consensus(event, weights, bettable=bettable).get((market, description, point, name))
    if not c or c["n_books"] < min_books:
        return None
    p = sharpen(model_prob, c["prob"], w)
    implied_best = 1.0 / c["best_price"]
    return {"model_prob": model_prob, "consensus_prob": round(c["prob"], 4), "final_prob": round(p, 4),
            "n_books": c["n_books"], "best_book": c["best_book"], "best_odds": decimal_to_american(c["best_price"]),
            "edge_pts": round((p - implied_best) * 100, 2), "ev_per_unit": round(p * c["best_price"] - 1, 4)}


# ---------- closing lines ----------
def closing_price(snap_dir: Path, event_id: str, commence: str, market: str, name: str,
                  point=None, description: str = "", weights: dict | None = None):
    """Consensus prob + best price from the LAST snapshot taken before kickoff."""
    t0 = datetime.fromisoformat(commence.replace("Z", "+00:00"))
    for f in sorted(snap_dir.glob("*.json"), reverse=True):
        taken = datetime.strptime(f.stem, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        if taken >= t0:
            continue
        for ev in json.loads(f.read_text())["events"]:
            if ev["id"] == event_id:
                c = consensus(ev, weights or {}).get((market, description, point, name))
                if c:
                    return {**c, "snapshot": f.name}
    return None


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot"); s.add_argument("--sport", required=True)
    s.add_argument("--markets", default="h2h,spreads,totals"); s.add_argument("--config", default="edge_agent/config.yaml")
    a = ap.parse_args()
    cfg = yaml.safe_load(Path(a.config).read_text())["market"]
    events, quota = fetch_odds(a.sport, a.markets, cfg["regions"])
    p = save_snapshot(events, a.sport)
    print(f"saved {len(events)} events -> {p} · quota {quota}")


if __name__ == "__main__":
    main()
