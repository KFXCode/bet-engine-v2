"""
data/stats_provider.py
=======================
Advanced pitching/batting metrics via pybaseball (free, wraps FanGraphs +
Baseball Savant). This is the most fragile external dependency in the
project -- pybaseball scrapes public leaderboards that occasionally change
shape, and Statcast pulls are slow. Every call in this file is wrapped so a
single failed lookup degrades that ONE factor (data_quality="degraded")
instead of killing the whole daily run.

Results are cached in SQLite (stats_cache table) for CACHE_TTL_HOURS so you
don't re-scrape the same season leaderboard every time you run the tool.

If pybaseball changes a function name/column on you, this is the only file
you should need to touch.
"""

import logging
import time
from dataclasses import dataclass, asdict

import pandas as pd

import config
from data.http_utils import patch_requests_for_scraping

logger = logging.getLogger(__name__)

CACHE_TTL_HOURS = 12

patch_requests_for_scraping()


@dataclass
class PitcherProfile:
    name: str
    fip: float = None
    era: float = None
    k_pct: float = None
    bb_pct: float = None
    barrel_pct_allowed: float = None
    hard_hit_pct_allowed: float = None
    hr_per_9: float = None
    data_quality: str = "ok"


@dataclass
class TeamOffenseProfile:
    team: str
    barrel_pct: float = None
    hard_hit_pct: float = None
    woba: float = None
    data_quality: str = "ok"


@dataclass
class BatterProfile:
    name: str
    team: str = None
    barrel_pct: float = None
    hard_hit_pct: float = None
    iso: float = None
    hr_count: int = None
    recent_barrel_trend: float = None  # last-15-day barrel% minus season barrel%
    data_quality: str = "ok"


class _StatsCache:
    """Tiny key/value cache table living in the same SQLite file as
    everything else, so repeated runs in one day don't re-hit
    pybaseball/Savant."""

    def __init__(self, db_path=None):
        import sqlite3
        self.conn = sqlite3.connect(str(db_path or config.DB_PATH))
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS stats_cache (
                   key TEXT PRIMARY KEY, payload TEXT NOT NULL, cached_at REAL NOT NULL
               )"""
        )
        self.conn.commit()

    def get(self, key):
        import json
        row = self.conn.execute(
            "SELECT payload, cached_at FROM stats_cache WHERE key=?", (key,)
        ).fetchone()
        if not row:
            return None
        payload, cached_at = row
        if time.time() - cached_at > CACHE_TTL_HOURS * 3600:
            return None
        try:
            return json.loads(payload)
        except Exception:
            return None

    def set(self, key, value):
        import json
        self.conn.execute(
            "INSERT OR REPLACE INTO stats_cache (key, payload, cached_at) VALUES (?, ?, ?)",
            (key, json.dumps(value), time.time()),
        )
        self.conn.commit()


class StatsProvider:
    def get_pitcher_profile(self, pitcher_name):
        raise NotImplementedError

    def get_team_offense_profile(self, team_abbr):
        raise NotImplementedError

    def get_batter_profile(self, batter_name, team_abbr=None):
        raise NotImplementedError


class PyBaseballStatsProvider(StatsProvider):
    def __init__(self):
        self.cache = _StatsCache()
        self._pitching_table = None
        self._batting_table = None
        self._pitcher_barrel_table = None   # season-long Statcast LEADERBOARD (one fast call), not per-player pulls
        self._batter_barrel_table = None

    # -- pitchers -------------------------------------------------------
    def get_pitcher_profile(self, pitcher_name):
        if not pitcher_name or pitcher_name == "TBD":
            return PitcherProfile(name=pitcher_name or "TBD", data_quality="missing")

        cache_key = f"pitcher:{pitcher_name}:{_season()}"
        cached = self.cache.get(cache_key)
        if cached:
            return PitcherProfile(**cached)

        try:
            row = self._lookup_pitcher_row(pitcher_name)
            if row is None:
                profile = PitcherProfile(name=pitcher_name, data_quality="not_found")
            else:
                profile = PitcherProfile(
                    name=pitcher_name,
                    fip=_safe_float(row.get("FIP")),
                    era=_safe_float(row.get("ERA")),
                    k_pct=_safe_float(row.get("K%")),
                    bb_pct=_safe_float(row.get("BB%")),
                    hr_per_9=_safe_float(row.get("HR/9")),
                    data_quality="ok",
                )
                barrel, hard_hit = self._statcast_barrel_hard_hit_allowed(pitcher_name)
                profile.barrel_pct_allowed = barrel
                profile.hard_hit_pct_allowed = hard_hit
                if barrel is None:
                    profile.data_quality = "partial"
        except Exception as exc:
            logger.warning("pitcher profile lookup failed for %s: %s", pitcher_name, exc)
            profile = PitcherProfile(name=pitcher_name, data_quality="degraded")

        self.cache.set(cache_key, asdict(profile))
        return profile

    def _lookup_pitcher_row(self, pitcher_name):
        import pybaseball as pyb  # imported lazily so a broken pybaseball
        # install doesn't block the whole program from starting.

        if self._pitching_table is None:
            pyb.cache.enable()
            season = _season()
            self._pitching_table = pyb.pitching_stats(season, season, qual=0)
        table = self._pitching_table
        matches = table[table["Name"].str.lower() == pitcher_name.lower()]
        if matches.empty:
            last = pitcher_name.split()[-1].lower()
            matches = table[table["Name"].str.lower().str.contains(last, na=False)]
        if matches.empty:
            return None
        return matches.iloc[0].to_dict()

    def _statcast_barrel_hard_hit_allowed(self, pitcher_name):
        """Barrel%/hard-hit% allowed, from Baseball Savant's precomputed
        season leaderboard (one bulk call, cached) instead of pulling every
        pitch the pitcher threw all season (statcast_pitcher) -- that raw
        per-player pull is what was silently timing out/erroring for most
        pitchers. Returns (None, None) on any failure -- callers treat that
        as 'unavailable', not fatal."""
        try:
            import pybaseball as pyb

            if self._pitcher_barrel_table is None:
                self._pitcher_barrel_table = pyb.statcast_pitcher_exitvelo_barrels(_season(), minBBE=0)
            table = self._pitcher_barrel_table
            if table is None or table.empty:
                return None, None

            row = _match_savant_name(table, pitcher_name)
            if row is None:
                return None, None
            barrel = _safe_float(row.get("brl_percent"))
            hard_hit = _safe_float(row.get("ev95percent"))
            return barrel, hard_hit
        except Exception as exc:
            logger.debug("statcast pitcher leaderboard lookup failed for %s: %s", pitcher_name, exc)
            return None, None

    # -- team offense -----------------------------------------------------
    def get_team_offense_profile(self, team_abbr):
        cache_key = f"team_offense:{team_abbr}:{_season()}"
        cached = self.cache.get(cache_key)
        if cached:
            return TeamOffenseProfile(**cached)
        try:
            import pybaseball as pyb

            season = _season()
            table = pyb.team_batting(season, season)
            row = table[table["Team"].str.contains(team_abbr, case=False, na=False)]
            if row.empty:
                profile = TeamOffenseProfile(team=team_abbr, data_quality="not_found")
            else:
                r = row.iloc[0].to_dict()
                profile = TeamOffenseProfile(
                    team=team_abbr, woba=_safe_float(r.get("wOBA")),
                    data_quality="partial",  # FanGraphs team_batting doesn't include barrel/hard-hit directly
                )
        except Exception as exc:
            logger.warning("team offense lookup failed for %s: %s", team_abbr, exc)
            profile = TeamOffenseProfile(team=team_abbr, data_quality="degraded")
        self.cache.set(cache_key, asdict(profile))
        return profile

    # -- batters (used by HR prop workflow) --------------------------------
    def get_batter_profile(self, batter_name, team_abbr=None):
        cache_key = f"batter:{batter_name}:{_season()}"
        cached = self.cache.get(cache_key)
        if cached:
            return BatterProfile(**cached)
        try:
            import pybaseball as pyb

            if self._batter_barrel_table is None:
                self._batter_barrel_table = pyb.statcast_batter_exitvelo_barrels(_season(), minBBE=0)
            table = self._batter_barrel_table
            if table is None or table.empty:
                profile = BatterProfile(name=batter_name, team=team_abbr, data_quality="degraded")
            else:
                row = _match_savant_name(table, batter_name)
                if row is None:
                    profile = BatterProfile(name=batter_name, team=team_abbr, data_quality="not_found")
                else:
                    barrel = _safe_float(row.get("brl_percent"))
                    hard_hit = _safe_float(row.get("ev95percent"))
                    profile = BatterProfile(
                        name=batter_name, team=team_abbr, barrel_pct=barrel, hard_hit_pct=hard_hit,
                        data_quality="ok" if barrel is not None else "partial",
                    )
        except Exception as exc:
            logger.warning("batter profile lookup failed for %s: %s", batter_name, exc)
            profile = BatterProfile(name=batter_name, team=team_abbr, data_quality="degraded")
        self.cache.set(cache_key, asdict(profile))
        return profile


def _season():
    from datetime import datetime
    return datetime.now().year


def _match_savant_name(table, full_name):
    """Baseball Savant leaderboard tables key players by 'Last, First' in a
    'last_name, first_name' column -- convert our 'First Last' and match,
    falling back to a last-name-only contains match for suffixes/accents."""
    if "last_name, first_name" not in table.columns:
        return None
    first, last = _split_name(full_name)
    target = f"{last}, {first}".strip().lower()
    col = table["last_name, first_name"].astype(str).str.lower()
    exact = table[col == target]
    if not exact.empty:
        return exact.iloc[0].to_dict()
    contains = table[col.str.contains(last.lower(), na=False)]
    if not contains.empty:
        return contains.iloc[0].to_dict()
    return None


def _safe_float(v):
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _split_name(full_name):
    parts = full_name.strip().split()
    if len(parts) < 2:
        return full_name, ""
    return parts[0], " ".join(parts[1:])


class _MockStatsProvider(StatsProvider):
    """Neutral placeholder stats so the pipeline runs with STATS_MODE=mock --
    useful for a quick, network-free smoke test of the whole pipeline."""

    def get_pitcher_profile(self, pitcher_name):
        return PitcherProfile(name=pitcher_name or "TBD", fip=4.20, era=4.20, k_pct=22.0,
                               bb_pct=8.0, barrel_pct_allowed=7.5, hard_hit_pct_allowed=38.0,
                               hr_per_9=1.3, data_quality="mock")

    def get_team_offense_profile(self, team_abbr):
        return TeamOffenseProfile(team=team_abbr, barrel_pct=7.5, hard_hit_pct=38.0, woba=0.315, data_quality="mock")

    def get_batter_profile(self, batter_name, team_abbr=None):
        return BatterProfile(name=batter_name, team=team_abbr, barrel_pct=7.5, hard_hit_pct=38.0,
                              iso=0.16, hr_count=10, recent_barrel_trend=0.0, data_quality="mock")


def get_stats_provider():
    if config.STATS_MODE == "api":
        return PyBaseballStatsProvider()
    return _MockStatsProvider()
