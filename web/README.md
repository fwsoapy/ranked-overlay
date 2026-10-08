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
| `/ranked/<slug>?page=<n>` | `olitracker.com/api/ranked/<slug>?page=<n>` (JSON leaderboard, 100 players a page) |
| `/lookup?name=<display name>` | api-fortnite.com account lookup, returns `{ "accountId": "..." }` |

OliTracker's leaderboard web pages now sit behind a Cloudflare check, so the Worker reads its JSON leaderboard
(`/api/ranked/<slug>`) instead. It only goes 10 pages deep (the top 1,000), so below that the overlay takes ELO
from the player's profile or recent matches. A Worker deployed before this change can't read the leaderboard any
more: paste the current `worker.js` in again (steps below).

## Deploying or updating it

1. In the [Cloudflare dashboard](https://dash.cloudflare.com/) open **Workers & Pages**, then the `ranked-proxy`
   Worker (or create one with **Create application > Start with Hello World!**).
2. Click **Edit code**, replace everything with the contents of `worker.js`, and click **Deploy**.
3. For the name lookup: **Settings > Variables and Secrets > Add**, type **Secret**, name `FORTNITE_API_KEY`,
   value your api-fortnite.com key. Without it, names are looked up through OliTracker's player search instead
   (exact name matches only).

If you fork the repo, add your own GitHub Pages address to `ALLOWED_ORIGINS` in `worker.js`, and point
`DEFAULT_PROXY` in `src/web/engine.js` at your Worker (then run `python tools/build.py`).

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
