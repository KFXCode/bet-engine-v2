"""
data/public_betting_scraper.py
================================
Best-effort scraper for a public-betting-splits PAGE URL you provide (e.g.
sportsbettingdime.com's MLB odds or public-betting-trends page) -- lets
config.PUBLIC_BETTING_URL replace typing tickets%/handle% numbers into
manual_inputs/public_betting_<date>.json by hand every day.

If that page is a stable/evergreen URL (no date in it -- sportsbettingdime's
own MLB odds and trends pages are, they just show "today" whenever you load
them), you only ever set this ONE time; the daily run re-fetches the same
URL and gets that day's fresh numbers automatically.

HOW IT PARSES: fetches the page, strips HTML down to plain running text,
and for each of today's teams looks for that team's name/alias then reads
the 1-2 percentages immediately following it as (bet%, handle%). This is
deliberately layout-agnostic (text-pattern matching, not a rigid table/CSS
scraper) since there's no way to know a given page's exact markup ahead of
time -- more resilient to minor page-layout tweaks, but it CAN misread a
page whose running-text order doesn't put a team's own numbers right after
its name.

KNOWN LIMITATION: sites commonly paywall the money%/handle% number (e.g.
sportsbettingdime gates it behind "SBD Plus") while leaving bet%/tickets%
free. If only one percentage is found near a team, it's used as bet% with
handle% left unknown -- that game is flagged data_quality="partial" rather
than guessed, so the report is honest about what's real vs backfilled.

If a game comes back "not found" or a number looks wrong, that means this
page's real text layout doesn't match the assumptions above -- send the URL
and what the page actually shows, and this parsing logic can be tightened
against the real thing (it was written from page-content research, not a
live look at the raw markup).
"""

import logging
import re

import requests

from data.teams import TEAMS
from data.http_utils import patch_requests_for_scraping

logger = logging.getLogger(__name__)

patch_requests_for_scraping()

_PCT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")
_NEARBY_WINDOW = 160  # chars after a team-name mention to look for its own percentages


def _strip_html(html):
    """Cheap tag stripper -- good enough to turn a page into readable
    running text without a hard bs4/lxml dependency for this one function."""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&#39;|&rsquo;|&apos;", "'", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _team_mention_offsets(text_lower, team_abbr):
    info = TEAMS.get(team_abbr)
    if not info:
        return []
    names = sorted([info["name"], team_abbr] + info["aliases"], key=len, reverse=True)
    offsets = []
    for name in names:
        for m in re.finditer(re.escape(name.lower()), text_lower):
            offsets.append(m.start())
    return sorted(offsets)


def _nearby_percentages(text, offset):
    snippet = text[offset:offset + _NEARBY_WINDOW]
    return [float(m.group(1)) for m in _PCT_RE.finditer(snippet)]


def fetch_and_parse_splits(url, games):
    """Returns dict game_id -> PublicSplit for whichever of `games` this
    page's text seems to mention. Games not found are simply absent from
    the returned dict -- the caller (public_betting_provider.py) fills in a
    neutral "missing" split for those, same as it already does for anything
    the manual JSON file doesn't cover."""
    from data.public_betting_provider import PublicSplit  # lazy -- avoids a circular import

    if not url:
        return {}

    try:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        text = _strip_html(resp.text)
    except Exception as exc:
        logger.warning("Couldn't fetch/parse public betting URL %s: %s", url, exc)
        return {}

    text_lower = text.lower()
    out = {}
    for game in games:
        home_offsets = _team_mention_offsets(text_lower, game.home_team)
        away_offsets = _team_mention_offsets(text_lower, game.away_team)
        if not home_offsets and not away_offsets:
            continue

        home_pcts = _nearby_percentages(text, home_offsets[0]) if home_offsets else []
        away_pcts = _nearby_percentages(text, away_offsets[0]) if away_offsets else []

        home_bet = home_pcts[0] if home_pcts else None
        home_handle = home_pcts[1] if len(home_pcts) > 1 else None

        if home_bet is None and away_pcts:
            # Only the away side's numbers turned up -- infer home's as the
            # complement (two-way market, bet% + handle% each sum to ~100).
            home_bet = round(100 - away_pcts[0], 1)
            home_handle = round(100 - away_pcts[1], 1) if len(away_pcts) > 1 else None

        if home_bet is None:
            continue

        quality = "url" if home_handle is not None else "partial"
        out[game.game_id] = PublicSplit(
            tickets_pct_home=home_bet,
            handle_pct_home=home_handle if home_handle is not None else home_bet,
            source=url,
            data_quality=quality,
        )
    return out
