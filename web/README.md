# Hosted overlay (work in progress)

The goal is a version of the overlay that runs as a website, so people paste one
URL into OBS instead of running Python and a `.bat` file. This folder is
separate from the design folders and doesn't touch the current version.

OliTracker's API doesn't send CORS headers, so a web page can't read it
directly. `worker.js` is a small Cloudflare Worker that fetches it on the page's
behalf. `test.html` checks that the Worker works, including from inside OBS.

## Deploying the Worker

1. Sign up at [cloudflare.com](https://dash.cloudflare.com/sign-up) (free, no card or domain needed).
2. Open **Workers & Pages**, click **Create**, then **Create Worker** (the "Hello World" start is fine). Name it `ranked-proxy` and click **Deploy**.
3. Click **Edit code**, replace everything with the contents of `worker.js`, then click **Deploy**.
4. Optional, for the Account ID lookup: in the Worker's **Settings > Variables and Secrets**, add a **Secret** named `FORTNITE_API_KEY` with your api-fortnite.com key.

Your Worker URL looks like `https://ranked-proxy.<your-subdomain>.workers.dev`.

## Testing it

Open `test.html` in a browser, or add it to OBS as a Browser Source (tick
**Local file**), and fill in the Worker URL and your Epic account ID. You can
also skip the form:

```
test.html?proxy=https://ranked-proxy.<your-subdomain>.workers.dev&id=<account id>
```

Two green lines means the plan works: the stats JSON and a leaderboard page both
came through the Worker.

## Routes

| Route | Forwards to |
| --- | --- |
| `/stats/<account id>` | `olitracker.com/api/stats/<account id>` |
| `/ranked/<slug>?page=<n>` | `olitracker.com/ranked/<slug>` (raw HTML, parsed in the page) |
| `/lookup?name=<display name>` | api-fortnite.com account lookup, returns `{ "accountId": "..." }` |

Anything else returns 404, so it can't be used as a general proxy.

The free Workers plan allows 100,000 requests a day. One overlay polling every
30 seconds uses roughly 150 an hour.
