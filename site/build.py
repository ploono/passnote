"""Assemble the GitHub Pages site: everything in site/ (except this script) plus the brand
files the page uses from assets/, at the same relative paths (assets/...).

Usage: python3 site/build.py OUT_DIR
"""
from __future__ import annotations

import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = os.path.join(ROOT, "site")
MARKER = ".passnote-site"  # marks a folder this script built, so a rebuild may clear it
ASSETS = (
    "assets/logo-tight.svg",
    "assets/logo-tight-dark.svg",
    "assets/favicon.svg",
    "assets/sly.svg",
    "assets/sly-dark.svg",
    "assets/social-preview.png",
    "assets/readme/architecture.svg",
    "assets/readme/delivery.svg",
    "assets/readme/wake.svg",
)
SKIP = {"build.py", "__pycache__"}


class BuildError(Exception):
    pass


def build(out: str) -> str:
    out = os.path.abspath(out)
    if os.path.exists(out):
        if not os.path.isfile(os.path.join(out, MARKER)):
            raise BuildError(f"{out} exists and wasn't built by this script; pick an empty or new folder")
        shutil.rmtree(out)
    shutil.copytree(SITE, out, ignore=lambda folder, names: [n for n in names if n in SKIP])
    for rel in ASSETS:
        dest = os.path.join(out, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(os.path.join(ROOT, rel), dest)
    with open(os.path.join(out, MARKER), "w", encoding="utf-8") as fh:
        fh.write("built by site/build.py\n")
    return out


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        sys.stderr.write("usage: build.py OUT_DIR\n")
        return 2
    try:
        print(build(argv[0]))
    except BuildError as exc:
        sys.stderr.write(f"build.py: {exc}\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
