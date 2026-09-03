#!/usr/bin/env python3
"""
whop_post.py — posts the card into a Whop forum so it sits behind the paywall.

Access control is the whole point: a forum post inside a Whop experience is
visible only to members with an active subscription. When someone cancels, they
lose the post. Nothing is emailed and nothing sits at a public URL.

Runs after grade.py in the same workflow. Fires on the Monday card and on every
pre-kickoff refresh, but only posts when the card has actually CHANGED — an
unchanged refresh is a no-op, so members are not spammed.

Secrets (repo settings -> Secrets and variables -> Actions):
  WHOP_API_KEY         must carry the forum:post:create permission
  WHOP_EXPERIENCE_ID   the FORUM experience id, exp_xxxxx
Optional:
  MEMBER_LINK          this week's rotating board link, shown in the post
  EDGE_THRESHOLD       default 5
  MAX_CREDIBLE_EDGE    default 20

Missing secrets are a silent no-op. A Whop outage never crashes the run.
"""

import hashlib
import json
import os
import sys

import requests

# The endpoint is /forum_posts with an UNDERSCORE. The hyphenated form
# /forum-posts returns 404 while the workflow still reports success, which
# looks exactly like "posted fine, nothing showed up".
WHOP_URL = "https://api.whop.com/api/v1/forum_posts"

API_KEY = os.environ.get("WHOP_API_KEY", "").strip()
EXPERIENCE_ID = os.environ.get("WHOP_EXPERIENCE_ID", "").strip()
MEMBER_LINK = os.environ.get("MEMBER_LINK", "").strip()

SLATE_PATH = os.environ.get("EDGE_SLATE_PATH", "edge_slate.json")
RESULTS_PATH = os.environ.get("RESULTS_PATH", "results.json")
STATE_PATH = os.environ.get("WHOP_STATE_PATH", "whop_posted.json")
HTTP_TIMEOUT = 25


def amer(v):
    """American odds, formatted the way a sportsbook shows them."""
    if v is None:
        return "n/a"
    v = int(round(float(v)))
    return ("+%d" % v) if v > 0 else ("%d" % v)


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def kickoff_window(iso):
    """Group picks by the day they kick off, so each window posts as one card."""
    from datetime import datetime
    try:
        d = datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
    except ValueError:
        return "Unscheduled"
    return d.strftime("%A, %b %d")


def build_markdown(picks, results):
    """Phone-scannable. Bet and price bold and first, edge immediately after,
    at most two lines of reasoning. Empty sections are omitted entirely."""
    if not picks:
        return None, None

    picks = sorted(picks, key=lambda p: -p["edge"])
    windows = {}
    for p in picks:
        windows.setdefault(kickoff_window(p.get("commence")), []).append(p)

    lines = []
    lines.append("**%d picks cleared the rule.** Ranked by edge, biggest first."
                 % len(picks))
    lines.append("")
    lines.append("Every pick below is a **single bet**. They were each priced on "
                 "their own — parlaying them multiplies the vig.")
    lines.append("")

    for window, group in windows.items():
        lines.append("---")
        lines.append("### %s" % window)
        lines.append("")
        for p in group:
            lines.append("**%s  %s**" % (p["label"], amer(p["price"])))
            lines.append("Edge **+%.1f pts** · our %.0f%% vs price %.0f%% · EV %+.1f¢ per $1"
                         % (p["edge"], p["p"] * 100.0, p["implied"] * 100.0,
                            p["ev"] * 100.0))
            lines.append("*%s · %s*" % (p["sport"], p["game"]))
            lines.append("")

    lines.append("---")

    # footer: average CLV with its sample size. A CLV number without n is
    # meaningless, so n is always printed next to it.
    avg_clv = results.get("avg_clv")
    beat = results.get("clv_beat_rate")
    n = len([r for r in results.get("picks", []) if r.get("clv") is not None])
    rec = results.get("record") or {}
    w, l, pu = rec.get("w", 0), rec.get("l", 0), rec.get("p", 0)

    if avg_clv is not None and n:
        lines.append("**Average closing-line value: %+.2f pts** across %d graded picks"
                     % (avg_clv, n)
                     + (" · %.0f%% beat the close" % (beat * 100.0) if beat is not None else ""))
    else:
        lines.append("**Closing-line value:** not yet — no graded picks. "
                     "This fills in as games finish.")

    if w + l + pu:
        lines.append("Record %d-%d%s" % (w, l, (" (%d push)" % pu) if pu else ""))

    if MEMBER_LINK:
        lines.append("")
        lines.append("Full board — probabilities, EV, the arithmetic and the "
                     "Passes tab: %s" % MEMBER_LINK)

    lines.append("")
    lines.append("_Confirm the current price at your book before placing "
                 "anything — lines move. Entertainment only, not financial "
                 "advice. 21+. 1-800-GAMBLER._")

    body = "\n".join(lines)
    title = "%d picks — %s" % (len(picks), list(windows.keys())[0])
    return title, body


def post(title, content):
    """Post to Whop. Never raises — a Whop outage must not fail the run."""
    payload = {
        "experience_id": EXPERIENCE_ID,
        "title": title,
        "content": content,
        "is_mention": True,
    }
    try:
        r = requests.post(
            WHOP_URL,
            headers={"Authorization": "Bearer %s" % API_KEY,
                     "Content-Type": "application/json"},
            json=payload,
            timeout=HTTP_TIMEOUT,
        )
    except Exception as e:
        print("Whop request failed outright: %s" % e)
        print("The card is still published to the board; only the forum post "
              "was skipped.")
        return False

    if 200 <= r.status_code < 300:
        print("Posted to Whop forum (HTTP %d)." % r.status_code)
        return True

    # Log the API's own message, then a plain-English reading of it.
    try:
        detail = json.dumps(r.json())
    except ValueError:
        detail = r.text[:400]
    print("Whop returned HTTP %d: %s" % (r.status_code, detail))

    hints = {
        401: "401 means the API key is wrong, expired, or was pasted with "
             "whitespace. Regenerate it and update the WHOP_API_KEY secret.",
        403: "403 means the key lacks the forum:post:create permission. Edit "
             "the key in Whop's developer settings and enable that scope — the "
             "key itself is fine.",
        404: "404 means WHOP_EXPERIENCE_ID is not a forum experience. It must "
             "be the exp_xxxxx id of the FORUM itself — not the whop id, not "
             "the product id. Open the forum in Whop and copy the exp_ id from "
             "the URL.",
        400: "400 usually means the same missing-permission problem, reported "
             "as 'Actor is missing all required permissions'. Check the key's "
             "scopes.",
    }
    if r.status_code in hints:
        print("HINT: %s" % hints[r.status_code])
    return False


def main():
    if not API_KEY or not EXPERIENCE_ID:
        print("WHOP_API_KEY or WHOP_EXPERIENCE_ID not set — skipping the forum "
              "post. This is a no-op, not an error.")
        return

    slate = load(SLATE_PATH, None)
    if not slate:
        print("No slate on disk — nothing to post.")
        return

    # Derive picks with the SAME function the grader uses, so the forum post and
    # the graded ledger can never disagree about what was published.
    try:
        from grade import todays_picks
    except Exception as e:
        print("Could not import the pick rule from grade.py: %s" % e)
        return

    raw = todays_picks(slate)
    if not raw:
        print("No picks cleared the rule — posting nothing. An empty card is a "
              "normal outcome and does not need an announcement.")
        return

    picks = []
    for c in raw:
        picks.append({
            "sport": c["sport"],
            "game": "%s @ %s" % (c["away"], c["home"]),
            "label": c["label"],
            "price": c["price"],
            "p": c["p"],
            "implied": 1.0 / (1.0 + c["price"] / 100.0) if c["price"] > 0
                       else 1.0 / (1.0 + 100.0 / abs(c["price"])),
            "edge": c["edge"],
            "ev": c["ev"],
            "commence": c.get("commence"),
        })

    results = load(RESULTS_PATH, {})
    title, body = build_markdown(picks, results)
    if not body:
        return

    # Only post when the card actually changed. Hash the picks themselves, not
    # the rendered text, so a moving CLV footer alone does not trigger a repost.
    sig = hashlib.sha256(json.dumps(
        sorted([(p["label"], p["price"], round(p["edge"], 2)) for p in picks]),
        sort_keys=True).encode()).hexdigest()

    state = load(STATE_PATH, {})
    if state.get("signature") == sig:
        print("Card is unchanged since the last post — not reposting.")
        return

    if post(title, body):
        state["signature"] = sig
        state["posted_count"] = len(picks)
        with open(STATE_PATH, "w") as f:
            json.dump(state, f, indent=1)
        print("Recorded the post signature so refreshes will not repost this "
              "same card.")
    else:
        print("Post failed — signature not recorded, so the next run will try "
              "again.")


if __name__ == "__main__":
    main()
