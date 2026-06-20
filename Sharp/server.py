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


def _elo_series(data, ranking_id):
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
    for day in reversed(data.get("match_history") or []):
        ent = (day.get("elo") or {}).get(ranking_id) if ranking_id else None
        if isinstance(ent, dict):
            st = _num(ent.get("start"))
            if st is not None:
                return st
    return None


def windowed_elo_delta(data, window, current_elo, ranking_id=None):
    if current_elo is None or data is None:
        return None
    secs = _WINDOW_SECS.get(window)
    if secs is None:
        return None
    cutoff     = int(time.time()) - secs
    rid        = ranking_id or _detect_ranking_id(data)
    pts        = _elo_series(data, rid)
    if not pts:
        return None
    baseline = None
    for ts, e in pts:
        if ts <= cutoff:
            baseline = e
        else:
            break
    if baseline is None:
        baseline = _earliest_day_start_elo(data, rid)
        if baseline is None:
            baseline = pts[0][1]
    return current_elo - baseline


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

    stats = compute_windowed_stats(raw, window, resolved_key or None)

    s["is_unreal"]       = is_unreal
    s["progression_pct"] = progression if not is_unreal else None
    s["prog_delta"]      = prog_delta if not is_unreal else None
    s["rank_display"]    = f"#{placement} {label}".strip() if (is_unreal and placement) else (label or "-")
    s["elo_text"]        = f"{elo} ELO" if (is_unreal and elo is not None) else None

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
        if window == "season":
            s["session_text"] = f"+{elo} ALL SEASON" if elo is not None else "- ALL SEASON"
            s["session_sign"] = "pos" if elo is not None else "zero"
        elif window in ("12h", "24h"):
            wd = windowed_elo_delta(raw, window, elo, resolved_key or None)
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
        pct_left = (100 - progression) if progression is not None else None
        s["pct_to_next"] = pct_left
        sign = "+" if prog_delta >= 0 else ""
        s["session_text"] = f"{sign}{prog_delta}% TODAY" if prog_delta != 0 else "+0% TODAY"
        s["session_sign"] = "pos" if prog_delta > 0 else ("neg" if prog_delta < 0 else "zero")

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
    <link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@700;800;900&display=swap" rel="stylesheet">
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Rajdhani:wght@600;700&family=Bebas+Neue&display=swap');

        * { box-sizing: border-box; margin: 0; padding: 0; }

        :root {
            --accent: #cc1111;
            --accent-rgb: 204, 17, 17;
        }

        body {
            background: transparent;
            font-family: 'Rajdhani', sans-serif;
            display: flex;
            flex-direction: column;
            align-items: flex-start;
        }

        .overlay-container {
            position: relative;
            display: flex;
            flex-direction: column;
            gap: 0;
            user-select: none;
            min-width: 480px;
        }

        .card {
            position: relative;
            background: rgba(8, 4, 4, 0.93);
            border-left: 4px solid var(--accent);
            padding: 22px 32px 22px 26px;
            clip-path: polygon(0 0, 100% 0, 97% 100%, 0 100%);
        }

        .rank-row {
            display: flex;
            align-items: baseline;
            gap: 10px;
            line-height: 1;
        }

        .rank-label {
            font-family: 'Bebas Neue', sans-serif;
            font-size: 80px;
            color: #fff;
            letter-spacing: 3px;
            white-space: nowrap;
        }

        .unreal-rainbow {
            font-family: 'Bebas Neue', sans-serif;
            font-size: 80px;
            letter-spacing: 3px;
            white-space: nowrap;
            color: var(--accent);
        }

        .stat-row {
            display: flex;
            align-items: center;
            gap: 18px;
            margin-top: 2px;
        }

        .elo-val {
            font-family: 'Bebas Neue', sans-serif;
            font-size: 44px;
            color: var(--accent);
            letter-spacing: 2px;
            white-space: nowrap;
        }

        .sep {
            width: 2px;
            height: 32px;
            background: rgba(180, 30, 30, 0.5);
        }

        .session-delta {
            font-family: 'Rajdhani', sans-serif;
            font-weight: 700;
            font-size: 28px;
            letter-spacing: 1px;
            white-space: nowrap;
            text-transform: uppercase;
        }
        .session-pos  { color: #33ff88; }
        .session-neg  { color: #ff3333; }
        .session-zero { color: #ffffff; }

        .next-row {
            display: flex;
            align-items: center;
            gap: 8px;
            margin-top: 6px;
        }

        .next-gap-val {
            font-family: 'Rajdhani', sans-serif;
            font-weight: 700;
            font-size: 22px;
            color: rgba(255,220,220,0.9);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            white-space: nowrap;
        }
        .next-to {
            font-size: 16px;
            font-weight: 600;
            color: rgba(255,180,180,0.7);
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        .next-hash {
            font-family: 'Bebas Neue', sans-serif;
            font-size: 24px;
            color: #ff5555;
            letter-spacing: 1px;
        }

        .prog-wrap {
            margin-top: 10px;
            display: flex;
            flex-direction: column;
            gap: 6px;
        }
        .prog-header {
            display: flex;
            justify-content: space-between;
            align-items: baseline;
        }
        .prog-pct-text {
            font-family: 'Bebas Neue', sans-serif;
            font-size: 44px;
            color: var(--accent);
            letter-spacing: 2px;
        }
        .prog-bar-track {
            width: 100%;
            height: 5px;
            background: rgba(255,30,30,0.15);
            border-radius: 2px;
            overflow: hidden;
        }
        .prog-bar-fill {
            height: 100%;
            background: var(--accent);
            border-radius: 2px;
            transition: width 0.5s ease;
        }

        .ad-tag {
            font-family: 'Rajdhani', sans-serif;
            font-weight: 600;
            font-size: 26px;
            letter-spacing: 1.5px;
            text-transform: uppercase;
            color: #ffffff;
            margin-left: 14px;
            padding-left: 14px;
            border-left: 1px solid rgba(180, 60, 60, 0.3);
            white-space: nowrap;
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
            font-family: 'Rajdhani', sans-serif;
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
    </style>
</head>
<body>
    <div class="overlay-container">

        <!-- MAIN CARD -->
        <div class="card">

            <!-- Rank -->
            <div class="rank-row">
                <div class="rank-label unreal-rainbow" id="rankText">#- UNREAL</div>
            </div>

            <!-- ELO + session delta -->
            <div class="stat-row" id="rightSection">
                <div class="elo-val" id="eloText">-- ELO</div>
                <div class="sep"></div>
                <span class="session-delta session-zero" id="sessionText">+0 TODAY</span>
            </div>

            <!-- non-unreal progress -->
            <div class="prog-wrap" id="progSection" style="display:none;">
                <div class="prog-header">
                    <div class="prog-pct-text" id="progPct">0%</div>
                    <span class="session-delta session-zero" id="sessionText2">+0% TODAY</span>
                </div>
                <div class="prog-bar-track">
                    <div class="prog-bar-fill" id="progFill" style="width:0%"></div>
                </div>
            </div>

            <!-- next leaderboard position -->
            <div class="next-row" id="leftSubRow">
                <span class="next-gap-val" id="nextGap">-</span>
                <span class="next-to" id="nextToLabel">TO</span>
                <span class="next-hash" id="nextPos">#-</span>
                <span class="ad-tag">Use Code YourCode #ad</span>
            </div>
        </div>

        <!-- MODE BUTTONS -->
        <div class="error-text" id="errorText" style="display:none"></div>
        <div class="mode-bar" id="modeBar"></div>

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
        var curWin       = 'session';

        function $(s) { return document.querySelector(s); }

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

            var rankEl = $('#rankText');
            rankEl.textContent = d.rank_display;
            if (d.is_unreal) {
                rankEl.className = 'rank-label unreal-rainbow';
            } else {
                rankEl.className = 'rank-label';
            }

            var rightSection = $('#rightSection');
            var progSection  = $('#progSection');
            var leftSubRow   = $('#leftSubRow');

            if (d.is_unreal) {
                var eloStr = d.elo_text || '-- ELO';
                var eloEl  = $('#eloText');
                eloEl.textContent = eloStr;
                rightSection.style.display = '';
                progSection.style.display  = 'none';

                var gapEl  = $('#nextGap');
                var posEl  = $('#nextPos');
                if (d.next_gap && d.next_pos) {
                    gapEl.textContent = d.next_gap + ' ELO';
                    posEl.textContent = '#' + d.next_pos;
                    leftSubRow.style.visibility = 'visible';
                } else {
                    gapEl.textContent = '-';
                    posEl.textContent = '#-';
                    leftSubRow.style.visibility = 'hidden';
                }

                var sessEl = $('#sessionText');
                sessEl.textContent = d.session_text || '+0 TODAY';
                sessEl.className   = 'session-delta session-' + (d.session_sign || 'zero');
            } else {
                rightSection.style.display = 'none';
                progSection.style.display  = '';

                if (d.progression_pct !== null && d.progression_pct !== undefined) {
                    $('#progPct').textContent    = d.progression_pct + '%';
                    $('#progFill').style.width   = d.progression_pct + '%';
                }

                if (d.pct_to_next !== null && d.pct_to_next !== undefined) {
                    $('#nextGap').textContent = d.pct_to_next + '% TO NEXT';
                    $('#nextPos').textContent = '';
                    $('#nextToLabel').textContent = '';
                } else {
                    $('#nextGap').textContent     = '-';
                    $('#nextToLabel').textContent = 'TO';
                    $('#nextPos').textContent     = '#-';
                }
                leftSubRow.style.visibility = 'visible';

                var sess2 = $('#sessionText2');
                sess2.textContent = d.session_text || '+0% TODAY';
                sess2.className   = 'session-delta session-' + (d.session_sign || 'zero');
            }

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
            var url = '/data?window=' + curWin + (activeMode ? '&mode=' + encodeURIComponent(activeMode) : '');
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
