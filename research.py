"""Research layer: gathers public evidence for each pick and cites every claim.

Three sources, all legal and public:
  1. Market tape (our own odds snapshots): opening vs current line, which way sharp books moved,
     sharp-vs-recreational disagreement. Deterministic, no API cost.
  2. Weather (US National Weather Service, api.weather.gov): free public-domain forecast for
     outdoor football venues listed in data/venues.csv.
  3. Web research agent (Claude + web search tool): injuries, confirmed lineups, beat-writer
     news, rest/travel, context, each claim tied to a source URL.

Integrity rules, enforced in code:
  - Any claim whose URL wasn't actually returned by the search is DROPPED (no invented sources).
  - Other handicappers' picks, "expert consensus" and tout sites are not evidence.
  - Only public information. Nothing from paid-leak groups, insiders, or private accounts.
  - Research can DOWNGRADE a BET to HOLD (material news the model may not have). It can never
    upgrade a PASS to a BET. Verdicts are logged so the weekly review can measure whether
    research flags actually predict CLV before they get more weight.
"""
from __future__ import annotations

import csv
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import requests

from . import market as mk

SYSTEM = """You are the research desk for a disciplined sports betting model. You do NOT pick bets.
Your job: find the most reliable, most recent PUBLIC information that bears on one specific pick, and
report it with a source URL for every claim.

Search for, in priority order:
1. Injury / availability: official team or league injury reports, confirmed inactives, beat writers
   from established outlets. Note the exact status (out, doubtful, questionable, probable) and timestamp.
2. Lineups & roles: confirmed starters, minutes or snap restrictions, QB/goalie/starting-pitcher-style
   changes, coaching changes.
3. Situational: rest days, back-to-backs, travel, altitude, short weeks.
4. Weather for outdoor football (wind 15+ mph, heavy rain/snow matter most).
5. For player props: recent role and usage changes, not just averages.

Rules:
- Every claim needs a URL you actually retrieved. No URL, no claim.
- Prefer primary sources (team/league sites, official injury reports) and established outlets.
- IGNORE other people's picks, predictions, "best bets", consensus-picks pages, and tout sites.
  Those are opinions, not evidence.
- Only public information. Never use or seek leaked, insider, or non-public information.
- Give each item's publish time when you can. Flag anything published AFTER the model ran.
- Be neutral. If you find nothing material, say so. That's a normal, useful answer.

Return ONLY a JSON object, no prose, with this shape:
{"summary": "2-3 sentence plain-English read",
 "evidence": [{"claim": "...", "direction": "supports|against|neutral", "category": "injury|lineup|situational|weather|usage|other",
               "url": "https://...", "published": "ISO time or null", "after_model_run": true/false}],
 "verdict": "supports|neutral|contradicts",
 "confidence": "low|medium|high",
 "material_news_after_model_run": true/false}"""


# ---------------- 1. market tape ----------------
def line_movement(snap_dir: Path, event_id: str, market: str, name: str, point=None, description: str = "",
                  weights: dict | None = None, sharp_books: set | None = None) -> dict | None:
    """Open (first snapshot) vs latest consensus, plus sharp-vs-rec split on the latest snapshot."""
    files = sorted(Path(snap_dir).glob("*.json"))
    seen = []
    for f in files:
        for ev in json.loads(f.read_text())["events"]:
            if ev["id"] == event_id:
                c = mk.consensus(ev, weights or {}).get((market, description, point, name))
                if c:
                    seen.append((f.stem, c["prob"], ev))
    if not seen:
        return None
    (t0, p0, _), (t1, p1, ev) = seen[0], seen[-1]
    out = {"open_prob": round(p0, 4), "now_prob": round(p1, 4), "move_pts": round((p1 - p0) * 100, 2),
           "snapshots": len(seen), "from": t0, "to": t1}
    if sharp_books:
        sharp = {"bookmakers": [b for b in ev["bookmakers"] if b["key"] in sharp_books]}
        rec = {"bookmakers": [b for b in ev["bookmakers"] if b["key"] not in sharp_books]}
        cs = mk.consensus(sharp, {}).get((market, description, point, name))
        cr = mk.consensus(rec, {}).get((market, description, point, name))
        if cs and cr:
            out["sharp_minus_rec_pts"] = round((cs["prob"] - cr["prob"]) * 100, 2)
    return out


def describe_move(m: dict | None) -> str:
    if not m or m["snapshots"] < 2:
        return "no line history yet"
    d = "toward" if m["move_pts"] > 0 else "away from"
    s = f"market moved {abs(m['move_pts']):.1f} pts {d} this side ({m['open_prob']:.1%} → {m['now_prob']:.1%})"
    if "sharp_minus_rec_pts" in m:
        s += f"; sharp books {m['sharp_minus_rec_pts']:+.1f} pts vs rec books"
    return s


# ---------------- 2. weather (NWS) ----------------
def load_venues(path: str = "data/venues.csv") -> dict:
    """team,lat,lon,roof  (roof = open|dome|retractable). Fill once per season."""
    p = Path(path)
    if not p.exists():
        return {}
    with p.open() as f:
        return {r["team"].strip().lower(): r for r in csv.DictReader(f)}


def nws_forecast(lat: float, lon: float, kickoff_iso: str, user_agent: str) -> dict | None:
    """Hourly forecast nearest kickoff from api.weather.gov (public domain, needs a User-Agent)."""
    h = {"User-Agent": user_agent, "Accept": "application/geo+json"}
    pts = requests.get(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}", headers=h, timeout=20)
    pts.raise_for_status()
    hourly = requests.get(pts.json()["properties"]["forecastHourly"], headers=h, timeout=20)
    hourly.raise_for_status()
    t0 = datetime.fromisoformat(kickoff_iso.replace("Z", "+00:00"))
    periods = hourly.json()["properties"]["periods"]
    best = min(periods, key=lambda p: abs(datetime.fromisoformat(p["startTime"]) - t0), default=None)
    if not best:
        return None
    wind = max((int(x) for x in re.findall(r"\d+", best.get("windSpeed", "0"))), default=0)
    return {"temp_f": best.get("temperature"), "wind_mph": wind, "forecast": best.get("shortForecast"),
            "precip_pct": (best.get("probabilityOfPrecipitation") or {}).get("value"),
            "source": "https://api.weather.gov (National Weather Service)"}


# ---------------- 3. web research agent ----------------
def _collect_urls(resp) -> set:
    """Every URL the search tool actually returned or cited. Claims citing anything else are dropped."""
    urls = set()
    for block in resp.content:
        d = block.model_dump() if hasattr(block, "model_dump") else dict(block)
        if d.get("type") == "web_search_tool_result":
            for r in d.get("content") or []:
                if isinstance(r, dict) and r.get("url"):
                    urls.add(r["url"])
        for c in d.get("citations") or []:
            if c.get("url"):
                urls.add(c["url"])
    return urls


def _final_json(resp) -> dict:
    text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("research agent returned no JSON")
    return json.loads(m.group(0))


def _norm_url(u: str) -> str:
    return u.split("#")[0].rstrip("/").lower()


def verify(result: dict, real_urls: set) -> dict:
    ok = {_norm_url(u) for u in real_urls}
    kept, dropped = [], 0
    for e in result.get("evidence", []):
        if e.get("url") and _norm_url(e["url"]) in ok:
            kept.append(e)
        else:
            dropped += 1
    result["evidence"], result["dropped_unsourced"] = kept, dropped
    if not kept:  # nothing verifiable -> can't contradict anything
        result["verdict"], result["confidence"], result["material_news_after_model_run"] = "neutral", "low", False
    return result


def research_pick(pick: dict, cfg: dict, client=None) -> dict:
    """pick: sport, game, pick, market, model, commence, model_run_at, final_prob, best_odds, line_move, weather."""
    R = cfg["research"]
    if client is None:
        import anthropic
        client = anthropic.Anthropic()  # ANTHROPIC_API_KEY
    model = R.get("model") or os.environ.get("RESEARCH_MODEL")
    if not model:
        raise ValueError("Set research.model in config.yaml (or RESEARCH_MODEL env var).")
    tool = {"type": R.get("web_search_tool", "web_search_20250305"), "name": "web_search", "max_uses": R.get("max_searches", 5)}
    if R.get("blocked_domains"):
        tool["blocked_domains"] = R["blocked_domains"]
    now = datetime.now(timezone.utc).isoformat(timespec="minutes")
    user = (f"Now: {now}\nSport: {pick['sport']}\nGame: {pick['game']} (starts {pick.get('commence', 'unknown')})\n"
            f"Pick: {pick['pick']} [{pick['market']}, {pick['model']} model]\n"
            f"Model probability {pick['final_prob']:.1%} at {int(pick['best_odds']):+d}\n"
            f"Model last ran: {pick.get('model_run_at', 'unknown')}\n"
            f"Line movement (our odds data): {pick.get('line_move', 'n/a')}\n"
            f"Weather (NWS): {pick.get('weather', 'n/a')}\n\n"
            "Research this pick and return the JSON.")
    resp = client.messages.create(model=model, max_tokens=R.get("max_tokens", 2500), system=SYSTEM,
                                  tools=[tool], messages=[{"role": "user", "content": user}])
    return verify(_final_json(resp), _collect_urls(resp))


def apply_to_decision(decision: str, research: dict) -> tuple[str, str]:
    """Research can only hold a bet back, never create one."""
    if decision != "BET" or not research:
        return decision, ""
    if research.get("material_news_after_model_run"):
        return "HOLD", "material news published after the model ran: re-run the engine first"
    if research.get("verdict") == "contradicts" and research.get("confidence") == "high":
        return "HOLD", "strong sourced evidence against: review before betting"
    return decision, ""
