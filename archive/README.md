# Archive

The old Windows version of Ranked Overlay, kept for reference. It is not
maintained or built any more: the overlay is a website now
([fwsoapy.github.io/ranked-overlay](https://fwsoapy.github.io/ranked-overlay/)),
built from `src/` by `tools/build.py`.

| Folder | What it was |
| --- | --- |
| `desktop/<Design>/` | Each design as a local Python server (`server.py`), its launcher (`overlay.bat`) and settings (`config.json`) |
| `wizard/` | The setup wizard that built those folders, and its templates |
| `src/` | The Python sources the design folders were generated from |
| `tools/` | The release script and the `update.json` checker |
| `workflows/release.yml` | The GitHub Actions workflow that built the wizard .exe for releases |
| `setup.bat`, `update.json` | The old installer and the update manifest old installs read |

## Old installs still check this repo for updates

Every old install still running asks this repo for updates. If it finds one,
it downloads `server.py` and `overlay.bat` and runs them, with no signature
or hash check. Whoever controls `fwsoapy/ranked-overlay` controls those PCs.
Right now the channel is dormant, and these rules keep it that way:

- **Don't put an `update.json` at the top of the repo.** Old installs read
  `https://raw.githubusercontent.com/fwsoapy/ranked-overlay/main/update.json`.
  The copy in here is out of their reach.
- **Don't create `v1.2.3`-style tags.** With no `update.json`, old installs
  fall back to the newest version tag. A `v3.0.0` tag for the website would
  make every old install show "Update available" at every start, then fail to
  download it, because `wizard/templates/` isn't at the top of the repo any
  more. Name tags something else (`web-2026-11`, say), or just don't tag.
- **Keep 2FA on the GitHub account, and don't rename or delete the account or
  the repo.** A freed-up name can be registered by someone else.

## Network

`server.py` only answers this PC (127.0.0.1), and other web pages can't read
it. It used to answer the whole network, so anyone on the same Wi-Fi, or any
site you visited, could read your Epic name, account ID and stats from it.
Running the overlay on one PC with OBS on another needs the website version,
which works anywhere.

`desktop/Classic/server.py` is still used by `tests/web/test_engine.py`, which
checks the website's engine gives the same answers as the Python server did.
