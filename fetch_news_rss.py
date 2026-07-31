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
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from urllib.error import HTTPError, URLError
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
MAX_PER_FEED = 6            # newest N items taken from any single feed
MAX_PER_CATEGORY = 12       # hard cap per category in the digest
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
            "wheat OR corn OR soybean OR palm oil prices market when:2d"
        ),
    },
    "India Agriculture": {
        "BusinessLine Agri-Business":
            "https://www.thehindubusinessline.com/economy/agri-business/feeder/default.rss",
        "India Crop & Policy Wire": gnews(
            "India agriculture monsoon OR sowing OR MSP OR foodgrain when:2d"
        ),
    },
    "Precious Metals": {
        # Verified working.
        "Mining.com": "https://www.mining.com/feed/",
        "Gold & Silver Wire": gnews(
            "gold price OR silver price OR precious metals when:2d"
        ),
    },
    "Bullion": {
        "India Bullion Wire": gnews(
            "India gold imports OR bullion OR MCX gold OR jewellers demand when:2d"
        ),
    },
    "Global Macro": {
        # Verified working, primary source: the Fed's own feeds.
        "Fed Monetary Policy": "https://www.federalreserve.gov/feeds/press_monetary.xml",
        "Fed Speeches & Testimony":
            "https://www.federalreserve.gov/feeds/speeches_and_testimony.xml",
        "Macro & Rates Wire": gnews(
            "inflation OR central bank OR interest rates OR dollar index when:2d"
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
    just link markup. Credit the real publisher and drop the noise."""
    publisher = None
    source = entry.get("source")
    if isinstance(source, dict):
        publisher = source.get("title")

    if publisher:
        # Google News appends " - Publisher" to every headline; remove it.
        if title.endswith(f" - {publisher}"):
            title = title[: -len(f" - {publisher}")].strip()
        return publisher, "", title

    summary = two_liner(clean_html(entry.get("summary", "")))
    # Some feeds echo the headline as the summary; that adds nothing.
    if summary and normalise_title(summary).startswith(normalise_title(title)[:60]):
        summary = ""
    return feed_name, summary, title


def fetch_new_articles() -> list:
    seen = load_seen()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=MAX_AGE_DAYS)
    seen_titles = set()
    collected = []

    for category, feeds in FEEDS.items():
        category_items = []

        for feed_name, url in feeds.items():
            entries, error = parse_feed(url)
            if error:
                print(f"  [skip] {category} / {feed_name}: {error}", file=sys.stderr)
                continue

            taken = 0
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
                    seen.setdefault(guid, now.isoformat())
                    continue

                publisher, summary, title = source_and_summary(entry, feed_name, title)

                seen[guid] = now.isoformat()
                seen_titles.add(title_key)
                category_items.append({
                    "category": category,
                    "source": publisher,
                    "title": title,
                    "link": entry.get("link", ""),
                    "published": entry.get("published", ""),
                    "summary": summary,
                    "guid": guid,
                })
                taken += 1

            print(f"  [ok]   {category} / {feed_name}: {taken} new")

        collected.extend(category_items[:MAX_PER_CATEGORY])

    save_seen(seen)
    return collected


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
def smtp_config() -> tuple[dict | None, str | None]:
    """Read SMTP settings from the environment.
    Returns (config, reason_it_is_unusable)."""
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASS", "").strip()
    recipients = [a.strip() for a in os.environ.get("MAIL_TO", "").split(",") if a.strip()]

    missing = [name for name, value in
               (("SMTP_USER", user), ("SMTP_PASS", password), ("MAIL_TO", recipients))
               if not value]
    if missing:
        return None, f"not configured (missing {', '.join(missing)})"

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
        "sender": os.environ.get("MAIL_FROM", "").strip() or user,
        "recipients": recipients,
    }, None


def build_message(cfg: dict, subject: str, html_body: str, attachments=()) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = cfg["sender"]
    message["To"] = ", ".join(cfg["recipients"])

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
    items = fetch_new_articles()

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
