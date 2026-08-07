# The Hundred Men's — Live Analyst Dashboard

This version fixes the earlier architecture: **all-time means cumulative through the latest successful refresh**, including the current season. It is not frozen at the end of 2025.

## Why Phil Salt was wrong before

The earlier file used an end-2025 career snapshot (Phil Salt 1,138) alongside a separate 2026 view. That was fine for a historical baseline but wrong for a dashboard labelled "all-time". The new bootstrap records Salt at **1,294** from the ESPNcricinfo record page supplied on 7 August 2026, and the updater rejects a batting table that regresses below that known threshold.

## Data sources

Career batting source of truth:
`https://www.espncricinfo.com/records/trophy/batting-most-runs-career/the-hundred-men-s-competition-826`

Career bowling:
`https://www.espncricinfo.com/records/trophy/bowling-most-wickets-career/the-hundred-men-s-competition-826`

2026 standings:
`https://www.cricbuzz.com/cricket-series/11493/the-hundred-mens-competition-2026/points-table`

## Make it shareably live with GitHub Pages

1. Create a new GitHub repository.
2. Upload the contents of this folder **including `.github/workflows/refresh-and-deploy.yml`**.
3. Make sure the default branch is `main`.
4. In GitHub: **Settings → Pages → Source → GitHub Actions**.
5. Open **Actions** and manually run **Refresh and deploy live dashboard** once.
6. GitHub Pages will give you a public URL you can share.

The workflow also runs every 10 minutes. GitHub scheduled jobs are *near-live*, not guaranteed to execute exactly on the minute.

## Local preview

Because the dashboard fetches `data/current.json`, use a tiny local web server rather than double-clicking the file:

```bash
python -m http.server 8000
```

Then open `http://localhost:8000`.

## Data safety

- Each source has `last_success` and `status`.
- ESPN refresh first tries the modern Records endpoint, then ESPN's legacy records engine as a fallback.
- If a refresh fails, the script preserves the previous verified values.
- The UI displays a visible stale/partial warning.
- The batting parser has a regression guard: after 7 Aug 2026 it will not accept an ESPN table whose leader is below 1,294.
- Player search operates over **all rows returned by ESPN**, not a manually maintained top-10 list.

## True ball-by-ball live

A ten-minute static refresh is appropriate for a shareable analytics dashboard and career records. For true delivery-by-delivery live scores/predictions, use a licensed cricket data API or backend feed. The front end can remain the same; replace `scripts/refresh_data.py` with the licensed feed and update `data/current.json`.

## Next analyst layer

For match prediction, keep source responsibilities separate:
- ESPN career records → current player career baselines.
- Current competition source → standings, squads, form, completed results.
- Cricsheet → historical ball-by-ball H2H, venue, phase, batter-v-bowler and chase/defend features.
- Licensed live feed → toss, XI, injuries/availability and ball-by-ball state when true live prediction is required.
