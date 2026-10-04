/*
 * Fortnite Ranked Overlay - browser engine.
 *
 * The hosted version of the overlay runs entirely in the page. This file is the
 * JavaScript twin of src/core_head.py: it reads the same OliTracker data, makes
 * the same decisions, and hands the designs exactly the same /data answer the
 * Python server does, so the eight designs work unchanged.
 *
 * Every request goes through a Cloudflare Worker (web/worker.js) because
 * OliTracker sends no CORS headers. The Worker has a daily request allowance,
 * so this file works hard to ask as little as possible:
 *
 *   - OliTracker keeps each profile for three minutes and only refreshes it
 *     when someone asks after that (OLI_CACHE_MS). Asking any sooner just
 *     returns the same numbers, so stats are fetched right after OliTracker's
 *     copy expires: about every three minutes, and every one of those fetches
 *     brings back fresh data. Never more than once a minute (REFRESH_MIN_MS).
 *   - Nothing is fetched while the overlay is hidden (an OBS source that isn't
 *     visible in any scene, or a background browser tab).
 *   - While OBS is open but not streaming or recording, it slows to every ten
 *     minutes (IDLE_MS).
 *   - Every overlay on the page's origin shares one cache in localStorage, so
 *     the same overlay in five scenes, or a scene switch that reloads it, does
 *     not cost five requests.
 *   - The leaderboard (only needed for Unreal) is read for the mode on screen
 *     only, and only re-read after a new match or every five minutes.
 *   - Failures back off up to ten minutes instead of retrying every minute.
 *
 * The Python names are kept in comments next to each port so the two files can
 * be compared side by side.
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.RankedEngine = api;
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var VERSION = '3.0.0';
  var DEFAULT_PROXY = 'https://ranked-proxy.fwsoapy-gaming.workers.dev';

  var REFRESH_MIN_MS = 60 * 1000;        // fastest anyone can poll stats
  var REFRESH_MAX_MS = 15 * 60 * 1000;
  var IDLE_MS = 10 * 60 * 1000;          // OBS open, not live
  var OLI_CACHE_MS = 180 * 1000;         // how long OliTracker keeps a profile
  var ALIGN_SLACK_MS = 3 * 1000;         // ask this long after it expires
  var STALE_RETRY_MS = 30 * 1000;        // came back unchanged: try again soon
  var SKEW_STEP_MS = 10 * 1000;          // ...and wait this much longer next time
  var SKEW_MAX_MS = 10 * 60 * 1000;
  var BACKOFF_MAX_MS = 10 * 60 * 1000;
  var LOOKUP_TTL_MS = 10 * 60 * 1000;    // leaderboard re-read when nothing changed
  var LOOKUP_HOT_MS = 60 * 1000;         // ...and right after a match
  var HOT_WINDOW_MS = 5 * 60 * 1000;     // how long "right after a match" lasts
  var EMPTY_PAGE_TTL_MS = 30 * 60 * 1000; // pages past the end of the board
  var SESSION_GAP_MS = 60 * 60 * 1000;   // a gap this long starts a new session
  var FETCH_TIMEOUT_MS = 15 * 1000;
  var LOOKUP_WAIT_MS = 6 * 1000;         // how long a mode switch waits for ELO

  // LEADERBOARD_* in core_head.py
  var LEADERBOARD_PAGE_SIZE = 100;
  var LEADERBOARD_PAGE_LIMIT = 100000;
  var LEADERBOARD_PAGES_BACK = 4;
  // Only used to order the mode buttons before a mode's leaderboard has been
  // read. The real lookup never assumes this depth.
  var LEADERBOARD_ASSUMED_DEPTH = 10000;

  var DIVISION_NAMES = {
    0: 'BRONZE I', 1: 'BRONZE II', 2: 'BRONZE III',
    3: 'SILVER I', 4: 'SILVER II', 5: 'SILVER III',
    6: 'GOLD I', 7: 'GOLD II', 8: 'GOLD III',
    9: 'PLATINUM I', 10: 'PLATINUM II', 11: 'PLATINUM III',
    12: 'DIAMOND I', 13: 'DIAMOND II', 14: 'DIAMOND III',
    15: 'ELITE I', 16: 'ELITE II', 17: 'ELITE III',
    18: 'CHAMPION I', 19: 'CHAMPION II', 20: 'CHAMPION III',
    21: 'UNREAL'
  };

  var MODE_LABELS = {
    'ranked-br-combined': 'BR',
    'ranked_blastberry_build': 'Reload',
    'ranked_squareclub': 'Boxfights',
    'ranked-squareclub': 'Boxfights',
    'ranked-rocket-racing': 'Racing',
    'delmar-competitive': 'Blitz'
  };

  var MODE_STAT_PATH = {
    'ranked-br-combined': ['ranked'],
    'ranked-blastberry-build': ['reload'],
    'ranked-squareclub': null,
    'delmar-competitive': null,
    'ranked-rocket-racing': null
  };

  var LABEL_NOISE = { ranked: 1, combined: 1, build: 1, competitive: 1, playlist: 1 };
  var BUCKET_STOPWORDS = { ranked: 1, combined: 1, build: 1, br: 1, all: 1, competitive: 1 };
  var WINDOW_SECS = { '12h': 43200, '24h': 86400 };

  function emptyStats() {
    return { wins: 0, losses: 0, kills: 0, matches: 0, kd: null, wr: null };
  }

  /* ------------------------------------------------------------------
   * Python semantics the port depends on
   * ---------------------------------------------------------------- */

  var hasOwn = Object.prototype.hasOwnProperty;

  function isDict(v) {
    return v !== null && typeof v === 'object' && !Array.isArray(v);
  }

  // dict.get(): None for a missing key, AttributeError on a non-dict.
  function dget(o, k) {
    if (!isDict(o)) throw new TypeError('not a dict');
    return hasOwn.call(o, k) ? o[k] : null;
  }

  function truthy(v) {
    if (Array.isArray(v)) return v.length > 0;
    if (isDict(v)) return Object.keys(v).length > 0;
    return !!v;
  }

  // `for x in (value or [])`
  function iter(v) {
    if (!truthy(v)) return [];
    if (Array.isArray(v)) return v;
    if (isDict(v)) return Object.keys(v);
    if (typeof v === 'string') return v.split('');
    throw new TypeError('not iterable');
  }

  // Python's round(): ties go to the even neighbour.
  function pyRound(v) {
    var f = Math.floor(v);
    var diff = v - f;
    if (diff === 0.5) return f % 2 === 0 ? f : f + 1;
    return Math.round(v);
  }

  // round(v, places), done on the exact decimal value of the double the way
  // Python does it, so 0.125 -> 0.12 and 1.115 -> 1.11 match the server.
  function pyRoundN(v, places) {
    if (!isFinite(v)) return v;
    var neg = v < 0;
    var s = Math.abs(v).toFixed(Math.min(100, places + 25));
    var dot = s.indexOf('.');
    var whole = s.slice(0, dot);
    var frac = s.slice(dot + 1);
    var keep = whole + frac.slice(0, places);
    var rest = frac.slice(places);
    var up;
    if (rest.charAt(0) > '5') up = true;
    else if (rest.charAt(0) < '5') up = false;
    else if (/[1-9]/.test(rest.slice(1))) up = true;
    else up = Number(keep.charAt(keep.length - 1)) % 2 === 1;   // exact tie: to even
    var digits = keep.split('').map(Number);
    if (up) {
      for (var i = digits.length - 1; i >= 0; i--) {
        if (digits[i] === 9) { digits[i] = 0; } else { digits[i] += 1; break; }
        if (i === 0) digits.unshift(1);
      }
    }
    var str = digits.join('');
    var intLen = str.length - places;
    var out = Number(str.slice(0, intLen) + (places ? '.' + str.slice(intLen) : ''));
    return neg && out !== 0 ? -out : out;
  }

  // int(value or 0)
  function pyInt(v) {
    if (!truthy(v)) return 0;
    if (typeof v === 'number') return Math.trunc(v);
    if (v === true) return 1;
    if (typeof v === 'string' && /^\s*[-+]?\d+\s*$/.test(v)) return parseInt(v, 10);
    throw new TypeError('invalid int');
  }

  // core_head.py: _num()
  function num(v) {
    if (typeof v === 'boolean') return null;
    if (typeof v === 'number' && isFinite(v)) return pyRound(v);
    return null;
  }

  function pathStr(path) {
    return path.map(String).join('/').toLowerCase();
  }

  function normalizeModeKey(key) {
    return String(truthy(key) ? key : '').toLowerCase().replace(/_/g, '-');
  }

  function compareTuples(a, b) {
    for (var i = 0; i < a.length; i++) {
      if (a[i] < b[i]) return -1;
      if (a[i] > b[i]) return 1;
    }
    return 0;
  }

  // max(items, key=fn): the first of the largest.
  function maxBy(items, keyFn) {
    var best = null, bestKey = null;
    for (var i = 0; i < items.length; i++) {
      var k = keyFn(items[i]);
      if (best === null || compareTuples(k, bestKey) > 0) { best = items[i]; bestKey = k; }
    }
    return best;
  }

  /* ------------------------------------------------------------------
   * Reading OliTracker's stats
   * ---------------------------------------------------------------- */

  function unrealPlacement(obj) {
    if (!isDict(obj)) return null;
    var v = num(obj.current_unreal_placement);
    return v !== null ? v : num(obj.unreal_placement);
  }

  function divisionName(div) {
    if (div === null || div === undefined) return null;
    return hasOwn.call(DIVISION_NAMES, div) ? DIVISION_NAMES[div] : 'DIVISION ' + div;
  }

  function modeLabel(key) {
    if (hasOwn.call(MODE_LABELS, key)) return MODE_LABELS[key];
    var lower = key.toLowerCase();
    if (lower.indexOf('squareclub') >= 0 || lower.indexOf('boxfight') >= 0) return 'Boxfights';
    if (lower.indexOf('blastberry') >= 0 || lower.indexOf('reload') >= 0) return 'Reload';
    if (lower.indexOf('br-combined') >= 0 || lower.indexOf('br_combined') >= 0) return 'BR';
    if (lower.indexOf('rocket') >= 0 || lower.indexOf('racing') >= 0) return 'Racing';
    var words = lower.split(/[-_\s]+/).filter(function (w) { return w && !hasOwn.call(LABEL_NOISE, w); });
    if (!words.length) return key;
    return words.map(function (w) {
      return w.length <= 3 ? w.toUpperCase() : w.charAt(0).toUpperCase() + w.slice(1);
    }).join(' ');
  }

  function looksLikeRankedMode(d) {
    return isDict(d)
      && (hasOwn.call(d, 'elo') || hasOwn.call(d, 'current_unreal_placement') || hasOwn.call(d, 'unreal_placement'))
      && (hasOwn.call(d, 'division') || hasOwn.call(d, 'promotion_progression'));
  }

  function findRankedModes(data) {
    var modes = [];
    if (isDict(data)) {
      var rs = data.ranked_stats;
      if (isDict(rs)) {
        Object.keys(rs).forEach(function (key) {
          if (looksLikeRankedMode(rs[key])) modes.push([[key], rs[key]]);
        });
      }
    }
    if (modes.length) return modes;

    (function walk(obj, path) {
      if (isDict(obj)) {
        if (looksLikeRankedMode(obj)) modes.push([path.slice(), obj]);
        Object.keys(obj).forEach(function (k) {
          if (k === 'match_history') return;
          walk(obj[k], path.concat([String(k)]));
        });
      } else if (Array.isArray(obj)) {
        obj.forEach(function (v, i) { walk(v, path.concat([i])); });
      }
    })(data, []);
    return modes;
  }

  function pickModeByKey(modes, modeKey) {
    var want = modeKey.toLowerCase();
    for (var i = 0; i < modes.length; i++) {
      if (pathStr(modes[i][0]) === want) return modes[i];
    }
    return [null, null];
  }

  function extractElo(obj) { return num(obj.elo); }

  function extractLabel(obj) {
    if (unrealPlacement(obj) !== null) return 'UNREAL';
    return divisionName(num(obj.division));
  }

  function progressPoints(obj) {
    if (unrealPlacement(obj) !== null) return null;
    var prog = num(obj.promotion_progression);
    if (prog === null) return null;
    var div = num(obj.division) || 0;
    return div * 100 + prog;
  }

  function extractProgression(obj) {
    if (unrealPlacement(obj) !== null) return null;
    return num(obj.promotion_progression);
  }

  function detectRankingId(data, preferred, override) {
    if (preferred) return preferred;
    var o = String(override || '').trim();
    if (o) return o;
    var tally = {}, order = [];
    iter(dget(data, 'match_history')).forEach(function (day) {
      iter(dget(day, 'matches')).forEach(function (grp) {
        var rd = dget(grp, 'ranked_data');
        rd = truthy(rd) ? rd : {};
        var rid = dget(rd, 'ranking_id');
        if (truthy(rid)) {
          if (!hasOwn.call(tally, rid)) { tally[rid] = 0; order.push(rid); }
          tally[rid] += pyInt(dget(grp, 'matches'));
        }
      });
    });
    if (!order.length) return null;
    var best = order[0];
    order.forEach(function (rid) { if (tally[rid] > tally[best]) best = rid; });
    return best;
  }

  function modeStatPath(rankingId, available) {
    if (!rankingId) return ['ranked'];
    var key = normalizeModeKey(rankingId);
    if (hasOwn.call(MODE_STAT_PATH, key)) return MODE_STAT_PATH[key];
    var label = modeLabel(rankingId);
    if (label === 'Reload') return ['reload'];
    if (label === 'BR') return ['ranked'];
    var tokens = key.split(/[-_]/);
    for (var i = 0; i < tokens.length; i++) {
      var t = tokens[i];
      if (t.length >= 2 && !hasOwn.call(BUCKET_STOPWORDS, t) && available && available[t]) return [t];
    }
    return null;
  }

  function statBlock(data, timeframe, rankingId) {
    var available = {};
    try {
      var tf = data.stats[timeframe];
      if (!isDict(tf)) throw new TypeError();
      Object.keys(tf).forEach(function (k) { available[k] = true; });
    } catch (e) { available = {}; }
    var pathKey = modeStatPath(rankingId, available);
    if (pathKey === null) return null;
    var block;
    try {
      block = data.stats[timeframe];
      for (var i = 0; i < pathKey.length; i++) {
        if (!isDict(block) || !hasOwn.call(block, pathKey[i])) return null;
        block = block[pathKey[i]];
      }
      if (!isDict(block) || !isDict(block.both) || !hasOwn.call(block.both, 'overall')) return null;
      return block.both.overall;
    } catch (e) {
      return null;
    }
  }

  function totals(wins, matches, kills) {
    var losses = matches - wins;
    return {
      wins: wins, losses: losses, kills: kills, matches: matches,
      kd: losses > 0 ? pyRoundN(kills / losses, 2) : null,
      wr: matches > 0 ? pyRoundN(wins / matches * 100, 1) : null
    };
  }

  function statsFromHistory(data, rankingId) {
    var wins = 0, matches = 0, kills = 0;
    var want = normalizeModeKey(rankingId);
    iter(dget(data, 'match_history')).forEach(function (day) {
      iter(dget(day, 'matches')).forEach(function (grp) {
        var rd = dget(grp, 'ranked_data');
        rd = truthy(rd) ? rd : {};
        if (normalizeModeKey(dget(rd, 'ranking_id')) !== want) return;
        wins += pyInt(dget(grp, 'wins'));
        matches += pyInt(dget(grp, 'matches'));
        kills += pyInt(dget(grp, 'kills'));
      });
    });
    return totals(wins, matches, kills);
  }

  function blockTotals(blk) {
    return totals(pyInt(dget(blk, 'wins')), pyInt(dget(blk, 'matches_played')), pyInt(dget(blk, 'kills')));
  }

  function seasonalRankedStats(data, rankingId) {
    try {
      var blk = statBlock(data, 'seasonal', rankingId);
      if (blk === null) return rankingId ? statsFromHistory(data, rankingId) : emptyStats();
      return blockTotals(blk);
    } catch (e) {
      return emptyStats();
    }
  }

  function lifetimeRankedStats(data, rankingId) {
    try {
      var blk = statBlock(data, 'lifetime', rankingId);
      if (blk === null) return emptyStats();
      return blockTotals(blk);
    } catch (e) {
      return emptyStats();
    }
  }

  function computeWindowedStats(data, spec, rankingId, nowSec, sessionStart, override) {
    if (data === null || data === undefined) return emptyStats();
    if (spec === 'season') return seasonalRankedStats(data, rankingId);
    if (spec === 'lifetime') return lifetimeRankedStats(data, rankingId);
    var cutoff = spec === 'session' ? sessionStart : nowSec - (WINDOW_SECS[spec] || 86400);
    var rid = detectRankingId(data, rankingId, override);
    var wins = 0, matches = 0, kills = 0;
    iter(dget(data, 'match_history')).forEach(function (day) {
      iter(dget(day, 'matches')).forEach(function (grp) {
        if ((dget(grp, 'last_modified') || 0) < cutoff) return;
        var rd = dget(grp, 'ranked_data');
        rd = truthy(rd) ? rd : {};
        if (rid && dget(rd, 'ranking_id') !== rid) return;
        wins += pyInt(dget(grp, 'wins'));
        matches += pyInt(dget(grp, 'matches'));
        kills += pyInt(dget(grp, 'kills'));
      });
    });
    return totals(wins, matches, kills);
  }

  /* ------------------------------------------------------------------
   * Leaderboard
   * ---------------------------------------------------------------- */

  // Same anchors as _LB_ROW / _LB_RANK / _LB_ACCT / _LB_ELO.
  var LB_ROW = /<tr[^>]*leaderboard-player-row[\s\S]*?<\/tr>/g;
  var LB_RANK = /leaderboard-rank-cell[^"]*"[^>]*>\s*#?([\d,]+)/;
  var LB_ACCT = /\/stats\/([0-9a-fA-F]{16,})/;
  var LB_ELO = /leaderboard-wins-cell[\s\S]*?<p[^>]*>\s*([\d,]+)\s*<\/p>/;

  function parseIntLoose(text) {
    var s = String(text).replace(/,/g, '').trim();
    return /^[-+]?\d+$/.test(s) ? parseInt(s, 10) : null;
  }

  function parseLeaderboardRows(html) {
    var rows = [];
    var chunks = String(html).match(LB_ROW) || [];
    chunks.forEach(function (chunk) {
      var rank = LB_RANK.exec(chunk);
      var elo = LB_ELO.exec(chunk);
      if (!rank || !elo) return;
      var placement = parseIntLoose(rank[1]);
      var eloValue = parseIntLoose(elo[1]);
      if (placement === null || eloValue === null) return;
      var acct = LB_ACCT.exec(chunk);
      rows.push({ placement: placement, elo: eloValue, account_id: acct ? acct[1].toLowerCase() : null });
    });
    rows.sort(function (a, b) { return a.placement - b.placement; });
    return rows;
  }

  function slugForMode(path) {
    var ps = pathStr(path);
    var has = function (s) { return ps.indexOf(s) >= 0; };
    if (has('squareclub') || has('boxfight')) return null;
    if (has('reload') || has('blastberry')) {
      if (has('zero') || has('zb') || has('nobuild')) return 'reload-zb';
      return 'reload';
    }
    if (has('rocket') || has('racing')) return 'rocket-racing';
    if (ps.split('-').indexOf('og') >= 0 || ps.split('_').indexOf('og') >= 0) return 'og';
    if (has('zero') || has('zb') || has('nobuild')) return 'zero-build';
    if (has('br-combined') || has('br_combined') || has('battle')) return 'battle-royale';
    return null;
  }

  function findOwnRow(rows, placement, accountId) {
    if (!rows.length) return null;
    var acct = String(accountId || '').toLowerCase();
    var i;
    if (acct) {
      for (i = 0; i < rows.length; i++) if (rows[i].account_id === acct) return rows[i];
    }
    for (i = 0; i < rows.length; i++) if (rows[i].placement === placement) return rows[i];
    var nearest = rows[0];
    for (i = 1; i < rows.length; i++) {
      if (Math.abs(rows[i].placement - placement) < Math.abs(nearest.placement - placement)) nearest = rows[i];
    }
    return nearest;
  }

  // lookup_leaderboard_elo(): [own ELO, target placement, ELO gap]
  function lookupLeaderboardElo(path, placement, accountId, enabled, getPage) {
    var none = [null, null, null];
    if (!enabled || !placement || placement < 1) return Promise.resolve(none);
    var slug = slugForMode(path);
    if (slug === null) return Promise.resolve(none);

    var page = Math.floor((placement - 1) / LEADERBOARD_PAGE_SIZE) + 1;
    return getPage(slug, page).then(function (rows) {
      var me = findOwnRow(rows, placement, accountId);
      if (me === null) return none;
      var myElo = me.elo, myPlace = me.placement;

      function step(i, rows) {
        var above = rows.filter(function (r) { return r.placement < myPlace && r.elo > myElo; });
        if (above.length) {
          var target = maxBy(above, function (r) { return [r.placement]; });
          return Promise.resolve([myElo, target.placement, target.elo - myElo]);
        }
        if (page - 1 < 1 || i === LEADERBOARD_PAGES_BACK - 1) return Promise.resolve([myElo, null, null]);
        page -= 1;
        return getPage(slug, page).then(function (prev) {
          if (!prev.length) return [myElo, null, null];
          return step(i + 1, prev);
        });
      }
      return step(0, rows);
    });
  }

  /* ------------------------------------------------------------------
   * ELO history windows
   * ---------------------------------------------------------------- */

  function seasonFingerprint(data) {
    try {
      var v = data.stats.seasonal.all.both.overall.matches_played;
      if (typeof v === 'number') return Math.trunc(v);
      if (typeof v === 'string' && /^\s*[-+]?\d+\s*$/.test(v)) return parseInt(v, 10);
      if (typeof v === 'boolean') return v ? 1 : 0;
      return null;
    } catch (e) {
      return null;
    }
  }

  function eloSeries(data, rankingId) {
    var points = [];
    var want = rankingId ? normalizeModeKey(rankingId) : null;
    var hist = isDict(data) ? data.match_history : null;
    iter(hist).forEach(function (day) {
      iter(dget(day, 'matches')).forEach(function (grp) {
        var rd = dget(grp, 'ranked_data');
        rd = truthy(rd) ? rd : {};
        if (want && normalizeModeKey(dget(rd, 'ranking_id')) !== want) return;
        var elo = num(dget(rd, 'elo'));
        var ts = dget(grp, 'last_modified');
        if (elo !== null && truthy(ts)) points.push([Math.trunc(Number(ts)), elo]);
      });
    });
    points.sort(function (a, b) { return a[0] - b[0] || a[1] - b[1]; });
    return points;
  }

  function earliestDayStartElo(data, rankingId) {
    var want = rankingId ? normalizeModeKey(rankingId) : null;
    var days = iter(isDict(data) ? data.match_history : null).slice().reverse();
    for (var i = 0; i < days.length; i++) {
      var elo = dget(days[i], 'elo');
      elo = truthy(elo) ? elo : {};
      var keys = Object.keys(elo);
      for (var j = 0; j < keys.length; j++) {
        if (want && normalizeModeKey(keys[j]) !== want) continue;
        var value = elo[keys[j]];
        var start = num(dget(truthy(value) ? value : {}, 'start'));
        if (start !== null) return start;
      }
    }
    return null;
  }

  function windowedEloDelta(data, window, currentElo, rankingId, nowSec, override) {
    if (currentElo === null || data === null || data === undefined) return null;
    var secs = WINDOW_SECS[window];
    if (secs === undefined) return null;
    var cutoff = nowSec - secs;
    var rid = rankingId || detectRankingId(data, null, override);
    var points = eloSeries(data, rid);
    if (!points.length) return null;
    var baseline = null;
    for (var i = 0; i < points.length; i++) {
      if (points[i][0] <= cutoff) baseline = points[i][1];
      else break;
    }
    if (baseline === null) {
      baseline = earliestDayStartElo(data, rid);
      if (baseline === null) baseline = points[0][1];
    }
    return currentElo - baseline;
  }

  /* ------------------------------------------------------------------
   * The engine: state, refreshing, and the /data answer
   * ---------------------------------------------------------------- */

  function parseConfig(search) {
    var q = new URLSearchParams(search || '');
    var refreshSec = parseInt(q.get('refresh') || '', 10);
    var refreshMs = isFinite(refreshSec) ? refreshSec * 1000 : REFRESH_MIN_MS;
    refreshMs = Math.min(REFRESH_MAX_MS, Math.max(REFRESH_MIN_MS, refreshMs));
    var hex = function (v) {
      v = String(v || '').replace('#', '').trim();
      return /^[0-9a-fA-F]{6}$/.test(v) ? v.toLowerCase() : null;
    };
    var proxy = String(q.get('proxy') || '').trim().replace(/\/+$/, '');
    if (!/^https:\/\/[^\s/]+(\/[^\s]*)?$/.test(proxy)) proxy = DEFAULT_PROXY;
    return {
      accountId: String(q.get('id') || '').trim().toLowerCase(),
      username: String(q.get('name') || '').trim(),
      creatorCode: String(q.get('code') || '').trim(),
      modeHint: String(q.get('mode') || '').trim(),
      refreshMs: refreshMs,
      nextLookup: q.get('next') !== '0',
      color: hex(q.get('color')),
      labelColor: hex(q.get('label')),
      demo: q.get('demo') === '1',
      proxy: proxy
    };
  }

  function validAccountId(id) {
    return /^[0-9a-f]{32}$/.test(String(id || ''));
  }

  // A localStorage wrapper that never throws and falls back to memory, so the
  // overlay still works (just without the shared cache) if storage is blocked.
  function makeStore(backing) {
    var mem = {};
    return {
      get: function (key) {
        var raw = null;
        try { raw = backing ? backing.getItem(key) : null; } catch (e) { raw = null; }
        if (raw === null && hasOwn.call(mem, key)) raw = mem[key];
        if (raw === null) return null;
        try { return JSON.parse(raw); } catch (e) { return null; }
      },
      set: function (key, value) {
        var raw = JSON.stringify(value);
        mem[key] = raw;
        try { if (backing) backing.setItem(key, raw); } catch (e) { /* full or blocked */ }
      },
      remove: function (key) {
        delete mem[key];
        try { if (backing) backing.removeItem(key); } catch (e) { /* blocked */ }
      },
      keys: function () {
        var out = Object.keys(mem);
        try {
          if (backing) for (var i = 0; i < backing.length; i++) {
            var k = backing.key(i);
            if (out.indexOf(k) < 0) out.push(k);
          }
        } catch (e) { /* blocked */ }
        return out;
      }
    };
  }

  function HttpError(status, message) {
    var e = new Error(message || 'HTTP ' + status);
    e.status = status;
    return e;
  }

  function browserFetchText(url) {
    var ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null;
    var timer = ctrl ? setTimeout(function () { ctrl.abort(); }, FETCH_TIMEOUT_MS) : null;
    return fetch(url, { cache: 'no-store', signal: ctrl ? ctrl.signal : undefined })
      .then(function (r) {
        return r.text().then(function (text) {
          if (!r.ok) {
            var msg = null;
            try { msg = JSON.parse(text).error; } catch (e) { /* not JSON */ }
            throw HttpError(r.status, msg);
          }
          return text;
        });
      })
      .finally(function () { if (timer) clearTimeout(timer); });
  }

  function describeError(e) {
    var status = e && e.status;
    if (status === 403) return "this site isn't allowed to use the proxy";
    if (status === 429) return 'proxy is busy, trying again shortly';
    if (status) return 'HTTP ' + status + ' from OliTracker';
    if (e && e.name === 'AbortError') return 'request failed: timed out';
    if (e && e.message === 'bad json') return 'request failed: response was not JSON';
    return "request failed: couldn't reach the proxy";
  }

  /*
   * createEngine(opts)
   *   cfg        parseConfig() result
   *   store      makeStore() result (shared between overlays)
   *   fetchText  url -> Promise<string>, rejects with .status on HTTP errors
   *   now        () -> milliseconds
   */
  function createEngine(opts) {
    var cfg = opts.cfg;
    var store = opts.store || makeStore(null);
    var fetchText = opts.fetchText || browserFetchText;
    var clock = opts.now || function () { return Date.now(); };
    var nowSec = function () { return Math.floor(clock() / 1000); };

    var id = cfg.accountId;
    var K = {
      stats: 'ro1:stats:' + id,
      lookup: 'ro1:lookup:' + id,
      session: 'ro1:session:' + id,
      busy: 'ro1:busy:' + id
    };

    var requests = 0;
    var listeners = [];
    var raw = null, rawAt = 0, modes = [];
    var lookups = {};          // mode key -> {elo, next_pos, gap, at, sig, hotUntil}
    var inflight = {};         // mode key -> Promise
    var pageInflight = {};     // page key -> Promise
    var requestedMode = '';
    var lastAttempt = 0;
    var failures = 0;
    var lastOk = true;
    var session = null;

    var STATE = {
      ok: false,
      username: cfg.username || '',
      rank_number: null,
      rank_label: null,
      elo: null,
      is_unreal: false,
      next_position: null,
      elo_to_next: null,
      session_delta: 0,
      prog_delta: 0,
      updated_at: null,
      error: 'starting up',
      active_mode_key: '',
      modes_available: []
    };

    /* --- persisted pieces ------------------------------------------- */

    function loadSession() {
      var s = store.get(K.session);
      var t = clock();
      if (!isDict(s) || typeof s.seen !== 'number' || t - s.seen > SESSION_GAP_MS || !isDict(s.elos)) {
        s = { start: nowSec(), seen: t, fp: null, elos: {}, progs: {}, resolvedAll: false };
      }
      s.seen = t;
      session = s;
      saveSession();
    }

    function saveSession() {
      if (session) store.set(K.session, session);
    }

    function touchSession() {
      if (!session) return;
      var fresh = store.get(K.session);
      // Two overlays that opened at the same moment can each start a session.
      // The older one wins so they settle on the same baselines.
      if (isDict(fresh) && isDict(fresh.elos) && typeof fresh.start === 'number'
          && fresh.start < session.start && clock() - fresh.seen <= SESSION_GAP_MS) {
        var mine = session;
        session = fresh;
        if (!isDict(session.progs)) session.progs = {};
        Object.keys(mine.elos).forEach(function (k) {
          if (!hasOwn.call(session.elos, k)) session.elos[k] = mine.elos[k];
        });
        Object.keys(mine.progs).forEach(function (k) {
          if (!hasOwn.call(session.progs, k)) session.progs[k] = mine.progs[k];
        });
      }
      // Another overlay may have recorded baselines since we last looked.
      else if (isDict(fresh) && fresh.start === session.start && isDict(fresh.elos)) {
        Object.keys(fresh.elos).forEach(function (k) {
          if (!hasOwn.call(session.elos, k)) session.elos[k] = fresh.elos[k];
        });
        Object.keys(fresh.progs || {}).forEach(function (k) {
          if (!hasOwn.call(session.progs, k)) session.progs[k] = fresh.progs[k];
        });
        if (fresh.resolvedAll) session.resolvedAll = true;
      }
      session.seen = clock();
      saveSession();
    }

    function loadLookups() {
      var l = store.get(K.lookup);
      lookups = isDict(l) ? l : {};
    }

    function saveLookups() { store.set(K.lookup, lookups); }

    /* --- state updates ported from refresh_once() --------------------- */

    function setError(msg) {
      STATE.error = msg;
      if (STATE.elo === null) STATE.ok = false;
    }

    function rollSeasonIfNeeded(data) {
      var fp = seasonFingerprint(data);
      if (fp === null) return;
      var previous = session.fp;
      session.fp = fp;
      if (previous !== null && previous !== undefined && fp * 2 < previous) {
        session.elos = {};
        session.progs = {};
      }
    }

    function recordBaseline(key, elo, points) {
      if (elo !== null && !hasOwn.call(session.elos, key)) session.elos[key] = elo;
      if (points !== null && !hasOwn.call(session.progs, key)) session.progs[key] = points;
    }

    function deltas(key, elo, points) {
      var startElo = hasOwn.call(session.elos, key) ? session.elos[key] : null;
      var startProg = hasOwn.call(session.progs, key) ? session.progs[key] : null;
      return [
        elo !== null && startElo !== null ? elo - startElo : 0,
        points !== null && startProg !== null ? points - startProg : 0
      ];
    }

    function cachedLookup(key) {
      return hasOwn.call(lookups, key) && isDict(lookups[key]) ? lookups[key] : {};
    }

    function modeRankKey(item) {
      var path = item[0], obj = item[1];
      var key = pathStr(path);
      var entry = cachedLookup(key);
      var elo = num(obj.elo);
      if (elo === null) {
        if (hasOwn.call(entry, 'elo')) elo = entry.elo === undefined ? null : entry.elo;
        else if (cfg.nextLookup && slugForMode(path) !== null) {
          // Not read yet: guess from the placement so the buttons don't
          // reshuffle once the leaderboard comes back.
          var p = unrealPlacement(obj);
          if (p !== null && p >= 1 && p <= LEADERBOARD_ASSUMED_DEPTH) elo = 0;
        }
      }
      var placed = unrealPlacement(obj);
      var div = num(obj.division) || 0;
      return [elo !== null ? 1 : 0, div, placed !== null ? -placed : 0, elo || 0];
    }

    function pickBestMode(list) {
      if (!list.length) return [null, null];
      var hint = cfg.modeHint.trim().toLowerCase();
      if (hint) {
        for (var i = 0; i < list.length; i++) {
          if (pathStr(list[i][0]).indexOf(hint) >= 0) return list[i];
        }
      }
      return maxBy(list, modeRankKey);
    }

    function pickMode(modeKey) {
      if (!modes.length) return [null, null];
      if (modeKey) {
        var hit = pickModeByKey(modes, modeKey);
        if (hit[1] !== null) return hit;
      }
      return pickBestMode(modes);
    }

    function modeView(path, obj) {
      var key = path ? pathStr(path) : '';
      var label = extractLabel(obj);
      var placement = unrealPlacement(obj);
      var isUnreal = label === 'UNREAL';
      var cached = cachedLookup(key);
      var elo = extractElo(obj);
      if (elo === null && isUnreal) elo = hasOwn.call(cached, 'elo') && cached.elo !== undefined ? cached.elo : null;
      return {
        key: key,
        label: label,
        placement: placement,
        progression: extractProgression(obj),
        is_unreal: isUnreal,
        elo: elo,
        next_pos: hasOwn.call(cached, 'next_pos') && cached.next_pos !== undefined ? cached.next_pos : null,
        next_gap: hasOwn.call(cached, 'gap') && cached.gap !== undefined ? cached.gap : null,
        points: progressPoints(obj)
      };
    }

    // A value that changes whenever this account finishes a match in the
    // mode. It decides when the leaderboard is worth reading again.
    function matchSignature(key, obj) {
      var want = normalizeModeKey(key);
      var latest = 0, count = 0;
      try {
        iter(raw && raw.match_history).forEach(function (day) {
          iter(dget(day, 'matches')).forEach(function (grp) {
            var rd = dget(grp, 'ranked_data');
            rd = truthy(rd) ? rd : {};
            if (normalizeModeKey(dget(rd, 'ranking_id')) !== want) return;
            count += pyInt(dget(grp, 'matches'));
            latest = Math.max(latest, Number(dget(grp, 'last_modified')) || 0);
          });
        });
      } catch (e) { /* odd history: fall back to the timer */ }
      return [count, latest, num(obj.elo), num(obj.division), num(obj.promotion_progression)].join(':');
    }

    /* --- network ------------------------------------------------------ */

    function get(url) {
      // Demo mode is the setup page's preview: it must never spend requests.
      if (cfg.demo) return Promise.reject(new Error('offline'));
      requests++;
      return fetchText(url);
    }

    function pageKey(slug, page) { return 'ro1:lb:' + slug + ':' + page; }

    var BUSY_MS = FETCH_TIMEOUT_MS + 2000;

    function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

    /*
     * Overlays opened together (OBS starting with the overlay in several
     * scenes) would all fetch the same thing at once. Before fetching, an
     * overlay claims the job in shared storage; the others wait for its
     * answer to land in the cache instead of asking for it again.
     */
    function claim(key) {
      var b = store.get(key);
      if (isDict(b) && clock() - b.at < BUSY_MS) return Promise.resolve(false);
      var token = Math.random().toString(36).slice(2);
      store.set(key, { at: clock(), token: token });
      // Two overlays can both see it free; the last write wins.
      return sleep(40).then(function () {
        var c = store.get(key);
        return isDict(c) && c.token === token;
      });
    }

    function release(key) {
      var c = store.get(key);
      if (isDict(c)) store.remove(key);
    }

    // Resolves with read()'s first non-null answer, or null after `ms` of
    // real time (real time, so a stopped test clock can't hang it).
    function waitFor(read, ms) {
      var until = Date.now() + ms;
      return new Promise(function (resolve) {
        (function poll() {
          var v = read();
          if (v !== null && v !== undefined) return resolve(v);
          if (Date.now() >= until) return resolve(null);
          setTimeout(poll, 200);
        })();
      });
    }

    function pruneLeaderboardCache() {
      var cutoff = clock() - 2 * 60 * 60 * 1000;
      store.keys().forEach(function (k) {
        if (k.indexOf('ro1:lbbusy:') === 0) {
          var b = store.get(k);
          if (!isDict(b) || clock() - b.at > 60 * 1000) store.remove(k);
          return;
        }
        if (k.indexOf('ro1:lb:') !== 0) return;
        var v = store.get(k);
        if (!isDict(v) || typeof v.at !== 'number' || v.at < cutoff) store.remove(k);
      });
    }

    function leaderboardPage(slug, page, maxAge) {
      if (page < 1 || page > LEADERBOARD_PAGE_LIMIT) return Promise.resolve([]);
      var key = pageKey(slug, page);
      var hit = store.get(key);
      var rowsOf = function (h) {
        return (h.rows || []).map(function (r) { return { placement: r[0], elo: r[1], account_id: r[2] }; });
      };
      if (isDict(hit) && Array.isArray(hit.rows)) {
        var ttl = hit.rows.length ? maxAge : Math.max(maxAge, EMPTY_PAGE_TTL_MS);
        if (clock() - hit.at < ttl) return Promise.resolve(rowsOf(hit));
      }
      if (pageInflight[key]) return pageInflight[key];
      var url = cfg.proxy + '/ranked/' + slug + (page > 1 ? '?page=' + page : '');
      var lock = 'ro1:lbbusy:' + slug + ':' + page;
      var asked = clock();
      var fetchIt = function () {
        return get(url).then(function (html) {
          var rows = parseLeaderboardRows(html);
          store.set(key, {
            at: clock(),
            rows: rows.map(function (r) { return [r.placement, r.elo, r.account_id]; })
          });
          return rows;
        }, function () {
          // A stale copy beats blanking the ELO on one bad fetch.
          return isDict(hit) && Array.isArray(hit.rows) ? rowsOf(hit) : [];
        });
      };
      var p = claim(lock).then(function (mine) {
        if (mine) return fetchIt().finally(function () { release(lock); });
        return waitFor(function () {
          var h = store.get(key);
          return isDict(h) && Array.isArray(h.rows) && h.at >= asked ? h : null;
        }, BUSY_MS).then(function (h) { return h ? rowsOf(h) : fetchIt(); });
      });
      pageInflight[key] = p;
      return p.finally(function () { delete pageInflight[key]; });
    }

    // Leaderboard re-reads never come faster than stats refreshes, so a slow
    // (idle) overlay re-reads slowly too.
    function lookupTtl() { return Math.max(LOOKUP_TTL_MS, 2 * interval()); }

    function lookupFresh(key, obj) {
      var entry = lookups[key];
      if (!isDict(entry)) return false;
      var t = clock();
      var maxAge = (entry.hotUntil || 0) > t ? LOOKUP_HOT_MS : lookupTtl();
      // A new placement is re-read from the cached page (free) so the row it
      // points at is the right one; only a page we don't have costs a request.
      return entry.sig === matchSignature(key, obj) && entry.placement === unrealPlacement(obj)
        && t - entry.at < maxAge;
    }

    function needsNetworkLookup(path, obj) {
      return !cfg.demo && unrealPlacement(obj) !== null && cfg.nextLookup && slugForMode(path) !== null;
    }

    // Resolves one mode's ELO / next target into `lookups`.
    function resolveMode(path, obj) {
      var key = pathStr(path);
      var apiElo = extractElo(obj);
      var placement = unrealPlacement(obj);
      if (!needsNetworkLookup(path, obj)) {
        lookups[key] = { elo: apiElo, next_pos: null, gap: null, at: clock(), sig: matchSignature(key, obj) };
        return Promise.resolve();
      }
      if (lookupFresh(key, obj)) return Promise.resolve();
      if (inflight[key]) return inflight[key];

      var sig = matchSignature(key, obj);
      var prev = isDict(lookups[key]) ? lookups[key] : null;
      var hotUntil = prev && prev.hotUntil ? prev.hotUntil : 0;
      if (prev && prev.sig !== sig) hotUntil = clock() + HOT_WINDOW_MS;   // just played
      var pageAge = hotUntil > clock() ? LOOKUP_HOT_MS : lookupTtl();

      // The player's own page follows pageAge; the pages above it (only read
      // to find the next ELO step) change slowly and can be older.
      var ownPage = Math.floor((placement - 1) / LEADERBOARD_PAGE_SIZE) + 1;
      var p = lookupLeaderboardElo(path, placement, id, true, function (slug, page) {
        return leaderboardPage(slug, page, page === ownPage ? pageAge : Math.max(pageAge, 2 * lookupTtl()));
      }).then(function (res) {
        // After a match the board is re-read every minute until it shows a
        // different ELO than before the match, then it goes back to slow.
        var before = null;
        if (prev && prev.sig !== sig) before = typeof prev.lb === 'number' ? prev.lb : null;
        else if (prev && typeof prev.preMatch === 'number') before = prev.preMatch;
        var settled = before !== null && res[0] !== null && res[0] !== before;
        var stillHot = !settled && hotUntil > clock();
        lookups[key] = {
          elo: apiElo !== null ? apiElo : res[0],
          next_pos: res[1],
          gap: res[2],
          at: clock(),
          sig: sig,
          placement: placement,
          lb: res[0],
          hotUntil: stillHot ? hotUntil : 0,
          preMatch: stillHot ? before : null
        };
        saveLookups();
      }).catch(function () { /* keep whatever we had */ });
      inflight[key] = p;
      return p.finally(function () { delete inflight[key]; });
    }

    function withTimeout(promise, ms) {
      return new Promise(function (resolve) {
        var done = false;
        var t = setTimeout(function () { if (!done) { done = true; resolve(); } }, ms);
        var finish = function () { if (!done) { done = true; clearTimeout(t); resolve(); } };
        promise.then(finish, finish);
      });
    }

    // Which modes are worth a leaderboard read on this refresh: every mode
    // once per session (so baselines and button order are right from the
    // start), then only the one on screen.
    function modesToResolve() {
      if (!session.resolvedAll) return modes.slice();
      var shown = pickMode(requestedMode);
      return shown[1] !== null ? [shown] : [];
    }

    function finalize() {
      if (!modes.length) return;
      // Baseline every mode so each one's delta starts from a real number the
      // first time the overlay switches to it.
      modes.forEach(function (m) {
        var v = modeView(m[0], m[1]);
        recordBaseline(v.key, v.elo, v.points);
      });
      var available = modes.slice().sort(function (a, b) {
        return compareTuples(modeRankKey(b), modeRankKey(a));
      }).map(function (m) {
        var k = pathStr(m[0]);
        return { key: k, label: modeLabel(k) };
      });
      var best = pickBestMode(modes);
      var v = modeView(best[0], best[1]);
      var d = deltas(v.key, v.elo, v.points);
      STATE.ok = true;
      STATE.rank_number = v.placement;
      STATE.rank_label = v.label;
      STATE.elo = v.elo;
      STATE.is_unreal = v.is_unreal;
      STATE.next_position = v.next_pos;
      STATE.elo_to_next = v.next_gap;
      STATE.session_delta = d[0];
      STATE.prog_delta = d[1];
      STATE.updated_at = Math.floor(rawAt / 1000);
      STATE.error = null;
      STATE.active_mode_key = v.key;
      STATE.modes_available = available;
      saveSession();
    }

    // Takes in a stats response (ours or another overlay's) and brings the
    // derived state up to date, reading the leaderboard where needed.
    function absorb(data, at) {
      raw = data;
      rawAt = at;
      modes = findRankedModes(data);
      if (!modes.length) {
        setError('no ranked data found');
        return Promise.resolve();
      }
      rollSeasonIfNeeded(data);
      var todo = modesToResolve();
      return Promise.all(todo.map(function (m) { return resolveMode(m[0], m[1]); })).then(function () {
        if (!session.resolvedAll) { session.resolvedAll = true; }
        finalize();
      });
    }

    function readCachedStats() {
      var c = store.get(K.stats);
      if (!isDict(c) || typeof c.at !== 'number' || typeof c.text !== 'string') return null;
      return c;
    }

    function parseStats(text) {
      try { return JSON.parse(text); } catch (e) { throw new Error('bad json'); }
    }

    // The shortest gap allowed between two stats requests right now.
    function interval() {
      return opts.isIdle && opts.isIdle() ? Math.max(IDLE_MS, cfg.refreshMs) : cfg.refreshMs;
    }

    function lastUpdated(data) {
      var t = isDict(data) ? Date.parse(String(data.last_updated || '')) : NaN;
      return isFinite(t) ? t : null;
    }

    /*
     * When the cached stats are next worth replacing. Normally that is just
     * after OliTracker's own copy expires (its last_updated + three minutes),
     * which is when a request is guaranteed to bring back something new. If a
     * request comes back unchanged anyway (a slow PC clock, or OliTracker
     * not refreshing that profile), it retries once soon and then backs off.
     */
    function dueAt(cached) {
      var floor = interval();
      var slow = Math.max(OLI_CACHE_MS, floor);
      if (!cached) return lastAttempt + slow;
      var stale = cached.stale || 0;
      if (stale >= 2) return cached.at + Math.min(BACKOFF_MAX_MS, slow * (stale - 1));
      if (stale === 1) return cached.at + Math.max(STALE_RETRY_MS, floor);
      var lu = cached.lu;
      var skew = typeof cached.skew === 'number' ? cached.skew : 0;
      if (typeof lu === 'number' && Math.abs(cached.at - skew - lu) <= OLI_CACHE_MS * 20) {
        // lu is OliTracker's clock; skew turns it into this PC's clock.
        return Math.max(cached.at + floor, lu + skew + OLI_CACHE_MS + ALIGN_SLACK_MS);
      }
      return cached.at + slow;
    }

    function isDue() {
      return clock() >= dueAt(readCachedStats()) - 1500;
    }

    /*
     * One refresh. Uses the shared cache when it is fresh enough, otherwise
     * fetches. Resolves to true when it had something new to show.
     */
    function refresh(force) {
      touchSession();
      var cached = readCachedStats();
      var t = clock();
      if (!force && cached && t < dueAt(cached) - 1500) {
        // Same data as before: the shown mode may still be due a lookup.
        if (cached.at === rawAt && raw !== null) return absorb(raw, rawAt).then(function () { return true; });
        var data;
        try { data = parseStats(cached.text); } catch (e) { data = null; }
        if (data !== null) return absorb(data, cached.at).then(function () { return true; });
        // A damaged cache entry: fetch instead.
      }
      return claim(K.busy).then(function (mine) {
        if (!mine && !force) {
          // Another overlay is fetching right now: use its answer.
          var since = cached ? cached.at : 0;
          return waitFor(function () {
            var c = readCachedStats();
            return c && c.at > since ? c : null;
          }, BUSY_MS).then(function (c) {
            var data = null;
            if (c) { try { data = parseStats(c.text); } catch (e) { data = null; } }
            // Nothing came of it: the next scheduled run tries again itself.
            return data === null ? false : absorb(data, c.at).then(function () { return true; });
          });
        }
        return fetchStats(cached).finally(function () { release(K.busy); });
      });
    }

    function fetchStats(cached) {
      var t = clock();
      lastAttempt = t;
      return get(cfg.proxy + '/stats/' + id).then(function (text) {
        var data = parseStats(text);
        var at = clock();
        var lu = lastUpdated(data);
        var stale = cached && lu !== null && cached.lu === lu ? (cached.stale || 0) + 1 : 0;
        // How far this PC's clock is from OliTracker's. When this request is
        // the one that made OliTracker refresh, last_updated is "now" on its
        // clock, so the difference is the offset. If someone else refreshed
        // it a moment earlier the guess comes out high, which only means one
        // slightly later fetch. Coming back unchanged means we were early.
        var prevSkew = cached && typeof cached.skew === 'number' ? cached.skew : 0;
        var skew = lu === null ? prevSkew
          : stale ? prevSkew + SKEW_STEP_MS
          : t - lu;
        skew = Math.max(-SKEW_MAX_MS, Math.min(SKEW_MAX_MS, skew));
        store.set(K.stats, { at: at, text: text, lu: lu, stale: stale, skew: skew });
        lastOk = true;
        failures = 0;
        pruneLeaderboardCache();
        return absorb(data, at).then(function () { return true; });
      }, function (e) {
        failures++;
        lastOk = false;
        setError(describeError(e));
        // Keep showing the last good numbers, from the cache if need be.
        if (raw === null && cached) {
          try { return absorb(parseStats(cached.text), cached.at).then(function () { STATE.error = describeError(e); return true; }); }
          catch (ignored) { /* nothing usable */ }
        }
        return true;
      });
    }

    // When the next refresh is due, in ms from now.
    function nextDelay() {
      var t = clock();
      if (failures > 0) {
        var back = Math.min(BACKOFF_MAX_MS, Math.max(OLI_CACHE_MS, cfg.refreshMs) * Math.pow(2, failures - 1));
        return Math.max(1000, lastAttempt + back - t);
      }
      return Math.max(1000, dueAt(readCachedStats()) - t);
    }

    /* --- snapshot() ---------------------------------------------------- */

    function snapshot(window, modeKey) {
      window = window || 'session';
      var s = {};
      Object.keys(STATE).forEach(function (k) { s[k] = STATE[k]; });
      s.modes_available = STATE.modes_available.slice();

      var pm = modeKey && modes.length ? pickMode(modeKey) : (modes.length ? pickBestMode(modes) : [null, null]);
      var path = pm[0], mode = pm[1];
      var v, delta, progDelta;
      if (mode !== null) {
        v = modeView(path, mode);
        var d = deltas(v.key, v.elo, v.points);
        delta = d[0]; progDelta = d[1];
      } else {
        v = {
          key: '',
          label: s.rank_label || '',
          placement: s.rank_number,
          progression: null,
          is_unreal: s.is_unreal || false,
          elo: s.elo,
          next_pos: s.next_position,
          next_gap: s.elo_to_next,
          points: null
        };
        delta = s.session_delta || 0;
        progDelta = s.prog_delta || 0;
      }

      var resolvedKey = v.key;
      var elo = v.elo;
      var label = v.label || '';
      var placement = v.placement;
      var progression = v.progression;
      var isUnreal = v.is_unreal;
      var nxt = v.next_pos;
      var gap = v.next_gap;

      var season = computeWindowedStats(raw, 'season', resolvedKey || null, nowSec(), session ? session.start : nowSec(), '');

      s.active_mode_key = resolvedKey || s.active_mode_key || '';
      s.rank_number = placement;
      s.rank_label = label;
      s.elo = elo;
      s.session_delta = delta;

      s.is_unreal = isUnreal;
      s.elo_unavailable = !!(isUnreal && elo === null);
      s.progression_pct = !isUnreal ? progression : null;
      s.prog_delta = !isUnreal ? progDelta : null;
      s.rank_display = isUnreal && placement ? ('#' + placement + ' ' + label).trim() : (label || '-');
      s.elo_text = isUnreal && elo !== null ? elo + ' ELO' : null;

      s.season_kd = season.kd !== null ? season.kd.toFixed(2) : '-';
      s.season_wr = season.wr !== null ? season.wr.toFixed(1) + '%' : '-%';
      s.season_wins = season.wins;
      s.season_kills = season.kills;

      var nextDivName = null;
      if (!isUnreal) {
        var div = mode !== null ? num(mode.division) : null;
        if (div !== null && hasOwn.call(DIVISION_NAMES, div + 1)) nextDivName = divisionName(div + 1);
      }
      s.next_rank_name = nextDivName;

      if (isUnreal && nxt && gap !== null) {
        s.next_gap = String(gap);
        s.next_pos = String(nxt);
      } else if (isUnreal && nxt) {
        s.next_gap = null;
        s.next_pos = String(nxt);
      } else if (!isUnreal && progression !== null) {
        s.next_gap = (100 - progression) + '%';
        s.next_pos = nextDivName || 'NEXT RANK';
      } else {
        s.next_gap = null;
        s.next_pos = null;
      }

      var sign;
      if (isUnreal) {
        if (window === 'season') {
          s.session_text = elo !== null ? '+' + elo + ' ALL SEASON' : '- ALL SEASON';
          s.session_sign = elo !== null ? 'pos' : 'zero';
        } else if (window === '12h' || window === '24h') {
          var wd = windowedEloDelta(raw, window, elo, resolvedKey || null, nowSec(), '');
          var labelStr = window === '12h' ? 'PAST 12H' : 'PAST 24H';
          if (wd === null) {
            s.session_text = '+0 ELO ' + labelStr;
            s.session_sign = 'zero';
          } else {
            sign = wd >= 0 ? '+' : '';
            s.session_text = sign + wd + ' ELO ' + labelStr;
            s.session_sign = wd > 0 ? 'pos' : (wd < 0 ? 'neg' : 'zero');
          }
        } else {
          sign = delta >= 0 ? '+' : '';
          s.session_text = sign + delta + ' ELO TODAY';
          s.session_sign = delta > 0 ? 'pos' : (delta < 0 ? 'neg' : 'zero');
        }
      } else {
        s.pct_to_next = progression !== null ? 100 - progression : null;
        sign = progDelta >= 0 ? '+' : '';
        s.session_text = sign + progDelta + '% TODAY';
        s.session_sign = progDelta > 0 ? 'pos' : (progDelta < 0 ? 'neg' : 'zero');
      }

      s.gap_unavailable = !!(isUnreal && (s.next_gap === null || s.next_pos === null));
      s.session_start = session ? session.start : nowSec();
      s.window = window;
      return s;
    }

    /* --- what the designs call ---------------------------------------- */

    var firstDone = null;

    function emit() {
      listeners.slice().forEach(function (fn) {
        try { fn(); } catch (e) { /* a design bug shouldn't stop the engine */ }
      });
    }

    function parseDataUrl(url) {
      var qs = String(url || '').split('?')[1] || '';
      var q = new URLSearchParams(qs);
      return {
        window: q.get('window') || q.get('stats_window') || 'session',
        mode: q.get('mode') || ''
      };
    }

    function fetchData(url) {
      var p = parseDataUrl(url);
      return (firstDone || Promise.resolve()).then(function () {
        requestedMode = p.mode;
        var pm = pickMode(p.mode);
        var known = pm[1] !== null && isDict(lookups[pathStr(pm[0])]);
        if (pm[1] !== null && session && (lastOk || !known)
            && !lookupFresh(pathStr(pm[0]), pm[1]) && needsNetworkLookup(pm[0], pm[1])) {
          return withTimeout(resolveMode(pm[0], pm[1]).then(function () {
            var v = modeView(pm[0], pm[1]);
            recordBaseline(v.key, v.elo, v.points);
            saveSession();
          }), LOOKUP_WAIT_MS);
        }
      }).then(function () {
        var snap = snapshot(p.window, p.mode || null);
        return { ok: true, json: function () { return Promise.resolve(snap); } };
      });
    }

    function onUpdate(fn) { listeners.push(fn); }

    function init() {
      loadSession();
      loadLookups();
      if (!validAccountId(id)) {
        STATE.error = id
          ? "that Epic account ID doesn't look right, remake your link on the setup page"
          : 'no Epic account ID in the link, make one on the setup page';
        firstDone = Promise.resolve();
        return firstDone;
      }
      firstDone = refresh(false).then(function () {}, function () {});
      return firstDone;
    }

    // Lets another overlay's fresh stats show up here without a request.
    function adoptShared() {
      var cached = readCachedStats();
      if (!cached || cached.at === rawAt) return Promise.resolve(false);
      var data;
      try { data = parseStats(cached.text); } catch (e) { return Promise.resolve(false); }
      return absorb(data, cached.at).then(function () { return true; });
    }

    return {
      config: cfg,
      init: init,
      refresh: refresh,
      adoptShared: adoptShared,
      nextDelay: nextDelay,
      touchSession: touchSession,
      snapshot: snapshot,
      fetchData: fetchData,
      onUpdate: onUpdate,
      emit: emit,
      hasData: function () { return raw !== null; },
      isDue: isDue,
      interval: interval,
      keys: K,
      requestCount: function () { return requests; },
      _state: function () { return { STATE: STATE, lookups: lookups, session: session, modes: modes }; },
      _setDemo: function (data, demoLookups, baselines) {
        raw = data;
        rawAt = clock();
        modes = findRankedModes(data);
        lookups = demoLookups;
        session = { start: nowSec(), seen: clock(), fp: null, elos: baselines.elos, progs: baselines.progs, resolvedAll: true };
        finalize();
        firstDone = Promise.resolve();
      }
    };
  }

  /* ------------------------------------------------------------------
   * Demo data: what the setup page previews before an account is set.
   * Matches the numbers on the design preview images.
   * ---------------------------------------------------------------- */

  function demoData() {
    var overall = function (wins, matches, kills) {
      return { both: { overall: { wins: wins, matches_played: matches, kills: kills } } };
    };
    return {
      raw: {
        user_id: 'demo',
        stats: {
          seasonal: {
            all: overall(52, 410, 980),
            ranked: overall(37, 130, 318),
            reload: overall(21, 96, 288)
          },
          lifetime: {}
        },
        ranked_stats: {
          'ranked-br-combined': { division: 21, promotion_progression: 50, current_unreal_placement: 66, elo: null },
          'ranked_blastberry_build': { division: 13, promotion_progression: 47, current_unreal_placement: null, elo: null },
          'ranked-squareclub': { division: 8, promotion_progression: 72, current_unreal_placement: null, elo: null }
        },
        match_history: []
      },
      lookups: { 'ranked-br-combined': { elo: 1542, next_pos: 65, gap: 14, at: 0, sig: '' } },
      baselines: {
        elos: { 'ranked-br-combined': 1519 },
        progs: { 'ranked_blastberry_build': 13 * 100 + 41, 'ranked-squareclub': 8 * 100 + 72 }
      }
    };
  }

  /* ------------------------------------------------------------------
   * Running inside an overlay page
   * ---------------------------------------------------------------- */

  function startOverlay(search) {
    var cfg = parseConfig(search);
    var storage = null;
    try { storage = window.localStorage; } catch (e) { storage = null; }
    var store = makeStore(storage);

    if (cfg.labelColor) {
      var style = document.createElement('style');
      style.textContent = ':root{--stat-label-color:#' + cfg.labelColor + ' !important}';
      document.head.appendChild(style);
    }

    // OBS tells browser sources when they are shown and whether the stream
    // or recording is running. Outside OBS none of this exists and the
    // overlay behaves as visible and live.
    var obs = typeof window.obsstudio !== 'undefined';
    var obsVisible = true;
    var tabVisible = typeof document.visibilityState === 'string' ? document.visibilityState !== 'hidden' : true;
    var outputs = { streaming: null, recording: null, virtualcam: null };

    function idle() {
      if (!obs) return false;
      var known = 0, on = false;
      Object.keys(outputs).forEach(function (k) {
        if (outputs[k] === true) on = true;
        if (outputs[k] !== null) known++;
      });
      // Only slow down when OBS has positively said nothing is running.
      return !on && known === 3;
    }

    var engine = createEngine({ cfg: cfg, store: store, isIdle: idle });
    var timer = null;

    function visible() { return obsVisible && tabVisible; }

    function schedule() {
      clearTimeout(timer);
      timer = null;
      if (cfg.demo || !visible() || !validAccountId(cfg.accountId)) return;
      var jitter = Math.floor(Math.random() * 3000);
      timer = setTimeout(run, engine.nextDelay() + jitter);
    }

    function run() {
      clearTimeout(timer);
      timer = null;
      engine.refresh(false).then(function (changed) {
        if (changed) engine.emit();
      }, function () {}).then(schedule);
    }

    function wake() {
      if (!visible()) { schedule(); return; }
      if (engine.isDue()) run();
      else schedule();
    }

    if (cfg.demo) {
      var d = demoData();
      engine._setDemo(d.raw, d.lookups, d.baselines);
    } else {
      // The design's first tick() already waits for this, so no emit needed.
      engine.init().then(schedule);
      setInterval(engine.touchSession, 60 * 1000);
    }

    document.addEventListener('visibilitychange', function () {
      tabVisible = document.visibilityState !== 'hidden';
      wake();
    });

    if (obs) {
      window.addEventListener('obsSourceVisibleChanged', function (e) {
        obsVisible = !(e && e.detail && e.detail.visible === false);
        wake();
      });
      var setOutput = function (name, value) {
        return function () { outputs[name] = value; wake(); };
      };
      window.addEventListener('obsStreamingStarted', setOutput('streaming', true));
      window.addEventListener('obsStreamingStopped', setOutput('streaming', false));
      window.addEventListener('obsRecordingStarted', setOutput('recording', true));
      window.addEventListener('obsRecordingStopped', setOutput('recording', false));
      window.addEventListener('obsVirtualcamStarted', setOutput('virtualcam', true));
      window.addEventListener('obsVirtualcamStopped', setOutput('virtualcam', false));
      try {
        if (typeof window.obsstudio.getStatus === 'function') {
          window.obsstudio.getStatus(function (st) {
            if (!st) return;
            outputs.streaming = !!st.streaming;
            outputs.recording = !!st.recording;
            outputs.virtualcam = !!st.virtualcam;
            schedule();
          });
        }
      } catch (e) { /* no permission: stay at the normal pace */ }
    }

    // Another overlay (another scene, another design) fetched: show it too.
    window.addEventListener('storage', function (e) {
      if (cfg.demo || e.key !== engine.keys.stats) return;
      engine.adoptShared().then(function (changed) {
        if (changed) engine.emit();
        schedule();
      });
    });

    return {
      version: VERSION,
      config: cfg,
      fetchData: engine.fetchData,
      onUpdate: engine.onUpdate,
      requestCount: engine.requestCount,
      snapshot: engine.snapshot
    };
  }

  /* ------------------------------------------------------------------
   * Used by the setup page
   * ---------------------------------------------------------------- */

  function lookupAccount(name, proxy) {
    var url = (proxy || DEFAULT_PROXY) + '/lookup?name=' + encodeURIComponent(name);
    return browserFetchText(url).then(function (text) {
      var d = JSON.parse(text);
      if (!d || !validAccountId(String(d.accountId || '').toLowerCase())) throw HttpError(404, 'No account found with that name');
      return String(d.accountId).toLowerCase();
    });
  }

  return {
    VERSION: VERSION,
    DEFAULT_PROXY: DEFAULT_PROXY,
    REFRESH_MIN_MS: REFRESH_MIN_MS,
    IDLE_MS: IDLE_MS,
    OLI_CACHE_MS: OLI_CACHE_MS,
    parseConfig: parseConfig,
    validAccountId: validAccountId,
    makeStore: makeStore,
    createEngine: createEngine,
    startOverlay: startOverlay,
    lookupAccount: lookupAccount,
    demoData: demoData,
    // exposed for tests
    _core: {
      findRankedModes: findRankedModes,
      parseLeaderboardRows: parseLeaderboardRows,
      lookupLeaderboardElo: lookupLeaderboardElo,
      computeWindowedStats: computeWindowedStats,
      windowedEloDelta: windowedEloDelta,
      modeLabel: modeLabel,
      slugForMode: slugForMode,
      pyRound: pyRound,
      pyRoundN: pyRoundN
    }
  };
});
