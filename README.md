# Democracy Tracker

Serious, open research tool that tracks democracy scores for countries worldwide.

```
score = clamp(baseline + news_delta, 0, 100)
```

- **Baseline** (annual): 60% [V-Dem](https://www.v-dem.net/) Liberal Democracy Index + 40% [Freedom House](https://freedomhouse.org/) total score (via [Our World in Data](https://ourworldindata.org/))
- **News delta** (weekly, planned): evidence from free trusted news sources, shown alongside the score

See [METHODOLOGY.md](METHODOLOGY.md) for the full model.

## Data

| File | Description |
|------|-------------|
| `data/scores.json` | Current scores (schema v2) |
| `data/news_sources.json` | Free RSS allowlist |
| `data/iso3_numeric.json` | ISO3 → numeric map for the world map |
| `scripts/import_baselines.py` | Regenerates V-Dem + FH baselines |
| `scripts/update_news_delta.py` | Weekly free-RSS news deltas + evidence |

```bash
pip install -r requirements.txt
python scripts/import_baselines.py
python scripts/update_news_delta.py
```

## Status

| Step | Status |
|------|--------|
| Schema + methodology | Done |
| Baseline import (all countries) | Done |
| Map UI for new schema | Done |
| Free news delta pipeline | Done |
| Evidence panel | Done (from `scores.json`) |
