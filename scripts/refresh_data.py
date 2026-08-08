from __future__ import annotations

import json
import re
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "data" / "current.json"

CRICSHEET_HUNDRED = "https://cricsheet.org/downloads/hnd_json.zip"
CRICSHEET_LOCAL_CANDIDATES = [ROOT / "data" / "hnd_json.zip", ROOT / "hnd_json.zip"]
CRICBUZZ_TABLE = "https://www.cricbuzz.com/cricket-series/11493/the-hundred-mens-competition-2026/points-table"
CRICBUZZ_BASE = "https://www.cricbuzz.com"
IPL_HUNDRED_SERIES = "https://www.ipl.com/matches/the-hundred-mens-129912"
IPL_MATCH_ID_BASE = 95162

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "en-GB,en;q=0.9",
    "Cache-Control": "no-cache",
}

TEAM_NAMES = {
    "TRE": "Trent Rockets",
    "MIL": "MI London",
    "WEF": "Welsh Fire",
    "MSG": "Manchester Super Giants",
    "SUL": "SunRisers Leeds",
    "SOU": "Southern Brave",
    "LDN": "London Spirit",
    "BRM": "Birmingham Phoenix",
}

IPL_TEAM_SLUGS = {
    "TRE": "trent-rockets-men",
    "MIL": "mi-london-men",
    "WEF": "welsh-fire-men",
    "MSG": "manchester-super-giants-men",
    "SUL": "sunrisers-leeds-men",
    "SOU": "southern-brave-men",
    "LDN": "london-spirit-men",
    "BRM": "birmingham-phoenix-men",
}

TEAM_ALIASES = {
    "Trent Rockets": "TRE",
    "Oval Invincibles": "MIL",
    "MI London": "MIL",
    "Welsh Fire": "WEF",
    "Manchester Originals": "MSG",
    "Manchester Super Giants": "MSG",
    "Northern Superchargers": "SUL",
    "Sunrisers Leeds": "SUL",
    "SunRisers Leeds": "SUL",
    "Southern Brave": "SOU",
    "London Spirit": "LDN",
    "Birmingham Phoenix": "BRM",
}

NON_BOWLER_WICKETS = {
    "run out", "retired hurt", "retired out", "obstructing the field"
}

PHASE_LABELS = ["1–25", "26–50", "51–75", "76–100"]

def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

def get(url, timeout=35):
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r

def clean_role(name):
    name = str(name)
    name = re.sub(
        r"\s*\([^)]*(?:\bc\b|\bwk\b)[^)]*\)\s*",
        " ",
        name,
        flags=re.I,
    )
    name = re.sub(r"\s+", " ", name).strip(" ,")
    return name

def person_key(name):
    n = clean_role(name)
    n = re.sub(r"[^A-Za-z0-9' -]", "", n).strip()
    parts = [p for p in n.split() if p]
    if not parts:
        return ""
    surname = re.sub(r"[^a-z0-9]", "", parts[-1].lower())
    first = re.sub(r"[^a-z0-9]", "", parts[0].lower())
    return first[:1] + surname

def team_code(name):
    return TEAM_ALIASES.get(str(name).strip())

def new_player():
    return {
        "display": "",
        "matches": 0,
        "innings": 0,
        "runs": 0,
        "balls": 0,
        "dismissals": 0,
        "fours": 0,
        "sixes": 0,
        "high_score": 0,
        "not_out_high": False,
        "bowling_innings": 0,
        "balls_bowled": 0,
        "runs_conceded": 0,
        "wickets": 0,
        "best_wickets": 0,
        "best_runs": 9999,
    }

def ensure(stats, display_names, name, prefer_name=False):
    key = person_key(name)
    if not key:
        return None, None
    rec = stats.setdefault(key, new_player())
    clean = clean_role(name)
    current = display_names.get(key, "")
    if prefer_name or not current or len(clean) > len(current):
        display_names[key] = clean
    if prefer_name or not rec["display"] or len(clean) > len(rec["display"]):
        rec["display"] = clean
    return key, rec

def add_match_appearance(stats, display_names, name, seen, prefer_name=False):
    key, rec = ensure(stats, display_names, name, prefer_name=prefer_name)
    if key and key not in seen:
        rec["matches"] += 1
        seen.add(key)

def add_batting_innings(stats, display_names, rows, prefer_name=False):
    for row in rows:
        key, rec = ensure(stats, display_names, row["name"], prefer_name=prefer_name)
        if not key:
            continue
        rec["innings"] += 1
        rec["runs"] += int(row["runs"])
        rec["balls"] += int(row["balls"])
        rec["fours"] += int(row.get("fours", 0))
        rec["sixes"] += int(row.get("sixes", 0))
        if row.get("dismissed", False):
            rec["dismissals"] += 1
        score = int(row["runs"])
        not_out = not row.get("dismissed", False)
        if score > rec["high_score"] or (score == rec["high_score"] and not_out):
            rec["high_score"] = score
            rec["not_out_high"] = not_out

def add_bowling_innings(stats, display_names, rows, prefer_name=False):
    for row in rows:
        key, rec = ensure(stats, display_names, row["name"], prefer_name=prefer_name)
        if not key:
            continue
        rec["bowling_innings"] += 1
        rec["balls_bowled"] += int(row["balls"])
        rec["runs_conceded"] += int(row["runs"])
        rec["wickets"] += int(row["wickets"])
        w, r = int(row["wickets"]), int(row["runs"])
        if w > rec["best_wickets"] or (w == rec["best_wickets"] and r < rec["best_runs"]):
            rec["best_wickets"], rec["best_runs"] = w, r

def canonical_pair(a, b):
    return tuple(sorted((a, b)))

def add_h2h(h2h, a, b, winner=None, result=None):
    if not a or not b or a == b:
        return
    x, y = canonical_pair(a, b)
    rec = h2h.setdefault((x, y), {
        "a": x, "b": y, "games": 0, "a_wins": 0, "b_wins": 0, "ties": 0, "no_results": 0
    })
    rec["games"] += 1
    if winner == x:
        rec["a_wins"] += 1
    elif winner == y:
        rec["b_wins"] += 1
    elif result == "tie":
        rec["ties"] += 1
    else:
        rec["no_results"] += 1

def innings_totals(innings):
    runs = 0
    wickets = 0
    legal = 0
    for over in innings.get("overs", []) or []:
        for d in over.get("deliveries", []) or []:
            runs += int((d.get("runs", {}) or {}).get("total", 0) or 0)
            extras = d.get("extras", {}) or {}
            if "wides" not in extras and "noballs" not in extras:
                legal += 1
            wickets += len(d.get("wickets", []) or [])
    return runs, wickets, legal

def aggregate_cricsheet():
    archive = next((x for x in CRICSHEET_LOCAL_CANDIDATES if x.exists()), None)
    if archive is None:
        raise RuntimeError("Missing hnd_json.zip in repository root or data/.")
    if archive.stat().st_size < 500_000:
        raise RuntimeError("hnd_json.zip is unexpectedly small.")

    stats = {}
    display_names = {}
    historical_h2h = {}
    player_matchups = defaultdict(lambda: {
        "balls": 0, "runs": 0, "dots": 0, "fours": 0, "sixes": 0, "dismissals": 0
    })
    phase = defaultdict(lambda: [defaultdict(int) for _ in range(4)])
    venue_team = defaultdict(lambda: defaultdict(lambda: {"matches": 0, "wins": 0}))
    venue_summary = defaultdict(lambda: {
        "matches": 0, "first_innings_runs": 0, "chase_wins": 0, "defend_wins": 0, "other": 0
    })
    latest_bbb_date = None
    historical_count = 0
    total_mens_matches = 0

    with zipfile.ZipFile(archive) as z:
        for filename in z.namelist():
            if not filename.endswith(".json"):
                continue
            try:
                obj = json.loads(z.read(filename))
            except Exception:
                continue

            info = obj.get("info", {}) or {}
            if str(info.get("gender", "")).lower() not in {"male", "men"}:
                continue
            event_name = str((info.get("event", {}) or {}).get("name", ""))
            if "Hundred" not in event_name:
                continue

            teams_raw = info.get("teams", []) or []
            if len(teams_raw) != 2:
                continue
            codes = [team_code(t) for t in teams_raw]
            if not all(codes):
                continue

            total_mens_matches += 1
            season = info.get("season")
            try:
                season_num = int(str(season)[:4])
            except Exception:
                continue

            dates = info.get("dates", []) or []
            date_str = str(dates[0]) if dates else None
            if date_str and (latest_bbb_date is None or date_str > latest_bbb_date):
                latest_bbb_date = date_str

            # Maintain display-name lookup for every Cricsheet match, including
            # the available 2026 ball-by-ball portion.
            for team, plist in (info.get("players", {}) or {}).items():
                for player in plist or []:
                    key = person_key(player)
                    if key:
                        display_names.setdefault(key, clean_role(player))

            # Team/venue and player matchup analytics use all ball-by-ball
            # currently present in the uploaded Cricsheet archive.
            venue = str(info.get("venue", "Unknown venue"))
            winner_raw = (info.get("outcome", {}) or {}).get("winner")
            winner_code = team_code(winner_raw) if winner_raw else None
            result = str((info.get("outcome", {}) or {}).get("result", "")).lower()
            venue_summary[venue]["matches"] += 1
            for c in codes:
                venue_team[venue][c]["matches"] += 1
                if c == winner_code:
                    venue_team[venue][c]["wins"] += 1

            inn_list = obj.get("innings", []) or []
            if inn_list:
                first_runs, _, _ = innings_totals(inn_list[0])
                venue_summary[venue]["first_innings_runs"] += first_runs
                if winner_code:
                    second_team = team_code(inn_list[1].get("team")) if len(inn_list) > 1 else None
                    if winner_code == second_team:
                        venue_summary[venue]["chase_wins"] += 1
                    else:
                        venue_summary[venue]["defend_wins"] += 1
                else:
                    venue_summary[venue]["other"] += 1

            for innings in inn_list:
                batting_code = team_code(innings.get("team"))
                legal_ball = 0
                for over in innings.get("overs", []) or []:
                    for d in over.get("deliveries", []) or []:
                        batter = d.get("batter")
                        bowler = d.get("bowler")
                        runs_obj = d.get("runs", {}) or {}
                        br = int(runs_obj.get("batter", 0) or 0)
                        total = int(runs_obj.get("total", 0) or 0)
                        extras = d.get("extras", {}) or {}

                        if batter and bowler:
                            bk, wk = person_key(batter), person_key(bowler)
                            if bk and wk:
                                m = player_matchups[(bk, wk)]
                                if "wides" not in extras:
                                    m["balls"] += 1
                                m["runs"] += br
                                if "wides" not in extras and total == 0:
                                    m["dots"] += 1
                                if br == 4:
                                    m["fours"] += 1
                                if br == 6:
                                    m["sixes"] += 1
                                for w in d.get("wickets", []) or []:
                                    if (
                                        person_key(w.get("player_out", "")) == bk
                                        and str(w.get("kind", "")).lower() not in NON_BOWLER_WICKETS
                                    ):
                                        m["dismissals"] += 1

                        if batting_code:
                            # Phase progression uses legal deliveries; total runs
                            # still include extras on the delivery.
                            phase_index = min(3, legal_ball // 25)
                            phase[batting_code][phase_index]["runs"] += total
                            for w in d.get("wickets", []) or []:
                                phase[batting_code][phase_index]["wickets"] += 1
                            if "wides" not in extras and "noballs" not in extras:
                                phase[batting_code][phase_index]["balls"] += 1
                                legal_ball += 1

            # Career totals and historical team H2H are frozen at end-2025;
            # 2026 is layered from current scorecards to avoid double-counting.
            if season_num > 2025:
                continue

            historical_count += 1
            outcome = info.get("outcome", {}) or {}
            add_h2h(
                historical_h2h,
                codes[0], codes[1],
                winner=team_code(outcome.get("winner")) if outcome.get("winner") else None,
                result=str(outcome.get("result", "")).lower(),
            )

            seen = set()
            for _, plist in (info.get("players", {}) or {}).items():
                for player in plist or []:
                    add_match_appearance(stats, display_names, player, seen)

            for innings in inn_list:
                bat_rows = defaultdict(lambda: {
                    "runs": 0, "balls": 0, "fours": 0, "sixes": 0, "dismissed": False
                })
                bowl_rows = defaultdict(lambda: {"balls": 0, "runs": 0, "wickets": 0})

                for over in innings.get("overs", []) or []:
                    for d in over.get("deliveries", []) or []:
                        batter = d.get("batter")
                        bowler = d.get("bowler")
                        runs_obj = d.get("runs", {}) or {}
                        extras = d.get("extras", {}) or {}

                        if batter:
                            b = bat_rows[batter]
                            br = int(runs_obj.get("batter", 0) or 0)
                            b["runs"] += br
                            if "wides" not in extras:
                                b["balls"] += 1
                            b["fours"] += int(br == 4)
                            b["sixes"] += int(br == 6)

                        if bowler:
                            bw = bowl_rows[bowler]
                            if "wides" not in extras and "noballs" not in extras:
                                bw["balls"] += 1
                            total = int(runs_obj.get("total", 0) or 0)
                            non_bowler = (
                                int(extras.get("byes", 0) or 0)
                                + int(extras.get("legbyes", 0) or 0)
                                + int(extras.get("penalty", 0) or 0)
                            )
                            bw["runs"] += max(0, total - non_bowler)

                        for w in d.get("wickets", []) or []:
                            out = w.get("player_out")
                            kind = str(w.get("kind", "")).lower()
                            if out:
                                bat_rows[out]["dismissed"] = kind != "retired hurt"
                            if bowler and kind not in NON_BOWLER_WICKETS:
                                bowl_rows[bowler]["wickets"] += 1

                add_batting_innings(
                    stats, display_names,
                    [{"name": n, **r} for n, r in bat_rows.items()]
                )
                add_bowling_innings(
                    stats, display_names,
                    [{"name": n, **r} for n, r in bowl_rows.items()]
                )

    if historical_count < 150:
        raise RuntimeError(f"Historical men's sample too small: {historical_count}")

    return {
        "stats": stats,
        "display_names": display_names,
        "historical_h2h": historical_h2h,
        "player_matchups": player_matchups,
        "phase": phase,
        "venue_team": venue_team,
        "venue_summary": venue_summary,
        "latest_bbb_date": latest_bbb_date,
        "historical_count": historical_count,
        "total_mens_matches": total_mens_matches,
    }

def get_2026_scorecard_urls():
    html = get(CRICBUZZ_TABLE).text
    hrefs = re.findall(
        r'href=["\']([^"\']*/live-cricket-(?:scores|scorecard)/\d+/[^"\']*the-hundred-mens-competition-2026[^"\']*)["\']',
        html,
        flags=re.I,
    )
    urls, seen = [], set()
    for href in hrefs:
        full = href if href.startswith("http") else CRICBUZZ_BASE + (href if href.startswith("/") else "/" + href)
        full = full.replace("/live-cricket-scores/", "/live-cricket-scorecard/")
        mid = re.search(r"/live-cricket-scorecard/(\d+)/", full)
        if mid and mid.group(1) not in seen:
            seen.add(mid.group(1))
            urls.append(full)
    if len(urls) < 20:
        raise RuntimeError(f"Only found {len(urls)} unique 2026 scorecard links.")
    return urls

def split_scorecard_sections(lines):
    batting, bowling = [], []
    i = 0

    def is_int(x):
        return bool(re.fullmatch(r"\d+", str(x).strip()))

    def is_num(x):
        return bool(re.fullmatch(r"\d+(?:\.\d+)?", str(x).strip().replace("−", "-")))

    def find_five(start, lookahead=16):
        upper = min(len(lines) - 4, start + lookahead)
        for k in range(start, upper):
            vals = lines[k:k+5]
            if (
                len(vals) == 5
                and all(is_int(v) for v in vals[:4])
                and is_num(vals[4])
            ):
                return k
        return None

    while i < len(lines):
        if (
            lines[i] == "Batter"
            and i + 5 < len(lines)
            and lines[i+1:i+6] == ["R", "B", "4s", "6s", "SR"]
        ):
            i += 6
            rows = []
            while i < len(lines) and lines[i] not in {"Extras", "Bowler", "Total", "Did not Bat", "INFO"}:
                raw_name = lines[i].strip()
                numeric_at = find_five(i + 1)
                if not raw_name or numeric_at is None:
                    i += 1
                    continue
                name = clean_role(raw_name)
                dismissal = clean_role(" ".join(lines[i+1:numeric_at]))
                try:
                    runs, balls, fours, sixes = map(int, lines[numeric_at:numeric_at+4])
                    float(lines[numeric_at+4].replace("−", "-"))
                except Exception:
                    i += 1
                    continue
                d = dismissal.lower()
                rows.append({
                    "name": name,
                    "runs": runs,
                    "balls": balls,
                    "fours": fours,
                    "sixes": sixes,
                    "dismissed": not (
                        "not out" in d or "retired hurt" in d or "absent hurt" in d
                    ),
                })
                i = numeric_at + 5
            if rows:
                batting.append(rows)
            continue

        if (
            lines[i] == "Bowler"
            and i + 5 < len(lines)
            and lines[i+1:i+6] == ["B", "D", "R", "W", "RPB"]
        ):
            i += 6
            rows = []
            while i < len(lines) and lines[i] not in {"Fall of Wickets", "Batter", "INFO", "Partnerships"}:
                raw_name = lines[i].strip()
                numeric_at = find_five(i + 1, 10)
                if not raw_name or numeric_at is None:
                    i += 1
                    continue
                name = clean_role(raw_name)
                try:
                    balls = int(lines[numeric_at])
                    runs = int(lines[numeric_at+2])
                    wickets = int(lines[numeric_at+3])
                    float(lines[numeric_at+4].replace("−", "-"))
                except Exception:
                    i += 1
                    continue
                rows.append({
                    "name": name, "balls": balls, "runs": runs, "wickets": wickets
                })
                i = numeric_at + 5
            if rows:
                bowling.append(rows)
            continue

        i += 1

    return batting, bowling

def parse_url_codes(url):
    m = re.search(r"/\d+/([a-z]+)-vs-([a-z]+)-", url, flags=re.I)
    if not m:
        return None, None
    a, b = m.group(1).upper(), m.group(2).upper()
    return (a if a in TEAM_NAMES else None), (b if b in TEAM_NAMES else None)

def parse_match_meta(url, lines):
    a, b = parse_url_codes(url)

    result_line = next(
        (
            x for x in lines[:180]
            if " won by " in x.lower()
            or x.lower() in {"match tied", "no result", "match abandoned"}
        ),
        "",
    )
    winner = None
    for code, name in TEAM_NAMES.items():
        if result_line.lower().startswith(name.lower()):
            winner = code
            break

    result_type = None
    low = result_line.lower()
    if "tied" in low:
        result_type = "tie"
    elif "no result" in low or "abandoned" in low:
        result_type = "no result"

    venue = None
    for i, x in enumerate(lines):
        if x == "Venue" and i + 1 < len(lines):
            venue = lines[i+1]
            break
    if not venue:
        # Top-of-page token can appear as "Venue:".
        for i, x in enumerate(lines[:160]):
            if x.rstrip(":") == "Venue" and i + 1 < len(lines):
                venue = lines[i+1]
                break

    date = None
    for i, x in enumerate(lines):
        if x == "Date" and i + 1 < len(lines):
            raw = lines[i+1]
            try:
                date = datetime.strptime(raw + " 2026", "%A, %B %d %Y").date().isoformat()
            except Exception:
                pass
            break

    scores = {}
    for i, x in enumerate(lines[:-1]):
        if x in TEAM_NAMES and re.match(r"^\d+(?:-\d+)?\s*\(\d+", lines[i+1]):
            m = re.match(r"^(\d+)(?:-(\d+))?\s*\((\d+)", lines[i+1])
            if m:
                scores[x] = {
                    "runs": int(m.group(1)),
                    "wickets": int(m.group(2) or 10),
                    "balls": int(m.group(3)),
                }

    return {
        "a": a, "b": b, "winner": winner, "result_type": result_type,
        "result": result_line, "venue": venue, "date": date, "scores": scores,
    }

def parse_squads(lines):
    squads = {}
    for i, token in enumerate(lines):
        if not token.endswith(" squad"):
            continue
        code = team_code(token[:-6].strip())
        if not code:
            continue
        try:
            p = lines.index("Players", i, min(len(lines), i + 10))
        except ValueError:
            continue
        names = []
        j = p + 1
        while j < len(lines) and lines[j] not in {"Bench", "Support Staff"}:
            n = clean_role(lines[j].rstrip(","))
            if n and not re.fullmatch(r"\d+|\d+:\d+.*", n):
                names.append(n)
            j += 1
        squads[code] = names
    return squads

def aggregate_cricbuzz_2026(base):
    stats = base["stats"]
    display_names = base["display_names"]
    team_h2h = dict(base["historical_h2h"])
    urls = get_2026_scorecard_urls()

    parsed = 0
    team_form = defaultdict(list)
    player_recent = defaultdict(list)
    current_team = {}
    current_match_summaries = []

    for url in urls:
        html = get(url).text
        soup = BeautifulSoup(html, "html.parser")
        lines = [re.sub(r"\s+", " ", x).strip() for x in soup.stripped_strings]
        batting, bowling = split_scorecard_sections(lines)
        if not batting:
            continue
        parsed += 1

        meta = parse_match_meta(url, lines)
        squads = parse_squads(lines)

        match_seen = set()
        for code, names in squads.items():
            for name in names:
                key, _ = ensure(stats, display_names, name, prefer_name=True)
                if key:
                    current_team[key] = code
                    match_seen.add(key)

        match_player = defaultdict(dict)

        for inn in batting:
            for r in inn:
                key, _ = ensure(stats, display_names, r["name"], prefer_name=True)
                if key:
                    match_seen.add(key)
                    match_player[key]["batting"] = dict(r)
            add_batting_innings(stats, display_names, inn, prefer_name=True)

        for inn in bowling:
            for r in inn:
                key, _ = ensure(stats, display_names, r["name"], prefer_name=True)
                if key:
                    match_seen.add(key)
                    match_player[key]["bowling"] = dict(r)
            # Career bowling is deliberately NOT added here.
            # Cricbuzz's current bowling table markup has proven unstable.
            # A separate IPL.com scorecard pass below is the source of truth
            # for all 2026 bowling figures.

        for key in match_seen:
            stats[key]["matches"] += 1

        if meta["a"] and meta["b"] and (meta["winner"] or meta["result_type"]):
            add_h2h(
                team_h2h, meta["a"], meta["b"],
                winner=meta["winner"], result=meta["result_type"],
            )

            for code, opp in ((meta["a"], meta["b"]), (meta["b"], meta["a"])):
                if meta["winner"] == code:
                    outcome = "W"
                elif meta["winner"] == opp:
                    outcome = "L"
                elif meta["result_type"] == "tie":
                    outcome = "T"
                else:
                    outcome = "NR"
                team_form[code].append({
                    "date": meta["date"], "opponent": opp, "result": outcome,
                    "venue": meta["venue"], "match_result": meta["result"],
                })

        for key, rec in match_player.items():
            code = current_team.get(key)
            opp = None
            if code == meta["a"]:
                opp = meta["b"]
            elif code == meta["b"]:
                opp = meta["a"]
            player_recent[key].append({
                "date": meta["date"],
                "team": code,
                "opponent": opp,
                "batting": rec.get("batting"),
                "bowling": rec.get("bowling"),
                "result": meta["result"],
            })

        current_match_summaries.append(meta)

    if parsed < 20:
        raise RuntimeError(f"Only {parsed} 2026 scorecards contained match data.")

    for rows in team_form.values():
        rows.sort(key=lambda x: x.get("date") or "")
    for rows in player_recent.values():
        rows.sort(key=lambda x: x.get("date") or "")

    return {
        "parsed": parsed,
        "discovered": len(urls),
        "team_h2h": team_h2h,
        "team_form": team_form,
        "player_recent": player_recent,
        "current_team": current_team,
        "match_summaries": current_match_summaries,
    }

def parse_cricbuzz_match_number_and_codes(url):
    """
    Cricbuzz result URL example:
      .../sul-vs-brm-24th-match-the-hundred-mens-competition-2026
    """
    m = re.search(
        r"/([a-z]+)-vs-([a-z]+)-(\d+)(?:st|nd|rd|th)-match-",
        url,
        flags=re.I,
    )
    if not m:
        return None
    a, b, num = m.group(1).upper(), m.group(2).upper(), int(m.group(3))
    if a not in TEAM_NAMES or b not in TEAM_NAMES:
        return None
    return a, b, num


def build_ipl_scorecard_url(cricbuzz_url):
    parsed = parse_cricbuzz_match_number_and_codes(cricbuzz_url)
    if not parsed:
        return None
    a, b, match_no = parsed
    match_id = IPL_MATCH_ID_BASE + match_no
    return (
        f"{IPL_HUNDRED_SERIES}/"
        f"{IPL_TEAM_SLUGS[a]}-vs-{IPL_TEAM_SLUGS[b]}-{match_id}/scorecard"
    )


def overs_to_balls(token):
    token = str(token).strip()
    if re.fullmatch(r"\d+", token):
        overs, rem = int(token), 0
    else:
        m = re.fullmatch(r"(\d+)\.([0-5])", token)
        if not m:
            return None
        overs, rem = int(m.group(1)), int(m.group(2))
    balls = overs * 6 + rem
    return balls if 0 < balls <= 20 else None


def extract_ipl_bowling_rows(lines):
    """
    IPL.com renders Hundred bowling as:
      player | O | M | R | W | ECO

    Their over notation is conventional 6-ball notation even for a Hundred
    scorecard: 20 balls appears as 3.2 overs, 15 balls as 2.3 overs.

    Validate every candidate using:
      economy ~= runs * 6 / balls
    so batting rows/commentary cannot be mistaken for bowling figures.
    """
    blocked = {
        "Batting", "Bowling", "O", "M", "R", "W", "ECO",
        "R", "B", "4s", "6s", "S/R", "Extras", "Total",
        "FALL OF WICKETS", "Match Flow", "Points Table",
        "Team", "M", "W", "L", "T", "NR",
    }
    rows = []
    seen = set()

    def as_int(x):
        x = str(x).strip()
        return int(x) if re.fullmatch(r"\d+", x) else None

    def as_float(x):
        x = str(x).strip().replace("−", "-")
        return float(x) if re.fullmatch(r"\d+(?:\.\d+)?", x) else None

    for i, raw in enumerate(lines):
        raw = str(raw).strip()
        if not raw or raw in blocked:
            continue

        name = clean_role(raw)
        if (
            not name
            or len(name) > 60
            or not re.search(r"[A-Za-z]", name)
            or name.lower().startswith(("image", "powerplay", "strategic timeout"))
        ):
            continue

        # The numeric tail is normally immediate. Allow up to two intervening
        # presentation nodes in case the site inserts a role/image label.
        for k in range(i + 1, min(i + 4, len(lines) - 4)):
            balls = overs_to_balls(lines[k])
            maidens = as_int(lines[k + 1])
            runs = as_int(lines[k + 2])
            wickets = as_int(lines[k + 3])
            economy = as_float(lines[k + 4])

            if None in (balls, maidens, runs, wickets, economy):
                continue
            if not (0 <= maidens <= 4 and 0 <= runs <= 100 and 0 <= wickets <= 10):
                continue

            expected = runs * 6 / balls
            if abs(expected - economy) > 0.16:
                continue

            key = person_key(name)
            if not key:
                continue
            sig = (key, balls, runs, wickets)
            if sig in seen:
                break

            seen.add(sig)
            rows.append({
                "name": name,
                "balls": balls,
                "runs": runs,
                "wickets": wickets,
            })
            break

    return rows


def aggregate_ipl_bowling_2026(base, cricbuzz_urls):
    """
    Recompute ALL current-season bowling from IPL.com scorecards.

    We intentionally do not mix this with Cricbuzz bowling rows. Each 2026
    match is added once from one bowling source, preventing partial/double
    counting.
    """
    stats = base["stats"]
    display_names = base["display_names"]

    fetched = 0
    parsed_matches = 0
    bowling_rows = 0
    wickets_added = 0
    failed_urls = []

    for cb_url in cricbuzz_urls:
        url = build_ipl_scorecard_url(cb_url)
        if not url:
            continue

        try:
            html = get(url).text
        except Exception as e:
            failed_urls.append(f"{url}: {e}")
            continue

        fetched += 1
        soup = BeautifulSoup(html, "html.parser")
        lines = [re.sub(r"\s+", " ", x).strip() for x in soup.stripped_strings]
        rows = extract_ipl_bowling_rows(lines)

        # A completed Hundred scorecard should normally contain at least
        # roughly 7-10 bowling rows across both innings.
        if len(rows) < 5:
            failed_urls.append(f"{url}: only {len(rows)} bowling rows parsed")
            continue

        parsed_matches += 1
        bowling_rows += len(rows)
        wickets_added += sum(r["wickets"] for r in rows)
        add_bowling_innings(stats, display_names, rows, prefer_name=True)

    # We expect the current completed-match set discovered by Cricbuzz to be
    # broadly available at IPL.com too. Allow one transient miss, not a silent
    # partial season.
    minimum = max(20, len(cricbuzz_urls) - 1)
    if parsed_matches < minimum:
        raise RuntimeError(
            f"IPL bowling source parsed {parsed_matches}/{len(cricbuzz_urls)} "
            f"2026 scorecards (minimum {minimum}); first failures: {failed_urls[:3]}"
        )

    return {
        "fetched": fetched,
        "parsed": parsed_matches,
        "rows": bowling_rows,
        "wickets_added": wickets_added,
        "failed": failed_urls,
    }

def build_career_tables(stats, display_names, current_team):
    batting, bowling = [], []
    for key, r in stats.items():
        display = display_names.get(key) or r["display"] or key
        team = current_team.get(key)

        if r["runs"] or r["innings"]:
            avg = r["runs"] / r["dismissals"] if r["dismissals"] else None
            sr = 100 * r["runs"] / r["balls"] if r["balls"] else None
            batting.append({
                "key": key, "player": display, "team": team,
                "matches": r["matches"], "innings": r["innings"],
                "runs": r["runs"],
                "high_score": f'{r["high_score"]}{"*" if r["not_out_high"] else ""}',
                "average": round(avg, 2) if avg is not None else None,
                "strike_rate": round(sr, 2) if sr is not None else None,
                "fours": r["fours"], "sixes": r["sixes"],
            })

        if r["wickets"] or r["bowling_innings"]:
            avg = r["runs_conceded"] / r["wickets"] if r["wickets"] else None
            econ6 = 6 * r["runs_conceded"] / r["balls_bowled"] if r["balls_bowled"] else None
            sr = r["balls_bowled"] / r["wickets"] if r["wickets"] else None
            best = f'{r["best_wickets"]}/{r["best_runs"]}' if r["best_runs"] < 9999 else None
            bowling.append({
                "key": key, "player": display, "team": team,
                "matches": r["matches"], "innings": r["bowling_innings"],
                "wickets": r["wickets"], "best": best,
                "average": round(avg, 2) if avg is not None else None,
                "economy": round(econ6, 2) if econ6 is not None else None,
                "strike_rate": round(sr, 2) if sr is not None else None,
                "balls": r["balls_bowled"],
            })

    batting.sort(key=lambda x: (x["runs"], x["strike_rate"] or 0), reverse=True)
    bowling.sort(key=lambda x: (x["wickets"], -(x["average"] or 9999)), reverse=True)
    for i, x in enumerate(batting, 1):
        x["rank"] = i
    for i, x in enumerate(bowling, 1):
        x["rank"] = i
    return batting, bowling

def build_analytics(base, current, batting, bowling):
    display_names = base["display_names"]

    h2h = list(current["team_h2h"].values())
    h2h.sort(key=lambda x: (x["a"], x["b"]))

    matchups = []
    for (bk, wk), r in base["player_matchups"].items():
        if r["balls"] < 3:
            continue
        matchups.append({
            "batter_key": bk,
            "bowler_key": wk,
            "batter": display_names.get(bk, bk),
            "bowler": display_names.get(wk, wk),
            **r,
            "strike_rate": round(100 * r["runs"] / r["balls"], 1) if r["balls"] else None,
            "dot_pct": round(100 * r["dots"] / r["balls"], 1) if r["balls"] else None,
        })
    matchups.sort(key=lambda x: (x["balls"], x["dismissals"]), reverse=True)

    phases = {}
    for code, arr in base["phase"].items():
        phases[code] = []
        for label, r in zip(PHASE_LABELS, arr):
            balls = int(r.get("balls", 0))
            phases[code].append({
                "phase": label,
                "balls": balls,
                "runs": int(r.get("runs", 0)),
                "wickets": int(r.get("wickets", 0)),
                "runs_per_ball": round(r.get("runs", 0) / balls, 3) if balls else None,
                "wickets_per_100": round(100 * r.get("wickets", 0) / balls, 2) if balls else None,
            })

    venues = []
    for venue, s in base["venue_summary"].items():
        teams = []
        for code, r in base["venue_team"][venue].items():
            teams.append({
                "code": code,
                "matches": r["matches"],
                "wins": r["wins"],
                "win_pct": round(100 * r["wins"] / r["matches"], 1) if r["matches"] else None,
            })
        teams.sort(key=lambda x: (-x["matches"], x["code"]))
        venues.append({
            "venue": venue,
            "matches": s["matches"],
            "avg_first_innings": round(s["first_innings_runs"] / s["matches"], 1) if s["matches"] else None,
            "chase_wins": s["chase_wins"],
            "defend_wins": s["defend_wins"],
            "teams": teams,
        })
    venues.sort(key=lambda x: x["matches"], reverse=True)

    players = {}
    bat_by_key = {x["key"]: x for x in batting}
    bowl_by_key = {x["key"]: x for x in bowling}
    all_keys = set(bat_by_key) | set(bowl_by_key) | set(current["player_recent"])
    for key in all_keys:
        players[key] = {
            "key": key,
            "player": display_names.get(key) or bat_by_key.get(key, {}).get("player") or bowl_by_key.get(key, {}).get("player") or key,
            "team": current["current_team"].get(key),
            "batting": bat_by_key.get(key),
            "bowling": bowl_by_key.get(key),
            "recent": current["player_recent"].get(key, [])[-8:],
        }

    return {
        "team_h2h": h2h,
        "team_form": {k: v[-8:] for k, v in current["team_form"].items()},
        "player_matchups": matchups,
        "phases": phases,
        "venues": venues,
        "players": list(players.values()),
        "ball_by_ball_cutoff": base["latest_bbb_date"],
    }

def cricbuzz_standings():
    html = get(CRICBUZZ_TABLE).text
    soup = BeautifulSoup(html, "html.parser")
    tokens = [re.sub(r"\s+", " ", x).strip() for x in soup.stripped_strings]
    found = {}
    code_re = re.compile(r"^(TRE|MIL|WEF|MSG|SUL|SOU|LDN|BRM)(?:\s*\([A-Z]+\))?$")

    def numeric(tok):
        tok = tok.replace("−", "-")
        if re.fullmatch(r"[+\-]?\d+\.\d+", tok):
            return float(tok)
        if re.fullmatch(r"\d+", tok):
            return int(tok)
        return None

    for opp_i, tok in enumerate(tokens):
        if tok != "Opposition":
            continue
        window = tokens[max(0, opp_i - 18):opp_i]
        code_pos, code = None, None
        for j in range(len(window) - 1, -1, -1):
            m = code_re.fullmatch(window[j])
            if m:
                code_pos, code = j, m.group(1)
                break
        if code is None:
            continue
        nums = [numeric(x) for x in window[code_pos+1:]]
        nums = [x for x in nums if x is not None]
        if len(nums) < 6:
            continue
        p, w, l, nr, pts, nrr = nums[-6:]
        if not all(isinstance(v, int) for v in [p, w, l, nr, pts]):
            continue
        if not (0 <= p <= 8 and 0 <= w <= p and 0 <= l <= p and 0 <= nr <= p):
            continue
        found[code] = {
            "team": TEAM_NAMES[code], "code": code,
            "p": int(p), "w": int(w), "l": int(l), "nr": int(nr),
            "pts": int(pts), "nrr": float(nrr),
        }

    if len(found) != 8:
        missing = sorted(set(TEAM_NAMES) - set(found))
        raise RuntimeError(f"Standings parser found {len(found)}/8; missing {missing}")

    rows = list(found.values())
    rows.sort(key=lambda x: (x["pts"], x["nrr"]), reverse=True)
    for rank, row in enumerate(rows, 1):
        row["rank"] = rank
    return rows

def main():
    refreshed = now()
    failures = []
    sources = {}

    try:
        base = aggregate_cricsheet()
        sources["cricsheet"] = {
            "name": "Cricsheet – The Hundred ball-by-ball",
            "url": CRICSHEET_HUNDRED,
            "status": "ok",
            "last_success": refreshed,
            "detail": (
                f"{base['historical_count']} men's matches through 2025 for career baseline; "
                f"ball-by-ball analytics available through {base['latest_bbb_date']}"
            ),
        }
    except Exception as e:
        failures.append(f"cricsheet: {e}")
        base = None
        sources["cricsheet"] = {
            "name": "Cricsheet – The Hundred ball-by-ball",
            "url": CRICSHEET_HUNDRED,
            "status": "error",
            "error": str(e),
        }

    current = None
    if base:
        salt_hist = base["stats"].get("psalt", {}).get("runs", 0)
        adil_hist = base["stats"].get("arashid", {}).get("wickets", 0)
        try:
            current = aggregate_cricbuzz_2026(base)
            sources["current_matches"] = {
                "name": "Cricbuzz – 2026 batting, results and squads",
                "url": CRICBUZZ_TABLE,
                "status": "ok",
                "last_success": refreshed,
                "detail": (
                    f"{current['parsed']} scorecards parsed / {current['discovered']} discovered; "
                    f"Salt 2026 runs added={base['stats'].get('psalt', {}).get('runs', 0)-salt_hist}"
                ),
            }
        except Exception as e:
            failures.append(f"current_matches: {e}")
            sources["current_matches"] = {
                "name": "Cricbuzz – 2026 batting, results and squads",
                "url": CRICBUZZ_TABLE,
                "status": "error",
                "error": str(e),
            }

        if current:
            try:
                current_urls = get_2026_scorecard_urls()
                ipl_bowling = aggregate_ipl_bowling_2026(base, current_urls)
                sources["current_bowling"] = {
                    "name": "IPL.com – 2026 bowling scorecards",
                    "url": "https://www.ipl.com/completed-cricket-score-129912",
                    "status": "ok",
                    "last_success": refreshed,
                    "detail": (
                        f"{ipl_bowling['parsed']} current scorecards; "
                        f"{ipl_bowling['rows']} bowling rows; "
                        f"{ipl_bowling['wickets_added']} wickets aggregated; "
                        f"Adil Rashid 2026 wickets added="
                        f"{base['stats'].get('arashid', {}).get('wickets', 0)-adil_hist}"
                    ),
                }
            except Exception as e:
                failures.append(f"current_bowling: {e}")
                sources["current_bowling"] = {
                    "name": "IPL.com – 2026 bowling scorecards",
                    "url": "https://www.ipl.com/completed-cricket-score-129912",
                    "status": "error",
                    "error": str(e),
                }

    try:
        standings = cricbuzz_standings()
        sources["standings"] = {
            "name": "Cricbuzz – 2026 points table",
            "url": CRICBUZZ_TABLE,
            "status": "ok",
            "last_success": refreshed,
        }
    except Exception as e:
        standings = []
        failures.append(f"standings: {e}")
        sources["standings"] = {
            "name": "Cricbuzz – 2026 points table",
            "url": CRICBUZZ_TABLE,
            "status": "error",
            "error": str(e),
        }

    batting, bowling = ([], [])
    analytics = {}
    if base and current:
        batting, bowling = build_career_tables(
            base["stats"], base["display_names"], current["current_team"]
        )
        analytics = build_analytics(base, current, batting, bowling)

        salt = next((x for x in batting if x["key"] == "psalt"), None)
        adil = next((x for x in bowling if x["key"] == "arashid"), None)

        # Regression guards against silently reverting to the frozen 2025 tables.
        if not salt or salt["runs"] < 1294:
            failures.append(
                f"validation: Phil Salt={salt['runs'] if salt else 'missing'}, expected >=1294"
            )
        if not adil or adil["wickets"] < 53:
            failures.append(
                f"validation: Adil Rashid={adil['wickets'] if adil else 'missing'}, expected >=53"
            )
        if len(analytics.get("team_h2h", [])) < 28:
            failures.append(
                f"validation: H2H matrix has {len(analytics.get('team_h2h', []))}/28 pairings"
            )
        if len(analytics.get("player_matchups", [])) < 100:
            failures.append(
                f"validation: only {len(analytics.get('player_matchups', []))} player matchups"
            )

    data = {
        "schema_version": 4,
        "generated_at_utc": refreshed,
        "status": {
            "overall": "ok" if not failures else "error",
            "stale": bool(failures),
            "message": "Analyst dataset refreshed and validated." if not failures else "Refresh failed validation.",
            "failures": failures,
        },
        "sources": sources,
        "teams": [{"code": c, "name": n} for c, n in TEAM_NAMES.items()],
        "career_batting": batting,
        "career_bowling": bowling,
        "standings": standings,
        "analytics": analytics,
        "method": {
            "career": "End-2025 Cricsheet baseline + 2026 Cricbuzz batting/results + 2026 IPL.com bowling scorecards, recomputed on refresh.",
            "team_h2h": "Completed 2021–2025 Cricsheet results + completed/currently published 2026 Cricbuzz results.",
            "player_h2h": "Delivery-level batter-v-bowler analysis from the local Cricsheet archive; freshness is shown separately.",
            "economy": "Bowling economy is shown as runs per 6 balls to align with conventional ESPN-style career economy.",
            "prediction": "Analyst Edge is a directional index, not a calibrated win probability.",
        },
    }

    if failures:
        (ROOT / "data" / "last_failed_refresh.json").write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(json.dumps(data["status"], indent=2))
        return 1

    DATA_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    salt = next(x for x in batting if x["key"] == "psalt")
    adil = next(x for x in bowling if x["key"] == "arashid")
    print(json.dumps({
        "status": "ok",
        "salt_runs": salt["runs"],
        "adil_rashid_wickets": adil["wickets"],
        "batters": len(batting),
        "bowlers": len(bowling),
        "team_h2h_pairs": len(analytics["team_h2h"]),
        "player_matchups": len(analytics["player_matchups"]),
        "standings_rows": len(standings),
        "ball_by_ball_cutoff": analytics["ball_by_ball_cutoff"],
    }, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
