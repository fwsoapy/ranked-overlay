import os
import re
import sys
import json
import time
import shutil
import threading
import subprocess
import urllib.request
import urllib.parse

DESIGNS = ["Minimal", "Classic", "Sharp", "Wide", "Slash", "Rainbow", "Modern", "Pulse"]

COLOR_NAMES = {
    "red":     "ff3b30",
    "orange":  "ff7a00",
    "yellow":  "ffd400",
    "green":   "2ed573",
    "cyan":    "00e5ff",
    "blue":    "2979ff",
    "purple":  "7c3aed",
    "magenta": "ff00ff",
    "pink":    "ff4fa3",
    "white":   "ffffff",
}

ACCOUNT_API_KEY = "5944cf9e101f8c722009a2dd790e705295555503d544144bfcd312af2eb0fa87"


def resource_path(relative):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)


def run_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def banner():
    print("=" * 56)
    print("   Fortnite Ranked Overlay - Setup Wizard")
    print("=" * 56)
    print()


def check_python():
    print("Checking for Python (needed to run the overlay)...")
    for cmd in ("python", "py"):
        try:
            r = subprocess.run([cmd, "--version"], capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                print(f"  Found: {r.stdout.strip() or r.stderr.strip()}")
                return True
        except (FileNotFoundError, OSError):
            continue

    print("  Python was not found on this PC.")
    choice = input("  Install it now via winget? (Y/N): ").strip().lower()
    if choice != "y":
        print("  Skipping. You will need to install Python yourself before running the overlay.")
        print("  Get it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH').")
        return False

    try:
        subprocess.run(
            ["winget", "install", "--id", "Python.Python.3.12", "-e",
             "--silent", "--accept-package-agreements", "--accept-source-agreements"],
            timeout=300,
        )
        print("  Python installed. You may need to restart this wizard for PATH changes to apply.")
        return True
    except Exception as e:
        print(f"  Could not install automatically ({e}).")
        print("  Get it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH').")
        return False


_preview_state = {"root": None}


def _tk_preview_worker(path):
    try:
        import tkinter as tk
        root = tk.Tk()
        root.title("Pick a design (1-8)")
        img = tk.PhotoImage(file=path)
        label = tk.Label(root, image=img)
        label.image = img
        label.pack()
        root.attributes("-topmost", True)
        _preview_state["root"] = root
        root.mainloop()
    except Exception:
        pass


def show_previews():
    path = resource_path(os.path.join("assets", "design-previews.png"))
    if not os.path.exists(path):
        print("  (Preview image not found, picking blind, sorry.)")
        return
    t = threading.Thread(target=_tk_preview_worker, args=(path,), daemon=True)
    t.start()
    time.sleep(0.5)


def close_previews():
    root = _preview_state.get("root")
    if root is not None:
        try:
            root.quit()
        except Exception:
            pass
        _preview_state["root"] = None


def auto_close(seconds=5):
    for i in range(seconds, 0, -1):
        print(f"\rClosing in {i}...  ", end="", flush=True)
        time.sleep(1)
    print()


def ask_design():
    print("\nWhich design do you want? A preview image with all 8 just opened.\n")
    for i, name in enumerate(DESIGNS, 1):
        print(f"  {i}. {name}")
    while True:
        choice = input("\nType a number (1-8): ").strip()
        if choice.isdigit() and 1 <= int(choice) <= 8:
            return DESIGNS[int(choice) - 1]
        print("  Not a valid choice, try again.")


def ask_color():
    print("\nWant a custom accent color?")
    print("  Type a hex code (like ff7a00), or one of: " + ", ".join(COLOR_NAMES.keys()))
    print("  Or just press Enter to keep the design's default color.")
    while True:
        raw = input("\nColor: ").strip().lower().lstrip("#")
        if raw == "":
            return None
        if raw in COLOR_NAMES:
            return COLOR_NAMES[raw]
        if re.fullmatch(r"[0-9a-f]{6}", raw):
            return raw
        print("  That's not a basic color name or a 6-digit hex code, try again.")


def ask_display_choice():
    print("\nWhat should show below your rank: your season stats, or a creator code?")
    print("  1. Season stats (K/D, Win%, Kills, Wins)")
    print("  2. Creator code (\"Use Code X #ad\")")
    while True:
        choice = input("\nType 1 or 2: ").strip()
        if choice == "1":
            return ""
        if choice == "2":
            code = input("Enter your creator code: ").strip()
            code = code.replace(":)", "").replace(":-)", "").strip()
            if code:
                return code
            print("  That was empty, defaulting to stats instead.")
            return ""
        print("  Type 1 or 2.")


def ask_username():
    while True:
        name = input("\nWhat's your Epic display name? ").strip()
        if name:
            return name
        print("  Can't be empty.")


def lookup_account_id(username):
    print(f"\nLooking up account ID for '{username}'...")
    name_q = urllib.parse.quote(username)
    urls = [
        f"https://prod.api-fortnite.com/api/v1/account/displayName/{name_q}",
        f"https://prod.api-fortnite.com/api/v1/profile/progress?displayName={name_q}",
        f"https://prod.api-fortnite.com/api/v1/profile/stats?displayName={name_q}",
    ]
    headers = {
        "x-api-key": ACCOUNT_API_KEY,
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/125.0.0.0 Safari/537.36"),
    }
    for url in urls:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            continue

        account_id = data.get("accountId") or data.get("account_id") or data.get("id")
        if not account_id and isinstance(data.get("data"), dict):
            account_id = data["data"].get("accountId") or data["data"].get("account_id")
        if account_id:
            print(f"  Found it: {account_id}")
            return account_id

    print("  Couldn't find that account automatically.")
    return None


def ask_account_id_fallback(username):
    while True:
        print(f"\nLook up '{username}' manually at https://olitracker.com, then paste the account ID below.")
        manual = input("Account ID (or leave blank to retry the lookup): ").strip()
        if manual:
            return manual
        retry = input("Try the automatic lookup again? (Y/N): ").strip().lower()
        if retry == "y":
            return None
        print("  Continuing without an account ID, you can edit server.py and add it yourself later.")
        return ""


def confirm_summary(design, accent, display_choice, username, account_id):
    print("\n" + "-" * 56)
    print("Here's what you picked:")
    print(f"  Design:        {design}")
    print(f"  Accent color:  {'default' if accent is None else '#' + accent}")
    if display_choice:
        print(f"  Shows:         creator code ({display_choice})")
    else:
        print(f"  Shows:         season stats")
    print(f"  Username:      {username}")
    print(f"  Account ID:    {account_id or '(none set)'}")
    print("-" * 56)
    return input("Build it? (Y/N, or type 'r' to restart): ").strip().lower()


def build_overlay(design, accent, display_choice, username, account_id, dest_root):
    src_dir = resource_path(os.path.join("templates", design))
    dest_dir = os.path.join(dest_root, f"{design} Overlay")

    if os.path.exists(dest_dir):
        overwrite = input(f"\n'{dest_dir}' already exists. Overwrite it? (Y/N): ").strip().lower()
        if overwrite != "y":
            print("Cancelled.")
            return None
        shutil.rmtree(dest_dir)

    os.makedirs(dest_dir, exist_ok=True)
    for fname in ("account-id.bat", "start.bat", "stop.bat"):
        shutil.copy(os.path.join(src_dir, fname), os.path.join(dest_dir, fname))

    server_text = open(os.path.join(src_dir, "server.py"), encoding="utf-8").read()

    server_text = re.sub(r'EPIC_USERNAME\s*=\s*".*?"',
                          f'EPIC_USERNAME    = "{username}"', server_text, count=1)
    server_text = re.sub(r'EPIC_ACCOUNT_ID\s*=\s*".*?"',
                          f'EPIC_ACCOUNT_ID  = "{account_id or "your-account-id-here"}"',
                          server_text, count=1)
    server_text = re.sub(r'CREATOR_CODE\s*=\s*".*?"',
                          f'CREATOR_CODE     = "{display_choice}"', server_text, count=1)

    if accent is not None:
        r = int(accent[0:2], 16)
        g = int(accent[2:4], 16)
        b = int(accent[4:6], 16)
        server_text = re.sub(r'--accent:\s*#[0-9a-fA-F]{6};',
                              f'--accent: #{accent};', server_text, count=1)
        server_text = re.sub(r'--accent-rgb:\s*[0-9]+,\s*[0-9]+,\s*[0-9]+;',
                              f'--accent-rgb: {r}, {g}, {b};', server_text, count=1)

    with open(os.path.join(dest_dir, "server.py"), "w", encoding="utf-8") as f:
        f.write(server_text)

    return dest_dir


def main():
    banner()
    check_python()
    show_previews()

    while True:
        design = ask_design()
        close_previews()
        accent = ask_color()
        display_choice = ask_display_choice()
        username = ask_username()

        account_id = lookup_account_id(username)
        while account_id is None:
            account_id = ask_account_id_fallback(username)
            if account_id is None:
                account_id = lookup_account_id(username)

        answer = confirm_summary(design, accent, display_choice, username, account_id)
        if answer == "r":
            print()
            continue
        if answer == "y":
            break
        print("Cancelled.")
        return

    dest_dir = build_overlay(design, accent, display_choice, username, account_id, run_dir())
    if dest_dir is None:
        return

    print(f"\nDone. Your overlay is ready in:\n  {dest_dir}")
    print("\nIn OBS: add a Browser Source pointed at http://localhost:8888/overlay")

    launch = input("\nStart the overlay now? (Y/N): ").strip().lower()
    if launch == "y":
        start_bat = os.path.join(dest_dir, "start.bat")
        subprocess.Popen(
            ["cmd", "/c", start_bat],
            cwd=dest_dir,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        print("Starting...")

    print()
    auto_close(5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"\nSomething went wrong: {e}")
        input("Press Enter to close...")
