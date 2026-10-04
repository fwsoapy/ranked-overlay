// Cloudflare Worker that lets the hosted overlay talk to OliTracker.
//
// OliTracker's API doesn't send CORS headers, so a page on github.io (or an
// OBS browser source pointed at one) isn't allowed to read it directly. This
// Worker fetches on the page's behalf and adds the header. It only forwards
// the handful of paths the overlay needs, so it can't be used as an open proxy.
//
// Routes:
//   GET /stats/<32-hex account id>          -> olitracker.com/api/stats/<id>
//   GET /ranked/<slug>?page=<n>             -> olitracker.com/ranked/<slug> (raw HTML)
//   GET /lookup?name=<epic display name>    -> { "accountId": "..." }
//
// The lookup route reads its key from the FORTNITE_API_KEY secret (Worker
// settings > Variables and Secrets), never from this file.

const OLI = "https://olitracker.com";

// Same list as _slug_for_mode() in src/core_head.py.
const SLUGS = new Set(["battle-royale", "zero-build", "reload", "reload-zb", "og", "rocket-racing"]);

const ACCOUNT_ID = /^[0-9a-f]{32}$/i;

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

function reply(body, status, type, maxAge) {
  return new Response(body, {
    status,
    headers: {
      ...CORS,
      "Content-Type": type,
      // Lets the browser reuse a response instead of asking again on every
      // poll. Leaderboard pages are big and change slowly.
      "Cache-Control": `public, max-age=${maxAge}`,
    },
  });
}

function error(status, message) {
  return reply(JSON.stringify({ error: message }), status, "application/json", 0);
}

async function forward(url, accept, type, maxAge) {
  let r;
  try {
    r = await fetch(url, { headers: { ...BROWSER_HEADERS, Accept: accept } });
  } catch (e) {
    return error(502, "Couldn't reach OliTracker");
  }
  if (!r.ok) return error(r.status, `OliTracker returned ${r.status}`);
  return reply(r.body, 200, type, maxAge);
}

async function lookup(name, env) {
  if (!env.FORTNITE_API_KEY) return error(500, "FORTNITE_API_KEY secret isn't set on the Worker");
  const q = encodeURIComponent(name);
  const urls = [
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
      const id =
        d.accountId || d.account_id || d.id ||
        (d.data && (d.data.accountId || d.data.account_id));
      if (id) return reply(JSON.stringify({ accountId: id }), 200, "application/json", 3600);
    } catch (e) {
      // try the next endpoint
    }
  }
  return error(404, "No account found with that name");
}

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });
    if (request.method !== "GET") return error(405, "GET only");

    const url = new URL(request.url);
    const parts = url.pathname.split("/").filter(Boolean);

    if (parts[0] === "stats" && parts.length === 2 && ACCOUNT_ID.test(parts[1])) {
      return forward(`${OLI}/api/stats/${parts[1].toLowerCase()}`,
        "application/json, text/plain, */*", "application/json", 15);
    }

    if (parts[0] === "ranked" && parts.length === 2 && SLUGS.has(parts[1])) {
      const page = parseInt(url.searchParams.get("page") || "1", 10);
      if (!(page >= 1 && page <= 100000)) return error(400, "Bad page number");
      const target = `${OLI}/ranked/${parts[1]}` + (page > 1 ? `?page=${page}` : "");
      return forward(target, "text/html,application/xhtml+xml,*/*", "text/html; charset=utf-8", 60);
    }

    if (parts[0] === "lookup" && parts.length === 1) {
      const name = (url.searchParams.get("name") || "").trim();
      if (!name || name.length > 64) return error(400, "Pass ?name=<epic display name>");
      return lookup(name, env);
    }

    return error(404, "Not found");
  },
};
