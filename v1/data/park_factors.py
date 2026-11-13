"""
data/park_factors.py
=====================
Static park factor reference table (100 = neutral; >100 favors hitters).
Approximate, blended from public Statcast/FanGraphs-style park factor
reporting. Park factors drift year to year (renovations, altitude, roof
changes) -- refresh this table each season from Baseball Savant's Park
Factors leaderboard (baseballsavant.mlb.com/leaderboard/statcast-park-factors).

Used by: engine/grading_factors.py (situational factor) and engine/hr_props.py
(park + motivation overlay).
"""

PARK_FACTORS = {
    # team_abbr: (runs_factor, hr_factor)
    "ARI": (97, 95),
    "ATL": (101, 103),
    "BAL": (112, 119),
    "BOS": (108, 90),
    "CHC": (103, 102),
    "CWS": (102, 106),
    "CIN": (110, 123),
    "CLE": (97, 96),
    "COL": (128, 116),
    "DET": (94, 90),
    "HOU": (103, 108),
    "KC": (101, 104),
    "LAA": (99, 98),
    "LAD": (108, 120),
    "MIA": (92, 90),
    "MIL": (103, 106),
    "MIN": (100, 100),
    "NYM": (97, 94),
    "NYY": (106, 114),
    "OAK": (118, 112),   # Athletics @ Sutter Health Park, West Sacramento (temporary, hitter-friendly)
    "PHI": (107, 110),
    "PIT": (98, 90),
    "SD": (93, 90),
    "SEA": (83, 88),
    "SF": (92, 82),
    "STL": (97, 90),
    "TB": (91, 92),
    "TEX": (108, 112),
    "TOR": (106, 108),
    "WSH": (99, 100),
}


def park_factor_for(team_abbr):
    """Returns (runs_factor, hr_factor) for the HOME team's park. Defaults to
    neutral (100, 100) for unrecognized codes rather than raising."""
    return PARK_FACTORS.get(team_abbr, (100, 100))
