"""
build_dashboard.py  –  World Cup Dashboard Compiler v7
=======================================================
Fixes vs v6:
1. Score: read from header.competitions[0].competitors[].score (not counted from shots)
2. Momentum: scraped from ESPN match page HTML momentum bars via CSS calc() values
3. Shot coords: goals use exact minute from plays[], non-goal shots distributed realistically
4. All game IDs forced to str; marker-or-regex replacement in export_html
"""

import json, os, sys, time, random, re
from datetime import date, timedelta

import requests

try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False

try:
    from statsbombpy import sb as statsbomb
    STATSBOMB_AVAILABLE = True
except ImportError:
    STATSBOMB_AVAILABLE = False

ESPN_BASE        = "https://site.api.espn.com/apis/site/v2/sports/soccer"
ESPN_WEB         = "https://www.espn.com/soccer/match/_/gameId"
LEAGUE           = "fifa.world"
TOURNAMENT_START = date(2026, 6, 11)
TOURNAMENT_END   = date(2026, 7, 19)
SB_COMPETITION_ID = 43
SB_SEASON_ID      = 106
SEED_GAME_IDS     = ["760419"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_clock(s):
    """'67'' -> 67,  '90'+3'' -> 93"""
    if not s:
        return 0
    s = str(s).replace("'", "").strip()
    if '+' in s:
        parts = s.split('+')
        try:
            return int(parts[0]) + int(parts[1])
        except (ValueError, IndexError):
            pass
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return 0

def safe_float(val, default=0.0):
    try:
        f = float(val)
        return default if (f != f) else f
    except (TypeError, ValueError):
        return default

def extract_name(val):
    if isinstance(val, dict):
        return val.get("name", "Unknown")
    return str(val) if val is not None else "Unknown"

def today():
    return date.today()


# ---------------------------------------------------------------------------
# Compiler
# ---------------------------------------------------------------------------

class WorldCupDataCompiler:

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0 (WorldCupDashboard/7.0)"})

    # ------------------------------------------------------------------
    # Discover game IDs
    # ------------------------------------------------------------------
    def discover_all_game_ids(self):
        ids  = set(SEED_GAME_IDS)
        end  = min(today(), TOURNAMENT_END)
        cur  = TOURNAMENT_START
        while cur <= end:
            url = f"{ESPN_BASE}/{LEAGUE}/scoreboard"
            try:
                r = self.session.get(url, params={"dates": cur.strftime("%Y%m%d"), "limit": 50}, timeout=10)
                if r.status_code == 200:
                    for evt in r.json().get("events", []):
                        eid = evt.get("id")
                        if eid:
                            ids.add(str(eid))
            except Exception as exc:
                print(f"  [!] Scoreboard {cur}: {exc}")
            cur += timedelta(days=1)
            time.sleep(0.15)
        result = sorted(ids)
        print(f"[+] Discovered {len(result)} event ID(s)")
        return result

    # ------------------------------------------------------------------
    # Fetch match summary (API)
    # ------------------------------------------------------------------
    def get_match_detail(self, game_id):
        url = f"{ESPN_BASE}/{LEAGUE}/summary"
        try:
            r = self.session.get(url, params={"event": game_id}, timeout=12)
            if r.status_code == 404:
                print(f"  [!] {game_id} -> 404")
                return None
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            print(f"  [-] {game_id}: {exc}")
            return None

    # ------------------------------------------------------------------
    # Scrape momentum from ESPN match page HTML
    # ESPN renders momentum as a series of divs with:
    #   style="height: Xpx; left: calc(Y% + ...); width: calc(Z% - ...);"
    # left % = time position (0-100% maps to 0-90 min)
    # height px = momentum intensity
    # We need to know which team each bar belongs to — ESPN uses two
    # sibling containers (home/away) that we identify by DOM position.
    # ------------------------------------------------------------------
    def scrape_momentum(self, game_id, team1, team2):
        if not BS4_AVAILABLE:
            print("  [!] beautifulsoup4 not installed, skipping momentum scrape")
            return []
        try:
            url = f"{ESPN_WEB}/{game_id}"
            r = self.session.get(url, timeout=15)
            if r.status_code != 200:
                print(f"  [!] Momentum page {url} -> {r.status_code}")
                return []

            soup = BeautifulSoup(r.text, "html.parser")

            # Find all momentum bar divs — they have inline style with
            # both "height" and "calc" in the style attribute
            momentum_bars = []
            all_divs = soup.find_all("div", style=True)
            bar_divs = [
                d for d in all_divs
                if "calc(" in d.get("style","") and "height:" in d.get("style","")
                and "left:" in d.get("style","")
            ]

            print(f"  [+] Found {len(bar_divs)} momentum bar divs")
            if not bar_divs:
                return []

            # Parse each bar: extract left% (time) and height (intensity)
            # Determine team by which half of the div list the bar is in
            half = len(bar_divs) // 2
            for i, div in enumerate(bar_divs):
                style = div.get("style", "")
                # Extract height in px
                h_match = re.search(r'height:\s*([\d.]+)px', style)
                # Extract left calc percentage
                l_match = re.search(r'left:\s*calc\(([\d.]+)%', style)
                if not h_match or not l_match:
                    continue
                height_px  = float(h_match.group(1))
                left_pct   = float(l_match.group(1))
                minute     = round(left_pct * 90 / 100)
                team       = team1 if i < half else team2
                momentum_bars.append({
                    "minute":    minute,
                    "team":      team,
                    "intensity": round(height_px, 2),
                })

            # Convert to bucketed format keyed by minute
            buckets = {}
            for bar in momentum_bars:
                m   = (bar["minute"] // 1) * 1   # 1-min resolution
                key = bar["team"]
                if m not in buckets:
                    buckets[m] = {team1: 0.0, team2: 0.0}
                buckets[m][key] += bar["intensity"]

            result = []
            for minute in sorted(buckets):
                result.append({
                    "minute": minute,
                    team1:    round(buckets[minute].get(team1, 0), 2),
                    team2:    round(buckets[minute].get(team2, 0), 2),
                    "_t1":    round(buckets[minute].get(team1, 0), 2),
                    "_t2":    round(buckets[minute].get(team2, 0), 2),
                })
            print(f"  [+] Momentum: {len(result)} time points")
            return result

        except Exception as exc:
            print(f"  [!] Momentum scrape error: {exc}")
            return []

    # ------------------------------------------------------------------
    # Team names
    # ------------------------------------------------------------------
    @staticmethod
    def _team_names(raw):
        teams_list = raw.get("boxscore", {}).get("teams", [])
        if len(teams_list) >= 2:
            return (
                teams_list[0].get("team", {}).get("displayName", "Home"),
                teams_list[1].get("team", {}).get("displayName", "Away"),
            )
        comps = raw.get("header", {}).get("competitions", [{}])
        competitors = comps[0].get("competitors", []) if comps else []
        if len(competitors) >= 2:
            return (
                competitors[0].get("team", {}).get("displayName", "Home"),
                competitors[1].get("team", {}).get("displayName", "Away"),
            )
        return "Home", "Away"

    # ------------------------------------------------------------------
    # Extract score from header
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_score(raw):
        """Returns (score1, score2) as strings, e.g. ('4', '1')"""
        comps = raw.get("header", {}).get("competitions", [])
        if not comps:
            return "?", "?"
        competitors = comps[0].get("competitors", [])
        if len(competitors) >= 2:
            return (
                str(competitors[0].get("score", "?")),
                str(competitors[1].get("score", "?")),
            )
        return "?", "?"

    # ------------------------------------------------------------------
    # Extract timeline + shots from roster plays
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_events(rosters):
        timeline = []
        shots    = []
        buckets  = {}   # for shot-based momentum fallback

        for team_entry in rosters:
            team_name = (team_entry.get("team") or {}).get("displayName", "Unknown")

            for player in team_entry.get("roster") or []:
                athlete  = (player.get("athlete") or {})
                p_name   = athlete.get("displayName", "Unknown")

                # ── Timeline events from plays[] ─────────────────────
                for play in player.get("plays") or []:
                    clock_raw = ((play.get("clock") or {}).get("displayValue", ""))
                    minute    = parse_clock(clock_raw)
                    period    = 1 if minute <= 45 else 2

                    is_goal   = bool(play.get("scoringPlay")) and bool(play.get("didScore"))
                    is_assist = bool(play.get("scoringPlay")) and bool(play.get("didAssist"))
                    is_sub    = bool(play.get("substitution"))
                    is_yc     = bool(play.get("yellowCard"))
                    is_rc     = bool(play.get("redCard"))
                    is_pk     = bool(play.get("penaltyKick"))
                    is_og     = bool(play.get("ownGoal"))

                    if is_og:
                        etype = "goal"
                        text  = f"OWN GOAL: {p_name} ({team_name}) {clock_raw}"
                    elif is_goal:
                        etype = "goal"
                        text  = f"GOAL: {p_name} ({team_name}) {clock_raw}"
                    elif is_assist:
                        etype = "other"
                        text  = f"ASSIST: {p_name} ({team_name}) {clock_raw}"
                    elif is_yc:
                        etype = "yellow_card"
                        text  = f"YELLOW CARD: {p_name} ({team_name}) {clock_raw}"
                    elif is_rc:
                        etype = "red_card"
                        text  = f"RED CARD: {p_name} ({team_name}) {clock_raw}"
                    elif is_sub:
                        etype = "substitution"
                        if player.get("subbedOut"):
                            sub_for = (((player.get("subbedOutFor") or {}).get("athlete") or {})).get("displayName", "?")
                            text = f"SUB: {p_name} OFF → {sub_for} ON ({team_name}) {clock_raw}"
                        else:
                            sub_for = (((player.get("subbedInFor") or {}).get("athlete") or {})).get("displayName", "?")
                            text = f"SUB: {p_name} ON (replaces {sub_for}) ({team_name}) {clock_raw}"
                    elif is_pk:
                        etype = "penalty"
                        text  = f"PENALTY: {p_name} ({team_name}) {clock_raw}"
                    else:
                        continue

                    timeline.append({
                        "minute":     minute,
                        "period":     period,
                        "text":       text,
                        "event_type": etype,
                        "team":       team_name,
                        "player":     p_name,
                    })

                    # Goal shot with real minute
                    if is_goal or is_og:
                        rng = random.Random(hash(f"{p_name}{minute}goal"))
                        shots.append({
                            "minute":  minute,
                            "team":    team_name,
                            "player":  p_name,
                            "x":       round(rng.uniform(108, 118), 1),
                            "y":       round(rng.uniform(30, 50), 1),
                            "outcome": "Goal",
                            "xg":      0.35,
                        })
                        b = (minute // 5) * 5
                        buckets.setdefault(b, {})
                        buckets[b][team_name] = buckets[b].get(team_name, 0) + 1

                # ── Non-goal shots from aggregate stats ──────────────
                stats_map = {s.get("name", ""): s for s in player.get("stats") or []}
                total_shots = int(safe_float((stats_map.get("totalShots") or {}).get("value", 0)))
                goals       = int(safe_float((stats_map.get("totalGoals") or {}).get("value", 0)))
                on_target   = int(safe_float((stats_map.get("shotsOnTarget") or {}).get("value", 0)))
                non_goal    = max(0, total_shots - goals)

                for i in range(non_goal):
                    rng = random.Random(hash(f"{p_name}_shot_{i}"))
                    minute_est = rng.randint(1, 90)
                    is_ot = i < max(0, on_target - goals)
                    shots.append({
                        "minute":  minute_est,
                        "team":    team_name,
                        "player":  p_name,
                        "x":       round(rng.uniform(80, 116), 1),
                        "y":       round(rng.uniform(15, 65), 1),
                        "outcome": "On Target" if is_ot else "Off Target",
                        "xg":      round(rng.uniform(0.03, 0.25), 2),
                    })
                    b = (minute_est // 5) * 5
                    buckets.setdefault(b, {})
                    buckets[b][team_name] = buckets[b].get(team_name, 0) + 1

        timeline.sort(key=lambda e: (e["period"], e["minute"]))
        return timeline, shots, buckets

    # ------------------------------------------------------------------
    # Shot-based momentum fallback
    # ------------------------------------------------------------------
    @staticmethod
    def _momentum_from_buckets(buckets, team1, team2):
        return [
            {
                "minute": m,
                team1:    buckets[m].get(team1, 0),
                team2:    buckets[m].get(team2, 0),
                "_t1":    buckets[m].get(team1, 0),
                "_t2":    buckets[m].get(team2, 0),
            }
            for m in sorted(buckets)
        ]

    # ------------------------------------------------------------------
    # Team stats
    # ------------------------------------------------------------------
    @staticmethod
    def _team_stats(raw, team1, team2):
        stats = {team1: [], team2: []}
        for t in raw.get("boxscore", {}).get("teams", []):
            name = (t.get("team") or {}).get("displayName")
            if name in stats:
                for s in t.get("statistics") or []:
                    stats[name].append({
                        "name":         s.get("name", ""),
                        "label":        s.get("label", s.get("name", "")),
                        "displayValue": str(s.get("displayValue", "")),
                    })
        comps = raw.get("header", {}).get("competitions", [])
        if comps:
            for comp in comps[0].get("competitors", []):
                cname = (comp.get("team") or {}).get("displayName")
                if cname not in stats:
                    continue
                existing = {x["name"] for x in stats[cname]}
                for s in comp.get("statistics") or []:
                    sname = s.get("name", s.get("label", ""))
                    if sname and sname not in existing:
                        stats[cname].append({
                            "name":         sname,
                            "label":        s.get("label", sname),
                            "displayValue": str(s.get("value", s.get("displayValue", ""))),
                        })
        return stats

    # ------------------------------------------------------------------
    # Strip plays from lineups before storing
    # ------------------------------------------------------------------
    @staticmethod
    def _clean_lineups(rosters):
        clean = []
        for team_entry in rosters:
            team_copy = dict(team_entry)
            team_copy["roster"] = [
                {k: v for k, v in p.items() if k != "plays"}
                for p in team_entry.get("roster") or []
            ]
            clean.append(team_copy)
        return clean

    # ------------------------------------------------------------------
    # StatsBomb
    # ------------------------------------------------------------------
    def _statsbomb_shots(self, team1, team2):
        if not STATSBOMB_AVAILABLE:
            return []
        try:
            matches = statsbomb.matches(competition_id=SB_COMPETITION_ID, season_id=SB_SEASON_ID)
            t1l, t2l = team1.lower(), team2.lower()
            match_id = None
            for _, row in matches.iterrows():
                h = str(row.get("home_team", "")).lower()
                a = str(row.get("away_team", "")).lower()
                if (t1l in h or h in t1l or t1l in a or a in t1l) and \
                   (t2l in h or h in t2l or t2l in a or a in t2l):
                    match_id = row["match_id"]
                    break
            if not match_id:
                return []
            events   = statsbomb.events(match_id=match_id)
            type_col = events["type"].apply(extract_name)
            shots_df = events[type_col == "Shot"]
            result   = []
            for _, row in shots_df.iterrows():
                loc = row.get("location") or [0, 0]
                result.append({
                    "minute":  int(row.get("minute", 0)),
                    "team":    extract_name(row.get("team", "")),
                    "player":  extract_name(row.get("player", "")),
                    "x":       safe_float(loc[0] if len(loc) > 0 else 0),
                    "y":       safe_float(loc[1] if len(loc) > 1 else 0),
                    "outcome": extract_name(row.get("shot_outcome", "Unknown")),
                    "xg":      safe_float(row.get("shot_statsbomb_xg", 0.0)),
                })
            return result
        except Exception as exc:
            print(f"  [!] StatsBomb: {exc}")
            return []

    # ------------------------------------------------------------------
    # Aggregate
    # ------------------------------------------------------------------
    @staticmethod
    def _aggregate(team_name, matchups):
        agg_shots, agg_tl, agg_mom, agg_stats = [], [], [], {}
        for title, mdata in matchups.items():
            opp   = mdata["team2"] if mdata["team1"] == team_name else mdata["team1"]
            is_t1 = mdata["team1"] == team_name
            for s in mdata["shots"]:
                agg_shots.append({**s, "_matchup": title})
            for e in mdata["timeline"]:
                agg_tl.append({**e, "_matchup": title})
            for b in mdata["momentum"]:
                our   = b.get("_t1" if is_t1 else "_t2", 0)
                opp_v = b.get("_t2" if is_t1 else "_t1", 0)
                agg_mom.append({
                    "minute": b["minute"], team_name: our,
                    "Opponents": opp_v, "_t1": our, "_t2": opp_v,
                    "_matchup": title,
                })
            for stat in mdata["team_stats"].get(team_name, []):
                sname = stat.get("name", "")
                try:
                    val = float(str(stat.get("displayValue","0")).replace("%","") or 0)
                except (TypeError, ValueError):
                    val = 0.0
                agg_stats.setdefault(sname, {"label": stat.get("label", sname), "team": 0.0, "opp": 0.0})
                agg_stats[sname]["team"] += val
            for stat in mdata["team_stats"].get(opp, []):
                sname = stat.get("name", "")
                try:
                    val = float(str(stat.get("displayValue","0")).replace("%","") or 0)
                except (TypeError, ValueError):
                    val = 0.0
                agg_stats.setdefault(sname, {"label": stat.get("label", sname), "team": 0.0, "opp": 0.0})
                agg_stats[sname]["opp"] += val
        return {
            "matchup": "All Matches", "team1": team_name, "team2": "Opponents",
            "score1": "—", "score2": "—",
            "team_stats": {
                team_name:   [{"name": k, "label": v["label"], "displayValue": str(round(v["team"],1))} for k,v in agg_stats.items()],
                "Opponents": [{"name": k, "label": v["label"], "displayValue": str(round(v["opp"], 1))} for k,v in agg_stats.items()],
            },
            "lineups": [], "timeline": agg_tl, "shots": agg_shots, "momentum": agg_mom,
        }

    # ------------------------------------------------------------------
    # Main compile loop
    # ------------------------------------------------------------------
    def compile(self, game_ids):
        registry = {}
        for gid in game_ids:
            print(f"[+] {gid}...")
            raw = self.get_match_detail(gid)
            if not raw:
                continue

            team1, team2  = self._team_names(raw)
            score1, score2 = self._extract_score(raw)
            matchup_title = f"{team1} vs {team2}"
            print(f"    -> {matchup_title} ({score1}-{score2})")

            rosters    = raw.get("rosters") or []
            team_stats = self._team_stats(raw, team1, team2)

            # Extract events from roster plays FIRST
            timeline, espn_shots, buckets = self._extract_events(rosters)
            print(f"       timeline={len(timeline)} shots={len(espn_shots)}")

            # Momentum: try ESPN page scrape, fall back to shot buckets
            momentum = self.scrape_momentum(gid, team1, team2)
            if not momentum:
                print(f"       Falling back to shot-based momentum")
                momentum = self._momentum_from_buckets(buckets, team1, team2)

            # StatsBomb for real shot coords (2022 only)
            sb_shots = self._statsbomb_shots(team1, team2)
            shots = sb_shots if sb_shots else espn_shots

            # Strip plays before storing as lineups
            clean_lineups = self._clean_lineups(rosters)

            game_data = {
                "matchup":    matchup_title,
                "team1":      team1,
                "team2":      team2,
                "score1":     score1,
                "score2":     score2,
                "team_stats": team_stats,
                "lineups":    clean_lineups,
                "timeline":   timeline,
                "shots":      shots,
                "momentum":   momentum,
            }

            for team in [team1, team2]:
                registry.setdefault(team, {})[matchup_title] = game_data

            time.sleep(0.4)

        for team_name, matchups in registry.items():
            matchups["All Matches"] = self._aggregate(team_name, matchups)

        print(f"[+] Done — {len(registry)} teams")
        return registry

    # ------------------------------------------------------------------
    # Export — handles both first run (marker) and re-runs (regex replace)
    # ------------------------------------------------------------------
    @staticmethod
    def export_html(registry):
        if not os.path.exists("index.html"):
            print("[-] index.html not found")
            return
        with open("index.html", "r", encoding="utf-8") as f:
            template = f.read()

        payload = f"const MATCH_DATA = {json.dumps(registry, ensure_ascii=True, indent=2)};"
        marker  = "/* {{DATA_PAYLOAD_MARKER}} */"

        if marker in template:
            result = template.replace(marker, payload, 1)
            print("[+] First run — marker replaced")
        else:
            result = re.sub(
                r'const MATCH_DATA\s*=\s*\{.*?\};',
                payload,
                template,
                count=1,
                flags=re.DOTALL
            )
            if result == template:
                print("[-] WARNING: Could not find marker or MATCH_DATA to replace!")
            else:
                print("[+] Re-run — MATCH_DATA block replaced")

        with open("index.html", "w", encoding="utf-8") as f:
            f.write(result)
        print("[+] index.html updated")


if __name__ == "__main__":
    compiler = WorldCupDataCompiler()
    print("[*] World Cup Dashboard Compiler v7")
    game_ids = compiler.discover_all_game_ids()
    compiled = compiler.compile(game_ids)
    if compiled:
        WorldCupDataCompiler.export_html(compiled)
    else:
        print("[-] No data compiled")
        sys.exit(1)
