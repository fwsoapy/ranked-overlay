# Fortnite Ranked Overlay

A live ranked overlay for Fortnite streamers. Pulls real-time ELO, rank, and season stats from [OliTracker](https://olitracker.com) and displays them as a browser source in OBS. Supports multiple game modes, automatic non-Unreal progression tracking, and a built-in mode switcher for BR, Reload, and Boxfights.

7 designs to choose from, each in its own self-contained folder — just grab the one you like.

---

## Features

- Live rank, ELO, and leaderboard position pulled every 10 seconds
- Session ELO delta: tracks how much you've gained or lost since you started the overlay
- Unreal leaderboard: shows ELO to next rank (`NEXT 14 ELO to #66`)
- Non-Unreal ranks: shows promotion progress % and percent gained today (`53% TO GOLD III`)
- Mode switcher buttons for BR, Reload, and Boxfights, each with their own independent stats
- Season stats (K/D, Win%, Kills, Wins) accurate per game mode
- 7 overlay designs to choose from, any accent color you want

---

## Designs

Click a preview to open that design's folder.

<table>
<tr>
<td align="center" width="50%">
<a href="Minimal"><img src="Minimal/preview.png" width="320"><br><b>Minimal</b></a>
<br>Clean single-row card with rank and ELO side-by-side and a bold colored left border.
</td>
<td align="center" width="50%">
<a href="Classic"><img src="Classic/preview.png" width="320"><br><b>Classic</b></a>
<br>A timeless dark card with a thin top accent line and subtle dividers between sections.
</td>
</tr>
<tr>
<td align="center" width="50%">
<a href="Sharp"><img src="Sharp/preview.png" width="320"><br><b>Sharp</b></a>
<br>Stacked sections with a strong accent color and clipped corners. Feels structured and aggressive.
</td>
<td align="center" width="50%">
<a href="Wide"><img src="Wide/preview.png" width="320"><br><b>Wide</b></a>
<br>Spread out horizontally with a glowing accent bar on the left. Great for wider stream layouts.
</td>
</tr>
<tr>
<td align="center" width="50%">
<a href="Slash"><img src="Slash/preview.png" width="320"><br><b>Slash</b></a>
<br>A diagonal cut splits the rank and ELO into two panels. Stands out on any stream.
</td>
<td align="center" width="50%">
<a href="Rainbow"><img src="Rainbow/preview.png" width="320"><br><b>Rainbow</b></a>
<br>Animated rainbow rank text and a shimmering ELO value. High energy.
</td>
</tr>
<tr>
<td align="center" width="50%">
<a href="Modern"><img src="Modern/preview.png" width="320"><br><b>Modern</b></a>
<br>Sleek card with a soft radial glow accent and a bold colored left border.
</td>
<td></td>
</tr>
</table>

Each design works in any color. Open `server.py`, find the hex color value in the CSS near the top of `OVERLAY_HTML`, and swap it for whatever you want. Use [coolors.co](https://coolors.co) to pick one.

---

## Requirements

- Python 3 or later
- Windows (the `.bat` files are Windows only; Mac/Linux users can run `python server.py` directly)
- OBS Studio with a Browser Source
- Your Epic Account ID (the bundled `account-id.bat` looks this up for you — see Setup below)

---

## Setup

Every design folder (`Minimal/`, `Classic/`, `Sharp/`, `Wide/`, `Slash/`, `Rainbow/`, `Modern/`) is self-contained — it has its own `server.py`, `account-id.bat`, `start.bat`, and `stop.bat`. You only ever need the one folder for the design you picked.

### 1. Download the files

Click **Code > Download ZIP** at the top of this page, then unzip it anywhere on your PC. Your Desktop works fine. Open the folder for the design you picked from the table above — everything you need is in there.

### 2. Find your Epic Account ID

Double-click `account-id.bat`. Enter your Epic display name and it will print your account ID in the console window and copy it to your clipboard.

### 3. Add your account ID to the server file

Open `server.py` in Notepad (right-click > Open with > Notepad) and find these two lines near the top:

```python
EPIC_USERNAME    = "YourUsername"
EPIC_ACCOUNT_ID  = "your-account-id-here"
```

Replace both values with your username and account ID. Save the file.

### 4. Start the overlay

Double-click `start.bat`. A window will briefly appear confirming it started, then close itself. The overlay is now running in the background.

To stop it, double-click `stop.bat`.

### 5. Add it to OBS

1. In OBS, click the **+** button under Sources
2. Select **Browser**
3. Set the URL to `http://localhost:8888/overlay`
4. Set Width to `600` and Height to `300` (adjust to taste)
5. Click OK

The overlay will appear and start showing your live stats within a few seconds of your first game.

---

## Switching game modes

The overlay shows mode buttons (BR, Reload, Boxfights) below the widget. Click a button to switch and the rank, ELO, and stats all update for that mode. In OBS you can interact with browser sources by right-clicking the source and selecting **Interact**.

---

## Troubleshooting

**Overlay shows "starting up" for a long time**
OliTracker may be slow to respond. Wait 30 seconds, and if it still does not load, check that your Account ID in `server.py` is correct.

**Port already in use error**
Something else is using port 8888. Run `stop.bat` first, then start it again. If the issue persists, change `PORT = 8888` to another number like `8889` in `server.py` and update the OBS URL to match.

**Stats look wrong after switching modes**
Give it one poll cycle (about 10 seconds) after clicking a mode button. The server fetches fresh data on each cycle.

**OBS shows a black box instead of the overlay**
Make sure `start.bat` has been run first. The browser source needs the local server to be running. Also check that the URL in OBS is exactly `http://localhost:8888/overlay`.

---

## Changing the accent color

Open `server.py` and find the CSS inside `OVERLAY_HTML`. The main accent color is defined as a hex value like `#7c3aed` (purple) or `#dc2626` (red). Do a find-and-replace in Notepad to swap it out for any color you want. Use [coolors.co](https://coolors.co) to pick one.

---

## How it works

The overlay is a small Python web server that runs locally on your PC. It polls the OliTracker API every 10 seconds, parses your ranked stats, and serves a single HTML page at `localhost:8888/overlay`. OBS loads that page as a browser source and auto-refreshes the displayed data. No data ever leaves your machine other than the API request to OliTracker.

---

## Credits

Built by fwsoapy on Discord. Stats powered by [OliTracker](https://olitracker.com).
