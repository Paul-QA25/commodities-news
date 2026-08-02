#!/usr/bin/env python3
"""
Daily news digest — focused edition.

Categories (deliberately small, curated sources only):
    1. Agri Commodities   - global grains, oilseeds, softs
    2. India Agriculture  - Indian crops, monsoon, MSP, policy
    3. Precious Metals    - gold/silver mining, supply, price drivers
    4. Bullion            - physical gold/silver trade, imports, MCX/IBJA
    5. Global Macro       - Fed, inflation, rates, currencies

Outputs (written next to this script):
    news_digest_YYYY-MM-DD.html   styled digest, one card per story
    news_digest_YYYY-MM-DD.pdf    same content as PDF (optional)
    news_feed_log.jsonl           append-only audit log
    seen_articles.json            dedup state, auto-pruned

It also emails the digest, if the SMTP environment variables below are set.
Without them the run still succeeds and just writes the files.

    SMTP_USER   full email address to send from      (required)
    SMTP_PASS   app password, NOT your login password (required)
    MAIL_TO     recipient(s), comma separated          (required)
    SMTP_HOST   default smtp.gmail.com
    SMTP_PORT   default 465 (SSL). Use 587 for STARTTLS.
    MAIL_FROM   default SMTP_USER

Gmail needs an App Password (Google Account -> Security -> 2-Step
Verification -> App passwords). A normal account password will be rejected.

Usage:
    python3 fetch_news_rss.py            # fetch, write files, email if configured
    python3 fetch_news_rss.py --check    # test every feed, print status, exit
    python3 fetch_news_rss.py --test-email  # send a test mail and exit
    python3 fetch_news_rss.py --no-email # skip sending
    python3 fetch_news_rss.py --reset    # forget history, start fresh

Dependencies:
    pip install feedparser reportlab      # reportlab optional (PDF only)
"""

import argparse
import gzip
import html
import json
import mimetypes
import os
import re
import smtplib
import ssl
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

try:
    import feedparser
except ModuleNotFoundError:
    sys.exit("feedparser is not installed. Run: pip install feedparser")


# --------------------------------------------------------------------------
# Tunables
# --------------------------------------------------------------------------
REQUEST_TIMEOUT = 20        # seconds per HTTP attempt
RETRIES = 2                 # attempts per feed before giving up
MAX_PER_FEED = 8            # newest N items considered from any single feed
CATEGORY_POOL = 8           # candidates kept per category before final ranking
DIGEST_SIZE = 15            # hard cap on stories in the finished digest
MIN_PER_CATEGORY = 1        # guarantee each category a slot before topping up
MAX_PER_PUBLISHER = 3       # no single outlet may dominate the digest
MAX_AGE_DAYS = 3            # ignore anything older than this
SEEN_RETENTION_DAYS = 45    # how long a story stays in the dedup memory

# Many publishers return 403 to Python's default user agent. Sending a normal
# browser UA is what makes the difference between "no entries returned" and a
# working feed — this was the main reason the old script came back empty.
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def gnews(query: str) -> str:
    """Build a Google News RSS URL.

    These are used only where no publisher offers a stable topic feed. They are
    not a source in themselves — every item links through to a real publisher
    (Reuters, Business Standard, Mint, Economic Times, Bloomberg, etc.), and the
    publisher name is shown on each card.
    """
    from urllib.parse import quote_plus
    return (
        f"https://news.google.com/rss/search?q={quote_plus(query)}"
        "&hl=en-IN&gl=IN&ceid=IN:en"
    )


FEEDS = {
    "Agri Commodities": {
        # Verified working, primary source: USDA's own release feeds.
        "USDA NASS Reports": "https://www.nass.usda.gov/rss/reports.xml",
        "USDA NASS News": "https://www.nass.usda.gov/rss/news.xml",
        "Grains & Oilseeds Wire": gnews(
            '("wheat prices" OR "corn prices" OR "soybean prices" OR '
            '"palm oil prices" OR "grain exports") when:2d'
        ),
    },
    "India Agriculture": {
        "BusinessLine Agri-Business":
            "https://www.thehindubusinessline.com/economy/agri-business/feeder/default.rss",
        "India Crop & Policy Wire": gnews(
            '(India) ("crop sowing" OR "kharif" OR "rabi" OR '
            '"minimum support price" OR "foodgrain output" OR '
            '"monsoon rainfall" OR "farm exports") when:2d'
        ),
    },
    "Precious Metals": {
        # Verified working.
        "Mining.com": "https://www.mining.com/feed/",
        "Gold & Silver Wire": gnews(
            '("gold price" OR "silver price" OR "precious metals" OR '
            '"gold futures" OR "bullion market") when:2d'
        ),
    },
    "Bullion": {
        "India Bullion Wire": gnews(
            '(India) ("gold imports" OR "gold demand" OR "bullion" OR '
            '"MCX gold" OR "import duty gold" OR "jewellery demand") when:2d'
        ),
    },
    "Global Macro": {
        # Verified working, primary source: the Fed's own feeds.
        "Federal Reserve Policy": "https://www.federalreserve.gov/feeds/press_monetary.xml",
        "Federal Reserve Speeches":
            "https://www.federalreserve.gov/feeds/speeches_and_testimony.xml",
        "Macro & Rates Wire": gnews(
            '("interest rates" OR "inflation data" OR "central bank" OR '
            '"dollar index" OR "Fed policy" OR "RBI policy") when:2d'
        ),
    },
}

CATEGORY_COLORS = {
    "Agri Commodities": "#65A30D",   # olive
    "India Agriculture": "#15803D",  # green
    "Precious Metals": "#B45309",    # amber
    "Bullion": "#A16207",            # dark gold
    "Global Macro": "#6D28D9",       # purple
}

# Allowlist keyed on DOMAIN, not publisher name. Name matching was too loose —
# a short key like "pti" or "mint" matches unrelated sites, and it let through
# things like drishtiias.com that aren't news outlets at all. Google News gives
# us the publisher's URL alongside the title, so we match on that instead.
#
# Tier 1 = global newswires and official bodies; tier 2 = respected national
# press and specialist commodity trade media. Tier is used as a tie-break so a
# Reuters story outranks a same-age story from a smaller outlet.
PROMINENT_DOMAINS = {
    # --- Tier 1: wires and primary sources ---
    "reuters.com": 1, "bloomberg.com": 1, "apnews.com": 1, "ft.com": 1,
    "wsj.com": 1, "economist.com": 1, "nikkei.com": 1,
    "usda.gov": 1, "federalreserve.gov": 1, "imf.org": 1, "worldbank.org": 1,
    "fao.org": 1, "rbi.org.in": 1, "pib.gov.in": 1,

    # --- Tier 2: Indian business press ---
    "business-standard.com": 2, "thehindubusinessline.com": 2,
    "thehindu.com": 2, "livemint.com": 2, "indiatimes.com": 2,
    "financialexpress.com": 2, "moneycontrol.com": 2, "indianexpress.com": 2,
    "hindustantimes.com": 2, "ndtvprofit.com": 2, "ptinews.com": 2,
    "businesstoday.in": 2, "cnbctv18.com": 2, "zeebiz.com": 2,

    # --- Tier 2: global business press ---
    "cnbc.com": 2, "bbc.com": 2, "bbc.co.uk": 2, "theguardian.com": 2,
    "aljazeera.com": 2, "marketwatch.com": 2, "barrons.com": 2,
    "fortune.com": 2, "forbes.com": 2, "scmp.com": 2, "cnn.com": 2,

    # --- Tier 2: commodity and agriculture trade press ---
    "spglobal.com": 2, "argusmedia.com": 2, "fastmarkets.com": 2,
    "kitco.com": 2, "mining.com": 2, "miningweekly.com": 2,
    "world-grain.com": 2, "agweb.com": 2, "dtnpf.com": 2,
    "agriculture.com": 2, "farmprogress.com": 2, "agrimoney.com": 2,
    "chinimandi.com": 2, "palmoilmagazine.com": 2, "world-agri.com": 2,
    "oilprice.com": 2, "agricensus.com": 2, "krishijagran.com": 2,
}


def domain_of(url: str) -> str:
    """Bare hostname, lowercased, without a www prefix."""
    if not url:
        return ""
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def domain_tier(url: str) -> int:
    """1 or 2 for an allowlisted domain, 0 otherwise. Subdomains inherit, so
    economictimes.indiatimes.com matches indiatimes.com."""
    host = domain_of(url)
    if not host:
        return 0
    for domain, tier in PROMINENT_DOMAINS.items():
        if host == domain or host.endswith("." + domain):
            return tier
    return 0


# A prominent outlet still runs lifestyle filler. These headline shapes are
# never market news, whoever published them.
OFF_TOPIC_PATTERNS = re.compile(
    r"\b(best (month|time|day|way) to|how to (grow|plant|make|start|cook)|"
    r"gardening|home garden|backyard|houseplant|indoor plant|"
    r"recipe|recipes|horoscope|zodiac|astrology|quiz|"
    r"weight loss|skin ?care|beauty tips|vastu|"
    r"here'?s what experts say|things you (should|need to) know|"
    r"tips for beginners|step[- ]by[- ]step guide)\b",
    re.IGNORECASE,
)

# Conversely, a market story almost always names a commodity, a price action,
# a policy lever, or a trade flow. Wire results must hit at least one of these.
ON_TOPIC_PATTERNS = re.compile(
    r"\b(price|prices|pricing|futures|market|markets|rally|rallies|slump|"
    r"surge|plunge|gain|gains|fall|falls|rise|rises|decline|record high|"
    r"export|exports|import|imports|tariff|duty|duties|quota|ban|levy|"
    r"output|production|yield|yields|harvest|sowing|acreage|crop|crops|"
    r"stocks|stockpile|inventory|inventories|supply|demand|shortage|surplus|"
    r"msp|procurement|subsidy|subsidies|mandi|arrivals|"
    r"inflation|interest rate|rate cut|rate hike|monetary|central bank|"
    r"fed|fomc|rbi|ecb|gdp|dollar|rupee|currency|bond|yield curve|"
    r"gold|silver|bullion|platinum|palladium|ounce|troy|hallmark\w*|"
    r"jewell\w*|etf|refiner\w*|"
    r"wheat|rice|paddy|corn|maize|soybean|soyoil|soymeal|edible oil|"
    r"palm oil|sunflower|mustard|rapeseed|canola|sugar|cane|cotton|"
    r"coffee|cocoa|tea|pulses|chana|tur|onion|potato|"
    r"crude|oil|gas|energy|freight|"
    r"monsoon|rainfall|drought|flood|el ni|la ni|weather|"
    r"mcx|ncdex|comex|cbot|ice futures|exchange|tonne|tonnes|quintal|lakh|"
    r"trade|trading|forecast|outlook|report|data|usda|wasde)\b",
    re.IGNORECASE,
)


# Indian outlets publish a "Gold Price Today in <City>" page per city per day,
# purely for search traffic. They're legitimately-sourced and full of on-topic
# words, so neither filter above catches them — they need their own test.
RATE_TABLE_PATTERNS = [
    # "Gold Price Today in Bellary", "Silver Rate Today in Anantapur"
    re.compile(r"\b(gold|silver|platinum|petrol|diesel|cng|lpg)\s+"
               r"(price|rate)s?\s+today\b", re.IGNORECASE),
    re.compile(r"\btoday'?s\s+(gold|silver|petrol|diesel)\s+(price|rate)", re.IGNORECASE),
    # Carat tables: "18K, 22K & 24K Rate"
    re.compile(r"\b(gold|silver)\b.*\b(18|22|24)\s?k\b", re.IGNORECASE),
    # "1 KG, Silver Price in Trivandrum"
    re.compile(r"\b1\s?kg\b.*\bsilver\b|\bsilver\b.*\b1\s?kg\b", re.IGNORECASE),
    re.compile(r"\bcheck\s+(the\s+)?(latest\s+)?(gold|silver)\s+(rate|price)", re.IGNORECASE),
]

# A spelled-out date inside the headline ("| 02 August 2026", "2nd August
# 2026") is the giveaway for a daily-refreshed template. Real headlines
# almost never carry one.
DATE_IN_TITLE = re.compile(
    r"\b\d{1,2}(st|nd|rd|th)?\s+"
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+20\d\d\b",
    re.IGNORECASE,
)
PRICE_WORDS = re.compile(r"\b(price|rate|gold|silver)\b", re.IGNORECASE)


def looks_like_rate_table(title: str) -> bool:
    if any(pattern.search(title) for pattern in RATE_TABLE_PATTERNS):
        return True
    # Date-stamped price page: "... Silver Price in Trivandrum | 02 August 2026"
    return bool(DATE_IN_TITLE.search(title) and PRICE_WORDS.search(title))


def topic_verdict(title: str, strict: bool) -> str | None:
    """Return a rejection reason, or None if the headline should be kept.
    `strict` requires a positive market signal, used for wire results."""
    if looks_like_rate_table(title):
        return "daily rate table"
    if OFF_TOPIC_PATTERNS.search(title):
        return "off-topic"
    if strict and not ON_TOPIC_PATTERNS.search(title):
        return "no market signal"
    return None


try:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
except NameError:                     # notebooks have no __file__
    BASE_DIR = os.getcwd()

SEEN_FILE = os.path.join(BASE_DIR, "seen_articles.json")
LOG_FILE = os.path.join(BASE_DIR, "news_feed_log.jsonl")


# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------
def clean_html(raw: str) -> str:
    """Strip tags, decode entities, collapse whitespace."""
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def two_liner(text: str, max_chars: int = 200) -> str:
    """Trim a summary to roughly two sentences."""
    if not text:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    result = " ".join(sentences[:2]).strip()
    if len(result) > max_chars:
        result = result[:max_chars].rsplit(" ", 1)[0].rstrip(",.;:") + "…"
    return result


def normalise_title(title: str) -> str:
    """Key used to spot the same story arriving from two different feeds."""
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def entry_datetime(entry) -> datetime | None:
    """Best-effort publication time as an aware UTC datetime."""
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                return datetime.fromtimestamp(time.mktime(parsed), tz=timezone.utc)
            except (TypeError, ValueError, OverflowError):
                continue
    return None


# --------------------------------------------------------------------------
# Dedup state
# --------------------------------------------------------------------------
def load_seen() -> dict:
    """Return {guid: iso_timestamp}. Tolerates the old list format and
    a corrupted file — neither should abort the run."""
    if not os.path.exists(SEEN_FILE):
        return {}
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"Warning: could not read {SEEN_FILE} ({exc}); starting fresh.",
              file=sys.stderr)
        return {}

    now_iso = datetime.now(timezone.utc).isoformat()
    if isinstance(data, list):                      # legacy format
        return {guid: now_iso for guid in data}
    if isinstance(data, dict):
        return data
    return {}


def save_seen(seen: dict) -> None:
    """Prune old entries so the file can't grow without bound, then write."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=SEEN_RETENTION_DAYS)
    pruned = {}
    for guid, stamp in seen.items():
        try:
            if datetime.fromisoformat(stamp) >= cutoff:
                pruned[guid] = stamp
        except (TypeError, ValueError):
            pruned[guid] = datetime.now(timezone.utc).isoformat()
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(pruned, f, indent=2)


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------
def download(url: str) -> bytes:
    """Fetch a feed with a browser user agent, timeout and one retry."""
    last_error = None
    for attempt in range(1, RETRIES + 1):
        try:
            with urlopen(Request(url, headers=HTTP_HEADERS),
                         timeout=REQUEST_TIMEOUT) as response:
                raw = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw
        except (HTTPError, URLError, OSError, gzip.BadGzipFile) as exc:
            last_error = exc
            if attempt < RETRIES:
                time.sleep(2)
    raise RuntimeError(last_error)


def parse_feed(url: str):
    """Download and parse. Returns (entries, error_string)."""
    try:
        raw = download(url)
    except RuntimeError as exc:
        return [], f"download failed: {exc}"

    parsed = feedparser.parse(raw)
    if not parsed.entries:
        reason = getattr(parsed, "bozo_exception", None)
        return [], f"no entries{f' ({reason})' if reason else ''}"
    return parsed.entries, None


def source_and_summary(entry, feed_name: str, title: str):
    """Google News wraps items from other publishers, and its summaries are
    just link markup. Credit the real publisher and drop the noise.
    Returns (publisher_name, summary, title, publisher_url_or_None)."""
    publisher = publisher_url = None
    source = entry.get("source")
    if isinstance(source, dict):
        publisher = source.get("title")
        # feedparser exposes <source url="..."> as href.
        publisher_url = source.get("href") or source.get("url")

    if publisher:
        # Google News appends " - Publisher" to every headline; remove it.
        if title.endswith(f" - {publisher}"):
            title = title[: -len(f" - {publisher}")].strip()
        # A publisher_url of "" (not None) still marks this as a wire result,
        # so a missing URL fails the allowlist rather than bypassing it.
        return publisher, "", title, (publisher_url or "")

    summary = two_liner(clean_html(entry.get("summary", "")))
    # Some feeds echo the headline as the summary; that adds nothing.
    if summary and normalise_title(summary).startswith(normalise_title(title)[:60]):
        summary = ""
    # Hand-picked publisher feed: trusted because it's in FEEDS at all.
    return feed_name, summary, title, None


def collect_candidates(seen: dict) -> list:
    """Gather everything new and worth considering. Ranking and the final cut
    happen separately in select_top(), so nothing is marked as seen here."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=MAX_AGE_DAYS)
    seen_titles = set()
    candidates = []

    for category, feeds in FEEDS.items():
        pool = []

        for feed_name, url in feeds.items():
            entries, error = parse_feed(url)
            if error:
                print(f"  [skip] {category} / {feed_name}: {error}", file=sys.stderr)
                continue

            taken = 0
            rejects = Counter()
            for entry in entries:
                if taken >= MAX_PER_FEED:
                    break

                guid = entry.get("id") or entry.get("link")
                if not guid or guid in seen:
                    continue

                published = entry_datetime(entry)
                if published and published < cutoff:
                    continue

                title = clean_html(entry.get("title", ""))
                if not title:
                    continue

                title_key = normalise_title(title)
                if title_key in seen_titles:      # same story, another feed
                    continue

                publisher, summary, title, publisher_url = source_and_summary(
                    entry, feed_name, title)
                via_wire = publisher_url is not None

                if via_wire:
                    # Wire result: the publisher's own domain must be listed.
                    tier = domain_tier(publisher_url)
                    if tier == 0:
                        rejects[f"not listed: {domain_of(publisher_url) or publisher}"] += 1
                        continue
                else:
                    # Hand-picked feed, trusted by virtue of being in FEEDS.
                    tier = domain_tier(entry.get("link", "")) or 2

                # Prominent outlets still run lifestyle filler; wire results
                # additionally have to look like market news.
                reason = topic_verdict(title, strict=via_wire)
                if reason:
                    rejects[reason] += 1
                    continue

                seen_titles.add(title_key)
                pool.append({
                    "category": category,
                    "source": publisher,
                    "title": title,
                    "link": entry.get("link", ""),
                    "published": entry.get("published", ""),
                    "published_iso": published.isoformat() if published else "",
                    "summary": summary,
                    "guid": guid,
                    "tier": tier,
                })
                taken += 1

            note = ""
            if rejects:
                detail = ", ".join(f"{n}x {why}" for why, n in rejects.most_common(3))
                note = f" ({sum(rejects.values())} rejected — {detail})"
            print(f"  [ok]   {category} / {feed_name}: {taken} kept{note}")

        pool.sort(key=story_rank)
        candidates.extend(pool[:CATEGORY_POOL])

    return candidates


def story_rank(item: dict) -> tuple:
    """Sort key: better sources first, then newest first."""
    published = item.get("published_iso") or ""
    return (item["tier"], _invert(published))


def _invert(iso: str) -> str:
    """Make a string sort descending — newest timestamps come first."""
    return "".join(chr(0x10FFFF - ord(c)) if ord(c) < 0x10FFFF else c
                   for c in iso) if iso else "\uffff"


def select_top(candidates: list, limit: int) -> list:
    """Cut the pool down to `limit` stories, spread across categories.

    Each category gets MIN_PER_CATEGORY slots first so a busy day in Global
    Macro can't crowd out Bullion entirely; remaining slots go to the
    best-ranked stories regardless of category.
    """
    by_category = {}
    for item in candidates:
        by_category.setdefault(item["category"], []).append(item)
    for pool in by_category.values():
        pool.sort(key=story_rank)

    chosen, chosen_ids = [], set()
    counts = {category: 0 for category in by_category}
    by_publisher = Counter()

    # Pass 1: guaranteed slots, in the order categories appear in FEEDS.
    for category in FEEDS:
        for item in by_category.get(category, [])[:MIN_PER_CATEGORY]:
            if len(chosen) < limit:
                chosen.append(item)
                chosen_ids.add(item["guid"])
                counts[item["category"]] += 1
                by_publisher[item["source"]] += 1

    # Pass 2: best-ranked stories overall, but no category may take more than
    # its share — one prolific feed shouldn't eat half the digest.
    ceiling = max(MIN_PER_CATEGORY, round(limit / max(len(FEEDS), 1)) + 1)
    for item in sorted(candidates, key=story_rank):
        if len(chosen) >= limit:
            break
        if (item["guid"] in chosen_ids
                or counts[item["category"]] >= ceiling
                or by_publisher[item["source"]] >= MAX_PER_PUBLISHER):
            continue
        chosen.append(item)
        chosen_ids.add(item["guid"])
        counts[item["category"]] += 1
        by_publisher[item["source"]] += 1

    # Pass 3: if quiet categories left slots unused, relax the ceiling rather
    # than ship a short digest.
    for item in sorted(candidates, key=story_rank):
        if len(chosen) >= limit:
            break
        if item["guid"] not in chosen_ids:
            chosen.append(item)
            chosen_ids.add(item["guid"])

    # Present in category order rather than rank order, so the digest reads
    # like a newspaper instead of a leaderboard.
    order = list(FEEDS)
    chosen.sort(key=lambda i: (order.index(i["category"])
                               if i["category"] in order else 99,
                               story_rank(i)))
    return chosen


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------
def log_articles(items) -> None:
    stamp = datetime.now(timezone.utc).isoformat()
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        for item in items:
            record = dict(item, fetched_at=stamp)
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def print_digest(items) -> None:
    header = f"{len(items)} new article(s) — {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}"
    print(f"\n{header}\n" + "=" * 70)
    current = None
    for item in items:
        if item["category"] != current:
            current = item["category"]
            print(f"\n--- {current} ---")
        print(f"\n  {item['title']}")
        if item["summary"]:
            print(f"    {item['summary']}")
        print(f"    [{item['source']}]")
    print("\n" + "=" * 70)


def group_by_category(items) -> dict:
    grouped = {category: [] for category in FEEDS}
    for item in items:
        grouped.setdefault(item.get("category", "Other"), []).append(item)
    return grouped


def write_html_digest(items) -> str:
    """Colour-coded HTML digest. Always written, even on an empty day, so the
    downstream email/upload step always finds a file."""
    today = datetime.now(timezone.utc)
    path = os.path.join(BASE_DIR, f"news_digest_{today:%Y-%m-%d}.html")

    sections = []
    for category, cat_items in group_by_category(items).items():
        if not cat_items:
            continue
        color = CATEGORY_COLORS.get(category, "#374151")
        cards = []
        for item in cat_items:
            title = html.escape(item["title"])
            summary = html.escape(item["summary"])
            source = html.escape(item["source"])
            link = html.escape(item["link"], quote=True)

            read_more = (
                f' <a href="{link}" style="color:{color};text-decoration:underline;'
                f'font-weight:600;white-space:nowrap;">(Read more)</a>' if link else ""
            )
            body = (
                f'<div style="font-size:13px;color:#4b5563;line-height:1.5;'
                f'margin-bottom:6px;">{summary}{read_more}</div>'
                if (summary or link) else ""
            )
            cards.append(
                f'<div style="background:#fff;border:1px solid #e5e7eb;'
                f'border-left:4px solid {color};border-radius:6px;'
                f'padding:14px 16px;margin-bottom:10px;">'
                f'<div style="font-size:15px;font-weight:600;color:#111827;'
                f'line-height:1.4;margin-bottom:4px;">{title}</div>{body}'
                f'<div style="font-size:11px;color:#9ca3af;text-transform:uppercase;'
                f'letter-spacing:0.03em;">{source}</div></div>'
            )

        sections.append(
            f'<div style="margin-bottom:26px;">'
            f'<div style="background:{color};color:#fff;font-size:14px;font-weight:700;'
            f'padding:8px 14px;border-radius:6px 6px 0 0;">'
            f'{html.escape(category)} &nbsp;({len(cat_items)})</div>'
            f'<div style="border:1px solid {color}22;border-top:none;padding:12px;'
            f'background:#f9fafb;border-radius:0 0 6px 6px;">{"".join(cards)}</div></div>'
        )

    body_content = "".join(sections) or (
        '<div style="text-align:center;color:#6b7280;padding:40px 0;font-size:14px;">'
        'No new articles since the last run.</div>'
    )

    with open(path, "w", encoding="utf-8") as f:
        f.write(f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f3f4f6;
font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
  <div style="max-width:640px;margin:0 auto;padding:20px;">
    <div style="text-align:center;margin-bottom:24px;">
      <div style="font-size:20px;font-weight:800;color:#111827;">Daily Market Digest</div>
      <div style="font-size:13px;color:#6b7280;margin-top:2px;">{today:%B %d, %Y}</div>
    </div>
    {body_content}
    <div style="text-align:center;color:#9ca3af;font-size:11px;margin-top:20px;">
      Automated digest &middot; {len(items)} item(s) today
    </div>
  </div>
</body></html>""")
    return path


def write_pdf_digest(items) -> str:
    """PDF mirror of the HTML digest. Requires reportlab; caller treats any
    failure here as non-fatal."""
    from reportlab.lib import colors as rl_colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer,
                                    Table, TableStyle)

    today = datetime.now(timezone.utc)
    path = os.path.join(BASE_DIR, f"news_digest_{today:%Y-%m-%d}.pdf")
    styles = getSampleStyleSheet()

    def style(name, **kwargs):
        return ParagraphStyle(name, parent=styles["Normal"], **kwargs)

    title_style = ParagraphStyle("T", parent=styles["Title"], fontSize=20,
                                 alignment=TA_CENTER,
                                 textColor=rl_colors.HexColor("#111827"))
    date_style = style("D", fontSize=10, alignment=TA_CENTER, spaceAfter=18,
                       textColor=rl_colors.HexColor("#6b7280"))
    header_style = style("H", fontSize=12, fontName="Helvetica-Bold",
                         textColor=rl_colors.white)
    headline_style = style("HL", fontSize=11, leading=14, spaceAfter=2,
                           fontName="Helvetica-Bold",
                           textColor=rl_colors.HexColor("#111827"))
    summary_style = style("S", fontSize=9.5, leading=13, spaceAfter=3,
                          textColor=rl_colors.HexColor("#4b5563"))
    source_style = style("Src", fontSize=7.5, spaceAfter=12,
                         textColor=rl_colors.HexColor("#9ca3af"))
    footer_style = style("F", fontSize=8, alignment=TA_CENTER, spaceBefore=16,
                         textColor=rl_colors.HexColor("#9ca3af"))

    story = [Paragraph("Daily Market Digest", title_style),
             Paragraph(f"{today:%B %d, %Y}", date_style)]

    for category, cat_items in group_by_category(items).items():
        if not cat_items:
            continue
        color_hex = CATEGORY_COLORS.get(category, "#374151")
        header = Table(
            [[Paragraph(f"{html.escape(category)}&nbsp;&nbsp;({len(cat_items)})",
                        header_style)]],
            colWidths=[6.9 * inch],
        )
        header.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), rl_colors.HexColor(color_hex)),
            ("LEFTPADDING", (0, 0), (-1, -1), 10),
            ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        story += [header, Spacer(1, 6)]

        for item in cat_items:
            link = html.escape(item["link"], quote=True)
            read_more = (f'<link href="{link}" color="{color_hex}">'
                         f'<b>(Read more)</b></link>' if link else "")
            line = " ".join(x for x in (html.escape(item["summary"]), read_more) if x)
            story.append(Paragraph(html.escape(item["title"]), headline_style))
            if line:
                story.append(Paragraph(line, summary_style))
            story.append(Paragraph(html.escape(item["source"]).upper(), source_style))
        story.append(Spacer(1, 10))

    if len(story) == 2:
        story.append(Paragraph("No new articles since the last run.", styles["Normal"]))
    story.append(Paragraph(f"Automated digest &middot; {len(items)} item(s) today",
                           footer_style))

    SimpleDocTemplate(path, pagesize=letter, topMargin=0.6 * inch,
                      bottomMargin=0.6 * inch, leftMargin=0.6 * inch,
                      rightMargin=0.6 * inch).build(story)
    return path


# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------
def header_safe(value: str) -> str:
    """Strip CR/LF from anything going into a mail header.

    Secrets pasted into GitHub frequently carry stray newlines, and the email
    module raises ValueError rather than cleaning them up. Header injection is
    the reason it's strict, so we remove them rather than pass them through.
    """
    return re.sub(r"[\r\n]+", " ", value).strip()


def split_recipients(raw: str) -> list[str]:
    """Accept addresses separated by commas, semicolons, or line breaks."""
    candidates = [part.strip(" \t\"'<>") for part in re.split(r"[,;\s]+", raw)]
    seen, result = set(), []
    for address in candidates:
        if "@" in address and address not in seen:
            seen.add(address)
            result.append(address)
    return result


def smtp_config() -> tuple[dict | None, str | None]:
    """Read SMTP settings from the environment.
    Returns (config, reason_it_is_unusable)."""
    user = header_safe(os.environ.get("SMTP_USER", ""))
    password = os.environ.get("SMTP_PASS", "").strip()
    raw_recipients = os.environ.get("MAIL_TO", "")
    recipients = split_recipients(raw_recipients)

    missing = [name for name, value in
               (("SMTP_USER", user), ("SMTP_PASS", password), ("MAIL_TO", raw_recipients.strip()))
               if not value]
    if missing:
        return None, f"not configured (missing {', '.join(missing)})"

    if not recipients:
        return None, f"MAIL_TO has no usable address in {raw_recipients.strip()!r}"

    # GitHub Actions substitutes an empty string for an undefined `vars.X`, so
    # `os.environ.get(name, default)` would hand back "" instead of the default.
    host = os.environ.get("SMTP_HOST", "").strip() or "smtp.gmail.com"
    port_raw = os.environ.get("SMTP_PORT", "").strip() or "465"
    try:
        port = int(port_raw)
    except ValueError:
        return None, f"SMTP_PORT is not a number: {port_raw!r}"

    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "sender": header_safe(os.environ.get("MAIL_FROM", "")) or user,
        "recipients": recipients,
    }, None


def build_message(cfg: dict, subject: str, html_body: str, attachments=()) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = header_safe(subject)
    message["From"] = header_safe(cfg["sender"])
    message["To"] = header_safe(", ".join(cfg["recipients"]))

    # Plain-text fallback first, then the HTML alternative. Clients that can't
    # render HTML (and most spam filters) want to see both.
    message.set_content(
        "This digest is formatted in HTML. If you are seeing this line, your "
        "mail client could not display it — the PDF attachment has the same "
        "content."
    )
    message.add_alternative(html_body, subtype="html")

    for path in attachments:
        if not path or not os.path.exists(path):
            continue
        guessed, _ = mimetypes.guess_type(path)
        maintype, _, subtype = (guessed or "application/octet-stream").partition("/")
        with open(path, "rb") as f:
            message.add_attachment(f.read(), maintype=maintype, subtype=subtype,
                                   filename=os.path.basename(path))
    return message


def deliver(cfg: dict, message: EmailMessage) -> None:
    """Open a connection and send. Port 465 is implicit SSL, everything else
    is treated as STARTTLS (587)."""
    context = ssl.create_default_context()
    if cfg["port"] == 465:
        server = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=30, context=context)
    else:
        server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=30)
    with server:
        if cfg["port"] != 465:
            server.starttls(context=context)
        server.login(cfg["user"], cfg["password"])
        server.send_message(message)


def send_digest_email(html_path: str, pdf_path: str | None, item_count: int) -> bool:
    """Email the digest. Returns True on success, False if it was skipped or
    failed; the caller decides whether that matters."""
    cfg, reason = smtp_config()
    if cfg is None:
        print(f"Email skipped: {reason}.", file=sys.stderr)
        return False

    with open(html_path, "r", encoding="utf-8") as f:
        body = f.read()

    today = datetime.now(timezone.utc)
    subject = (f"Daily Market Digest — {today:%d %b %Y} ({item_count} item"
               f"{'s' if item_count != 1 else ''})")

    try:
        deliver(cfg, build_message(cfg, subject, body, [pdf_path]))
    except smtplib.SMTPAuthenticationError:
        print("Email FAILED: the server rejected the login. For Gmail you need "
              "an App Password, not your account password.", file=sys.stderr)
        return False
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        print(f"Email FAILED: {exc}", file=sys.stderr)
        return False

    print(f"Email sent to {', '.join(cfg['recipients'])}")
    return True


def send_test_email() -> int:
    cfg, reason = smtp_config()
    if cfg is None:
        print(f"Cannot send test email: {reason}.", file=sys.stderr)
        return 1
    print(f"Sending test mail via {cfg['host']}:{cfg['port']} as {cfg['user']}…")
    message = build_message(
        cfg, "Digest test email",
        "<p>If you are reading this, SMTP is configured correctly.</p>",
    )
    try:
        deliver(cfg, message)
    except Exception as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    print(f"Sent to {', '.join(cfg['recipients'])}. Check spam if it doesn't arrive.")
    return 0


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def check_feeds() -> int:
    """Test every configured feed and report which ones work."""
    failures = 0
    for category, feeds in FEEDS.items():
        print(f"\n{category}")
        for name, url in feeds.items():
            entries, error = parse_feed(url)
            if error:
                failures += 1
                print(f"  FAIL  {name:<28} {error}")
            else:
                print(f"  OK    {name:<28} {len(entries)} entries")
    print(f"\n{failures} feed(s) failing.")
    return failures


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the daily news digest.")
    parser.add_argument("--check", action="store_true",
                        help="test all feeds and exit")
    parser.add_argument("--test-email", action="store_true",
                        help="send a test email and exit")
    parser.add_argument("--no-email", action="store_true",
                        help="build the digest but don't send it")
    parser.add_argument("--reset", action="store_true",
                        help="clear dedup history before running")
    parser.add_argument("--limit", type=int, default=DIGEST_SIZE,
                        metavar="N", help=f"stories in the digest (default {DIGEST_SIZE})")
    args = parser.parse_args()

    if args.check:
        check_feeds()
        return

    if args.test_email:
        sys.exit(send_test_email())

    if args.reset and os.path.exists(SEEN_FILE):
        os.remove(SEEN_FILE)
        print("Dedup history cleared.")

    print(f"Fetching {sum(len(f) for f in FEEDS.values())} feeds…")
    seen = load_seen()
    candidates = collect_candidates(seen)
    items = select_top(candidates, args.limit)

    # Only what actually ships is remembered. A story that just misses the cut
    # on a busy day stays eligible tomorrow instead of vanishing unread.
    stamp = datetime.now(timezone.utc).isoformat()
    for item in items:
        seen[item["guid"]] = stamp
    save_seen(seen)

    print(f"\nSelected {len(items)} of {len(candidates)} candidate stories.")

    if items:
        print_digest(items)
        log_articles(items)
    else:
        print("\nNo new articles since the last run.")

    html_path = write_html_digest(items)
    print(f"\nHTML digest: {html_path}")

    pdf_path = None
    try:
        pdf_path = write_pdf_digest(items)
        print(f"PDF digest:  {pdf_path}")
    except ModuleNotFoundError as exc:
        print(f"PDF skipped (missing dependency): {exc}", file=sys.stderr)
    except Exception as exc:
        print(f"PDF failed, HTML still written: {exc}", file=sys.stderr)

    if args.no_email:
        return

    configured = smtp_config()[0] is not None
    if not send_digest_email(html_path, pdf_path, len(items)) and configured:
        # Credentials were supplied but delivery failed — that should surface
        # as a red run in Actions rather than passing silently.
        sys.exit(1)


if __name__ == "__main__":
    main()
