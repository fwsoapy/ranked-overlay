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

EPIC_USERNAME    = "YourUsername"
EPIC_ACCOUNT_ID  = "your-account-id-here"
CREATOR_CODE     = ""
API_BASE         = "https://olitracker.com/api"
PORT             = 8888
POLL_SECONDS     = 10
OVERLAY_POLL_MS  = 10000

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
}

MODE_STAT_PATH = {
    "ranked-br-combined":      ("ranked",),
    "ranked_blastberry_build": ("reload",),
    "ranked_squareclub":       None,
    "ranked-squareclub":       None,
}

MODES_WITHOUT_SEASONAL_BLOCK = {"ranked_squareclub", "ranked-squareclub"}

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


def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(round(v))
    return None


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
        and ("elo" in d or "unreal_placement" in d)
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


def pick_best_mode(modes):
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
        return (1 if elo is not None else 0, 1 if placed is not None else 0, elo or 0)

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


def extract_progression(mode_obj):
    if _num(mode_obj.get("unreal_placement")) is not None:
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


def _stat_block(data, timeframe, ranking_id):
    path_key = MODE_STAT_PATH.get(ranking_id or "", ("ranked",))
    if path_key is None:
        return None
    try:
        block = data["stats"][timeframe]
        for k in path_key:
            block = block[k]
        return block["both"]["overall"]
    except (KeyError, TypeError):
        try:
            return data["stats"][timeframe]["ranked"]["both"]["overall"]
        except (KeyError, TypeError):
            return None


def _stats_from_history(data, ranking_id):
    total_wins = total_matches = total_kills = 0
    for day in (data.get("match_history") or []):
        for grp in (day.get("matches") or []):
            rd = grp.get("ranked_data") or {}
            if rd.get("ranking_id") != ranking_id:
                continue
            total_wins    += int(grp.get("wins",    0) or 0)
            total_matches += int(grp.get("matches", 0) or 0)
            total_kills   += int(grp.get("kills",   0) or 0)
    losses = total_matches - total_wins
    kd = round(total_kills / losses, 2) if losses > 0 else None
    wr = round(total_wins / total_matches * 100, 1) if total_matches > 0 else None
    return {"wins": total_wins, "losses": losses, "kills": total_kills, "matches": total_matches, "kd": kd, "wr": wr}


def _seasonal_ranked_stats(data, ranking_id=None):
    if ranking_id in MODES_WITHOUT_SEASONAL_BLOCK:
        return _stats_from_history(data, ranking_id)
    try:
        blk = _stat_block(data, "seasonal", ranking_id)
        if blk is None:
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


class _LeaderboardParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows   = []
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
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
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
    if "reload" in ps or "blastberry" in ps:
        return "reload"
    if "zero" in ps or "zb" in ps or "nobuild" in ps:
        return "zero-build"
    return "battle-royale"


def fetch_elo_to_next(target_placement, mode_path):
    if not ENABLE_NEXT_LOOKUP or not target_placement or target_placement < 1:
        return None
    slug     = _slug_for_mode(mode_path)
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


def _set_error(msg):
    with _lock:
        STATE["error"] = msg
        if STATE.get("elo") is None:
            STATE["ok"] = False
    print("[overlay] " + msg)


def refresh_once():
    global _start_elos, _start_progressions, _last_raw, _last_modes
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

    modes_available = []
    for path, obj in modes:
        key = _path_str(path)
        modes_available.append({"key": key, "label": _mode_label(key)})

    path, mode  = pick_best_mode(modes)
    mode_key    = _path_str(path) if path else ""
    elo         = extract_elo(mode)
    label       = extract_label(mode)
    placement   = extract_placement(mode)
    progression = extract_progression(mode)
    is_unreal   = (label == "UNREAL")

    next_pos    = (placement - 1) if (is_unreal and placement and placement > 1) else None
    elo_to_next = None
    if is_unreal and elo is not None and next_pos:
        nxt = fetch_elo_to_next(next_pos, path)
        if nxt is not None and nxt >= elo:
            elo_to_next = nxt - elo

    with _lock:
        if elo is not None and mode_key not in _start_elos:
            _start_elos[mode_key] = elo
        session_delta = (elo - _start_elos[mode_key]) if (elo is not None and mode_key in _start_elos) else 0

        if progression is not None and mode_key not in _start_progressions:
            _start_progressions[mode_key] = progression
        prog_delta = (progression - _start_progressions[mode_key]) if (progression is not None and mode_key in _start_progressions) else 0

        STATE.update({
            "ok":               True,
            "rank_number":      placement,
            "rank_label":       label,
            "elo":              elo,
            "is_unreal":        is_unreal,
            "next_position":    next_pos,
            "elo_to_next":      elo_to_next,
            "session_delta":    session_delta,
            "prog_delta":       prog_delta,
            "updated_at":       int(time.time()),
            "error":            None,
            "active_mode_key":  mode_key,
            "modes_available":  modes_available,
        })

    sess   = compute_windowed_stats(data, "session", mode_key)
    kd_txt = f"{sess['kd']:.2f}" if sess["kd"] is not None else "-"
    wr_txt = f"{sess['wr']:.1f}%" if sess["wr"] is not None else "-"
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    elo_str = f"{elo} ELO" if elo is not None else label
    print(f"[overlay] {ts}  {elo_str}  |  session {session_delta:+d}")
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
        resolved_key = _path_str(path) if path else ""
        elo         = extract_elo(mode)
        label       = extract_label(mode)
        placement   = extract_placement(mode)
        progression = extract_progression(mode)
        is_unreal   = (label == "UNREAL")
        nxt         = (placement - 1) if (is_unreal and placement and placement > 1) else None
        gap         = s.get("elo_to_next") if resolved_key == s.get("active_mode_key") else None
        with _lock:
            start      = _start_elos.get(resolved_key)
            start_prog = _start_progressions.get(resolved_key)
        delta      = (elo - start) if (elo is not None and start is not None) else 0
        prog_delta = (progression - start_prog) if (progression is not None and start_prog is not None) else 0
    else:
        resolved_key = ""
        elo         = s.get("elo")
        label       = s.get("rank_label") or ""
        placement   = s.get("rank_number")
        progression = None
        is_unreal   = s.get("is_unreal", False)
        nxt         = s.get("next_position")
        gap         = s.get("elo_to_next")
        delta       = s.get("session_delta", 0) or 0
        prog_delta  = s.get("prog_delta", 0) or 0

    season_stats = compute_windowed_stats(raw, "season", resolved_key or None)

    s["is_unreal"]       = is_unreal
    s["progression_pct"] = progression if not is_unreal else None
    s["prog_delta"]      = prog_delta if not is_unreal else None
    s["rank_display"]    = f"#{placement} {label}".strip() if (is_unreal and placement) else (label or "-")
    s["elo_text"]        = f"{elo} ELO" if (is_unreal and elo is not None) else None

    s["season_kd"]    = f"{season_stats['kd']:.2f}"  if season_stats["kd"] is not None else "-"
    s["season_wr"]    = f"{season_stats['wr']:.1f}%" if season_stats["wr"] is not None else "-%"
    s["season_wins"]  = season_stats["wins"]
    s["season_kills"] = season_stats["kills"]

    if is_unreal and nxt and gap is not None:
        s["next_gap"] = str(gap)
        s["next_pos"] = str(nxt)
    elif is_unreal and nxt:
        s["next_gap"] = None
        s["next_pos"] = str(nxt)
    else:
        s["next_gap"] = None
        s["next_pos"] = None

    if is_unreal:
        sign = "+" if delta >= 0 else ""
        s["session_text"] = f"{sign}{delta} ELO TODAY"
        s["session_sign"] = "pos" if delta > 0 else ("neg" if delta < 0 else "zero")
    else:
        pct_left = (100 - progression) if progression is not None else None
        s["pct_to_next"] = pct_left
        sign = "+" if prog_delta >= 0 else ""
        s["session_text"] = f"{sign}{prog_delta}% TODAY" if prog_delta != 0 else "+0% TODAY"
        s["session_sign"] = "pos" if prog_delta > 0 else ("neg" if prog_delta < 0 else "zero")
        div = _num(mode.get("division")) if mode is not None else None
        if div is not None:
            next_div_name = division_name(div + 1) if (div + 1) in DIVISION_NAMES else None
        else:
            next_div_name = None
        s["next_rank_name"] = next_div_name

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
        out.append(
            "  - " + "/".join(map(str, path))
            + f"  div={obj.get('division')} ({division_name(_num(obj.get('division')))})"
            + f"  unreal={obj.get('unreal_placement')}  elo={obj.get('elo')}"
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
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@500;700;800&display=swap" rel="stylesheet">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }

        :root {
            --accent: #ac46ff;
            --accent-rgb: 172, 70, 255;
            --accent-light: color-mix(in srgb, var(--accent) 65%, white);
        }

        body {
            background: transparent;
            font-family: 'Plus Jakarta Sans', sans-serif;
            display: flex;
            flex-direction: column;
            justify-content: flex-start;
            align-items: flex-start;
            height: 100vh;
            padding: 16px;
        }

        .wrap {
            display: flex;
            flex-direction: column;
            align-items: stretch;
            gap: 0;
        }

        .overlay-container {
            display: flex;
            flex-direction: column;
            background: rgba(12, 10, 16, 0.85);
            backdrop-filter: blur(8px);
            border-left: 6px solid var(--accent);
            padding: 18px 20px;
            min-width: 560px;
            letter-spacing: -0.02em;
            user-select: none;
        }

        .main-row {
            display: flex;
            justify-content: space-between;
            align-items: baseline;
            margin-bottom: 10px;
        }

        .rank-text {
            font-weight: 800;
            font-size: 44px;
            color: #ffffff;
            text-transform: uppercase;
        }

        .rank-text .purple { color: var(--accent); }

        .elo-text {
            font-weight: 800;
            font-size: 44px;
            color: var(--accent);
            text-transform: uppercase;
        }

        .sub-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-weight: 700;
            font-size: 22px;
            text-transform: uppercase;
            color: rgba(255, 255, 255, 0.75);
            padding-bottom: 12px;
            border-bottom: 1px solid rgba(255, 255, 255, 0.08);
            visibility: visible;
        }

        .sub-row.hidden { visibility: hidden; }

        .next-target {
            display: flex;
            gap: 6px;
            align-items: baseline;
        }

        .next-target .highlight { color: #ffffff; }
        .next-target .purple    { color: var(--accent-light); }

        .session-delta { font-weight: 700; }
        .session-pos  { color: #2ed573; }
        .session-neg  { color: #ff4757; }
        .session-zero { color: #ffffff; }

        .season-row {
            display: flex;
            gap: 24px;
            margin-top: 12px;
            min-height: 32px;
            align-items: center;
            font-weight: 500;
            font-size: 22px;
            color: rgba(255, 255, 255, 0.75);
        }

        .creator-row {
            margin-top: 12px;
            min-height: 32px;
            display: flex;
            align-items: center;
            font-weight: 700;
            font-size: 24px;
            letter-spacing: 0.04em;
            text-transform: uppercase;
            color: var(--accent-light);
        }

        .stat-item span {
            font-weight: 700;
            color: rgba(255, 255, 255, 0.90);
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
            font-family: 'Plus Jakarta Sans', sans-serif;
            font-size: 14px;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.08em;
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
    </style>
</head>
<body>
    <div class="wrap">
        <div class="overlay-container">
            <div class="main-row">
                <div class="rank-text" id="rankText">#- UNREAL</div>
                <div class="elo-text" id="eloRight">-- ELO</div>
            </div>

            <div class="sub-row" id="subRow">
                <div class="next-target" id="nextContainer">
                    <span>Next:</span>
                    <span class="highlight">-</span>
                    <span>to</span>
                    <span class="highlight">#-</span>
                </div>
                <div class="session-delta">
                    <span class="session-zero" id="sessionText">+0 ELO TODAY</span>
                </div>
            </div>

            <div class="season-row" id="statsRow">
                <div class="stat-item">KD: <span id="seasonKd">-</span></div>
                <div class="stat-item">WR: <span id="seasonWr">-%</span></div>
                <div class="stat-item">KILLS: <span id="seasonKills">-</span></div>
                <div class="stat-item">WINS: <span id="seasonWins">-</span></div>
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
        var FN_NS  = "Minimal";
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
            $('#creatorRow').textContent = creatorCode ? ('Use Code ' + creatorCode + ' #ad') : '';
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
                return parts.slice(0, -1).join(' ') + ' <span class="purple">' + last + '</span>';
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

            var rankEl    = $('#rankText');
            var eloRight  = $('#eloRight');
            var subRow    = $('#subRow');
            var nextCont  = $('#nextContainer');
            var sessEl    = $('#sessionText');

            if (d.is_unreal) {
                var m = d.rank_display.match(/^(#\d+)\s+(.+)$/);
                if (m) {
                    rankEl.innerHTML = '<span class="purple">' + m[1] + '</span> ' + m[2];
                } else {
                    rankEl.textContent = d.rank_display;
                }

                eloRight.textContent = d.elo_text || '-- ELO';

                if (d.next_gap && d.next_pos) {
                    nextCont.innerHTML =
                        'Next: <span class="highlight">' + d.next_gap + ' ELO</span>' +
                        ' to <span class="purple">#</span><span class="highlight">' + d.next_pos + '</span>';
                } else {
                    nextCont.innerHTML =
                        'Next: <span class="highlight">-</span> to ' +
                        '<span class="purple">#</span><span class="highlight">-</span>';
                }

                sessEl.textContent = d.session_text || '+0 ELO TODAY';
                sessEl.className   = 'session-' + (d.session_sign || 'zero');
                subRow.classList.remove('hidden');

            } else {
                rankEl.innerHTML = purpleNumeral(d.rank_display);
                eloRight.textContent = (d.progression_pct !== null && d.progression_pct !== undefined)
                    ? d.progression_pct + '%' : '-%';

                if (d.pct_to_next !== null && d.pct_to_next !== undefined && d.next_rank_name) {
                    nextCont.innerHTML =
                        '<span class="highlight">' + d.pct_to_next + '%</span>' +
                        ' TO ' + purpleNumeral(d.next_rank_name);
                } else if (d.pct_to_next !== null && d.pct_to_next !== undefined) {
                    nextCont.innerHTML =
                        '<span class="highlight">' + d.pct_to_next + '%</span> TO NEXT';
                } else {
                    nextCont.textContent = '-';
                }

                sessEl.textContent = d.session_text || '+0% TODAY';
                sessEl.className   = 'session-' + (d.session_sign || 'zero');
                subRow.classList.remove('hidden');
            }

            $('#seasonKd').textContent    = d.season_kd    != null ? d.season_kd    : '-';
            $('#seasonWr').textContent    = d.season_wr    != null ? d.season_wr    : '-';
            $('#seasonKills').textContent = d.season_kills != null ? d.season_kills : '-';
            $('#seasonWins').textContent  = d.season_wins  != null ? d.season_wins  : '-';

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
