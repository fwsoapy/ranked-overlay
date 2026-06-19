# Fortnite Ranked Overlay

A live ranked overlay for Fortnite streamers. Pulls real-time ELO, rank, and season stats from [OliTracker](https://olitracker.com) and displays them as a browser source in OBS. Supports multiple game modes, automatic non-Unreal progression tracking, and a built-in mode switcher for BR, Reload, and Boxfights.

---

## Features

- Live rank, ELO, and leaderboard position pulled every 10 seconds
- Session ELO delta: tracks how much you've gained or lost since you started the overlay
- Unreal leaderboard: shows ELO to next rank (`NEXT 14 ELO to #66`)
- Non-Unreal ranks: shows promotion progress % and percent gained today (`53% TO GOLD III`)
- Mode switcher buttons for BR, Reload, and Boxfights, each with their own independent stats
- Season stats (K/D, Win%, Kills, Wins) accurate per game mode
- 5 overlay designs to choose from, any accent color you want

---

## Designs

| # | Style | Description |
|---|-------|-------------|
| 1 | **Slash** | Two-panel header split by a diagonal blade. Clean and aggressive. |
| 2 | **Side Bar** | Left accent bar with inline rank and ELO. Compact horizontal layout. |
| 3 | **Classic** | Simple dark card. Rank and ELO side by side, stats below. |
| 4 | **Horizontal** | Wide layout with a vertical divider between rank and ELO sections. |
| 5 | **Minimal** | One line of text. Just the essentials. |

Each design comes in any color: purple, red, blue, green, orange, or whatever you want. Just ask in the issues tab and we can set it up.

---

## Requirements

- Python 3.8 or later
- Windows (the `.bat` files are Windows only; Mac/Linux users can run `python server.py` directly)
- OBS Studio with a Browser Source
- Your Epic Account ID (find it at [olitracker.com](https://olitracker.com) by searching your username)

---

## Setup

### 1. Download the files

Click **Code > Download ZIP** at the top of this page, then unzip it anywhere on your PC. Your Desktop works fine.

### 2. Find your Epic Account ID

1. Go to [olitracker.com](https://olitracker.com)
2. Search for your Fortnite username
3. Copy the long ID from the URL. It looks like `a7c42959a8e24785b51746b5eeb5baff`

### 3. Add your account ID to the server file

Open `server.py` in Notepad (right-click > Open with > Notepad) and find these two lines near the top:

```python
EPIC_USERNAME    = "YourUsername"
EPIC_ACCOUNT_ID  = "your-account-id-here"
```

Replace both values with your username and account ID. Save the file.

### 4. Start the overlay

Double-click `START_OVERLAY.bat`. A window will briefly appear confirming it started, then close itself. The overlay is now running in the background.

To stop it, double-click `STOP_OVERLAY.bat`.

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
Something else is using port 8888. Run `STOP_OVERLAY.bat` first, then start it again. If the issue persists, change `PORT = 8888` to another number like `8889` in `server.py` and update the OBS URL to match.

**Stats look wrong after switching modes**
Give it one poll cycle (about 10 seconds) after clicking a mode button. The server fetches fresh data on each cycle.

**OBS shows a black box instead of the overlay**
Make sure `START_OVERLAY.bat` has been run first. The browser source needs the local server to be running. Also check that the URL in OBS is exactly `http://localhost:8888/overlay`.

---

## Changing the accent color

Open `server.py` and find the CSS inside `OVERLAY_HTML`. The main accent color is defined as a hex value like `#7c3aed` (purple) or `#dc2626` (red). Do a find-and-replace in Notepad to swap it out for any color you want. Use [coolors.co](https://coolors.co) to pick one.

---

## How it works

The overlay is a small Python web server that runs locally on your PC. It polls the OliTracker API every 10 seconds, parses your ranked stats, and serves a single HTML page at `localhost:8888/overlay`. OBS loads that page as a browser source and auto-refreshes the displayed data. No data ever leaves your machine other than the API request to OliTracker.

---

## Credits

Built by fwsoapy on Discord. Stats powered by [OliTracker](https://olitracker.com).
