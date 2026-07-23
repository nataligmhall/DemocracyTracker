#!/usr/bin/env python3
"""
Democracy Tracker — Weekly News Delta Updater

Fetches free allowlisted RSS feeds, matches democracy-relevant articles to
countries, applies a capped news_delta on top of the research baseline, and
stores the same articles as evidence in data/scores.json.

  score = clamp(baseline + news_delta, 0, 100)
  news_delta = clamp(prev_delta * DECAY + week_impact, -MAX_DELTA, +MAX_DELTA)

Usage:
  python scripts/update_news_delta.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Optional
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import requests

# ── Config ────────────────────────────────────────────────────────────────────

ROOT = os.path.join(os.path.dirname(__file__), "..")
DATA_FILE = os.path.join(ROOT, "data", "scores.json")
SOURCES_FILE = os.path.join(ROOT, "data", "news_sources.json")

USER_AGENT = "DemocracyTracker/1.0 (https://github.com/nataligmhall/DemocracyTracker)"
TIMEOUT = 25
MAX_DELTA = 5.0
DECAY = 0.5
MAX_EVIDENCE = 10
ARTICLE_WEIGHT_CAP = 1.5
REQ_DELAY = 0.4

# Democracy / governance relevance (title or summary must match)
# Avoid bare "Democratic" (US party) — require democracy as a system/topic.
TOPIC_RE = re.compile(
    r"\b("
    r"democracy|democracies|democratization|democratisation|"
    r"democratic\s+(?:reform|reforms|election|elections|institutions?|transition|"
    r"backsliding|erosion|norms?|process|crisis|collapse|revival)|"
    r"election|electoral|vote|voting|ballot|ballot\s+box|"
    r"parliament|congress|senate|legislat\w*|constitut\w*|"
    r"(?:military\s+)?coup(?:\s+d['’]état)?|coup\s+attempt|junta|martial\s+law|"
    r"authoritarian|dictat\w*|autocrat\w*|"
    r"censorship|crackdown|repression|political\s+prisoner|"
    r"human\s+rights|civil\s+(libert(?:y|ies)|society)|press\s+freedom|"
    r"free\s+speech|rule\s+of\s+law|judiciary|opposition\s+part(?:y|ies)|"
    r"protest(?:s|ers|ing)?|demonstrat\w*|journalist|media\s+ban|"
    r"impeach\w*|referendum|term\s+limit|dissident|exile|"
    r"rigged\s+election|electoral\s+fraud|disinformation|"
    r"freedom\s+house|amnesty\s+international|ohchr|"
    r"political\s+freedom|political\s+freedoms"
    r")\b",
    re.I,
)

# Positive democracy signals
POS_RE = re.compile(
    r"\b("
    r"free\s+(and\s+)?fair|democratic\s+reform|restor(e|ed|ing)\s+democracy|"
    r"press\s+freedom|freed(?:om)?\s+of\s+(?:the\s+)?press|"
    r"releas(?:e|ed|es|ing)\s+(?:political\s+)?prisoners?|"
    r"pardon(?:ed|s)?|acquitt(?:ed|al)|drop(?:ped|s)?\s+charges|"
    r"independent\s+(?:judiciary|media|election)|"
    r"opposition\s+(?:win|wins|won|victory)|peaceful\s+transfer|"
    r"electoral\s+reform|anti[- ]corruption\s+(?:law|bill|reform)|"
    r"human\s+rights\s+(?:win|victory|progress|improve)"
    r")\b",
    re.I,
)

# Negative / erosion signals
NEG_RE = re.compile(
    r"\b("
    r"crackdown|censorship|"
    r"(?:military\s+)?coup(?:\s+d['’]état)?|coup\s+attempt|staged\s+a\s+coup|"
    r"junta|martial\s+law|"
    r"authoritarian|dictatorship|autocrat(?:ic|s)?|"
    r"political\s+prisoner|arrest(?:ed|s)?\s+(?:opposition|journalist|protester)|"
    r"jail(?:ed|s)?\s+(?:opposition|journalist|protester)|"
    r"detain(?:ed|s|ing)\s+(?:opposition|journalist|protester)|"
    r"ban(?:ned|s)?\s+(?:opposition|media|party|protest|newspaper|tv\s+station)|"
    r"(?<!social\s)media\s+ban|opposition\s+ban|party\s+ban|"
    r"shut(?:s|ting)?\s+down\s+(?:media|newspaper|tv|outlet)|"
    r"rigged\s+election|electoral\s+fraud|stolen\s+election|"
    r"dissolve(?:s|d)?\s+parliament|emergency\s+powers|"
    r"repression|persecut(?:e|ion|ed)|torture|"
    r"assassinate|assassination|extrajudicial|"
    r"suspend(?:s|ed|ing)?\s+(?:election|constitution|parliament)|"
    r"term\s+limit\s+(?:scrap|remove|abolish)|"
    r"crush(?:es|ed|ing)?\s+protest|"
    r"military\s+rule|human\s+rights\s+crisis"
    r")\b",
    re.I,
)

# Extra aliases beyond official names (matched as whole words / phrases)
ALIASES: dict[str, list[str]] = {
    "USA": ["United States", "U.S.", "USA", "US"],
    "GBR": ["United Kingdom", "Britain", "UK"],
    "RUS": ["Russia", "Russian Federation"],
    "CHN": ["China"],
    "KOR": ["South Korea", "Republic of Korea"],
    "PRK": ["North Korea", "DPRK"],
    "COD": ["Democratic Republic of Congo", "Democratic Republic of the Congo", "DR Congo", "DRC", "Congo-Kinshasa"],
    "COG": ["Republic of the Congo", "Congo-Brazzaville"],
    "CIV": ["Ivory Coast", "Côte d'Ivoire", "Cote d'Ivoire"],
    "MMR": ["Myanmar", "Burma"],
    "IRN": ["Iran"],
    "TUR": ["Turkey", "Türkiye", "Turkiye"],
    "ARE": ["United Arab Emirates", "UAE"],
    "SYR": ["Syria"],
    "PSE": ["Palestine", "Palestinian"],
    "TZA": ["Tanzania"],
    "CZE": ["Czechia", "Czech Republic"],
    "SVK": ["Slovakia"],
    "VEN": ["Venezuela"],
    "UKR": ["Ukraine"],
    "BLR": ["Belarus"],
    "MKD": ["North Macedonia"],
    "SWZ": ["Eswatini", "Swaziland"],
    "TLS": ["Timor-Leste", "East Timor"],
    "FSM": ["Micronesia"],
    "LAO": ["Laos"],
    "MDA": ["Moldova"],
    "BIH": ["Bosnia", "Bosnia and Herzegovina"],
    "CAF": ["Central African Republic"],
    "GNQ": ["Equatorial Guinea"],
    "PNG": ["Papua New Guinea"],
    "STP": ["Sao Tome", "São Tomé"],
}

# Tokens that are too ambiguous alone (need longer alias or full official name)
# (Handled inline in match_countries for GEO / GIN / NER.)


# ── Label (shared with importer) ──────────────────────────────────────────────

def regime_label(score: float) -> str:
    if score >= 80:
        return "Liberal Democracy"
    if score >= 60:
        return "Electoral Democracy"
    if score >= 40:
        return "Hybrid Regime"
    if score >= 20:
        return "Electoral Autocracy"
    return "Closed Autocracy"


# ── RSS fetch (stdlib XML — no feedparser required) ───────────────────────────

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "dc": "http://purl.org/dc/elements/1.1/",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rss": "http://purl.org/rss/1.0/",
}


def _text(el: Optional[ET.Element]) -> str:
    if el is None:
        return ""
    return "".join(el.itertext()).strip()


def _find_link(entry: ET.Element) -> str:
    # RSS 2.0
    link = entry.find("link")
    if link is not None and (_text(link) or link.get("href")):
        return (_text(link) or link.get("href") or "").strip()
    # Atom
    for link in entry.findall("atom:link", NS) + entry.findall("{http://www.w3.org/2005/Atom}link"):
        href = link.get("href")
        rel = link.get("rel", "alternate")
        if href and rel in ("alternate", None, ""):
            return href.strip()
    # RDF RSS 1.0
    link = entry.find("rss:link", NS)
    if link is not None:
        return _text(link)
    return ""


def _find_date(entry: ET.Element) -> Optional[datetime]:
    for tag in (
        "pubDate",
        "published",
        "updated",
        "{http://www.w3.org/2005/Atom}published",
        "{http://www.w3.org/2005/Atom}updated",
        "{http://purl.org/dc/elements/1.1/}date",
    ):
        el = entry.find(tag) if not tag.startswith("{") else entry.find(tag)
        if el is None and ":" not in tag and not tag.startswith("{"):
            continue
        raw = _text(el) if el is not None else ""
        if not raw:
            continue
        try:
            dt = parsedate_to_datetime(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except (TypeError, ValueError, IndexError):
            pass
        try:
            # ISO-ish
            raw2 = raw.replace("Z", "+00:00")
            dt = datetime.fromisoformat(raw2)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def parse_feed_xml(xml_text: str, source_name: str) -> list[dict]:
    """Parse RSS 2.0 / Atom / RSS 1.0 RDF into article dicts."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    articles = []
    # RSS 2.0 channel/item
    items = root.findall("./channel/item")
    # Atom
    if not items:
        items = root.findall("{http://www.w3.org/2005/Atom}entry")
    if not items:
        items = root.findall("atom:entry", NS)
    # RSS 1.0
    if not items:
        items = root.findall("{http://purl.org/rss/1.0/}item")
    if not items:
        items = root.findall("rss:item", NS)

    for entry in items:
        title = (
            _text(entry.find("title"))
            or _text(entry.find("{http://www.w3.org/2005/Atom}title"))
            or _text(entry.find("rss:title", NS))
        )
        link = _find_link(entry)
        summary = (
            _text(entry.find("description"))
            or _text(entry.find("{http://www.w3.org/2005/Atom}summary"))
            or _text(entry.find("{http://www.w3.org/2005/Atom}content"))
            or _text(entry.find("rss:description", NS))
        )
        # Strip crude HTML tags from summary
        summary = re.sub(r"<[^>]+>", " ", summary)
        summary = re.sub(r"\s+", " ", summary).strip()

        if not title or not link:
            continue

        published = _find_date(entry)
        # Google News (and some others) expose publisher in <source>
        source_el = entry.find("source")
        pub_source = source_name
        if source_el is not None and (_text(source_el) or "").strip():
            pub_source = _text(source_el).strip()

        # Strip trailing " - Reuters" style suffixes from Google News titles
        clean_title = re.sub(
            r"\s+-\s+(Reuters|AP|Associated Press|AFP|BBC News|Deutsche Welle)\s*$",
            "",
            title.strip(),
            flags=re.I,
        )

        articles.append({
            "title": clean_title,
            "url": link,
            "summary": summary[:500],
            "source": pub_source,
            "published": published,
        })
    return articles


def google_news_reuters_url(country_name: str, max_age_days: int = 30) -> str:
    """Build a free Google News RSS URL filtered to Reuters for one country."""
    from urllib.parse import quote_plus
    q = (
        f"\"{country_name}\" source:Reuters "
        f"(democracy OR election OR parliament OR congress OR government OR "
        f"protest OR \"human rights\" OR opposition OR authoritarian OR "
        f"censorship OR judiciary OR vote) when:{max_age_days}d"
    )
    return (
        "https://news.google.com/rss/search?"
        f"q={quote_plus(q)}&hl=en-US&gl=US&ceid=US:en"
    )


def enrich_missing_with_reuters(
    countries: dict,
    evidence_cand: dict,
    matchers: list,
    *,
    max_age_days: int,
    min_evidence: int = 2,
    max_fetches: int = 80,
) -> int:
    """
    For countries still short on evidence, fetch Reuters via Google News RSS.
    Returns number of countries enriched. Capped to keep weekly runtime sane.
    """
    # Prioritize large / commonly viewed countries
    PRIORITY = [
        "USA", "GBR", "BRA", "AUS", "IND", "CHN", "RUS", "UKR", "FRA", "DEU",
        "JPN", "KOR", "MEX", "CAN", "ITA", "ESP", "POL", "TUR", "IDN", "ZAF",
        "NGA", "ARG", "SAU", "IRN", "ISR", "EGY", "PAK", "BGD", "PHL", "VNM",
        "COL", "CHL", "PER", "VEN", "THA", "MMR", "ETH", "KEN", "COD", "HUN",
        "ROU", "CZE", "SWE", "NOR", "NLD", "BEL", "CHE", "AUT", "PRT", "GRC",
        "NZL", "MYS", "SGP", "ARE", "IRQ", "AFG", "SYR", "CUB", "NIC", "BLR",
    ]
    priority_rank = {code: i for i, code in enumerate(PRIORITY)}

    needing = [
        (iso3, c) for iso3, c in countries.items()
        if len(evidence_cand.get(iso3) or []) < min_evidence
    ]
    needing.sort(
        key=lambda x: (
            priority_rank.get(x[0], 1000),
            len(evidence_cand.get(x[0]) or []),
            x[1]["name"],
        )
    )
    needing = needing[:max_fetches]

    print(f"\nReuters enrichment for {len(needing)} countries with thin coverage…")
    enriched = 0
    for iso3, c in needing:
        url = google_news_reuters_url(c["name"], max_age_days)
        print(f"  → Reuters/{iso3} …", end=" ", flush=True)
        items = fetch_feed({"id": f"reuters_{iso3}", "name": "Reuters", "url": url})
        added = 0
        for art in items[:12]:
            text = f"{art['title']} {art['summary']}"
            # Must mention this country
            hits = match_countries(text, art["title"], matchers, title_only=False)
            if iso3 not in hits:
                continue
            weight, tag = classify_weight(art["title"], art["summary"])
            # Keep wire context even if topic classifier is unsure, if country in title
            title_hits = match_countries(text, art["title"], matchers, title_only=True)
            if tag == "irrelevant":
                if iso3 not in title_hits:
                    continue
                tag = "wire context"
                weight = 0.0
            date_str = ""
            if art.get("published"):
                date_str = art["published"].strftime("%Y-%m-%d")
            evidence_cand[iso3].append({
                "title": art["title"],
                "url": art["url"],
                "date": date_str,
                "source": art.get("source") or "Reuters",
                "weight": round(weight if iso3 in title_hits else 0.0, 2),
                "tag": tag if tag != "irrelevant" else "wire context",
            })
            added += 1
        print(f"{added} kept ({len(items)} parsed)")
        if added:
            enriched += 1
        time.sleep(REQ_DELAY)
    return enriched


def fetch_feed(feed: dict) -> list[dict]:
    url = feed["url"]
    name = feed["name"]
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        return parse_feed_xml(resp.text, name)
    except Exception as exc:
        print(f"    [warn] {feed.get('id', name)}: {exc}")
        return []


# ── Country matching ──────────────────────────────────────────────────────────

def build_matchers(countries: dict) -> list[tuple[str, re.Pattern, str]]:
    """
    Return list of (iso3, compiled_pattern, display_name) sorted by pattern
    specificity (longer phrases first) to prefer 'South Korea' over 'Korea'.
    """
    matchers: list[tuple[str, str, str]] = []  # iso3, phrase, name

    # Official names that are too ambiguous as bare tokens
    SKIP_BARE_NAME = {"COG"}  # "Congo" alone → prefer COD aliases / Brazzaville only

    for iso3, c in countries.items():
        name = c["name"]
        phrases = set()
        if iso3 not in SKIP_BARE_NAME:
            phrases.add(name)

        for alias in ALIASES.get(iso3, []):
            phrases.add(alias.strip())

        if name.startswith("United States"):
            phrases.update(["United States", "U.S.", "USA", "US"])
        if name == "United Kingdom":
            phrases.update(["United Kingdom", "Britain", "UK"])

        for phrase in phrases:
            p = phrase.strip()
            if len(p) < 2:
                continue
            # Never match bare "Congo" (ambiguous between COD/COG)
            if p.lower() == "congo":
                continue
            # Allow short ISO-style tokens we handle explicitly
            if len(p) < 3 and p not in ("US", "UK"):
                continue
            matchers.append((iso3, p, name))

    # Sort longer phrases first
    matchers.sort(key=lambda x: len(x[1]), reverse=True)

    compiled = []
    for iso3, phrase, name in matchers:
        if phrase in ("U.S.", "USA", "US", "UK", "UAE", "DRC", "DPRK"):
            if phrase == "U.S.":
                pat = re.compile(r"\bU\.S\.?\b", re.I)
            elif phrase == "US":
                # Capital US only — avoid "us" pronoun; Google/Reuters headlines use US
                pat = re.compile(r"\bUS\b")
            elif phrase == "UK":
                pat = re.compile(r"\bUK\b")
            else:
                pat = re.compile(rf"\b{re.escape(phrase)}\b", re.I)
        else:
            pat = re.compile(rf"\b{re.escape(phrase)}\b", re.I)
        compiled.append((iso3, pat, name))
    return compiled


def _passes_ambiguity_guards(iso3: str, text: str) -> bool:
    if iso3 == "GEO":
        if re.search(
            r"\b(Atlanta|Georgia\s+(voter|election|law|state|republicans?|democrats?|governor))\b",
            text,
            re.I,
        ):
            if not re.search(r"\b(Tbilisi|Caucasus|South\s+Ossetia|Abkhazia)\b", text, re.I):
                return False
    if iso3 == "COG":
        if re.search(
            r"\b(DRC|DR\s+Congo|Democratic\s+Republic\s+of\s+(?:the\s+)?Congo|Congo-Kinshasa)\b",
            text,
            re.I,
        ):
            return False
        if not re.search(r"\b(Congo-Brazzaville|Republic\s+of\s+the\s+Congo)\b", text, re.I):
            return False
    if iso3 == "GIN" and re.search(r"\b(Equatorial|Papua New|Bissau)\s+Guinea\b", text, re.I):
        return False
    if iso3 == "NER" and re.search(r"\bNigeria\b", text, re.I) and not re.search(r"\bNiger\b", text, re.I):
        return False
    return True


def match_countries(
    text: str,
    title: str,
    matchers: list,
    *,
    title_only: bool = False,
) -> list[str]:
    """
    Return unique iso3 codes mentioned.
    title_only=True → used for score deltas (higher precision).
    title_only=False → title or body (broader context for evidence).
    """
    found = []
    seen = set()
    haystack = title if title_only else text

    for iso3, pat, _name in matchers:
        if iso3 in seen:
            continue
        if not pat.search(haystack):
            continue
        if not _passes_ambiguity_guards(iso3, text):
            continue
        seen.add(iso3)
        found.append(iso3)
    return found


# ── Scoring an article ────────────────────────────────────────────────────────

def classify_weight(title: str, summary: str) -> tuple[float, str]:
    """
    Return (weight, tag). Weight in [-ARTICLE_WEIGHT_CAP, +ARTICLE_WEIGHT_CAP].
    0 means relevant topic but neutral / unclear direction.
    """
    # Metaphorical "coup for X" is not a military coup / democracy topic
    title_clean = re.sub(r"\bcoup for\b", "win for", title, flags=re.I)
    summary_clean = re.sub(r"\bcoup for\b", "win for", summary, flags=re.I)
    text = f"{title_clean}. {summary_clean}"

    if not TOPIC_RE.search(title_clean) and not TOPIC_RE.search(text):
        return 0.0, "irrelevant"

    pos = bool(POS_RE.search(text))
    neg = bool(NEG_RE.search(text))

    if pos and not neg:
        return 1.0, "pro-democracy"
    if neg and not pos:
        return -1.0, "erosion"
    if pos and neg:
        pos_m = POS_RE.search(title_clean)
        neg_m = NEG_RE.search(title_clean)
        if pos_m and (not neg_m or pos_m.start() < neg_m.start()):
            return 0.5, "mixed"
        if neg_m:
            return -0.5, "mixed"
        return 0.0, "mixed"
    # Topic in title → governance context; summary-only topic → drop
    if TOPIC_RE.search(title_clean):
        return 0.0, "governance"
    return 0.0, "irrelevant"


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    now = datetime.now(timezone.utc)
    print("Democracy Tracker — News Delta Updater")
    print(f"Run date: {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 56)

    if not os.path.exists(DATA_FILE):
        print(f"[ERROR] missing {DATA_FILE} — run import_baselines.py first", file=sys.stderr)
        return 1

    with open(DATA_FILE, encoding="utf-8") as f:
        data = json.load(f)

    countries = data.get("countries") or {}
    if not countries:
        print("[ERROR] no countries in scores.json", file=sys.stderr)
        return 1

    with open(SOURCES_FILE, encoding="utf-8") as f:
        sources = json.load(f)

    max_age_days = int(sources.get("max_age_days", 14))
    cutoff = now - timedelta(days=max_age_days)
    feeds = sources.get("feeds") or []

    print(f"\nFetching {len(feeds)} feeds (last {max_age_days} days)…")
    all_articles: list[dict] = []
    seen_urls = set()

    for feed in feeds:
        print(f"  → {feed.get('id', feed.get('name'))} …", end=" ", flush=True)
        items = fetch_feed(feed)
        kept = 0
        for art in items:
            url = art["url"]
            # Normalize URL for dedupe
            key = url.split("#")[0].rstrip("/")
            if key in seen_urls:
                continue
            pub = art.get("published")
            if pub is not None and pub < cutoff:
                continue
            # If no date, keep (many feeds omit dates)
            seen_urls.add(key)
            all_articles.append(art)
            kept += 1
        print(f"{kept} new ({len(items)} parsed)")
        time.sleep(REQ_DELAY)

    print(f"\nUnique articles: {len(all_articles)}")
    matchers = build_matchers(countries)

    # Accumulate per-country impact and evidence candidates
    impacts: dict[str, float] = {k: 0.0 for k in countries}
    evidence_cand: dict[str, list] = {k: [] for k in countries}
    matched_articles = 0
    relevant_articles = 0

    for art in all_articles:
        text = f"{art['title']} {art['summary']}"
        weight, tag = classify_weight(art["title"], art["summary"])
        if tag == "irrelevant":
            continue
        relevant_articles += 1

        # Broader match for evidence/context; title-only for score movement
        evidence_hits = match_countries(text, art["title"], matchers, title_only=False)
        delta_hits = match_countries(text, art["title"], matchers, title_only=True)
        if not evidence_hits:
            continue
        matched_articles += 1

        date_str = ""
        if art.get("published"):
            date_str = art["published"].strftime("%Y-%m-%d")

        domain = urlparse(art["url"]).netloc.replace("www.", "")

        # Score impact: title mentions only
        if delta_hits and weight != 0:
            split = weight / max(1, len(delta_hits))
            split = max(-ARTICLE_WEIGHT_CAP, min(ARTICLE_WEIGHT_CAP, split))
            for iso3 in delta_hits:
                impacts[iso3] += split

        # Evidence: any mention, including neutral governance context
        for iso3 in evidence_hits:
            # Weight shown is the delta contribution if this country was in title
            shown_weight = 0.0
            if iso3 in delta_hits and weight != 0:
                shown_weight = weight / max(1, len(delta_hits))
                shown_weight = max(-ARTICLE_WEIGHT_CAP, min(ARTICLE_WEIGHT_CAP, shown_weight))
            evidence_cand[iso3].append({
                "title": art["title"],
                "url": art["url"],
                "date": date_str,
                "source": art["source"] or domain,
                "weight": round(shown_weight, 2),
                "tag": tag,
            })

    print(f"Topic-relevant: {relevant_articles}  country-matched: {matched_articles}")

    if sources.get("wire_enrichment", True):
        enrich_missing_with_reuters(
            countries,
            evidence_cand,
            matchers,
            max_age_days=max_age_days,
            min_evidence=2,
            max_fetches=60,
        )
        for iso3, arts in evidence_cand.items():
            wire_extra = sum(
                float(a.get("weight") or 0)
                for a in arts
                if "reuters" in (a.get("source") or "").lower()
                and abs(float(a.get("weight") or 0)) > 0
            )
            if impacts.get(iso3, 0) == 0 and wire_extra:
                impacts[iso3] = max(-MAX_DELTA, min(MAX_DELTA, wire_extra))

    # Apply decay + week impact + cap; refresh scores & evidence
    changed = 0
    with_evidence = 0
    WIRE_NAMES = ("reuters", "associated press", "ap", "afp", "agence france")
    ALLOWED_SOURCES = WIRE_NAMES + (
        "bbc", "deutsche welle", "dw", "un news", "human rights watch", "hrw",
    )

    def is_wire(article: dict) -> bool:
        src = (article.get("source") or "").lower()
        if src == "ap":
            return True
        return any(w in src for w in WIRE_NAMES)

    def is_allowed_source(article: dict) -> bool:
        src = (article.get("source") or "").lower()
        if is_wire(article):
            return True
        return any(w in src for w in ALLOWED_SOURCES)

    for iso3, c in countries.items():
        baseline = float(c.get("baseline", c.get("score", 50)))
        prev_delta = float(c.get("news_delta", 0) or 0)
        week = max(-MAX_DELTA, min(MAX_DELTA, impacts.get(iso3, 0.0)))
        new_delta = max(-MAX_DELTA, min(MAX_DELTA, prev_delta * DECAY + week))
        new_delta = round(new_delta, 2)
        score = max(0, min(100, round(baseline + new_delta)))

        cands = [
            a for a in (evidence_cand.get(iso3) or [])
            if is_allowed_source(a)
        ]
        # Prefer wire services (Reuters / AP / AFP) for what we show
        wires = [a for a in cands if is_wire(a)]
        ordered = wires if wires else cands
        ordered.sort(
            key=lambda a: (
                1 if is_wire(a) else 0,
                abs(float(a.get("weight") or 0)),
                1 if a.get("tag") in ("pro-democracy", "erosion", "mixed") else 0,
            ),
            reverse=True,
        )

        seen = set()
        evidence = []
        for a in ordered:
            u = a["url"]
            if u in seen:
                continue
            seen.add(u)
            evidence.append(a)
            if len(evidence) >= MAX_EVIDENCE:
                break

        if evidence:
            with_evidence += 1

        old_score = c.get("score")
        old_delta = c.get("news_delta")
        c["news_delta"] = new_delta
        c["score"] = score
        c["label"] = regime_label(score)
        c["evidence"] = evidence
        if old_score != score or old_delta != new_delta or evidence:
            changed += 1

    data["updated"] = now.strftime("%Y-%m-%d")
    data["schema_version"] = data.get("schema_version", 2)

    # Refresh methodology news_delta block
    meth = data.setdefault("methodology", {})
    meth["model"] = "baseline_plus_news_delta"
    meth["formula"] = "score = clamp(baseline + news_delta, 0, 100)"
    meth["news_delta"] = {
        "status": "active",
        "cap": MAX_DELTA,
        "decay": DECAY,
        "max_age_days": max_age_days,
        "sources_file": "data/news_sources.json",
        "note": "Weekly free-RSS evidence adjusts baseline; see METHODOLOGY.md",
        "last_run": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "articles_fetched": len(all_articles),
        "articles_matched": matched_articles,
        "countries_with_evidence": with_evidence,
    }

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"\nSaved → {os.path.abspath(DATA_FILE)}")
    print(f"Countries updated: {changed}  with evidence: {with_evidence}")

    # Show largest |delta| moves
    moved = sorted(
        countries.values(),
        key=lambda c: abs(float(c.get("news_delta") or 0)),
        reverse=True,
    )
    print("\nLargest |news_delta|")
    for c in moved[:12]:
        d = float(c["news_delta"])
        if d == 0 and not c.get("evidence"):
            continue
        print(
            f"  {c['news_delta']:+5.2f}  score={c['score']:3d}  "
            f"{c['name']:<28}  evidence={len(c.get('evidence') or [])}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
