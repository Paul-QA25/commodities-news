#!/usr/bin/env python3
"""
Daily fetcher for commodities RSS feeds:
  - The Hindu BusinessLine (Commodities)
  - OilPrice.com (Main feed)

What it does each run:
  1. Downloads all configured feeds (see FEEDS dict below).
  2. Compares entries against a local "seen" list (seen_articles.json)
     so already-processed items aren't reported again.
  3. Prints any new items (plain text, tagged by source) and appends
     them to a log file (commodities_feed_log.jsonl), one JSON object
     per line.
  4. Writes a WhatsApp-ready plain-text digest file
     (commodities_digest_YYYY-MM-DD.txt).

To add more feeds later, just add another "Name": "url" entry to the
FEEDS dictionary below.

Run manually:
    python3 fetch_commodities_rss.py

Automate it (see notes at the bottom of this file for cron / Task
Scheduler / systemd examples).
"""

import html
import json
import os
import re
import sys
from datetime import datetime, timezone

import feedparser


def clean_html(raw_html: str) -> str:
    """Strip HTML tags and decode entities, returning plain text."""
    if not raw_html:
        return ""
    # Remove tags
    text = re.sub(r"<[^>]+>", " ", raw_html)
    # Decode entities like &amp; &nbsp; etc.
    text = html.unescape(text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text

FEEDS = {
    "BusinessLine Commodities": "https://www.thehindubusinessline.com/markets/commodities/feeder/default.rss",
    "OilPrice.com": "https://oilprice.com/rss/main",
}

# Files are created next to this script when run as a .py file, so
# cron/Task Scheduler runs always find them. Jupyter notebooks don't
# define __file__, so fall back to the current working directory there.
try:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
except NameError:
    BASE_DIR = os.getcwd()
SEEN_FILE = os.path.join(BASE_DIR, "seen_articles.json")
LOG_FILE = os.path.join(BASE_DIR, "commodities_feed_log.jsonl")


def load_seen() -> set:
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen(seen: set) -> None:
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, indent=2)


def fetch_new_articles():
    seen = load_seen()
    new_items = []

    for source_name, feed_url in FEEDS.items():
        feed = feedparser.parse(feed_url)

        if feed.bozo:
            # bozo=True means the feed didn't parse perfectly; often still usable,
            # but worth flagging so you notice if a source changes its feed format.
            print(f"Warning: '{source_name}' feed had a parse issue: {feed.bozo_exception}", file=sys.stderr)

        if not feed.entries:
            print(f"No entries returned for '{source_name}' - it may be empty, blocked, or unreachable.")
            continue

        for entry in feed.entries:
            guid = entry.get("id") or entry.get("link")
            if guid and guid not in seen:
                new_items.append({
                    "source": source_name,
                    "title": clean_html(entry.get("title", "")),
                    "link": entry.get("link", ""),
                    "published": entry.get("published", ""),
                    "summary": clean_html(entry.get("summary", "")),
                    "guid": guid,
                })
                seen.add(guid)

    save_seen(seen)
    return new_items


def log_articles(items) -> None:
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        for item in items:
            record = dict(item)
            record["fetched_at"] = datetime.now(timezone.utc).isoformat()
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def print_digest(items) -> None:
    """Print a clean, plain-text digest of articles to the console."""
    print(f"\n{len(items)} new article(s) — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 70)
    for i, item in enumerate(items, 1):
        print(f"\n[{i}] ({item.get('source', 'Unknown')}) {item['title']}")
        if item.get("published"):
            print(f"    Published: {item['published']}")
        if item.get("summary"):
            print(f"    {item['summary']}")
        print(f"    Link: {item['link']}")
    print("\n" + "=" * 70)


def write_txt_digest(items) -> str:
    """Write a plain-text digest file suitable for sharing (e.g. on WhatsApp).
    Always writes a file for today, even if there are no new items, so
    automated delivery (e.g. daily email) has something consistent to find.
    Returns the path to the file written."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    txt_path = os.path.join(BASE_DIR, f"commodities_digest_{today}.txt")

    lines = []
    lines.append("COMMODITIES NEWS DIGEST")
    lines.append(f"{today}")
    lines.append("-" * 40)

    if items:
        for i, item in enumerate(items, 1):
            lines.append("")
            lines.append(f"{i}. [{item.get('source', 'Unknown')}] {item['title']}")
            if item.get("summary"):
                lines.append(item["summary"])
            lines.append(item["link"])
    else:
        lines.append("")
        lines.append("No new articles since yesterday.")

    lines.append("")
    lines.append("-" * 40)

    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    return txt_path


def main():
    new_items = fetch_new_articles()
    if new_items:
        print_digest(new_items)
        log_articles(new_items)
    else:
        print("No new articles since last run.")
    txt_path = write_txt_digest(new_items)
    print(f"\nPlain-text digest saved to: {txt_path}")


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------------
# AUTOMATING THIS DAILY
# ---------------------------------------------------------------------------
#
# First, install the one dependency:
#   pip install feedparser
#
# --- Linux / macOS (cron) ---
# Run: crontab -e
# Add a line to run every day at 7:00 AM:
#   0 7 * * * /usr/bin/python3 /full/path/to/fetch_commodities_rss.py >> /full/path/to/cron.log 2>&1
#
# --- Linux (systemd timer, alternative to cron) ---
# Create /etc/systemd/system/rss-fetch.service:
#   [Service]
#   ExecStart=/usr/bin/python3 /full/path/to/fetch_commodities_rss.py
# Create /etc/systemd/system/rss-fetch.timer:
#   [Timer]
#   OnCalendar=*-*-* 07:00:00
#   Persistent=true
#   [Install]
#   WantedBy=timers.target
# Then: sudo systemctl enable --now rss-fetch.timer
#
# --- Windows (Task Scheduler) ---
# 1. Open Task Scheduler -> Create Basic Task
# 2. Trigger: Daily, pick a time
# 3. Action: Start a program
#      Program/script: python
#      Arguments: C:\full\path\to\fetch_commodities_rss.py
#
# --- Cloud alternative (no machine needed) ---
# A free GitHub Actions workflow with a "schedule: cron" trigger can run
# this script daily and commit the log file back to a repo. Ask if you'd
# like that workflow file too.
# ---------------------------------------------------------------------------
