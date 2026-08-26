import json
import os
import re
import tempfile
import time
import datetime
import threading
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

EPIC_USERNAME    = "YourUsername"
EPIC_ACCOUNT_ID  = "your-account-id-here"
CREATOR_CODE     = ""
API_BASE         = "https://olitracker.com/api"
PORT             = 8888
POLL_SECONDS     = 30      # how often the server asks OliTracker for new data
OVERLAY_POLL_MS  = 30000   # how often the browser page asks the server

RANKED_MODE_HINT   = ""
RANKED_MODE_KEY    = ""
ENABLE_NEXT_LOOKUP = True

SESSION_START = int(time.time())

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://olitracker.com/",
}

_lock               = threading.Lock()
_start_elos         = {}
_start_progressions = {}
_last_raw           = None
_last_modes         = []
_baseline_day       = None

# mode key -> {"elo", "next_pos", "gap"}. Filled by the poll loop for every
# mode, so snapshot() can answer for whichever mode the overlay asks about
# without doing a leaderboard fetch on the request path.
_elo_lookup         = {}

_lb_lock            = threading.Lock()
_lb_cache           = {}   # (slug, page) -> (fetched_at, rows)

STATE = {
    "ok":              False,
    "username":        EPIC_USERNAME,
    "rank_number":     None,
    "rank_label":      None,
    "elo":             None,
    "is_unreal":       False,
    "next_position":   None,
    "elo_to_next":     None,
    "session_delta":   0,
    "prog_delta":      0,
    "updated_at":      None,
    "error":           "starting up",
    "active_mode_key": "",
    "modes_available": [],
}

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

MODE_LABELS = {
    "ranked-br-combined":      "BR",
    "ranked_blastberry_build": "Reload",
    "ranked_squareclub":       "Boxfights",
    "ranked-squareclub":       "Boxfights",
    "ranked-rocket-racing":    "Racing",
    "delmar-competitive":      "Blitz",
}

# Which stats bucket belongs to which ranked mode. `None` means OliTracker
# publishes no bucket for that mode at all, so its stats come from
# match_history instead. Keys are normalised, so - and _ spellings both hit.
MODE_STAT_PATH = {
    "ranked-br-combined":      ("ranked",),
    "ranked-blastberry-build": ("reload",),
    "ranked-squareclub":       None,   # no bucket of its own, counted from history
    "delmar-competitive":      None,
    "ranked-rocket-racing":    None,
}

# OliTracker's public leaderboard stops at page 100 (100 rows a page). Past
# that there is no ELO to read for a placement, at any price.
LEADERBOARD_PAGE_SIZE     = 100
LEADERBOARD_MAX_PAGE      = 100
LEADERBOARD_MAX_PLACEMENT = LEADERBOARD_PAGE_SIZE * LEADERBOARD_MAX_PAGE
LEADERBOARD_TTL           = 60    # seconds; keeps pace with POLL_SECONDS
LEADERBOARD_PAGES_BACK    = 4     # pages to walk up through an ELO tie

def _state_dir():
    """Where the daily ELO baseline is kept.

    Deliberately not the overlay folder -- that stays as the four files it
    ships with, so nothing extra shows up next to server.py.
    """
    base = (
        os.environ.get("LOCALAPPDATA")
        or os.environ.get("APPDATA")
        or tempfile.gettempdir()
    )
    return os.path.join(base, "FortniteRankOverlay")


BASELINE_FILE = os.path.join(_state_dir(), "elo_baseline.json")

_WINDOW_SECS = {"12h": 43200, "24h": 86400}

_EMPTY_STATS = {
    "wins": 0, "losses": 0, "kills": 0,
    "matches": 0, "kd": None, "wr": None,
}


def _http_get_json(url, timeout=15):
    req = urllib.request.Request(url, headers=BROWSER_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        snippet = raw[:200].replace("\n", " ").replace("\r", " ")
        raise ValueError("response was not JSON: " + snippet)


def _path_str(path):
    return "/".join(str(p) for p in path).lower()


def _normalize_mode_key(key):
    """OliTracker spells mode keys with both - and _; fold them together."""
    return str(key or "").lower().replace("_", "-")


def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(round(v))
    return None


def _unreal_placement(obj):
    """Current Unreal leaderboard rank.

    OliTracker calls this field `current_unreal_placement`; older responses
    used `unreal_placement`, so accept either.
    """
    if not isinstance(obj, dict):
        return None
    v = _num(obj.get("current_unreal_placement"))
    return v if v is not None else _num(obj.get("unreal_placement"))


def division_name(div):
    if div is None:
        return None
    return DIVISION_NAMES.get(div, f"DIVISION {div}")


def _mode_label(key):
    if key in MODE_LABELS:
        return MODE_LABELS[key]
    lower = key.lower()
    if "squareclub" in lower or "boxfight" in lower:
        return "Boxfights"
    if "blastberry" in lower or "reload" in lower:
        return "Reload"
    if "br-combined" in lower or "br_combined" in lower:
        return "BR"
    return key


def _looks_like_ranked_mode(d):
    return (
        isinstance(d, dict)
        and ("elo" in d or "current_unreal_placement" in d or "unreal_placement" in d)
        and ("division" in d or "promotion_progression" in d)
    )


def find_ranked_modes(data):
    modes = []
    if isinstance(data, dict):
        rs = data.get("ranked_stats")
        if isinstance(rs, dict):
            for key, obj in rs.items():
                if _looks_like_ranked_mode(obj):
                    modes.append(((key,), obj))
    if modes:
        return modes

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


def pick_mode_by_key(modes, mode_key):
    for path, obj in modes:
        if _path_str(path) == mode_key.lower():
            return path, obj
    return None, None


def _mode_rank_key(item):
    """Sort key for "which mode should the overlay lead with" (higher = better).

    The headline number on the overlay is the ELO, so a mode whose ELO can
    actually be resolved outranks one that only has a placement. Ordering by
    raw dict order used to hand the default to Reload -- placement #562k, no
    published ELO -- which is why the overlay opened with an empty ELO row.
    """
    path, obj = item
    key = _path_str(path)
    with _lock:
        resolved = (_elo_lookup.get(key) or {}).get("elo")
    elo = _num(obj.get("elo"))
    if elo is None:
        elo = resolved
    placed = _unreal_placement(obj)
    div    = _num(obj.get("division")) or 0
    return (
        1 if elo is not None else 0,
        div,
        -placed if placed is not None else 0,   # better (lower) placement wins
        elo or 0,
    )


def pick_best_mode(modes):
    if not modes:
        return None, None
    hint = RANKED_MODE_HINT.strip().lower()
    if hint:
        for path, obj in modes:
            if hint in _path_str(path):
                return path, obj
    return max(modes, key=_mode_rank_key)


def extract_elo(mode_obj):
    """The mode's real ELO from the stats endpoint, or None.

    OliTracker returns `elo: null` here for this account on every mode, so an
    Unreal mode's ELO is filled in from the leaderboard instead (mode_view).
    Below Unreal there is no ELO at all -- promotion_progression is a
    percentage and is reported on its own, never dressed up as an ELO score.
    """
    return _num(mode_obj.get("elo"))


def extract_label(mode_obj):
    if _unreal_placement(mode_obj) is not None:
        return "UNREAL"
    return division_name(_num(mode_obj.get("division")))


def extract_placement(mode_obj):
    return _unreal_placement(mode_obj)


def _progress_points(mode_obj):
    """Rank progress on a continuous scale (division * 100 + progression).

    promotion_progression restarts at ~0 on promotion, so comparing it against
    a baseline taken in the previous division reads a rank-up as a big loss.
    Folding the division in keeps the number monotonic across the boundary.
    Returns None for Unreal, which is measured in ELO instead.
    """
    if _unreal_placement(mode_obj) is not None:
        return None
    prog = _num(mode_obj.get("promotion_progression"))
    if prog is None:
        return None
    div = _num(mode_obj.get("division")) or 0
    return div * 100 + prog


def extract_progression(mode_obj):
    if _unreal_placement(mode_obj) is not None:
        return None
    return _num(mode_obj.get("promotion_progression"))


def _detect_ranking_id(data, preferred_mode_key=None):
    if preferred_mode_key:
        return preferred_mode_key
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


def _mode_stat_path(ranking_id):
    if not ranking_id:
        return ("ranked",)
    key = _normalize_mode_key(ranking_id)
    if key in MODE_STAT_PATH:
        return MODE_STAT_PATH[key]
    label = _mode_label(ranking_id)
    if label == "Reload":
        return ("reload",)
    if label == "BR":
        return ("ranked",)
    return None


def _stat_block(data, timeframe, ranking_id):
    """The stats bucket for exactly one ranked mode, or None.

    This must never fall back to another mode's bucket. OliTracker only emits a
    `reload` bucket once the account has played Reload inside that timeframe,
    and quietly borrowing `ranked` when it was missing is what made BR and
    Reload report identical KD/WR/kills/wins.
    """
    path_key = _mode_stat_path(ranking_id)
    if path_key is None:
        return None
    try:
        block = data["stats"][timeframe]
        for k in path_key:
            block = block[k]
        return block["both"]["overall"]
    except (KeyError, TypeError):
        return None


def _stats_from_history(data, ranking_id):
    total_wins = total_matches = total_kills = 0
    for day in (data.get("match_history") or []):
        for grp in (day.get("matches") or []):
            rd = grp.get("ranked_data") or {}
            if _normalize_mode_key(rd.get("ranking_id")) != _normalize_mode_key(ranking_id):
                continue
            total_wins    += int(grp.get("wins",    0) or 0)
            total_matches += int(grp.get("matches", 0) or 0)
            total_kills   += int(grp.get("kills",   0) or 0)
    losses = total_matches - total_wins
    kd = round(total_kills / losses, 2) if losses > 0 else None
    wr = round(total_wins / total_matches * 100, 1) if total_matches > 0 else None
    return {"wins": total_wins, "losses": losses, "kills": total_kills, "matches": total_matches, "kd": kd, "wr": wr}


def _seasonal_ranked_stats(data, ranking_id=None):
    try:
        blk = _stat_block(data, "seasonal", ranking_id)
        if blk is None:
            # No seasonal bucket for this mode: either the mode has none at all
            # (Boxfights) or the account has not played it this season. Count
            # its own matches instead of borrowing another mode's totals.
            return _stats_from_history(data, ranking_id) if ranking_id else dict(_EMPTY_STATS)
        wins    = int(blk.get("wins",           0) or 0)
        matches = int(blk.get("matches_played", 0) or 0)
        kills   = int(blk.get("kills",          0) or 0)
        losses  = matches - wins
        kd = round(kills / losses, 2) if losses > 0 else None
        wr = round(wins / matches * 100, 1) if matches > 0 else None
        return {"wins": wins, "losses": losses, "kills": kills, "matches": matches, "kd": kd, "wr": wr}
    except Exception:
        return dict(_EMPTY_STATS)


def _lifetime_ranked_stats(data, ranking_id=None):
    try:
        blk = _stat_block(data, "lifetime", ranking_id)
        if blk is None:
            return dict(_EMPTY_STATS)
        wins    = int(blk.get("wins",           0) or 0)
        matches = int(blk.get("matches_played", 0) or 0)
        kills   = int(blk.get("kills",          0) or 0)
        losses  = matches - wins
        kd = round(kills / losses, 2) if losses > 0 else None
        wr = round(wins / matches * 100, 1) if matches > 0 else None
        return {"wins": wins, "losses": losses, "kills": kills, "matches": matches, "kd": kd, "wr": wr}
    except Exception:
        return dict(_EMPTY_STATS)


def compute_windowed_stats(data, window_spec, ranking_id=None):
    if data is None:
        return dict(_EMPTY_STATS)
    if window_spec == "season":
        return _seasonal_ranked_stats(data, ranking_id)
    if window_spec == "lifetime":
        return _lifetime_ranked_stats(data, ranking_id)

    now = int(time.time())
    cutoff = SESSION_START if window_spec == "session" else now - _WINDOW_SECS.get(window_spec, 86400)

    rid = _detect_ranking_id(data, ranking_id)
    total_wins = total_matches = total_kills = 0

    for day in (data.get("match_history") or []):
        for grp in (day.get("matches") or []):
            if (grp.get("last_modified") or 0) < cutoff:
                continue
            rd = grp.get("ranked_data") or {}
            if rid and rd.get("ranking_id") != rid:
                continue
            total_wins    += int(grp.get("wins",    0) or 0)
            total_matches += int(grp.get("matches", 0) or 0)
            total_kills   += int(grp.get("kills",   0) or 0)

    losses = total_matches - total_wins
    kd = round(total_kills / losses, 2) if losses > 0 else None
    wr = round(total_wins / total_matches * 100, 1) if total_matches > 0 else None
    return {"wins": total_wins, "losses": losses, "kills": total_kills, "matches": total_matches, "kd": kd, "wr": wr}


# One leaderboard row: rank cell, the player's /stats/<account id> link, and
# the ELO cell. Anchoring on the cell classes matters -- the old parser took
# "the largest number in the row", which happily read digits out of player
# names ("Twitch 666k2b" scored 6662 ELO).
_LB_ROW  = re.compile(r"<tr[^>]*leaderboard-player-row.*?</tr>", re.S)
_LB_RANK = re.compile(r"leaderboard-rank-cell[^\"]*\"[^>]*>\s*#?([\d,]+)", re.S)
_LB_ACCT = re.compile(r"/stats/([0-9a-fA-F]{16,})")
_LB_ELO  = re.compile(r"leaderboard-wins-cell.*?<p[^>]*>\s*([\d,]+)\s*</p>", re.S)


def _int(text):
    try:
        return int(str(text).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _parse_leaderboard_rows(html):
    rows = []
    for chunk in _LB_ROW.findall(html):
        rank = _LB_RANK.search(chunk)
        elo  = _LB_ELO.search(chunk)
        if not rank or not elo:
            continue
        placement = _int(rank.group(1))
        elo_value = _int(elo.group(1))
        if placement is None or elo_value is None:
            continue
        acct = _LB_ACCT.search(chunk)
        rows.append({
            "placement":  placement,
            "elo":        elo_value,
            "account_id": acct.group(1).lower() if acct else None,
        })
    rows.sort(key=lambda r: r["placement"])
    return rows


def _http_get_text(url, timeout=15):
    headers = dict(BROWSER_HEADERS)
    headers["Accept"] = "text/html,application/xhtml+xml,*/*"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _leaderboard_page(slug, page):
    """Rows of one leaderboard page, cached for LEADERBOARD_TTL seconds.

    The poll loop runs every POLL_SECONDS; without the cache that would mean
    re-downloading a ~180KB page from OliTracker several times a minute.
    """
    if page < 1 or page > LEADERBOARD_MAX_PAGE:
        return []
    key = (slug, page)
    now = time.time()
    with _lb_lock:
        hit = _lb_cache.get(key)
        if hit and now - hit[0] < LEADERBOARD_TTL:
            return hit[1]

    url = f"https://olitracker.com/ranked/{slug}"
    if page > 1:
        url += f"?page={page}"
    try:
        rows = _parse_leaderboard_rows(_http_get_text(url))
    except Exception as e:
        print(f"[overlay] leaderboard {slug} page {page} failed: {e}")
        # Serve the stale copy rather than blanking the ELO on one bad fetch.
        return hit[1] if hit else []

    with _lb_lock:
        _lb_cache[key] = (now, rows)
    return rows


def _slug_for_mode(path):
    """Which OliTracker leaderboard page carries this mode's ELO, if any.

    Unknown modes return None on purpose. Guessing "battle-royale" for anything
    unrecognised would read a completely unrelated player's ELO off the wrong
    board, which is worse than showing no ELO at all.
    """
    ps = _path_str(path)
    if "squareclub" in ps or "boxfight" in ps:
        return None   # OliTracker publishes no Boxfights leaderboard
    if "reload" in ps or "blastberry" in ps:
        if "zero" in ps or "zb" in ps or "nobuild" in ps:
            return "reload-zb"
        return "reload"
    if "rocket" in ps or "racing" in ps:
        return "rocket-racing"
    if "og" in ps.split("-") or "og" in ps.split("_"):
        return "og"
    if "zero" in ps or "zb" in ps or "nobuild" in ps:
        return "zero-build"
    if "br-combined" in ps or "br_combined" in ps or "battle" in ps:
        return "battle-royale"
    return None


def _find_own_row(slug, placement, account_id):
    """The leaderboard row to read this account's ELO from.

    Thousands of players tie on the same score up here, and the board reshuffles
    within a tie block between renders, so the account's own row is not
    dependably on the page its placement points at. The row *at* the placement
    OliTracker reports is the stable answer -- an account sitting at placement N
    has, by definition, the ELO shown at placement N. Matching the account id is
    kept as a refinement for when it does land on the page.
    """
    page = (placement - 1) // LEADERBOARD_PAGE_SIZE + 1
    rows = _leaderboard_page(slug, page)
    if not rows:
        return page, rows, None

    acct = (account_id or "").lower()
    if acct:
        for row in rows:
            if row["account_id"] == acct:
                return page, rows, row

    for row in rows:
        if row["placement"] == placement:
            return page, rows, row

    nearest = min(rows, key=lambda r: abs(r["placement"] - placement))
    return page, rows, nearest


def lookup_leaderboard_elo(mode_path, placement, account_id=EPIC_ACCOUNT_ID):
    """(own ELO, target placement, ELO gap) for an Unreal mode.

    The stats endpoint reports `elo: null` for this account on every mode, so
    the leaderboard is the only place the number exists. The target is the
    nearest placement above that actually sits on a *higher* ELO: hundreds of
    players tie on the same score, and "0 ELO to #9413" tells a streamer
    nothing.
    """
    if not ENABLE_NEXT_LOOKUP or not placement or placement < 1:
        return None, None, None
    slug = _slug_for_mode(mode_path)
    if slug is None or placement > LEADERBOARD_MAX_PLACEMENT:
        # Outside the published top 10,000 there is no row to read.
        return None, None, None

    page, rows, me = _find_own_row(slug, placement, account_id)
    if me is None:
        return None, None, None

    my_elo, my_place = me["elo"], me["placement"]

    target = None
    for step in range(LEADERBOARD_PAGES_BACK):
        above = [r for r in rows if r["placement"] < my_place and r["elo"] > my_elo]
        if above:
            target = max(above, key=lambda r: r["placement"])
            break
        if page - 1 < 1 or step == LEADERBOARD_PAGES_BACK - 1:
            break
        page -= 1
        rows = _leaderboard_page(slug, page)
        if not rows:
            break

    if target is None:
        return my_elo, None, None
    return my_elo, target["placement"], target["elo"] - my_elo


def _today_key():
    return datetime.date.today().isoformat()


def _load_baselines():
    """Restore today's starting ELO so "TODAY" survives an overlay restart."""
    global _baseline_day
    try:
        with open(BASELINE_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return
    if not isinstance(saved, dict) or saved.get("day") != _today_key():
        return   # yesterday's numbers; today starts from scratch
    with _lock:
        _baseline_day = saved["day"]
        for key, value in (saved.get("elos") or {}).items():
            if _num(value) is not None:
                _start_elos[key] = _num(value)
        for key, value in (saved.get("progressions") or {}).items():
            if _num(value) is not None:
                _start_progressions[key] = _num(value)
    print(f"[overlay] restored today's baseline from {BASELINE_FILE}")


def _save_baselines():
    with _lock:
        payload = {
            "day":          _baseline_day,
            "elos":         dict(_start_elos),
            "progressions": dict(_start_progressions),
        }
    try:
        os.makedirs(os.path.dirname(BASELINE_FILE), exist_ok=True)
        with open(BASELINE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except OSError as e:
        print(f"[overlay] could not save today's baseline: {e}")


def _roll_day_if_needed():
    global _baseline_day
    today = _today_key()
    with _lock:
        if _baseline_day == today:
            return
        _baseline_day = today
        _start_elos.clear()
        _start_progressions.clear()
    print(f"[overlay] new day ({today}) - today's counters reset")


def _record_baseline(mode_key, elo, points):
    with _lock:
        if elo is not None and mode_key not in _start_elos:
            _start_elos[mode_key] = elo
        if points is not None and mode_key not in _start_progressions:
            _start_progressions[mode_key] = points


def _day_elo_change(data, mode_key):
    """Today's ELO swing straight from OliTracker, if it publishes one.

    Each match_history day carries an `elo` block shaped
    {"<mode>": {"start": .., "end": .., "change": ..}}. When it is there it
    beats anything measured locally, because it covers the whole day rather
    than only the stretch since the overlay was started.
    """
    if not isinstance(data, dict):
        return None
    days = data.get("match_history") or []
    if not days:
        return None
    today = _today_key()
    want  = _normalize_mode_key(mode_key)
    for day in days:
        date = str(day.get("date") or "")[:10]
        if date and date != today:
            continue
        block = day.get("elo") or {}
        if not isinstance(block, dict):
            continue
        for key, value in block.items():
            if _normalize_mode_key(key) != want or not isinstance(value, dict):
                continue
            change = _num(value.get("change"))
            if change is not None:
                return change
            start, end = _num(value.get("start")), _num(value.get("end"))
            if start is not None and end is not None:
                return end - start
        break   # only the newest day is "today"
    return None


def _deltas(mode_key, elo, points, data=None):
    published = _day_elo_change(data, mode_key)
    with _lock:
        start_elo  = _start_elos.get(mode_key)
        start_prog = _start_progressions.get(mode_key)
    if published is not None:
        elo_delta = published
    elif elo is not None and start_elo is not None:
        elo_delta = elo - start_elo
    else:
        elo_delta = 0
    prog_delta = (points - start_prog) if (points is not None and start_prog is not None) else 0
    return elo_delta, prog_delta


def _elo_series(data, ranking_id):
    """Every (timestamp, ELO) point OliTracker records for one mode."""
    points = []
    want   = _normalize_mode_key(ranking_id) if ranking_id else None
    for day in ((data or {}).get("match_history") or []):
        for grp in (day.get("matches") or []):
            rd = grp.get("ranked_data") or {}
            if want and _normalize_mode_key(rd.get("ranking_id")) != want:
                continue
            elo = _num(rd.get("elo"))
            ts  = grp.get("last_modified")
            if elo is not None and ts:
                points.append((int(ts), elo))
    points.sort()
    return points


def _earliest_day_start_elo(data, ranking_id):
    want = _normalize_mode_key(ranking_id) if ranking_id else None
    for day in reversed((data or {}).get("match_history") or []):
        for key, value in (day.get("elo") or {}).items():
            if want and _normalize_mode_key(key) != want:
                continue
            start = _num((value or {}).get("start"))
            if start is not None:
                return start
    return None


def windowed_elo_delta(data, window, current_elo, ranking_id=None):
    """ELO gained over the last 12h/24h, or None when history is too thin."""
    if current_elo is None or data is None:
        return None
    secs = _WINDOW_SECS.get(window)
    if secs is None:
        return None
    cutoff = int(time.time()) - secs
    rid    = ranking_id or _detect_ranking_id(data)
    points = _elo_series(data, rid)
    if not points:
        return None
    baseline = None
    for ts, elo in points:
        if ts <= cutoff:
            baseline = elo
        else:
            break
    if baseline is None:
        baseline = _earliest_day_start_elo(data, rid)
        if baseline is None:
            baseline = points[0][1]
    return current_elo - baseline


def mode_view(path, mode_obj):
    """Everything the overlay shows for one ranked mode, resolved in one place.

    refresh_once() and snapshot() used to each derive this themselves, which is
    why the ELO gap only ever appeared for the mode refresh_once happened to
    pick -- switching modes in the overlay dropped it.
    """
    key       = _path_str(path) if path else ""
    label     = extract_label(mode_obj)
    placement = extract_placement(mode_obj)
    is_unreal = (label == "UNREAL")

    with _lock:
        cached = dict(_elo_lookup.get(key) or {})

    elo = extract_elo(mode_obj)
    if elo is None and is_unreal:
        elo = cached.get("elo")

    return {
        "key":         key,
        "label":       label,
        "placement":   placement,
        "progression": extract_progression(mode_obj),
        "is_unreal":   is_unreal,
        "elo":         elo,
        "next_pos":    cached.get("next_pos"),
        "next_gap":    cached.get("gap"),
        "points":      _progress_points(mode_obj),
    }


def _set_error(msg):
    with _lock:
        STATE["error"] = msg
        if STATE.get("elo") is None:
            STATE["ok"] = False
    print("[overlay] " + msg)


def refresh_once():
    global _last_raw, _last_modes
    url = f"{API_BASE}/stats/{EPIC_ACCOUNT_ID}"
    try:
        data = _http_get_json(url)
    except urllib.error.HTTPError as e:
        _set_error(f"HTTP {e.code} from OliTracker")
        return
    except Exception as e:
        _set_error(f"request failed: {e}")
        return

    _last_raw   = data
    modes       = find_ranked_modes(data)
    _last_modes = modes

    if not modes:
        _set_error("no ranked data found")
        return

    _roll_day_if_needed()

    # Resolve ELO for every mode, not just the active one. The overlay's mode
    # buttons can select a mode pick_best_mode() didn't choose, and snapshot()
    # reads these results instead of scraping on the request path.
    for mpath, mobj in modes:
        mkey      = _path_str(mpath)
        api_elo   = extract_elo(mobj)
        placement = extract_placement(mobj)
        lb_elo = next_pos = gap = None
        if placement is not None:
            lb_elo, next_pos, gap = lookup_leaderboard_elo(mpath, placement)
        with _lock:
            _elo_lookup[mkey] = {
                "elo":      api_elo if api_elo is not None else lb_elo,
                "next_pos": next_pos,
                "gap":      gap,
            }

    # Best mode first, so the button order matches the mode the overlay opens on.
    modes_available = []
    for mpath, _mobj in sorted(modes, key=_mode_rank_key, reverse=True):
        mkey = _path_str(mpath)
        modes_available.append({"key": mkey, "label": _mode_label(mkey)})

    # Baseline every mode so each one's "today" delta starts from a real
    # number the first time the overlay switches to it.
    for mpath, mobj in modes:
        v = mode_view(mpath, mobj)
        _record_baseline(v["key"], v["elo"], v["points"])
    _save_baselines()

    path, mode = pick_best_mode(modes)
    v = mode_view(path, mode)
    session_delta, prog_delta = _deltas(v["key"], v["elo"], v["points"], data)

    with _lock:
        STATE.update({
            "ok":               True,
            "rank_number":      v["placement"],
            "rank_label":       v["label"],
            "elo":              v["elo"],
            "is_unreal":        v["is_unreal"],
            "next_position":    v["next_pos"],
            "elo_to_next":      v["next_gap"],
            "session_delta":    session_delta,
            "prog_delta":       prog_delta,
            "updated_at":       int(time.time()),
            "error":            None,
            "active_mode_key":  v["key"],
            "modes_available":  modes_available,
        })

    sess   = compute_windowed_stats(data, "session", v["key"])
    kd_txt = f"{sess['kd']:.2f}"  if sess["kd"] is not None else "-"
    wr_txt = f"{sess['wr']:.1f}%" if sess["wr"] is not None else "-"
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    elo_str = f"{v['elo']} ELO" if v["elo"] is not None else (v["label"] or "-")
    gap_str = f"   next #{v['next_pos']} in {v['next_gap']} ELO" if v["next_gap"] is not None else ""
    print(f"[overlay] {ts}  {elo_str}  |  today {session_delta:+d}{gap_str}")
    print(f"          session: {sess['wins']}W / {sess['losses']}L   KD {kd_txt}   WR {wr_txt}")


def poll_loop():
    refresh_once()
    while True:
        time.sleep(POLL_SECONDS)
        refresh_once()


def snapshot(window="session", mode_key=None):
    with _lock:
        s   = dict(STATE)
        raw = _last_raw
        mds = list(_last_modes)

    if mode_key and mds:
        path, mode = pick_mode_by_key(mds, mode_key)
        if mode is None:
            path, mode = pick_best_mode(mds)
    else:
        path, mode = pick_best_mode(mds) if mds else (None, None)

    if mode is not None:
        v = mode_view(path, mode)
        delta, prog_delta = _deltas(v["key"], v["elo"], v["points"], raw)
    else:
        v = {
            "key":         "",
            "label":       s.get("rank_label") or "",
            "placement":   s.get("rank_number"),
            "progression": None,
            "is_unreal":   s.get("is_unreal", False),
            "elo":         s.get("elo"),
            "next_pos":    s.get("next_position"),
            "next_gap":    s.get("elo_to_next"),
            "points":      None,
        }
        delta      = s.get("session_delta", 0) or 0
        prog_delta = s.get("prog_delta", 0) or 0

    resolved_key = v["key"]
    elo          = v["elo"]
    label        = v["label"] or ""
    placement    = v["placement"]
    progression  = v["progression"]
    is_unreal    = v["is_unreal"]
    nxt          = v["next_pos"]
    gap          = v["next_gap"]

    season_stats = compute_windowed_stats(raw, "season", resolved_key or None)

    # Answer for the mode that was actually asked about, not whichever one the
    # poll loop last picked.
    s["active_mode_key"] = resolved_key or s.get("active_mode_key", "")
    s["rank_number"]     = placement
    s["rank_label"]      = label
    s["elo"]             = elo
    s["session_delta"]   = delta

    s["is_unreal"]       = is_unreal
    s["elo_unavailable"] = bool(is_unreal and elo is None)
    s["progression_pct"] = progression if not is_unreal else None
    s["prog_delta"]      = prog_delta if not is_unreal else None
    s["rank_display"]    = f"#{placement} {label}".strip() if (is_unreal and placement) else (label or "-")
    s["elo_text"]        = f"{elo} ELO" if (is_unreal and elo is not None) else None

    s["season_kd"]    = f"{season_stats['kd']:.2f}"  if season_stats["kd"] is not None else "-"
    s["season_wr"]    = f"{season_stats['wr']:.1f}%" if season_stats["wr"] is not None else "-%"
    s["season_wins"]  = season_stats["wins"]
    s["season_kills"] = season_stats["kills"]

    next_div_name = None
    if not is_unreal:
        div = _num(mode.get("division")) if mode is not None else None
        if div is not None and (div + 1) in DIVISION_NAMES:
            next_div_name = division_name(div + 1)
    s["next_rank_name"] = next_div_name

    if is_unreal and nxt and gap is not None:
        s["next_gap"] = str(gap)
        s["next_pos"] = str(nxt)
    elif is_unreal and nxt:
        s["next_gap"] = None
        s["next_pos"] = str(nxt)
    elif not is_unreal and progression is not None:
        # Designs that reuse the same "next" row below Unreal want the climb
        # expressed as a percentage toward the next division.
        s["next_gap"] = f"{100 - progression}%"
        s["next_pos"] = next_div_name or "NEXT RANK"
    else:
        s["next_gap"] = None
        s["next_pos"] = None

    if is_unreal:
        if window == "season":
            s["session_text"] = f"+{elo} ALL SEASON" if elo is not None else "- ALL SEASON"
            s["session_sign"] = "pos" if elo is not None else "zero"
        elif window in ("12h", "24h"):
            wd    = windowed_elo_delta(raw, window, elo, resolved_key or None)
            label_str = "PAST 12H" if window == "12h" else "PAST 24H"
            if wd is None:
                s["session_text"] = f"+0 ELO {label_str}"
                s["session_sign"] = "zero"
            else:
                sign = "+" if wd >= 0 else ""
                s["session_text"] = f"{sign}{wd} ELO {label_str}"
                s["session_sign"] = "pos" if wd > 0 else ("neg" if wd < 0 else "zero")
        else:
            sign = "+" if delta >= 0 else ""
            s["session_text"] = f"{sign}{delta} ELO TODAY"
            s["session_sign"] = "pos" if delta > 0 else ("neg" if delta < 0 else "zero")
    else:
        s["pct_to_next"] = (100 - progression) if progression is not None else None
        sign = "+" if prog_delta >= 0 else ""
        s["session_text"] = f"{sign}{prog_delta}% TODAY"
        s["session_sign"] = "pos" if prog_delta > 0 else ("neg" if prog_delta < 0 else "zero")

    s["gap_unavailable"] = bool(is_unreal and (s.get("next_gap") is None or s.get("next_pos") is None))
    s["session_start"] = SESSION_START
    s["window"]        = window
    return s


def debug_report():
    out = []
    out.append("Fortnite Ranked Overlay - debug")
    out.append(f"username     : {EPIC_USERNAME}")
    out.append(f"account      : {EPIC_ACCOUNT_ID}")
    out.append(f"api          : {API_BASE}/stats/{EPIC_ACCOUNT_ID}")
    out.append(f"session start: {datetime.datetime.fromtimestamp(SESSION_START):%Y-%m-%d %H:%M:%S} ({SESSION_START})")
    out.append("")
    out.append("CURRENT STATE")
    s = snapshot()
    for k in ("ok", "rank_number", "rank_label", "elo", "session_delta", "next_position", "elo_to_next", "updated_at", "error"):
        out.append(f"  {k}: {s.get(k)}")
    out.append("")
    out.append("STATS ranking_id")
    out.append(f"  {_detect_ranking_id(_last_raw) if _last_raw else None}")
    out.append("")
    out.append("SESSION STATS")
    sess = compute_windowed_stats(_last_raw, "session")
    for k, v in sess.items():
        out.append(f"  {k}: {v}")
    out.append("")
    out.append("SEASON STATS")
    seas = compute_windowed_stats(_last_raw, "season")
    for k, v in seas.items():
        out.append(f"  {k}: {v}")
    out.append("")
    out.append(f"ranked-mode candidates: {len(_last_modes)}")
    for path, obj in _last_modes:
        key  = _path_str(path)
        v    = mode_view(path, obj)
        seas = compute_windowed_stats(_last_raw, "season", key)
        out.append(
            "  - " + key
            + f"  div={obj.get('division')} ({division_name(_num(obj.get('division')))})"
            + f"  unreal={_unreal_placement(obj)}  api_elo={obj.get('elo')}"
        )
        out.append(
            f"      resolved elo={v['elo']}  next=#{v['next_pos']}"
            + f"  gap={v['next_gap']}  stat_path={_mode_stat_path(key)}"
        )
        out.append(
            f"      season: {seas['wins']}W/{seas['losses']}L  kills={seas['kills']}"
            + f"  kd={seas['kd']}  wr={seas['wr']}"
        )
    if _last_modes:
        cp, _ = pick_best_mode(_last_modes)
        out.append("  chosen: " + ("/".join(map(str, cp)) if cp else "None"))
    out.append("")
    out.append("Raw JSON: /raw")
    return "\n".join(out)


OVERLAY_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Fortnite Rank Overlay</title>
    <link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=Space+Mono:wght@400;700&display=swap" rel="stylesheet">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }

        :root {
            --accent: #a855f7;
            --accent-rgb: 168, 85, 247;
            --accent-light: color-mix(in srgb, var(--accent) 65%, white);
        }

        body {
            background: transparent;
            font-family: 'Space Grotesk', sans-serif;
            display: flex;
            flex-direction: column;
            align-items: flex-start;
            padding: 20px;
        }

        .wrap {
            display: flex;
            flex-direction: column;
            align-items: stretch;
            gap: 0;
        }

        .card {
            background: rgba(13, 11, 18, 0.92);
            border: 1px solid rgba(var(--accent-rgb), 0.20);
            min-width: 480px;
            overflow: hidden;
            position: relative;
        }

        .slash-header {
            position: relative;
            display: flex;
            align-items: stretch;
            height: 84px;
            overflow: hidden;
        }

        .slash-left {
            display: flex;
            flex-direction: column;
            justify-content: center;
            padding: 0 52px 0 22px;
            background: rgba(var(--accent-rgb), 0.15);
            position: relative;
            z-index: 1;
            flex: 1;
            min-width: 0;
        }

        .slash-blade {
            position: absolute;
            right: 180px;
            top: 0;
            bottom: 0;
            width: 44px;
            background: rgba(13, 11, 18, 0.92);
            transform: skewX(-12deg);
            z-index: 2;
            border-left: 2px solid var(--accent);
        }

        .slash-right {
            width: 200px;
            flex-shrink: 0;
            display: flex;
            flex-direction: column;
            justify-content: center;
            align-items: flex-end;
            padding: 0 22px 0 36px;
            background: rgba(13, 11, 18, 0.92);
            position: relative;
            z-index: 1;
        }

        .rank-label-small {
            font-family: 'Space Mono', monospace;
            font-size: 11px;
            font-weight: 400;
            letter-spacing: 0.18em;
            text-transform: uppercase;
            color: var(--accent-light);
            line-height: 1;
            margin-bottom: 4px;
        }

        .rank-value {
            font-size: 36px;
            font-weight: 700;
            color: #ffffff;
            text-transform: uppercase;
            letter-spacing: -0.01em;
            line-height: 1;
            white-space: nowrap;
        }

        .rank-value .accent { color: var(--accent); }

        .elo-label-small {
            font-family: 'Space Mono', monospace;
            font-size: 11px;
            font-weight: 400;
            letter-spacing: 0.18em;
            text-transform: uppercase;
            color: var(--accent-light);
            line-height: 1;
            margin-bottom: 4px;
            text-align: right;
        }

        .elo-value {
            font-size: 36px;
            font-weight: 700;
            color: var(--accent);
            letter-spacing: -0.01em;
            line-height: 1;
            text-align: right;
            text-shadow: 0 0 28px rgba(var(--accent-rgb), 0.50);
        }

        .prog-row {
            display: none;
            flex-direction: column;
            align-items: flex-end;
            gap: 4px;
        }

        .prog-row.visible { display: flex; }

        .prog-value {
            font-size: 36px;
            font-weight: 700;
            color: var(--accent);
            letter-spacing: -0.01em;
            line-height: 1;
            text-shadow: 0 0 28px rgba(var(--accent-rgb), 0.50);
        }

        .prog-track {
            width: 80px;
            height: 4px;
            background: rgba(var(--accent-rgb), 0.18);
            border-radius: 2px;
            overflow: hidden;
        }

        .prog-fill {
            height: 100%;
            background: var(--accent);
            border-radius: 2px;
            transition: width 0.4s ease;
        }

        .mid-band {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 9px 22px;
            background: rgba(var(--accent-rgb), 0.08);
            border-top: 1px solid rgba(var(--accent-rgb), 0.18);
            border-bottom: 1px solid rgba(var(--accent-rgb), 0.12);
            visibility: visible;
            min-height: 42px;
        }

        .mid-band.hidden { visibility: hidden; }

        .next-text {
            font-family: 'Space Mono', monospace;
            font-size: 15px;
            font-weight: 400;
            letter-spacing: 0.06em;
            text-transform: uppercase;
            color: var(--accent-light);
        }

        .next-text .hl     { color: #ffffff; font-weight: 700; }
        .next-text .accent { color: var(--accent); font-weight: 700; }

        .session-badge {
            font-family: 'Space Mono', monospace;
            font-size: 15px;
            font-weight: 700;
            letter-spacing: 0.06em;
            text-transform: uppercase;
            padding: 4px 12px;
            border-radius: 2px;
        }

        .session-pos  { color: #34d399; background: rgba(52,211,153,0.10); border: 1px solid rgba(52,211,153,0.25); }
        .session-neg  { color: #f87171; background: rgba(248,113,113,0.10); border: 1px solid rgba(248,113,113,0.25); }
        .session-zero { color: #ffffff; background: rgba(var(--accent-rgb),0.08); border: 1px solid rgba(var(--accent-rgb),0.25); }

        .stats-band {
            display: flex;
            padding: 0 22px;
            gap: 0;
            min-height: 58px;
            align-items: center;
        }

        .creator-row {
            padding: 0 22px;
            min-height: 58px;
            display: flex;
            align-items: center;
            font-family: 'Space Mono', monospace;
            font-size: 23px;
            font-weight: 700;
            letter-spacing: 0.06em;
            text-transform: uppercase;
            color: var(--accent);
        }

        .stat-block {
            display: flex;
            flex-direction: column;
            gap: 2px;
            flex: 1;
            position: relative;
        }

        .stat-block + .stat-block::before {
            content: '';
            position: absolute;
            left: 0; top: 4px; bottom: 4px;
            width: 1px;
            background: rgba(var(--accent-rgb), 0.20);
        }

        .stat-block + .stat-block { padding-left: 16px; }

        .s-label {
            font-family: 'Space Mono', monospace;
            font-size: 11px;
            font-weight: 400;
            letter-spacing: 0.16em;
            text-transform: uppercase;
            color: var(--accent-light);
            line-height: 1;
        }

        .s-value {
            font-size: 22px;
            font-weight: 700;
            color: #f5f0ff;
            line-height: 1;
            letter-spacing: -0.01em;
        }

        .error-text {
            font-size: 11px;
            font-weight: 600;
            color: rgba(245, 166, 35, 0.9);
            text-align: center;
            line-height: 1.4;
            margin-top: 4px;
        }

        .mode-bar {
            display: flex;
            flex-direction: column;
            gap: 6px;
            margin-top: 50px;
            width: 100%;
        }

        .mode-btn {
            font-family: 'Space Grotesk', sans-serif;
            font-size: 13px;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.10em;
            color: rgba(220, 220, 220, 0.55);
            background: rgba(20, 20, 20, 0.90);
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 6px;
            padding: 12px 16px;
            cursor: pointer;
            user-select: none;
            width: 100%;
            text-align: center;
            transition: color 0.12s, background 0.12s, border-color 0.12s;
        }

        .mode-btn:hover {
            color: rgba(255, 255, 255, 0.90);
            background: rgba(40, 40, 40, 0.95);
            border-color: rgba(255, 255, 255, 0.20);
        }

        .mode-btn.active {
            color: #ffffff;
            background: rgba(55, 55, 55, 0.98);
            border-color: rgba(255, 255, 255, 0.30);
        }
    
        #codeInput.mode-btn {
            text-align: left;
            cursor: text;
        }

        #codeInput.mode-btn::placeholder {
            color: rgba(220, 220, 220, 0.35);
        }
        /* Set when OliTracker has no ELO for this account (see elo_unavailable). */
        .elo-na { display: none !important; }
    </style>
</head>
<body>
    <div class="wrap">
        <div class="card">

            <div class="slash-header">
                <div class="slash-left">
                    <div class="rank-label-small">RANK</div>
                    <div class="rank-value" id="rankValue">#- UNREAL</div>
                </div>
                <div class="slash-blade"></div>
                <div class="slash-right">
                    <div class="elo-label-small" id="rightLabel">ELO</div>
                    <div class="elo-value" id="eloValue" style="display:block">--</div>
                    <div class="prog-row" id="progRow">
                        <div class="prog-value" id="progPct">0%</div>
                        <div class="prog-track">
                            <div class="prog-fill" id="progFill" style="width:0%"></div>
                        </div>
                    </div>
                </div>
            </div>

            <div class="mid-band" id="midBand">
                <span class="next-text" id="nextText">NEXT - ELO → <span class="accent">#-</span></span>
                <span class="session-badge session-zero" id="sessionText">+0 TODAY</span>
            </div>

            <div class="stats-band" id="statsRow">
                <div class="stat-block">
                    <div class="s-label">K/D</div>
                    <div class="s-value" id="seasonKd">-</div>
                </div>
                <div class="stat-block">
                    <div class="s-label">WIN%</div>
                    <div class="s-value" id="seasonWr">-</div>
                </div>
                <div class="stat-block">
                    <div class="s-label">KILLS</div>
                    <div class="s-value" id="seasonKills">-</div>
                </div>
                <div class="stat-block">
                    <div class="s-label">WINS</div>
                    <div class="s-value" id="seasonWins">-</div>
                </div>
            </div>
            <div class="creator-row" id="creatorRow"></div>

        </div>

        <div class="error-text" id="errorText" style="display:none"></div>
        <div class="mode-bar" id="modeBar"></div>
        <div class="mode-bar" id="displayToggleBar">
            <button class="mode-btn" id="statsToggleBtn">Stats</button>
            <button class="mode-btn" id="codeToggleBtn">Creator Code</button>
            <input class="mode-btn" id="codeInput" type="text" placeholder="Enter creator code" style="display:none;">
        </div>
    </div>

    <script>
        (function() {
            var c = new URLSearchParams(location.search).get('color');
            if (c) {
                c = c.replace('#', '');
                if (/^[0-9a-fA-F]{6}$/.test(c)) {
                    var r = parseInt(c.substr(0, 2), 16);
                    var g = parseInt(c.substr(2, 2), 16);
                    var b = parseInt(c.substr(4, 2), 16);
                    document.documentElement.style.setProperty('--accent', '#' + c);
                    document.documentElement.style.setProperty('--accent-rgb', r + ', ' + g + ', ' + b);
                }
            }
        })();

        var POLL_MS      = __POLL_MS__;
        var activeMode   = localStorage.getItem('fn_overlay_mode') || '';
        var modeBarBuilt = false;

        function $(s) { return document.querySelector(s); }

        var INITIAL_CREATOR_CODE = "__INITIAL_CREATOR_CODE__";
        // localStorage is shared by every overlay on localhost, so namespace the
        // keys per design and gate them on a signature of the server's config.
        // That way the wizard's choice (stats vs. code) and the configured code
        // always win on first load, and only an in-browser toggle for THIS
        // overlay persists, instead of one typed code bleeding across overlays.
        var FN_NS  = "Slash";
        var K_MODE = 'fn_display_mode_' + FN_NS;
        var K_CODE = 'fn_creator_code_' + FN_NS;
        var K_SIG  = 'fn_config_sig_' + FN_NS;

        var displayMode, creatorCode;
        if (localStorage.getItem(K_SIG) === INITIAL_CREATOR_CODE) {
            displayMode = localStorage.getItem(K_MODE) || (INITIAL_CREATOR_CODE ? 'code' : 'stats');
            var _savedCode = localStorage.getItem(K_CODE);
            creatorCode = (_savedCode === null) ? INITIAL_CREATOR_CODE : _savedCode;
        } else {
            displayMode = INITIAL_CREATOR_CODE ? 'code' : 'stats';
            creatorCode = INITIAL_CREATOR_CODE;
            localStorage.setItem(K_SIG, INITIAL_CREATOR_CODE);
            localStorage.setItem(K_MODE, displayMode);
            localStorage.setItem(K_CODE, creatorCode);
        }

        function renderCreatorText() {
            var el = $('#creatorRowText') || $('#creatorRow');
            el.textContent = creatorCode ? ('Use Code ' + creatorCode + ' #ad') : '';
        }

        function applyDisplayMode() {
            var statsEl = $('#statsRow');
            var codeEl  = $('#creatorRow');
            var input   = $('#codeInput');
            if (displayMode === 'code') {
                statsEl.style.display = 'none';
                codeEl.style.display  = '';
                input.style.display   = '';
            } else {
                statsEl.style.display = '';
                codeEl.style.display  = 'none';
                input.style.display   = 'none';
            }
            $('#statsToggleBtn').classList.toggle('active', displayMode === 'stats');
            $('#codeToggleBtn').classList.toggle('active', displayMode === 'code');
        }

        $('#statsToggleBtn').addEventListener('click', function() {
            displayMode = 'stats';
            localStorage.setItem(K_MODE, displayMode);
            applyDisplayMode();
        });
        $('#codeToggleBtn').addEventListener('click', function() {
            displayMode = 'code';
            localStorage.setItem(K_MODE, displayMode);
            applyDisplayMode();
        });
        $('#codeInput').addEventListener('input', function(e) {
            creatorCode = e.target.value;
            localStorage.setItem(K_CODE, creatorCode);
            renderCreatorText();
        });
        $('#codeInput').value = creatorCode || '';
        renderCreatorText();
        applyDisplayMode();

        var ROMAN = {'I':true,'II':true,'III':true,'IV':true,'V':true,'VI':true};

        function purpleNumeral(text) {
            if (!text) return '';
            var parts = text.split(' ');
            var last  = parts[parts.length - 1];
            if (ROMAN[last]) {
                return parts.slice(0, -1).join(' ') + ' <span class="accent">' + last + '</span>';
            }
            return text;
        }

        function buildModeBar(modes) {
            if (modeBarBuilt) return;
            modeBarBuilt = true;
            var bar = $('#modeBar');
            bar.innerHTML = '';
            modes.forEach(function(m) {
                var btn = document.createElement('button');
                btn.className   = 'mode-btn';
                btn.textContent = m.label;
                btn.dataset.key = m.key;
                btn.addEventListener('click', function() {
                    if (activeMode === m.key) return;
                    activeMode = m.key;
                    localStorage.setItem('fn_overlay_mode', activeMode);
                    document.querySelectorAll('.mode-btn').forEach(function(b) {
                        b.classList.toggle('active', b.dataset.key === activeMode);
                    });
                    tick();
                });
                bar.appendChild(btn);
            });
        }

        function applyData(d) {
            if (!d || !d.rank_display) return;

            var errEl = $('#errorText');
            if (errEl) {
                if (d.error) {
                    errEl.textContent = d.error;
                    errEl.style.display = 'block';
                } else {
                    errEl.style.display = 'none';
                }
            }

            var rankEl   = $('#rankValue');
            var eloEl    = $('#eloValue');
            var progRow  = $('#progRow');
            var midBand  = $('#midBand');
            var nextText = $('#nextText');
            var sessEl   = $('#sessionText');
            var rightLbl = $('#rightLabel');

            if (d.is_unreal) {
                var m = d.rank_display.match(/^(#\d+)\s+(.+)$/);
                if (m) {
                    rankEl.innerHTML = '<span class="accent">' + m[1] + '</span> ' + m[2];
                } else {
                    rankEl.textContent = d.rank_display;
                }

                rightLbl.textContent   = 'ELO';
                eloEl.textContent      = d.elo_text ? d.elo_text.replace(' ELO','') : '--';
                eloEl.style.display    = 'block';
                progRow.classList.remove('visible');

                if (d.next_gap && d.next_pos) {
                    nextText.innerHTML =
                        'NEXT <span class="hl">' + d.next_gap + ' ELO</span>' +
                        ' \u2192 <span class="accent">#</span><span class="hl">' + d.next_pos + '</span>';
                } else {
                    nextText.innerHTML = 'NEXT <span class="hl">- ELO</span> \u2192 <span class="accent">#</span><span class="hl">-</span>';
                }

                sessEl.textContent = d.session_text || '+0 TODAY';
                sessEl.className   = 'session-badge session-' + (d.session_sign || 'zero');
                midBand.classList.remove('hidden');

            } else {
                rankEl.innerHTML = purpleNumeral(d.rank_display);

                rightLbl.textContent = 'PROGRESS';
                eloEl.style.display  = 'none';
                if (d.progression_pct !== null && d.progression_pct !== undefined) {
                    $('#progPct').textContent  = d.progression_pct + '%';
                    $('#progFill').style.width = d.progression_pct + '%';
                }
                progRow.classList.add('visible');

                if (d.pct_to_next !== null && d.pct_to_next !== undefined && d.next_rank_name) {
                    nextText.innerHTML =
                        '<span class="hl">' + d.pct_to_next + '%</span> TO ' +
                        purpleNumeral(d.next_rank_name);
                } else if (d.pct_to_next !== null && d.pct_to_next !== undefined) {
                    nextText.innerHTML = '<span class="hl">' + d.pct_to_next + '%</span> TO NEXT';
                } else {
                    nextText.textContent = '-';
                }

                sessEl.textContent = d.session_text || '+0% TODAY';
                sessEl.className   = 'session-badge session-' + (d.session_sign || 'zero');
                midBand.classList.remove('hidden');
            }

            $('#seasonKd').textContent    = d.season_kd    != null ? d.season_kd    : '-';
            $('#seasonWr').textContent    = d.season_wr    != null ? d.season_wr    : '-';
            $('#seasonKills').textContent = d.season_kills != null ? d.season_kills : '-';
            $('#seasonWins').textContent  = d.season_wins  != null ? d.season_wins  : '-';

            // OliTracker only publishes ELO for the ranks it tracks. Below that
            // there is no ELO and no leaderboard row to measure a gap against,
            // so hide those widgets rather than render empty placeholders.
            var _eloNA = !!d.elo_unavailable;
            ['.slash-right', '.slash-blade', '#midBand'].forEach(function(sel) {
                var el = document.querySelector(sel);
                if (el) el.classList.toggle('elo-na', _eloNA);
            });
            // No ELO means no gap either, so the gap row goes with it.
            var _gapNA = _eloNA || !!d.gap_unavailable;
            ['#nextText'].forEach(function(sel) {
                document.querySelectorAll(sel).forEach(function(el) {
                    el.classList.toggle('elo-na', _gapNA);
                });
            });

            var modes = d.modes_available;
            if (modes && modes.length > 0) {
                if (!activeMode) activeMode = d.active_mode_key || modes[0].key;
                buildModeBar(modes);
                document.querySelectorAll('.mode-btn').forEach(function(b) {
                    b.classList.toggle('active', b.dataset.key === activeMode);
                });
            }
        }

        function tick() {
            var url = '/data?window=session' + (activeMode ? '&mode=' + encodeURIComponent(activeMode) : '');
            fetch(url, { cache: 'no-store' })
                .then(function(r) { return r.json(); })
                .then(applyData)
                .catch(function() {});
        }

        tick();
        setInterval(tick, POLL_MS);
    </script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

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
            html = html.replace("__INITIAL_CREATOR_CODE__", CREATOR_CODE.strip().replace('"', '\\"'))
            self._send(200, html, "text/html; charset=utf-8")
        elif path == "/data":
            w = _p("window", _p("stats_window", "session"))
            m = _p("mode", "")
            self._send(200, json.dumps(snapshot(w, m or None)), "application/json")
        elif path == "/raw":
            body = json.dumps(_last_raw, indent=2, default=str) if _last_raw else "{}"
            self._send(200, body, "application/json")
        elif path == "/debug":
            self._send(200, debug_report(), "text/plain; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain")


def main():
    print(f"Fortnite Ranked Overlay - port {PORT}")
    print(f"OBS Browser Source: http://localhost:{PORT}/overlay")
    print(f"Polling OliTracker every {POLL_SECONDS}s - Ctrl+C to stop\n")

    _load_baselines()
    threading.Thread(target=poll_loop, daemon=True).start()

    try:
        server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    except OSError as e:
        print(f"Could not start on port {PORT}: {e}")
        print("Another program may be using that port.")
        input("Press Enter to close...")
        return

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping. Bye!")


if __name__ == "__main__":
    main()
