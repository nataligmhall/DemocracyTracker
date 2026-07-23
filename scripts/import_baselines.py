#!/usr/bin/env python3
"""
Democracy Tracker — Baseline Importer

Downloads V-Dem Liberal Democracy Index and Freedom House total scores
via Our World in Data (free), then writes data/scores.json:

  baseline = 0.6 × V-Dem(0–100) + 0.4 × Freedom House(0–100)
  score    = baseline   # news_delta not yet implemented

Usage:
  python scripts/import_baselines.py
"""

from __future__ import annotations

import csv
import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Optional

import requests

# ── Config ────────────────────────────────────────────────────────────────────

ROOT = os.path.join(os.path.dirname(__file__), "..")
DATA_FILE = os.path.join(ROOT, "data", "scores.json")
ISO_FILE = os.path.join(ROOT, "data", "iso3_numeric.json")
RAW_DIR = os.path.join(ROOT, "data", "raw")

USER_AGENT = "DemocracyTracker/1.0 (https://github.com/nataligmhall/DemocracyTracker)"
TIMEOUT = 60
MIN_YEAR = 2020
VDEM_WEIGHT = 0.6
FH_WEIGHT = 0.4

VDEM_URL = (
    "https://ourworldindata.org/grapher/liberal-democracy-index.csv"
    "?v=1&csvType=full&useColumnShortNames=false"
)
FH_URL = (
    "https://ourworldindata.org/grapher/freedom-score-fh.csv"
    "?v=1&csvType=full&useColumnShortNames=false"
)

ISO3_RE = re.compile(r"^[A-Z]{3}$")

SCHEMA_VERSION = 2


# ── Helpers ───────────────────────────────────────────────────────────────────

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


def download_csv(url: str, dest_name: str) -> str:
    """Fetch a CSV and cache it under data/raw/. Return local path."""
    os.makedirs(RAW_DIR, exist_ok=True)
    dest = os.path.join(RAW_DIR, dest_name)
    print(f"  ↓ {url}")
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    resp.raise_for_status()
    with open(dest, "w", encoding="utf-8") as f:
        f.write(resp.text)
    print(f"    saved {dest} ({len(resp.text):,} bytes)")
    return dest


def latest_by_iso3(path: str, value_col: str) -> dict[str, dict]:
    """
    Return {ISO3: {name, year, value}} for the newest eligible row per country.
    Skips regional/historical OWID codes and rows older than MIN_YEAR.
    """
    out: dict[str, dict] = {}
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if value_col not in (reader.fieldnames or []):
            raise ValueError(f"{path}: missing column {value_col!r}; got {reader.fieldnames}")

        for row in reader:
            code = (row.get("Code") or "").strip()
            if not ISO3_RE.match(code):
                continue
            try:
                year = int(row["Year"])
                value = float(row[value_col])
            except (KeyError, TypeError, ValueError):
                continue
            if year < MIN_YEAR:
                continue

            prev = out.get(code)
            if prev is None or year > prev["year"]:
                out[code] = {
                    "name": (row.get("Entity") or code).strip(),
                    "year": year,
                    "value": value,
                }
    return out


def blend(
    vdem: Optional[dict],
    fh: Optional[dict],
) -> tuple[float, str, dict]:
    """
    Return (baseline, mode, components).
    mode is blend | vdem_only | fh_only.
    """
    components: dict = {}

    vdem_scaled = None
    if vdem is not None:
        vdem_scaled = max(0.0, min(100.0, vdem["value"] * 100.0))
        components["vdem"] = {
            "raw": round(vdem["value"], 4),
            "scaled": round(vdem_scaled, 2),
            "year": vdem["year"],
            "weight": VDEM_WEIGHT,
        }

    fh_scaled = None
    if fh is not None:
        fh_scaled = max(0.0, min(100.0, fh["value"]))
        components["freedom_house"] = {
            "raw": round(fh["value"], 2),
            "scaled": round(fh_scaled, 2),
            "year": fh["year"],
            "weight": FH_WEIGHT,
        }

    if vdem_scaled is not None and fh_scaled is not None:
        baseline = VDEM_WEIGHT * vdem_scaled + FH_WEIGHT * fh_scaled
        mode = "blend"
    elif vdem_scaled is not None:
        baseline = vdem_scaled
        mode = "vdem_only"
        components["vdem"]["weight"] = 1.0
    elif fh_scaled is not None:
        baseline = fh_scaled
        mode = "fh_only"
        components["freedom_house"]["weight"] = 1.0
    else:
        raise ValueError("no baseline inputs")

    return round(baseline, 2), mode, components


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    now = datetime.now(timezone.utc)
    print("Democracy Tracker — Baseline Importer")
    print(f"Run date: {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 56)

    print("\nDownloading source data (Our World in Data)…")
    try:
        vdem_path = download_csv(VDEM_URL, "vdem_libdem.csv")
        fh_path = download_csv(FH_URL, "fh_freedom_score.csv")
    except requests.RequestException as exc:
        print(f"\n[ERROR] download failed: {exc}", file=sys.stderr)
        return 1

    print("\nParsing…")
    vdem = latest_by_iso3(vdem_path, "Liberal democracy index")
    fh = latest_by_iso3(fh_path, "Total democracy score")
    print(f"  V-Dem countries (≥{MIN_YEAR}): {len(vdem)}")
    print(f"  Freedom House countries (≥{MIN_YEAR}): {len(fh)}")

    with open(ISO_FILE, encoding="utf-8") as f:
        iso3_to_numeric: dict[str, int] = json.load(f)
    print(f"  ISO numeric codes loaded: {len(iso3_to_numeric)}")

    codes = sorted(set(vdem) | set(fh))
    countries: dict[str, dict] = {}
    mode_counts = {"blend": 0, "vdem_only": 0, "fh_only": 0}
    missing_iso = []

    # Preserve weekly news layer across baseline refreshes
    previous: dict = {}
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, encoding="utf-8") as f:
                previous = json.load(f).get("countries") or {}
        except (OSError, json.JSONDecodeError):
            previous = {}

    for code in codes:
        v = vdem.get(code)
        f = fh.get(code)
        name = (v or f)["name"]
        baseline, mode, components = blend(v, f)
        mode_counts[mode] += 1

        # Prefer FH naming when both exist and differ slightly; keep V-Dem if FH absent
        if f is not None:
            name = f["name"]
        elif v is not None:
            name = v["name"]

        prev = previous.get(code) or {}
        news_delta = float(prev.get("news_delta", 0) or 0)
        evidence = prev.get("evidence") if isinstance(prev.get("evidence"), list) else []
        score = max(0, min(100, round(baseline + news_delta)))
        iso_numeric = iso3_to_numeric.get(code)
        if iso_numeric is None:
            missing_iso.append(code)

        countries[code] = {
            "name": name,
            "iso3": code,
            "iso_numeric": iso_numeric,
            "baseline": baseline,
            "news_delta": news_delta,
            "score": score,
            "label": regime_label(score),
            "baseline_mode": mode,
            "components": components,
            "evidence": evidence,
        }

    if missing_iso:
        print(f"  [warn] no ISO numeric for: {', '.join(missing_iso)}")

    baseline_years = sorted(
        {
            c["components"][src]["year"]
            for c in countries.values()
            for src in c["components"]
        }
    )

    output = {
        "schema_version": SCHEMA_VERSION,
        "updated": now.strftime("%Y-%m-%d"),
        "methodology": {
            "model": "baseline_plus_news_delta",
            "formula": "score = clamp(baseline + news_delta, 0, 100)",
            "baseline": {
                "vdem_weight": VDEM_WEIGHT,
                "freedom_house_weight": FH_WEIGHT,
                "min_year": MIN_YEAR,
                "years_present": baseline_years,
                "sources": {
                    "vdem": {
                        "producer": "V-Dem Institute",
                        "indicator": "Liberal democracy index (v2x_libdem)",
                        "scale": "0–1 (scaled ×100)",
                        "via": "Our World in Data",
                        "url": "https://ourworldindata.org/grapher/liberal-democracy-index",
                    },
                    "freedom_house": {
                        "producer": "Freedom House",
                        "indicator": "Total democracy score (Political Rights + Civil Liberties)",
                        "scale": "0–100",
                        "via": "Our World in Data",
                        "url": "https://ourworldindata.org/grapher/freedom-score-fh",
                    },
                },
            },
            "news_delta": {
                "status": "active",
                "cap": 5,
                "decay": 0.5,
                "note": "Preserved from weekly updater when present; see METHODOLOGY.md",
            },
        },
        "countries": countries,
    }

    os.makedirs(os.path.dirname(os.path.abspath(DATA_FILE)), exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"\nSaved → {os.path.abspath(DATA_FILE)}")
    print(f"Countries: {len(countries)}")
    print(
        f"Modes: blend={mode_counts['blend']}  "
        f"vdem_only={mode_counts['vdem_only']}  "
        f"fh_only={mode_counts['fh_only']}"
    )

    ranked = sorted(countries.values(), key=lambda c: c["score"], reverse=True)
    print("\nTop 5")
    for c in ranked[:5]:
        print(f"  {c['score']:3d}  {c['name']:<28}  {c['label']}  [{c['baseline_mode']}]")
    print("Bottom 5")
    for c in ranked[-5:]:
        print(f"  {c['score']:3d}  {c['name']:<28}  {c['label']}  [{c['baseline_mode']}]")

    # Spot-check familiar countries
    print("\nSpot checks")
    for code in ("NOR", "USA", "HUN", "IND", "RUS", "CHN", "AND"):
        c = countries.get(code)
        if not c:
            print(f"  {code}: (missing)")
            continue
        print(
            f"  {code}: score={c['score']} baseline={c['baseline']} "
            f"mode={c['baseline_mode']} label={c['label']}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
