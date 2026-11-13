"""
data/rosters.py
================
Active-roster batter lists via MLB Stats API, used by the HR prop workflow.

Note: this is the ACTIVE ROSTER, not confirmed starting lineups -- lineups
don't post until roughly 3-4 hours before first pitch. Re-run the HR prop
workflow later in the day for tighter accuracy; for the morning run this is
the best available approximation.
"""

import logging

import requests

import config
from data.situational import TEAM_IDS

logger = logging.getLogger(__name__)

ROSTER_API = "https://statsapi.mlb.com/api/v1/teams/{team_id}/roster"
PITCHER_POSITIONS = {"P"}


def get_team_batters(team_abbr):
    team_id = TEAM_IDS.get(team_abbr)
    if not team_id:
        return []
    try:
        resp = requests.get(ROSTER_API.format(team_id=team_id), params={"rosterType": "active"}, timeout=15)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        logger.warning("Roster fetch failed for %s: %s", team_abbr, exc)
        return []

    batters = []
    for entry in payload.get("roster", []):
        pos = entry.get("position", {}).get("abbreviation")
        if pos in PITCHER_POSITIONS:
            continue
        name = entry.get("person", {}).get("fullName")
        if name:
            batters.append(name)
    # Roster order isn't meaningful (jersey/alpha), but capping here keeps the
    # daily run's statcast-pull time bounded -- see config.HR_PROP_ROSTER_LIMIT.
    return batters[: config.HR_PROP_ROSTER_LIMIT]
