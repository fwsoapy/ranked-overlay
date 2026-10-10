#!/usr/bin/env python3
"""Checks the website's engine (src/web/engine.js) against the Python server.

The hosted overlay and the downloadable one have to show the same numbers. This
runs the real Python logic (a built server.py) and the JavaScript port through
the same scenarios, on the same OliTracker responses, and compares every field
of every /data answer. It also checks the website stays inside its request
budget, since the Cloudflare Worker it goes through has a daily allowance.

    python tests/web/test_engine.py

Needs Node.js on the PATH. The fixtures are real OliTracker responses with the
player names and account IDs replaced.
"""

import contextlib
import copy
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
FX = os.path.join(HERE, "fixtures")

T0 = 1791080000          # a fixed clock, in seconds
STEP_SECS = 400          # time between refreshes in a scenario
# The windows the Python designs ask for. The website's own ELO change
# windows (today / 12h / 24h, from match history) are checked separately in
# engine_harness.js, since the Python server has no equivalent.
WINDOWS = ["session", "season"]


def stats(name):
    with open(os.path.join(FX, f"stats-{name}.json"), encoding="utf-8") as f:
        return json.load(f)


def scenarios():
    acct = json.load(open(os.path.join(FX, "accounts.json")))
    out = []

    # BR Unreal inside the published board, Reload Unreal past the end of it,
    # Boxfights below Unreal. Then: climbs, drops, a promotion.
    a = stats("multi")
    b = copy.deepcopy(a)
    b["ranked_stats"]["ranked-squareclub"]["promotion_progression"] = 65
    b["ranked_stats"]["ranked-br-combined"]["current_unreal_placement"] = 4401
    # Reload games: match_history calls the mode ranked-blastberry-combined
    # while ranked_stats calls it ranked_blastberry_build.
    b.setdefault("match_history", [{"date": "x", "elo": {}, "matches": []}])
    b["match_history"][0].setdefault("matches", []).append(
        {"matches": 4, "wins": 3, "kills": 20, "last_modified": T0 + 100,
         "top_3_5_10": 3, "top_6_12_25": 4, "playlist_id": "playlist_habanero_nobuild_piperboot_duos",
         "ranked_data": {"ranking_id": "ranked-blastberry-combined"}})
    # The totals are what the Record counts: 4 Reload games, 3 won, in duos...
    play(b, "reload", "duos", 3, 4, 20, 3, 4, T0 + 100)
    # ...and 2 in BR that history never heard of: a win and a loss, in solos.
    play(b, "ranked", "solo", 1, 2, 9, 1, 2, T0 + 100)
    # Boxfights has no totals, so only history can count it.
    b["match_history"][0]["matches"].append(
        {"matches": 3, "wins": 1, "kills": 5, "last_modified": T0 + 100,
         "top_3_5_10": 1, "top_6_12_25": 2, "playlist_id": "playlist_squareclub",
         "ranked_data": {"ranking_id": "ranked-squareclub"}})
    c = copy.deepcopy(b)
    c["ranked_stats"]["ranked-squareclub"]["division"] = 4
    c["ranked_stats"]["ranked-squareclub"]["promotion_progression"] = 5
    c["ranked_stats"]["ranked-br-combined"]["current_unreal_placement"] = 5600
    out.append(("multi", acct["multi"], [a, b, c]))

    # #1, with OliTracker's own ELO and a real match history (12h/24h windows).
    a = stats("top1")
    b = copy.deepcopy(a)
    b["ranked_stats"]["ranked-br-combined"]["elo"] -= 40
    # Games after the overlay started, for the Record design's wins and losses.
    # Only the season totals move; match_history trails behind, as it does live.
    play(b, "ranked", "duos", 1, 3, 11, 2, 3, T0 + 100)
    c = copy.deepcopy(b)
    play(c, "ranked", "solo", 0, 2, 1, 1, 2, T0 + 500)
    out.append(("top1", acct["top1"], [a, b, c]))

    # The very last row of the board, then a new season wiping the baselines.
    a = stats("tail")
    b = copy.deepcopy(a)
    b["ranked_stats"]["ranked-br-combined"]["current_unreal_placement"] = 9950
    c = copy.deepcopy(b)
    c["stats"]["seasonal"]["all"]["both"]["overall"]["matches_played"] = 1
    c["ranked_stats"]["ranked-br-combined"]["current_unreal_placement"] = 9990
    out.append(("tail", acct["tail"], [a, b, c]))

    out.append(("top100", acct["top100"], [stats("top100")]))
    out.append(("noranked", acct["noranked"], [stats("noranked")]))

    # Playlists this build has never heard of, odd values, Python's rounding.
    synth = {
        "stats": {"seasonal": {"all": {"both": {"overall": {"matches_played": 300}}},
                               "og": {"both": {"overall": {"wins": 3, "matches_played": 8, "kills": 1}}}},
                  "lifetime": {}},
        "ranked_stats": {
            "ranked-og-combined": {"division": 17, "promotion_progression": 12.5, "elo": None,
                                   "current_unreal_placement": None},
            "ranked-zb-solo": {"division": 2, "promotion_progression": 99.5, "elo": None},
            "ranked-rocket-racing": {"division": 21, "current_unreal_placement": 120, "elo": None},
        },
        "match_history": [{"date": "x", "elo": {}, "matches": [
            {"wins": 1, "matches": 3, "kills": 7, "last_modified": T0 - 100,
             "ranked_data": {"ranking_id": "ranked-zb-solo", "elo": None}}]}],
    }
    s2 = copy.deepcopy(synth)
    s2["ranked_stats"]["ranked-og-combined"]["promotion_progression"] = 11.5
    s2["ranked_stats"]["ranked-zb-solo"]["division"] = 3
    s2["ranked_stats"]["ranked-zb-solo"]["promotion_progression"] = 0.5
    out.append(("synthetic", "0123456789abcdef0123456789abcdef", [synth, s2]))

    # Nothing usable under ranked_stats: modes found by walking the response.
    walk = {"stats": {}, "ranked_stats": {"weird": {"x": 1}},
            "profile": {"modes": [{"division": 9, "promotion_progression": 30, "elo": None},
                                  {"inner": {"division": 21, "unreal_placement": 5, "elo": 4000}}]},
            "match_history": [{"matches": [{"ranked_data": {"division": 1, "elo": 3}}]}]}
    out.append(("walk", "fedcba9876543210fedcba9876543210", [walk]))
    return out


def play(data, bucket, size, wins, matches, kills, top1, top2, at):
    """Add games to a season-totals bucket, the way OliTracker's totals move."""
    both = data["stats"]["seasonal"][bucket]["both"]
    for part in (both["overall"], both.setdefault(size, {"matches_played": 0, "wins": 0, "kills": 0,
                                                         "top_3_5_10": 0, "top_6_12_25": 0})):
        part["wins"] = part.get("wins", 0) + wins
        part["matches_played"] = part.get("matches_played", 0) + matches
        part["kills"] = part.get("kills", 0) + kills
        part["top_3_5_10"] = part.get("top_3_5_10", 0) + top1
        part["top_6_12_25"] = part.get("top_6_12_25", 0) + top2
        part["last_modified"] = at


def leaderboard_file(url):
    tail = url.split("/ranked/")[1]
    slug, _, query = tail.partition("?")
    page = int(query.split("=")[1]) if query else 1
    path = os.path.join(FX, f"lb-{slug}-{page}.html")
    if not os.path.exists(path):
        raise SystemExit(f"missing fixture {os.path.relpath(path, ROOT)}")
    return path


def load_server(account_id):
    folder = tempfile.mkdtemp()
    shutil.copy(os.path.join(ROOT, "archive", "desktop", "Classic", "server.py"), folder)
    with open(os.path.join(folder, "config.json"), "w") as f:
        json.dump({"epic_account_id": account_id, "epic_username": "tester", "auto_update": "off"}, f)
    spec = importlib.util.spec_from_file_location("server_" + account_id, os.path.join(folder, "server.py"))
    mod = importlib.util.module_from_spec(spec)
    with contextlib.redirect_stdout(io.StringIO()):
        spec.loader.exec_module(mod)
    return mod


def run_python(all_scenarios):
    result = {}
    for name, account_id, steps in all_scenarios:
        mod = load_server(account_id)
        clock = [T0]
        mod.time = types.SimpleNamespace(time=lambda: clock[0], sleep=lambda s: None)
        mod.SESSION_START = T0
        current = {}
        mod._http_get_json = lambda url, timeout=15: copy.deepcopy(current["data"])
        mod._http_get_text = lambda url, timeout=15: open(leaderboard_file(url), encoding="utf-8").read()
        out = []
        for i, data in enumerate(steps):
            clock[0] = T0 + i * STEP_SECS
            current["data"] = data
            with contextlib.redirect_stdout(io.StringIO()):
                mod.refresh_once()
            keys = [None] + [mod._path_str(p) for p, _ in mod._last_modes]
            for k in keys:
                for w in WINDOWS:
                    out.append([i, k, w, json.loads(json.dumps(mod.snapshot(w, k)))])
        result[name] = out
    return result


def main():
    all_scenarios = scenarios()
    expected = run_python(all_scenarios)

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"T0": T0, "step": STEP_SECS, "windows": WINDOWS, "fixtures": FX,
                   "scenarios": all_scenarios}, f)
        job = f.name
    try:
        proc = subprocess.run(["node", os.path.join(HERE, "engine_harness.js"), job],
                              capture_output=True, text=True, check=False)
    finally:
        os.unlink(job)
    if proc.returncode != 0:
        print(proc.stdout + proc.stderr)
        return 1
    report = json.loads(proc.stdout)
    actual = report["snapshots"]

    diffs = compared = 0
    for name, rows in expected.items():
        got = actual.get(name, [])
        if len(got) != len(rows):
            print(f"{name}: {len(rows)} snapshots from Python, {len(got)} from JS")
            diffs += 1
        for want, have in zip(rows, got):
            if want[:3] != have[:3]:
                print(f"{name}: out of step at {want[:3]} vs {have[:3]}")
                diffs += 1
                continue
            for key in sorted(set(want[3]) | set(have[3])):
                compared += 1
                a, b = json.dumps(want[3].get(key)), json.dumps(have[3].get(key))
                if a != b:
                    diffs += 1
                    if diffs <= 40:
                        print(f"{name} step {want[0]} mode={want[1]} window={want[2]} {key}: python={a} js={b}")

    checks_ok = True
    for name, ok, got, want in report["checks"]:
        checks_ok &= ok
        if not ok:
            print(f"FAIL {name}: got {json.dumps(got)}, want {json.dumps(want)}")
    print(f"website checks: {sum(1 for c in report['checks'] if c[1])}/{len(report['checks'])} passed")

    budget_ok = True
    for label, used, limit in report["budget"]:
        flag = "ok" if used <= limit else "OVER"
        budget_ok &= used <= limit
        print(f"requests/hour {label}: {used:.1f} (limit {limit}) {flag}")

    print(f"compared {compared} fields across {sum(len(r) for r in expected.values())} answers: {diffs} differences")
    return 0 if diffs == 0 and budget_ok and checks_ok else 1


if __name__ == "__main__":
    sys.exit(main())
