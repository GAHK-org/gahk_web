"""Under CGI (POST /api/update's fallback, large bodies, the operator's CGI-only switch) every request
starts a fresh python, and a persistent FastCGI process pays for whatever it imports in memory: what
only some requests need is imported when they need it."""

import json
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
# Needed only by the requests that use them (publishing images, sanitising, the calendar
# pass-through, the updater), never at start-up. Flask brings jinja2 and markupsafe along.
HEAVY = ("PIL", "nh3", "urllib.request", "tarfile", "html.parser")


def test_starting_the_app_imports_only_what_every_request_needs() -> None:
    code = (
        "import json,sys;sys.path.insert(0,sys.argv[1]);import gahk_web.cgi_entry;"
        "print(json.dumps(sorted(sys.modules)))"
    )
    done = subprocess.run([sys.executable, "-c", code, str(SRC)], capture_output=True, check=True, text=True)
    loaded = set(json.loads(done.stdout))

    assert not loaded & set(HEAVY)
    assert "psycopg" not in loaded  # imported when an app makes its database engine
