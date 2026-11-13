"""
data/http_utils.py
====================
Shared HTTP helpers for the scraper-style data sources in this project
(data/stats_provider.py, data/public_betting_scraper.py). Currently just the
browser User-Agent patch -- pulled out here so both scrapers use the exact
same fix instead of two copies drifting apart.
"""

import requests

# Plenty of scraped sites (FanGraphs, sportsbettingdime, etc.) block the
# default "python-requests/X.X" user agent with a 403/blank page -- it's
# bot-detection on request headers, not a real outage. This makes every
# outgoing request in the process look like a normal desktop browser
# instead, which is the standard fix.
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}


def patch_requests_for_scraping():
    """Idempotent -- safe to call from every module that needs it."""
    if getattr(requests.Session.request, "_patched_for_scraping", False):
        return

    _original_request = requests.Session.request

    def _patched_request(self, method, url, **kwargs):
        headers = kwargs.get("headers") or {}
        for k, v in BROWSER_HEADERS.items():
            headers.setdefault(k, v)
        kwargs["headers"] = headers
        return _original_request(self, method, url, **kwargs)

    _patched_request._patched_for_scraping = True
    requests.Session.request = _patched_request
