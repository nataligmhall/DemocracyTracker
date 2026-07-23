# Methodology

Democracy Tracker publishes a **0–100 democracy score** for each country.

```
score = clamp(baseline + news_delta, 0, 100)
```

| Layer | Cadence | Role |
|-------|---------|------|
| **Baseline** | Annual (refreshed monthly) | Research-backed floor from established indices |
| **News delta** | Weekly | Evidence-based nudge from trusted free news sources |
| **Displayed score** | Weekly | Baseline plus capped, decaying news pressure |

## Baseline (60% V-Dem + 40% Freedom House)

```
vdem_scaled = V-Dem Liberal Democracy Index × 100   # original range 0–1
fh_scaled   = Freedom House total democracy score     # already 0–100

baseline = 0.6 × vdem_scaled + 0.4 × fh_scaled
```

**Fallbacks**

- If Freedom House is missing for a country/year → **V-Dem only**
- If V-Dem is missing → **Freedom House only**
- Rows older than 2020, regional aggregates, and historical entities are excluded

**Sources** (free, via [Our World in Data](https://ourworldindata.org/))

| Source | Indicator | OWID chart |
|--------|-----------|------------|
| V-Dem | Liberal democracy index (`v2x_libdem`) | [liberal-democracy-index](https://ourworldindata.org/grapher/liberal-democracy-index) |
| Freedom House | Total democracy score (PR + CL, 0–100) | [freedom-score-fh](https://ourworldindata.org/grapher/freedom-score-fh) |

Cite the original producers (V-Dem Institute; Freedom House) and OWID’s processing when using this data.

## News delta (weekly)

1. Fetch free allowlisted RSS feeds in [`data/news_sources.json`](data/news_sources.json), prioritizing **Reuters, AP, and AFP** (via Google News RSS filters), plus BBC, DW, UN News, and HRW.
2. Keep articles from the last **30 days** that match democracy / governance topics.
3. Match countries by official name + aliases (with guards for ambiguous names like Georgia).
4. Assign a small signed weight per story (`pro-democracy` ≈ +1, `erosion` ≈ −1, mixed ±0.5). Split across countries if several are named.
5. **Wire enrichment:** countries still short on headlines get a per-country Reuters Google News query.
6. The UI prefers wire-service stories (Reuters / AP / AFP) when presenting coverage.
7. Update each country:

```
week_impact = clamp(sum(weights), -5, +5)
news_delta  = clamp(prev_delta × 0.5 + week_impact, -5, +5)
score       = clamp(round(baseline + news_delta), 0, 100)
```

8. Store up to **10** matched articles in `evidence` — the same list the UI shows (wire services preferred).

No paid APIs. Baseline refreshes preserve existing `news_delta` and `evidence`. Guardian / NYT opinion presses are not used for displayed coverage.

## Regime labels

Labels are descriptive bands on the 0–100 score (inspired by V-Dem’s regime typology; not a separate official classification):

| Score | Label | Meaning |
|------:|-------|---------|
| ≥ 80 | Liberal Democracy | Free elections **plus** strong rights, rule of law, and checks on power |
| ≥ 60 | Electoral Democracy | Multiparty elections and basic political freedoms; liberal constraints weaker |
| ≥ 40 | Hybrid Regime | Mix of democratic and authoritarian traits; flawed or uneven competition |
| ≥ 20 | Electoral Autocracy | Elections exist but are not free/fair enough to change who holds power |
| < 20 | Closed Autocracy | No meaningful multiparty contest for executive power |

**Democracy vs republic:** A republic is a form of government (no monarch; law-bound, usually representative institutions). Democracy grades whether people can choose and constrain those in power. The two are compatible—e.g. the United States is a constitutional republic *and* scored here as an electoral democracy. We keep the democracy-quality label (not “Constitutional Republic”) as the primary tag so countries stay comparable: France, Germany, and Brazil are also republics, yet fall in different score bands. The Electoral College is an indirect election: voters in each state choose electors, who then cast the formal presidential ballots.


## Commands

```bash
pip install -r requirements.txt
python scripts/import_baselines.py    # refresh V-Dem + FH baselines
python scripts/update_news_delta.py   # weekly free-RSS news deltas
```
