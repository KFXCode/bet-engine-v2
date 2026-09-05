#!/usr/bin/env python3
"""
player_props.py — player projections and FanDuel prop lines for the Edge Engine.

The team-ratings model in edge_slate.py cannot price a player prop. It knows
Georgia is twelve points better than Auburn; it knows nothing about whether a
back gets 62 rushing yards. So this module builds a SECOND model from a
different data source: every player's own game log, scraped free from ESPN box
scores and cached on disk so a run only ever fetches games it has not seen.

What it does

  1. Sweeps the scoreboard for finished games, then pulls each box score once
     and stores every player's line from it. The cache (player_logs.json) is
     what makes this cheap: the first run backfills, later runs add a handful
     of games.
  2. Projects each stat from that log — recency-weighted mean, shrunk toward a
     league baseline by how few games the player has, then adjusted for the
     defence he faces.
  3. Prices the prop with a distribution that fits the STAT, not one normal
     curve for everything: gamma for yardage (non-negative and right-skewed),
     negative binomial for counts that are more spread out than Poisson,
     Poisson for anytime touchdown.
  4. Emits every priced side. The caller does the EV filtering.

Honest limits, stated because they matter more than the model does:

  * No injury or inactive feed. A player who is questionable and then plays
    twelve snaps will be projected as though healthy. The activity gate below
    catches players who are already out, not players who are limited.
  * No weather, no snap-count projection, no depth-chart change detection.
  * Anytime touchdown is the weakest market here — red-zone usage is noisy and
    a season of games is a small sample for a rate that low. It carries the
    heaviest shrinkage and the highest games-played gate as a result.

Odds cost: the props endpoint bills PER MARKET PER EVENT, unlike the game-line
endpoint which bills once per league. Everything below — the event cap, the
disk cache with a TTL, the credit reserve — exists to keep that bill bounded.

Env: ODDS_API_KEY, ODDS_API_KEY_BACKUP, PROPS_CREDIT_RESERVE (default 200),
     PROPS_MAX_EVENTS (default 24), PROPS_CACHE_MINUTES (default 180),
     MAX_NEW_BOXSCORES (default 350), PLAYER_LOGS_PATH, PROPS_CACHE_PATH.
"""

import json
import math
import os
import re
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import requests

HTTP_TIMEOUT = 20
LOGS_PATH = os.environ.get("PLAYER_LOGS_PATH", "player_logs.json")
CACHE_PATH = os.environ.get("PROPS_CACHE_PATH", "props_cache.json")

CREDIT_RESERVE = int(os.environ.get("PROPS_CREDIT_RESERVE", "200"))
MAX_EVENTS = int(os.environ.get("PROPS_MAX_EVENTS", "24"))
CACHE_MINUTES = int(os.environ.get("PROPS_CACHE_MINUTES", "180"))
MAX_NEW_BOXSCORES = int(os.environ.get("MAX_NEW_BOXSCORES", "350"))

# ESPN blocks some datacenter IPs on some hosts. A single-host call returns
# nothing on a runner and a whole league silently vanishes, so every request
# goes through a helper that tries each host and takes the first that answers.
ESPN_HOSTS = ["https://site.api.espn.com", "https://site.web.api.espn.com"]

LEAGUE_PATHS = {"NFL": "football/nfl", "NBA": "basketball/nba"}

# How far back to build game logs. 400 days so a league between seasons still
# has last season to project from — week 1 has no current-season games at all,
# and a model with no history produces nothing exactly when the card is biggest.
LOG_DAYS = {"NFL": 400, "NBA": 400}

# odds-api market key -> (stat, distribution, sport, min games, shrink games)
MARKETS = {
    "player_pass_yds":    ("passYds",    "gamma",   "NFL", 4, 3.0),
    "player_rush_yds":    ("rushYds",    "gamma",   "NFL", 4, 3.0),
    "player_reception_yds": ("recYds",   "gamma",   "NFL", 4, 3.0),
    "player_receptions":  ("receptions", "count",   "NFL", 4, 3.0),
    "player_anytime_td":  ("tds",        "poisson", "NFL", 6, 6.0),
    "player_points":      ("points",     "gamma",   "NBA", 6, 5.0),
    "player_rebounds":    ("rebounds",   "count",   "NBA", 6, 5.0),
    "player_assists":     ("assists",    "count",   "NBA", 6, 5.0),
    "player_points_rebounds_assists": ("pra", "gamma", "NBA", 6, 5.0),
}

MARKET_LABEL = {
    "player_pass_yds": "Passing yards", "player_rush_yds": "Rushing yards",
    "player_reception_yds": "Receiving yards", "player_receptions": "Receptions",
    "player_anytime_td": "Anytime TD", "player_points": "Points",
    "player_rebounds": "Rebounds", "player_assists": "Assists",
    "player_points_rebounds_assists": "Pts+Reb+Ast",
}

# Recency: a game this many games back counts half as much as the last one.
HALF_LIFE = {"NFL": 6.0, "NBA": 12.0}
# A player whose last appearance is older than this is treated as unavailable.
STALE_DAYS = {"NFL": 24, "NBA": 12}
DEF_SHRINK = 4.0        # games before a defence's own number is trusted
DEF_CLAMP = (0.80, 1.25)


# ------------------------------------------------------------------ http ----
def espn_get(path, params, diag, what):
    """GET an ESPN site-API path from whichever host answers first."""
    for host in ESPN_HOSTS:
        try:
            r = requests.get(host + path, params=params, timeout=HTTP_TIMEOUT)
            if r.status_code == 200:
                return r.json()
        except Exception:
            continue
    diag.append("espn: every host refused %s" % what)
    return None


# ------------------------------------------------------------ box scores ----
def stat_map(group):
    """{label: value} for one ESPN box-score athlete row."""
    labels = [str(x).upper() for x in (group.get("labels") or [])]
    def row(athlete):
        vals = athlete.get("stats") or []
        return {labels[i]: vals[i] for i in range(min(len(labels), len(vals)))}
    return row


def num(d, key, default=0.0):
    raw = d.get(key)
    if raw is None:
        return default
    txt = str(raw).strip()
    if txt in ("", "-", "--"):
        return default
    # "12/20" style cells (completions/attempts) — take nothing, callers ask
    # for YDS/TD/REC which are plain numbers.
    try:
        return float(txt.replace(",", ""))
    except ValueError:
        return default


def parse_nfl(box):
    """{player: {stat: value}} from one NFL box score."""
    out = defaultdict(lambda: defaultdict(float))
    teams = {}
    for team_block in box.get("players", []):
        tname = ((team_block.get("team") or {}).get("displayName")) or "?"
        for group in team_block.get("statistics", []):
            gname = (group.get("name") or "").lower()
            if gname not in ("passing", "rushing", "receiving"):
                continue
            row = stat_map(group)
            for ath in group.get("athletes", []):
                name = ((ath.get("athlete") or {}).get("displayName") or "").strip()
                if not name:
                    continue
                s = row(ath)
                teams[name] = tname
                if gname == "passing":
                    out[name]["passYds"] += num(s, "YDS")
                elif gname == "rushing":
                    out[name]["rushYds"] += num(s, "YDS")
                    out[name]["tds"] += num(s, "TD")
                elif gname == "receiving":
                    out[name]["recYds"] += num(s, "YDS")
                    out[name]["receptions"] += num(s, "REC")
                    out[name]["tds"] += num(s, "TD")
    return out, teams


def parse_nba(box):
    out = defaultdict(lambda: defaultdict(float))
    teams = {}
    for team_block in box.get("players", []):
        tname = ((team_block.get("team") or {}).get("displayName")) or "?"
        for group in team_block.get("statistics", []):
            row = stat_map(group)
            for ath in group.get("athletes", []):
                name = ((ath.get("athlete") or {}).get("displayName") or "").strip()
                if not name or ath.get("didNotPlay"):
                    continue
                s = row(ath)
                if not (s.get("MIN") or "").strip():
                    continue
                teams[name] = tname
                pts = num(s, "PTS"); reb = num(s, "REB"); ast = num(s, "AST")
                out[name]["points"] = pts
                out[name]["rebounds"] = reb
                out[name]["assists"] = ast
                out[name]["pra"] = pts + reb + ast
    return out, teams


PARSERS = {"NFL": parse_nfl, "NBA": parse_nba}


def load_logs():
    try:
        with open(LOGS_PATH) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    for lg in LEAGUE_PATHS:
        data.setdefault(lg, {"seen": [], "games": []})
        data[lg].setdefault("seen", [])
        data[lg].setdefault("games", [])
    return data


def save_logs(data):
    # Bound the file: keep the newest LOG_DAYS worth and the ids that go with it.
    for lg, blob in data.items():
        cut = (date.today() - timedelta(days=LOG_DAYS.get(lg, 400))).isoformat()
        blob["games"] = [g for g in blob["games"] if g.get("date", "") >= cut]
        keep = {g.get("event") for g in blob["games"]}
        blob["seen"] = sorted(x for x in blob["seen"] if x in keep)
    with open(LOGS_PATH, "w") as f:
        json.dump(data, f, separators=(",", ":"))


def refresh_logs(league, diag):
    """Add every finished game not already cached. Newest first, capped."""
    path = LEAGUE_PATHS[league]
    logs = load_logs()
    blob = logs[league]
    seen = set(blob["seen"])

    # A full 400-day sweep is only needed the first time. Once the cache has
    # games in it, anything new is within the last few weeks — re-scanning a
    # year of scoreboards every run would triple the ESPN traffic and add
    # minutes to a job that also runs before kickoff.
    span = LOG_DAYS.get(league, 400) if not blob["games"] else 21
    if blob["games"]:
        newest = max(g.get("date", "") for g in blob["games"])
        try:
            behind = (date.today() - datetime.fromisoformat(newest).date()).days
            span = max(span, min(behind + 3, LOG_DAYS.get(league, 400)))
        except ValueError:
            pass

    wanted = []          # [(event_id, iso_date)] newest first
    today = date.today()
    for i in range(span):
        d = today - timedelta(days=i + 1)
        js = espn_get("/apis/site/v2/sports/%s/scoreboard" % path,
                      {"dates": d.strftime("%Y%m%d"), "limit": 400},
                      diag, "%s scoreboard %s" % (league, d))
        if not js:
            continue
        for ev in js.get("events", []):
            comp = (ev.get("competitions") or [{}])[0]
            if not comp.get("status", {}).get("type", {}).get("completed"):
                continue
            eid = str(ev.get("id") or "")
            if eid and eid not in seen:
                wanted.append((eid, d.isoformat()))
        if len(wanted) >= MAX_NEW_BOXSCORES:
            break
        time.sleep(0.03)

    if not wanted:
        diag.append("%s player logs: cache already current (%d player-games, "
                    "looked back %d days)" % (league, len(blob["games"]), span))
        return logs

    wanted = wanted[:MAX_NEW_BOXSCORES]
    parser = PARSERS[league]
    added = 0
    for eid, iso in wanted:
        js = espn_get("/apis/site/v2/sports/%s/summary" % path, {"event": eid},
                      diag, "%s box score %s" % (league, eid))
        if not js:
            continue
        box = js.get("boxscore") or {}
        try:
            players, teams = parser(box)
        except Exception as e:
            diag.append("%s box score %s did not parse — %s" % (league, eid, e))
            continue
        if not players:
            continue
        # opponent for each team in this game, for the defence adjustment
        sides = []
        for tb in box.get("players", []):
            nm = ((tb.get("team") or {}).get("displayName"))
            if nm:
                sides.append(nm)
        opp_of = {}
        if len(sides) == 2:
            opp_of = {sides[0]: sides[1], sides[1]: sides[0]}
        for name, stats in players.items():
            team = teams.get(name, "?")
            blob["games"].append({
                "event": eid, "date": iso, "player": name, "team": team,
                "opp": opp_of.get(team, "?"),
                **{k: round(v, 2) for k, v in stats.items() if v},
            })
        blob["seen"].append(eid)
        added += 1
        time.sleep(0.03)

    diag.append("%s player logs: added %d box scores, %d player-games cached"
                % (league, added, len(blob["games"])))
    save_logs(logs)
    return logs


# ----------------------------------------------------------- projections ----
def weighted(vals, half_life):
    """(mean, variance, effective n) with the newest entry weighted most.

    vals arrives oldest-first. Weight 0.5 ** (games_ago / half_life), so the
    most recent game counts 1.0 and one half-life back counts 0.5.
    """
    n = len(vals)
    ws = [0.5 ** ((n - 1 - i) / half_life) for i in range(n)]
    sw = sum(ws)
    if sw <= 0:
        return 0.0, 0.0, 0.0
    mean = sum(w * v for w, v in zip(ws, vals)) / sw
    sw2 = sum(w * w for w in ws)
    n_eff = (sw * sw) / sw2 if sw2 else 0.0
    if n_eff <= 1:
        return mean, 0.0, n_eff
    # reliability-weighted (unbiased) variance
    var = sum(w * (v - mean) ** 2 for w, v in zip(ws, vals)) / (sw - sw2 / sw)
    return mean, max(var, 0.0), n_eff


def build_projections(league, logs, diag):
    """{player: {stat: {'mean','sd','n','last','team'}}} plus defence factors."""
    games = sorted(logs[league]["games"], key=lambda g: g.get("date", ""))
    if not games:
        return {}, {}, None

    stats = sorted({MARKETS[m][0] for m in MARKETS if MARKETS[m][2] == league})

    by_player = defaultdict(lambda: defaultdict(list))   # player -> stat -> [(date, v)]
    last_seen, team_of = {}, {}
    allowed = defaultdict(lambda: defaultdict(list))     # defence -> stat -> [per-game total]
    per_game_def = defaultdict(lambda: defaultdict(float))  # (event,def) -> stat -> total

    for g in games:
        p, d = g["player"], g["date"]
        last_seen[p] = max(last_seen.get(p, ""), d)
        team_of[p] = g.get("team", "?")
        for s in stats:
            if s in g:
                by_player[p][s].append((d, float(g[s])))
                if g.get("opp") and g["opp"] != "?":
                    per_game_def[(g["event"], g["opp"])][s] += float(g[s])
    for (_, defence), sm in per_game_def.items():
        for s, v in sm.items():
            allowed[defence][s].append(v)

    # league average allowed per game, then each defence relative to it
    league_avg = {}
    for s in stats:
        pool = [v for dfn in allowed.values() for v in dfn.get(s, [])]
        league_avg[s] = (sum(pool) / len(pool)) if pool else 0.0

    def_factor = defaultdict(dict)
    for defence, sm in allowed.items():
        for s, vals in sm.items():
            if not vals or league_avg.get(s, 0) <= 0:
                continue
            raw = (sum(vals) / len(vals)) / league_avg[s]
            n = len(vals)
            f = (n * raw + DEF_SHRINK * 1.0) / (n + DEF_SHRINK)   # toward neutral
            def_factor[defence][s] = min(DEF_CLAMP[1], max(DEF_CLAMP[0], f))

    # league baseline per stat, over players who actually produce it — this is
    # what a thin sample gets shrunk toward
    baseline = {}
    for s in stats:
        means = [sum(v for _, v in by_player[p][s]) / len(by_player[p][s])
                 for p in by_player if len(by_player[p].get(s, [])) >= 3]
        means = [m for m in means if m > 0]
        means.sort()
        baseline[s] = means[len(means) // 2] if means else 0.0

    half = HALF_LIFE.get(league, 8.0)
    latest = games[-1]["date"]
    proj = {}
    for p, per_stat in by_player.items():
        entry = {}
        for s, series in per_stat.items():
            series.sort(key=lambda x: x[0])
            vals = [v for _, v in series]
            if len(vals) < 3:
                continue
            mean_r, var_r, n_eff = weighted(vals, half)
            entry[s] = {"mean_raw": mean_r, "var_raw": var_r, "n": len(vals),
                        "n_eff": n_eff}
        if entry:
            proj[p] = {"stats": entry, "last": last_seen.get(p, ""),
                       "team": team_of.get(p, "?")}
    return proj, {"def": def_factor, "baseline": baseline, "latest": latest}, latest


# ------------------------------------------------------- distributions ------
def ncdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def gammap(a, x):
    """Regularized lower incomplete gamma P(a, x) = P(X <= x) for Gamma(a, 1)."""
    if a <= 0 or x <= 0:
        return 0.0
    if x < a + 1.0:
        ap, s, d = a, 1.0 / a, 1.0 / a
        for _ in range(1000):
            ap += 1.0
            d *= x / ap
            s += d
            if abs(d) < abs(s) * 1e-14:
                break
        return min(1.0, s * math.exp(-x + a * math.log(x) - math.lgamma(a)))
    tiny = 1e-300
    b = x + 1.0 - a
    c = 1.0 / tiny
    d = 1.0 / b
    h = d
    for i in range(1, 1000):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        step = d * c
        h *= step
        if abs(step - 1.0) < 1e-14:
            break
    q = math.exp(-x + a * math.log(x) - math.lgamma(a)) * h
    return min(1.0, max(0.0, 1.0 - q))


def p_over_gamma(mean, sd, line):
    """P(X > line) for a gamma matched on mean and sd.

    Gamma rather than normal because yardage is non-negative and skews right:
    a receiver's ceiling is far above his median but his floor is zero, and a
    normal curve puts real probability below zero and understates the long
    games.
    """
    if mean <= 0 or sd <= 0:
        return None
    shape = (mean * mean) / (sd * sd)
    scale = (sd * sd) / mean
    if shape <= 0 or scale <= 0:
        return None
    return 1.0 - gammap(shape, line / scale)


def p_over_count(mean, var, line):
    """P(X > line) for a count. Negative binomial when overdispersed, else Poisson.

    line is a half-point (4.5), so P(X > 4.5) = 1 - P(X <= 4): sum the pmf up
    to floor(line). Getting this off by one silently misprices every count prop.
    """
    if mean <= 0:
        return None
    k_max = int(math.floor(line))
    if k_max < 0:
        return 1.0
    if var > mean * 1.02:
        r = (mean * mean) / (var - mean)
        p = r / (r + mean)
        if r <= 0 or not (0 < p < 1):
            return None
        cdf = 0.0
        for k in range(k_max + 1):
            lp = (math.lgamma(k + r) - math.lgamma(r) - math.lgamma(k + 1)
                  + r * math.log(p) + k * math.log1p(-p))
            cdf += math.exp(lp)
    else:
        cdf = 0.0
        for k in range(k_max + 1):
            cdf += math.exp(-mean + k * math.log(mean) - math.lgamma(k + 1))
    return max(0.0, 1.0 - min(1.0, cdf))


def p_at_least_one(lam):
    """P(at least one TD) for a Poisson rate. Complement of P(none) = e^-lam."""
    if lam <= 0:
        return None
    return 1.0 - math.exp(-lam)


def half_point(x):
    return x is not None and abs(x * 2) % 2 == 1


# -------------------------------------------------------------- projecting --
def project(league, player, stat, ctx, proj, opponent, shrink_games):
    """Final mean and sd for one player-stat, or None if not projectable."""
    rec = proj.get(player)
    if not rec or stat not in rec["stats"]:
        return None
    s = rec["stats"][stat]

    # too long since he last appeared — treat as unavailable rather than guess
    try:
        gap = (datetime.fromisoformat(ctx["latest"]).date()
               - datetime.fromisoformat(rec["last"]).date()).days
    except ValueError:
        gap = 0
    if gap > STALE_DAYS.get(league, 21):
        return None

    base = ctx["baseline"].get(stat, 0.0)
    n_eff = s["n_eff"]
    mean = (n_eff * s["mean_raw"] + shrink_games * base) / (n_eff + shrink_games)
    if mean <= 0:
        return None

    factor = ctx["def"].get(opponent, {}).get(stat, 1.0)
    mean *= factor

    var = s["var_raw"]
    if var <= 0:
        var = mean * max(mean, 1.0) * 0.35
    sd = math.sqrt(var)
    # a mean estimated from few games is itself uncertain; widen by that much
    sd = sd * math.sqrt(1.0 + 1.0 / max(n_eff, 1.0))
    return {"mean": mean, "sd": sd, "var": var * (1.0 + 1.0 / max(n_eff, 1.0)),
            "n": s["n"], "factor": factor}


# ------------------------------------------------------------- odds fetch ----
def load_cache():
    try:
        with open(CACHE_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_cache(c):
    with open(CACHE_PATH, "w") as f:
        json.dump(c, f, separators=(",", ":"))


def fetch_event_props(sport_key, event_id, market_keys, keys, exhausted, diag, cache):
    """FanDuel props for one event, from cache when fresh enough.

    Billed per market per event, which is why the cache exists: the pre-kickoff
    refreshes re-read the same slate and would otherwise re-buy every market.
    """
    ck = "%s:%s:%s" % (sport_key, event_id, ",".join(sorted(market_keys)))
    hit = cache.get(ck)
    if hit:
        try:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(hit["at"])).total_seconds() / 60.0
            if age < CACHE_MINUTES:
                return hit["data"], True
        except (ValueError, KeyError):
            pass

    url = ("https://api.the-odds-api.com/v4/sports/%s/events/%s/odds"
           % (sport_key, event_id))
    for i, key in enumerate(keys):
        if key in exhausted:
            continue
        label = "primary" if i == 0 else "backup %d" % i
        try:
            r = requests.get(url, timeout=HTTP_TIMEOUT, params={
                "apiKey": key, "regions": "us", "oddsFormat": "american",
                "markets": ",".join(market_keys), "bookmakers": "fanduel"})
        except Exception as e:
            diag.append("props %s: %s key failed — %s" % (event_id, label, e))
            continue
        if r.status_code == 200:
            left = r.headers.get("x-requests-remaining")
            data = r.json()
            cache[ck] = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                         "data": data}
            if left is not None:
                try:
                    if int(left) <= CREDIT_RESERVE:
                        exhausted.add(key)
                        diag.append("props: %s key down to %s credits — at the "
                                    "reserve floor, stopping paid prop calls on it"
                                    % (label, left))
                except ValueError:
                    pass
            return data, False
        body = r.text[:160]
        if r.status_code in (401, 429) and ("USAGE" in body.upper() or "QUOTA" in body.upper()):
            exhausted.add(key)
            diag.append("props: %s key is out of credits" % label)
            continue
        if r.status_code == 404:
            return None, False        # event has no prop board yet
        diag.append("props %s on %s key: HTTP %d %s" % (event_id, label, r.status_code, body))
        return None, False
    return None, False


# ----------------------------------------------------------------- names ----
SUFFIX = re.compile(r"\b(jr|sr|ii|iii|iv|v)\b")


def norm_name(n):
    n = (n or "").lower()
    n = n.replace(".", " ").replace("'", "").replace("-", " ")
    n = SUFFIX.sub(" ", n)
    return " ".join(n.split())


def short_key(n):
    parts = norm_name(n).split()
    if len(parts) < 2:
        return norm_name(n)
    return parts[0][0] + " " + parts[-1]


def name_index(proj):
    idx, short = {}, {}
    for p in proj:
        idx[norm_name(p)] = p
        short.setdefault(short_key(p), []).append(p)
    return idx, short


def match_player(name, idx, short):
    k = norm_name(name)
    if k in idx:
        return idx[k]
    cands = short.get(short_key(name), [])
    # only accept a first-initial match when it is unambiguous — two players
    # sharing "j smith" must not silently resolve to whichever came first
    return cands[0] if len(cands) == 1 else None


# ----------------------------------------------------------------- build ----
def build_props(league, sport_key, events, keys, exhausted, diag):
    """[{...priced prop side...}] for the given upcoming events."""
    if league not in LEAGUE_PATHS:
        return []
    market_keys = [m for m, v in MARKETS.items() if v[2] == league]
    if not market_keys:
        return []

    logs = refresh_logs(league, diag)
    proj, ctx, latest = build_projections(league, logs, diag)
    if not proj or not ctx:
        diag.append("%s props: no player logs to project from — skipped" % league)
        return []
    idx, short = name_index(proj)
    diag.append("%s props: projections for %d players from %d player-games"
                % (league, len(proj), len(logs[league]["games"])))

    cache = load_cache()
    out = []
    used = cached_hits = 0
    for ev in events[:MAX_EVENTS]:
        eid = ev.get("id")
        home, away = ev.get("home_team"), ev.get("away_team")
        if not (eid and home and away):
            continue
        if all(k in exhausted for k in keys):
            diag.append("props: every key is at or past its reserve — "
                        "%d events left unpriced" % (len(events) - used))
            break
        data, from_cache = fetch_event_props(sport_key, eid, market_keys, keys,
                                             exhausted, diag, cache)
        used += 1
        cached_hits += 1 if from_cache else 0
        if not data:
            continue
        books = data.get("bookmakers") or []
        if not books:
            continue
        for m in books[0].get("markets", []):
            mkey = m.get("key")
            if mkey not in MARKETS:
                continue
            stat, kind, _lg, min_games, shrink = MARKETS[mkey]
            # group the two sides of each player's line together
            per_player = defaultdict(dict)
            for o in m.get("outcomes", []):
                who = o.get("description") or o.get("name")
                side = (o.get("name") or "").lower()
                per_player[who][side] = o
            for who, sides in per_player.items():
                matched = match_player(who, idx, short)
                if not matched:
                    continue
                # a player's own team is where he last played; the defence he
                # faces is the other team in this event
                team = proj[matched]["team"]
                opponent = away if norm_name(team) == norm_name(home) else home
                pr = project(league, matched, stat, ctx, proj, opponent, shrink)
                if not pr or pr["n"] < min_games:
                    continue

                if kind == "poisson":
                    yes = sides.get("yes")
                    if not yes or yes.get("price") is None:
                        continue
                    p = p_at_least_one(pr["mean"])
                    if p is None:
                        continue
                    no = sides.get("no")
                    out.append({
                        "sport": league, "home": home, "away": away,
                        "time": ev.get("commence_time"), "eventId": eid,
                        "player": matched, "market": mkey,
                        "marketLabel": MARKET_LABEL[mkey], "line": None,
                        "side": "Yes", "label": "%s anytime TD" % matched,
                        "p": round(p, 6), "price": yes.get("price"),
                        "other": (no or {}).get("price"),
                        "proj": round(pr["mean"], 3), "sd": None,
                        "n": pr["n"], "defFactor": round(pr["factor"], 3),
                    })
                    continue

                over, under = sides.get("over"), sides.get("under")
                if not over or not under:
                    continue
                line = over.get("point")
                if not half_point(line):
                    continue        # whole number: a push is a third outcome
                if kind == "gamma":
                    p_over = p_over_gamma(pr["mean"], pr["sd"], line)
                else:
                    p_over = p_over_count(pr["mean"], pr["var"], line)
                if p_over is None or not (0 < p_over < 1):
                    continue
                base = {
                    "sport": league, "home": home, "away": away,
                    "time": ev.get("commence_time"), "eventId": eid,
                    "player": matched, "market": mkey,
                    "marketLabel": MARKET_LABEL[mkey], "line": line,
                    "proj": round(pr["mean"], 3), "sd": round(pr["sd"], 3),
                    "n": pr["n"], "defFactor": round(pr["factor"], 3),
                }
                out.append(dict(base, side="Over", p=round(p_over, 6),
                                label="%s Over %s %s" % (matched, line, MARKET_LABEL[mkey].lower()),
                                price=over.get("price"), other=under.get("price")))
                out.append(dict(base, side="Under", p=round(1.0 - p_over, 6),
                                label="%s Under %s %s" % (matched, line, MARKET_LABEL[mkey].lower()),
                                price=under.get("price"), other=over.get("price")))

    save_cache(cache)
    diag.append("%s props: %d events read (%d served from cache, %d billed), "
                "%d priced sides" % (league, used, cached_hits, used - cached_hits, len(out)))
    return out
