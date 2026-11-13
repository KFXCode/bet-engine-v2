"""
data/teams.py
=============
Canonical MLB team list + name normalization. Different data sources spell
team names differently ("Athletics" vs "Oakland Athletics" vs "OAK") so
every provider runs names through normalize_team() to get one stable key.

Note: the Athletics play as "Athletics" (no city name) at Sutter Health Park
in West Sacramento through the 2027 season while their Las Vegas ballpark is
built -- that's intentional, not a typo.
"""

TEAMS = {
    "ARI": {"name": "Arizona Diamondbacks", "aliases": ["diamondbacks", "arizona", "d-backs", "az"]},
    "ATL": {"name": "Atlanta Braves", "aliases": ["braves", "atlanta"]},
    "BAL": {"name": "Baltimore Orioles", "aliases": ["orioles", "baltimore"]},
    "BOS": {"name": "Boston Red Sox", "aliases": ["red sox", "boston"]},
    "CHC": {"name": "Chicago Cubs", "aliases": ["cubs"]},
    "CWS": {"name": "Chicago White Sox", "aliases": ["white sox"]},
    "CIN": {"name": "Cincinnati Reds", "aliases": ["reds", "cincinnati"]},
    "CLE": {"name": "Cleveland Guardians", "aliases": ["guardians", "cleveland"]},
    "COL": {"name": "Colorado Rockies", "aliases": ["rockies", "colorado"]},
    "DET": {"name": "Detroit Tigers", "aliases": ["tigers", "detroit"]},
    "HOU": {"name": "Houston Astros", "aliases": ["astros", "houston"]},
    "KC": {"name": "Kansas City Royals", "aliases": ["royals", "kansas city"]},
    "LAA": {"name": "Los Angeles Angels", "aliases": ["angels"]},
    "LAD": {"name": "Los Angeles Dodgers", "aliases": ["dodgers"]},
    "MIA": {"name": "Miami Marlins", "aliases": ["marlins", "miami"]},
    "MIL": {"name": "Milwaukee Brewers", "aliases": ["brewers", "milwaukee"]},
    "MIN": {"name": "Minnesota Twins", "aliases": ["twins", "minnesota"]},
    "NYM": {"name": "New York Mets", "aliases": ["mets"]},
    "NYY": {"name": "New York Yankees", "aliases": ["yankees"]},
    "OAK": {"name": "Athletics", "aliases": ["athletics", "oakland", "sacramento", "west sacramento", "a's", "as"]},
    "PHI": {"name": "Philadelphia Phillies", "aliases": ["phillies", "philadelphia"]},
    "PIT": {"name": "Pittsburgh Pirates", "aliases": ["pirates", "pittsburgh"]},
    "SD": {"name": "San Diego Padres", "aliases": ["padres", "san diego"]},
    "SEA": {"name": "Seattle Mariners", "aliases": ["mariners", "seattle"]},
    "SF": {"name": "San Francisco Giants", "aliases": ["giants", "san francisco"]},
    "STL": {"name": "St. Louis Cardinals", "aliases": ["cardinals", "st. louis", "st louis"]},
    "TB": {"name": "Tampa Bay Rays", "aliases": ["rays", "tampa bay"]},
    "TEX": {"name": "Texas Rangers", "aliases": ["rangers", "texas"]},
    "TOR": {"name": "Toronto Blue Jays", "aliases": ["blue jays", "toronto"]},
    "WSH": {"name": "Washington Nationals", "aliases": ["nationals", "washington"]},
}

_LOOKUP = {}
for _abbr, _info in TEAMS.items():
    _LOOKUP[_abbr.lower()] = _abbr
    _LOOKUP[_info["name"].lower()] = _abbr
    for _alias in _info["aliases"]:
        _LOOKUP[_alias.lower()] = _abbr


def normalize_team(raw):
    """Best-effort mapping of any team name/city/abbreviation string to our
    canonical 2-3 letter abbreviation. Returns a best-guess (upper-cased,
    truncated) if nothing matches, so callers still display *something*
    sane instead of raising."""
    if not raw:
        return raw
    key = raw.strip().lower()
    if key in _LOOKUP:
        return _LOOKUP[key]
    for alias, abbr in _LOOKUP.items():
        if alias in key or key in alias:
            return abbr
    return raw.strip().upper()[:3]


def team_full_name(abbr):
    return TEAMS.get(abbr, {}).get("name", abbr)
