#!/usr/bin/env python3
"""Self-test of this gahk_web release: python3 -I selftest.py [--vendor DIR]

The updater runs it with the webhost's python3 before it switches to the release, with the packages
it has just installed for that python (--vendor; default: the release's vendor/). CI runs it on
Python 3.13 and 3.14 for every bundle. Only the release's own src/ and those packages are on sys.path
(any site-packages or dist-packages folder is removed), so it proves the release needs nothing else
installed. Exit status 0 means the release may go live.
"""

import sys
from pathlib import Path

RELEASE = Path(__file__).resolve().parent

args = sys.argv[1:]
if args and (len(args) != 2 or args[0] != "--vendor"):
    print(__doc__, file=sys.stderr)
    sys.exit(2)
VENDOR = Path(args[1]).resolve() if args else RELEASE / "vendor"

sys.path[:] = [str(RELEASE / "src"), str(VENDOR)] + [
    entry for entry in sys.path if "site-packages" not in entry and "dist-packages" not in entry
]

try:
    from gahk_web.selftest import main
except Exception as exc:  # an import failure is the most likely problem with a broken release
    print(f"selftest: cannot import gahk_web: {exc!r}", file=sys.stderr)
    sys.exit(1)

sys.exit(main(RELEASE, VENDOR))
