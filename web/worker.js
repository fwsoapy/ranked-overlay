// Cloudflare Worker that lets the hosted overlay talk to OliTracker.
//
// OliTracker's API doesn't send CORS headers, so a page on github.io (or an
// OBS browser source pointed at one) isn't allowed to read it directly. This
// Worker fetches on the page's behalf and adds the header. It only forwards
// the handful of paths the overlay needs, so it can't be used as an open proxy.
//
// Routes:
//   GET /stats/<32-hex account id>          -> olitracker.com/api/stats/<id>
//   GET /ranked/<slug>?page=<n>             -> olitracker.com/api/ranked/<slug>?page=<n> (JSON, pages 1-10)
//                                              or olitracker.com/ranked/<slug>?page=<n> (HTML, any page)
//   GET /lookup?name=<epic display name>    -> { "accountId": "..." } (api-fortnite.com, then OliTracker search)
//
// The lookup route reads its key from the FORTNITE_API_KEY secret (Worker
// settings > Variables and Secrets), never from this file.
//
// Requests from web pages are only answered for the sites in ALLOWED_ORIGINS,
// so nobody else can build their site on top of this Worker and spend its
// daily request allowance. Requests with no Origin at all (typing a URL into
// the address bar, curl) still work, which keeps testing easy. That also means
// the Origin check only stops other websites, not scripts, so each visitor
// (IP address) is also held to a pace no overlay comes near (RATE_LIMITS).
// That keeps one script from hammering OliTracker through this Worker and
// getting it blocked for everyone. It can't save the daily allowance itself:
// Cloudflare counts a request even when it's turned away here.

const OLI = "https://olitracker.com";

// Where the overlay is hosted. Add your own GitHub Pages address here if you
// fork the repo. localhost and local files ("null") are for testing.
const ALLOWED_ORIGINS = [
  "https://fwsoapy.github.io",
  "null",
];
const LOCAL_ORIGIN = /^https?:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/;

// Same list as _slug_for_mode() in archive/src/core_head.py.
const SLUGS = new Set(["battle-royale", "zero-build", "reload", "reload-zb", "og", "rocket-racing"]);

const ACCOUNT_ID = /^[0-9a-f]{32}$/i;

// How long a browser may reuse an answer before asking again. The overlay
// keeps its own cache too; these just stop an accidental reload from costing
// a request.
const MAX_AGE = { stats: 30, ranked: 300, lookup: 86400 };

const BROWSER_HEADERS = {
  "User-Agent":
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) " +
    "AppleWebKit/537.36 (KHTML, like Gecko) " +
    "Chrome/125.0.0.0 Safari/537.36",
  "Accept-Language": "en-US,en;q=0.9",
  "Referer": "https://olitracker.com/",
};

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Max-Age": "86400",
};

// The leaderboard route can pass along OliTracker's HTML. Nothing in it
// should ever run on this Worker's address if someone opens it in a browser.
const SAFE = {
  "X-Content-Type-Options": "nosniff",
  "Content-Security-Policy": "default-src 'none'; sandbox",
  "Referrer-Policy": "no-referrer",
};

// Requests per visitor per minute. An overlay asks about once every three
// minutes, a fresh start on Unreal reads a dozen or so leaderboard pages at
// once, and five overlays share one cache, so these are far above any real
// use. Counted per Worker instance (there are several), so it's a rough cap,
// which is all it needs to be.
const RATE_LIMITS = { data: 60, lookup: 15 };
const RATE_WINDOW_MS = 60 * 1000;
const seen = new Map();   // "route ip" -> { start, count }

function overLimit(kind, request) {
  const ip = request.headers.get("CF-Connecting-IP");
  if (!ip) return false;
  const now = Date.now();
  if (seen.size > 10000) {
    for (const [k, v] of seen) if (now - v.start >= RATE_WINDOW_MS) seen.delete(k);
    if (seen.size > 10000) seen.clear();
  }
  const key = kind + " " + ip;
  let e = seen.get(key);
  if (!e || now - e.start >= RATE_WINDOW_MS) {
    e = { start: now, count: 0 };
    seen.set(key, e);
  }
  e.count++;
  return e.count > RATE_LIMITS[kind];
}

function originAllowed(origin, env) {
  if (origin === null) return true;   // not a cross-site page request
  if (LOCAL_ORIGIN.test(origin)) return true;
  const extra = String((env && env.ALLOWED_ORIGINS) || "")
    .split(",").map((s) => s.trim()).filter(Boolean);
  return ALLOWED_ORIGINS.includes(origin) || extra.includes(origin);
}

function reply(body, status, type, maxAge) {
  return new Response(body, {
    status,
    headers: {
      ...CORS,
      ...SAFE,
      "Content-Type": type,
      "Cache-Control": maxAge > 0 ? `public, max-age=${maxAge}` : "no-store",
    },
  });
}

function error(status, message) {
  return reply(JSON.stringify({ error: message }), status, "application/json", 0);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// OliTracker often fails the first request for a profile it hasn't looked at
// in a while (it fetches it from Epic, which can take 30+ seconds and then
// time out), and answers the next one straight away. So a server error or a
// dropped connection is tried again, twice, before giving up.
const RETRY_DELAYS = [1500, 4000];

async function forward(url, accept, type, maxAge) {
  let status = 502;
  for (let attempt = 0; attempt <= RETRY_DELAYS.length; attempt++) {
    if (attempt > 0) await sleep(RETRY_DELAYS[attempt - 1]);
    let r;
    try {
      r = await fetch(url, { headers: { ...BROWSER_HEADERS, Accept: accept } });
    } catch (e) {
      status = 502;
      continue;
    }
    if (r.ok) return reply(r.body, 200, type, maxAge);
    status = r.status;
    // 4xx (bad id, not found) won't change by asking again, and a 429 means
    // OliTracker wants fewer requests, not more.
    if (status < 500) break;
  }
  if (status === 502) return error(502, "Couldn't reach OliTracker");
  return error(status, `OliTracker returned ${status}`);
}

// The leaderboard, 100 players a page. OliTracker's JSON API only serves
// pages 1-10 (the top 1000); its web page goes all the way down. So pages
// 1-10 come from the API with the web page as a backup, and deeper pages
// from the web page. The overlay reads either. A source only counts if the
// answer really is leaderboard rows (the API answers a bad page with a 200
// and an error object, and a Cloudflare check is a page with no rows).
const API_PAGES = 10;

async function leaderboard(slug, page) {
  const api = {
    url: `${OLI}/api/ranked/${slug}` + (page > 1 ? `?page=${page}` : ""),
    accept: "application/json, text/plain, */*",
    type: "application/json",
    good: (text) => text.trimStart().startsWith("["),
  };
  const web = {
    url: `${OLI}/ranked/${slug}` + (page > 1 ? `?page=${page}` : ""),
    accept: "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    type: "text/html; charset=utf-8",
    good: (text) => text.includes("leaderboard-player-row"),
  };
  const sources = page <= API_PAGES ? [api, web] : [web];
  let status = 502;
  for (const src of sources) {
    for (let attempt = 0; attempt <= RETRY_DELAYS.length; attempt++) {
      if (attempt > 0) await sleep(RETRY_DELAYS[attempt - 1]);
      let r, text;
      try {
        r = await fetch(src.url, { headers: { ...BROWSER_HEADERS, Accept: src.accept } });
        text = await r.text();
      } catch (e) {
        status = 502;
        continue;
      }
      if (r.ok && src.good(text)) return reply(text, 200, src.type, MAX_AGE.ranked);
      status = r.ok ? 404 : r.status;
      // A page that doesn't exist (or a Cloudflare check) won't change by
      // asking again straight away: move on to the next source. A 429 means
      // OliTracker wants fewer requests, so that one isn't retried either.
      if (r.ok || r.status < 500) break;
    }
  }
  return error(status, status === 404 ? "No leaderboard rows on that page" : `OliTracker returned ${status}`);
}

async function lookup(name, env) {
  const q = encodeURIComponent(name);
  const urls = !env.FORTNITE_API_KEY ? [] : [
    `https://prod.api-fortnite.com/api/v1/account/displayName/${q}`,
    `https://prod.api-fortnite.com/api/v1/profile/progress?displayName=${q}`,
    `https://prod.api-fortnite.com/api/v1/profile/stats?displayName=${q}`,
  ];
  for (const url of urls) {
    try {
      const r = await fetch(url, {
        headers: { ...BROWSER_HEADERS, "x-api-key": env.FORTNITE_API_KEY },
      });
      if (!r.ok) continue;
      const d = await r.json();
      const id = String(
        d.accountId || d.account_id || d.id ||
        (d.data && (d.data.accountId || d.data.account_id)) || "");
      if (ACCOUNT_ID.test(id)) {
        return reply(JSON.stringify({ accountId: id.toLowerCase() }), 200, "application/json", MAX_AGE.lookup);
      }
    } catch (e) {
      // try the next endpoint
    }
  }
  // OliTracker's own player search, for when the key isn't set or the
  // lookup above came up empty. Only an exact name match (ignoring case)
  // counts, so a near miss never picks someone else's account.
  try {
    const r = await fetch(`${OLI}/api/search?name=${q}`, {
      headers: { ...BROWSER_HEADERS, Accept: "application/json" },
    });
    if (r.ok) {
      const list = await r.json();
      const want = name.toLowerCase();
      const hit = Array.isArray(list) &&
        list.find((p) => p && typeof p.username === "string" && p.username.toLowerCase() === want);
      if (hit && ACCOUNT_ID.test(String(hit.id))) {
        return reply(JSON.stringify({ accountId: String(hit.id).toLowerCase() }), 200, "application/json", MAX_AGE.lookup);
      }
    }
  } catch (e) {
    // fall through to "not found"
  }
  return error(404, "No account found with that name");
}

export default {
  async fetch(request, env) {
    if (!originAllowed(request.headers.get("Origin"), env)) {
      return error(403, "This site isn't allowed to use this proxy");
    }
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: { ...CORS, ...SAFE } });
    if (request.method !== "GET") return error(405, "GET only");

    const url = new URL(request.url);
    const parts = url.pathname.split("/").filter(Boolean);
    const known = parts[0] === "stats" || parts[0] === "ranked" || parts[0] === "lookup";
    if (known && overLimit(parts[0] === "lookup" ? "lookup" : "data", request)) {
      return error(429, "Too many requests, slow down");
    }

    if (parts[0] === "stats" && parts.length === 2 && ACCOUNT_ID.test(parts[1])) {
      return forward(`${OLI}/api/stats/${parts[1].toLowerCase()}`,
        "application/json, text/plain, */*", "application/json", MAX_AGE.stats);
    }

    if (parts[0] === "ranked" && parts.length === 2 && SLUGS.has(parts[1])) {
      const page = parseInt(url.searchParams.get("page") || "1", 10);
      if (!(page >= 1 && page <= 100000)) return error(400, "Bad page number");
      return leaderboard(parts[1], page);
    }

    if (parts[0] === "lookup" && parts.length === 1) {
      const name = (url.searchParams.get("name") || "").trim();
      // The name goes into api-fortnite.com's URL path, where "." or ".."
      // would point at a different endpoint; control characters are never
      // part of a real name.
      if (!name || name.length > 64 || /^\.+$/.test(name) || /[\u0000-\u001f\u007f]/.test(name)) {
        return error(400, "Pass ?name=<epic display name>");
      }
      return lookup(name, env);
    }

    return error(404, "Not found");
  },
};
