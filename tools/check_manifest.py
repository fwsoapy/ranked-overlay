#!/usr/bin/env python3
"""Sanity-check update.json.

Every installed overlay reads this file to decide whether it is out of date and
whether it is still allowed to run. A typo in it reaches everybody at once, so
it is worth failing a build over.

    python tools/check_manifest.py
"""

import io
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DESIGNS = ["Minimal", "Classic", "Sharp", "Wide", "Slash", "Rainbow", "Modern", "Pulse"]
ALLOWED_FILES = {"server.py", "overlay.bat"}
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def read(path):
    with io.open(path, encoding="utf-8", newline="") as f:
        return f.read().replace("\r\n", "\n")


def as_tuple(text):
    return tuple(int(p) for p in text.split("."))


def built_version():
    found = re.search(r'^VERSION\s*=\s*"([^"]*)"', read(os.path.join(ROOT, "src", "core_head.py")), re.M)
    return found.group(1) if found else None


def main():
    problems = []

    try:
        manifest = json.loads(read(os.path.join(ROOT, "update.json")))
    except (OSError, ValueError) as e:
        print(f"update.json could not be read: {e}")
        return 1

    latest = str(manifest.get("latest") or "")
    min_supported = str(manifest.get("min_supported") or "")
    ref = str(manifest.get("ref") or "")
    files = manifest.get("files") or []

    if not SEMVER.match(latest):
        problems.append(f"latest ({latest!r}) is not a version like 2.3.0")
    if not SEMVER.match(min_supported):
        problems.append(f"min_supported ({min_supported!r}) is not a version like 2.3.0")

    if SEMVER.match(latest) and SEMVER.match(min_supported):
        if as_tuple(min_supported) > as_tuple(latest):
            problems.append(
                f"min_supported ({min_supported}) is newer than latest ({latest}); "
                "that would block every install with nothing to update to")

    if ref != "v" + latest:
        problems.append(f"ref ({ref!r}) should be 'v{latest}' -- installs download from that tag")

    unknown = [f for f in files if f not in ALLOWED_FILES]
    if unknown:
        problems.append(f"files lists something the updater will not install: {unknown}")
    if "server.py" not in files:
        problems.append("files must include server.py")

    version = built_version()
    if version is None:
        problems.append("could not read VERSION from src/core_head.py")
    elif version != latest:
        problems.append(
            f"update.json says the newest build is {latest}, but src/ builds {version}. "
            "Run: python tools/release.py <version>")

    # The design folders are what installs actually download from.
    for design in DESIGNS:
        path = os.path.join(ROOT, "wizard", "templates", design, "server.py")
        if not os.path.exists(path):
            problems.append(f"wizard/templates/{design}/server.py is missing")
            continue
        found = re.search(r'^VERSION\s*=\s*"([^"]*)"', read(path), re.M)
        if not found:
            problems.append(f"{design}: no VERSION in the built server.py")
        elif found.group(1) != latest:
            problems.append(f"{design}: built as {found.group(1)}, manifest says {latest}")

    if problems:
        print("update.json is not consistent with the repo:\n")
        for p in problems:
            print("  - " + p)
        return 1

    print(f"update.json OK: latest {latest}, min_supported {min_supported}, ref {ref}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
