from __future__ import annotations

import json
import re
import sys
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "data" / "current.json"

CRICSHEET_HUNDRED = "https://cricsheet.org/downloads/hnd_json.zip"
CRICSHEET_LOCAL_CANDIDATES = [ROOT / "data" / "hnd_json.zip", ROOT / "hnd_json.zip"]
CRICBUZZ_MATCHES = "https://www.cricbuzz.com/cricket-series/11493/the-hundred-mens-competition-2026/matches"
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

LINEAGE = {
    "Oval Invincibles": "MI London",
    "Manchester Originals": "Manchester Super Giants",
    "Northern Superchargers": "SunRisers Leeds",
}

NON_BOWLER_WICKETS = {
    "run out", "retired hurt", "retired out", "obstructing the field"
}

def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

def get(url, timeout=35):
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r

def clean_role(name):
    name = str(name)
    # Remove any parenthetical player-role marker containing captain/wicketkeeper
    # tokens, including "(c & wk)", "(c)", "(wk)", "(c/wk)", "(wk/c)".
    name = re.sub(
        r"\s*\([^)]*(?:\bc\b|\bwk\b)[^)]*\)\s*",
        " ",
        name,
        flags=re.I
    )
    name = re.sub(r"\s+", " ", name).strip()
    return name

def person_key(name):
    """Merge scorecard full names with Cricsheet initial+surname names."""
    n = clean_role(name)
    n = re.sub(r"[^A-Za-z0-9' -]", "", n).strip()
    parts = [p for p in n.split() if p]
    if not parts:
        return ""
    surname = re.sub(r"[^a-z0-9]", "", parts[-1].lower())
    first = re.sub(r"[^a-z0-9]", "", parts[0].lower())
    initial = first[:1]
    return initial + surname

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

def ensure(stats, name, prefer_name=False):
    key = person_key(name)
    if not key:
        return None, None
    rec = stats.setdefault(key, new_player())
    clean = clean_role(name)
    # Prefer current-season full names over historic initials.
    if prefer_name or not rec["display"] or len(clean) > len(rec["display"]):
        rec["display"] = clean
    return key, rec

def add_match_appearance(stats, name, seen):
    key, rec = ensure(stats, name)
    if key and key not in seen:
        rec["matches"] += 1
        seen.add(key)

def add_batting_innings(stats, rows, prefer_name=False):
    for row in rows:
        key, rec = ensure(stats, row["name"], prefer_name=prefer_name)
        if not key:
            continue
        rec["innings"] += 1
        rec["runs"] += row["runs"]
        rec["balls"] += row["balls"]
        rec["fours"] += row.get("fours", 0)
        rec["sixes"] += row.get("sixes", 0)
        if row.get("dismissed", False):
            rec["dismissals"] += 1
        score = row["runs"]
        not_out = not row.get("dismissed", False)
        if score > rec["high_score"] or (score == rec["high_score"] and not_out):
            rec["high_score"] = score
            rec["not_out_high"] = not_out

def add_bowling_innings(stats, rows, prefer_name=False):
    for row in rows:
        key, rec = ensure(stats, row["name"], prefer_name=prefer_name)
        if not key:
            continue
        rec["bowling_innings"] += 1
        rec["balls_bowled"] += row["balls"]
        rec["runs_conceded"] += row["runs"]
        rec["wickets"] += row["wickets"]
        w, r = row["wickets"], row["runs"]
        if w > rec["best_wickets"] or (w == rec["best_wickets"] and r < rec["best_runs"]):
            rec["best_wickets"], rec["best_runs"] = w, r

def aggregate_cricsheet_through_2025():
    # Historical data is kept in the repository because Cricsheet's download
    # endpoint can challenge GitHub-hosted runners. Accept the archive either
    # under data/ (preferred) or at repository root (backward-compatible).
    archive = next((x for x in CRICSHEET_LOCAL_CANDIDATES if x.exists()), None)
    if archive is None:
        checked = ", ".join(str(x.relative_to(ROOT)) for x in CRICSHEET_LOCAL_CANDIDATES)
        raise RuntimeError(
            f"Missing Cricsheet archive. Checked: {checked}. "
            "Upload hnd_json.zip without extracting it."
        )
    size = archive.stat().st_size
    if size < 500_000:
        raise RuntimeError(
            f"{archive.relative_to(ROOT)} is only {size} bytes; expected the "
            "Cricsheet Hundred JSON archive (roughly 1 MB+)."
        )
    stats = {}
    match_count = 0
    z = zipfile.ZipFile(archive)
    for name in z.namelist():
        if not name.endswith(".json"):
            continue
        try:
            obj = json.loads(z.read(name))
        except Exception:
            continue
        info = obj.get("info", {})
        if str(info.get("gender", "")).lower() not in {"male", "men"}:
            continue
        event = info.get("event", {}) or {}
        event_name = str(event.get("name", ""))
        if "Hundred" not in event_name:
            continue
        season = info.get("season")
        try:
            season_num = int(str(season)[:4])
        except Exception:
            continue
        if season_num > 2025:
            continue

        match_count += 1
        seen = set()
        for team, plist in (info.get("players", {}) or {}).items():
            for player in plist or []:
                add_match_appearance(stats, player, seen)

        for innings in obj.get("innings", []) or []:
            bat_seen = set()
            bowl_seen = set()
            bat_rows = defaultdict(lambda: {"runs":0,"balls":0,"fours":0,"sixes":0,"dismissed":False})
            bowl_rows = defaultdict(lambda: {"balls":0,"runs":0,"wickets":0})

            for over in innings.get("overs", []) or []:
                for d in over.get("deliveries", []) or []:
                    batter = d.get("batter")
                    bowler = d.get("bowler")
                    if batter:
                        b = bat_rows[batter]
                        br = int((d.get("runs", {}) or {}).get("batter", 0) or 0)
                        b["runs"] += br
                        extras = d.get("extras", {}) or {}
                        if "wides" not in extras:
                            b["balls"] += 1
                        if br == 4: b["fours"] += 1
                        if br == 6: b["sixes"] += 1
                        bat_seen.add(batter)

                    if bowler:
                        bw = bowl_rows[bowler]
                        extras = d.get("extras", {}) or {}
                        if "wides" not in extras and "noballs" not in extras:
                            bw["balls"] += 1
                        total = int((d.get("runs", {}) or {}).get("total", 0) or 0)
                        non_bowler = int(extras.get("byes", 0) or 0) + int(extras.get("legbyes", 0) or 0) + int(extras.get("penalty", 0) or 0)
                        bw["runs"] += max(0, total - non_bowler)
                        bowl_seen.add(bowler)

                    for w in d.get("wickets", []) or []:
                        out = w.get("player_out")
                        kind = str(w.get("kind", "")).lower()
                        if out:
                            bat_rows[out]["dismissed"] = kind != "retired hurt"
                        if bowler and kind not in NON_BOWLER_WICKETS:
                            bowl_rows[bowler]["wickets"] += 1

            add_batting_innings(stats, [
                {"name":n, **r} for n,r in bat_rows.items()
            ])
            add_bowling_innings(stats, [
                {"name":n, **r} for n,r in bowl_rows.items()
            ])

    if match_count < 150:
        raise RuntimeError(f"Cricsheet men's Hundred historical sample looks incomplete: {match_count} matches")
    return stats, match_count

def get_2026_scorecard_urls():
    html = get(CRICBUZZ_TABLE).text

    # The series "matches" page only exposes a small rolling window of nearby
    # fixtures in static HTML. The points table, however, links every completed
    # result for every team. Extract those result links and deduplicate by
    # Cricbuzz match id.
    hrefs = re.findall(
        r'href=["\']([^"\']*/live-cricket-(?:scores|scorecard)/\d+/[^"\']*the-hundred-mens-competition-2026[^"\']*)["\']',
        html,
        flags=re.I
    )

    urls = []
    seen = set()
    for href in hrefs:
        if href.startswith("http"):
            full = href
        else:
            full = CRICBUZZ_BASE + (href if href.startswith("/") else "/" + href)

        full = full.replace("/live-cricket-scores/", "/live-cricket-scorecard/")
        mid = re.search(r"/live-cricket-scorecard/(\d+)/", full)
        if not mid:
            continue
        match_id = mid.group(1)
        if match_id in seen:
            continue
        seen.add(match_id)
        urls.append(full)

    if len(urls) < 20:
        raise RuntimeError(
            f"Only found {len(urls)} unique 2026 scorecard links on the points table; "
            "expected at least 20 completed/current matches."
        )
    return urls

def split_scorecard_sections(lines):
    """Return batting and bowling blocks from Cricbuzz stripped text."""
    batting, bowling = [], []
    i = 0
    while i < len(lines):
        if lines[i] == "Batter" and i + 5 < len(lines) and lines[i+1:i+6] == ["R","B","4s","6s","SR"]:
            i += 6
            rows = []
            while i < len(lines) and lines[i] not in {"Extras","Bowler","Total","Did not Bat"}:
                # Batter block is name, dismissal, R, B, 4s, 6s, SR.
                if i + 6 >= len(lines):
                    break
                name = clean_role(lines[i])
                dismissal = lines[i+1]
                try:
                    runs = int(lines[i+2]); balls = int(lines[i+3])
                    fours = int(lines[i+4]); sixes = int(lines[i+5])
                    float(lines[i+6].replace("−","-"))
                except Exception:
                    i += 1
                    continue
                dismissed = "not out" not in dismissal.lower() and "retired hurt" not in dismissal.lower()
                rows.append({"name":name,"runs":runs,"balls":balls,"fours":fours,"sixes":sixes,"dismissed":dismissed})
                i += 7
            if rows:
                batting.append(rows)
            continue

        if lines[i] == "Bowler" and i + 5 < len(lines) and lines[i+1:i+6] == ["B","D","R","W","RPB"]:
            i += 6
            rows = []
            while i < len(lines) and lines[i] not in {"Fall of Wickets","Batter","INFO","Partnerships"}:
                if i + 5 >= len(lines):
                    break
                name = clean_role(lines[i])
                try:
                    balls = int(lines[i+1]); runs = int(lines[i+3]); wkts = int(lines[i+4])
                    float(lines[i+5].replace("−","-"))
                except Exception:
                    i += 1
                    continue
                rows.append({"name":name,"balls":balls,"runs":runs,"wickets":wkts})
                i += 6
            if rows:
                bowling.append(rows)
            continue
        i += 1
    return batting, bowling

def aggregate_cricbuzz_2026(stats):
    urls = get_2026_scorecard_urls()
    parsed = 0
    appearances = defaultdict(set)

    for url in urls:
        html = get(url).text
        soup = BeautifulSoup(html, "html.parser")
        lines = [re.sub(r"\s+", " ", s).strip() for s in soup.stripped_strings]
        batting, bowling = split_scorecard_sections(lines)
        if not batting:
            # Future match/pre-match scorecard; simply skip.
            continue
        parsed += 1

        # Count matches from players who appear in the scorecard batting/bowling sections.
        # This is robust enough for played matches; DNB-only players are added below from squad "Players" blocks where possible.
        match_seen = set()
        for inn in batting:
            for r in inn:
                key, rec = ensure(stats, r["name"], prefer_name=True)
                if key: match_seen.add(key)
            add_batting_innings(stats, inn, prefer_name=True)
        for inn in bowling:
            for r in inn:
                key, rec = ensure(stats, r["name"], prefer_name=True)
                if key: match_seen.add(key)
            add_bowling_innings(stats, inn, prefer_name=True)

        # Recover XI players listed under each squad's "Players" heading.
        # Between "Players" and "Bench" every comma-separated name is a player.
        for idx, val in enumerate(lines):
            if val == "Players":
                j = idx + 1
                while j < len(lines) and lines[j] not in {"Bench","Support Staff"}:
                    name = clean_role(lines[j].rstrip(","))
                    # Avoid headings accidentally entering the XI.
                    if name and not re.fullmatch(r"\d+|\d+:\d+.*", name):
                        key, rec = ensure(stats, name, prefer_name=True)
                        if key: match_seen.add(key)
                    j += 1

        for key in match_seen:
            stats[key]["matches"] += 1

    if parsed < 20:
        raise RuntimeError(f"Only {parsed} 2026 scorecards contained match data; expected at least 20.")
    return parsed, len(urls)

def build_tables(stats):
    batting = []
    bowling = []
    for key, r in stats.items():
        if r["runs"] or r["innings"]:
            avg = (r["runs"] / r["dismissals"]) if r["dismissals"] else None
            sr = (100 * r["runs"] / r["balls"]) if r["balls"] else None
            batting.append({
                "player": r["display"],
                "matches": r["matches"],
                "innings": r["innings"],
                "runs": r["runs"],
                "high_score": f'{r["high_score"]}{"*" if r["not_out_high"] else ""}',
                "average": round(avg, 2) if avg is not None else None,
                "strike_rate": round(sr, 2) if sr is not None else None,
                "fours": r["fours"],
                "sixes": r["sixes"],
            })
        if r["wickets"] or r["bowling_innings"]:
            avg = (r["runs_conceded"] / r["wickets"]) if r["wickets"] else None
            rpb = (r["runs_conceded"] / r["balls_bowled"]) if r["balls_bowled"] else None
            best = f'{r["best_wickets"]}/{r["best_runs"]}' if r["best_runs"] < 9999 else None
            bowling.append({
                "player": r["display"],
                "matches": r["matches"],
                "innings": r["bowling_innings"],
                "wickets": r["wickets"],
                "best": best,
                "average": round(avg, 2) if avg is not None else None,
                "runs_per_ball": round(rpb, 3) if rpb is not None else None,
                "balls": r["balls_bowled"],
            })
    batting.sort(key=lambda x: (x["runs"], x["strike_rate"] or 0), reverse=True)
    bowling.sort(key=lambda x: (x["wickets"], -(x["average"] or 9999)), reverse=True)
    for i, x in enumerate(batting, 1): x["rank"] = i
    for i, x in enumerate(bowling, 1): x["rank"] = i
    return batting, bowling

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

    # Every actual table row ends immediately before an "Opposition" heading.
    # Search backwards from that heading to find:
    # rank, team code/status, P, W, L, NR, Pts, NRR
    for opp_i, tok in enumerate(tokens):
        if tok != "Opposition":
            continue

        window_start = max(0, opp_i - 18)
        window = tokens[window_start:opp_i]

        code_pos = None
        code = None
        # Use the LAST team-code-like token before "Opposition".
        # That avoids unrelated navigation text earlier in the window.
        for j in range(len(window) - 1, -1, -1):
            m = code_re.fullmatch(window[j])
            if m:
                code_pos = j
                code = m.group(1)
                break

        if code is None:
            # Some Cricbuzz markup may split "LDN(E)" into "LDN" and "(E)".
            for j in range(len(window) - 1, -1, -1):
                if window[j] in TEAM_NAMES:
                    code_pos = j
                    code = window[j]
                    break

        if code is None:
            continue

        nums = []
        for x in window[code_pos + 1:]:
            v = numeric(x)
            if v is not None:
                nums.append(v)

        # Ranking row must contribute exactly/sufficiently:
        # P W L NR Pts NRR. If markup added another token, use the final six
        # numeric values immediately preceding "Opposition".
        if len(nums) < 6:
            continue
        p_, w_, l_, nr_, pts_, nrr_ = nums[-6:]

        if not all(isinstance(v, int) for v in [p_, w_, l_, nr_, pts_]):
            continue
        if not isinstance(nrr_, (int, float)):
            continue

        p_, w_, l_, nr_, pts_, nrr_ = (
            int(p_), int(w_), int(l_), int(nr_), int(pts_), float(nrr_)
        )

        if not (0 <= p_ <= 8 and 0 <= w_ <= p_ and 0 <= l_ <= p_ and 0 <= nr_ <= p_):
            continue
        if w_ + l_ + nr_ > p_ or not (0 <= pts_ <= 32) or not (-10 <= nrr_ <= 10):
            continue

        found[code] = {
            "team": TEAM_NAMES[code],
            "code": code,
            "p": p_,
            "w": w_,
            "l": l_,
            "nr": nr_,
            "pts": pts_,
            "nrr": nrr_,
        }

    if len(found) != 8:
        missing = sorted(set(TEAM_NAMES) - set(found))
        diagnostics = {}
        for code in missing:
            diagnostics[code] = [
                tokens[max(0, j-3):j+10]
                for j, t in enumerate(tokens)
                if t == code or t.startswith(code + "(")
            ][-3:]
        raise RuntimeError(
            f"Could not parse all 8 standings rows; found {len(found)}: "
            f"{sorted(found)}; missing: {missing}; nearby tokens: {diagnostics}"
        )

    rows = list(found.values())
    rows.sort(key=lambda x: (x["pts"], x["nrr"]), reverse=True)
    for rank, row in enumerate(rows, 1):
        row["rank"] = rank
    return rows

def main():
    refreshed = now()
    failures = []
    source_status = {}

    try:
        stats, historical_matches = aggregate_cricsheet_through_2025()
        source_status["historical"] = {
            "name": "Cricsheet – The Hundred JSON ball-by-ball (repository copy)",
            "url": CRICSHEET_HUNDRED,
            "status": "ok",
            "last_success": refreshed,
            "detail": f"{historical_matches} men's matches through 2025 aggregated"
        }
    except Exception as e:
        failures.append(f"historical: {e}")
        stats = {}
        historical_matches = 0
        source_status["historical"] = {
            "name": "Cricsheet – The Hundred JSON ball-by-ball (repository copy)",
            "url": CRICSHEET_HUNDRED,
            "status": "error",
            "last_success": None,
            "error": str(e)
        }

    if stats:
        try:
            parsed_2026, discovered_2026 = aggregate_cricbuzz_2026(stats)
            source_status["current_matches"] = {
                "name": "Cricbuzz – 2026 scorecards discovered via points-table results",
                "url": CRICBUZZ_TABLE,
                "status": "ok",
                "last_success": refreshed,
                "detail": f"{parsed_2026} scorecards with data / {discovered_2026} discovered"
            }
        except Exception as e:
            failures.append(f"current_matches: {e}")
            source_status["current_matches"] = {
                "name": "Cricbuzz – 2026 scorecards discovered via points-table results",
                "url": CRICBUZZ_TABLE,
                "status": "error",
                "last_success": None,
                "error": str(e)
            }

    try:
        standings = cricbuzz_standings()
        source_status["standings"] = {
            "name": "Cricbuzz – 2026 points table",
            "url": CRICBUZZ_TABLE,
            "status": "ok",
            "last_success": refreshed
        }
    except Exception as e:
        standings = []
        failures.append(f"standings: {e}")
        source_status["standings"] = {
            "name": "Cricbuzz – 2026 points table",
            "url": CRICBUZZ_TABLE,
            "status": "error",
            "last_success": None,
            "error": str(e)
        }

    batting, bowling = build_tables(stats) if stats else ([], [])

    # Critical validation: after the completed matches currently available,
    # Phil Salt's all-time total must not regress below the user-verified ESPN
    # figure of 1,294. This catches name-merging or incomplete-history errors.
    salt = None
    if stats:
        salt = next((x for x in batting if x["player"].lower() == "phil salt"), None)
        if not salt:
            # Historical Cricsheet may retain an abbreviated display name.
            salt = next((x for x in batting if person_key(x["player"]) == "psalt"), None)
        if not salt or salt["runs"] < 1294:
            salt_like = [
                (x["player"], x["runs"])
                for x in batting
                if "salt" in x["player"].lower()
            ]
            failures.append(
                f"validation: Phil Salt total is {salt['runs'] if salt else 'missing'}, "
                f"expected at least 1294; salt-like rows: {salt_like}"
            )

    data = {
        "schema_version": 3,
        "generated_at_utc": refreshed,
        "status": {
            "overall": "ok" if not failures else "error",
            "stale": bool(failures),
            "message": "All critical sources refreshed." if not failures else "Critical data refresh failed. See source health.",
            "failures": failures
        },
        "sources": source_status,
        "career_batting": batting,
        "career_bowling": bowling,
        "standings": standings,
        "method": {
            "career": "Cricsheet men's Hundred ball-by-ball through 2025 + all available 2026 Cricbuzz scorecards, recomputed from scratch each refresh.",
            "current": "2026 scorecards are re-read on every run, so completed and in-progress scorecard data can flow into cumulative totals as Cricbuzz publishes it.",
            "validation": "Phil Salt all-time runs may not regress below the independently verified 1,294 benchmark."
        }
    }

    # Only publish a new JSON when critical validation succeeds.
    # This prevents a broken scrape from replacing the last good dataset.
    if failures:
        debug = ROOT / "data" / "last_failed_refresh.json"
        debug.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(data["status"], indent=2))
        return 1

    DATA_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({
        "status":"ok",
        "batters":len(batting),
        "bowlers":len(bowling),
        "salt_runs": salt["runs"] if salt else None,
        "standings_rows":len(standings)
    }, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
