// Runs src/web/engine.js through the scenarios test_engine.py hands it, and
// simulates a few hours of streaming to count Worker requests. Prints JSON.
'use strict';

const fs = require('fs');
const path = require('path');
const E = require(path.join(__dirname, '..', '..', 'src', 'web', 'engine.js'));

const job = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const FX = job.fixtures;

function memoryStorage() {
  const m = new Map();
  return {
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => m.set(k, String(v)),
    removeItem: (k) => m.delete(k),
    get length() { return m.size; },
    key: (i) => [...m.keys()][i],
  };
}

function leaderboard(url) {
  const m = url.match(/\/ranked\/([a-z-]+)(?:\?page=(\d+))?$/);
  const file = path.join(FX, `lb-${m[1]}-${m[2] || 1}.html`);
  return fs.existsSync(file) ? fs.readFileSync(file, 'utf8') : '';
}

async function parity() {
  const result = {};
  for (const [name, accountId, steps] of job.scenarios) {
    let clock = job.T0 * 1000;
    let current = null;
    const fetchText = async (url) => (url.includes('/stats/') ? JSON.stringify(current) : leaderboard(url));
    const engine = E.createEngine({
      // elo=opened: the since-the-overlay-opened count, which is what the
      // Python server shows, so the two can be compared directly.
      cfg: E.parseConfig(`?id=${accountId}&name=tester&elo=opened`),
      store: E.makeStore(memoryStorage()),
      fetchText,
      now: () => clock,
    });
    const out = [];
    for (let i = 0; i < steps.length; i++) {
      clock = (job.T0 + i * job.step) * 1000;
      current = steps[i];
      if (i === 0) await engine.init(); else await engine.refresh(false);
      const keys = [null].concat(engine._state().modes.map((m) => m[0].map(String).join('/').toLowerCase()));
      for (const k of keys) {
        for (const w of job.windows) {
          const r = await engine.fetchData('/data?window=' + w + (k ? '&mode=' + encodeURIComponent(k) : ''));
          out.push([i, k, w, await r.json()]);
        }
      }
    }
    result[name] = out;
  }
  return result;
}

// Two overlays (the same source in two OBS scenes) sharing one cache, against
// an OliTracker that keeps each profile three minutes. Other players shift
// the placement every refresh and the streamer finishes a match every 15 min.
async function budget(hours, idle) {
  const multi = job.scenarios.find((s) => s[0] === 'multi');
  const accountId = multi[1];
  const base = multi[2][0];
  let clock = job.T0 * 1000;
  let seed = 7;
  const rand = () => ((seed = (seed * 16807) % 2147483647) / 2147483647);
  let placement = 4901, matches = 0, serverLu = 0, requests = 0;
  const shared = memoryStorage();
  const fetchText = async (url) => {
    requests++;
    if (!url.includes('/stats/')) return leaderboard(url);
    if (clock - serverLu >= 180e3) serverLu = clock;
    const d = JSON.parse(JSON.stringify(base));
    d.last_updated = new Date(serverLu).toISOString();
    d.ranked_stats['ranked-br-combined'].current_unreal_placement = placement;
    d.match_history[0].matches = matches ? [{
      matches, wins: 0, kills: 3, last_modified: job.T0 + matches * 900,
      ranked_data: { ranking_id: 'ranked-br-combined', elo: 3400 + matches },
    }] : [];
    return JSON.stringify(d);
  };
  const make = () => E.createEngine({
    cfg: E.parseConfig(`?id=${accountId}`), store: E.makeStore(shared), fetchText,
    now: () => clock, isIdle: () => idle,
  });
  const overlays = [make(), make()];
  const due = [];
  for (const o of overlays) {
    await o.init();
    await o.fetchData('/data?window=session');
    due.push(clock + o.nextDelay() + rand() * 3000);
  }
  const end = clock + hours * 3600e3;
  for (;;) {
    const i = due[0] <= due[1] ? 0 : 1;
    if (due[i] > end) break;
    const before = clock;
    clock = due[i];
    placement += Math.round((rand() - 0.5) * 40);
    if (Math.floor(clock / 900e3) !== Math.floor(before / 900e3)) matches++;
    await overlays[i].refresh(false);
    await overlays[i].fetchData('/data?window=session');
    due[i] = clock + overlays[i].nextDelay() + rand() * 3000;
  }
  return requests / hours;
}

// The website-only parts: the ELO change windows and the mode notice.
async function websiteChecks() {
  const checks = [];
  const check = (name, got, want) => checks.push([name, JSON.stringify(got) === JSON.stringify(want), got, want]);
  const C = E._core;

  // Midnight Central Time, standard time, daylight time, and both switch days.
  const iso = (ms) => new Date(ms).toISOString();
  check('CT midnight in winter (CST)', iso(C.centralMidnight(Date.parse('2026-01-15T20:00:00Z'))), '2026-01-15T06:00:00.000Z');
  check('CT midnight in summer (CDT)', iso(C.centralMidnight(Date.parse('2026-07-04T15:00:00Z'))), '2026-07-04T05:00:00.000Z');
  check('CT midnight just before it', iso(C.centralMidnight(Date.parse('2026-07-04T04:59:00Z'))), '2026-07-03T05:00:00.000Z');
  check('CT midnight on DST start day', iso(C.centralMidnight(Date.parse('2026-03-08T15:00:00Z'))), '2026-03-08T06:00:00.000Z');
  check('CT midnight on DST end day', iso(C.centralMidnight(Date.parse('2026-11-01T18:00:00Z'))), '2026-11-01T05:00:00.000Z');

  // A history: yesterday ended on 3000 ELO, then games this morning.
  const now = Date.parse('2026-07-04T20:00:00Z');          // 3pm Central
  const t = (isoStr) => Math.floor(Date.parse(isoStr) / 1000);
  const grp = (ts, elo, extra) => ({ matches: 2, wins: 0, kills: 3, last_modified: ts,
    ranked_data: Object.assign({ ranking_id: 'ranked-br-combined', elo }, extra || {}) });
  const history = [
    { date: '2026-07-04T00:00:00+00:00', elo: {}, matches: [grp(t('2026-07-04T16:00:00Z'), 3060)] },  // 11am CT
    { date: '2026-07-03T00:00:00+00:00', elo: {}, matches: [grp(t('2026-07-03T22:00:00Z'), 3000)] },  // yesterday 5pm CT
    { date: '2026-07-02T00:00:00+00:00', elo: {}, matches: [grp(t('2026-07-03T02:00:00Z'), 2950)] },
  ];
  const raw = {
    last_updated: new Date(now).toISOString(),
    stats: { seasonal: { all: { both: { overall: { matches_played: 50 } } } } },
    ranked_stats: { 'ranked-br-combined': { division: 21, current_unreal_placement: 40, elo: 3080 } },
    match_history: history,
  };
  check('ELO since CT midnight', C.eloDeltaSince(raw, Math.floor(C.centralMidnight(now) / 1000), 3080, 'ranked-br-combined', ''), 80);
  check('ELO past 12h', C.eloDeltaSince(raw, Math.floor(now / 1000) - 43200, 3080, 'ranked-br-combined', ''), 80);
  check('ELO past 24h', C.eloDeltaSince(raw, Math.floor(now / 1000) - 86400, 3080, 'ranked-br-combined', ''), 130);

  // Through the whole engine, as the designs see it.
  const text = async (query, data) => {
    let clock = now;
    const engine = E.createEngine({ cfg: E.parseConfig(query), store: E.makeStore(memoryStorage()),
      fetchText: async () => JSON.stringify(data), now: () => clock });
    await engine.init();
    const r = await engine.fetchData('/data?window=session');
    return await r.json();
  };
  const id = 'id=0123456789abcdef0123456789abcdef';
  check('default is today', (await text(`?${id}`, raw)).session_text, '+80 ELO TODAY');
  check('elo=12h label', (await text(`?${id}&elo=12h`, raw)).session_text, '+80 ELO PAST 12H');
  check('elo=24h label', (await text(`?${id}&elo=24h`, raw)).session_text, '+130 ELO PAST 24H');
  check('unknown elo falls back to today', (await text(`?${id}&elo=week`, raw)).session_text, '+80 ELO TODAY');

  // No usable history: count from when the overlay opened (+0 right away).
  const bare = JSON.parse(JSON.stringify(raw)); bare.match_history = [];
  check('no history -> since opened', (await text(`?${id}&elo=24h`, bare)).session_text, '+0 ELO PAST 24H');

  // Below Unreal: progress points from the history.
  const low = JSON.parse(JSON.stringify(raw));
  low.ranked_stats = { 'ranked-squareclub': { division: 7, promotion_progression: 30, current_unreal_placement: null, elo: null } };
  low.match_history = [
    { date: 'x', elo: {}, matches: [{ matches: 3, wins: 1, kills: 4, last_modified: t('2026-07-04T17:00:00Z'),
      ranked_data: { ranking_id: 'ranked-squareclub', division: 7, promotion_progression: 30 } }] },
    { date: 'y', elo: {}, matches: [{ matches: 3, wins: 1, kills: 4, last_modified: t('2026-07-03T20:00:00Z'),
      ranked_data: { ranking_id: 'ranked-squareclub', division: 6, promotion_progression: 80 } }] },
  ];
  check('progress today across a promotion', (await text(`?${id}`, low)).session_text, '+50% TODAY');
  check('progress past 12h', (await text(`?${id}&elo=12h`, low)).session_text, '+50% PAST 12H');

  // Asking for a mode the account doesn't have.
  const s1 = await text(`?${id}&mode=squareclub`, raw);
  check('missing mode notice', s1.error, 'no ranked Boxfights games yet, showing BR');
  check('missing mode still shows best', s1.rank_display, '#40 UNREAL');
  const s2 = await text(`?${id}&mode=br-combined`, raw);
  check('no notice when the mode exists', s2.error, null);
  // "Session": counts from the last Reset ELO gain, kept across restarts.
  {
    const storage = memoryStorage();
    let clock = now;
    let data = JSON.parse(JSON.stringify(raw));
    const open = () => E.createEngine({ cfg: E.parseConfig(`?${id}&elo=session`), store: E.makeStore(storage),
      fetchText: async () => JSON.stringify(data), now: () => clock });
    const shown = async (engine) => (await (await engine.fetchData('/data?window=session')).json()).session_text;
    let engine = open();
    await engine.init();
    check('session starts at +0', await shown(engine), '+0 ELO TODAY');
    clock += 10 * 60 * 1000; data.ranked_stats['ranked-br-combined'].elo = 3110; data.last_updated = new Date(clock).toISOString();
    await engine.refresh(true);
    check('session counts up', await shown(engine), '+30 ELO TODAY');
    engine = open(); await engine.init();      // OBS restarted
    check('session survives a restart', await shown(engine), '+30 ELO TODAY');
    engine.resetSession();
    check('reset puts it back to +0', await shown(engine), '+0 ELO TODAY');
    clock += 10 * 60 * 1000; data.ranked_stats['ranked-br-combined'].elo = 3095; data.last_updated = new Date(clock).toISOString();
    await engine.refresh(true);
    check('counts from the reset', await shown(engine), '-15 ELO TODAY');
    const second = open(); await second.init();
    check('another overlay shares the reset', await shown(second), '-15 ELO TODAY');
    // a new season wipes it rather than showing a huge loss
    clock += 10 * 60 * 1000;
    data.stats.seasonal.all.both.overall.matches_played = 1;
    data.ranked_stats['ranked-br-combined'].elo = 1200; data.last_updated = new Date(clock).toISOString();
    await engine.refresh(true);
    check('new season starts it over', await shown(engine), '+0 ELO TODAY');
  }
  // "Session" starts over by itself after 6 hours without an ELO change.
  {
    const H = 60 * 60 * 1000;
    const setup = () => {
      const storage = memoryStorage();
      const st = { clock: now, data: JSON.parse(JSON.stringify(raw)) };
      st.open = async () => {
        const e = E.createEngine({ cfg: E.parseConfig(`?${id}&elo=session`), store: E.makeStore(storage),
          fetchText: async () => JSON.stringify(st.data), now: () => st.clock });
        await e.init();
        return e;
      };
      st.step = async (engine, ms, elo, matchAt) => {
        st.clock += ms;
        if (elo !== undefined) st.data.ranked_stats['ranked-br-combined'].elo = elo;
        if (matchAt !== undefined) st.data.match_history.unshift({ date: 'z', elo: {}, matches: [grp(Math.floor(matchAt / 1000), elo)] });
        st.data.last_updated = new Date(st.clock).toISOString();
        await engine.refresh(true);
        return (await (await engine.fetchData('/data?window=session')).json()).session_text;
      };
      st.shown = async (engine) => (await (await engine.fetchData('/data?window=session')).json()).session_text;
      return st;
    };

    // Streams, then sits idle: still counted at 5h, gone after 6h.
    let st = setup();
    let engine = await st.open();
    await st.step(engine, 10 * 60 * 1000, 3110);
    check('idle 5h keeps the session', await st.step(engine, 5 * H), '+30 ELO TODAY');
    check('idle 6h starts it over', await st.step(engine, 1 * H + 60 * 1000), '+0 ELO TODAY');
    check('still +0 after a restart', await st.shown(await st.open()), '+0 ELO TODAY');
    check('counts the next stream', await st.step(engine, 10 * 60 * 1000, 3130), '+20 ELO TODAY');
    check('a 5h break mid-session keeps counting', await st.step(engine, 5 * H, 3125), '+15 ELO TODAY');

    // Closed for 10h, games played while closed (dated by the history):
    // those count, the last stream's +30 doesn't.
    st = setup();
    engine = await st.open();
    await st.step(engine, 10 * 60 * 1000, 3110);
    let last = st.clock;
    st.clock += 10 * H;
    st.data.ranked_stats['ranked-br-combined'].elo = 3150;
    st.data.match_history.unshift({ date: 'z', elo: {}, matches: [grp(Math.floor((last + 8 * H) / 1000), 3150)] });
    st.data.last_updated = new Date(st.clock).toISOString();
    check('games while closed count, old stream does not', await st.shown(await st.open()), '+40 ELO TODAY');

    // Same, but the history has no newer match: dated when it's noticed.
    st = setup();
    engine = await st.open();
    await st.step(engine, 10 * 60 * 1000, 3110);
    st.clock += 10 * H;
    st.data.ranked_stats['ranked-br-combined'].elo = 3150;
    st.data.last_updated = new Date(st.clock).toISOString();
    check('gap without history still starts over', await st.shown(await st.open()), '+40 ELO TODAY');

    // A game 2h after closing, then 8h of nothing: that's idle, so +0.
    st = setup();
    engine = await st.open();
    await st.step(engine, 10 * 60 * 1000, 3110);
    last = st.clock;
    st.clock += 10 * H;
    st.data.ranked_stats['ranked-br-combined'].elo = 3150;
    st.data.match_history.unshift({ date: 'z', elo: {}, matches: [grp(Math.floor((last + 2 * H) / 1000), 3150)] });
    st.data.last_updated = new Date(st.clock).toISOString();
    check('old games then 6h quiet starts over', await st.shown(await st.open()), '+0 ELO TODAY');

    // Pressing Reset after an idle stretch isn't undone by the timer.
    st = setup();
    engine = await st.open();
    await st.step(engine, 10 * 60 * 1000, 3110);
    await st.step(engine, 7 * H);
    engine.resetSession();
    check('manual reset after idle', await st.step(engine, 10 * 60 * 1000, 3100), '-10 ELO TODAY');
    check('manual reset holds', await st.step(engine, 1 * H), '-10 ELO TODAY');
  }
  // The Record design: wins and losses over the same stretch as the ELO change.
  {
    const rec = (d) => [d.record_wins, d.record_losses, d.record_kills, d.record_kd, d.record_wr, d.record_label];
    check('record past 24h', rec(await text(`?${id}&elo=24h`, raw)), [0, 4, 6, '1.50', '0.0%', 'PAST 24H']);
    check('record past 12h', rec(await text(`?${id}&elo=12h`, raw)), [0, 2, 3, '1.50', '0.0%', 'PAST 12H']);

    const H = 60 * 60 * 1000;
    const storage = memoryStorage();
    let clock = now;
    const data = JSON.parse(JSON.stringify(raw));
    const open = async () => {
      const e = E.createEngine({ cfg: E.parseConfig(`?${id}&elo=session`), store: E.makeStore(storage),
        fetchText: async () => JSON.stringify(data), now: () => clock });
      await e.init();
      return e;
    };
    const game = (won, elo, at) => {
      data.ranked_stats['ranked-br-combined'].elo = elo;
      data.match_history[0].matches.push({ matches: 1, wins: won ? 1 : 0, kills: won ? 9 : 2,
        last_modified: Math.floor(at / 1000), ranked_data: { ranking_id: 'ranked-br-combined', elo } });
    };
    const shown = async (engine, ms) => {
      clock += ms || 0;
      data.last_updated = new Date(clock).toISOString();
      await engine.refresh(true);
      return rec(await (await engine.fetchData('/data?window=session')).json()).slice(0, 2);
    };
    let engine = await open();
    check('session record starts 0-0', await shown(engine), [0, 0]);
    game(true, 3120, clock + 5 * 60 * 1000);
    game(false, 3105, clock + 25 * 60 * 1000);
    check('session record counts games', await shown(engine, 30 * 60 * 1000), [1, 1]);
    check('session record survives a restart', await shown(await open()), [1, 1]);
    engine.resetSession();
    check('reset clears the record', await shown(engine), [0, 0]);
    game(true, 3130, clock + 5 * 60 * 1000);
    check('counts after the reset', await shown(engine, 10 * 60 * 1000), [1, 0]);
    // Closed for 10h, two games while closed: only those count.
    clock += 10 * H;
    game(false, 3110, clock - 3 * H);
    game(true, 3140, clock - 2 * H);
    data.last_updated = new Date(clock).toISOString();
    check('new session counts games while closed', rec(await (await (await open()).fetchData('/data?window=session')).json()).slice(0, 2), [1, 1]);
    // And 6 quiet hours later it starts over.
    check('record starts over after 6h', await shown(engine, 7 * H), [0, 0]);
  }
  {
    // below Unreal it counts progress, and the label says SESSION
    const storage = memoryStorage();
    let clock = now;
    let data = JSON.parse(JSON.stringify(low));
    const engine = E.createEngine({ cfg: E.parseConfig(`?${id}&elo=session`), store: E.makeStore(storage),
      fetchText: async () => JSON.stringify(data), now: () => clock });
    await engine.init();
    clock += 10 * 60 * 1000; data.ranked_stats['ranked-squareclub'].promotion_progression = 62; data.last_updated = new Date(clock).toISOString();
    await engine.refresh(true);
    check('session progress below Unreal', (await (await engine.fetchData('/data?window=session')).json()).session_text, '+32% TODAY');
  }
  return checks;
}

(async () => {
  const snapshots = await parity();
  const checks = await websiteChecks();
  const live = await budget(3, false);
  const idleRate = await budget(3, true);
  process.stdout.write(JSON.stringify({
    snapshots,
    checks,
    budget: [['live, Unreal, 2 overlays', live, 30], ['OBS open, not live', idleRate, 15]],
  }));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
