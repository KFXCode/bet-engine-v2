"""Bet Card: out of everything the engine generated today, exactly which ones to bet.

  python -m edge_agent.slate --picks data/picks_today.csv            # live odds (ODDS_API_KEY)
  python -m edge_agent.slate --picks data/picks_today.csv --snapshot data/odds_snapshots/basketball_nba/<file>.json

Picks file columns (map names in config.yaml -> slate_columns):
  sport, home_team, away_team, model (sides|props), market (h2h|spreads|totals|player_points|...),
  selection (team name, Over/Under), point (blank for ML), player (props only), model_prob

For each pick:
  final_prob = w * model_prob + (1 - w) * sharp consensus (w = the tuned market_blend_w)
  edge       = final_prob - implied prob of the BEST price across books
  BET only if: edge >= threshold, edge < cap, >= min_books pricing it, and it's the best pick
  in its game for that model (no stacking correlated singles on one game).
Output ranks BETs by expected value and gives a "don't bet below" price for each.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from . import market as mk
from . import research as rs

SPORT_KEYS = mk.SPORTS


def _norm(s) -> str:
    return str(s).strip().lower()


def find_event(events: list, home: str, away: str):
    h, a = _norm(home), _norm(away)
    for ev in events:
        eh, ea = _norm(ev.get("home_team", "")), _norm(ev.get("away_team", ""))
        if (h in eh or eh in h) and (a in ea or ea in a):
            return ev
    return None


def floor_price(final_prob: float, threshold_pts: float) -> int | None:
    """Worst American odds that still clear the threshold: need final_prob - 1/d >= thr/100."""
    need = final_prob - threshold_pts / 100.0
    return mk.decimal_to_american(1.0 / need) if need > 0 else None


def build_card(picks: pd.DataFrame, events_by_sport: dict, cfg: dict, props_fetcher=None) -> pd.DataFrame:
    P, M = cfg["current_params"], cfg["market"]
    rows = []
    for _, r in picks.iterrows():
        model = _norm(r["model"])
        thr, w = P["thresholds"][model], P["market_blend_w"].get(model, 1.0)
        base = {"sport": r["sport"], "game": f"{r['away_team']} @ {r['home_team']}", "model": model, "market": r["market"],
                "pick": " ".join(str(x) for x in [r.get("player") or "", r["selection"],
                                                   "" if pd.isna(r.get("point")) else f"{r['point']:+g}" if r["market"] == "spreads" else r.get("point")]
                                 if str(x) not in ("", "nan")).strip(),
                "model_prob": r["model_prob"]}
        ev = find_event(events_by_sport.get(r["sport"], []), r["home_team"], r["away_team"])
        if ev is None:
            rows.append({**base, "decision": "PASS", "reason": "game not found in odds feed"}); continue
        if model == "props" and props_fetcher and not any(m["key"] == r["market"] for b in ev.get("bookmakers", []) for m in b["markets"]):
            ev = props_fetcher(ev, r["market"])
        point = None if pd.isna(r.get("point")) else float(r["point"])
        desc = "" if pd.isna(r.get("player")) else str(r["player"])
        q = mk.price_pick(ev, r["market"], r["selection"], float(r["model_prob"]), w, M["book_weights"],
                          point=point, description=desc, min_books=M["min_books"],
                          bettable=set(M["bettable_books"]) if M.get("bettable_books") else None)
        if q is None:
            rows.append({**base, "decision": "PASS", "reason": f"fewer than {M['min_books']} books pricing it, or none of your books offer it"}); continue
        row = {**base, **q, "floor_odds": floor_price(q["final_prob"], thr), "event_id": ev["id"],
               "commence": ev.get("commence_time"), "sport_key": ev.get("sport_key"), "home_team": r["home_team"],
               "sel": r["selection"], "point": point, "player": desc, "model_run_at": r.get("model_run_at")}
        if q["edge_pts"] >= P["edge_cap"]:
            row.update(decision="PASS", reason=f"edge {q['edge_pts']:.1f} ≥ {P['edge_cap']} cap: likely model error")
        elif q["edge_pts"] < thr:
            row.update(decision="PASS", reason=f"edge {q['edge_pts']:.1f} < {thr} after market check")
        else:
            row.update(decision="BET", reason="")
        rows.append(row)

    card = pd.DataFrame(rows)
    if card.empty:
        return card
    # One bet per game per model: keep the highest-EV one, pass the rest as correlated.
    bets = card[card["decision"] == "BET"].sort_values("ev_per_unit", ascending=False)
    dup = bets.duplicated(subset=["event_id", "model"], keep="first")
    card.loc[bets[dup].index, ["decision", "reason"]] = ["PASS", "correlated with a better bet in this game"]
    # Stake: flat 1u by default; optional capped fractional Kelly.
    st = cfg.get("staking", {"mode": "flat", "unit": 1.0})
    card["stake_u"] = np.nan
    b = card["decision"] == "BET"
    if st.get("mode") == "kelly":
        dec = 1 + np.where(card["best_odds"] > 0, card["best_odds"] / 100, 100 / -card["best_odds"].abs())
        k = (card["final_prob"] * dec - 1) / (dec - 1)
        card.loc[b, "stake_u"] = np.clip(k[b] * st.get("kelly_fraction", 0.25) * st.get("bankroll_u", 100), 0, st.get("max_u", 2.0)).round(2)
    else:
        card.loc[b, "stake_u"] = st.get("unit", 1.0)
    order = {"BET": 0, "HOLD": 1, "PASS": 2}
    return card.sort_values(["decision", "ev_per_unit"], key=lambda s: s.map(order) if s.name == "decision" else -s.fillna(-9)).reset_index(drop=True)


def add_research(card: pd.DataFrame, cfg: dict, client=None, snap_root: str = "data/odds_snapshots") -> pd.DataFrame:
    """Gather evidence for every BET (and near-misses, for context). Research can only downgrade."""
    R, M = cfg.get("research", {}), cfg["market"]
    if not R.get("enabled", True) or card.empty:
        return card
    venues = rs.load_venues(R.get("venues_path", "data/venues.csv"))
    card = card.copy()
    for col in ["line_move", "weather", "research"]:
        if col not in card.columns:
            card[col] = None
    targets = card.index[card["decision"] == "BET"].tolist()
    for i in targets:
        r = card.loc[i]
        mv = None
        if isinstance(r.get("line_move"), str) and r["line_move"]:
            pass  # already computed from the engine's own price history
        elif isinstance(r.get("sport_key"), str):
            mv = rs.line_movement(Path(snap_root) / r["sport_key"], r["event_id"], r["market"], r["sel"],
                                  r["point"], r["player"] or "", M["book_weights"], set(R.get("sharp_books", [])))
        if not (isinstance(r.get("line_move"), str) and r["line_move"]):
            card.at[i, "line_move"] = rs.describe_move(mv)
        v = venues.get(str(r["home_team"]).lower())
        if v and v.get("roof", "open") == "open" and r["sport"] in ("NFL", "NCAAF") and isinstance(r.get("commence"), str):
            try:
                w = rs.nws_forecast(float(v["lat"]), float(v["lon"]), r["commence"], R.get("nws_user_agent", "edge-agent"))
                card.at[i, "weather"] = w and f"{w['temp_f']}°F, wind {w['wind_mph']} mph, {w['forecast']}, precip {w['precip_pct']}% (NWS)"
            except Exception as e:  # weather is supplementary; never block the card
                card.at[i, "weather"] = f"unavailable ({type(e).__name__})"
        try:
            res = rs.research_pick({**r.to_dict(), "line_move": card.at[i, "line_move"], "weather": card.at[i, "weather"]}, cfg, client)
        except Exception as e:
            res = {"summary": f"research failed ({type(e).__name__}); bet stands on model + market only",
                   "evidence": [], "verdict": "neutral", "confidence": "low", "material_news_after_model_run": False}
        card.at[i, "research"] = res
        d, why = rs.apply_to_decision(card.at[i, "decision"], res)
        if d != card.at[i, "decision"]:
            card.at[i, "decision"], card.at[i, "reason"] = d, why
            card.at[i, "stake_u"] = np.nan
    return card


def _why(i, r) -> list[str]:
    res = r.research if isinstance(r.research, dict) else {}
    icon = {"supports": "✅", "against": "⚠️", "neutral": "•"}
    out = [f"**{i}. {r.pick}** ({r.game}): research verdict **{res.get('verdict', 'n/a')}** "
           f"({res.get('confidence', 'n/a')} confidence)", f"- {res.get('summary', '')}",
           f"- **Market:** {r.line_move}"]
    if isinstance(r.weather, str) and r.weather:
        out.append(f"- **Weather:** {r.weather}")
    for e in res.get("evidence", []):
        late = " _(after model ran)_" if e.get("after_model_run") else ""
        out.append(f"- {icon.get(e.get('direction'), '•')} {e['claim']}{late} [source]({e['url']})")
    if res.get("dropped_unsourced"):
        out.append(f"- _{res['dropped_unsourced']} claim(s) dropped: source not verifiable_")
    return out + [""]


def render(card: pd.DataFrame, date: str) -> str:
    def am(x):
        return "—" if pd.isna(x) else f"{int(x):+d}"
    bets, passes = card[card["decision"] == "BET"], card[card["decision"] == "PASS"]
    L = [f"# Bet Card, {date}\n", f"**{len(bets)} bet{'s' if len(bets) != 1 else ''}** out of {len(card)} engine picks. Singles only. "
         "Don't take a bet if the price has moved past its floor.\n",
         "| # | Game | Pick | Book | Odds | Don't bet below | Model | Market | Final | Edge | EV/u | Stake |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(bets.itertuples(), 1):
        L.append(f"| {i} | {r.game} | **{r.pick}** | {r.best_book} | {am(r.best_odds)} | {am(r.floor_odds)} | "
                 f"{r.model_prob:.1%} | {r.consensus_prob:.1%} | {r.final_prob:.1%} | {r.edge_pts:+.1f} | {r.ev_per_unit:+.3f} | {r.stake_u:g}u |")
    if "research" in card.columns:
        L.append("\n## Why each bet\n")
        for i, r in enumerate(bets.itertuples(), 1):
            L += _why(i, r)
        holds = card[card["decision"] == "HOLD"]
        if len(holds):
            L.append("\n## On HOLD: research found something\n")
            for r in holds.itertuples():
                L += _why("HOLD", r) + [f"- **Held because:** {r.reason}"]
    L += ["\n## Passed\n", "| Game | Pick | Why |", "|---|---|---|"]
    L += [f"| {r.game} | {r.pick} | {r.reason} |" for r in passes.itertuples()]
    L.append("\n_Final = blend of the engine's probability and the sharp-weighted, no-vig market consensus. "
             "Edge is measured against the best available price. These are model estimates, not guarantees._")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--picks", required=True)
    ap.add_argument("--snapshot", help="use a saved odds snapshot instead of live odds")
    ap.add_argument("--config", default="edge_agent/config.yaml")
    ap.add_argument("--out", default="reports/bet_card")
    ap.add_argument("--no-research", action="store_true", help="skip the web research step")
    a = ap.parse_args()
    cfg = yaml.safe_load(Path(a.config).read_text())
    picks = pd.read_csv(a.picks).rename(columns={v: k for k, v in cfg.get("slate_columns", {}).items()})
    events_by_sport, fetcher = {}, None
    if a.snapshot:
        evs = json.loads(Path(a.snapshot).read_text())["events"]
        events_by_sport = {s: evs for s in picks["sport"].unique()}
    else:
        for s in picks["sport"].unique():
            events_by_sport[s], q = mk.fetch_odds(SPORT_KEYS[s], "h2h,spreads,totals", cfg["market"]["regions"])
            print(f"{s}: {len(events_by_sport[s])} events · quota {q}")

        def fetcher(ev, market):
            data, _ = mk.fetch_event_props(ev["sport_key"], ev["id"], market, cfg["market"]["regions"])
            return data
    card = build_card(picks, events_by_sport, cfg, fetcher)
    if not a.no_research:
        card = add_research(card, cfg)
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    (out / f"{date}.md").write_text(render(card, date))
    flat = card.copy()
    if "research" in flat.columns:  # log verdicts so the weekly review can measure whether research helps
        flat["research_verdict"] = flat["research"].map(lambda x: x.get("verdict") if isinstance(x, dict) else None)
        flat["research_confidence"] = flat["research"].map(lambda x: x.get("confidence") if isinstance(x, dict) else None)
        (out / f"{date}.research.json").write_text(json.dumps(
            {r["pick"]: r["research"] for _, r in card.iterrows() if isinstance(r.get("research"), dict)}, indent=2, default=str))
        flat = flat.drop(columns=["research"])
    flat.to_csv(out / f"{date}.csv", index=False)
    print(render(card, date))


if __name__ == "__main__":
    main()
