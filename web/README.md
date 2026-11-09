# The website's Cloudflare Worker

The [website version](https://fwsoapy.github.io/ranked-overlay/) of the overlay runs entirely in the page
(`src/web/engine.js`, built into `docs/`). OliTracker doesn't send CORS headers, so a web page isn't allowed
to read it directly. `worker.js` is a small Cloudflare Worker that fetches OliTracker on the page's behalf
and adds the header.

It only forwards the three things the overlay needs, and only answers pages on the sites listed in
`ALLOWED_ORIGINS`, so nobody else can build on it and use up its daily allowance.

| Route | Forwards to |
| --- | --- |
| `/stats/<account id>` | `olitracker.com/api/stats/<account id>` |
| `/ranked/<slug>?page=<n>` | `olitracker.com/api/ranked/<slug>?page=<n>` (JSON, pages 1-10) or `olitracker.com/ranked/<slug>?page=<n>` (HTML, any page), 100 players a page |
| `/lookup?name=<display name>` | api-fortnite.com account lookup, returns `{ "accountId": "..." }` |

The leaderboard comes from two places: OliTracker's JSON API (`/api/ranked/<slug>`) for the top 1000 (pages
1-10, all it serves), and OliTracker's leaderboard web page for anything deeper, so players past #1000 still get
their ELO and the gap to the next spot. Each one backs the other up where it can. A Worker deployed before this
can't read past #1000: paste the current `worker.js` in again (steps below).

## Deploying or updating it

1. In the [Cloudflare dashboard](https://dash.cloudflare.com/) open **Workers & Pages**, then the `ranked-proxy`
   Worker (or create one with **Create application > Start with Hello World!**).
2. Click **Edit code**, replace everything with the contents of `worker.js`, and click **Deploy**.
3. For the name lookup: **Settings > Variables and Secrets > Add**, type **Secret**, name `FORTNITE_API_KEY`,
   value your api-fortnite.com key. Without it, names are looked up through OliTracker's player search instead
   (exact name matches only).

If you fork the repo, add your own GitHub Pages address to `ALLOWED_ORIGINS` in `worker.js`, and point
`DEFAULT_PROXY` in `src/web/engine.js` at your Worker (then run `python tools/build.py`). An overlay link's
`?proxy=<worker url>` only counts when the overlay page itself is served from your own PC (localhost or a local
file), for trying out a Worker before you change `DEFAULT_PROXY`. On the real site it's ignored, so nobody can
hand a streamer a link that feeds their overlay from somewhere else.

## Limits

Each visitor (IP address) gets 60 stats and leaderboard requests a minute and 15 name lookups, far more than any
overlay uses. Past that the Worker answers 429 and the overlay backs off. This stops one script from hammering
OliTracker through the Worker and getting it blocked for everyone. It doesn't protect the daily allowance below,
because Cloudflare counts a request even when the Worker turns it away. If it ever runs out regularly, the
Workers Paid plan ($5 a month) has no daily cap: 10 million requests a month included, then $0.30 per million.

## How many requests it uses

The free plan allows 100,000 requests a day. The overlay asks right after OliTracker refreshes a profile
(about every 3 minutes) and never more often, shares one cache between every overlay in OBS, stops while
the overlay is hidden and slows down when OBS isn't live. Roughly:

| Situation | Requests per hour |
| --- | --- |
| Live, below Unreal | ~20 |
| Live, Unreal (also reads the leaderboard) | ~25 |
| OBS open, not streaming or recording | ~10 |
| Overlay not on screen | 0 |

`python tests/web/test_engine.py` fails if a change pushes these up.

## Testing it

`test.html` checks the Worker from a browser or an OBS Browser Source (tick **Local file**):

```
test.html?proxy=https://ranked-proxy.<your-subdomain>.workers.dev&id=<account id>
```

Two green lines means stats and a leaderboard page both came through.
