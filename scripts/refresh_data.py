from __future__ import annotations
import json, re, sys, time
from pathlib import Path
from datetime import datetime, timezone
from io import StringIO

import pandas as pd
import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "data" / "current.json"

ESPN_BAT = "https://www.espncricinfo.com/records/trophy/batting-most-runs-career/the-hundred-men-s-competition-826"
ESPN_BOWL = "https://www.espncricinfo.com/records/trophy/bowling-most-wickets-career/the-hundred-men-s-competition-826"
ESPN_BAT_LEGACY = "https://stats.espncricinfo.com/ci/engine/records/batting/most_runs_career.html?id=826;type=trophy"
ESPN_BOWL_LEGACY = "https://stats.espncricinfo.com/ci/engine/records/bowling/most_wickets_career.html?id=826;type=trophy"
CRICBUZZ_TABLE = "https://www.cricbuzz.com/cricket-series/11493/the-hundred-mens-competition-2026/points-table"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0 Safari/537.36",
    "Accept-Language": "en-GB,en;q=0.9",
    "Cache-Control": "no-cache",
}

TEAM_CODES = {
    "TRE":"Trent Rockets", "MIL":"MI London", "WEF":"Welsh Fire",
    "MSG":"Manchester Super Giants", "SUL":"SunRisers Leeds",
    "SOU":"Southern Brave", "LDN":"London Spirit", "BRM":"Birmingham Phoenix"
}

def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")

def load():
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return {"sources":{}, "career_batting":[], "career_bowling":[], "standings":[]}

def get(url, timeout=25):
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.text

def clean_num(v, default=None):
    if v is None:
        return default
    s = re.sub(r"[^0-9.\-]", "", str(v))
    if not s or s in {"-","."}: return default
    try:
        return float(s) if "." in s else int(s)
    except ValueError:
        return default

def normalize_cols(df):
    df = df.copy()
    df.columns = [str(c).strip().replace("\n"," ") for c in df.columns]
    return df

def find_col(cols, patterns):
    for p in patterns:
        for c in cols:
            if re.fullmatch(p, c, flags=re.I) or re.search(p, c, flags=re.I):
                return c
    return None

def espn_tables(url, kind, fallback_url=None):
    # ESPN record pages are commonly server-rendered. Loop pages so Player Finder
    # can load the complete career table rather than only a top-10 snapshot.
    # If the modern record endpoint blocks automated access, try ESPN's legacy
    # records engine before preserving the previous last-good dataset.
    seen = set()
    rows = []
    base_url = url
    try:
        html_first = get(base_url)
    except Exception:
        if not fallback_url:
            raise
        base_url = fallback_url
        html_first = get(base_url)

    for page in range(1, 11):
        if page == 1:
            html = html_first
        else:
            sep = "&" if "?" in base_url else "?"
            u = f"{base_url}{sep}page={page}"
            html = get(u)
        tables = pd.read_html(StringIO(html))
        candidate = None
        for df in tables:
            df = normalize_cols(df)
            cols = list(df.columns)
            player_col = find_col(cols, [r"^Player$", r"Player"])
            if not player_col:
                continue
            if kind == "batting":
                metric_col = find_col(cols, [r"^Runs$", r"Runs"])
            else:
                metric_col = find_col(cols, [r"^Wkts$", r"Wickets", r"Wkts"])
            if metric_col:
                candidate = (df, player_col, metric_col)
                break
        if not candidate:
            if page == 1:
                raise RuntimeError(f"No ESPN {kind} records table found")
            break

        df, pc, mc = candidate
        new_on_page = 0
        for _, r in df.iterrows():
            player = str(r.get(pc, "")).strip()
            if not player or player.lower() == "nan":
                continue
            metric = clean_num(r.get(mc))
            if metric is None:
                continue
            key = (player, metric)
            if key in seen:
                continue
            seen.add(key); new_on_page += 1
            rec = {"player": player}
            cols = list(df.columns)
            if kind == "batting":
                rec.update({
                    "runs": int(metric),
                    "matches": clean_num(r.get(find_col(cols,[r"^Mat$",r"Matches"]))),
                    "innings": clean_num(r.get(find_col(cols,[r"^Inns$",r"Innings"]))),
                    "average": clean_num(r.get(find_col(cols,[r"^Ave$",r"Average"]))),
                    "strike_rate": clean_num(r.get(find_col(cols,[r"^SR$",r"Strike Rate"]))),
                    "high_score": str(r.get(find_col(cols,[r"^HS$",r"High Score"]), "")),
                    "fifties": clean_num(r.get(find_col(cols,[r"^50$",r"50s?"]))),
                    "hundreds": clean_num(r.get(find_col(cols,[r"^100$",r"100s?"]))),
                })
            else:
                rec.update({
                    "wickets": int(metric),
                    "matches": clean_num(r.get(find_col(cols,[r"^Mat$",r"Matches"]))),
                    "innings": clean_num(r.get(find_col(cols,[r"^Inns$",r"Innings"]))),
                    "average": clean_num(r.get(find_col(cols,[r"^Ave$",r"Average"]))),
                    "economy": clean_num(r.get(find_col(cols,[r"^Econ$",r"Economy"]))),
                    "strike_rate": clean_num(r.get(find_col(cols,[r"^SR$",r"Strike Rate"]))),
                    "best": str(r.get(find_col(cols,[r"^BBI$",r"Best"]), "")),
                })
            rows.append(rec)
        if new_on_page == 0:
            break
        # Be polite to the source.
        time.sleep(0.7)

    if kind == "batting":
        rows.sort(key=lambda x: x["runs"], reverse=True)
        # Guard against silently accepting an old cached pre-2026 table.
        if not rows or rows[0]["runs"] < 1294:
            raise RuntimeError("ESPN batting table appears stale or incomplete (leader below 1294)")
    else:
        rows.sort(key=lambda x: x["wickets"], reverse=True)
        if not rows or rows[0]["wickets"] < 51:
            raise RuntimeError("ESPN bowling table appears incomplete")
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows

def cricbuzz_standings():
    html = get(CRICBUZZ_TABLE)
    # First try semantic HTML tables.
    try:
        for df in pd.read_html(StringIO(html)):
            df = normalize_cols(df)
            cols = list(df.columns)
            pts_col = find_col(cols, [r"^Pts$", r"Points"])
            nrr_col = find_col(cols, [r"^NRR$", r"NRR"])
            team_col = find_col(cols, [r"^Teams?$", r"Team"])
            if pts_col and nrr_col and team_col and len(df) >= 8:
                out=[]
                for _, r in df.iterrows():
                    team = str(r.get(team_col,"")).strip()
                    code = next((c for c,n in TEAM_CODES.items() if c in team or n.lower() in team.lower()), None)
                    if not code: continue
                    cols2=list(df.columns)
                    out.append({
                        "team": TEAM_CODES[code], "code":code,
                        "p": int(clean_num(r.get(find_col(cols2,[r"^P$",r"Played"])),0)),
                        "w": int(clean_num(r.get(find_col(cols2,[r"^W$",r"Won"])),0)),
                        "l": int(clean_num(r.get(find_col(cols2,[r"^L$",r"Lost"])),0)),
                        "nr": int(clean_num(r.get(find_col(cols2,[r"^NR$",r"No Result"])),0)),
                        "pts": int(clean_num(r.get(pts_col),0)),
                        "nrr": float(clean_num(r.get(nrr_col),0)),
                    })
                if len(out) == 8:
                    out.sort(key=lambda x:(x["pts"],x["nrr"]), reverse=True)
                    for i,x in enumerate(out,1): x["rank"]=i
                    return out
    except Exception:
        pass

    # Fallback tailored to Cricbuzz's text layout.
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    out=[]
    for code, name in TEAM_CODES.items():
        # Locate code followed by P W L NR Pts NRR. Allows plus/minus NRR.
        m = re.search(rf"\b{re.escape(code)}\b\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+([+\-]?\d+\.\d+)", text)
        if m:
            p,w,l,nr,pts,nrr=m.groups()
            out.append({"team":name,"code":code,"p":int(p),"w":int(w),"l":int(l),"nr":int(nr),"pts":int(pts),"nrr":float(nrr)})
    if len(out) != 8:
        raise RuntimeError(f"Could not parse all 8 Cricbuzz standings rows (got {len(out)})")
    out.sort(key=lambda x:(x["pts"],x["nrr"]), reverse=True)
    for i,x in enumerate(out,1): x["rank"]=i
    return out

def main():
    data=load()
    refreshed=now()
    failures=[]

    for key, url, fallback_url, kind in [
        ("career_batting", ESPN_BAT, ESPN_BAT_LEGACY, "batting"),
        ("career_bowling", ESPN_BOWL, ESPN_BOWL_LEGACY, "bowling"),
    ]:
        try:
            rows=espn_tables(url, kind, fallback_url=fallback_url)
            data[key]=rows
            data.setdefault("sources",{}).setdefault(key,{})
            data["sources"][key].update({"name":f"ESPNcricinfo – Hundred men's career {kind}", "url":url, "last_success":refreshed, "status":"ok"})
        except Exception as e:
            failures.append(f"{key}: {e}")
            data.setdefault("sources",{}).setdefault(key,{})
            data["sources"][key]["status"]="error_preserving_last_good"
            data["sources"][key]["error"]=str(e)

    try:
        data["standings"]=cricbuzz_standings()
        data.setdefault("sources",{}).setdefault("standings",{})
        data["sources"]["standings"].update({"name":"Cricbuzz – Hundred 2026 points table","url":CRICBUZZ_TABLE,"last_success":refreshed,"status":"ok"})
    except Exception as e:
        failures.append(f"standings: {e}")
        data.setdefault("sources",{}).setdefault("standings",{})
        data["sources"]["standings"]["status"]="error_preserving_last_good"
        data["sources"]["standings"]["error"]=str(e)

    data["generated_at_utc"]=refreshed
    data["status"]={
        "overall":"ok" if not failures else "partial",
        "stale": bool(failures),
        "message":"All configured sources refreshed." if not failures else "Some sources failed; last-good values were preserved.",
        "failures":failures
    }
    DATA_FILE.write_text(json.dumps(data,indent=2,ensure_ascii=False),encoding="utf-8")
    print(json.dumps(data["status"],indent=2))
    # Do not fail the deployment if one source is temporarily blocked. The UI
    # exposes source health and retains the last verified values.
    return 0

if __name__=="__main__":
    raise SystemExit(main())
