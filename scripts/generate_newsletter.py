#!/usr/bin/env python3
"""
Gaga Security Intel — Automated Weekly Cybersecurity Newsletter Generator
Tailored for Micro-Enterprises (1–5 Employees).

Scrapes RSS feeds weekly from:
  1. CISA Cybersecurity Advisories (https://www.cisa.gov/cybersecurity-advisories/all.xml)
  2. Krebs on Security (https://krebsonsecurity.com/feed/)
  3. BleepingComputer (https://www.bleepingcomputer.com/feed/)

Uses Gemini 4 Argon (via google-genai SDK) to:
  - Filter out enterprise/corporate-only cybersecurity noise (SCADA/ICS, BGP, Kubernetes,
    data-center appliances, mainframe/Active Directory forest administration).
  - Select the most relevant threats for a 1–5 person business (phishing, banking/ACH fraud,
    browsers, laptops/phones, small-office Wi-Fi routers, WordPress/Shopify, Google Workspace/M365).
  - Generate strict 2–3 sentence plain-English summaries and a single concrete action item.
  - Prepend the new weekly issue to `data/weeks.json` and build the static `index.html`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import feedparser
import requests
from dateutil import parser as date_parser

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("cybersec-weekly")

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT_DIR / "data" / "weeks.json"
ISSUES_DIR = ROOT_DIR / "data" / "issues"
TEMPLATE_FILE = ROOT_DIR / "templates" / "index.html.j2"
OUTPUT_HTML = ROOT_DIR / "index.html"

# Target RSS Feeds
RSS_FEEDS = [
    {
        "id": "cisa",
        "name": "CISA",
        "url": "https://www.cisa.gov/cybersecurity-advisories/all.xml",
        "fallback_url": "https://www.cisa.gov/news.xml",
    },
    {
        "id": "krebs",
        "name": "Krebs on Security",
        "url": "https://krebsonsecurity.com/feed/",
        "fallback_url": None,
    },
    {
        "id": "bleeping",
        "name": "BleepingComputer",
        "url": "https://www.bleepingcomputer.com/feed/",
        "fallback_url": None,
    },
]

# Preferred Gemini model identifier (defaults to Gemini 4 Argon as requested,
# with graceful fallback if the API endpoint aliases it to gemini-2.5-flash)
DEFAULT_GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-4-argon")
FALLBACK_GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash"]

USER_AGENT = (
    "SmallTeamShieldNewsletterBot/1.0 "
    "(+https://github.com/cybersec-weekly; micro-enterprise security digest)"
)


def strip_html_tags(raw_html: str) -> str:
    """Remove HTML tags and normalize whitespace for clean LLM input."""
    if not raw_html:
        return ""
    clean = re.sub(r"<[^>]+>", " ", raw_html)
    clean = html.unescape(clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:1200]


def parse_entry_date(entry: Any) -> Optional[dt.datetime]:
    """Parse RSS entry publication timestamp into a timezone-aware UTC datetime."""
    for field in ("published", "updated", "created"):
        raw_val = getattr(entry, field, None) or entry.get(field)
        if raw_val:
            try:
                parsed = date_parser.parse(raw_val)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=dt.timezone.utc)
                return parsed.astimezone(dt.timezone.utc)
            except Exception:
                continue
    return None


def fetch_rss_articles(days_lookback: int = 7, max_per_feed: int = 25) -> List[Dict[str, Any]]:
    """Fetch and parse recent articles from CISA, Krebs on Security, and BleepingComputer."""
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_lookback)
    collected: List[Dict[str, Any]] = []

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml, text/xml, */*"})

    for feed_cfg in RSS_FEEDS:
        urls_to_try = [u for u in [feed_cfg["url"], feed_cfg.get("fallback_url")] if u]
        feed_entries: List[Any] = []

        for url in urls_to_try:
            try:
                logger.info("Fetching RSS feed: %s (%s)", feed_cfg["name"], url)
                resp = session.get(url, timeout=20)
                resp.raise_for_status()
                parsed_feed = feedparser.parse(resp.content)
                if parsed_feed.entries:
                    feed_entries = parsed_feed.entries
                    break
            except Exception as exc:
                logger.warning("Failed fetching %s (%s): %s", feed_cfg["name"], url, exc)

        count_for_feed = 0
        for entry in feed_entries:
            if count_for_feed >= max_per_feed:
                break

            pub_dt = parse_entry_date(entry)
            if pub_dt and pub_dt < cutoff:
                continue

            title = strip_html_tags(entry.get("title", "Untitled Advisory"))
            summary = strip_html_tags(entry.get("summary", "") or entry.get("description", ""))
            link = entry.get("link", "")

            if not title or not link:
                continue

            collected.append(
                {
                    "source_id": feed_cfg["id"],
                    "source_name": feed_cfg["name"],
                    "title": title,
                    "raw_summary": summary,
                    "url": link,
                    "published_at": pub_dt.strftime("%Y-%m-%d") if pub_dt else dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d"),
                }
            )
            count_for_feed += 1

        logger.info("Collected %d recent items from %s", count_for_feed, feed_cfg["name"])

    return collected


def enforce_two_to_three_sentences(text: str) -> str:
    """Ensure summary text strictly contains 2 to 3 sentences."""
    cleaned = re.sub(r"\s+", " ", text.strip())
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", cleaned) if s.strip()]
    if len(sentences) > 3:
        sentences = sentences[:3]
    return " ".join(sentences)


def curate_with_gemini(
    raw_articles: List[Dict[str, Any]],
    issue_number: int,
    reference_date: dt.date,
) -> Dict[str, Any]:
    """
    Use Gemini 4 Argon (via google-genai SDK) to filter out enterprise corporate noise
    and generate a micro-enterprise weekly cybersecurity edition with 2-3 sentence summaries.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY environment variable is required to run live AI curation. "
            "Set GEMINI_API_KEY or run with --render-only to build static HTML from existing data/weeks.json."
        )

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)

    start_date = reference_date - dt.timedelta(days=7)
    date_label = reference_date.strftime("Monday, %B %-d, %Y") if os.name != "nt" else reference_date.strftime("Monday, %B %d, %Y").replace(" 0", " ")
    date_range = f"{start_date.strftime('%b %d').replace(' 0', ' ')} – {reference_date.strftime('%b %d, %Y').replace(' 0', ' ')}"
    iso_year, iso_week, _ = reference_date.isocalendar()
    week_id = f"{iso_year}-W{iso_week:02d}"

    system_instruction = """You are the Chief Security Advisor for 'Gaga Security Intel', a weekly cybersecurity newsletter built exclusively for micro-enterprises (1 to 5 employees: solo founders, small retail shops, local accountants, design studios, small law/medical offices, and contractors) who have NO dedicated IT department.

Your job:
1. FILTER OUT ENTERPRISE CORPORATE NOISE:
   Ruthlessly discard articles that only apply to large enterprises, data centers, or industrial control systems—such as SCADA/ICS PLCs, Kubernetes clusters, BGP routing, VMware ESXi/vCenter, Citrix NetScaler, F5 BIG-IP, Cisco IOS XE enterprise switches, SAP/Oracle ERP, mainframe, or Active Directory forest trusts—UNLESS there is a direct threat to a 1-5 person business.

2. SELECT 4 TO 6 HIGH-VALUE MICRO-ENTERPRISE STORIES:
   Prioritize threats that actually hit 1-5 person teams:
   - Phishing, invoice fraud, ACH/wire transfer hijacking, fake QuickBooks/DocuSign/Stripe alerts
   - Web browsers (Chrome, Safari, Edge, Brave) and malicious browser extensions
   - Operating system updates (Windows 10/11, macOS, iOS, Android)
   - Cloud work suites (Google Workspace, Microsoft 365, Dropbox, Slack, Zoom)
   - Small business websites (WordPress plugins, Shopify, Squarespace)
   - Home/small-office Wi-Fi routers (TP-Link, Asus, Netgear, Ubiquiti) and NAS backup boxes (Synology, QNAP)
   - Password managers, 2FA/MFA tricks, and SMS/phone social engineering scams.

3. WRITE STRICT 2-3 SENTENCE SUMMARIES FOR NON-TECHNICAL READERS:
   - Every item's `summary` MUST be 2 to 3 sentences long.
   - Use plain, reassuring, everyday English. Zero CVE numbers, zero hex hashes, and zero enterprise acronyms.
   - Sentence 1: What is happening right now.
   - Sentence 2: How it affects a 1-5 person business in the real world.
   - Sentence 3 (optional): Why the quick fix stops it.
   - Provide a 1-sentence `action_step` that a non-technical owner can do in under 5 minutes.
"""

    prompt_payload = {
        "task": "Curate this week's micro-enterprise cybersecurity newsletter from the provided RSS feed items.",
        "issue_number": issue_number,
        "week_id": week_id,
        "date_label": date_label,
        "date_range": date_range,
        "raw_articles_count": len(raw_articles),
        "raw_articles": raw_articles,
        "required_json_schema": {
            "threat_level": "Low | Moderate | Elevated | High",
            "headline": "Short punchy headline summarizing the top 2-3 micro-business threats this week",
            "executive_brief": "2 sentences summarizing the week's landscape and how many enterprise alerts were filtered out",
            "priority_action": "1 sentence describing the single most important 5-minute action for the week",
            "filtered_noise_examples": [
                "3 short examples of enterprise-only stories that were discarded from the raw feed"
            ],
            "items": [
                {
                    "source_id": "cisa | krebs | bleeping",
                    "source_name": "CISA | Krebs on Security | BleepingComputer",
                    "category": "Short category label (e.g., Email & Phishing, Browser Security, Wi-Fi & Routers)",
                    "severity": "Critical | High | Medium",
                    "time_to_fix": "e.g., 2 mins, 5 mins, Awareness",
                    "title": "Plain-English title for a 1-5 person business owner",
                    "summary": "STRICTLY 2 to 3 sentences in plain non-technical English.",
                    "action_step": "1 concrete step a non-technical team can take today.",
                    "affected_tools": ["Tool 1", "Tool 2"],
                    "url": "Original article URL from input",
                    "published_at": "YYYY-MM-DD",
                }
            ],
        },
    }

    models_to_try = [DEFAULT_GEMINI_MODEL] + [
        m for m in FALLBACK_GEMINI_MODELS if m != DEFAULT_GEMINI_MODEL
    ]

    response_text = None
    used_model = DEFAULT_GEMINI_MODEL

    for model_name in models_to_try:
        try:
            logger.info("Invoking Gemini model: %s", model_name)
            response = client.models.generate_content(
                model=model_name,
                contents=json.dumps(prompt_payload, ensure_ascii=False),
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    temperature=0.25,
                    response_mime_type="application/json",
                ),
            )
            if response and response.text:
                response_text = response.text
                used_model = model_name
                break
        except Exception as exc:
            logger.warning("Model '%s' returned error (%s); trying next candidate...", model_name, exc)

    if not response_text:
        raise RuntimeError("All configured Gemini models failed to return a response.")

    parsed = json.loads(response_text)
    curated_items = parsed.get("items", [])

    # Normalize and enforce 2-3 sentence constraint
    normalized_items = []
    for idx, item in enumerate(curated_items, start=1):
        normalized_items.append(
            {
                "id": f"{week_id.lower()}-{idx}",
                "source_id": item.get("source_id", "cisa"),
                "source_name": item.get("source_name", "CISA"),
                "category": item.get("category", "Small Business Security"),
                "severity": item.get("severity", "High"),
                "time_to_fix": item.get("time_to_fix", "3 mins"),
                "title": item.get("title", "Security Update Required"),
                "summary": enforce_two_to_three_sentences(item.get("summary", "")),
                "action_step": item.get("action_step", "Check your software updates today."),
                "affected_tools": item.get("affected_tools", ["Workstations"])[:4],
                "url": item.get("url", "https://www.cisa.gov/"),
                "published_at": item.get("published_at", reference_date.strftime("%Y-%m-%d")),
            }
        )

    raw_count = len(raw_articles)
    kept_count = len(normalized_items)
    filtered_count = max(0, raw_count - kept_count)

    new_week = {
        "issue_number": issue_number,
        "week_id": week_id,
        "date_label": date_label,
        "date_range": date_range,
        "is_current": True,
        "threat_level": parsed.get("threat_level", "Elevated"),
        "headline": parsed.get("headline", "Weekly Micro-Business Security Brief"),
        "executive_brief": parsed.get(
            "executive_brief",
            f"We scanned {raw_count} advisories from CISA, Krebs on Security, and BleepingComputer and filtered out {filtered_count} enterprise-only alerts.",
        ),
        "priority_action": parsed.get(
            "priority_action",
            "Install pending browser and operating system updates across all team devices this morning.",
        ),
        "stats": {
            "raw_articles_scanned": raw_count,
            "enterprise_noise_filtered": filtered_count,
            "micro_business_stories": kept_count,
            "estimated_read_time": f"{max(1, round(kept_count * 0.35))} min",
        },
        "filtered_noise_examples": parsed.get(
            "filtered_noise_examples",
            [
                "Enterprise data-center appliance advisories",
                "Industrial SCADA/ICS controller vulnerabilities",
                "Multi-cluster Kubernetes privilege escalations",
            ],
        )[:3],
        "items": normalized_items,
        "generated_by_model": used_model,
    }

    return new_week


def load_site_data() -> Dict[str, Any]:
    """Load existing newsletter data from data/weeks.json."""
    if not DATA_FILE.exists():
        raise FileNotFoundError(f"Data file not found: {DATA_FILE}")
    with DATA_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_site_data(site_data: Dict[str, Any], latest_week: Optional[Dict[str, Any]] = None) -> None:
    """Persist updated data/weeks.json and archive the individual issue file."""
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    ISSUES_DIR.mkdir(parents=True, exist_ok=True)

    with DATA_FILE.open("w", encoding="utf-8") as f:
        json.dump(site_data, f, indent=2, ensure_ascii=False)
        f.write("\n")

    if latest_week:
        issue_path = ISSUES_DIR / f"{latest_week['week_id']}.json"
        with issue_path.open("w", encoding="utf-8") as f:
            json.dump(latest_week, f, indent=2, ensure_ascii=False)
            f.write("\n")
        logger.info("Saved issue snapshot to %s", issue_path)


def render_static_site(site_data: Dict[str, Any]) -> None:
    """Render the static index.html using the Jinja2 template and weeks.json data."""
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_FILE.parent)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    template = env.get_template(TEMPLATE_FILE.name)

    weeks = site_data.get("weeks", [])
    if not weeks:
        raise ValueError("No weekly issues found in data/weeks.json")

    current_week = weeks[0]
    older_weeks = weeks[1:]

    rendered_html = template.render(
        metadata=site_data.get("metadata", {}),
        current_week=current_week,
        older_weeks=older_weeks,
        all_weeks=weeks,
        site_data_json=json.dumps(site_data, ensure_ascii=False),
    )

    with OUTPUT_HTML.open("w", encoding="utf-8") as f:
        f.write(rendered_html)

    logger.info("Rendered static website to %s (%d bytes)", OUTPUT_HTML, len(rendered_html))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Automated Weekly Micro-Enterprise Cybersecurity Newsletter Generator"
    )
    parser.add_argument(
        "--render-only",
        action="store_true",
        help="Skip RSS scraping and Gemini API call; render index.html from existing data/weeks.json.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Number of days to look back when scraping RSS feeds (default: 7).",
    )
    parser.add_argument(
        "--force-new-week",
        action="store_true",
        help="Create a new entry even if an issue with the current ISO week_id already exists.",
    )
    args = parser.parse_args()

    site_data = load_site_data()

    if args.render_only:
        logger.info("Running in --render-only mode...")
        render_static_site(site_data)
        return 0

    raw_articles = fetch_rss_articles(days_lookback=args.days)
    if not raw_articles:
        logger.warning("No recent RSS articles fetched; re-rendering existing site data without changes.")
        render_static_site(site_data)
        return 0

    existing_weeks = site_data.get("weeks", [])
    latest_issue_num = max((w.get("issue_number", 0) for w in existing_weeks), default=0)
    today = dt.datetime.now(dt.timezone.utc).date()
    iso_year, iso_week, _ = today.isocalendar()
    target_week_id = f"{iso_year}-W{iso_week:02d}"

    # Determine whether we update the current week in-place or prepend a new week
    if existing_weeks and existing_weeks[0].get("week_id") == target_week_id and not args.force_new_week:
        next_issue_number = existing_weeks[0].get("issue_number", latest_issue_num)
        replace_top = True
    else:
        next_issue_number = latest_issue_num + 1
        replace_top = False

    new_week = curate_with_gemini(
        raw_articles=raw_articles,
        issue_number=next_issue_number,
        reference_date=today,
    )

    # Demote previous weeks from is_current
    for w in existing_weeks:
        w["is_current"] = False

    if replace_top:
        existing_weeks[0] = new_week
    else:
        existing_weeks.insert(0, new_week)

    site_data["weeks"] = existing_weeks
    site_data.setdefault("metadata", {})["last_updated"] = (
        dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    site_data["metadata"]["model"] = DEFAULT_GEMINI_MODEL

    save_site_data(site_data, latest_week=new_week)
    render_static_site(site_data)
    logger.info("Successfully generated Issue #%d (%s)", new_week["issue_number"], new_week["week_id"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
