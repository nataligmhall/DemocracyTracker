#!/usr/bin/env python3
"""
Democracy Tracker — Score Updater
Queries the GDELT 2.0 DOC API (free, no key required) to compute weekly
democracy-health scores for 10 countries based on news tone and volume.

Score methodology:
  1. Fetch average news tone for democracy-positive signals over 4 weeks
  2. Fetch average news tone for repression/censorship signals over 4 weeks
  3. Derive a 0–100 score from the tone spread and article-volume ratio
  4. Pull 6 recent articles as human-readable "reasons"

Runs every Monday via GitHub Actions and commits updated data/scores.json.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

# ── Config ────────────────────────────────────────────────────────────────────

GDELT_API  = "https://api.gdeltproject.org/api/v2/doc/doc"
DATA_FILE  = os.path.join(os.path.dirname(__file__), "..", "data", "scores.json")
TIMESPAN   = "4w"      # look-back window for tone analysis
ART_SPAN   = "2w"      # look-back window for article fetch
MAX_ARTS   = 6         # articles shown per country
REQ_DELAY  = 2.5       # seconds between API calls (be polite)
TIMEOUT    = 30        # request timeout in seconds

# ── Country definitions ────────────────────────────────────────────────────────

COUNTRIES = {
    "USA": {"name": "United States",  "search": "United States"},
    "RUS": {"name": "Russia",         "search": "Russia"},
    "VEN": {"name": "Venezuela",      "search": "Venezuela"},
    "IRN": {"name": "Iran",           "search": "Iran"},
    "HUN": {"name": "Hungary",        "search": "Hungary"},
    "TUR": {"name": "Turkey",         "search": "Turkey"},
    "BRA": {"name": "Brazil",         "search": "Brazil"},
    "IND": {"name": "India",          "search": "India"},
    "POL": {"name": "Poland",         "search": "Poland"},
    "CHN": {"name": "China",          "search": "China"},
}

# ── Query terms ───────────────────────────────────────────────────────────────

# Signals associated with democratic health (positive tone = good sign)
POS_TERMS = (
    '(democracy OR election OR "civil liberties" OR "press freedom" '
    'OR "human rights" OR "free speech" OR "rule of law" OR "independent judiciary")'
)

# Signals associated with democratic erosion (negative tone = bad sign)
NEG_TERMS = (
    '(crackdown OR censorship OR "political prisoner" OR authoritarian '
    'OR repression OR "rigged election" OR "media ban" OR "political persecution" '
    'OR "arrested journalist" OR "opposition ban")'
)

# Broad governance query for fetching readable article titles
NEWS_TERMS = (
    '(democracy OR government OR election OR opposition OR "human rights" '
    'OR protest OR "civil society")'
)

# ── HTTP helpers ──────────────────────────────────────────────────────────────

def gdelt_get(params: dict, retries: int = 3) -> dict:
    """Call GDELT DOC API with automatic retries and exponential back-off."""
    for attempt in range(retries):
        try:
            resp = requests.get(GDELT_API, params=params, timeout=TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.Timeout:
            print(f"    [warn] timeout on attempt {attempt + 1}")
        except requests.exceptions.HTTPError as e:
            print(f"    [warn] HTTP {e.response.status_code} on attempt {attempt + 1}")
        except Exception as e:
            print(f"    [warn] error on attempt {attempt + 1}: {e}")
        if attempt < retries - 1:
            time.sleep(2 ** attempt)
    return {}


# ── Tone fetcher ─────────────────────────────────────────────────────────────

def get_tone(country_search: str, terms: str) -> tuple[float, float]:
    """
    Return (weighted_avg_tone, total_article_count) for a query over TIMESPAN.

    GDELT timelinetone response shape:
      {"timeline": [{"series": "...", "data": [{"date": ..., "value": ..., "norm": ...}]}]}

    `value` = average tone for that time bucket (roughly -10 to +5 in practice)
    `norm`  = normalised article volume (proxy for count)
    """
    query = f'"{country_search}" {terms}'
    data  = gdelt_get({
        "query":    query,
        "mode":     "timelinetone",
        "timespan": TIMESPAN,
        "format":   "json",
    })

    timeline = data.get("timeline", [])
    if not timeline:
        return 0.0, 0.0

    # The first series is "Average Tone"
    buckets = timeline[0].get("data", []) if timeline else []
    if not buckets:
        return 0.0, 0.0

    tones  = []
    norms  = []
    for b in buckets:
        try:
            tones.append(float(b["value"]))
            norms.append(float(b.get("norm", 1)))
        except (KeyError, TypeError, ValueError):
            continue

    if not tones:
        return 0.0, 0.0

    total_norm = sum(norms)
    if total_norm > 0:
        avg_tone = sum(t * n for t, n in zip(tones, norms)) / total_norm
    else:
        avg_tone = sum(tones) / len(tones)

    return round(avg_tone, 3), round(total_norm, 1)


# ── Article fetcher ───────────────────────────────────────────────────────────

def get_articles(country_search: str) -> list[dict]:
    """Fetch recent news articles for display as score reasons."""
    query = f'"{country_search}" {NEWS_TERMS}'
    data  = gdelt_get({
        "query":      query,
        "mode":       "artlist",
        "maxrecords": str(MAX_ARTS),
        "timespan":   ART_SPAN,
        "sort":       "DateDesc",
        "format":     "json",
    })

    results = []
    for art in data.get("articles", [])[:MAX_ARTS]:
        title = (art.get("title") or "").strip()
        url   = art.get("url", "")
        if not title or not url:
            continue

        # Parse seendate: "20240115T120000Z" → "2024-01-15"
        raw_date = art.get("seendate", "")
        date_str = ""
        if raw_date and len(raw_date) >= 8:
            try:
                date_str = datetime.strptime(raw_date[:8], "%Y%m%d").strftime("%Y-%m-%d")
            except ValueError:
                pass

        results.append({
            "title":  title,
            "url":    url,
            "date":   date_str,
            "source": art.get("domain", ""),
        })

    return results


# ── Scoring ───────────────────────────────────────────────────────────────────

def compute_score(
    pos_tone: float, pos_count: float,
    neg_tone: float, neg_count: float,
) -> int:
    """
    Combine tone and volume signals into a 0–100 democracy score.

    Components:
      tone_component  — how much more positive the democracy signals are vs.
                        repression signals.  Typical spread: –15 to +10.
                        Scaled so ±10 spread moves score by ±20 points.

      ratio_component — share of democracy-positive articles vs. repression
                        articles.  A 50/50 split is neutral (0 contribution).
                        A 75/25 split adds +12.5; a 25/75 split subtracts –12.5.

    Base score of 50 means "no signal either way".
    """
    total = pos_count + neg_count
    ratio = (pos_count / total) if total > 0 else 0.5

    tone_spread       = pos_tone - neg_tone           # usually –5 to +15
    tone_component    = tone_spread * 2.0             # scale to ±30 max contribution
    ratio_component   = (ratio - 0.5) * 25.0         # –12.5 to +12.5

    raw = 50.0 + tone_component + ratio_component
    return int(max(0, min(100, round(raw))))


# ── Per-country processor ─────────────────────────────────────────────────────

def process_country(key: str, info: dict) -> dict:
    name   = info["name"]
    search = info["search"]
    print(f"\n  [{key}] {name}")

    # Positive signals
    print("    → democracy signals ...", end=" ", flush=True)
    pos_tone, pos_count = get_tone(search, POS_TERMS)
    print(f"tone={pos_tone:+.2f}  vol={pos_count:.0f}")
    time.sleep(REQ_DELAY)

    # Negative signals
    print("    → repression signals ...", end=" ", flush=True)
    neg_tone, neg_count = get_tone(search, NEG_TERMS)
    print(f"tone={neg_tone:+.2f}  vol={neg_count:.0f}")
    time.sleep(REQ_DELAY)

    # Articles
    print("    → recent articles ...", end=" ", flush=True)
    articles = get_articles(search)
    print(f"{len(articles)} found")
    time.sleep(REQ_DELAY)

    score = compute_score(pos_tone, pos_count, neg_tone, neg_count)
    print(f"    ✓ score = {score}  (tone_spread={pos_tone - neg_tone:+.2f}  "
          f"ratio={pos_count / (pos_count + neg_count) * 100:.0f}% pos)"
          if (pos_count + neg_count) > 0 else f"    ✓ score = {score}")

    return {
        "name":  name,
        "score": score,
        "metrics": {
            "pos_tone":  round(pos_tone, 2),
            "neg_tone":  round(neg_tone, 2),
            "pos_count": int(pos_count),
            "neg_count": int(neg_count),
        },
        "reasons": articles,
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    now = datetime.now(timezone.utc)
    print(f"Democracy Tracker — Score Updater")
    print(f"Run date: {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 50)

    # Load existing scores as fallback in case of API failure
    existing: dict = {}
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, encoding="utf-8") as f:
            existing = json.load(f).get("countries", {})
        print(f"Loaded {len(existing)} existing scores as fallback.\n")

    output = {
        "updated":   now.strftime("%Y-%m-%d"),
        "countries": {},
    }

    errors = []
    for key, info in COUNTRIES.items():
        try:
            output["countries"][key] = process_country(key, info)
        except Exception as exc:
            print(f"\n  [ERROR] {key}: {exc}")
            errors.append(key)
            if key in existing:
                print(f"  [fallback] using previous score for {key}")
                output["countries"][key] = existing[key]

    # Persist
    os.makedirs(os.path.dirname(os.path.abspath(DATA_FILE)), exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"\nSaved → {os.path.abspath(DATA_FILE)}")

    # Summary table
    print("\n" + "=" * 50)
    print(f"{'Country':<22}  {'Score':>5}  {'Label'}")
    print("-" * 50)
    ranked = sorted(output["countries"].items(), key=lambda x: x[1]["score"], reverse=True)
    for key, d in ranked:
        label = (
            "Authoritarian"        if d["score"] <= 15 else
            "Highly Authoritarian" if d["score"] <= 35 else
            "Hybrid / Partly Free" if d["score"] <= 50 else
            "Partly Democratic"    if d["score"] <= 65 else
            "Flawed Democracy"     if d["score"] <= 80 else
            "Full Democracy"
        )
        print(f"{d['name']:<22}  {d['score']:>5}  {label}")

    if errors:
        print(f"\nWarning: {len(errors)} country/ies used fallback scores: {', '.join(errors)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
