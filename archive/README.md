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

Old installs that check for updates won't find `update.json` at the top of the
repo any more and simply skip the check.

`desktop/Classic/server.py` is still used by `tests/web/test_engine.py`, which
checks the website's engine gives the same answers as the Python server did.
