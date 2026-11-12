"""The CGI app starts a fresh python for every dynamic request, so what it imports is paid each time."""

import json
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
# Needed only by the requests that use them (publishing images, sanitising, rendering, the calendar
# pass-through, the updater), never at start-up.
HEAVY = ("PIL", "nh3", "jinja2", "urllib.request", "tarfile", "html.parser", "markupsafe")


def test_starting_the_app_imports_only_what_every_request_needs() -> None:
    code = (
        "import json,sys;sys.path.insert(0,sys.argv[1]);import gahk_web.cgi_entry;"
        "print(json.dumps(sorted(sys.modules)))"
    )
    done = subprocess.run([sys.executable, "-c", code, str(SRC)], capture_output=True, check=True, text=True)
    loaded = set(json.loads(done.stdout))

    assert not loaded & set(HEAVY)
    assert "psycopg" not in loaded  # imported on the first database connection
