

# ---------------------------------------------------------------------------
# Accent colour
#
# Each design ships with its own accent in its stylesheet. A custom one is kept
# in config.json and applied here, as an override appended to the page, rather
# than by rewriting the stylesheet on disk. That way a new version can restyle
# the overlay however it likes without losing anybody's colour.
# ---------------------------------------------------------------------------

def _hex_to_rgb(value):
    value = str(value or "").strip().lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", value):
        return None
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def migrate_accent_once():
    """Pick up an accent that an older version wrote into the stylesheet.

    Before 2.2 a custom colour was applied by editing the CSS in server.py, so
    on the first run after updating that is where it still is. Lift it into
    config.json so it survives from here on.
    """
    if "accent" in CONFIG:
        return
    found = re.search(r"--accent:\s*(#[0-9a-fA-F]{6});", OVERLAY_HTML)
    if not found:
        return
    CONFIG["accent"] = found.group(1).lstrip("#")
    label = re.search(r"--stat-label-color:\s*(#[0-9a-fA-F]{6});", OVERLAY_HTML)
    if label:
        CONFIG["stat_label_color"] = label.group(1).lstrip("#")
    save_config(CONFIG)


def _accent_override():
    """A <style> block for the configured accent, or "" for the design default."""
    rgb = _hex_to_rgb(CONFIG.get("accent"))
    if rgb is None:
        return ""
    rules = [f"--accent: #{str(CONFIG['accent']).lstrip('#')};",
             f"--accent-rgb: {rgb[0]}, {rgb[1]}, {rgb[2]};"]
    label = CONFIG.get("stat_label_color")
    if _hex_to_rgb(label) is not None:
        rules.append(f"--stat-label-color: #{str(label).lstrip('#')};")
    return "<style>:root{" + "".join(rules) + "}</style>"


BLOCKED_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<title>Update required</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: transparent; font-family: Inter, "Segoe UI", sans-serif; }
  .card {
    display: inline-block; padding: 14px 18px; border-radius: 10px;
    background: rgba(17, 17, 20, 0.92); border-top: 5px solid #ff3b30;
    color: #fff; line-height: 1.45;
  }
  .title { font-size: 15px; font-weight: 900; letter-spacing: .04em;
           text-transform: uppercase; color: #ff3b30; }
  .body  { font-size: 13px; margin-top: 4px; color: #d6d6db; }
  .ver   { font-size: 11px; margin-top: 6px; color: #8a8a93; }
</style></head>
<body>
  <div class="card">
    <div class="title">Overlay update required</div>
    <div class="body">
      Version __LATEST__ is needed to keep showing rank data.<br>
      Restart the overlay and choose <b>Update now</b>.
    </div>
    <div class="ver">You are on __CURRENT__ &middot; github.com/__REPO__</div>
  </div>
</body></html>"""


def render_overlay():
    """The overlay page as it is served."""
    if BLOCKED_BY_UPDATE:
        return (BLOCKED_HTML
                .replace("__LATEST__", str(BLOCKED_BY_UPDATE.get("version", "")))
                .replace("__CURRENT__", VERSION)
                .replace("__REPO__", UPDATE_REPO))
    html = OVERLAY_HTML.replace("__POLL_MS__", str(OVERLAY_POLL_MS))
    html = html.replace("__INITIAL_CREATOR_CODE__",
                        CREATOR_CODE.strip().replace('"', '\\"'))
    override = _accent_override()
    if override:
        html = html.replace("</head>", override + "</head>", 1)
    return html

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip("/")
        params = parse_qs(parsed.query)

        def _p(key, default=""):
            return (params.get(key, [default]) or [default])[0]

        if path in ("", "/overlay"):
            self._send(200, render_overlay(), "text/html; charset=utf-8")
        elif path == "/data":
            w = _p("window", _p("stats_window", "session"))
            m = _p("mode", "")
            self._send(200, json.dumps(snapshot(w, m or None)), "application/json")
        elif path == "/raw":
            body = json.dumps(_last_raw, indent=2, default=str) if _last_raw else "{}"
            self._send(200, body, "application/json")
        elif path == "/version":
            body = json.dumps({"version": VERSION, "design": DESIGN,
                               "auto_update": AUTO_UPDATE, "repo": UPDATE_REPO,
                               "branch": UPDATE_BRANCH,
                               "blocked": dict(BLOCKED_BY_UPDATE) or None})
            self._send(200, body, "application/json")
        elif path == "/debug":
            self._send(200, debug_report(), "text/plain; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain")


def _bind(port, attempts=20):
    """Bind the port, waiting out a version of ourselves that is still exiting.

    After an auto-update the outgoing process can hold the socket for a moment,
    so a first refusal is not a reason to give up.
    """
    last = None
    for _ in range(attempts):
        try:
            return ThreadingHTTPServer(("0.0.0.0", port), Handler)
        except OSError as e:
            last = e
            time.sleep(1)
    raise last


def main():
    print(f"Fortnite Ranked Overlay {VERSION} ({DESIGN}) - port {PORT}")
    print(f"OBS Browser Source: http://localhost:{PORT}/overlay")
    print(f"Polling OliTracker every {POLL_SECONDS}s - Ctrl+C to stop\n")

    migrate_accent_once()

    # The startup check happens before the port is bound, so a required update
    # is dealt with before anything is serving and there is no handover to do.
    try:
        if check_for_update():
            return
    except Exception as e:
        print(f"[overlay] update check skipped: {e}")

    threading.Thread(target=poll_loop, daemon=True).start()

    try:
        server = _bind(PORT)
    except OSError as e:
        print(f"Could not start on port {PORT}: {e}")
        print("Another program may be using that port.")
        input("Press Enter to close...")
        return

    # The daily re-check gets the server handle so it can release the port
    # cleanly if it ends up installing something.
    if AUTO_UPDATE != "off":
        threading.Thread(target=update_loop, args=(server,), daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping. Bye!")


if __name__ == "__main__":
    main()
