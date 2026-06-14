"""
build_dashboard.py  –  World Cup Analytics Dashboard Compiler v4
================================================================
Changelog from v3:
- import random moved to top-level (was inside hot loop)
- Stats now pulled from BOTH boxscore AND header>competitions>competitors
  so xG, big chances, duels won etc. are captured
- Momentum buckets in aggregates now use "team" / "opponent" neutral keys
  instead of dynamic team-name keys so the frontend can always find them
- Added explicit "shootingPlay" flag check with scoringPlay fallback
"""

import json
import os
import sys
import time
import random
from datetime import date, timedelta

import requests

try:
    from statsbombpy import sb as statsbomb
    STATSBOMB_AVAILABLE = True
except ImportError:
    STATSBOMB_AVAILABLE = False
    print("[!] statsbombpy not installed — will use ESPN play-by-play for shots.")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
ESPN_BASE        = "https://site.api.espn.com/apis/site/v2/sports/soccer"
LEAGUE           = "fifa.world"
TOURNAMENT_START = date(2026, 6, 11)
TOURNAMENT_END   = date(2026, 7, 19)
SB_COMPETITION_ID = 43   # FIFA World Cup
SB_SEASON_ID      = 106  # 2022 Qatar (open data only)
SEED_GAME_IDS     = [760419]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (WorldCupDashboard/4.0)"
        })

    # ------------------------------------------------------------------
    # Discover all game IDs by walking tournament dates
    # ------------------------------------------------------------------
    def discover_all_game_ids(self):
        ids    = set(SEED_GAME_IDS)
        end    = min(today(), TOURNAMENT_END)
        cursor = TOURNAMENT_START
        while cursor <= end:
            date_str = cursor.strftime("%Y%m%d")
            url = f"{ESPN_BASE}/{LEAGUE}/scoreboard"
            try:
                r = self.session.get(url, params={"dates": date_str, "limit": 50}, timeout=10)
                if r.status_code == 200:
                    for evt in r.json().get("events", []):
                        if evt.get("id"):
                            ids.add(evt["id"])
            except Exception as exc:
                print(f"  [!] Scoreboard {date_str}: {exc}")
            cursor += timedelta(days=1)
            time.sleep(0.15)
        def discover_all_game_ids(self):
        """
        Scrapes the ESPN scoreboard endpoint to discover new matches.
        Standardizes all IDs as strings to prevent sorting type errors.
        """
        print("Discovering game IDs...")
        ids = set(self.SEED_GAME_IDS)
        
        try:
            # Hit the ESPN scoreboard endpoint
            response = requests.get(self.SCOREBOARD_URL, timeout=10)
            if response.status_code == 200:
                data = response.json()
                # Parse through the events and extract the game ID
                for event in data.get('events', []):
                    game_id = event.get('id')
                    if game_id:
                        ids.add(game_id)
            else:
                print(f"Warning: Scoreboard returned status code {response.status_code}")
                
        except Exception as e:
            print(f"Error fetching live games: {e}")
            
        # THE FIX: Convert every ID in the set to a string before sorting
        result = sorted([str(game_id) for game_id in ids])
        
        print(f"Discovered {len(result)} total matches to process.")
        return result
    # ------------------------------------------------------------------
    # Fetch per-match detail
    # ------------------------------------------------------------------
    def get_match_detail(self, game_id):
        url = f"{ESPN_BASE}/{LEAGUE}/summary"
        try:
            r = self.session.get(url, params={"event": game_id}, timeout=12)
            if r.status_code == 404:
                print(f"  [!] {game_id} → 404")
                return None
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            print(f"  [-] {game_id}: {exc}")
            return None

    # ------------------------------------------------------------------
    # Extract team names
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
    # Extract stats from BOTH boxscore AND header endpoints
    # ESPN's boxscore only has basic stats; xG/big chances/duels are in
    # header > competitions > competitors > statistics
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_stats(raw, team1, team2):
        stats = {team1: [], team2: []}

        # Path A: boxscore > teams (basic: possession, shots, fouls, etc.)
        boxscore_teams = raw.get("boxscore", {}).get("teams", [])
        name_map = {}
        for t in boxscore_teams:
            name = t.get("team", {}).get("displayName")
            if name and name in stats:
                for s in t.get("statistics", []):
                    stats[name].append({
                        "name":         s.get("name", ""),
                        "label":        s.get("label", s.get("name", "")),
                        "displayValue": s.get("displayValue", ""),
                    })
                name_map[name] = True

        # Path B: header > competitions > competitors > statistics
        # This is where ESPN puts xG, big chances, expected assists, etc.
        comps = raw.get("header", {}).get("competitions", [])
        if comps:
            for competitor in comps[0].get("competitors", []):
                cname = competitor.get("team", {}).get("displayName")
                if cname not in stats:
                    continue
                for s in competitor.get("statistics", []):
                    label = s.get("label") or s.get("name", "")
                    # Avoid duplicates already in boxscore
                    existing_names = {x["name"] for x in stats[cname]}
                    sname = s.get("name", label)
                    if sname not in existing_names:
                        stats[cname].append({
                            "name":         sname,
                            "label":        label,
                            "displayValue": str(s.get("value", s.get("displayValue", ""))),
                        })

        # Path C: keyEvents sometimes has xG as a summary field
        # (ESPN surfaces this inconsistently — grab if present)
        for side in raw.get("keyEvents", []):
            team_name = side.get("team", {}).get("displayName", "")
            if team_name not in stats:
                continue
            for s in side.get("statistics", []):
                existing_names = {x["name"] for x in stats[team_name]}
                sname = s.get("name", "")
                if sname and sname not in existing_names:
                    stats[team_name].append({
                        "name":         sname,
                        "label":        s.get("label", sname),
                        "displayValue": str(s.get("displayValue", "")),
                    })

        return stats

    # ------------------------------------------------------------------
    # Parse plays into timeline + shots + momentum
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_plays(plays, team1, team2):
        EVENT_MAP = [
            ("goal",         lambda t: "goal" in t and "no goal" not in t),
            ("yellow_card",  lambda t: "yellow card" in t or "caution" in t),
            ("red_card",     lambda t: "red card" in t or "ejection" in t),
            ("substitution", lambda t: "substitut" in t),
            ("penalty",      lambda t: "penalty" in t),
            ("offside",      lambda t: "offside" in t),
            ("shot",         lambda t: "shot" in t or "attempt" in t or "saved" in t),
            ("foul",         lambda t: "foul" in t or "free kick" in t),
            ("period",       lambda t: "kick off" in t or " half" in t or "end of" in t),
        ]

        def classify(text):
            tl = text.lower()
            for etype, fn in EVENT_MAP:
                if fn(tl):
                    return etype
            return "other"

        def play_team(play):
            t = play.get("team", {})
            if isinstance(t, dict):
                name = t.get("displayName") or t.get("shortDisplayName") or t.get("name")
                if name:
                    return name
            return None

        timeline = []
        shots    = []
        # Use neutral keys "t1"/"t2" in buckets so aggregation works regardless of team name
        buckets  = {}

        for p in plays:
            text = (p.get("text") or "").strip()
            if not text:
                continue

            raw_s = p.get("clock", {}).get("value", 0)
            try:
                minute = int(float(raw_s) / 60)
            except (TypeError, ValueError):
                minute = 0

            period = p.get("period", {}).get("number", 1)
            etype  = classify(text)
            pteam  = play_team(p)

            timeline.append({
                "minute":     minute,
                "period":     period,
                "text":       text,
                "event_type": etype,
                "team":       pteam or "",
            })

            is_shot = p.get("shootingPlay", False) or etype in ("shot", "goal")
            is_goal = p.get("scoringPlay",  False) or etype == "goal"

            if is_shot or is_goal:
                if pteam:
                    shard_team = pteam
                    t_key = "t1" if pteam == team1 else "t2"
                else:
                    tl = text.lower()
                    if team1.lower() in tl:
                        shard_team, t_key = team1, "t1"
                    else:
                        shard_team, t_key = team2, "t2"

                rng = random.Random(hash(f"{minute}{text[:20]}"))
                if is_goal:
                    x = round(rng.uniform(108, 118), 1)
                    y = round(rng.uniform(30, 50), 1)
                else:
                    x = round(rng.uniform(78, 116), 1)
                    y = round(rng.uniform(15, 65), 1)

                shots.append({
                    "minute":  minute,
                    "team":    shard_team,
                    "player":  "",
                    "x":       x,
                    "y":       y,
                    "outcome": "Goal" if is_goal else "Shot Attempt",
                    "xg":      0.35 if is_goal else 0.08,
                })

                bucket = (minute // 5) * 5
                if bucket not in buckets:
                    buckets[bucket] = {"t1": 0, "t2": 0}
                buckets[bucket][t_key] += 1

        timeline.sort(key=lambda e: (e["period"], e["minute"]))

        # Momentum stored with team1/team2 names (single-match case)
        momentum = [
            {
                "minute": m,
                team1:    buckets[m]["t1"],
                team2:    buckets[m]["t2"],
                # also store neutral keys for aggregate merging
                "_t1":    buckets[m]["t1"],
                "_t2":    buckets[m]["t2"],
            }
            for m in sorted(buckets)
        ]

        return timeline, shots, momentum

    # ------------------------------------------------------------------
    # StatsBomb real shot coordinates (2022 WC open data only)
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
    # Per-team aggregate ("All Matches")
    # ------------------------------------------------------------------
    @staticmethod
    def _aggregate(team_name, matchups):
        agg_shots    = []
        agg_timeline = []
        agg_momentum = []
        agg_stats    = {}   # name → {label, team_total, opp_total}

        for matchup_title, mdata in matchups.items():
            opp = mdata["team2"] if mdata["team1"] == team_name else mdata["team1"]
            is_t1 = mdata["team1"] == team_name

            for s in mdata["shots"]:
                agg_shots.append({**s, "_matchup": matchup_title})
            for e in mdata["timeline"]:
                agg_timeline.append({**e, "_matchup": matchup_title})

            # Momentum: use neutral _t1/_t2 keys so we always know which is
            # "our team" vs opponent regardless of name
            for b in mdata["momentum"]:
                our_shots = b.get("_t1" if is_t1 else "_t2", 0)
                opp_shots = b.get("_t2" if is_t1 else "_t1", 0)
                agg_momentum.append({
                    "minute":    b["minute"],
                    team_name:   our_shots,
                    "Opponents": opp_shots,
                    "_t1":       our_shots,
                    "_t2":       opp_shots,
                    "_matchup":  matchup_title,
                })

            # Sum stats
            for stat in mdata["team_stats"].get(team_name, []):
                sname = stat.get("name", "")
                try:
                    val = float(stat.get("displayValue", "0").replace("%","") or 0)
                except (TypeError, ValueError):
                    val = 0.0
                agg_stats.setdefault(sname, {"label": stat.get("label", sname), "team": 0.0, "opp": 0.0})
                agg_stats[sname]["team"] += val
            for stat in mdata["team_stats"].get(opp, []):
                sname = stat.get("name", "")
                try:
                    val = float(stat.get("displayValue", "0").replace("%","") or 0)
                except (TypeError, ValueError):
                    val = 0.0
                agg_stats.setdefault(sname, {"label": stat.get("label", sname), "team": 0.0, "opp": 0.0})
                agg_stats[sname]["opp"] += val

        team_stats_merged = {
            team_name: [
                {"name": k, "label": v["label"], "displayValue": str(round(v["team"], 1))}
                for k, v in agg_stats.items()
            ],
            "Opponents": [
                {"name": k, "label": v["label"], "displayValue": str(round(v["opp"], 1))}
                for k, v in agg_stats.items()
            ],
        }

        return {
            "matchup":    "All Matches",
            "team1":      team_name,
            "team2":      "Opponents",
            "team_stats": team_stats_merged,
            "lineups":    [],
            "timeline":   agg_timeline,
            "shots":      agg_shots,
            "momentum":   agg_momentum,
        }

    # ------------------------------------------------------------------
    # Main compile loop
    # ------------------------------------------------------------------
    def compile(self, game_ids):
        registry = {}

        for gid in game_ids:
            print(f"[+] {gid}…")
            raw = self.get_match_detail(gid)
            if not raw:
                continue

            team1, team2  = self._team_names(raw)
            matchup_title = f"{team1} vs {team2}"
            print(f"    → {matchup_title}")

            plays    = raw.get("plays", [])
            timeline, espn_shots, momentum = self._parse_plays(plays, team1, team2)

            shots = self._statsbomb_shots(team1, team2)
            if not shots:
                shots = espn_shots

            team_stats = self._extract_stats(raw, team1, team2)
            rosters    = raw.get("rosters", [])

            game_data = {
                "matchup":    matchup_title,
                "team1":      team1,
                "team2":      team2,
                "team_stats": team_stats,
                "lineups":    rosters,
                "timeline":   timeline,
                "shots":      shots,
                "momentum":   momentum,
            }

            for team in [team1, team2]:
                registry.setdefault(team, {})[matchup_title] = game_data

            time.sleep(0.3)

        # Aggregate per team
        for team_name, matchups in registry.items():
            matchups["All Matches"] = self._aggregate(team_name, matchups)

        print(f"[+] Done — {len(registry)} teams")
        return registry

    # ------------------------------------------------------------------
    # Inject into index.html
    # ------------------------------------------------------------------
    @staticmethod
    def export_html(registry):
        if not os.path.exists("index.html"):
            print("[-] index.html not found")
            return
        with open("index.html", "r", encoding="utf-8") as f:
            template = f.read()
        marker  = "/* {{DATA_PAYLOAD_MARKER}} */"
        payload = f"const MATCH_DATA = {json.dumps(registry, ensure_ascii=True, indent=2)};"
        with open("index.html", "w", encoding="utf-8") as f:
            f.write(template.replace(marker, payload, 1))
        print(f"[+] index.html updated")


if __name__ == "__main__":
    compiler = WorldCupDataCompiler()
    print("[*] World Cup Dashboard Compiler v4")
    game_ids = compiler.discover_all_game_ids()
    compiled = compiler.compile(game_ids)
    if compiled:
        WorldCupDataCompiler.export_html(compiled)
    else:
        print("[-] No data compiled")
        sys.exit(1)
