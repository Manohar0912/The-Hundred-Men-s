from __future__ import annotations

import json
import re
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
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

def collect_profile_refs_from_squad_page(scorecard_url):
    squad_url = scorecard_url.replace(
        "/live-cricket-scorecard/",
        "/cricket-match-squads/",
    )
    html = get(squad_url).text
    soup = BeautifulSoup(html, "html.parser")

    match_m = re.search(r"/(\d+)/", scorecard_url)
    if not match_m:
        return {}
    match_id = match_m.group(1)

    refs = {}
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        m = re.search(r"/profiles/(\d+)/([^/?#]+)", href)
        if not m:
            continue

        player_id, slug = m.group(1), m.group(2)
        text = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
        name = re.sub(
            r"\s*(?:WK-Batter|Batter|Batting Allrounder|Bowling Allrounder|"
            r"Bowler|Allrounder|Wicketkeeper|WK)\s*$",
            "",
            text,
            flags=re.I,
        ).strip()
        if not name:
            name = " ".join(x.capitalize() for x in slug.split("-"))

        parent_text = re.sub(
            r"\s+", " ", a.parent.get_text(" ", strip=True)
        ).lower() if a.parent else ""
        if "coach" in parent_text and "bowler" not in parent_text:
            continue

        refs[player_id] = {
            "player_id": player_id,
            "name": clean_role(name),
            "match_ids": [match_id],
        }

    return refs


def merge_profile_refs(target, incoming):
    for player_id, ref in incoming.items():
        if player_id not in target:
            target[player_id] = {
                "player_id": player_id,
                "name": ref["name"],
                "match_ids": list(ref.get("match_ids", [])),
            }
            continue

        if ref.get("name") and len(ref["name"]) > len(target[player_id].get("name", "")):
            target[player_id]["name"] = ref["name"]

        existing = target[player_id].setdefault("match_ids", [])
        for match_id in ref.get("match_ids", []):
            if match_id not in existing:
                existing.append(match_id)


def parse_player_season_bowling(html, fallback_name):
    soup = BeautifulSoup(html, "html.parser")
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))

    marker = "In The Hundred Men's Competition 2026"
    pos = text.find(marker)
    if pos < 0:
        return None

    segment = text[pos:pos + 2200]

    def int_after(label):
        m = re.search(rf"\b{re.escape(label)}\s+(\d+)\b", segment, flags=re.I)
        return int(m.group(1)) if m else None

    def float_after(label):
        m = re.search(
            rf"\b{re.escape(label)}\s+(\d+(?:\.\d+)?)\b",
            segment,
            flags=re.I,
        )
        return float(m.group(1)) if m else None

    matches = int_after("Matches")
    innings = int_after("Innings")
    balls = int_after("Balls")
    wickets = int_after("Wickets")
    runs = int_after("Runs")
    average = float_after("Avg")
    rpb = float_after("RPB")
    strike_rate = float_after("SR")

    bbi = None
    bbi_m = re.search(r"\bBBI\s+(\d+)/(\d+)\b", segment, flags=re.I)
    if bbi_m:
        bbi = (int(bbi_m.group(1)), int(bbi_m.group(2)))

    if innings is None or balls is None or wickets is None or runs is None:
        return None
    if innings <= 0 or balls <= 0:
        return None
    if balls > innings * 20:
        return None
    if wickets < 0 or wickets > balls or runs < 0:
        return None
    if rpb is not None and abs((runs / balls) - rpb) > 0.12:
        return None

    return {
        "player": clean_role(fallback_name),
        "matches": matches or 0,
        "innings": innings,
        "balls": balls,
        "wickets": wickets,
        "runs": runs,
        "average": average,
        "rpb": rpb,
        "strike_rate": strike_rate,
        "bbi": bbi,
    }


def fetch_player_season_bowling(ref):
    player_id = ref["player_id"]
    name = ref["name"]
    match_ids = list(ref.get("match_ids", []))[-3:]

    errors = []
    for match_id in reversed(match_ids):
        url = (
            f"{CRICBUZZ_BASE}/player-match-performance/"
            f"match/{match_id}/player/{player_id}/bowling"
        )
        try:
            html = get(url).text
            row = parse_player_season_bowling(html, name)
            if row:
                row["player_id"] = player_id
                row["url"] = url
                return row
        except Exception as e:
            errors.append(str(e))

    return {
        "player_id": player_id,
        "player": name,
        "error": errors[-1] if errors else "no season bowling block",
    }


def fetch_current_bowling_aggregates(profile_refs):
    rows = []
    failures = []

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            pool.submit(fetch_player_season_bowling, ref): player_id
            for player_id, ref in profile_refs.items()
        }
        for fut in as_completed(futures):
            result = fut.result()
            if result.get("error"):
                failures.append(result)
            else:
                rows.append(result)

    by_key = {}
    for row in rows:
        key = person_key(row["player"])
        if not key:
            continue
        previous = by_key.get(key)
        if previous is None or row["balls"] > previous["balls"]:
            row["key"] = key
            by_key[key] = row

    rows = list(by_key.values())
    rows.sort(key=lambda x: (x["wickets"], x["balls"]), reverse=True)
    return rows, failures


def merge_2026_bowling_into_career(stats, display_names, rows):
    for row in rows:
        key = row["key"]
        rec = stats.setdefault(key, new_player())

        display = clean_role(row["player"])
        if display:
            display_names[key] = display
            rec["display"] = display

        rec["bowling_innings"] += int(row["innings"])
        rec["balls_bowled"] += int(row["balls"])
        rec["runs_conceded"] += int(row["runs"])
        rec["wickets"] += int(row["wickets"])

        if row.get("bbi"):
            w, r = row["bbi"]
            if w > rec["best_wickets"] or (w == rec["best_wickets"] and r < rec["best_runs"]):
                rec["best_wickets"] = w
                rec["best_runs"] = r

def parse_url_codes(url):
    m = re.search(r"/\d+/([a-z]+)-vs-([a-z]+)-", url, flags=re.I)
    if not m:
        return None, None
    a, b = m.group(1).upper(), m.group(2).upper()
    return (a if a in TEAM_NAMES else None), (b if b in TEAM_NAMES else None)

def parse_match_meta(url, lines):
    """
    Parse match result/date/venue directly from the current Hundred scorecard.

    Cricbuzz scorecards expose, near the top:
      Trent Rockets won by 7 wkts
    and in INFO:
      Date
      Wednesday, August 5
      Venue
      Trent Bridge, Nottingham

    We search the full scorecard text rather than a fixed first-N-token window.
    """
    a, b = parse_url_codes(url)
    text = " ".join(str(x) for x in lines)

    result_line = ""

    # Prefer an exact team-name result because it identifies the winner.
    for code, name in TEAM_NAMES.items():
        m = re.search(
            rf"\b{re.escape(name)}\s+won by\s+"
            rf"([^|•]+?)(?=(?:\s+[A-Z]{{2,4}}\s*(?:\(|\d))|\s+Batter\b|\s+INFO\b|$)",
            text,
            flags=re.I,
        )
        if m:
            result_line = f"{name} won by {m.group(1).strip()}"
            break

    # Fallback to individual stripped strings, which is the common layout.
    if not result_line:
        result_line = next(
            (
                x for x in lines
                if " won by " in str(x).lower()
                or str(x).lower() in {
                    "match tied", "no result", "match abandoned"
                }
            ),
            "",
        )

    winner = None
    low = str(result_line).lower().strip()
    for code, name in TEAM_NAMES.items():
        if low.startswith(name.lower() + " won by"):
            winner = code
            break

    result_type = None
    if "tied" in low:
        result_type = "tie"
    elif "no result" in low or "abandoned" in low:
        result_type = "no result"

    venue = None
    for i, x in enumerate(lines):
        token = str(x).strip()
        if token.rstrip(":") == "Venue" and i + 1 < len(lines):
            venue = str(lines[i + 1]).strip()
            break
        if token.startswith("Venue:"):
            venue = token.split(":", 1)[1].strip() or None
            if venue:
                break

    date = None
    # INFO layout: Date -> Wednesday, August 5
    for i, x in enumerate(lines):
        token = str(x).strip()
        if token.rstrip(":") == "Date" and i + 1 < len(lines):
            raw = str(lines[i + 1]).strip()
            for fmt in ("%A, %B %d %Y", "%A, %B %d"):
                try:
                    candidate = raw if "%Y" in fmt else raw + " 2026"
                    date = datetime.strptime(candidate, fmt).date().isoformat()
                    break
                except Exception:
                    pass
            if date:
                break

    # Top page fallback: "Date & Time: Wednesday, August 5, 6:30 PM LOCAL"
    if not date:
        m = re.search(
            r"Date\s*&\s*Time:\s*(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*"
            r"(July|August)\s+(\d{1,2})",
            text,
            flags=re.I,
        )
        if m:
            try:
                date = datetime.strptime(
                    f"{m.group(1)} {m.group(2)} 2026", "%B %d %Y"
                ).date().isoformat()
            except Exception:
                pass

    scores = {}
    for i, x in enumerate(lines[:-1]):
        token = str(x).strip()
        if token in TEAM_NAMES:
            score_token = str(lines[i + 1]).strip()
            m = re.match(r"^(\d+)(?:[-/](\d+))?\s*\((\d+)", score_token)
            if m:
                scores[token] = {
                    "runs": int(m.group(1)),
                    "wickets": int(m.group(2) or 10),
                    "balls": int(m.group(3)),
                }

    return {
        "a": a,
        "b": b,
        "winner": winner,
        "result_type": result_type,
        "result": result_line,
        "venue": venue,
        "date": date,
        "scores": scores,
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

def parse_scorecard_innings_codes(lines):
    """
    Cricbuzz scorecards expose innings labels such as:
      MIL (1st Inn)
      TRE (2nd Inn)

    Use those labels to map every batter/bowler to the team they represented
    in that match. This is much more reliable than depending only on the squad
    block parser.
    """
    codes = []
    pattern = re.compile(
        r"^(TRE|MIL|WEF|MSG|SUL|SOU|LDN|BRM)\s*"
        r"\((?:1st|2nd)\s+Inn\)$",
        flags=re.I,
    )
    for token in lines:
        m = pattern.fullmatch(str(token).strip())
        if not m:
            continue
        code = m.group(1).upper()
        if code not in codes:
            codes.append(code)
    return codes[:2]


def opposing_team(code, a, b):
    if code == a:
        return b
    if code == b:
        return a
    return None

def aggregate_cricbuzz_2026(base):
    stats = base["stats"]
    display_names = base["display_names"]
    team_h2h = dict(base["historical_h2h"])
    urls = get_2026_scorecard_urls()

    parsed = 0
    team_form = defaultdict(list)
    player_recent = defaultdict(list)
    current_team = {}
    team_membership = defaultdict(lambda: defaultdict(int))
    team_last_seen = defaultdict(dict)
    current_match_summaries = []
    profile_refs = {}

    for url in urls:
        html = get(url).text
        soup = BeautifulSoup(html, "html.parser")
        lines = [re.sub(r"\s+", " ", x).strip() for x in soup.stripped_strings]
        batting, bowling = split_scorecard_sections(lines)

        try:
            merge_profile_refs(
                profile_refs,
                collect_profile_refs_from_squad_page(url),
            )
        except Exception:
            pass

        if not batting:
            continue
        parsed += 1

        meta = parse_match_meta(url, lines)
        squads = parse_squads(lines)
        innings_codes = parse_scorecard_innings_codes(lines)

        match_seen = set()
        match_team = {}

        for code, names in squads.items():
            for name in names:
                key, _ = ensure(stats, display_names, name, prefer_name=True)
                if key:
                    match_team[key] = code
                    team_membership[key][code] += 3
                    if meta.get("date"):
                        team_last_seen[key][code] = max(
                            team_last_seen[key].get(code, ""),
                            meta["date"],
                        )
                    match_seen.add(key)

        match_player = defaultdict(dict)

        for inn_idx, inn in enumerate(batting):
            batting_team = (
                innings_codes[inn_idx]
                if inn_idx < len(innings_codes)
                else None
            )
            for r in inn:
                key, _ = ensure(stats, display_names, r["name"], prefer_name=True)
                if key:
                    match_seen.add(key)
                    match_player[key]["batting"] = dict(r)
                    if batting_team:
                        match_team[key] = batting_team
                        team_membership[key][batting_team] += 5
                        if meta.get("date"):
                            team_last_seen[key][batting_team] = max(
                                team_last_seen[key].get(batting_team, ""),
                                meta["date"],
                            )
            add_batting_innings(stats, display_names, inn, prefer_name=True)

        for inn_idx, inn in enumerate(bowling):
            batting_team = (
                innings_codes[inn_idx]
                if inn_idx < len(innings_codes)
                else None
            )
            bowling_team = opposing_team(
                batting_team,
                meta.get("a"),
                meta.get("b"),
            )
            for r in inn:
                key, _ = ensure(stats, display_names, r["name"], prefer_name=True)
                if key:
                    match_seen.add(key)
                    match_player[key]["bowling"] = dict(r)
                    if bowling_team:
                        match_team[key] = bowling_team
                        team_membership[key][bowling_team] += 5
                        if meta.get("date"):
                            team_last_seen[key][bowling_team] = max(
                                team_last_seen[key].get(bowling_team, ""),
                                meta["date"],
                            )

        # Career bowling is merged later from per-player tournament aggregates.

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
            code = match_team.get(key)
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

    # Resolve each player's current 2026 franchise from all evidence collected
    # across scorecards. Highest evidence count wins; latest appearance breaks
    # ties. This prevents Player Lab / Matchup Lab from losing roster mappings
    # when a single squad block fails to parse.
    for key, counts in team_membership.items():
        if not counts:
            continue
        current_team[key] = max(
            counts,
            key=lambda code: (
                counts[code],
                team_last_seen.get(key, {}).get(code, ""),
            ),
        )

    team_rosters = {code: [] for code in TEAM_NAMES}
    for key, code in current_team.items():
        team_rosters.setdefault(code, []).append(key)

    # Put the most frequently evidenced players first.
    for code, keys in team_rosters.items():
        keys.sort(
            key=lambda key: (
                team_membership.get(key, {}).get(code, 0),
                team_last_seen.get(key, {}).get(code, ""),
            ),
            reverse=True,
        )

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
        "team_rosters": team_rosters,
        "team_membership": {
            key: dict(counts)
            for key, counts in team_membership.items()
        },
        "match_summaries": current_match_summaries,
        "profile_refs": profile_refs,
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
        "team_rosters": current.get("team_rosters", {}),
        "ball_by_ball_cutoff": base["latest_bbb_date"],
    }

def cricbuzz_recent_form():
    """
    Parse each team's completed 2026 results directly from the Cricbuzz
    points-table opposition history.

    The points table publishes, for every team:
        Opposition | Date | Result | NRR Change

    Example:
        TRE
        Jul 24
        Lost by 10 runs
        -1.000

    This is more reliable for recent form than inferring results separately
    from every scorecard page.
    """
    html = get(CRICBUZZ_TABLE).text
    soup = BeautifulSoup(html, "html.parser")
    tokens = [
        re.sub(r"\s+", " ", x).strip()
        for x in soup.stripped_strings
        if str(x).strip()
    ]

    team_code_re = re.compile(
        r"^(TRE|MIL|WEF|MSG|SUL|SOU|LDN|BRM)(?:\s*\([A-Z]+\))?$"
    )
    opponent_code_re = re.compile(r"^(TRE|MIL|WEF|MSG|SUL|SOU|LDN|BRM)$")
    date_re = re.compile(r"^(Jul|Aug)\s+\d{2}$", flags=re.I)

    # Identify each standings team's Opposition block.
    blocks = []
    for i, tok in enumerate(tokens):
        if tok != "Opposition":
            continue

        window = tokens[max(0, i - 18):i]
        code = None
        for candidate in reversed(window):
            m = team_code_re.fullmatch(candidate)
            if m:
                code = m.group(1)
                break

        if code:
            blocks.append((i, code))

    if len(blocks) < 8:
        raise RuntimeError(
            f"Recent-form parser found only {len(blocks)} team opposition blocks"
        )

    form = {code: [] for code in TEAM_NAMES}

    for block_index, (opp_i, team_code_) in enumerate(blocks):
        end = blocks[block_index + 1][0] if block_index + 1 < len(blocks) else len(tokens)
        segment = tokens[opp_i + 1:end]

        # Scan for the repeated pattern:
        # opponent code -> date -> result -> NRR change
        for j in range(len(segment) - 2):
            opp_token = segment[j]
            if not opponent_code_re.fullmatch(opp_token):
                continue

            date_token = segment[j + 1]
            result_token = segment[j + 2]

            if not date_re.fullmatch(date_token):
                continue

            result_lower = result_token.lower()

            # Future/current fixtures are shown as "-".
            if result_token == "-":
                continue

            if result_lower.startswith("won"):
                outcome = "W"
            elif result_lower.startswith("lost"):
                outcome = "L"
            elif "tied" in result_lower:
                outcome = "T"
            elif (
                "no result" in result_lower
                or "abandon" in result_lower
                or "cancel" in result_lower
            ):
                outcome = "NR"
            else:
                # Ignore anything that is not a published completed result.
                continue

            try:
                date_iso = datetime.strptime(
                    f"{date_token} 2026", "%b %d %Y"
                ).date().isoformat()
            except Exception:
                date_iso = None

            form[team_code_].append({
                "date": date_iso,
                "opponent": opp_token,
                "result": outcome,
                "venue": None,
                "match_result": result_token,
                "source": "points-table",
            })

    # Sort chronologically and remove accidental duplicates.
    cleaned = {}
    for code, rows in form.items():
        seen = set()
        unique = []
        for row in sorted(rows, key=lambda x: x.get("date") or ""):
            sig = (row.get("date"), row.get("opponent"), row.get("result"))
            if sig in seen:
                continue
            seen.add(sig)
            unique.append(row)
        cleaned[code] = unique

    return cleaned

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
                "name": "Cricbuzz – The Hundred Men’s Competition 2026 scorecards/results",
                "url": CRICBUZZ_TABLE,
                "status": "ok",
                "last_success": refreshed,
                "detail": (
                    f"{current['parsed']} scorecards parsed / {current['discovered']} discovered; "
                    f"{len(current['profile_refs'])} player profiles discovered; "
                    f"Salt 2026 runs added={base['stats'].get('psalt', {}).get('runs', 0)-salt_hist}"
                ),
            }
        except Exception as e:
            failures.append(f"current_matches: {e}")
            sources["current_matches"] = {
                "name": "Cricbuzz – The Hundred Men’s Competition 2026 scorecards/results",
                "url": CRICBUZZ_TABLE,
                "status": "error",
                "error": str(e),
            }

        if current:
            try:
                current_bowling, bowling_failures = fetch_current_bowling_aggregates(
                    current["profile_refs"]
                )

                total_current_wickets = sum(x["wickets"] for x in current_bowling)
                adil_current = next(
                    (x for x in current_bowling if x["key"] == "arashid"),
                    None,
                )

                if len(current_bowling) < 30:
                    raise RuntimeError(
                        f"only {len(current_bowling)} current bowlers parsed"
                    )
                if total_current_wickets < 120:
                    raise RuntimeError(
                        f"only {total_current_wickets} current-season wickets parsed"
                    )
                if not adil_current or adil_current["wickets"] < 8:
                    raise RuntimeError(
                        f"Adil Rashid current-season wickets="
                        f"{adil_current['wickets'] if adil_current else 'missing'}, "
                        "expected at least 8"
                    )

                merge_2026_bowling_into_career(
                    base["stats"],
                    base["display_names"],
                    current_bowling,
                )

                sources["current_bowling"] = {
                    "name": "Cricbuzz – 2026 player tournament bowling aggregates",
                    "url": (
                        f"{CRICBUZZ_BASE}/player-match-performance/"
                        f"match/144849/player/1742/bowling"
                    ),
                    "status": "ok",
                    "last_success": refreshed,
                    "detail": (
                        f"{len(current_bowling)} bowlers parsed; "
                        f"{total_current_wickets} season wickets represented; "
                        f"{len(bowling_failures)} player pages skipped; "
                        f"Adil Rashid 2026={adil_current['wickets']}, "
                        f"all-time={base['stats'].get('arashid', {}).get('wickets', 0)}"
                    ),
                }
            except Exception as e:
                failures.append(f"current_bowling: {e}")
                sources["current_bowling"] = {
                    "name": "Cricbuzz – 2026 player tournament bowling aggregates",
                    "url": (
                        f"{CRICBUZZ_BASE}/player-match-performance/"
                        f"match/144849/player/1742/bowling"
                    ),
                    "status": "error",
                    "error": str(e),
                }

    try:
        standings = cricbuzz_standings()

        scorecard_form = current.get("team_form", {}) if current else {}

        # Validate recent-form coverage against matches played in the standings.
        # The UI only needs the latest five, so require min(P, 5).
        form_issues = []
        for row in standings:
            code = row["code"]
            played = int(row["p"])
            actual = len(scorecard_form.get(code, []))
            required = min(played, 5)

            if actual < required:
                form_issues.append(
                    f"{code}: {actual} completed scorecard results parsed, "
                    f"expected at least {required}"
                )

        if form_issues:
            # Include parsed match summaries to make any future source change
            # diagnosable in one log rather than another blind parser patch.
            parsed_results = [
                {
                    "a": x.get("a"),
                    "b": x.get("b"),
                    "winner": x.get("winner"),
                    "result_type": x.get("result_type"),
                    "date": x.get("date"),
                    "result": x.get("result"),
                }
                for x in (current.get("match_summaries", []) if current else [])
                if x.get("winner") or x.get("result_type")
            ]
            raise RuntimeError(
                "recent-form coverage incomplete: "
                + "; ".join(form_issues)
                + f"; completed match metadata parsed={len(parsed_results)}"
            )

        sources["standings"] = {
            "name": "Cricbuzz – 2026 points table",
            "url": CRICBUZZ_TABLE,
            "status": "ok",
            "last_success": refreshed,
            "detail": (
                "8 standings rows; recent form from completed scorecards: "
                + ", ".join(
                    f"{code}={len(scorecard_form.get(code, []))}"
                    for code in TEAM_NAMES
                )
            ),
        }
    except Exception as e:
        standings = []
        failures.append(f"standings/form: {e}")
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

        missing_form = [
            code for code in TEAM_NAMES
            if len(analytics.get("team_form", {}).get(code, [])) < 5
        ]
        if missing_form:
            failures.append(
                "validation: recent form missing/short for "
                + ", ".join(missing_form)
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
            "career": "End-2025 Cricsheet baseline + current 2026 Cricbuzz batting scorecards + per-player 2026 tournament bowling aggregates.",
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
