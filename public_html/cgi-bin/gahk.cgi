#!/usr/bin/env python3
"""gahk.dk's CGI entry point: hands each request to the active release of gahk_web.

Installed into ~/public_html/cgi-bin/ from each release (mode 755). It only finds the release and
starts it; all the logic lives in the release, so this stub rarely changes:

    ~/gahk_web/CURRENT              the active release (a git sha)
    ~/gahk_web/releases/<sha>/src   the gahk_web package
    ~/gahk_web/releases/<sha>/vendor   its pure-Python dependencies

Standard library only, and Python 3.11 compatible (the webhost's python3; 3.13 after its update).
"""

import os
import pwd
import re
import sys
from pathlib import Path


def find_home() -> Path:
    """The FTP home that holds gahk_web/ and public_html/.

    [VERIFY ON HETZNER] The script may be reached through a path that is not under the FTP home
    (/usr/www/users/<login>/ vs /usr/home/<login>/), so try, in order: $GAHK_WEB_HOME (SetEnv in
    .htaccess), the account's home directory (CGI runs as the account), the folder above
    public_html.
    """
    candidates = []
    if os.environ.get("GAHK_WEB_HOME"):
        candidates.append(Path(os.environ["GAHK_WEB_HOME"]))
    try:
        candidates.append(Path(pwd.getpwuid(os.getuid()).pw_dir))
    except (KeyError, OSError):
        pass
    candidates.append(Path(__file__).absolute().parent.parent.parent)
    for candidate in candidates:
        if (candidate / "gahk_web").is_dir():
            return candidate
    return candidates[-1]


def plain(status: str, message: str) -> None:
    sys.stdout.write(
        f"Status: {status}\r\nContent-Type: text/plain; charset=utf-8\r\n"
        f"Cache-Control: no-store\r\nRetry-After: 300\r\n\r\n{message}\n"
    )
    sys.stdout.flush()


def main() -> None:
    home = find_home()
    os.environ["GAHK_WEB_HOME"] = str(home)
    app_home = home / "gahk_web"

    # ---- HOOK (self-updater) -------------------------------------------------------------------
    # POST /api/update must work even before any release is installed (bootstrap) and when the
    # active release is broken. The updater (~/gahk_web/bin/updater.py, standard library only)
    # handles a signed POST /api/update itself, here, before the release is imported.
    # ---------------------------------------------------------------------------------------------

    try:
        sha = (app_home / "CURRENT").read_text(encoding="utf-8").strip()
    except OSError:
        sha = ""
    release = app_home / "releases" / sha
    if not re.fullmatch(r"[0-9a-f]{7,40}", sha) or not (release / "src" / "gahk_web").is_dir():
        plain("503 Service Unavailable", "gahk.dk er ved at blive installeret. Prøv igen om lidt.")
        return

    sys.path[:0] = [str(release / "src"), str(release / "vendor")]
    from gahk_web.cgi_entry import main as run  # only importable once sys.path is set

    run()


if __name__ == "__main__":
    main()
