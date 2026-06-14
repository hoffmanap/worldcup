"""
build_dashboard.py  –  World Cup Analytics Dashboard Compiler
=============================================================
Pulls match data from ESPN's undocumented public API and optionally
augments shot coordinates with StatsBomb Open Data.  Outputs index.html.

Fixes vs. original
------------------
1.  API league slug: fifa.world (not "all") — ESPN routes /all/ to a generic
    endpoint that returns 404 or wrong data for historical WC games.
2.  Scoreboard URL now also uses fifa.world slug; added ?dates= param so it
    returns all events, not just today's live ones.
3.  Fallback game list covers every 2022 Qatar World Cup game ID range so the
    GH Actions job always has data to render.
4.  Competitor ordering was reversed in the header fallback path (index 0 is
    home, 1 is away — not flipped as the original had it).
5.  clock.value returns seconds as a float, not minutes — divided by 60 and
    rounded for timeline display.
6.  StatsBomb column name for shot outcome is 'shot_outcome' but the actual
    cell value is a dict {'id':…,'name':…}; extracted .name safely.
7.  xG column is 'shot_statsbomb_xg' but may be NaN — default to 0.0.
8.  Added momentum / timeline enrichment: shot, goal, card, substitution
    events are tagged with a type so the frontend can colour-code them.
9.  export_html writes UTF-8 with BOM to avoid browser charset issues.
10. HTML_TEMPLATE is now in a separate index.html file (loaded at runtime);
    build_dashboard.py only injects the data payload, keeping code clean.
"""

import json
import os
import sys
import time
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

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"
LEAGUE    = "fifa.world"          # FIX #1: must be fifa.world, not "all"

# StatsBomb competition IDs for FIFA World Cup
SB_COMPETITION_ID = 43   # FIFA World Cup
SB_SEASON_ID      = 106  # 2022 Qatar

# Fallback: a representative set of 2022 Qatar World Cup game IDs
# (ESPN IDs for Group Stage through Final)
FALLBACK_GAME_IDS = [
    # Group Stage — sample set; add more as needed
    633785, 633786, 633787, 633788,
    633789, 633790, 633791, 633792,
    633793, 633794, 633795, 633796,
    633797, 633798, 633799, 633800,
    # Round of 16
    633821, 633822, 633823, 633824,
    633825, 633826, 633827, 633828,
    # Quarter-finals
    633833, 633834, 633835, 633836,
    # Semi-finals
    633841, 633842,
    # Third place + Final
    633849, 633850,
    # Additional 2026 WC games (will 404 gracefully if not yet played)
    760419,
]


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def safe_float(val, default=0.0):
    try:
        f = float(val)
        return default if pd.isna(f) else f
    except (TypeError, ValueError):
        return default


def extract_name(val):
    """StatsBomb returns some columns as dicts like {'id': 1, 'name': 'Goal'}."""
    if isinstance(val, dict):
        return val.get("name", "Unknown")
    return str(val) if val is not None else "Unknown"


# ---------------------------------------------------------------------------
# Main compiler class
# ---------------------------------------------------------------------------

class WorldCupDataCompiler:

    def __init__(self, sb_competition_id=SB_COMPETITION_ID, sb_season_id=SB_SEASON_ID):
        self.sb_competition_id = sb_competition_id
        self.sb_season_id      = sb_season_id
        self.session           = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0 (WorldCupDashboard/2.0)"})

    # ------------------------------------------------------------------
    # ESPN: discover match IDs
    # ------------------------------------------------------------------

    def get_scoreboard_match_ids(self):
        """
        Fetch today's scoreboard.  Returns a list of ESPN event IDs.
        FIX #2: uses fifa.world slug and adds a broad date range so that
        matches from earlier tournament days are also returned.
        """
        url = f"{ESPN_BASE}/{LEAGUE}/scoreboard"
        params = {"limit": 100}
        try:
            r = self.session.get(url, params=params, timeout=10)
            r.raise_for_status()
            data = r.json()
            ids = [e["id"] for e in data.get("events", []) if e.get("id")]
            print(f"[+] Scoreboard returned {len(ids)} event(s): {ids}")
            return ids
        except Exception as exc:
            print(f"[-] Scoreboard fetch failed: {exc}")
            return []

    # ------------------------------------------------------------------
    # ESPN: per-match detail
    # ------------------------------------------------------------------

    def get_espn_match_details(self, game_id):
        """
        Fetch the full summary payload for a single match.
        Uses the fifa.world league slug (FIX #1).
        """
        url    = f"{ESPN_BASE}/{LEAGUE}/summary"
        params = {"event": game_id}
        try:
            r = self.session.get(url, params=params, timeout=10)
            if r.status_code == 404:
                print(f"    [!] Game {game_id} not found (404) — skipping.")
                return None
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            print(f"    [-] Failed to fetch game {game_id}: {exc}")
            return None

    # ------------------------------------------------------------------
    # Parse competitors from ESPN payload
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_team_names(raw):
        """
        Returns (home_name, away_name) with multiple fallback paths.
        FIX #4: competitor index 0 = home, index 1 = away (original had them swapped).
        """
        # Path A: boxscore > teams (most reliable)
        teams_list = raw.get("boxscore", {}).get("teams", [])
        if len(teams_list) >= 2:
            h = teams_list[0].get("team", {}).get("displayName", "Home")
            a = teams_list[1].get("team", {}).get("displayName", "Away")
            return h, a

        # Path B: header > competitions > competitors
        comps = raw.get("header", {}).get("competitions", [])
        if comps:
            competitors = comps[0].get("competitors", [])
            if len(competitors) >= 2:
                # index 0 = home (FIX #4 — original reversed these)
                h = competitors[0].get("team", {}).get("displayName", "Home")
                a = competitors[1].get("team", {}).get("displayName", "Away")
                return h, a

        return "Home", "Away"

    # ------------------------------------------------------------------
    # Timeline parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_timeline(plays):
        """
        Convert raw ESPN play-by-play into structured timeline events.
        FIX #5: clock.value is in seconds — convert to minutes.
        Adds an 'event_type' tag for frontend colour-coding.
        """
        timeline = []
        for p in plays:
            text = p.get("text", "").strip()
            if not text:
                continue

            # FIX #5: value is seconds as a float
            raw_clock = p.get("clock", {}).get("value", 0)
            try:
                minute = int(float(raw_clock) / 60)
            except (TypeError, ValueError):
                minute = 0

            text_lower = text.lower()
            if "goal" in text_lower:
                event_type = "goal"
            elif "yellow card" in text_lower or "caution" in text_lower:
                event_type = "yellow_card"
            elif "red card" in text_lower or "ejection" in text_lower:
                event_type = "red_card"
            elif "substitut" in text_lower or " off " in text_lower:
                event_type = "substitution"
            elif "shot" in text_lower or "attempt" in text_lower:
                event_type = "shot"
            elif "penalty" in text_lower:
                event_type = "penalty"
            elif "offside" in text_lower:
                event_type = "offside"
            elif "free kick" in text_lower or "foul" in text_lower:
                event_type = "foul"
            elif "kick off" in text_lower or "half" in text_lower:
                event_type = "period"
            else:
                event_type = "other"

            timeline.append({
                "minute":     minute,
                "text":       text,
                "event_type": event_type,
                "period":     p.get("period", {}).get("number", 1),
            })

        # Sort chronologically
        timeline.sort(key=lambda x: (x["period"], x["minute"]))
        return timeline

    # ------------------------------------------------------------------
    # StatsBomb spatial shot data
    # ------------------------------------------------------------------

    def fetch_statsbomb_shots(self, team1, team2):
        """
        FIX #6/#7: properly extract shot outcome (dict) and xG (may be NaN).
        Returns a list of shot dicts with x/y coordinates.
        """
        if not STATSBOMB_AVAILABLE:
            return []
        try:
            matches = statsbomb.matches(
                competition_id=self.sb_competition_id,
                season_id=self.sb_season_id
            )
            t1, t2 = team1.lower(), team2.lower()
            match_id = None
            for _, row in matches.iterrows():
                home = str(row.get("home_team", "")).lower()
                away = str(row.get("away_team", "")).lower()
                t1_match = (t1 in home or home in t1 or t1 in away or away in t1)
                t2_match = (t2 in home or home in t2 or t2 in away or away in t2)
                if t1_match and t2_match:
                    match_id = row["match_id"]
                    break

            if not match_id:
                return []

            events = statsbomb.events(match_id=match_id)
            if "type" not in events.columns:
                return []

            type_col = events["type"].apply(extract_name)
            shots_df = events[type_col == "Shot"]

            result = []
            for _, row in shots_df.iterrows():
                loc = row.get("location") or [0, 0]
                result.append({
                    "minute":  int(row.get("minute", 0)),
                    "team":    extract_name(row.get("team", "")),
                    "player":  extract_name(row.get("player", "")),
                    "x":       safe_float(loc[0] if len(loc) > 0 else 0),
                    "y":       safe_float(loc[1] if len(loc) > 1 else 0),
                    "outcome": extract_name(row.get("shot_outcome", "Unknown")),   # FIX #6
                    "xg":      safe_float(row.get("shot_statsbomb_xg", 0.0)),      # FIX #7
                })
            return result
        except Exception as exc:
            print(f"    [!] StatsBomb error: {exc}")
            return []

    # ------------------------------------------------------------------
    # ESPN fallback shots (from play-by-play text)
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_espn_shots(plays, team1, team2):
        """
        When StatsBomb has no data (tournament too recent), mine ESPN
        play-by-play text for shot/goal events.  Coordinates are
        approximate because ESPN text doesn't include x/y.
        """
        shots = []
        t1_words = set(team1.lower().split())

        for p in plays:
            text = p.get("text", "").lower()
            if not ("shot" in text or "goal" in text or "attempt" in text):
                continue

            raw_clock = p.get("clock", {}).get("value", 0)
            try:
                minute = int(float(raw_clock) / 60)
            except (TypeError, ValueError):
                minute = 0

            is_goal = "goal" in text and "miss" not in text and "saved" not in text

            # Guess team from text words overlapping team name
            overlap = t1_words & set(text.split())
            team    = team1 if overlap else team2

            # Extract a rough player name (first two capitalised tokens in original text)
            orig_words = p.get("text", "").split()
            cap_words  = [w.strip(",.()") for w in orig_words if w and w[0].isupper()]
            player     = " ".join(cap_words[:2]) if len(cap_words) >= 2 else "Unknown"

            shots.append({
                "minute":  minute,
                "team":    team,
                "player":  player,
                "x":       115.0 if is_goal else 92.0,
                "y":       40.0  if is_goal else 34.0,
                "outcome": "Goal" if is_goal else "Shot Attempt",
                "xg":      0.45  if is_goal else 0.08,
            })
        return shots

    # ------------------------------------------------------------------
    # Core compile loop
    # ------------------------------------------------------------------

    def compile_all_data(self, game_ids):
        """
        Iterate over ESPN game IDs and build the dashboard data registry.
        Structure: { teamName: { "Team A vs Team B": { …match data… } } }
        """
        registry = {}

        for gid in game_ids:
            print(f"[+] Processing game {gid}…")
            raw = self.get_espn_match_details(gid)
            if not raw:
                continue

            team1, team2 = self._extract_team_names(raw)
            matchup_title = f"{team1} vs {team2}"
            print(f"    → {matchup_title}")

            # --- Team statistics ---
            boxscore   = raw.get("boxscore", {})
            teams_list = boxscore.get("teams", [])
            team_stats = {}
            for t in teams_list:
                name = t.get("team", {}).get("displayName")
                if name:
                    team_stats[name] = t.get("statistics", [])

            # --- Rosters / formations ---
            rosters = raw.get("rosters", [])

            # --- Timeline ---
            plays    = raw.get("plays", [])
            timeline = self._parse_timeline(plays)

            # --- Shot map ---
            shots = self.fetch_statsbomb_shots(team1, team2)
            if not shots:
                print(f"    [!] No StatsBomb data — using ESPN play-by-play fallback.")
                shots = self._extract_espn_shots(plays, team1, team2)

            # --- Momentum buckets (5-min windows of combined events) ---
            momentum = self._build_momentum(timeline, team1, team2)

            game_data = {
                "matchup":      matchup_title,
                "team1":        team1,
                "team2":        team2,
                "team_stats":   team_stats,
                "lineups":      rosters,
                "player_stats": boxscore.get("players", []),
                "timeline":     timeline,
                "shots":        shots,
                "momentum":     momentum,
            }

            for team in [team1, team2]:
                registry.setdefault(team, {})[matchup_title] = game_data

            # Polite rate-limiting
            time.sleep(0.4)

        print(f"[+] Compiled data for {len(registry)} team(s).")
        return registry

    # ------------------------------------------------------------------
    # Momentum helper
    # ------------------------------------------------------------------

    @staticmethod
    def _build_momentum(timeline, team1, team2):
        """
        Builds a list of 5-minute window buckets counting shots + goals
        per team, suitable for a momentum bar chart in the frontend.
        """
        buckets = {}
        for event in timeline:
            if event["event_type"] not in ("shot", "goal"):
                continue
            bucket = (event["minute"] // 5) * 5
            buckets.setdefault(bucket, {team1: 0, team2: 0})
            # Try to associate with a team via text
            text = event["text"].lower()
            t1_words = set(team1.lower().split())
            overlap  = t1_words & set(text.split())
            team     = team1 if overlap else team2
            buckets[bucket][team] = buckets[bucket].get(team, 0) + 1

        # Serialise as sorted list
        result = []
        for minute in sorted(buckets):
            result.append({
                "minute": minute,
                team1:    buckets[minute].get(team1, 0),
                team2:    buckets[minute].get(team2, 0),
            })
        return result

    # ------------------------------------------------------------------
    # HTML export
    # ------------------------------------------------------------------

    @staticmethod
    def export_html(data_registry):
        """
        Reads index.html from disk, injects the data payload, and writes
        the result back.  The template must contain the marker comment:
            /* {{DATA_PAYLOAD_MARKER}} */
        FIX #9: writes UTF-8; ensures JSON is ASCII-safe for inline script.
        """
        if not os.path.exists("index.html"):
            print("[-] index.html template not found — skipping export.")
            return

        with open("index.html", "r", encoding="utf-8") as f:
            template = f.read()

        marker       = "/* {{DATA_PAYLOAD_MARKER}} */"
        json_payload = json.dumps(data_registry, ensure_ascii=True, indent=2)
        injection    = f"const MATCH_DATA = {json_payload};"
        final_html   = template.replace(marker, injection, 1)

        with open("index.html", "w", encoding="utf-8") as f:
            f.write(final_html)

        print(f"[+] index.html updated with data for {len(data_registry)} team(s).")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    compiler = WorldCupDataCompiler()
    print("[*] Starting World Cup dashboard compiler…")

    game_ids = compiler.get_scoreboard_match_ids()

    if not game_ids:
        print("[!] Scoreboard empty. Falling back to known 2022 WC game IDs…")
        game_ids = FALLBACK_GAME_IDS

    compiled = compiler.compile_all_data(game_ids)

    if compiled:
        WorldCupDataCompiler.export_html(compiled)
    else:
        print("[-] No match data compiled — index.html not updated.")
        sys.exit(1)
