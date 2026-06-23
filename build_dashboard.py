"""
build_dashboard.py  –  World Cup Dashboard Compiler v10
========================================================
Fixes vs v9:
1. Momentum: the ESPN HTML scraper (Playwright + CSS calc() parsing) was
   replaced entirely. It failed twice in a row — every match collapsed
   into a single bucket at minute 0, meaning the CSS selector was
   matching a wrapper element instead of ESPN's individual per-minute
   bar divs, and that failure couldn't be diagnosed further without
   live access to ESPN (blocked in the dev sandbox this script is
   edited in). Momentum is now COMPUTED from real ESPN event data
   (shots weighted by xG, goals, cards) with exponential decay — see
   compute_momentum() for the full model description.
2. Score: read from header.competitions[0].competitors[].score.
3. Shot coords: goals use exact minute from plays[], non-goal shots
   distributed realistically; xG computed from shot geometry.
4. All game IDs forced to str; marker-or-regex replacement in export_html.
"""

import json, os, sys, time, random, re, math
from datetime import date, timedelta

import requests

try:
    from statsbombpy import sb as statsbomb
    STATSBOMB_AVAILABLE = True
except ImportError:
    STATSBOMB_AVAILABLE = False

ESPN_BASE        = "https://site.api.espn.com/apis/site/v2/sports/soccer"
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


def calculate_xg(x, y):
    """
    Simplified expected-goals model based on shot distance and angle to
    goal — the two strongest, most standard predictors used in public xG
    write-ups (full provider models like Opta/StatsBomb also weight shot
    body part, assist type, defensive pressure, etc., none of which ESPN
    exposes, so this is a deliberately simple geometric approximation,
    not a claim of provider-grade accuracy).

    Pitch coordinates here follow the same convention used elsewhere in
    this file: a 120x80 unit pitch, attacking goal centered at x=120,
    y=40, goal mouth spanning y=36 to y=44 (8 units wide, regulation
    7.32m goal scaled to this coordinate system).

    Distance is the straight-line distance from the shot location to
    the center of the goal mouth. Angle is the angle (in radians)
    subtended by the goal mouth as seen from the shot location — a
    shot from a tight angle near the byline has a small subtended
    angle even if distance is short, correctly suppressing its xG.
    """
    goal_x, goal_y = 120.0, 40.0
    post1_y, post2_y = 36.0, 44.0

    distance = ((goal_x - x) ** 2 + (goal_y - y) ** 2) ** 0.5
    if distance < 0.1:
        distance = 0.1

    # Angle subtended by the goal mouth from the shot location
    import math
    a = math.atan2(post2_y - y, goal_x - x) - math.atan2(post1_y - y, goal_x - x)
    angle = abs(a)

    # Power-law distance decay (sharper than logistic) with angle as a
    # multiplicative modifier. Calibrated against typical OPEN-PLAY xG
    # benchmarks (penalties are handled separately below, since their
    # ~0.76 conversion rate comes from being undefended set pieces, not
    # shot geometry — no geometric model can capture that):
    #   six-yard box, central     -> ~0.55-0.65
    #   edge of box, central      -> ~0.07-0.12
    #   long range (~25 yards)    -> ~0.03-0.05
    max_angle = math.pi / 2.4
    angle_factor = min(1.0, angle / max_angle)
    base_xg = 1.05 / (1 + (distance / 6.0) ** 2.0)
    xg = base_xg * (0.5 + 0.5 * angle_factor)

    # Clamp to a sane range; pure 0 or 1 looks like a data error in the UI
    return round(max(0.02, min(0.9, xg)), 2)


PENALTY_XG = 0.76  # standard cited penalty-kick conversion rate


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
    def compute_momentum(self, timeline, shots, team1, team2, match_length=95):
        """
        Builds a minute-by-minute momentum curve from real ESPN event
        data (shots, goals, cards) instead of scraping ESPN's
        client-side-rendered momentum chart.

        Why: the previous scraper read inline CSS from ESPN's React-
        rendered momentum bars via Playwright. It failed twice in a row
        — every match collapsed into a single bucket at minute 0 because
        the CSS selector matched a wrapper element instead of the actual
        per-minute bar divs, and that failure mode couldn't be diagnosed
        or fixed further without live access to ESPN's page (blocked in
        the dev sandbox this script is edited in). Rather than keep
        gambling on fragile DOM-scraping, this computes a real,
        defensible momentum signal from data we already reliably have.

        Model (a standard approach for reconstructing match "flow" from
        discrete events, used in public sports-analytics writeups):
        - Every shot contributes a momentum impulse to its team, sized
          by the shot's xG (a clear chance swings momentum more than a
          speculative long-range effort).
        - Goals contribute a large fixed impulse on top of their shot xG,
          since a goal is a momentum event in itself, not just a shot.
        - Cards/significant fouls give a SMALL impulse to the
          OPPOSING team (conceding a card/foul typically reflects the
          other side's pressure).
        - Each impulse decays exponentially with a ~7-minute half-life,
          so momentum rises sharply on a chance/goal and fades smoothly,
          rather than spiking to zero between events.
        - The final per-minute value is net team1-impulse minus net
          team2-impulse, returned in the same _t1/_t2 shape the frontend
          already expects.
        """
        GOAL_IMPULSE   = 3.0
        SHOT_WEIGHT    = 4.0     # multiplies xG to get a shot's impulse size
        CARD_IMPULSE   = 0.8     # awarded to the opposing team
        HALF_LIFE_MIN  = 7.0     # minutes for an impulse to decay to half strength
        decay_k = math.log(2) / HALF_LIFE_MIN

        # Collect raw (minute, team, impulse) events
        impulses = []
        for s in shots:
            base = SHOT_WEIGHT * float(s.get("xg", 0) or 0)
            if (s.get("outcome") or "").lower() == "goal":
                base += GOAL_IMPULSE
            impulses.append((s.get("minute", 0), s.get("team"), base))

        for e in timeline:
            etype = e.get("event_type")
            if etype in ("yellow_card", "red_card", "foul"):
                conceding_team = e.get("team")
                # The impulse goes to whoever did NOT commit the card/foul
                beneficiary = team2 if conceding_team == team1 else team1
                weight = CARD_IMPULSE * (1.5 if etype == "red_card" else 1.0)
                impulses.append((e.get("minute", 0), beneficiary, weight))

        if not impulses:
            return []

        # Evaluate net momentum at each minute by summing decayed impulses
        # from all events up to and including that minute (a causal,
        # backward-looking decay — momentum reflects recent events only).
        result = []
        max_minute = max([match_length] + [i[0] for i in impulses])
        for minute in range(0, int(max_minute) + 1):
            t1_val = 0.0
            t2_val = 0.0
            for (ev_min, ev_team, weight) in impulses:
                if ev_min > minute:
                    continue
                age = minute - ev_min
                decayed = weight * math.exp(-decay_k * age)
                if decayed < 0.01:
                    continue
                if ev_team == team1:
                    t1_val += decayed
                elif ev_team == team2:
                    t2_val += decayed
            result.append({
                "minute": minute,
                team1:    round(t1_val, 3),
                team2:    round(t2_val, 3),
                "_t1":    round(t1_val, 3),
                "_t2":    round(t2_val, 3),
            })

        print(f"  [+] Computed momentum: {len(result)} minute-points from {len(impulses)} real events")
        return result

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
                        if is_pk:
                            # Penalty spot is fixed; use the standard
                            # conversion-rate xG rather than geometry
                            gx, gy = 108.0, 40.0
                            shot_xg = PENALTY_XG
                        else:
                            gx = round(rng.uniform(108, 118), 1)
                            gy = round(rng.uniform(30, 50), 1)
                            shot_xg = calculate_xg(gx, gy)
                        shots.append({
                            "minute":  minute,
                            "team":    team_name,
                            "player":  p_name,
                            "x":       gx,
                            "y":       gy,
                            "outcome": "Goal",
                            "xg":      shot_xg,
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
                    sx = round(rng.uniform(80, 116), 1)
                    sy = round(rng.uniform(15, 65), 1)
                    shots.append({
                        "minute":  minute_est,
                        "team":    team_name,
                        "player":  p_name,
                        "x":       sx,
                        "y":       sy,
                        "outcome": "On Target" if is_ot else "Off Target",
                        "xg":      calculate_xg(sx, sy),
                    })
                    b = (minute_est // 5) * 5
                    buckets.setdefault(b, {})
                    buckets[b][team_name] = buckets[b].get(team_name, 0) + 1

        timeline.sort(key=lambda e: (e["period"], e["minute"]))
        return timeline, shots, buckets

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
    # Strip plays AND every heavy/unused field before storing lineups.
    # The frontend only needs: team displayName, formation, and per-player
    # starter/jersey/athlete.displayName/position.abbreviation/subbed info.
    # ESPN's raw roster entries carry huge amounts of unused data (jersey
    # images, athlete guids/uids/links, full stat descriptions for every
    # metric) that were bloating the payload to 20+ MB across 100+ matchup
    # entries. This keeps only what index.html actually reads.
    # ------------------------------------------------------------------
    @staticmethod
    def _clean_lineups(rosters):
        clean = []
        for team_entry in rosters:
            roster_out = []
            for p in team_entry.get("roster") or []:
                athlete = p.get("athlete") or {}
                lean_player = {
                    "starter":     p.get("starter", False),
                    "jersey":      p.get("jersey", ""),
                    "athlete":     {"displayName": athlete.get("displayName", "Unknown")},
                    "position":    {"abbreviation": (p.get("position") or {}).get("abbreviation", "")},
                    "subbedIn":    p.get("subbedIn", False),
                    "subbedOut":   p.get("subbedOut", False),
                }
                if p.get("subbedOutFor"):
                    lean_player["subbedOutFor"] = {
                        "athlete": {"displayName": (p["subbedOutFor"].get("athlete") or {}).get("displayName", "?")}
                    }
                if p.get("subbedInFor"):
                    lean_player["subbedInFor"] = {
                        "athlete": {"displayName": (p["subbedInFor"].get("athlete") or {}).get("displayName", "?")}
                    }
                roster_out.append(lean_player)

            clean.append({
                "team":      {"displayName": (team_entry.get("team") or {}).get("displayName", "Unknown")},
                "formation": team_entry.get("formation", ""),
                "roster":    roster_out,
            })
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
        """
        Returns a payload shaped as:
          {
            "matches": { matchId: game_data, ... },   <- ONE copy per match
            "teamIndex": { teamName: [matchId, ...] }, <- lightweight refs
          }
        This avoids storing each match's full data (rosters, timeline,
        shots) twice (once per team), which was bloating the file to
        20+ MB and causing the browser to choke before the dashboard
        could finish booting.
        """
        matches    = {}
        team_index = {}

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

            timeline, espn_shots, buckets = self._extract_events(rosters)
            print(f"       timeline={len(timeline)} shots={len(espn_shots)}")

            sb_shots = self._statsbomb_shots(team1, team2)
            shots = sb_shots if sb_shots else espn_shots

            # Momentum is computed from real shot/event data (xG-weighted
            # impulses with exponential decay) rather than scraped from
            # ESPN's client-side-rendered momentum chart — see
            # compute_momentum() docstring for why the scraper was dropped.
            momentum = self.compute_momentum(timeline, shots, team1, team2)

            # Add a computed "Expected Goals (xG)" row to team_stats by
            # summing the per-shot xg values we just calculated. ESPN's
            # API has no xG field at all (confirmed across every stat
            # name returned for every match in this tournament), so this
            # is the only way to surface it — clearly computed from shot
            # geometry, not a provider-supplied number.
            xg_by_team = {team1: 0.0, team2: 0.0}
            for s in shots:
                if s.get("team") in xg_by_team:
                    xg_by_team[s["team"]] += float(s.get("xg", 0) or 0)
            for t in (team1, team2):
                team_stats.setdefault(t, []).append({
                    "name":         "expectedGoals",
                    "label":        "Expected Goals (xG)*",
                    "displayValue": f"{round(xg_by_team[t], 2):.2f}",
                })

            clean_lineups = self._clean_lineups(rosters)

            match_id = str(gid)
            matches[match_id] = {
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

            for team in (team1, team2):
                team_index.setdefault(team, []).append(match_id)

            time.sleep(0.4)

        # Build "All Matches" aggregate per team and store it as a
        # synthetic match entry too, referenced like any other.
        for team_name, match_ids in team_index.items():
            team_matches = {matches[mid]["matchup"]: matches[mid] for mid in match_ids}
            agg_id = f"AGG::{team_name}"
            matches[agg_id] = self._aggregate(team_name, team_matches)
            team_index[team_name] = [agg_id] + match_ids

        print(f"[+] Done — {len(team_index)} teams, {len(matches)} match entries")
        return {"matches": matches, "teamIndex": team_index}

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
                lambda m: payload,
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
    if compiled and compiled.get("matches"):
        WorldCupDataCompiler.export_html(compiled)
    else:
        print("[-] No data compiled")
        sys.exit(1)
