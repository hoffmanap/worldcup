"""
build_dashboard.py  –  World Cup Analytics Dashboard Compiler
=============================================================
Pulls every completed/scheduled 2026 FIFA World Cup match from ESPN's
public API, then optionally augments shot x/y with StatsBomb Open Data.
Writes data into index.html via a marker comment.

Key design decisions
--------------------
* Historical coverage: ESPN exposes a /scoreboard?dates=YYYYMMDD endpoint.
  We walk every date from the tournament start (2026-06-11) through today
  so the first run captures every past game, and the GH Actions cron picks
  up new ones on each subsequent run.
* Per-team aggregation: besides per-matchup entries we synthesise an
  "All Matches" aggregate so the UI can show cumulative team stats.
* Shot attribution: ESPN play-by-play `shootingPlay` / `scoringPlay` flags
  are used instead of fragile text-matching.  Team is read from the play's
  own `team.displayName` field when present.
* Momentum: same play-level team field; no more word-overlap heuristic.
"""

import json
import os
import sys
import time
from datetime import date, timedelta

import requests
import pandas as pd

try:
    from statsbombpy import sb as statsbomb
    STATSBOMB_AVAILABLE = True
except ImportError:
    STATSBOMB_AVAILABLE = False
    print("[!] statsbombpy not installed — spatial shot data will use ESPN fallback.")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ESPN_BASE  = "https://site.api.espn.com/apis/site/v2/sports/soccer"
LEAGUE     = "fifa.world"

# 2026 FIFA World Cup runs 11 Jun – 19 Jul 2026
TOURNAMENT_START = date(2026, 6, 11)
TOURNAMENT_END   = date(2026, 7, 19)

# StatsBomb — 2022 Qatar WC (open data).  2026 data won't be available yet.
SB_COMPETITION_ID = 43
SB_SEASON_ID      = 106

# Hard-coded known 2026 WC ESPN game IDs as a last-resort fallback.
# Add more as ESPN assigns them; the scraper will discover most automatically.
SEED_GAME_IDS = [760419]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def safe_float(val, default=0.0):
    try:
        f = float(val)
        return default if (f != f) else f   # NaN check without pandas
    except (TypeError, ValueError):
        return default


def extract_name(val):
    """StatsBomb returns some columns as dicts {'id':…,'name':…}."""
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
            "User-Agent": "Mozilla/5.0 (WorldCupDashboard/3.0; +github.com)"
        })

    # ------------------------------------------------------------------
    # Step 1: collect every event ID for the tournament
    # ------------------------------------------------------------------

    def discover_all_game_ids(self):
        """
        Walk every date from TOURNAMENT_START through today (inclusive)
        hitting the ESPN scoreboard endpoint.  Returns a deduplicated,
        sorted list of event IDs.
        """
        ids    = set(SEED_GAME_IDS)
        end    = min(today(), TOURNAMENT_END)
        cursor = TOURNAMENT_START

        while cursor <= end:
            date_str = cursor.strftime("%Y%m%d")
            url      = f"{ESPN_BASE}/{LEAGUE}/scoreboard"
            try:
                r = self.session.get(url, params={"dates": date_str, "limit": 50}, timeout=10)
                if r.status_code == 200:
                    for evt in r.json().get("events", []):
                        if evt.get("id"):
                            ids.add(evt["id"])
            except Exception as exc:
                print(f"  [!] Scoreboard {date_str} failed: {exc}")
            cursor += timedelta(days=1)
            time.sleep(0.15)   # polite pacing

        result = sorted(ids)
        print(f"[+] Discovered {len(result)} total event ID(s): {result}")
        return result

    # ------------------------------------------------------------------
    # Step 2: fetch per-match ESPN detail
    # ------------------------------------------------------------------

    def get_match_detail(self, game_id):
        url = f"{ESPN_BASE}/{LEAGUE}/summary"
        try:
            r = self.session.get(url, params={"event": game_id}, timeout=12)
            if r.status_code == 404:
                print(f"  [!] {game_id} → 404, skipping.")
                return None
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            print(f"  [-] {game_id} fetch error: {exc}")
            return None

    # ------------------------------------------------------------------
    # Step 3: extract structured data from raw ESPN payload
    # ------------------------------------------------------------------

    @staticmethod
    def _team_names(raw):
        """Return (team1_name, team2_name).  Multiple fallback paths."""
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

    @staticmethod
    def _parse_plays(plays, team1, team2):
        """
        Convert ESPN play array into:
          - timeline  list[dict]
          - shots     list[dict]   (ESPN fallback, no real x/y)
          - momentum  list[dict]   5-min shot buckets

        FIX: team is read from play["team"]["displayName"] when present,
        NOT from fragile word-overlap on description text.
        FIX: clock.value is total elapsed seconds → divide by 60.
        FIX: ESPN uses shootingPlay / scoringPlay boolean flags.
        """
        EVENT_MAP = {
            "goal":         lambda t: "goal" in t,
            "yellow_card":  lambda t: "yellow card" in t or "caution" in t,
            "red_card":     lambda t: "red card" in t or "ejection" in t,
            "substitution": lambda t: "substitut" in t,
            "shot":         lambda t: "shot" in t or "attempt" in t or "save" in t,
            "penalty":      lambda t: "penalty" in t,
            "offside":      lambda t: "offside" in t,
            "foul":         lambda t: "foul" in t or "free kick" in t,
            "period":       lambda t: "kick off" in t or "half" in t or "end" in t,
        }

        def classify(text):
            tl = text.lower()
            for etype, fn in EVENT_MAP.items():
                if fn(tl):
                    return etype
            return "other"

        def play_team(play):
            """Read team directly from the play object."""
            t = play.get("team", {})
            if isinstance(t, dict):
                name = t.get("displayName") or t.get("shortDisplayName") or t.get("name")
                if name:
                    return name
            return None   # unknown → caller decides

        timeline = []
        shots    = []
        buckets  = {}   # minute_bucket → {team1: n, team2: n}

        for p in plays:
            text = (p.get("text") or "").strip()
            if not text:
                continue

            # Elapsed time in minutes
            raw_s = p.get("clock", {}).get("value", 0)
            try:
                minute = int(float(raw_s) / 60)
            except (TypeError, ValueError):
                minute = 0

            period   = p.get("period", {}).get("number", 1)
            etype    = classify(text)
            pteam    = play_team(p)   # may be None

            timeline.append({
                "minute":     minute,
                "period":     period,
                "text":       text,
                "event_type": etype,
                "team":       pteam or "",
            })

            # ── Shot / goal for shot map & momentum ──────────────────
            is_shot  = p.get("shootingPlay", False) or etype in ("shot", "goal")
            is_goal  = p.get("scoringPlay",  False) or etype == "goal"

            if is_shot or is_goal:
                # Team attribution — play field first, then text keyword
                if pteam:
                    shard_team = pteam
                else:
                    # Last-resort: look for team name in text
                    tl = text.lower()
                    shard_team = team1 if team1.lower() in tl else team2

                outcome = "Goal" if is_goal else "Shot Attempt"

                # ESPN doesn't provide real pitch coordinates in play-by-play.
                # We distribute shots along a realistic x range with slight y noise
                # based on minute so the shot map isn't a single blob.
                import random
                rng = random.Random(hash(f"{minute}{text[:20]}"))
                if is_goal:
                    x = rng.uniform(108, 118)
                    y = rng.uniform(28, 52)
                else:
                    x = rng.uniform(78, 116)
                    y = rng.uniform(15, 65)

                shots.append({
                    "minute":  minute,
                    "team":    shard_team,
                    "player":  "",   # no reliable player field in ESPN play-by-play
                    "x":       round(x, 1),
                    "y":       round(y, 1),
                    "outcome": outcome,
                    "xg":      0.35 if is_goal else 0.08,
                })

                # Momentum bucket
                bucket = (minute // 5) * 5
                buckets.setdefault(bucket, {team1: 0, team2: 0})
                key = team1 if shard_team == team1 else team2
                buckets[bucket][key] = buckets[bucket].get(key, 0) + 1

        timeline.sort(key=lambda e: (e["period"], e["minute"]))

        momentum = [
            {"minute": m, team1: buckets[m].get(team1, 0), team2: buckets[m].get(team2, 0)}
            for m in sorted(buckets)
        ]

        return timeline, shots, momentum

    @staticmethod
    def _team_stats(raw, team1, team2):
        stats = {}
        for t in raw.get("boxscore", {}).get("teams", []):
            name = t.get("team", {}).get("displayName")
            if name:
                stats[name] = t.get("statistics", [])
        return stats

    # ------------------------------------------------------------------
    # Step 4 (optional): StatsBomb real shot coordinates
    # ------------------------------------------------------------------

    def _statsbomb_shots(self, team1, team2):
        if not STATSBOMB_AVAILABLE:
            return []
        try:
            matches = statsbomb.matches(
                competition_id=SB_COMPETITION_ID,
                season_id=SB_SEASON_ID,
            )
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
            print(f"  [!] StatsBomb error: {exc}")
            return []

    # ------------------------------------------------------------------
    # Step 5: aggregate helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _aggregate_for_team(team_name, matchups):
        """
        Merge all per-matchup data into a single 'All Matches' aggregate
        for the given team.  Stats are summed where numeric, concatenated
        otherwise.
        """
        agg_shots    = []
        agg_timeline = []
        agg_momentum = []
        agg_stats    = {}   # stat_name → {label, team_total, opp_total}

        for matchup_title, mdata in matchups.items():
            opp = mdata["team2"] if mdata["team1"] == team_name else mdata["team1"]

            # Tag shots & timeline with the opponent for filter use
            for s in mdata["shots"]:
                agg_shots.append({**s, "_matchup": matchup_title})
            for e in mdata["timeline"]:
                agg_timeline.append({**e, "_matchup": matchup_title})

            # Re-bucket momentum (just concatenate — frontend sums per minute)
            for b in mdata["momentum"]:
                agg_momentum.append({**b, "_matchup": matchup_title})

            # Sum team stats
            team_stats_list = mdata["team_stats"].get(team_name, [])
            opp_stats_list  = mdata["team_stats"].get(opp, [])
            for stat in team_stats_list:
                sname = stat.get("name", "")
                try:
                    val = float(stat.get("value", stat.get("displayValue", 0)) or 0)
                except (TypeError, ValueError):
                    val = 0.0
                agg_stats.setdefault(sname, {
                    "label":       stat.get("label") or stat.get("name", sname),
                    "team_total":  0.0,
                    "opp_total":   0.0,
                })
                agg_stats[sname]["team_total"] += val
            for stat in opp_stats_list:
                sname = stat.get("name", "")
                try:
                    val = float(stat.get("value", stat.get("displayValue", 0)) or 0)
                except (TypeError, ValueError):
                    val = 0.0
                agg_stats.setdefault(sname, {
                    "label":      stat.get("label") or stat.get("name", sname),
                    "team_total": 0.0,
                    "opp_total":  0.0,
                })
                agg_stats[sname]["opp_total"] += val

        # Convert aggregated stats back to the same list format the frontend expects
        team_stats_merged = {
            team_name: [
                {
                    "name":         sname,
                    "label":        v["label"],
                    "displayValue": str(round(v["team_total"], 1)),
                }
                for sname, v in agg_stats.items()
            ],
            "Opponents": [
                {
                    "name":         sname,
                    "label":        v["label"],
                    "displayValue": str(round(v["opp_total"], 1)),
                }
                for sname, v in agg_stats.items()
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
    # Step 6: compile everything
    # ------------------------------------------------------------------

    def compile(self, game_ids):
        """
        Returns registry: { teamName: { "All Matches": …, "Team A vs Team B": … } }
        """
        registry = {}

        for gid in game_ids:
            print(f"[+] Processing {gid}…")
            raw = self.get_match_detail(gid)
            if not raw:
                continue

            team1, team2  = self._team_names(raw)
            matchup_title = f"{team1} vs {team2}"
            print(f"    → {matchup_title}")

            # Try StatsBomb first (real coordinates); fall back to ESPN heuristic
            shots = self._statsbomb_shots(team1, team2)
            if not shots:
                print(f"    [!] No StatsBomb data — using ESPN play-by-play.")
                plays = raw.get("plays", [])
                timeline, shots, momentum = self._parse_plays(plays, team1, team2)
            else:
                plays    = raw.get("plays", [])
                timeline, _discard_shots, momentum = self._parse_plays(plays, team1, team2)

            team_stats = self._team_stats(raw, team1, team2)
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

        # Build per-team "All Matches" aggregates
        for team_name, matchups in registry.items():
            if len(matchups) >= 1:
                matchups["All Matches"] = self._aggregate_for_team(team_name, matchups)

        print(f"[+] Compiled data for {len(registry)} team(s).")
        return registry

    # ------------------------------------------------------------------
    # Step 7: inject into index.html
    # ------------------------------------------------------------------

    @staticmethod
    def export_html(data_registry):
        if not os.path.exists("index.html"):
            print("[-] index.html template not found.")
            return

        with open("index.html", "r", encoding="utf-8") as f:
            template = f.read()

        marker       = "/* {{DATA_PAYLOAD_MARKER}} */"
        json_payload = json.dumps(data_registry, ensure_ascii=True, indent=2)
        injection    = f"const MATCH_DATA = {json_payload};"
        final        = template.replace(marker, injection, 1)

        with open("index.html", "w", encoding="utf-8") as f:
            f.write(final)

        print(f"[+] index.html updated ({len(data_registry)} team(s)).")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    compiler = WorldCupDataCompiler()
    print("[*] World Cup Dashboard Compiler v3")

    game_ids = compiler.discover_all_game_ids()
    compiled = compiler.compile(game_ids)

    if compiled:
        WorldCupDataCompiler.export_html(compiled)
    else:
        print("[-] No data compiled.")
        sys.exit(1)
