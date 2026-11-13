"""
output/publish_github_pages.py
================================
Pushes the freshly-rendered latest.html straight to a GitHub repo via the
GitHub REST API (no local git install/config needed) so it serves at a
stable GitHub Pages URL -- the "one link" for your phone.

Uses the "Create or update file contents" endpoint:
https://docs.github.com/en/rest/repos/contents

One-time setup (see README.md's "Mobile access" section for the long
version):
  1. Create a GitHub repo (public, so Pages is free) -- e.g. "mlb-picks".
  2. GitHub -> Settings -> Developer settings -> Fine-grained tokens ->
     generate one scoped to just that repo, permission "Contents: Read and
     write". Copy it.
  3. In that repo: Settings -> Pages -> Source: "Deploy from a branch" ->
     Branch: main / (root).
  4. Fill in .env: GITHUB_PAGES_ENABLED=true, GITHUB_TOKEN=<the token>,
     GITHUB_REPO=<your-username>/<repo-name>.
  5. Your link is https://<your-username>.github.io/<repo-name>/

Every run after that, run_daily.py calls publish_latest_report() with the
rendered HTML and it shows up at that same URL within a few seconds.
Failures here (bad token, no internet, repo not found) are logged and
swallowed -- a publish problem should never crash the local report.
"""

import base64
import logging

import requests

import config

logger = logging.getLogger(__name__)

API_BASE = "https://api.github.com"


def publish_latest_report(html_content):
    if not config.GITHUB_PAGES_ENABLED:
        return {"published": False, "reason": "disabled"}
    if not config.GITHUB_TOKEN or not config.GITHUB_REPO:
        logger.warning("GITHUB_PAGES_ENABLED is true but GITHUB_TOKEN/GITHUB_REPO isn't set in .env -- skipping publish.")
        return {"published": False, "reason": "not_configured"}

    url = f"{API_BASE}/repos/{config.GITHUB_REPO}/contents/{config.GITHUB_PAGES_PATH}"
    headers = {
        "Authorization": f"Bearer {config.GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        sha = _get_existing_sha(url, headers)
        payload = {
            "message": f"Daily report update ({config.GITHUB_BRANCH})",
            "content": base64.b64encode(html_content.encode("utf-8")).decode("ascii"),
            "branch": config.GITHUB_BRANCH,
        }
        if sha:
            payload["sha"] = sha

        resp = requests.put(url, headers=headers, json=payload, timeout=20)
        if resp.status_code not in (200, 201):
            logger.warning("GitHub Pages publish failed (%s): %s", resp.status_code, resp.text[:300])
            return {"published": False, "reason": f"http_{resp.status_code}"}

        pages_url = _pages_url()
        logger.info("Published to GitHub Pages: %s", pages_url)
        return {"published": True, "url": pages_url}
    except Exception as exc:
        logger.warning("GitHub Pages publish failed: %s", exc)
        return {"published": False, "reason": str(exc)}


def _get_existing_sha(url, headers):
    """The GitHub contents API requires the current file's sha to update it
    (not needed the very first time it's created). Returns None if the file
    doesn't exist yet."""
    resp = requests.get(url, headers=headers, params={"ref": config.GITHUB_BRANCH}, timeout=20)
    if resp.status_code == 200:
        return resp.json().get("sha")
    return None


def _pages_url():
    """Assumes the README setup (Pages source = branch root). index.html is
    the implicit default document there; any other path is appended as-is."""
    owner, _, repo = config.GITHUB_REPO.partition("/")
    base = f"https://{owner}.github.io/{repo}/"
    path = config.GITHUB_PAGES_PATH
    return base if path in ("index.html", "") else f"{base}{path}"
