#!/usr/bin/env python3
"""Cut a release.

Stamps a version into src/, rebuilds every design folder, and rewrites
update.json -- the file every installed overlay reads to find out whether it is
out of date. Doing it by hand across sixteen files is how versions drift, so
this is the only supported way to bump one.

    python tools/release.py 2.3.0
    python tools/release.py 2.3.0 --notes "Fixes ELO after the season reset"
    python tools/release.py 3.0.0 --min-supported 3.0.0 --notes "OliTracker changed its API"

--min-supported is the lever that makes an old build stop working. Anything
below it gets a "your version no longer works, update now?" prompt that cannot
be dismissed with "not now". Leave it alone for an ordinary release: it locks
people out mid-stream, so it is only for the case where an old build genuinely
cannot show correct data any more.

Nothing is pushed. The script prints the git commands to run once you have
looked at the diff, and the tag matters: installed overlays download from the
tag named in update.json, not from the branch, so update.json must never be
pushed before the tag it points at exists.
"""

import argparse
import io
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_HEAD = os.path.join(ROOT, "src", "core_head.py")
MANIFEST = os.path.join(ROOT, "update.json")

VERSION_RE = re.compile(r'^(VERSION\s*=\s*)"([^"]*)"', re.M)
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def read(path):
    with io.open(path, encoding="utf-8", newline="") as f:
        return f.read().replace("\r\n", "\n")


def write(path, text):
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def current_version():
    found = VERSION_RE.search(read(SRC_HEAD))
    if not found:
        raise SystemExit("could not find VERSION in src/core_head.py")
    return found.group(2)


def as_tuple(text):
    return tuple(int(p) for p in text.split("."))


def stamp_version(version):
    text = read(SRC_HEAD)
    text = VERSION_RE.sub(lambda m: f'{m.group(1)}"{version}"', text, count=1)
    write(SRC_HEAD, text)


def load_manifest():
    try:
        with io.open(MANIFEST, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_manifest(version, min_supported, notes):
    manifest = {
        "latest":        version,
        "min_supported": min_supported,
        "ref":           "v" + version,
        "notes":         notes,
        "files":         ["server.py", "overlay.bat"],
    }
    write(MANIFEST, json.dumps(manifest, indent=2) + "\n")
    return manifest


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("version", help="the version being released, e.g. 2.3.0")
    ap.add_argument("--notes", default="",
                    help="one line, shown in the update prompt people see")
    ap.add_argument("--min-supported", default=None,
                    help="oldest version still allowed to run (default: unchanged)")
    args = ap.parse_args()

    version = args.version.lstrip("v")
    if not SEMVER_RE.match(version):
        raise SystemExit(f"'{args.version}' is not a three-part version like 2.3.0")

    previous = current_version()
    if as_tuple(version) <= as_tuple(previous):
        raise SystemExit(
            f"{version} is not newer than the current {previous}. Installed "
            "overlays compare version numbers, so a release has to go up.")

    manifest = load_manifest()
    min_supported = args.min_supported or manifest.get("min_supported") or previous
    min_supported = str(min_supported).lstrip("v")
    if not SEMVER_RE.match(min_supported):
        raise SystemExit(f"'{min_supported}' is not a three-part version")
    if as_tuple(min_supported) > as_tuple(version):
        raise SystemExit("min_supported cannot be newer than the release itself")

    stamp_version(version)

    build = os.path.join(ROOT, "tools", "build.py")
    if subprocess.call([sys.executable, build]) != 0:
        raise SystemExit("build failed; nothing was released")

    write_manifest(version, min_supported, args.notes)

    forced = as_tuple(min_supported) > as_tuple(previous)
    print()
    print(f"  {previous} -> {version}")
    print(f"  min_supported: {min_supported}"
          + ("   <-- everything older is now blocked" if forced else ""))
    print(f"  notes: {args.notes or '(none)'}")
    if forced:
        print()
        print("  Anyone below that version gets a prompt they cannot postpone.")
        print("  Make sure that is what you meant.")
    print()
    print("Check the diff, then:")
    print(f"    git add -A && git commit -m \"v{version}: {args.notes or 'release'}\"")
    print(f"    git tag v{version}")
    print(f"    git push origin main --tags")
    print()
    print("Push the tag with the commit. Installed overlays download from the tag")
    print("named in update.json, so update.json must not land on main before it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
