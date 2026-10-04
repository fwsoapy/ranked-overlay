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
      cfg: E.parseConfig(`?id=${accountId}&name=tester`),
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

(async () => {
  const snapshots = await parity();
  const live = await budget(3, false);
  const idleRate = await budget(3, true);
  process.stdout.write(JSON.stringify({
    snapshots,
    budget: [['live, Unreal, 2 overlays', live, 30], ['OBS open, not live', idleRate, 15]],
  }));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
