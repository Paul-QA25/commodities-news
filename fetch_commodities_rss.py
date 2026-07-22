#!/usr/bin/env python3
"""
Daily fetcher for RSS feeds across 7 categories: Commodities, World News,
India News, Agriculture, Energy, Weather & Climate, Financial Markets.
(See the FEEDS dictionary below for the full source list per category.)

What it does each run:
  1. Downloads all configured feeds.
  2. Compares entries against a local "seen" list (seen_articles.json)
     so already-processed items aren't reported again.
  3. Logs new items to commodities_feed_log.jsonl (one JSON object per
     line, includes links internally for audit purposes).
  4. Writes a styled HTML digest (news_digest_YYYY-MM-DD.html): color-coded
     sections by category, each item shown as headline + two-line summary
     only (no links in the digest itself).

To add more feeds later, add a "Name": "url" entry inside the relevant
category dict in FEEDS below. To add a whole new category, add a new
top-level key to FEEDS plus a matching entry in CATEGORY_COLORS.

Run manually:
    python3 fetch_commodities_rss.py

Automate it (see notes at the bottom of this file for cron / Task
Scheduler / systemd examples).
"""

import html
import json
import os
import re
import socket
import sys
from datetime import datetime, timezone

import feedparser

# With many feeds configured, one slow/unresponsive server could otherwise
# stall the whole run indefinitely. 15s is generous for a news RSS file.
socket.setdefaulttimeout(15)


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


def two_liner(text: str, max_chars: int = 200) -> str:
    """Reduce a summary to roughly two sentences / two lines worth of text."""
    if not text:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    result = " ".join(sentences[:2]).strip()
    if len(result) > max_chars:
        result = result[:max_chars].rsplit(" ", 1)[0].rstrip(",.;:") + "…"
    return result

FEEDS = {
    "Commodities": {
        "BusinessLine Commodities": "https://www.thehindubusinessline.com/markets/commodities/feeder/default.rss",
        "OilPrice.com": "https://oilprice.com/rss/main",
        "Nasdaq Commodities": "https://www.nasdaq.com/feed/rssoutbound?category=Commodities",
        "MarketWatch Top Stories": "https://feeds.marketwatch.com/marketwatch/topstories/",
        "Investing.com": "https://www.investing.com/rss/news.rss",
        "FXStreet": "https://www.fxstreet.com/rss/news",
        "Mining.com": "https://www.mining.com/feed/",
        "Business Standard Commodities": "https://www.business-standard.com/rss/markets-commodities-106.rss",
        "Financial Express Commodities": "https://www.financialexpress.com/market/commodities/feed/",
        "Moneycontrol Top News": "https://www.moneycontrol.com/rss/MCtopnews.xml",
    },
    "World News": {
        "BBC News World": "https://feeds.bbci.co.uk/news/world/rss.xml",
        "Al Jazeera": "https://www.aljazeera.com/xml/rss/all.xml",
    },
    "India News": {
        "NDTV Top Stories": "https://feeds.feedburner.com/ndtvnews-top-stories",
        "The Hindu National": "https://www.thehindu.com/news/national/?service=rss",
        "Times of India Top Stories": "https://timesofindia.indiatimes.com/rssfeedstopstories.cms",
        "PIB (Press Information Bureau)": "https://www.pib.gov.in/ViewRss.aspx?reg=1&lang=1",
    },
    "Agriculture": {
        "USDA News": "https://www.usda.gov/rss/latest.xml",
        "USDA NASS News": "http://www.nass.usda.gov/rss/news.xml",
        "USDA NASS Reports (WASDE/Crop Progress)": "http://www.nass.usda.gov/rss/reports.xml",
        "IFPRI": "https://www.ifpri.org/rss.xml",
        "CGIAR": "https://www.cgiar.org/feed/",
        "Farm Progress": "https://www.farmprogress.com/rss.xml",
        "AgWeb": "https://www.agweb.com/rss.xml",
        "DTN Progressive Farmer": "https://www.dtnpf.com/agriculture/rss",
        "Successful Farming": "https://www.agriculture.com/rss.xml",
    },
    # Commodity-specific trade press: Grains, Oilseeds, Sugar, Soy/Sunflower
    # oil, Palm Oil, Cocoa, Coffee (World & India). Tea intentionally
    # omitted - no confirmed working feed found; add one if you find it.
    "Agri Commodities": {
        "World Grain - Wheat": "https://www.world-grain.com/rss/topic/1351-wheat",
        "World Grain - Oilseeds": "https://www.world-grain.com/rss/topic/1344-oilseeds",
        "World Grain - Soybean": "https://www.world-grain.com/rss/topic/1350-soybean",
        "World Grain - Sunflower Seed": "https://www.world-grain.com/rss/topic/1923-sunflower-seed",
        "BusinessLine Agribusiness": "https://www.thehindubusinessline.com/economy/agri-business/feeder/default.rss",
        "ChiniMandi (Sugar)": "https://www.chinimandi.com/all-news/feed",
        "Palm Oil Magazine": "https://www.palmoilmagazine.com/feed/",
        "ConfectioneryNews (Cocoa)": "https://www.confectionerynews.com/arc/outboundfeeds/rss/",
        "Daily Coffee News": "https://dailycoffeenews.com/feed",
    },
    "Energy": {
        "EIA Today in Energy": "https://www.eia.gov/rss/todayinenergy.xml",
        "EIA What's New": "https://www.eia.gov/rss/whatsnew.xml",
        "EIA Petroleum": "https://www.eia.gov/rss/petroleum.xml",
    },
    "Weather & Climate": {
        "NOAA News": "https://www.noaa.gov/rss.xml",
        "NOAA Climate.gov": "https://www.climate.gov/feed.xml",
        "CPC (ENSO/Drought)": "https://www.cpc.ncep.noaa.gov/products/rss.xml",
        "NASA Earth Observatory": "https://earthobservatory.nasa.gov/feeds/image-of-the-day.rss",
        "Copernicus Climate": "https://climate.copernicus.eu/rss.xml",
    },
    "Financial Markets": {
        "Bloomberg Markets": "https://feeds.bloomberg.com/markets/news.rss",
        "Bloomberg Economics": "https://feeds.bloomberg.com/economics/news.rss",
        "Bloomberg Industries": "https://feeds.bloomberg.com/industries/news.rss",
    },
}

# Accent color per category, used for the HTML digest section headers.
CATEGORY_COLORS = {
    "Commodities": "#B45309",       # amber
    "World News": "#1D4ED8",        # blue
    "India News": "#15803D",        # green
    "Agriculture": "#65A30D",       # olive green
    "Agri Commodities": "#A16207",  # dark gold
    "Energy": "#C2410C",            # burnt orange
    "Weather & Climate": "#0284C7", # sky blue
    "Financial Markets": "#6D28D9", # purple
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

    for category, feeds_in_category in FEEDS.items():
        for source_name, feed_url in feeds_in_category.items():
            try:
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
                            "category": category,
                            "source": source_name,
                            "title": clean_html(entry.get("title", "")),
                            "link": entry.get("link", ""),  # kept for internal dedup/log only, not displayed
                            "published": entry.get("published", ""),
                            "summary": two_liner(clean_html(entry.get("summary", ""))),
                            "guid": guid,
                        })
                        seen.add(guid)
            except Exception as exc:
                # One misbehaving feed (malformed response, unexpected data
                # shape, connection error not caught by feedparser itself,
                # etc.) must never take down the whole run. Log it and move on.
                print(f"ERROR: '{source_name}' failed unexpectedly and was skipped: {exc}", file=sys.stderr)
                continue

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
        print(f"\n[{i}] ({item.get('category', '')} / {item.get('source', 'Unknown')}) {item['title']}")
        if item.get("summary"):
            print(f"    {item['summary']}")
    print("\n" + "=" * 70)


def write_html_digest(items) -> str:
    """Write a styled HTML digest, grouped into color-coded sections by
    category, showing headline + two-line summary, each followed by an
    inline "(Click to Read More)" link to the original article.
    Always writes a file for today, even with no new items, so the
    email step has something consistent to find.
    Returns the path to the file written."""
    today_display = datetime.now(timezone.utc).strftime("%B %d, %Y")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    html_path = os.path.join(BASE_DIR, f"news_digest_{today}.html")

    # Group items by category, preserving the category order defined in FEEDS.
    by_category = {cat: [] for cat in FEEDS.keys()}
    for item in items:
        by_category.setdefault(item.get("category", "Other"), []).append(item)

    sections_html = []
    for category, cat_items in by_category.items():
        if not cat_items:
            continue
        color = CATEGORY_COLORS.get(category, "#374151")
        cards = []
        for item in cat_items:
            title = html.escape(item["title"])
            summary = html.escape(item.get("summary", ""))
            source = html.escape(item.get("source", ""))
            link = html.escape(item.get("link", ""), quote=True)

            read_more = (
                f' <a href="{link}" style="color:{color};text-decoration:underline;'
                f'font-weight:600;white-space:nowrap;">(Click to Read More)</a>'
                if link else ""
            )

            if summary:
                summary_html = (
                    f'<div style="font-size:13px;color:#4b5563;line-height:1.5;margin-bottom:6px;">'
                    f'{summary}{read_more}</div>'
                )
            elif link:
                # No summary available - still offer the link on its own line.
                summary_html = (
                    f'<div style="font-size:13px;line-height:1.5;margin-bottom:6px;">{read_more.strip()}</div>'
                )
            else:
                summary_html = ""

            cards.append(
                f'<div style="background:#ffffff;border:1px solid #e5e7eb;border-left:4px solid {color};'
                f'border-radius:6px;padding:14px 16px;margin-bottom:10px;">'
                f'<div style="font-size:15px;font-weight:600;color:#111827;line-height:1.4;margin-bottom:4px;">{title}</div>'
                f'{summary_html}'
                f'<div style="font-size:11px;color:#9ca3af;text-transform:uppercase;letter-spacing:0.03em;">{source}</div>'
                f'</div>'
            )
        sections_html.append(
            f'<div style="margin-bottom:26px;">'
            f'<div style="background:{color};color:#ffffff;font-size:14px;font-weight:700;'
            f'padding:8px 14px;border-radius:6px 6px 0 0;letter-spacing:0.02em;">'
            f'{html.escape(category)} &nbsp;({len(cat_items)})</div>'
            f'<div style="border:1px solid {color}22;border-top:none;padding:12px;background:#f9fafb;'
            f'border-radius:0 0 6px 6px;">{"".join(cards)}</div>'
            f'</div>'
        )

    body_content = "".join(sections_html) if sections_html else (
        '<div style="text-align:center;color:#6b7280;padding:40px 0;font-size:14px;">'
        'No new articles since yesterday.</div>'
    )

    full_html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
  <div style="max-width:640px;margin:0 auto;padding:20px;">
    <div style="text-align:center;margin-bottom:24px;">
      <div style="font-size:20px;font-weight:800;color:#111827;">Daily News Digest</div>
      <div style="font-size:13px;color:#6b7280;margin-top:2px;">{today_display}</div>
    </div>
    {body_content}
    <div style="text-align:center;color:#9ca3af;font-size:11px;margin-top:20px;">
      Automated digest &middot; {len(items)} new item(s) today
    </div>
  </div>
</body>
</html>"""

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(full_html)

    return html_path


def main():
    new_items = fetch_new_articles()
    if new_items:
        print_digest(new_items)
        log_articles(new_items)
    else:
        print("No new articles since last run.")
    html_path = write_html_digest(new_items)
    print(f"\nHTML digest saved to: {html_path}")


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
