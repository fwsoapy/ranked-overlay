import ast
import json
import os
import re
import subprocess
import sys
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

# Set AUTO_UPDATE to False to stop the overlay updating itself. When it is on,
# the overlay checks this repo for a newer tagged version on startup and once a
# day after that, and quietly installs it. Your settings above, your accent
# colour and your port are carried across; nothing is uploaded anywhere.
VERSION              = "2.1.0"
DESIGN               = "Wide"
AUTO_UPDATE          = True
UPDATE_REPO          = "fwsoapy/ranked-overlay"
UPDATE_CHECK_SECONDS = 86400

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
_season_fp          = None

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

# OliTracker publishes 100 rows a page and, today, stops around the top
# 10,000 of each mode. That ceiling is never assumed: a page either comes back
# with rows or it does not, so if a future season publishes a deeper board the
# overlay picks the extra ELO up on its own.
LEADERBOARD_PAGE_SIZE     = 100
LEADERBOARD_PAGE_LIMIT    = 100000  # sanity stop, not a real ceiling
LEADERBOARD_TTL           = 60      # seconds; keeps pace with POLL_SECONDS
LEADERBOARD_PAGES_BACK    = 4       # pages to walk up through an ELO tie

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


# Words that describe how a playlist is packaged rather than what it is, so
# they get dropped when naming a button for a mode we have not seen before.
_LABEL_NOISE = {"ranked", "combined", "build", "competitive", "playlist"}


def _mode_label(key):
    """A short button label for a ranked mode.

    Unrecognised keys are tidied up rather than printed raw, so a playlist
    added in a future season still gets a readable button instead of
    "ranked-og-combined" sprawling across the mode bar.
    """
    if key in MODE_LABELS:
        return MODE_LABELS[key]
    lower = key.lower()
    if "squareclub" in lower or "boxfight" in lower:
        return "Boxfights"
    if "blastberry" in lower or "reload" in lower:
        return "Reload"
    if "br-combined" in lower or "br_combined" in lower:
        return "BR"
    if "rocket" in lower or "racing" in lower:
        return "Racing"
    words = [w for w in re.split(r"[-_\s]+", lower) if w and w not in _LABEL_NOISE]
    if not words:
        return key
    return " ".join(w.upper() if len(w) <= 3 else w.capitalize() for w in words)


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


# Tokens that name a mode family rather than a stats bucket, so they must not
# be matched against bucket names when guessing at an unfamiliar playlist.
_BUCKET_STOPWORDS = {"ranked", "combined", "build", "br", "all", "competitive"}


def _mode_stat_path(ranking_id, available=None):
    """Which stats bucket belongs to a ranked mode.

    `available` is the set of bucket names OliTracker actually returned. A new
    season can introduce a playlist this build has never heard of, so rather
    than giving up, an unknown key is matched against the buckets on offer. If
    nothing matches, the mode is counted from its own match history instead,
    which is always correct even if it reaches back fewer days.
    """
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
    for token in re.split(r"[-_]", key):
        if len(token) >= 2 and token not in _BUCKET_STOPWORDS and token in (available or ()):
            return (token,)
    return None


def _stat_block(data, timeframe, ranking_id):
    """The stats bucket for exactly one ranked mode, or None.

    This must never fall back to another mode's bucket. OliTracker only emits a
    `reload` bucket once the account has played Reload inside that timeframe,
    and quietly borrowing `ranked` when it was missing is what made BR and
    Reload report identical KD/WR/kills/wins.
    """
    try:
        available = set(data["stats"][timeframe].keys())
    except (KeyError, TypeError, AttributeError):
        available = set()
    path_key = _mode_stat_path(ranking_id, available)
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
    if page < 1 or page > LEADERBOARD_PAGE_LIMIT:
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
    if slug is None:
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


def _season_fingerprint(data):
    """A number that only ever grows inside one season: total matches played."""
    try:
        return int(data["stats"]["seasonal"]["all"]["both"]["overall"]["matches_played"])
    except (KeyError, TypeError, ValueError):
        return None


def _roll_season_if_needed(data):
    """Clear today's baselines when Fortnite starts a new ranked season.

    Season rollover wipes the seasonal totals and resets rank, so a baseline
    taken last season would read as an enormous overnight loss. The tell is
    seasonal matches collapsing, which cannot happen inside a season.

    It has to be a collapse rather than any decrease. OliTracker recounting, or
    answering with a half-built profile, can shave a few matches off the total,
    and that must not be mistaken for a new season and wipe someone's counter
    mid-stream. A real rollover drops the count to near zero.
    """
    global _season_fp
    fp = _season_fingerprint(data)
    if fp is None:
        return
    with _lock:
        previous = _season_fp
        _season_fp = fp
        if previous is not None and fp * 2 < previous:
            _start_elos.clear()
            _start_progressions.clear()
        else:
            previous = None
    if previous is not None:
        print(f"[overlay] new season detected ({previous} -> {fp} matches) - counters re-baselined")


def _record_baseline(mode_key, elo, points):
    with _lock:
        if elo is not None and mode_key not in _start_elos:
            _start_elos[mode_key] = elo
        if points is not None and mode_key not in _start_progressions:
            _start_progressions[mode_key] = points


def _deltas(mode_key, elo, points):
    """How much this mode has moved since the overlay was started.

    Measured from the first reading taken after launch, not from midnight and
    not from anything OliTracker publishes per calendar day. Close the overlay
    and open it again and the count starts over, which is the point: it shows
    what this session earned.
    """
    with _lock:
        start_elo  = _start_elos.get(mode_key)
        start_prog = _start_progressions.get(mode_key)
    elo_delta  = (elo - start_elo)     if (elo    is not None and start_elo  is not None) else 0
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

    _roll_season_if_needed(data)

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

    path, mode = pick_best_mode(modes)
    v = mode_view(path, mode)
    session_delta, prog_delta = _deltas(v["key"], v["elo"], v["points"])

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


# Keeping itself current
#
# Fortnite seasons come and go and OliTracker changes shape with them. Rather
# than leaving everyone on a build that slowly stops matching the API, the
# overlay looks at the repo's version tags and installs a newer one itself.
# It only ever reads from UPDATE_REPO over HTTPS, it refuses anything that
# will not parse, and it keeps a copy of the outgoing file.

def _parse_version(text):
    parts = re.findall(r"\d+", str(text or ""))
    return tuple(int(p) for p in parts[:3]) if parts else (0,)


def _latest_version_tag():
    """The newest version tag on the repo, or None if that cannot be read."""
    url = f"https://api.github.com/repos/{UPDATE_REPO}/tags?per_page=100"
    headers = {"User-Agent": "fortnite-rank-overlay", "Accept": "application/vnd.github+json"}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as r:
        tags = json.loads(r.read().decode("utf-8", "replace"))
    best = None
    for tag in tags if isinstance(tags, list) else []:
        name = (tag or {}).get("name") or ""
        if not re.fullmatch(r"v?\d+(?:\.\d+)*", name):
            continue
        if best is None or _parse_version(name) > _parse_version(best):
            best = name
    return best


def _carry_over_settings(old_text, new_text):
    """Move this install's own settings into the downloaded file.

    An update must never cost someone their account ID, creator code, accent
    colour or port, so each of those is lifted out of the running file and
    written into the new one before anything is installed. Everything else,
    including the overlay markup itself, comes from the new build.
    """
    def carry(pattern, group):
        """Copy one setting across, keeping the new file's own spacing."""
        nonlocal new_text
        old = re.search(pattern, old_text)
        if old is None or re.search(pattern, new_text) is None:
            return
        value = old.group(group)
        new_text = re.sub(
            pattern,
            lambda m: m.group(0)[:m.start(group) - m.start(0)] + value
                      + m.group(0)[m.end(group) - m.start(0):],
            new_text, count=1)

    for name in ("EPIC_USERNAME", "EPIC_ACCOUNT_ID", "CREATOR_CODE",
                 "RANKED_MODE_HINT", "RANKED_MODE_KEY"):
        carry(name + r'\s*=\s*"([^"]*)"', 1)

    carry(r"PORT \s*=\s*(\d+)".replace(" ", ""), 1)
    carry(r"AUTO_UPDATE\s*=\s*(True|False)", 1)
    carry(r"UPDATE_CHECK_SECONDS\s*=\s*(\d+)", 1)

    # Accent colour lives in the stylesheet rather than the config block.
    carry(r"--accent:\s*(#[0-9a-fA-F]{6});", 1)
    carry(r"--accent-rgb:\s*(\d+,\s*\d+,\s*\d+);", 1)
    carry(r"--stat-label-color:\s*(#[0-9a-fA-F]{6});", 1)

    return new_text


def _install_update(tag):
    """Download the tagged build of this design and put it in place."""
    url = (f"https://raw.githubusercontent.com/{UPDATE_REPO}/{tag}"
           f"/wizard/templates/{DESIGN}/server.py")
    downloaded = _http_get_text(url, timeout=30)

    if "OVERLAY_HTML" not in downloaded or "def refresh_once" not in downloaded:
        raise ValueError("that download does not look like an overlay server")
    ast.parse(downloaded)

    here = os.path.abspath(__file__)
    with open(here, encoding="utf-8") as f:
        current = f.read()
    merged = _carry_over_settings(current, downloaded)
    ast.parse(merged)          # refuse to install something that will not run

    os.makedirs(_state_dir(), exist_ok=True)
    backup = os.path.join(_state_dir(), f"server-{VERSION}-backup.py")
    with open(backup, "w", encoding="utf-8") as f:
        f.write(current)
    with open(here, "w", encoding="utf-8") as f:
        f.write(merged)
    return here, backup


def _restart_self():
    """Hand over to the freshly written file."""
    script = os.path.abspath(__file__)
    subprocess.Popen([sys.executable, script], cwd=os.path.dirname(script))
    os._exit(0)


def check_for_update():
    if not AUTO_UPDATE:
        return False
    if DESIGN not in ("Classic", "Minimal", "Modern", "Pulse",
                      "Rainbow", "Sharp", "Slash", "Wide"):
        return False                      # unknown design, nothing to fetch
    tag = _latest_version_tag()
    if not tag or _parse_version(tag) <= _parse_version(VERSION):
        return False
    print(f"[overlay] version {tag} is out, updating from {VERSION}")
    _, backup = _install_update(tag)
    print(f"[overlay] installed. previous version kept at {backup}")
    print("[overlay] restarting...")
    _restart_self()
    return True


def update_loop():
    while AUTO_UPDATE:
        try:
            check_for_update()
        except Exception as e:
            # An update is a nice-to-have, never a reason to stop the overlay.
            print(f"[overlay] update check skipped: {e}")
        time.sleep(max(3600, UPDATE_CHECK_SECONDS))


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
        delta, prog_delta = _deltas(v["key"], v["elo"], v["points"])
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
    <link href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@500;700;800;900&family=Barlow:wght@500;600&display=swap" rel="stylesheet">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }

        :root {
            --accent: #38bdf8;
            --accent-rgb: 56, 189, 248;
            --accent-light: color-mix(in srgb, var(--accent) 65%, white);
        }

        body {
            background: transparent;
            font-family: 'Barlow Condensed', sans-serif;
            display: flex;
            flex-direction: column;
            justify-content: flex-start;
            align-items: flex-start;
            height: 100vh;
            padding: 0;
        }

        .wrap {
            display: flex;
            flex-direction: column;
            align-items: stretch;
            gap: 0;
        }

        .overlay-container {
            position: relative;
            display: flex;
            flex-direction: row;
            align-items: stretch;
            min-width: 600px;
            overflow: hidden;
            background: rgba(4, 10, 18, 0.90);
            backdrop-filter: blur(12px);
            border: 1px solid rgba(var(--accent-rgb), 0.18);
            box-shadow:
                0 0 0 1px rgba(0,0,0,0.6),
                0 4px 32px rgba(0,0,0,0.5),
                inset 0 1px 0 rgba(255,255,255,0.04);
        }

        .accent-bar {
            width: 7px;
            background: linear-gradient(180deg, var(--accent) 0%, color-mix(in srgb, var(--accent) 70%, black) 100%);
            flex-shrink: 0;
        }

        .accent-bar::after {
            content: '';
            position: absolute;
            top: 0;
            right: -20px;
            width: 0;
            height: 0;
            border-top: 0 solid transparent;
            border-bottom: 300px solid transparent;
            border-left: 20px solid color-mix(in srgb, var(--accent) 70%, black);
            opacity: 0.35;
            pointer-events: none;
        }

        .content {
            flex: 1;
            display: flex;
            flex-direction: column;
            padding: 14px 22px 14px 24px;
            gap: 0;
        }

        .top-row {
            display: flex;
            align-items: baseline;
            gap: 14px;
            margin-bottom: 6px;
        }

        .top-divider {
            font-weight: 300;
            font-size: 34px;
            color: rgba(var(--accent-rgb), 0.4);
            align-self: center;
        }

        .elo-group {
            display: flex;
            align-items: center;
            gap: 20px;
            margin-left: auto;
        }

        .rank-badge {
            display: flex;
            align-items: baseline;
            gap: 8px;
        }

        .rank-number {
            font-weight: 900;
            font-size: 42px;
            color: var(--accent);
            letter-spacing: -0.01em;
            line-height: 1;
        }

        .rank-label {
            font-weight: 900;
            font-size: 42px;
            color: #ffffff;
            letter-spacing: 0.02em;
            line-height: 1;
            text-transform: uppercase;
        }

        .elo-value {
            font-weight: 900;
            font-size: 42px;
            letter-spacing: 0.02em;
            line-height: 1;
            text-transform: uppercase;
            color: var(--accent);
            text-shadow:
                0 0 20px rgba(var(--accent-rgb), 0.7),
                0 0 40px rgba(var(--accent-rgb), 0.3);
        }

        .prog-group {
            display: none;
            align-items: center;
            gap: 12px;
            margin-left: auto;
        }

        .prog-group.visible { display: flex; }

        .prog-pct {
            font-weight: 900;
            font-size: 38px;
            color: var(--accent);
            letter-spacing: 0.02em;
            line-height: 1;
            text-shadow:
                0 0 20px rgba(var(--accent-rgb), 0.7),
                0 0 40px rgba(var(--accent-rgb), 0.3);
        }

        .prog-track {
            width: 90px;
            height: 8px;
            background: rgba(var(--accent-rgb), 0.15);
            border-radius: 4px;
            overflow: hidden;
        }

        .prog-fill {
            height: 100%;
            background: linear-gradient(90deg, var(--accent), color-mix(in srgb, var(--accent) 70%, black));
            border-radius: 4px;
            transition: width 0.4s ease;
        }

        .divider {
            width: 100%;
            height: 1px;
            background: linear-gradient(90deg,
                rgba(var(--accent-rgb),0.5) 0%,
                rgba(var(--accent-rgb),0.1) 60%,
                transparent 100%);
            margin-bottom: 8px;
        }

        .mid-row {
            display: flex;
            align-items: center;
            justify-content: space-between;
            min-height: 40px;
        }

        .next-value {
            font-weight: 800;
            font-size: 22px;
            letter-spacing: 0.04em;
            color: var(--accent-light);
            text-transform: uppercase;
            white-space: nowrap;
            visibility: visible;
        }

        .next-value.hidden { visibility: hidden; }
        .next-value .hl { color: #fff; }

        .session-pill {
            font-weight: 800;
            font-size: 18px;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            padding: 4px 12px;
            border-radius: 3px;
            background: rgba(255,255,255,0.05);
            border: 1px solid rgba(255,255,255,0.1);
            color: #fff;
        }

        .session-pos  { color: #3ddc68; border-color: rgba(61,220,104,0.3);  background: rgba(61,220,104,0.08); }
        .session-neg  { color: #ff4a5e; border-color: rgba(255,74,94,0.3);   background: rgba(255,74,94,0.08); }
        .session-zero { color: #fff; }

        .stats-row {
            display: flex;
            gap: 20px;
            align-items: center;
        }

        .stat-chip {
            display: flex;
            flex-direction: column;
            gap: 2px;
        }

        .stat-chip .s-label {
            font-family: 'Barlow', sans-serif;
            font-weight: 600;
            font-size: 12px;
            letter-spacing: 0.14em;
            text-transform: uppercase;
            color: var(--accent-light);
            line-height: 1;
        }

        .stat-chip .s-value {
            font-weight: 800;
            font-size: 20px;
            letter-spacing: 0.02em;
            color: #ffffff;
            line-height: 1;
        }

        .creator-row {
            font-weight: 800;
            font-size: 26px;
            letter-spacing: 0.06em;
            text-transform: uppercase;
            color: var(--accent-light);
            display: flex;
            align-items: center;
        }

        .stat-sep {
            width: 1px;
            height: 30px;
            background: rgba(var(--accent-rgb),0.2);
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
            gap: 7px;
            width: 100%;
            margin-top: 50px;
        }

        .mode-btn {
            font-family: 'Barlow', sans-serif;
            font-size: 14px;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.10em;
            color: rgba(220, 220, 220, 0.55);
            background: rgba(20, 20, 20, 0.90);
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 8px;
            padding: 13px 18px;
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
        <div class="overlay-container">
            <div class="accent-bar"></div>
            <div class="content">

                <div class="top-row">
                    <div class="rank-badge">
                        <span class="rank-number" id="rankNumber">#-</span>
                        <span class="rank-label" id="rankLabel">UNREAL</span>
                    </div>
                    <span class="top-divider" id="topDivider">|</span>
                    <div class="elo-group" id="eloGroup">
                        <div class="elo-value" id="eloText">-- ELO</div>
                        <div class="session-pill session-zero" id="sessionText">+0 ELO TODAY</div>
                    </div>
                    <div class="prog-group" id="progGroup">
                        <div class="prog-track">
                            <div class="prog-fill" id="progFill" style="width:0%"></div>
                        </div>
                        <div class="prog-pct" id="progPct">0%</div>
                        <div class="session-pill session-zero" id="sessionText2">+0% TODAY</div>
                    </div>
                </div>

                <div class="divider"></div>

                <div class="mid-row">
                    <span class="next-value hidden" id="nextRow">
                        <span id="nextLabel">NEXT</span> <span class="hl" id="nextGap">-</span><span id="nextUnit"> ELO</span> <span id="nextArrow">&rarr;</span> <span class="hl" id="nextPos">#-</span>
                    </span>
                    <div class="stats-row" id="statsRow">
                        <div class="stat-chip">
                            <span class="s-label">K/D</span>
                            <span class="s-value" id="seasonKd">-</span>
                        </div>
                        <div class="stat-sep"></div>
                        <div class="stat-chip">
                            <span class="s-label">WIN%</span>
                            <span class="s-value" id="seasonWr">-%</span>
                        </div>
                        <div class="stat-sep"></div>
                        <div class="stat-chip">
                            <span class="s-label">KILLS</span>
                            <span class="s-value" id="seasonKills">-</span>
                        </div>
                        <div class="stat-sep"></div>
                        <div class="stat-chip">
                            <span class="s-label">WINS</span>
                            <span class="s-value" id="seasonWins">-</span>
                        </div>
                    </div>
                    <div class="creator-row" id="creatorRow"></div>
                </div>

            </div>
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
        var FN_NS  = "Wide";
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

        function splitRankDisplay(rd, isUnreal) {
            if (!rd) return { num: '', label: 'UNREAL' };
            if (isUnreal) {
                var m = rd.match(/^(#\d+)\s+(.+)$/);
                if (m) return { num: m[1], label: m[2] };
            }
            return { num: '', label: rd };
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

            var parts = splitRankDisplay(d.rank_display, d.is_unreal);
            $('#rankNumber').textContent = parts.num;
            $('#rankLabel').textContent  = parts.label;

            var eloGroup  = $('#eloGroup');
            var progGroup = $('#progGroup');
            var nextRow   = $('#nextRow');

            if (d.is_unreal) {
                $('#eloText').textContent = d.elo_text || '-- ELO';
                var sessEl = $('#sessionText');
                sessEl.textContent = d.session_text || '+0 ELO TODAY';
                sessEl.className   = 'session-pill session-' + (d.session_sign || 'zero');
                eloGroup.classList.remove('visible');
                eloGroup.style.display = '';
                progGroup.classList.remove('visible');

                var hasGap = d.next_gap && d.next_pos;
                if (hasGap) {
                    $('#nextLabel').textContent = 'NEXT';
                    $('#nextGap').textContent   = d.next_gap;
                    $('#nextUnit').textContent  = ' ELO';
                    $('#nextArrow').textContent = '→';
                    $('#nextPos').textContent   = '#' + d.next_pos;
                    nextRow.classList.remove('hidden');
                } else {
                    nextRow.classList.add('hidden');
                }
            } else {
                eloGroup.style.display = 'none';
                if (d.progression_pct !== null && d.progression_pct !== undefined) {
                    $('#progFill').style.width = d.progression_pct + '%';
                    $('#progPct').textContent  = d.progression_pct + '%';
                }
                var sess2 = $('#sessionText2');
                sess2.textContent = d.session_text || '+0% TODAY';
                sess2.className   = 'session-pill session-' + (d.session_sign || 'zero');
                progGroup.classList.add('visible');

                if (d.next_gap && d.next_pos) {
                    $('#nextLabel').textContent = '';
                    $('#nextGap').textContent   = d.next_gap;
                    $('#nextUnit').textContent  = '';
                    $('#nextArrow').textContent = 'TO';
                    $('#nextPos').textContent   = d.next_pos;
                    nextRow.classList.remove('hidden');
                } else {
                    nextRow.classList.add('hidden');
                }
            }

            $('#seasonKd').textContent    = d.season_kd    != null ? d.season_kd    : '-';
            $('#seasonWr').textContent    = d.season_wr    != null ? d.season_wr    : '-';
            $('#seasonKills').textContent = d.season_kills != null ? d.season_kills : '-';
            $('#seasonWins').textContent  = d.season_wins  != null ? d.season_wins  : '-';

            // OliTracker only publishes ELO for the ranks it tracks. Below that
            // there is no ELO and no leaderboard row to measure a gap against,
            // so hide those widgets rather than render empty placeholders.
            var _eloNA = !!d.elo_unavailable;
            ['#eloGroup', '#topDivider', '#nextRow'].forEach(function(sel) {
                var el = document.querySelector(sel);
                if (el) el.classList.toggle('elo-na', _eloNA);
            });
            // No ELO means no gap either, so the gap row goes with it.
            var _gapNA = _eloNA || !!d.gap_unavailable;
            ['#nextRow'].forEach(function(sel) {
                document.querySelectorAll(sel).forEach(function(el) {
                    el.classList.toggle('elo-na', _gapNA);
                });
            });

            var modes = d.modes_available;
            if (modes && modes.length > 0) {
                if (!activeMode) {
                    activeMode = d.active_mode_key || modes[0].key;
                }
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
        elif path == "/version":
            body = json.dumps({"version": VERSION, "design": DESIGN,
                               "auto_update": AUTO_UPDATE, "repo": UPDATE_REPO})
            self._send(200, body, "application/json")
        elif path == "/debug":
            self._send(200, debug_report(), "text/plain; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain")


def _bind(port, attempts=20):
    """Bind the port, waiting out a version of ourselves that is still exiting.

    After an auto-update the outgoing process can hold the socket for a moment,
    so a first refusal is not a reason to give up.
    """
    last = None
    for _ in range(attempts):
        try:
            return ThreadingHTTPServer(("0.0.0.0", port), Handler)
        except OSError as e:
            last = e
            time.sleep(1)
    raise last


def main():
    print(f"Fortnite Ranked Overlay {VERSION} ({DESIGN}) - port {PORT}")
    print(f"OBS Browser Source: http://localhost:{PORT}/overlay")
    print(f"Polling OliTracker every {POLL_SECONDS}s - Ctrl+C to stop\n")

    threading.Thread(target=poll_loop, daemon=True).start()
    if AUTO_UPDATE:
        threading.Thread(target=update_loop, daemon=True).start()

    try:
        server = _bind(PORT)
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
