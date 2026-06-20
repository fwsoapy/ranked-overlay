#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fortnite Ranked OBS Overlay - local server
-------------------------------------------
Polls the OliTracker API every 30 seconds, caches the result, and serves a
live, self-updating overlay that you point an OBS Browser Source at:

    http://localhost:8888/overlay

Other endpoints:
    /data?stats_window=session&record_window=session   JSON the overlay reads
    /debug                                             diagnostics
    /raw                                               raw OliTracker response

Window options: 12h | 24h | session | season

Stats (KD/WR) and Record (W/L) are computed from match_history so ELO
changes are completely decoupled from win/loss counting — correct for
Fortnite Ranked where you can gain ELO on a loss via kills.

Standard library only — no pip install needed. Python 3 required.
"""

import json
import re
import time
import datetime
import threading
import urllib.request
import urllib.error
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# ============================================================================
#  CONFIG  —  edit these if you need to
# ============================================================================
EPIC_USERNAME    = "YourUsername"
EPIC_ACCOUNT_ID  = "your-account-id-here"
API_BASE          = "https://olitracker.com/api"
PORT              = 8888
POLL_SECONDS      = 10          # API poll interval (seconds)
OVERLAY_POLL_MS   = 10000       # browser auto-refresh (milliseconds)

# Which ranked mode to show.  "" = auto (picks the one with a real ELO).
RANKED_MODE_HINT  = ""
# The ranking_id used in match_history to filter ranked matches for stats.
# Leave "" to AUTO-DETECT the mode you play most — recommended, and works for
# any account.  Only set this if you want to force a specific ranking_id.
RANKED_MODE_KEY   = ""
# Try to look up the ELO gap to the next leaderboard position.
ENABLE_NEXT_LOOKUP = True
# ============================================================================

SESSION_START = int(time.time())   # epoch-seconds when this server process started

BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/125.0.0.0 Safari/537.36"),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://olitracker.com/",
}

# ---- shared state ----------------------------------------------------------
_lock = threading.Lock()
STATE = {
    "ok":            False,
    "username":      EPIC_USERNAME,
    "rank_number":   None,
    "rank_label":    None,
    "elo":           None,
    "next_position": None,
    "elo_to_next":   None,
    "session_delta": 0,      # ELO gained / lost since overlay started
    "updated_at":    None,
    "error":         "starting up",
}
_start_elo  = None    # ELO on first successful fetch (baseline for session delta)
_last_raw   = None    # full cached API response (re-used for windowed stats)
_last_modes = []      # ranked-mode candidates (shown in /debug)


# ===========================================================================
#  HTTP + JSON helpers
# ===========================================================================
def _http_get_json(url, timeout=15):
    req = urllib.request.Request(url, headers=BROWSER_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        snippet = raw[:200].replace("\n", " ").replace("\r", " ")
        raise ValueError("response was not JSON (first 200 chars): " + snippet)


def _path_str(path):
    return "/".join(str(p) for p in path).lower()


def _num(v):
    """Return v as int if it is a real number (not bool), else None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(round(v))
    return None


# ===========================================================================
#  Find + choose the ranked Battle Royale block
# ===========================================================================
DIVISION_NAMES = {
    0:  "BRONZE I",    1:  "BRONZE II",    2:  "BRONZE III",
    3:  "SILVER I",    4:  "SILVER II",    5:  "SILVER III",
    6:  "GOLD I",      7:  "GOLD II",      8:  "GOLD III",
    9:  "PLATINUM I", 10:  "PLATINUM II", 11:  "PLATINUM III",
    12: "DIAMOND I",  13:  "DIAMOND II",  14:  "DIAMOND III",
    15: "ELITE I",    16:  "ELITE II",    17:  "ELITE III",
    18: "CHAMPION I", 19:  "CHAMPION II", 20:  "CHAMPION III",
    21: "UNREAL",
}


def division_name(div):
    if div is None:
        return None
    return DIVISION_NAMES.get(div, f"DIVISION {div}")


def _looks_like_ranked_mode(d):
    return (isinstance(d, dict)
            and ("elo" in d or "unreal_placement" in d)
            and ("division" in d or "promotion_progression" in d))


def find_ranked_modes(data):
    """Return [((mode_key,), mode_obj), ...] from the top-level ranked_stats."""
    modes = []
    if isinstance(data, dict):
        rs = data.get("ranked_stats")
        if isinstance(rs, dict):
            for key, obj in rs.items():
                if _looks_like_ranked_mode(obj):
                    modes.append(((key,), obj))
    if modes:
        return modes
    # Fallback: recursive scan, never descending into match_history
    def walk(obj, path):
        if isinstance(obj, dict):
            if _looks_like_ranked_mode(obj):
                modes.append((tuple(path), obj))
            for k, v in obj.items():
                if k == "match_history":
                    continue
                walk(v, path + [str(k)])
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                walk(v, path + [i])
    walk(data, [])
    return modes


def pick_br_mode(modes):
    """Pick the mode to display — prefers real ELO + Unreal placement."""
    if not modes:
        return None, None
    hint = RANKED_MODE_HINT.strip().lower()
    if hint:
        for path, obj in modes:
            if hint in _path_str(path):
                return path, obj
    def rank_key(item):
        _, obj = item
        elo    = _num(obj.get("elo"))
        placed = _num(obj.get("unreal_placement"))
        return (1 if elo    is not None else 0,
                1 if placed is not None else 0,
                elo or 0)
    return max(modes, key=rank_key)


def extract_elo(mode_obj):
    v = _num(mode_obj.get("elo"))
    return v if v is not None else _num(mode_obj.get("promotion_progression"))


def extract_label(mode_obj):
    if _num(mode_obj.get("unreal_placement")) is not None:
        return "UNREAL"
    return division_name(_num(mode_obj.get("division")))


def extract_placement(mode_obj):
    return _num(mode_obj.get("unreal_placement"))


# ===========================================================================
#  Windowed stats — computed from match_history on every /data request
#
#  WHY match_history and not ELO diffs?
#  In Fortnite Ranked you can gain ELO on a loss (kill points) or lose ELO
#  on a win that was uncontested.  ELO delta != W/L.  The match_history
#  block contains explicit wins/matches/kills per session group, so we read
#  those directly and derive KD as kills / (matches - wins) — in BR every
#  non-win ends in exactly one death.
# ===========================================================================
_WINDOW_SECS = {
    "12h": 43200,    # past 12 hours
    "24h": 86400,    # past 24 hours
}
_EMPTY_STATS = {
    "wins": 0, "losses": 0, "kills": 0,
    "matches": 0, "kd": None, "wr": None,
}


def _detect_ranking_id(data):
    """Decide which match_history ranking_id to count games for.

    A manual RANKED_MODE_KEY wins.  Otherwise auto-detect the mode with the
    most games played — for someone grinding one queue this is exactly the
    mode shown on the overlay, and it works for ANY account (no hardcoding)."""
    override = RANKED_MODE_KEY.strip()
    if override:
        return override
    tally = {}
    for day in (data.get("match_history") or []):
        for grp in (day.get("matches") or []):
            rd  = grp.get("ranked_data") or {}
            rid = rd.get("ranking_id")
            if rid:
                tally[rid] = tally.get(rid, 0) + int(grp.get("matches", 0) or 0)
    return max(tally, key=tally.get) if tally else None


def compute_windowed_stats(data, window_spec):
    """
    Compute wins / losses / KD / WR for the given time window.

    window_spec: "12h" | "24h" | "session" | "season"  (plus "lifetime" for /debug)

    Returns a dict with keys: wins, losses, kills, matches, kd, wr
    """
    if data is None:
        return dict(_EMPTY_STATS)

    if window_spec == "season":
        return _seasonal_ranked_stats(data)
    if window_spec == "lifetime":
        return _lifetime_ranked_stats(data)

    now = int(time.time())
    if window_spec == "session":
        cutoff = SESSION_START
    else:
        secs   = _WINDOW_SECS.get(window_spec, 86400)
        cutoff = now - secs

    ranking_id = _detect_ranking_id(data)
    total_wins = total_matches = total_kills = 0

    for day in (data.get("match_history") or []):
        for grp in (day.get("matches") or []):
            if (grp.get("last_modified") or 0) < cutoff:
                continue
            rd = grp.get("ranked_data") or {}
            if ranking_id and rd.get("ranking_id") != ranking_id:
                continue
            total_wins    += int(grp.get("wins",    0) or 0)
            total_matches += int(grp.get("matches", 0) or 0)
            total_kills   += int(grp.get("kills",   0) or 0)

    losses = total_matches - total_wins
    kd = round(total_kills / losses, 2) if losses > 0 else None
    wr = round(total_wins / total_matches * 100, 1) if total_matches > 0 else None

    return {
        "wins": total_wins, "losses": losses,
        "kills": total_kills, "matches": total_matches,
        "kd": kd, "wr": wr,
    }


def _seasonal_ranked_stats(data):
    """Pull current-SEASON ranked KD / WR from the API's pre-aggregated block."""
    try:
        blk     = data["stats"]["seasonal"]["ranked"]["both"]["overall"]
        wins    = int(blk.get("wins",           0) or 0)
        matches = int(blk.get("matches_played", 0) or 0)
        kills   = int(blk.get("kills",          0) or 0)
        losses  = matches - wins
        kd = round(kills / losses, 2) if losses > 0 else None
        wr = round(wins / matches * 100, 1) if matches > 0 else None
        return {
            "wins": wins, "losses": losses,
            "kills": kills, "matches": matches,
            "kd": kd, "wr": wr,
        }
    except Exception:
        return dict(_EMPTY_STATS)


def _lifetime_ranked_stats(data):
    """Pull all-time ranked KD / WR from the API's pre-aggregated lifetime block."""
    try:
        blk     = data["stats"]["lifetime"]["ranked"]["both"]["overall"]
        wins    = int(blk.get("wins",           0) or 0)
        matches = int(blk.get("matches_played", 0) or 0)
        kills   = int(blk.get("kills",          0) or 0)
        losses  = matches - wins
        kd = round(kills / losses, 2) if losses > 0 else None
        wr = round(wins / matches * 100, 1) if matches > 0 else None
        return {
            "wins": wins, "losses": losses,
            "kills": kills, "matches": matches,
            "kd": kd, "wr": wr,
        }
    except Exception:
        return dict(_EMPTY_STATS)


# ===========================================================================
#  Best-effort: ELO gap to the next leaderboard position
# ===========================================================================
class _LeaderboardParser(HTMLParser):
    """Pull (placement, elo) pairs out of olitracker.com leaderboard HTML."""
    def __init__(self):
        super().__init__()
        self.rows  = []
        self._cells = []
        self._in_td = False
        self._in_tr = False

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._in_tr = True
            self._cells = []
        elif tag in ("td", "th") and self._in_tr:
            self._in_td = True

    def handle_endtag(self, tag):
        if tag == "td" and self._in_td:
            self._in_td = False
        elif tag == "tr" and self._in_tr:
            self._in_tr = False
            self._try_row(self._cells)
            self._cells = []

    def handle_data(self, data):
        if self._in_td:
            t = data.strip()
            if t:
                self._cells.append(t)

    def _try_row(self, cells):
        nums = []
        for c in cells:
            clean = re.sub(r"[^\d]", "", c)
            if clean:
                nums.append(int(clean))
        if len(nums) >= 2:
            placement = nums[0]
            elo = max(nums[1:])
            if 1 <= placement <= 50000 and elo > 100:
                self.rows.append({"placement": placement, "elo": elo})


def _scrape_leaderboard_page(page_url):
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/125.0.0.0 Safari/537.36"),
        "Accept":          "text/html,application/xhtml+xml,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer":         "https://olitracker.com/",
    }
    req = urllib.request.Request(page_url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read().decode("utf-8", "replace")
    parser = _LeaderboardParser()
    parser.feed(raw)
    return parser.rows


def _slug_for_mode(path):
    ps = _path_str(path)
    if "reload" in ps and ("zero" in ps or "zb" in ps or "nobuild" in ps):
        return "reload-zb"
    if "reload" in ps:
        return "reload"
    if "zero" in ps or "zb" in ps or "nobuild" in ps:
        return "zero-build"
    return "battle-royale"


def fetch_elo_to_next(target_placement, mode_path):
    """Scrape the HTML leaderboard to find ELO at target_placement. Never raises."""
    if not ENABLE_NEXT_LOOKUP or not target_placement or target_placement < 1:
        return None
    slug = _slug_for_mode(mode_path)
    base_url = f"https://olitracker.com/ranked/{slug}"
    pages_to_try = set()
    page_num = max(1, (target_placement - 1) // 100 + 1)
    pages_to_try.add(page_num)
    if page_num > 1:
        pages_to_try.add(page_num - 1)
    pages_to_try.add(1)

    for page in sorted(pages_to_try):
        url = f"{base_url}?page={page}" if page > 1 else base_url
        try:
            rows = _scrape_leaderboard_page(url)
        except Exception as e:
            print(f"[overlay] leaderboard page {page} fetch failed: {e}")
            continue
        for row in rows:
            if row["placement"] == target_placement:
                return row["elo"]
    return None


# ===========================================================================
#  Polling loop
# ===========================================================================
def _set_error(msg):
    with _lock:
        STATE["error"] = msg
        if STATE.get("elo") is None:
            STATE["ok"] = False
    print("[overlay] " + msg)


def refresh_once():
    global _start_elo, _last_raw, _last_modes
    url = f"{API_BASE}/stats/{EPIC_ACCOUNT_ID}"
    try:
        data = _http_get_json(url)
    except urllib.error.HTTPError as e:
        _set_error(f"HTTP {e.code} from OliTracker (account public? id correct?)")
        return
    except Exception as e:
        _set_error(f"request failed: {e}")
        return

    _last_raw   = data
    modes       = find_ranked_modes(data)
    _last_modes = modes
    if not modes:
        _set_error("no ranked data found in API response — see /raw")
        return

    path, mode = pick_br_mode(modes)
    elo       = extract_elo(mode)
    label     = extract_label(mode)
    placement = extract_placement(mode)

    if elo is None:
        _set_error("found ranked block but no ELO field — see /debug")
        return

    next_pos    = (placement - 1) if (placement and placement > 1) else None
    elo_to_next = None
    if next_pos:
        nxt = fetch_elo_to_next(next_pos, path)
        if nxt is not None and nxt >= elo:
            elo_to_next = nxt - elo

    with _lock:
        if _start_elo is None:
            _start_elo = elo   # establish session baseline on first fetch

        STATE.update({
            "ok":            True,
            "rank_number":   placement,
            "rank_label":    label,
            "elo":           elo,
            "next_position": next_pos,
            "elo_to_next":   elo_to_next,
            "session_delta": elo - _start_elo,
            "updated_at":    int(time.time()),
            "error":         None,
        })

    # ---- simple stats summary, printed after every parse ----
    sess = compute_windowed_stats(data, "session")
    kd_txt = f"{sess['kd']:.2f}" if sess["kd"] is not None else "—"
    wr_txt = f"{sess['wr']:.1f}%" if sess["wr"] is not None else "—"
    if elo_to_next is not None and next_pos:
        gap_txt = f"{elo_to_next} ELO to #{next_pos}"
    elif next_pos:
        gap_txt = f"#{next_pos} (gap n/a)"
    else:
        gap_txt = "top of leaderboard"
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[overlay] {ts}  #{placement} {label}  |  {elo} ELO  |  "
          f"next: {gap_txt}  |  session {elo - _start_elo:+d} ELO")
    print(f"          session record: {sess['wins']}W / {sess['losses']}L"
          f"   ·   KD {kd_txt}   ·   WR {wr_txt}   ({sess['matches']} games)")


def poll_loop():
    refresh_once()          # immediate first fetch
    while True:
        time.sleep(POLL_SECONDS)
        refresh_once()


# ===========================================================================
#  Windowed ELO change (for the small ELO slot on the overlay)
# ===========================================================================
def _elo_series(data, ranking_id):
    """Sorted [(timestamp, elo), ...] from match_history for this ranking_id."""
    pts = []
    for day in (data.get("match_history") or []):
        for grp in (day.get("matches") or []):
            rd = grp.get("ranked_data") or {}
            if ranking_id and rd.get("ranking_id") != ranking_id:
                continue
            e  = _num(rd.get("elo"))
            ts = grp.get("last_modified")
            if e is not None and ts:
                pts.append((int(ts), e))
    pts.sort()
    return pts


def _earliest_day_start_elo(data, ranking_id):
    """The 'start' ELO of the oldest day on record (match_history is newest-first)."""
    for day in reversed(data.get("match_history") or []):
        ent = (day.get("elo") or {}).get(ranking_id) if ranking_id else None
        if isinstance(ent, dict):
            st = _num(ent.get("start"))
            if st is not None:
                return st
    return None


def windowed_elo_delta(data, window, current_elo):
    """ELO change across the past 12h / 24h.  Returns an int, or None."""
    if current_elo is None or data is None:
        return None
    secs = _WINDOW_SECS.get(window)
    if secs is None:
        return None
    cutoff     = int(time.time()) - secs
    ranking_id = _detect_ranking_id(data)
    pts        = _elo_series(data, ranking_id)
    if not pts:
        return None
    # Baseline = the ELO as it stood just before the window began
    baseline = None
    for ts, e in pts:
        if ts <= cutoff:
            baseline = e
        else:
            break
    if baseline is None:                      # whole history is inside the window
        baseline = _earliest_day_start_elo(data, ranking_id)
        if baseline is None:
            baseline = pts[0][1]
    return current_elo - baseline


# ===========================================================================
#  Snapshot — builds display strings for the overlay (accepts window params)
# ===========================================================================
def snapshot(window="session"):
    with _lock:
        s   = dict(STATE)
        raw = _last_raw

    elo       = s.get("elo")
    placement = s.get("rank_number")
    label     = s.get("rank_label") or ""
    delta     = s.get("session_delta", 0) or 0
    nxt       = s.get("next_position")
    gap       = s.get("elo_to_next")

    # One window drives W/L, KD and WR together
    stats = compute_windowed_stats(raw, window)

    s["rank_display"]  = (f"#{placement} {label}".strip() if placement
                          else (label or "—"))
    s["elo_text"]      = (f"{elo} ELO" if elo is not None else "—— ELO")
    s["kd_text"]       = (f"{stats['kd']:.2f}"  if stats["kd"] is not None else "—")
    s["winrate_text"]  = (f"{stats['wr']:.1f}%" if stats["wr"] is not None else "—%")
    s["wl_wins"]       = stats["wins"]
    s["wl_losses"]     = stats["losses"]
    s["stat_matches"]  = stats["matches"]

    # ELO-to-next — expose parts separately for styled rendering
    if nxt and gap is not None:
        s["next_text"] = f"{gap} ELO UNTIL #{nxt}"
        s["next_gap"]  = str(gap)
        s["next_pos"]  = str(nxt)
    elif nxt:
        s["next_text"] = f"ELO UNTIL #{nxt}"
        s["next_gap"]  = None
        s["next_pos"]  = str(nxt)
    else:
        s["next_text"] = "—"
        s["next_gap"]  = None
        s["next_pos"]  = None

    # ELO-change slot — follows the selected window
    if window == "season":
        s["session_text"] = f"+{elo} ALL SEASON" if elo is not None else "— ALL SEASON"
        s["session_sign"] = "pos" if elo is not None else "zero"
    elif window == "12h":
        wd = windowed_elo_delta(raw, "12h", elo)
        if wd is None:
            s["session_text"] = "+0 ELO PAST 12H"
            s["session_sign"] = "zero"
        else:
            sign = "+" if wd >= 0 else ""
            s["session_text"] = f"{sign}{wd} ELO PAST 12H"
            s["session_sign"] = "pos" if wd > 0 else ("neg" if wd < 0 else "zero")
    elif window == "24h":
        wd = windowed_elo_delta(raw, "24h", elo)
        if wd is None:
            s["session_text"] = "+0 ELO PAST 24H"
            s["session_sign"] = "zero"
        else:
            sign = "+" if wd >= 0 else ""
            s["session_text"] = f"{sign}{wd} ELO PAST 24H"
            s["session_sign"] = "pos" if wd > 0 else ("neg" if wd < 0 else "zero")
    else:  # session
        sign = "+" if delta >= 0 else ""
        s["session_text"] = f"{sign}{delta} ELO TODAY"
        s["session_sign"] = "pos" if delta > 0 else ("neg" if delta < 0 else "zero")

    s["session_start"] = SESSION_START
    s["window"]        = window
    return s


def debug_report():
    out = []
    out.append("Fortnite Ranked Overlay - debug")
    out.append(f"username     : {EPIC_USERNAME}")
    out.append(f"account      : {EPIC_ACCOUNT_ID}")
    out.append(f"api          : {API_BASE}/stats/{EPIC_ACCOUNT_ID}")
    out.append(f"session start: "
               f"{datetime.datetime.fromtimestamp(SESSION_START):%Y-%m-%d %H:%M:%S}"
               f" ({SESSION_START})")
    out.append("")
    out.append("CURRENT OVERLAY STATE")
    s = snapshot()
    for k in ("ok", "rank_number", "rank_label", "elo", "session_delta",
              "next_position", "elo_to_next", "updated_at", "error"):
        out.append(f"  {k}: {s.get(k)}")
    out.append("")
    out.append("STATS ranking_id (auto-detected from match_history)")
    out.append(f"  {_detect_ranking_id(_last_raw) if _last_raw else None}")
    out.append("")
    out.append("SESSION WINDOWED STATS (from match_history)")
    sess = compute_windowed_stats(_last_raw, "session")
    for k, v in sess.items():
        out.append(f"  {k}: {v}")
    out.append("")
    out.append("ALL-SEASON RANKED STATS")
    seas = compute_windowed_stats(_last_raw, "season")
    for k, v in seas.items():
        out.append(f"  {k}: {v}")
    out.append("")
    out.append(f"ranked-mode candidates: {len(_last_modes)}")
    for path, obj in _last_modes:
        out.append(
            "  - " + "/".join(map(str, path))
            + f"  div={obj.get('division')} ({division_name(_num(obj.get('division')))})"
            + f"  unreal={obj.get('unreal_placement')}  elo={obj.get('elo')}")
    if _last_modes:
        cp, _ = pick_br_mode(_last_modes)
        out.append("  chosen: " + ("/".join(map(str, cp)) if cp else "None"))
    out.append("")
    out.append("Raw JSON: /raw")
    return "\n".join(out)


# ===========================================================================
#  Overlay HTML
# ===========================================================================
OVERLAY_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Fortnite Rank Overlay</title>
    <link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@700;800;900&display=swap" rel="stylesheet">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }

        body {
            background: transparent;
            font-family: 'Montserrat', sans-serif;
            display: flex;
            flex-direction: column;
            align-items: center;
        }

        /* =========================================================
           OVERLAY CARD  (what OBS sees — size your browser source
           to ~90 px tall so the settings panel below stays hidden)
           ========================================================= */
        .overlay-container {
            display: flex;
            flex-direction: column;
            align-items: stretch;
            background: linear-gradient(180deg, rgba(20,18,26,0.95) 0%, rgba(12,11,16,0.95) 100%);
            padding: 15px 30px;
            box-shadow: 0 8px 24px rgba(0,0,0,0.5);
            border: 1px solid rgba(255,255,255,0.05);
            user-select: none;
        }

        .section { display: flex; flex-direction: column; justify-content: center; }
        .left-section  { align-items: center; padding-right: 25px; }
        .right-section { align-items: center; padding-left:  25px; }

        .divider {
            width: 2px; height: 50px;
            background: linear-gradient(to bottom,
                rgba(255,255,255,0) 0%,
                rgba(255,255,255,0.6) 50%,
                rgba(255,255,255,0) 100%);
            box-shadow: 0 0 4px rgba(255,255,255,0.3);
        }

        .main-text {
            font-weight: 900; font-size: 28px;
            letter-spacing: 0.5px; line-height: 1.1;
            text-transform: uppercase; white-space: nowrap;
        }

        /* Slow rainbow for rank label */
        .unreal-rainbow {
            background: linear-gradient(120deg,
                #3333ff,#9933ff,#ff3333,#ff9933,#ffff33,#33cc33,#3333ff);
            background-size: 800% 100%;
            background-repeat: repeat;
            -webkit-background-clip: text; background-clip: text;
            -webkit-text-fill-color: transparent;
            animation: slowRainbow 45s linear infinite;
        }
        @keyframes slowRainbow {
            0%   { background-position: 800% 0%; }
            100% { background-position: 0% 0%; }
        }

        /* Shimmer for ELO number — thin purple line sweeps across white */
        .elo-container {
            position: relative; display: inline-block; color: #ffffff;
        }
        .elo-container::after {
            content: attr(data-text);
            position: absolute; top: 0; left: 0; width: 100%; height: 100%;
            background: linear-gradient(90deg,
                transparent 40%,#c080ff 47%,#8855ef 50%,#c080ff 53%,transparent 60%)
                no-repeat;
            background-size: 400% 100%; background-position: 100% 0%;
            -webkit-background-clip: text; background-clip: text;
            -webkit-text-fill-color: transparent;
            animation: shimmer 10s ease-in-out infinite;
        }
        @keyframes shimmer {
            0%   { background-position: 100% 0%; }
            40%  { background-position: 0% 0%; }
            100% { background-position: 0% 0%; }
        }

        /* Shimmer for "X ELO UNTIL #xxx" — white base, very thin purple sweep */
        .next-shimmer {
            position: relative; display: inline-block; color: #ffffff;
        }
        .next-shimmer::after {
            content: attr(data-text);
            position: absolute; top: 0; left: 0; width: 100%; height: 100%;
            background: linear-gradient(90deg,
                transparent 43%,#c080ff 48%,#9966ff 50%,#c080ff 52%,transparent 57%)
                no-repeat;
            background-size: 500% 100%; background-position: 100% 0%;
            -webkit-background-clip: text; background-clip: text;
            -webkit-text-fill-color: transparent;
            animation: shimmerNext 14s ease-in-out infinite;
        }
        @keyframes shimmerNext {
            0%   { background-position: 100% 0%; }
            35%  { background-position: 0% 0%; }
            100% { background-position: 0% 0%; }
        }

        .sub-text {
            font-weight: 800; font-size: 16px;
            letter-spacing: 0.5px; margin-top: 4px;
            text-transform: uppercase; white-space: nowrap; text-align: center;
        }

        /* Left sub: ELO to next rank */
        .left-sub { color: #a39cb5; }

        /* Next rank sub-parts */
        .next-gap-val { font-size: 16px; font-weight: 800; color: #ffffff;
                        letter-spacing: 0.5px; text-transform: uppercase; white-space: nowrap; }
        .next-to   { font-size: 11px; font-weight: 700; color: #b8b8c4;
                     letter-spacing: 0.5px; text-transform: uppercase; }
        .next-hash { font-size: 16px; font-weight: 800; color: #b066fe;
                     letter-spacing: 0.5px; text-transform: uppercase; white-space: nowrap; }

        /* Right sub: single ELO delta line — same font as .sub-text */
        .right-sub {
            display: flex; flex-direction: column;
            align-items: center; gap: 2px;
            margin-top: 4px;
            font-family: 'Montserrat', sans-serif;
            font-weight: 800; font-size: 16px;
            letter-spacing: 0.5px;
            text-transform: uppercase; white-space: nowrap;
        }

        /* Bottom centered code-ad row (spans full card width) */
        .bottom-row {
            width: 100%;
            text-align: center;
            margin-top: 8px;
            font-weight: 800; font-size: 16px;
            letter-spacing: 0.5px;
            text-transform: uppercase; white-space: nowrap;
        }
        .code-ad { color: #ffffff; font-style: italic; }

        .wl-wins   { color: #2ed573; text-shadow: 0 0 10px rgba(46,213,115,0.3); }
        .wl-sep    { color: #ffffff; margin: 0 2px; }
        .wl-losses { color: #ff4757; text-shadow: 0 0 10px rgba(255,71,87,0.3); }
        .row-divider { color: rgba(255,255,255,0.3); font-weight: 400; font-size: 14px; }

        .session-pos  { color: #2ed573; text-shadow: 0 0 10px rgba(46,213,115,0.2); }
        .session-neg  { color: #ff4757; text-shadow: 0 0 10px rgba(255,71,87,0.2); }
        .session-zero { color: #ffffff; }

        /* =========================================================
           CONTROL BUTTONS  (browser only — hidden by OBS crop)
           ========================================================= */
        .controls-panel {
            margin-top: 100px;        /* 100px below the overlay */
            text-align: center;
        }

        .ctrl-obs-note {
            display: block;
            color: #6f6f7a;           /* readable reminder on a light page */
            font-size: 11px; font-weight: 700;
            letter-spacing: 0.5px; text-transform: uppercase;
            margin-bottom: 16px;
        }

        /* buttons centered, spaced apart, plain text (no background) */
        .btn-row {
            display: flex; align-items: center; justify-content: center;
            gap: 30px; flex-wrap: wrap;
        }

        .ctrl-btn {
            background: none;
            border: none;
            padding: 4px 2px;
            color: #3a3340;           /* dark — clearly visible on the page */
            font-family: 'Montserrat', sans-serif;
            font-weight: 800; font-size: 15px;
            letter-spacing: 0.5px; text-transform: uppercase;
            cursor: pointer; outline: none;
            transition: color 0.12s ease;
        }
        .ctrl-btn:hover  { color: #000000; }
        .ctrl-btn.active { color: #8b3ff2; }   /* vivid purple = selected */
    </style>
</head>
<body>

    <!-- ===== OVERLAY CARD (OBS browser source points here) ===== -->
    <div class="overlay-container" style="flex-direction: column; align-items: stretch;">
        <div style="display:flex; align-items:center;">
            <div class="section left-section">
                <div class="main-text unreal-rainbow" id="rankText">#— UNREAL</div>
                <div class="sub-text left-sub">
                    <span id="nextContainer" style="display:inline-flex;align-items:center;gap:5px;">
                        <span class="next-gap-val" id="nextGap">—</span>
                        <span class="next-to">TO</span>
                        <span class="next-hash" id="nextPos">#—</span>
                    </span>
                </div>
            </div>
            <div class="divider"></div>
            <div class="section right-section">
                <div class="main-text elo-container" id="eloText" data-text="—— ELO">—— ELO</div>
                <div class="right-sub">
                    <span class="session-zero" id="sessionText">+0 ELO TODAY</span>
                </div>
            </div>
        </div>
        <div class="bottom-row">
            <span class="code-ad">Use Code YourCode :) #ad</span>
        </div>
    </div>

    <!-- ===== CONTROL BUTTONS (open in your browser; OBS won't see this) ===== -->
    <div class="controls-panel">
        <span class="ctrl-obs-note">&#128250; OBS: set browser source height ~90 px to hide these buttons</span>

        <div class="btn-row" id="windowButtons">
            <button class="ctrl-btn" data-window="12h">Past 12 Hours</button>
            <button class="ctrl-btn" data-window="24h">Past 24 Hours</button>
            <button class="ctrl-btn" data-window="session">Session</button>
            <button class="ctrl-btn" data-window="season">All Season</button>
        </div>
    </div>

    <script>
        var POLL_MS = __POLL_MS__;

        // Persist chosen window across page reloads
        var curWin = localStorage.getItem('fn_ov_window') || 'session';

        function $(s) { return document.querySelector(s); }

        // ---- highlight the active button ----
        function syncUI() {
            document.querySelectorAll('#windowButtons .ctrl-btn').forEach(function(b) {
                b.classList.toggle('active', b.dataset.window === curWin);
            });
        }

        // ---- fetch data and update overlay ----
        function tick() {
            fetch('/data?window=' + curWin, { cache: 'no-store' })
                .then(function(r) { return r.json(); })
                .then(function(d) {
                    if (!d || (d.ok === false && !d.elo_text)) return;

                    $('#rankText').textContent = d.rank_display || '#— UNREAL';

                    var eloEl  = $('#eloText');
                    var eloStr = d.elo_text || '—— ELO';
                    eloEl.textContent = eloStr;
                    eloEl.setAttribute('data-text', eloStr);

                    // ELO to next rank — update three styled parts
                    var gapEl  = $('#nextGap');
                    var posEl  = $('#nextPos');
                    var gapStr = d.next_gap ? d.next_gap + ' ELO' : '—';
                    var posStr = d.next_pos ? '#' + d.next_pos : '#—';
                    gapEl.textContent = gapStr;
                    posEl.textContent = posStr;

                    // Window-driven ELO delta (right sub)
                    var sessEl = $('#sessionText');
                    sessEl.textContent = d.session_text || '+0 ELO TODAY';
                    sessEl.className   = 'session-' + (d.session_sign || 'zero');
                })
                .catch(function() { /* keep last values on a network blip */ });
        }

        // ---- button clicks ----
        document.querySelectorAll('#windowButtons .ctrl-btn').forEach(function(btn) {
            btn.addEventListener('click', function() {
                curWin = this.dataset.window;
                localStorage.setItem('fn_ov_window', curWin);
                syncUI();
                tick();
            });
        });

        // ---- init ----
        syncUI();
        tick();
        setInterval(function() { tick(); }, POLL_MS);
    </script>
</body>
</html>"""


# ===========================================================================
#  Tiny web server
# ===========================================================================
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # keep the console quiet

    def _send(self, code, body, ctype):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip("/")
        params = parse_qs(parsed.query)

        def _p(key, default=""):
            return (params.get(key, [default]) or [default])[0]

        if path in ("", "/overlay"):
            html = OVERLAY_HTML.replace("__POLL_MS__", str(OVERLAY_POLL_MS))
            self._send(200, html, "text/html; charset=utf-8")

        elif path == "/data":
            w = _p("window", _p("stats_window", "session"))
            self._send(200, json.dumps(snapshot(w)), "application/json")

        elif path == "/raw":
            body = json.dumps(_last_raw, indent=2, default=str) if _last_raw else "{}"
            self._send(200, body, "application/json")

        elif path == "/debug":
            self._send(200, debug_report(), "text/plain; charset=utf-8")

        else:
            self._send(404, "not found", "text/plain")


# ===========================================================================
#  Entry point
# ===========================================================================
def main():
    bar = "=" * 62
    print(bar)
    print(f"  Fortnite Ranked Overlay — port {PORT}")
    print(bar)
    print(f"  OBS Browser Source :  http://localhost:{PORT}/overlay")
    print(f"  Settings panel     :  open the same URL in your browser")
    print(f"  Data / debug / raw :  /data  /debug  /raw")
    print(f"  Polling OliTracker every {POLL_SECONDS} s")
    print(bar)
    print("  Leave this window open while streaming.")
    print("  Press Ctrl+C to stop.\n")

    threading.Thread(target=poll_loop, daemon=True).start()

    # Auto-open browser after 3 s
    import subprocess, sys
    def _open():
        time.sleep(3)
        url = f"http://localhost:{PORT}/overlay"
        try:
            if sys.platform == "win32":
                subprocess.Popen(["cmd", "/c", "start", "", url],
                                 shell=False, creationflags=0x08000000)
        except Exception:
            pass
    threading.Thread(target=_open, daemon=True).start()

    try:
        server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    except OSError as e:
        print(f"\n  Could not start on port {PORT}: {e}")
        print("  Another program may be using that port.")
        print("  Change PORT at the top of server.py and try again.")
        input("\n  Press Enter to close...")
        return

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopping overlay. Bye!")


if __name__ == "__main__":
    main()
