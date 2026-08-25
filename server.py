#!/usr/bin/env python3
"""
Fantasy Football Terminal — FastAPI Backend

A CRT-styled live draft assistant for ESPN fantasy football leagues.
Configure your league in the Config block below before first run.

Strategies sourced from:
- FantasyPros (Steve Krebs, Mike Fanelli, Evan Tarracciano, Andrew Erickson)
- The Ringer (Danny Heifetz) — "10 Rules to Win Your Fantasy Football League"
- DraftSharks (Jared Smola, Shane Hallam) — "Elite QB or Late-Round QB?"
- ETR / Establish the Run — "How to Beat 10-Team Leagues"
- Shawn Siegele (RotoViz) — Zero-RB
- JJ Zachariason — "The Late-Round Quarterback"
- League History: your own league's champion draft patterns (see LEAGUE_HISTORY)
"""

import os
import time
import random
import threading
from collections import defaultdict
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from espn_api.football import League

# ── Config ─────────────────────────────────────────────────────────────────
# Fill these in before your first run. See README "League Setup".
#
# Finding your cookies: log into espn.com in Chrome, press F12 →
# Application → Cookies → https://www.espn.com, then copy the values of
# espn_s2 and SWID. Keep them private — they authenticate your account.
#
# WARNING: never commit real cookie values to a public repository.
LEAGUE_ID    = 0      # ← your ESPN league ID (the number in the league URL)
YEAR         = 2026   # ← season year
ESPN_S2      = ''     # ← paste your espn_s2 cookie value
SWID         = ''     # ← paste your SWID cookie value (keep the braces)
MY_TEAM_ID   = 1      # ← your team's ID within the league
TOTAL_TEAMS  = 10     # ← teams in your league
TOTAL_ROUNDS = 15     # ← rounds in your draft
POLL_SECONDS = 5
BOARD_REFRESH_SECONDS = 600   # re-pull player projections/injuries every 10 min

STARTER_NEEDS = {'QB': 1, 'RB': 2, 'WR': 2, 'TE': 1, 'FLEX': 2, 'K': 1}
DEMO_CHAOS_RATE = 0.10   # odds a demo opponent makes a deliberately bad pick
FLEX_ELIGIBLE  = {'RB', 'WR', 'TE'}

# ── Bye Weeks 2026 — Confirmed from ESPN Schedule Grid ─────────────────────
# Source: espn.com/nfl/schedulegrid, 2026 season
# KEY FINDING: No team has a bye in weeks 15-17 — fantasy playoffs are bye-free.
# Heaviest bye week: Week 11 (ATL, CLE, GB, LAR, NE, SEA — 6 teams)
FANTASY_PLAYOFF_WEEKS = {15, 16, 17}   # all bye-free — no playoff bye concern
FANTASY_REG_SEASON_END = 14

BYE_WEEKS = {
    'ARI': 14, 'ATL': 11, 'BAL': 13, 'BUF': 7,
    'CAR': 5,  'CHI': 10, 'CIN': 6,  'CLE': 11,
    'DAL': 14, 'DEN': 10, 'DET': 6,  'GB':  11,
    'HOU': 8,  'IND': 13, 'JAX': 7,  'KC':  5,
    'LAC': 7,  'LAR': 11, 'LV':  13, 'MIA': 6,
    'MIN': 6,  'NE':  11, 'NO':  8,  'NYG': 8,
    'NYJ': 13, 'PHI': 10, 'PIT': 9,  'SEA': 11,
    'SF':  8,  'TB':  10, 'TEN': 9,  'WSH': 7,
}

# ── Strength of Schedule — Defense rankings vs fantasy positions (2026) ──────
# Scale 1-10: 10 = elite defense (hardest matchup), 1 = worst (easiest matchup)
# Source: 2025 finish + offseason moves. Updated as season info available.
DEFENSE_VS_POS = {
    # Format: 'TEAM': {'QB': X, 'RB': X, 'WR': X, 'TE': X}
    'SF':  {'QB': 9, 'RB': 9, 'WR': 8, 'TE': 7},
    'BAL': {'QB': 8, 'RB': 9, 'WR': 7, 'TE': 8},
    'BUF': {'QB': 7, 'RB': 8, 'WR': 9, 'TE': 8},
    'PHI': {'QB': 8, 'RB': 7, 'WR': 9, 'TE': 7},
    'MIN': {'QB': 7, 'RB': 8, 'WR': 8, 'TE': 6},
    'PIT': {'QB': 7, 'RB': 7, 'WR': 8, 'TE': 7},
    'CLE': {'QB': 8, 'RB': 6, 'WR': 7, 'TE': 6},
    'LAC': {'QB': 6, 'RB': 7, 'WR': 8, 'TE': 6},
    'DEN': {'QB': 7, 'RB': 7, 'WR': 7, 'TE': 6},
    'SEA': {'QB': 6, 'RB': 6, 'WR': 7, 'TE': 5},
    'GB':  {'QB': 6, 'RB': 6, 'WR': 6, 'TE': 5},
    'NE':  {'QB': 6, 'RB': 5, 'WR': 6, 'TE': 5},
    'KC':  {'QB': 5, 'RB': 5, 'WR': 6, 'TE': 5},
    'DET': {'QB': 5, 'RB': 6, 'WR': 5, 'TE': 5},
    'DAL': {'QB': 5, 'RB': 5, 'WR': 6, 'TE': 5},
    'ATL': {'QB': 5, 'RB': 5, 'WR': 5, 'TE': 5},
    'HOU': {'QB': 5, 'RB': 5, 'WR': 5, 'TE': 5},
    'IND': {'QB': 5, 'RB': 5, 'WR': 5, 'TE': 4},
    'WSH': {'QB': 5, 'RB': 5, 'WR': 5, 'TE': 4},
    'CIN': {'QB': 5, 'RB': 5, 'WR': 5, 'TE': 5},
    'LV':  {'QB': 5, 'RB': 5, 'WR': 5, 'TE': 4},
    'TEN': {'QB': 4, 'RB': 5, 'WR': 5, 'TE': 4},
    'TB':  {'QB': 4, 'RB': 4, 'WR': 5, 'TE': 4},
    'CAR': {'QB': 4, 'RB': 4, 'WR': 4, 'TE': 4},
    'NYJ': {'QB': 4, 'RB': 4, 'WR': 5, 'TE': 4},
    'NYG': {'QB': 4, 'RB': 4, 'WR': 4, 'TE': 4},
    'NO':  {'QB': 4, 'RB': 4, 'WR': 4, 'TE': 4},
    'JAX': {'QB': 4, 'RB': 4, 'WR': 4, 'TE': 4},
    'LAR': {'QB': 4, 'RB': 4, 'WR': 4, 'TE': 4},
    'MIA': {'QB': 3, 'RB': 4, 'WR': 4, 'TE': 3},
    'CHI': {'QB': 3, 'RB': 3, 'WR': 4, 'TE': 3},
    'ARI': {'QB': 3, 'RB': 3, 'WR': 4, 'TE': 3},
}

# Playoff-week SOS: how hard is each team's schedule in weeks 14-16?
# Precomputed based on typical opponent strength. Updated when schedule available.
PLAYOFF_SOS = {
    # 'TEAM': avg defense difficulty faced in weeks 14-16 (higher = harder)
    'SF':6.5,'BAL':6.0,'BUF':5.5,'PHI':6.0,'MIN':5.0,'PIT':5.5,
    'CLE':5.0,'LAC':5.0,'DEN':5.5,'SEA':4.5,'GB':5.0,'NE':5.5,
    'KC':5.0,'DET':4.5,'DAL':5.0,'ATL':4.5,'HOU':4.5,'IND':4.0,
    'WSH':5.0,'CIN':4.5,'LV':4.0,'TEN':4.0,'TB':4.0,'CAR':4.0,
    'NYJ':4.5,'NYG':4.0,'NO':4.0,'JAX':4.0,'LAR':4.5,'MIA':4.0,
    'CHI':3.5,'ARI':3.5,
}

# ── Conflict Analysis Engine ────────────────────────────────────────────────
SEVERITY = {'critical': 3, 'warning': 2, 'info': 1}

def analyze_conflicts(candidate, my_roster, current_round):
    """
    Given a candidate player and current roster, return list of conflict dicts.
    Each: {severity, title, detail, type}
    Severities: 'critical' (red), 'warning' (amber), 'info' (teal)
    """
    conflicts = []
    pos = candidate.get('position', '')
    team = candidate.get('proTeam', '')
    name = candidate.get('name', '')

    # ── 1. BYE WEEK CONTEXT ───────────────────────────────────────────────
    bye = BYE_WEEKS.get(team)
    if bye:
        # No team has a playoff bye in 2026 — all clear weeks 15-17
        # But week 14 (last reg season game) and week 11 (6 teams off) are notable
        if bye == FANTASY_REG_SEASON_END:
            conflicts.append({
                'severity': 'warning',
                'type': 'late_bye',
                'title': f'WEEK 14 BYE — Final Regular Season Week',
                'detail': (
                    f'{name} ({team}) has their bye in Week 14 — the last week before '
                    f'fantasy playoffs. Missing a starter that week could cost you a '
                    f'playoff seed or home-field. Make sure you have depth at {pos} '
                    f'for Week 14. Good news: no team has a playoff bye in 2026.'
                ),
            })
        elif bye == 11:
            conflicts.append({
                'severity': 'info',
                'type': 'heavy_bye_week',
                'title': f'WEEK 11 BYE — Heaviest Bye Week (6 teams off)',
                'detail': (
                    f'{name} ({team}) has a Week 11 bye. Week 11 is the heaviest '
                    f'bye week in 2026 — ATL, CLE, GB, LAR, NE, SEA are all off. '
                    f'Waiver wire will be very thin that week. Plan ahead with bench depth.'
                ),
            })

    # ── 2. BYE WEEK STACK ─────────────────────────────────────────────────
    if bye and my_roster:
        same_bye = [p for p in my_roster if BYE_WEEKS.get(p.get('proTeam','')) == bye]
        if len(same_bye) >= 2:
            names_str = ', '.join(p['name'] for p in same_bye)
            conflicts.append({
                'severity': 'critical' if len(same_bye) >= 3 else 'warning',
                'type': 'bye_stack',
                'title': f'BYE WEEK STACK — Week {bye} ({len(same_bye)+1} players)',
                'detail': (
                    f'Adding {name} gives you {len(same_bye)+1} players on Week {bye} bye: '
                    f'{names_str} + {name}. You will be forced to start waiver wire pickups '
                    f'that week. {"CRITICAL — 4+ players off." if len(same_bye) >= 3 else "Consider spreading bye weeks."}'
                ),
            })
        elif len(same_bye) == 1:
            conflicts.append({
                'severity': 'info',
                'type': 'bye_note',
                'title': f'BYE NOTE — Week {bye} (2 players)',
                'detail': (
                    f'{name} and {same_bye[0]["name"]} both have Week {bye} bye. '
                    f'Manageable with bench depth — just be aware.'
                ),
            })

    # ── 2b. SAME-POSITION BYE COLLISION ───────────────────────────────────
    # A bye shared with a same-position player is worse than a generic stack:
    # both slots at that position go empty the same week.
    if bye and my_roster:
        same_pos_bye = [p for p in my_roster
                        if p.get('position') == pos and BYE_WEEKS.get(p.get('proTeam', '')) == bye]
        if same_pos_bye:
            names_str = ', '.join(p['name'] for p in same_pos_bye)
            # Critical when the collision wipes out your full starting quota at this position
            all_out = len(same_pos_bye) >= STARTER_NEEDS.get(pos, 1)
            conflicts.append({
                'severity': 'critical' if all_out else 'warning',
                'type': 'pos_bye_collision',
                'title': f'{pos} BYE COLLISION — Week {bye}',
                'detail': (
                    f'{name} shares a Week {bye} bye with your {pos}'
                    f'{"s" if len(same_pos_bye) > 1 else ""} {names_str}. '
                    f'Every one of them sits the same week — you would be starting '
                    f'waiver-wire fill-ins at {pos} in Week {bye}.'
                    + (' CRITICAL: your entire starting quota at this position is off that week.'
                       if all_out else ' Consider a comparable player with a different bye.')
                ),
            })

    # ── 3. NFL TEAM STACK ─────────────────────────────────────────────────
    if team and my_roster:
        same_team = [p for p in my_roster if p.get('proTeam') == team]
        if len(same_team) >= 2:
            names_str = ', '.join(p['name'] for p in same_team)
            conflicts.append({
                'severity': 'critical' if len(same_team) >= 3 else 'warning',
                'type': 'team_stack',
                'title': f'TEAM STACK — {team} ({len(same_team)+1} players)',
                'detail': (
                    f'You would have {len(same_team)+1} players from {team}: '
                    f'{names_str} + {name}. A single bad game, QB injury, or blowout tanks '
                    f'multiple starters simultaneously. '
                    f'{"DANGER: 4+ from one team." if len(same_team) >= 3 else "3 from one team is risky — one injury cascades."}'
                ),
            })
        elif len(same_team) == 1 and pos in ('WR', 'TE') and any(p.get('position') == 'QB' for p in same_team):  # I20 fix
            conflicts.append({
                'severity': 'info',
                'type': 'qb_stack',
                'title': f'QB STACK — {team}',
                'detail': (
                    f'{name} and your QB {same_team[0]["name"]} are both on {team}. '
                    f'QB stacks are a known strategy — when your QB throws TDs, your WR/TE scores too. '
                    f'High upside but correlated downside. Intentional stacks can win weeks.'
                ),
            })

    # ── 4. PLAYOFF SOS ────────────────────────────────────────────────────
    sos = PLAYOFF_SOS.get(team, 5.0)
    if sos >= 6.0 and pos in ('QB', 'RB', 'WR', 'TE'):
        conflicts.append({
            'severity': 'warning',
            'type': 'playoff_sos',
            'title': f'TOUGH PLAYOFF SCHEDULE — {team}',
            'detail': (
                f'{team} faces a difficult schedule in fantasy playoff weeks 14-16 '
                f'(SOS difficulty: {sos}/10 — top-tier defenses). '
                f'{name} may underperform when it matters most. '
                f'Consider this against a comparable player with a softer playoff draw.'
            ),
        })

    # ── 5. POSITION DEPTH WARNING ─────────────────────────────────────────
    # W7 fix: K-too-early check runs regardless of roster size
    if pos == 'K' and current_round < 13:
        conflicts.append({
            'severity': 'critical',
            'type': 'early_k',
            'title': f'KICKER TOO EARLY — Round {current_round}',
            'detail': (
                f'Drafting a kicker in Round {current_round} is a major mistake in a 15-round draft. '
                f'Kickers are interchangeable — the difference between K1 and K15 is ~2 pts/week. '
                f'Wait until Round 14-15. Use this pick on an RB, WR, or TE with real upside.'
            ),
        })

    if my_roster:
        pos_counts = defaultdict(int)
        for p in my_roster:
            pos_counts[p.get('position', '')] += 1

        need = STARTER_NEEDS.get(pos, 0)
        have = pos_counts[pos]
        flex_have = sum(max(0, pos_counts[fp] - STARTER_NEEDS[fp]) for fp in FLEX_ELIGIBLE)
        total_flex_need = STARTER_NEEDS.get('FLEX', 2)

        # I15 fix: removed dead pass block — strategy scoring handles QB suppression
        # Second QB before R5 is wasteful in single-QB league
        if pos == 'QB' and current_round < 5 and pos_counts['QB'] >= 1:
            conflicts.append({
                'severity': 'warning',
                'type': 'double_qb',
                'title': f'SECOND QB — Round {current_round}',
                'detail': (
                    f'Drafting a second QB in Round {current_round} wastes a valuable pick. '
                    f'Single-QB leagues only start one — save this slot for RB/WR depth instead. '
                    f'Stream a QB off waivers if needed mid-season.'
                ),
            })

        # RB thin — need 2 starters + 2 FLEX but only have 0-1 RBs late in draft
        if pos != 'RB' and current_round >= 8 and pos_counts['RB'] < 2:
            conflicts.append({
                'severity': 'warning',
                'type': 'rb_thin',
                'title': f'RB DEPTH CONCERN — Round {current_round}',
                'detail': (
                    f'You only have {pos_counts["RB"]} RB(s) entering Round {current_round} '
                    f'and need 2 starters + FLEX coverage. RBs are nearly impossible to find '
                    f'on waivers mid-season. Consider drafting an RB here before taking another {pos}.'
                ),
            })

    # ── 6. INJURY STATUS ──────────────────────────────────────────────────
    if candidate.get('injured') and candidate.get('injuryStatus'):
        status = candidate['injuryStatus']
        severity = 'critical' if status in ('OUT', 'IR', 'DOUBTFUL') else 'warning'
        conflicts.append({
            'severity': severity,
            'type': 'injury',
            'title': f'INJURY FLAG — {status}',
            'detail': (
                f'{name} is currently listed as {status}. '
                f'{"Do not draft unless targeting the IR slot." if severity == "critical" else "Monitor injury status before draft day — could be fine or could worsen."}'
            ),
        })

    # Sort by severity descending
    conflicts.sort(key=lambda c: SEVERITY.get(c['severity'], 0), reverse=True)
    return conflicts

# ── Player intel: values, busts, sleepers ──────────────────────────────────
# Source: FantasyPros Aug 13 2026 + The Ringer + DraftSharks
VALUE_TAGS = {
    # KEY VALUES (ADP > ECR gap = cheap relative to expert rank)
    'Chase Brown':       {'tag': 'VALUE', 'note': 'RB9 talent at ADP 16 — uncontested CIN starter (FantasyPros)'},
    'Quinshon Judkins':  {'tag': 'VALUE', 'note': 'RB2-level ceiling at RB22 ADP — Browns improved O-line (FantasyPros)'},
    'DJ Moore':          {'tag': 'VALUE', 'note': 'WR26 ADP with Josh Allen — WR1 role in BUF (FantasyPros)'},
    'Zay Flowers':       {'tag': 'VALUE', 'note': 'WR17 talent at WR7-12 finish rate — 25yo prime, $140M extension (FantasyPros)'},
    'Emeka Egbuka':      {'tag': 'VALUE', 'note': 'WR18 ADP, clear WR1 in TB with Evans gone — 22.1% target share (FantasyPros)'},
    'Drake Maye':        {'tag': 'VALUE', 'note': 'QB4 at ADP 47 — 11 picks cheaper than Lamar, now has A.J. Brown (FantasyPros)'},
    'Luther Burden III': {'tag': 'VALUE', 'note': 'Historic 2.69 YPRR as rookie — D.J. Moore targets now his (FantasyPros)'},
    'Rashee Rice':       {'tag': 'VALUE', 'note': '18.8 pts/gm when healthy — suspension served, cleared (FantasyPros)'},
    'Kenneth Walker III':{'tag': 'VALUE', 'note': 'Clear KC bell cow, Super Bowl MVP — multiple experts say should be R1 (FantasyPros)'},
    'A.J. Brown':        {'tag': 'VALUE', 'note': 'WR-NE undervalued — flagged by Ringer & FantasyPros as R1-szn loading'},
    'Bhayshul Tuten':    {'tag': 'VALUE', 'note': 'RB2 with RB1 upside at RB3 price — Etienne gone, Coen offense loves him (FantasyPros)'},
    'Jaylen Waddle':     {'tag': 'VALUE', 'note': 'WR1 in DEN pass-heavy scheme, Hill gone — WR22 ADP (FantasyPros)'},
    'Rome Odunze':       {'tag': 'VALUE', 'note': 'WR28 ADP, WR1 upside in Ben Johnson top-5 offense Year 2 (FantasyPros)'},
    'TreVeyon Henderson':{'tag': 'VALUE', 'note': 'Possible changing of guard at NE — explosive ceiling (FantasyPros)'},
    # BUSTS (ECR < ADP = overvalued by public)
    'Travis Etienne Jr.':{'tag': 'BUST', 'note': 'Alvin Kamara kills passing role — avoid at RB18 (FantasyPros)'},
    'Bucky Irving':      {'tag': 'BUST', 'note': 'YPC fell 5.4→3.4; Gainwell taking first-team reps (FantasyPros)'},
    'Jaylen Warren':     {'tag': 'BUST', 'note': 'Rico Dowdle is cheaper and McCarthys guy — overdrafted (FantasyPros)'},
    'J.K. Dobbins':      {'tag': 'BUST', 'note': 'DEN 3-way committee chaos — avoid entire situation (FantasyPros)'},
    'Trey McBride':      {'tag': 'BUST', 'note': 'Flagged as bust by FantasyPros consensus — reach at current ADP'},
    'Malik Nabers':      {'tag': 'BUST', 'note': 'NYG WR1 but Dart era uncertain — experts say avoid (FantasyPros)'},
    'DK Metcalf':        {'tag': 'BUST', 'note': 'Listed as WR bust by FantasyPros — overdrafted (FantasyPros)'},
    'Davante Adams':     {'tag': 'BUST', 'note': 'WR bust consensus — age and situation (FantasyPros)'},
    'Courtland Sutton':  {'tag': 'BUST', 'note': 'DEN committee chaos — WR bust (FantasyPros)'},
    'Jake Ferguson':     {'tag': 'BUST', 'note': 'DAL: Lamb+Pickens eat 250 targets — no room for TE1 (FantasyPros)'},
    'Harold Fannin Jr.': {'tag': 'BUST', 'note': 'TE bust per FantasyPros consensus rankings'},
    'Mark Andrews':      {'tag': 'BUST', 'note': 'TE bust at current ADP (FantasyPros)'},
    # SLEEPERS (late round targets)
    'Tre Harris':        {'tag': 'SLEEPER', 'note': 'ECR 64 at ADP 74 — WR sleeper (FantasyPros staff)'},
    'Adonai Mitchell':   {'tag': 'SLEEPER', 'note': 'ECR 62 at ADP 71 — NYJ WR sleeper (FantasyPros staff)'},
    'MarShawn Lloyd':    {'tag': 'SLEEPER', 'note': 'GB RB sleeper — late round dart (FantasyPros staff)'},
    'Tank Bigsby':       {'tag': 'SLEEPER', 'note': 'Saquon Barkley handcuff at PHI — 5.5 YPC (FantasyPros)'},
    'Dylan Sampson':     {'tag': 'SLEEPER', 'note': 'CLE RB sleeper, ECR 50 vs ADP 52 (FantasyPros)'},
    'Keaton Mitchell':   {'tag': 'SLEEPER', 'note': 'LAC RB sleeper, ECR 45 vs ADP 56 (FantasyPros)'},
    'Carnell Tate':      {'tag': 'SLEEPER', 'note': 'TEN WR sleeper — late round (FantasyPros staff)'},
    'Chig Okonkwo':      {'tag': 'SLEEPER', 'note': 'WAS TE sleeper — late round value (FantasyPros)'},
    'Isaiah Likely':     {'tag': 'SLEEPER', 'note': 'NYG TE sleeper — late round (FantasyPros)'},
    'Stefon Diggs':      {'tag': 'SLEEPER', 'note': 'ECR 45 at ADP 49 — WAS sleeper (FantasyPros)'},
    'Greg Dulcich':      {'tag': 'SLEEPER', 'note': 'MIA TE — 13 pts/gm with 4+ targets in 2025 (FantasyPros)'},
    'Jadarian Price':    {'tag': 'SLEEPER', 'note': 'SEA RB sleeper — high ROI option (FantasyPros)'},
    'RJ Harvey':         {'tag': 'SLEEPER', 'note': 'DEN RB upside dart — late round only (FantasyPros)'},
    'Xavier Worthy':     {'tag': 'SLEEPER', 'note': 'KC WR sleeper — late rounds (FantasyPros)'},
}

# ── League History ─────────────────────────────────────────────────────
# Pulled directly from ESPN API 2023-2025
# ── League History ─────────────────────────────────────────────────────────
# EXAMPLE DATA — replace with your own league's recent champions.
# The LEAGUE HISTORY strategy is built from these patterns, so it is only
# as useful as the data you put here. Pull past drafts from your league's
# ESPN history page, or delete this block and use another strategy.
LEAGUE_HISTORY = {
    2023: {
        'champion': 'Example Team A',
        'record': '10-4', 'pts': 1620.5,
        'draft': ['Derrick Henry (RB)', 'CeeDee Lamb (WR)', 'Jalen Hurts (QB)', 'Jahmyr Gibbs (RB)'],
        'strategy': 'RB → WR → Mobile QB → RB (Robust RB + early mobile QB R3)',
    },
    2024: {
        'champion': 'Example Team B',
        'record': '10-4', 'pts': 1560.0,
        'draft': ['Amon-Ra St. Brown (WR)', 'Derrick Henry (RB)', 'Rachaad White (RB)', 'James Cook (RB)'],
        'strategy': 'WR1 → RB → RB → RB (Hero WR then triple RB in R2-R4, QB not until R5)',
    },
    2025: {
        'champion': 'Example Team C',
        'record': '7-7', 'pts': 1572.9,
        'draft': ['Amon-Ra St. Brown (WR)', 'Puka Nacua (WR)', 'Tee Higgins (WR)', 'Jayden Daniels (QB)'],
        'strategy': 'WR → WR → WR → QB (Zero-RB through R3, mobile QB R4, RBs from R5+)',
    },
}

# League pattern insight — derived from LEAGUE_HISTORY above.
# Rewrite these bullets to match your own league's results.
LEAGUE_PATTERN = """
LEAGUE CHAMPION PATTERN (from LEAGUE_HISTORY above):
• All 3 champions took a WR or RB in R1 — no pure QB R1 ever won
• 2 of 3 champions had an elite WR in R1
• Mobile QBs taken R3-R4 by 2 of 3 champions (Hurts R3, Daniels R4)
• The 2024 winning formula: WR1 → 3 straight RBs → QB R5
• Zero-RB works here (2025 champion took WR-WR-WR then QB)
• The points-for leader often loses in the bracket — schedule matters
• DO NOT take a QB in R1 — no champion in this data ever has
"""

# ── Strategy definitions ───────────────────────────────────────────────────
STRATEGIES = {
    'league-history': {
        'label': 'LEAGUE HISTORY',
        'desc': '★ YOUR LEAGUE\'S WINNING PATTERN — WR1 R1 then 2-3 RBs, mobile QB R3-5. Built from the champion drafts in LEAGUE_HISTORY — edit it with your own league\'s data.',
        'source': 'Your league\'s own champion drafts (edit LEAGUE_HISTORY)',
        'round_targets': {
            1: ['WR', 'RB'],        # elite WR preferred per league history
            2: ['RB', 'WR'],        # start loading RBs
            3: ['RB', 'WR', 'QB'], # 2nd RB OR mobile QB (Hurts/Daniels tier)
            4: ['RB', 'QB', 'WR'], # 3rd RB or QB if not taken
            5: ['QB', 'WR', 'RB'], # lock in QB here at latest
            6: ['WR', 'RB', 'TE'],
            7: ['WR', 'RB'],
            8: ['WR', 'RB', 'TE'],
            9: ['RB', 'WR'],
            10: ['WR', 'RB'],
            11: ['RB', 'WR', 'TE'],
            12: ['TE', 'WR', 'RB'],
            13: ['WR', 'RB'],
            14: ['K'],
            15: ['RB', 'K', 'QB'],
        },
        'avoid': {1: 'QB', 2: 'QB'},  # no QB before R3 per league history
        'value_boost': 20,  # bonus for VALUE-tagged players
    },
    'rb-heavy': {
        'label': 'ROBUST RB',
        'desc': 'Grab 2-3 elite RBs in R1-R4 to lock down the scarcest position (aka RB Heavy). RB scarcity is real in 10-team leagues. Source: DraftSharks (Smola), ETR (Silva), Footballguys.',
        'source': 'DraftSharks (Jared Smola) + ETR + Footballguys',
        'round_targets': {
            1: ['RB', 'WR'],
            2: ['RB', 'WR'],
            3: ['RB', 'WR', 'TE'],
            4: ['WR', 'RB', 'TE'],
            5: ['WR', 'TE', 'QB'],
            6: ['QB', 'WR', 'RB'],
            7: ['WR', 'RB', 'TE'],
            8: ['WR', 'RB'],
            9: ['RB', 'WR'],
            10: ['WR', 'RB'],
            11: ['RB', 'WR', 'TE'],
            12: ['WR', 'RB'],
            13: ['TE', 'WR'],
            14: ['K'],
            15: ['K', 'QB'],
        },
        'avoid': {1: 'QB', 2: 'QB', 3: 'QB', 4: 'QB'},
        'value_boost': 15,
    },
    'hero-rb': {
        'label': 'HERO RB',
        'desc': 'One elite RB anchor in R1, then stack WRs through the middle rounds — RB depth comes from mid/late upside darts. Widely cited as the balanced-risk default (aka Anchor RB).',
        'source': 'FantasyPros / Fantasy Life / Yahoo analyst roundtables — "Hero/Anchor RB"',
        'round_targets': {
            1: ['RB'],
            2: ['WR', 'TE'],
            3: ['WR', 'TE'],
            4: ['WR', 'TE'],
            5: ['WR', 'QB'],
            6: ['QB', 'RB', 'WR'],
            7: ['RB', 'WR'],
            8: ['RB', 'WR'],
            9: ['RB', 'WR'],
            10: ['WR', 'RB'],
            11: ['RB', 'TE'],
            12: ['RB', 'WR', 'TE'],
            13: ['RB', 'WR'],
            14: ['K'],
            15: ['RB', 'K'],
        },
        'avoid': {1: 'QB', 2: 'QB', 3: ['QB', 'RB'], 4: ['QB', 'RB']},
        'value_boost': 15,
    },
    'late-qb': {
        'label': 'LATE-ROUND QB',
        'desc': 'Hammer RB/WR for 7 straight rounds — QB scoring flattens after the elite tier, so wait until R8-12 and consider a 2nd late QB to stream. Data-supported for championship rates.',
        'source': 'JJ Zachariason "Late-Round QB" / FantasyPros / DraftStrategy.io',
        'round_targets': {
            1: ['RB', 'WR'],
            2: ['RB', 'WR'],
            3: ['WR', 'RB'],
            4: ['WR', 'RB', 'TE'],
            5: ['RB', 'WR', 'TE'],
            6: ['WR', 'RB'],
            7: ['WR', 'RB', 'TE'],
            8: ['QB', 'WR', 'RB'],
            9: ['QB', 'RB', 'WR'],
            10: ['QB', 'WR', 'RB'],
            11: ['RB', 'WR', 'TE'],
            12: ['QB', 'WR', 'RB'],
            13: ['RB', 'WR'],
            14: ['K'],
            15: ['QB', 'K', 'RB'],
        },
        'avoid': {1: 'QB', 2: 'QB', 3: 'QB', 4: 'QB', 5: 'QB', 6: 'QB', 7: 'QB'},
        'value_boost': 15,
    },
    'zero-rb': {
        'label': 'ZERO-RB',
        'desc': 'Stack WR/TE early, avoid RB until R5+. Live on waivers. Source: Shawn Siegele (RotoViz), FantasyPros, ETR. Best from picks 4-8.',
        'source': 'Shawn Siegele (RotoViz) — popularized 2014; validated FantasyPros/ETR annually',
        'round_targets': {
            1: ['WR', 'TE'],
            2: ['WR', 'TE'],
            3: ['WR', 'QB', 'TE'],
            4: ['WR', 'QB'],
            5: ['RB', 'WR'],
            6: ['RB', 'WR'],
            7: ['RB', 'WR'],
            8: ['RB', 'WR'],
            9: ['RB', 'WR'],
            10: ['RB', 'WR'],
            11: ['RB', 'WR'],
            12: ['RB', 'TE'],
            13: ['RB', 'WR'],
            14: ['K'],
            15: ['RB', 'K'],
        },
        'avoid': {1: ['RB', 'QB'], 2: ['RB', 'QB'], 3: ['RB'], 4: ['RB']},
        'value_boost': 10,
    },
    'hero-qb': {
        'label': 'HERO QB',
        'desc': 'Lock an elite QB in R1 for a weekly ceiling advantage. Source: DraftSharks (Hallam) "Elite QB or Late-Round QB?" WARNING: spending R1 capital on a QB is contrarian — most league champions do not.',
        'source': 'DraftSharks (Shane Hallam) — "Elite QB or Late-Round QB?" 2026',
        'round_targets': {
            1: ['QB'],
            2: ['RB', 'WR'],
            3: ['RB', 'WR'],
            4: ['WR', 'RB'],
            5: ['TE', 'WR'],
            6: ['WR', 'RB'],
            7: ['RB', 'WR'],
            8: ['WR', 'RB'],
            9: ['RB', 'WR'],
            10: ['WR', 'RB'],
            11: ['TE', 'WR'],
            12: ['RB', 'WR'],
            13: ['WR', 'RB'],
            14: ['K'],
            15: ['RB', 'K'],
        },
        'avoid': {2: 'QB', 3: 'QB', 4: 'QB'},
        'value_boost': 10,
    },
    'balanced': {
        'label': 'BALANCED BPA',
        'desc': 'Best Player Available — highest value regardless of position, need as tiebreak. Source: The Ringer (Danny Heifetz) "10 Rules to Win Your Fantasy Football League."',
        'source': 'The Ringer — Danny Heifetz "10 Rules to Win Your Fantasy Football League"',
        'round_targets': {
            1: ['RB', 'WR'],
            2: ['RB', 'WR'],
            3: ['WR', 'RB', 'TE'],
            4: ['WR', 'RB', 'TE', 'QB'],
            5: ['TE', 'WR', 'QB'],
            6: ['QB', 'WR', 'RB'],
            7: ['RB', 'WR'],
            8: ['WR', 'RB', 'TE'],
            9: ['RB', 'WR'],
            10: ['WR', 'RB'],
            11: ['RB', 'WR'],
            12: ['TE', 'WR'],
            13: ['RB', 'WR'],
            14: ['K'],
            15: ['QB', 'K'],
        },
        'avoid': {1: 'QB', 2: 'QB', 3: 'QB'},
        'value_boost': 15,
    },
}

# ── App State ──────────────────────────────────────────────────────────────
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Fictional team names for demo mode
DEMO_TEAMS = [
    'Gridiron Gophers', 'Sunday Scaries', 'Blitz Brigade',
    'End Zone Militia', 'Hail Mary Inc.', 'Play Action Heroes',
    'Turf Monsters', 'Two Minute Drill', 'Pylon Pirates', 'Backfield Bandits'
]

state = {
    'board': [],
    'drafted': set(),
    'draft_log': [],
    'my_roster': [],
    'last_poll': 0,
    'last_board_refresh': 0,
    'total_picks': 0,
    'strategy': 'league-history',
    'draft_pos': 5,
    'live': False,
    'status': 'INITIALIZING...',
    'my_pick_slots': [],
    'board_ready': False,
    'recommendations': [],
    'current_round': 1,
    'next_pick_num': 1,
    'next_pick_is_mine': False,
    # Demo mode state
    'demo': False,
    'demo_running': False,
    'demo_waiting_for_pick': False,
    'demo_speed': 4,  # seconds between opponent picks
}
state_lock = threading.Lock()
demo_pick_event = threading.Event()   # fired when user submits their demo pick
demo_stop_event  = threading.Event()   # fired when demo is cancelled — wakes sleeping opponent loop


def connect():
    return League(league_id=LEAGUE_ID, year=YEAR, espn_s2=ESPN_S2, swid=SWID)


def build_board():
    league = connect()
    positions = ['QB', 'RB', 'WR', 'TE', 'K']
    master = {}
    for pos in positions:
        for p in league.free_agents(size=80, position=pos):
            info = VALUE_TAGS.get(p.name, {})
            master[p.name] = {
                'name': p.name,
                'position': p.position,
                'proTeam': p.proTeam,
                'proj_season': round(p.projected_total_points, 1),
                'proj_avg': round(p.projected_avg_points, 2),
                'percent_owned': round(p.percent_owned, 1),
                'injured': p.injured,
                'injuryStatus': p.injuryStatus or '',
                'playerId': p.playerId,
                'tag': info.get('tag', ''),
                'note': info.get('note', ''),
            }
    board = sorted(master.values(), key=lambda x: x['proj_season'], reverse=True)
    for i, p in enumerate(board, 1):
        p['overall_rank'] = i
    return board


def snake_pick_num(round_num, slot, total=10):
    if round_num % 2 == 1:
        return (round_num - 1) * total + slot
    else:
        return (round_num - 1) * total + (total + 1 - slot)


def my_pick_slots(draft_pos, total=10, rounds=15):
    # draft_pos is your column on the board; snake_pick_num already handles
    # even-round reversal. (Pre-inverting the slot here double-inverted and
    # produced a linear draft: 7, 17, 27... instead of 7, 14, 27, 34...)
    return [snake_pick_num(r, draft_pos, total) for r in range(1, rounds + 1)]


def round_from_pick(pick_num, total=10):
    return ((pick_num - 1) // total) + 1


def get_need_score(pos, my_roster):
    counts = defaultdict(int)
    for p in my_roster:
        counts[p['position']] += 1
    starter_need = STARTER_NEEDS.get(pos, 0)
    have = counts[pos]
    flex_have = sum(max(0, counts[fp] - STARTER_NEEDS[fp]) for fp in FLEX_ELIGIBLE)
    if pos in FLEX_ELIGIBLE:
        unfilled = max(0, starter_need - have)
        unfilled_flex = max(0, STARTER_NEEDS['FLEX'] - flex_have)
        return unfilled + (0.5 * unfilled_flex)
    return max(0, starter_need - have)


def score_player(p, my_roster, strategy_key, current_round):
    """Score a single player for the given strategy/round. Returns numeric score."""
    strat = STRATEGIES[strategy_key]
    targets = strat['round_targets'].get(current_round, ['RB', 'WR', 'QB', 'TE', 'K'])
    avoid = strat['avoid'].get(current_round)
    value_boost = strat.get('value_boost', 15)
    pos = p['position']

    # avoid can be a string or list of positions
    avoid_list = avoid if isinstance(avoid, list) else ([avoid] if avoid else [])
    if pos in avoid_list:
        return -999

    # Surplus block — raw projections overvalue QBs, so without this the
    # engine recommends a 2nd/3rd QB whenever elite skill players run out.
    # One QB/TE until late rounds (backup in R12+), never a 3rd; one K ever.
    have = sum(1 for r in my_roster if r.get('position') == pos)
    if pos in ('QB', 'TE'):
        if have >= 2 or (have >= 1 and current_round < 12):
            return -999
    if pos == 'K' and have >= 1:
        return -999

    base = p['proj_season']

    # Position in targets → bonus; NOT in targets → penalty
    # W5 fix: extra QB suppression in early rounds for skill-position strategies
    if pos in targets:
        idx = targets.index(pos)
        strat_bonus = 15 - (idx * 4)  # +15, +11, +7, +3 ...
    else:
        qb_suppress = -25 if (pos == 'QB' and current_round <= 4
                              and strategy_key in ('zero-rb', 'rb-heavy', 'league-history', 'balanced')) else 0
        strat_bonus = -40 + qb_suppress

    need = get_need_score(pos, my_roster)
    need_bonus = need * 8

    tag_delta = 0
    if p.get('tag') == 'VALUE':
        tag_delta = value_boost
    elif p.get('tag') == 'BUST':
        tag_delta = -25

    k_penalty = 200 if (pos == 'K' and current_round < 13) else 0

    return round(base + strat_bonus + need_bonus + tag_delta - k_penalty, 1)


def recommend(board, drafted, my_roster, strategy_key, current_round):
    avail = [p for p in board if p['name'] not in drafted]
    strat = STRATEGIES[strategy_key]
    targets = strat['round_targets'].get(current_round, [])
    avoid_raw = strat['avoid'].get(current_round)
    avoid_list = avoid_raw if isinstance(avoid_raw, list) else ([avoid_raw] if avoid_raw else [])
    value_boost = strat.get('value_boost', 15)

    scored = []
    for p in avail[:120]:
        s = score_player(p, my_roster, strategy_key, current_round)
        if s <= -900:
            continue
        pos = p['position']
        # W6 fix: display strat_bonus matching what score_player actually used
        if pos in targets:
            strat_bonus = 15 - targets.index(pos) * 4
        else:
            qb_suppress = -25 if (pos == 'QB' and current_round <= 4
                                  and strategy_key in ('zero-rb', 'rb-heavy', 'league-history', 'balanced')) else 0
            strat_bonus = -40 + qb_suppress
        need_bonus = round(get_need_score(pos, my_roster) * 8, 1)
        tag_delta = value_boost if p.get('tag') == 'VALUE' else (-25 if p.get('tag') == 'BUST' else 0)
        scored.append({
            **p,
            'score': s,
            'strat_bonus': strat_bonus,
            'need_bonus': need_bonus,
            'tag_delta': tag_delta,
        })

    scored.sort(key=lambda x: x['score'], reverse=True)
    top = scored[:6]
    for p in top:
        p['conflicts'] = analyze_conflicts(p, my_roster, current_round)
    return top


def roast_pick(player_info, team_id, pick_num, current_round, draft_log):
    """
    Flag a questionable opponent pick for comic relief. Returns
    {'label', 'reason'} or None. Deliberately conservative — a roast
    should be obviously earned, not a nitpick.
    """
    pos = player_info.get('position', '?')
    name = player_info.get('name', 'That guy')
    rank = player_info.get('overall_rank', 999)

    # Count what this team already has
    counts = defaultdict(int)
    for e in draft_log:
        if e.get('team_id') == team_id:
            p = e.get('position')
            if p and p != '?':
                counts[p] += 1

    # 1. Kicker way too early — the classic
    if pos == 'K' and current_round < 12:
        return {'label': 'STUPID PICK',
                'reason': f'A KICKER. IN ROUND {current_round}. {name.upper()} WILL SCORE 7 POINTS AND BREAK YOUR HEART.'}

    # 2. Hoarding a onesie position
    if pos == 'QB' and counts['QB'] >= 2:
        return {'label': 'STUPID PICK',
                'reason': f'THAT IS QB #{counts["QB"] + 1}. YOU CAN ONLY START ONE. WE HAVE ALL SEEN THE ROSTER RULES.'}
    if pos == 'TE' and counts['TE'] >= 2:
        return {'label': 'STUPID PICK',
                'reason': f'TE #{counts["TE"] + 1}. BOLD STRATEGY — ASSUMING THE LEAGUE ADDED TWO TE SLOTS OVERNIGHT.'}

    # 3. Backup QB while the board still has starters
    if pos == 'QB' and counts['QB'] == 1 and current_round <= 9:
        return {'label': 'QUESTIONABLE',
                'reason': f'A SECOND QB IN ROUND {current_round}? THE FIRST ONE PLAYS EVERY WEEK, YOU KNOW.'}

    # 4. Big reach vs the projection board (skip fallback/unknown players)
    if rank < 900 and rank - pick_num >= 30:
        return {'label': 'STUPID PICK',
                'reason': f'{name.upper()} IS BOARD #{rank} AND WENT AT PICK {pick_num}. NOBODY WAS TAKING HIM. NOBODY.'}

    # 5. Reaching on a flagged bust early
    if player_info.get('tag') == 'BUST' and current_round <= 5:
        return {'label': 'STUPID PICK',
                'reason': f'{name.upper()} IS TAGGED BUST AND IT IS ONLY ROUND {current_round}. ENJOY THAT.'}

    # 6. Injured player early
    if player_info.get('injured') and current_round <= 4:
        status = player_info.get('injuryStatus') or 'INJURED'
        return {'label': 'QUESTIONABLE',
                'reason': f'{name.upper()} IS {status}. DRAFTING THE MEDICAL REPORT IN ROUND {current_round}.'}

    return None


def grade_pick(player_name, recommendations):
    """
    Grade a pick against what the engine recommended at that moment.
    Returns {'grade', 'note'} — rank 1 is an A+, falling off from there.
    """
    names = [r['name'] for r in recommendations]
    if player_name in names:
        rank = names.index(player_name) + 1
        table = {1: ('A+', 'Top recommendation'),
                 2: ('A',  'Rec #2'),
                 3: ('A-', 'Rec #3'),
                 4: ('B+', 'Rec #4'),
                 5: ('B',  'Rec #5'),
                 6: ('B-', 'Rec #6')}
        grade, note = table.get(rank, ('B-', f'Rec #{rank}'))
        return {'grade': grade, 'note': note, 'rec_rank': rank}
    return {'grade': 'C', 'note': 'Off-board — not in TOP PICKS', 'rec_rank': None}


def detect_position_run(draft_log, window=5, threshold=3):
    """
    Look at the last `window` picks; flag any position taken >= `threshold`
    times. Returns {'position', 'count', 'window'} or None.
    """
    recent = sorted(draft_log, key=lambda e: e['pick_num'], reverse=True)[:window]
    if len(recent) < window:
        return None
    counts = defaultdict(int)
    for e in recent:
        pos = e.get('position')
        if pos and pos != '?':
            counts[pos] += 1
    if not counts:
        return None
    pos, count = max(counts.items(), key=lambda kv: kv[1])
    if count >= threshold:
        return {'position': pos, 'count': count, 'window': window}
    return None


def score_full_board(board, drafted, my_roster, strategy_key, current_round):
    """Board always sorts by raw ESPN projection — best player available, position-agnostic.
    Strategy score is shown as secondary info but does NOT control sort order.
    Strategy only drives TOP PICKS recommendations.
    """
    result = []
    for p in board:
        if p['name'] in drafted:
            continue
        s = score_player(p, my_roster, strategy_key, current_round)
        # Avoid penalty → show raw proj so player still appears
        if s <= -900:
            s = p['proj_season']
        result.append({**p, 'strategy_score': round(s, 1)})
    # Always sort by raw projection — best player available regardless of strategy
    result.sort(key=lambda x: x['proj_season'], reverse=True)
    return result


def refresh_board(existing):
    """
    Re-pull player data and merge it into the existing board.

    build_board() reads ESPN's free-agent list, which drops players as they
    get drafted — so a straight replacement would erase everyone already
    taken. Instead we update the rows that come back, keep any row that has
    disappeared (i.e. was drafted), and append genuinely new players.
    """
    fresh = build_board()
    if not fresh:
        return existing
    by_name = {p['name']: p for p in fresh}
    merged = []
    for p in existing:
        f = by_name.pop(p['name'], None)
        merged.append({**p, **f} if f else p)
    merged.extend(by_name.values())
    merged.sort(key=lambda x: x['proj_season'], reverse=True)
    for i, p in enumerate(merged, 1):
        p['overall_rank'] = i
    return merged


def poll_draft():
    with state_lock:
        state['status'] = 'LOADING PLAYER BOARD...'
    try:
        board = build_board()
        with state_lock:
            state['board'] = board
            state['board_ready'] = True
            # Pre-compute initial recommendations
            recs = recommend(board, set(), [], 'league-history', 1)
            state['recommendations'] = recs
            state['status'] = 'BOARD READY. AWAITING DRAFT START.'
    except Exception as e:
        with state_lock:
            state['status'] = f'ERROR: {e}'
        return

    name_to_info = {p['name']: p for p in board}
    last_board_refresh = time.time()
    with state_lock:
        state['last_board_refresh'] = int(last_board_refresh)

    while True:
        # Keep projections and injury status current during long sessions.
        # Runs whether or not live mode is on, so the board is fresh by the
        # time the draft actually starts.
        if time.time() - last_board_refresh >= BOARD_REFRESH_SECONDS:
            last_board_refresh = time.time()
            try:
                with state_lock:
                    current = state['board'][:]
                merged = refresh_board(current)          # network call, outside the lock
                with state_lock:
                    state['board'] = merged
                    state['last_board_refresh'] = int(last_board_refresh)
                name_to_info = {p['name']: p for p in merged}
            except Exception:
                pass    # keep the existing board if ESPN is unreachable

        # C3 fix: read state['live'] under lock
        with state_lock:
            is_live = state['live']
        if not is_live:
            time.sleep(2)
            continue
        try:
            league = connect()
            picks = league.draft
            total = len(picks)

            with state_lock:
                last = state['total_picks']
                draft_pos = state['draft_pos']
                strategy = state['strategy']
                slots = my_pick_slots(draft_pos)
                state['my_pick_slots'] = slots

                if total > last:
                    new_picks = picks[last:total]
                    for i, pick in enumerate(new_picks):
                        pick_num = last + i + 1
                        # Ground truth from ESPN — immune to draft_pos/slot-math errors
                        is_mine = pick.team.team_id == MY_TEAM_ID
                        rnd = round_from_pick(pick_num)
                        entry = {
                            'pick_num': pick_num,
                            'round': rnd,
                            'round_pick': pick.round_pick,
                            'player': pick.playerName,
                            'position': name_to_info.get(pick.playerName, {}).get('position', '?'),
                            'team': pick.team.team_name,
                            'team_id': pick.team.team_id,
                            'is_mine': is_mine,
                        }
                        if is_mine:
                            entry['grade'] = grade_pick(pick.playerName, state.get('recommendations', []))
                        else:
                            # Roast questionable opponent picks (comic relief)
                            info = name_to_info.get(pick.playerName)
                            if info:
                                roast = roast_pick(info, pick.team.team_id, pick_num, rnd, state['draft_log'])
                                if roast:
                                    entry['roast'] = roast
                        state['draft_log'].insert(0, entry)
                        state['drafted'].add(pick.playerName)
                        if is_mine:
                            info = name_to_info.get(pick.playerName, {
                                'name': pick.playerName, 'position': '?',
                                'proTeam': '?', 'proj_season': 0,
                                'proj_avg': 0, 'overall_rank': 999,
                                'injured': False, 'injuryStatus': '',
                                'tag': '', 'note': '',
                            })
                            state['my_roster'].append(info)
                    state['total_picks'] = total

                next_pick = total + 1
                current_round = round_from_pick(next_pick)
                # W9 fix: compute recs outside lock by snapshotting state first
                board_s = state['board']
                drafted_s = set(state['drafted'])
                roster_s = state['my_roster'][:]
                state['next_pick_num'] = next_pick
                state['next_pick_is_mine'] = next_pick in slots
                state['current_round'] = current_round
                state['last_poll'] = int(time.time())

                if total == 0:
                    state['status'] = 'DRAFT ROOM OPEN. WAITING FOR FIRST PICK...'
                else:
                    mine_str = '>>> YOUR PICK <<<' if state['next_pick_is_mine'] else f'PICK #{next_pick} (R{current_round})'
                    state['status'] = f'{total} PICKS MADE | NEXT: {mine_str}'

            # Compute recs outside the lock
            recs = recommend(board_s, drafted_s, roster_s, strategy, current_round)
            with state_lock:
                state['recommendations'] = recs

        except Exception as e:
            with state_lock:
                state['status'] = f'POLL ERROR: {e}'

        time.sleep(POLL_SECONDS)


def demo_vorp_table(board):
    """
    Value Over Replacement Player — the reason real drafters don't take
    5 QBs: Josh Allen's 420 pts only beat the 12th QB by ~90, while an
    elite RB beats replacement by ~180. Ordering by VORP instead of raw
    projection makes demo opponents draft like humans.
    """
    by_pos = defaultdict(list)
    for p in board:  # board is already sorted by proj_season desc
        by_pos[p['position']].append(p)
    baseline_idx = {'QB': 11, 'RB': 24, 'WR': 24, 'TE': 11, 'K': 9}
    baseline = {}
    for pos, lst in by_pos.items():
        i = min(baseline_idx.get(pos, 20), len(lst) - 1)
        baseline[pos] = lst[i]['proj_season']
    return {p['name']: p['proj_season'] - baseline[p['position']] for p in board}


def demo_chaos_pick(counts, current_round, avail):
    """
    Every league has that guy. Occasionally make a genuinely bad pick so
    demo drafts feel like real ones (and the roast engine has something
    to chew on). Returns a player dict or None.
    """
    options = []
    if current_round < 12 and counts['K'] == 0:
        k = next((p for p in avail if p['position'] == 'K'), None)
        if k:
            options.append(k)                       # kicker way too early
    if counts['QB'] >= 1:
        qb = next((p for p in avail if p['position'] == 'QB'), None)
        if qb:
            options.append(qb)                      # QB they cannot start
    if len(avail) > 60:
        options.append(random.choice(avail[40:60])) # wild reach off the board
    return random.choice(options) if options else None


def demo_opponent_pick(team_idx, current_round, state, vorp):
    """
    Choose a realistic pick for a demo opponent:
    VORP order + positional roster sense + light randomness.
    Caller must hold state_lock.
    """
    counts = defaultdict(int)
    for e in state['draft_log']:
        if not e.get('is_mine') and e.get('team_id') == team_idx:
            counts[e.get('position', '?')] += 1

    avail = [p for p in state['board'] if p['name'] not in state['drafted']]
    avail.sort(key=lambda p: -vorp.get(p['name'], -999))

    # Roll for chaos before the sensible logic runs
    if random.random() < state.get('demo_chaos', DEMO_CHAOS_RATE):
        dumb = demo_chaos_pick(counts, current_round, avail)
        if dumb:
            return dumb

    # Must-fill: grab a kicker in the last two rounds if still missing
    if current_round >= 14 and counts['K'] == 0:
        k = next((p for p in avail if p['position'] == 'K'), None)
        if k:
            return k

    def allowed(p):
        pos = p['position']
        if pos == 'K':
            return current_round >= 13 and counts['K'] < 1
        if pos in ('QB', 'TE'):
            if counts[pos] >= 2:
                return False           # never a 3rd QB/TE
            if counts[pos] >= 1 and current_round < 12:
                return False           # backup only in late rounds
            return True
        return counts[pos] < 8         # RB/WR effectively uncapped

    candidates = [p for p in avail if allowed(p)][:3]
    if not candidates:
        candidates = avail[:1]
    if not candidates:
        return None
    weights = [0.6, 0.25, 0.15][:len(candidates)]
    return random.choices(candidates, weights=weights, k=1)[0]


def run_demo():
    """
    Demo mode: simulates a full 15-round snake draft.
    Opponents auto-pick from the board every demo_speed seconds.
    Pauses on your turn and waits for user to submit a pick via /api/demo/pick.
    """
    with state_lock:
        state['demo_running'] = True
        draft_pos = state['draft_pos']
        board = state['board'][:]
        state['status'] = 'DEMO MODE — DRAFT STARTING...'

    slots = my_pick_slots(draft_pos)
    vorp = demo_vorp_table(board)

    for pick_num in range(1, TOTAL_TEAMS * TOTAL_ROUNDS + 1):
        with state_lock:
            if not state['demo']:
                break  # user cancelled demo
            strategy = state['strategy']
            speed = state['demo_speed']

        current_round = round_from_pick(pick_num)
        is_mine = pick_num in slots

        # Figure out which team is picking
        slot_in_round = (pick_num - 1) % TOTAL_TEAMS
        if current_round % 2 == 1:
            team_idx = slot_in_round
        else:
            team_idx = TOTAL_TEAMS - 1 - slot_in_round
        team_name = DEMO_TEAMS[team_idx]

        if is_mine:
            # ── MY TURN — pause and wait ──────────────────────────────────
            with state_lock:
                demo_pick_event.clear()  # clear FIRST inside lock (prevents race)
                state['demo_waiting_for_pick'] = True
                state['next_pick_is_mine'] = True
                state['next_pick_num'] = pick_num
                state['current_round'] = current_round
                state['status'] = f'DEMO | R{current_round} PICK #{pick_num} — ⚡ YOUR TURN! Click a recommendation or type a name below.'
                recs = recommend(state['board'], state['drafted'], state['my_roster'], strategy, current_round)
                state['recommendations'] = recs

            demo_pick_event.wait(timeout=300)  # wait up to 5 min for user pick

            with state_lock:
                state['demo_waiting_for_pick'] = False
                state['next_pick_is_mine'] = False
                # pick was already recorded by /api/demo/pick endpoint

        else:
            # ── OPPONENT AUTO-PICK ────────────────────────────────────────
            # Use demo_stop_event.wait() instead of time.sleep() so stop is instant
            demo_stop_event.wait(timeout=speed)
            if demo_stop_event.is_set():
                break  # cancelled

            with state_lock:
                # Realistic opponent pick: VORP order + roster needs + randomness
                player_info = demo_opponent_pick(team_idx, current_round, state, vorp)
                if not player_info:
                    break
                picked = player_info['name']

                state['drafted'].add(picked)
                roast = roast_pick(player_info, team_idx, pick_num, current_round, state['draft_log'])
                entry = {
                    'pick_num': pick_num,
                    'round': current_round,
                    'round_pick': slot_in_round + 1,
                    'player': picked,
                    'position': player_info['position'],
                    'team': team_name,
                    'team_id': team_idx,
                    'is_mine': False,
                }
                if roast:
                    entry['roast'] = roast
                state['draft_log'].insert(0, entry)
                state['total_picks'] = pick_num
                state['next_pick_num'] = pick_num + 1
                state['next_pick_is_mine'] = (pick_num + 1) in slots
                state['current_round'] = round_from_pick(pick_num + 1)
                state['last_poll'] = int(time.time())

                # Update recommendations
                recs = recommend(state['board'], state['drafted'], state['my_roster'], strategy, state['current_round'])
                state['recommendations'] = recs
                state['status'] = f'DEMO | {pick_num} PICKS MADE | {team_name} took {picked}'

    with state_lock:
        state['demo_running'] = False
        state['demo'] = False
        picks_made = len(state['my_roster'])
        state['status'] = f'DEMO COMPLETE! You drafted {picks_made} players. Click RESET DEMO to try again.'


@app.get('/api/conflicts')
def get_conflicts(player: str):
    """Return conflict analysis for any player vs current roster."""
    with state_lock:
        my_roster = state['my_roster']
        current_round = state['current_round']
        board = state['board']
    player_info = next((p for p in board if p['name'].lower() == player.lower()), None)
    if not player_info:
        player_info = next((p for p in board if player.lower() in p['name'].lower()), None)
    if not player_info:
        return JSONResponse({'ok': False, 'conflicts': [], 'error': 'Player not found'})
    conflicts = analyze_conflicts(player_info, my_roster, current_round)
    return JSONResponse({'ok': True, 'player': player_info['name'], 'conflicts': conflicts,
                         'bye_week': BYE_WEEKS.get(player_info.get('proTeam',''), None)})


@app.get('/api/state')
def get_state():
    # W8 fix: copy state under lock, compute scored_board outside lock
    with state_lock:
        board_snap    = state['board'][:]
        drafted_snap  = set(state['drafted'])
        roster_snap   = state['my_roster'][:]
        strategy_snap = state['strategy']
        round_snap    = state.get('current_round', 1)
        state_copy = {
            'status':                state['status'],
            'live':                  state['live'],
            'strategy':              state['strategy'],
            'draft_pos':             state['draft_pos'],
            'total_picks':           state['total_picks'],
            'current_round':         state.get('current_round', 1),
            'next_pick_num':         state.get('next_pick_num', 1),
            'next_pick_is_mine':     state.get('next_pick_is_mine', False),
            'my_pick_slots':         state.get('my_pick_slots', []),
            'recommendations':       state.get('recommendations', []),
            'my_roster':             state['my_roster'],
            'draft_log':             state['draft_log'][:],  # full log — grid board needs every pick
            'drafted':               list(state['drafted']),  # C4: full set not just log[:30]
            'board_ready':           state['board_ready'],
            'last_poll':             state['last_poll'],
            'last_board_refresh':    state.get('last_board_refresh', 0),
            'demo':                  state['demo'],
            'demo_running':          state['demo_running'],
            'demo_waiting_for_pick': state['demo_waiting_for_pick'],
            'demo_speed':            state['demo_speed'],
            'position_run':          detect_position_run(state['draft_log']),
            'bye_weeks':             BYE_WEEKS,
            'strategies':            {k: {'label': v['label'], 'desc': v['desc'], 'source': v['source']} for k, v in STRATEGIES.items()},
            'league_history':           LEAGUE_HISTORY,
            'league_pattern':           LEAGUE_PATTERN,
        }
    # Score board outside the lock
    scored_board = score_full_board(board_snap, drafted_snap, roster_snap, strategy_snap, round_snap)
    state_copy['available_board'] = scored_board[:150]
    return JSONResponse(state_copy)


@app.post('/api/config')
async def set_config(data: dict):
    with state_lock:
        if 'strategy' in data:
            state['strategy'] = data['strategy']
            # Recompute recs immediately on strategy change
            if state['board']:
                next_pick = state['total_picks'] + 1
                rnd = round_from_pick(next_pick)
                state['recommendations'] = recommend(
                    state['board'], state['drafted'],
                    state['my_roster'], data['strategy'], rnd
                )
        if 'draft_pos' in data:
            state['draft_pos'] = int(data['draft_pos'])
            state['my_pick_slots'] = my_pick_slots(int(data['draft_pos']))
        if 'live' in data:
            state['live'] = bool(data['live'])
        if 'demo_speed' in data:
            state['demo_speed'] = max(1, min(10, int(data['demo_speed'])))
        if 'demo_chaos' in data:
            state['demo_chaos'] = max(0.0, min(1.0, float(data['demo_chaos'])))
    return {'ok': True}


@app.post('/api/demo/start')
async def demo_start():
    """Start demo mode — resets state and launches the sim thread."""
    with state_lock:
        if not state['board_ready']:
            return JSONResponse({'ok': False, 'error': 'Board not loaded yet'}, status_code=400)
        if state['demo_running']:
            return JSONResponse({'ok': False, 'error': 'Demo already running'}, status_code=400)
        # Clear stale signals from a previous stop — otherwise the new demo
        # thread sees the old stop event and exits on its first pick
        demo_stop_event.clear()
        demo_pick_event.clear()
        # Reset draft state
        state['drafted'] = set()
        state['draft_log'] = []
        state['my_roster'] = []
        state['total_picks'] = 0
        state['current_round'] = 1
        state['next_pick_num'] = 1
        state['next_pick_is_mine'] = False
        state['demo'] = True
        state['demo_waiting_for_pick'] = False
        state['live'] = False  # demo is separate from live
        slots = my_pick_slots(state['draft_pos'])
        state['my_pick_slots'] = slots
        recs = recommend(state['board'], set(), [], state['strategy'], 1)
        state['recommendations'] = recs

    t = threading.Thread(target=run_demo, daemon=True)
    t.start()
    return {'ok': True}


@app.post('/api/demo/pick')
async def demo_pick(data: dict):
    """User submits their pick during demo mode."""
    player_name = data.get('player', '').strip()
    with state_lock:
        if not state['demo_waiting_for_pick']:
            return JSONResponse({'ok': False, 'error': 'Not your turn'}, status_code=400)

        # Find the player on the board
        player_info = next((p for p in state['board'] if p['name'].lower() == player_name.lower()), None)
        if not player_info:
            # Try partial match
            player_info = next((p for p in state['board'] if player_name.lower() in p['name'].lower()
                                and p['name'] not in state['drafted']), None)
        if not player_info:
            return JSONResponse({'ok': False, 'error': f'Player "{player_name}" not found or already drafted'}, status_code=400)
        if player_info['name'] in state['drafted']:
            return JSONResponse({'ok': False, 'error': f'{player_info["name"]} already drafted'}, status_code=400)

        pick_num = state['next_pick_num']
        current_round = state['current_round']
        draft_pos = state['draft_pos']
        slot_in_round = (pick_num - 1) % TOTAL_TEAMS

        # Grade against the recommendations shown when you were on the clock
        grade = grade_pick(player_info['name'], state.get('recommendations', []))

        state['drafted'].add(player_info['name'])
        state['my_roster'].append(player_info)
        state['draft_log'].insert(0, {
            'pick_num': pick_num,
            'round': current_round,
            'round_pick': slot_in_round + 1,
            'player': player_info['name'],
            'position': player_info['position'],
            'team': 'YOU',
            'team_id': MY_TEAM_ID,  # W11 fix: use constant not hardcoded 10
            'is_mine': True,
            'grade': grade,
        })
        state['total_picks'] = pick_num
        state['last_poll'] = int(time.time())
        state['status'] = f'DEMO | YOU picked {player_info["name"]} ({player_info["position"]}, {player_info["proTeam"]}) ✓'

    demo_pick_event.set()
    return {'ok': True, 'player': player_info['name'], 'position': player_info['position']}


@app.post('/api/demo/stop')
async def demo_stop():
    """Cancel demo mode and reset."""
    with state_lock:
        state['demo'] = False
        state['demo_running'] = False
        state['demo_waiting_for_pick'] = False
        state['drafted'] = set()
        state['draft_log'] = []
        state['my_roster'] = []
        state['total_picks'] = 0
        state['current_round'] = 1
        state['next_pick_num'] = 1
        state['next_pick_is_mine'] = False
        state['status'] = 'DEMO RESET. BOARD READY.'
        if state['board']:
            recs = recommend(state['board'], set(), [], state['strategy'], 1)
            state['recommendations'] = recs
    demo_pick_event.set()  # unblock thread if waiting
    demo_stop_event.set()  # wake sleeping opponent loop immediately
    return {'ok': True}


@app.get('/', response_class=HTMLResponse)
def index():
    # Serve index.html from the same folder as this script — portable across machines
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'index.html')
    with open(html_path, encoding='utf-8') as f:
        return f.read()


if __name__ == '__main__':
    t = threading.Thread(target=poll_draft, daemon=True)
    t.start()
    uvicorn.run(app, host='0.0.0.0', port=8888, log_level='warning')
