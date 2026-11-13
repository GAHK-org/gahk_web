# A fake Hetzner Webhosting S account for gahk_web's end-to-end test (scripts/fake_webhost/e2e.py).
#
# Debian trixie: Apache 2.4 (event MPM) with mod_rewrite, mod_headers, mod_expires, mod_ssl, mod_cgid
# and mod_fcgid, suEXEC, and the system python3 (3.13.5) with Debian's pip (25.1.1) and glibc 2.41:
# the webhost's exact versions, which e2e.py checks through a CGI script. One account, $LOGIN, with
# its home at /usr/home/$LOGIN and public_html directly under it; its scripts run as that user
# through suEXEC.
#
# What is KNOWN about the real webhost (checked on the account on 2026-11-12, DEPLOY.md):
#   - Apache ("Server: Apache"), .htaccess honoured; /usr/bin/python3 is CPython 3.13.5 on x86_64
#     with glibc 2.41 (what Debian trixie ships); Debian's pip 25.1.1 (/usr/lib/python3/dist-packages)
#     answers `python3 -m pip` and `python3 -I -m pip`, and `pip install --target` works from a CGI
#     request and reaches PyPI;
#   - the home is /usr/home/<login> with public_html directly under it;
#   - *.fcgi runs as FastCGI (mod_fcgid) and *.cgi/*.py as CGI anywhere under public_html, mapped
#     in the server configuration: no AddHandler or Options line in .htaccess is needed;
#   - a FastCGI process serves many requests, lives at least ~7 minutes idle, and mod_fcgid starts
#     a second one when the first is busy.
# What is GUESSED here (each a knob in entrypoint.sh, set with `docker run -e`, or a line below):
#   - suEXEC with the account's user and group (FAKE_SUEXEC=1); Hetzner may use something else
#     that runs scripts as the account (FAKE_SUEXEC=0: Apache itself runs as the account);
#   - mod_cgid (event MPM) rather than mod_cgi (prefork);
#   - mod_fcgid's limits: Debian's defaults (FCGID_* below); Hetzner's are unknown;
#   - how long a CGI script may run: Apache's Timeout, 300 s (APACHE_TIMEOUT);
#   - no server-wide ScriptAlias /cgi-bin/ (Debian's serve-cgi-bin.conf is switched off; with it,
#     every dynamic request would go to /usr/lib/cgi-bin and fail, DEPLOY.md section 13 item 1);
#   - TLS terminated by Apache itself (mod_ssl, %{HTTPS} = on), no Varnish in front;
#   - Options +ExecCGI +SymLinksIfOwnerMatch and AllowOverride All on public_html; DirectoryIndex
#     index.html.
# Not modelled at all: Hetzner's process, memory and CPU limits (and anything that kills long-running
# processes), Varnish, its PostgreSQL (a separate postgres:17 container stands in), konsoleH.
FROM debian:trixie-slim

ARG LOGIN=ext0e2e
ARG ACCOUNT_UID=2001

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      apache2 apache2-suexec-custom libapache2-mod-fcgid \
      python3 python3-pip ca-certificates openssl procps \
 && rm -rf /var/lib/apt/lists/*

# The account: its own user and group, home /usr/home/<login> (Apache must traverse it to reach
# public_html; gahk_web/ is the account's alone).
RUN groupadd -g "$ACCOUNT_UID" "$LOGIN" \
 && useradd -u "$ACCOUNT_UID" -g "$ACCOUNT_UID" -d "/usr/home/$LOGIN" -m -s /bin/sh "$LOGIN" \
 && chmod 711 "/usr/home/$LOGIN" \
 && install -d -o "$LOGIN" -g "$LOGIN" -m 755 "/usr/home/$LOGIN/public_html" \
 && install -d -o "$LOGIN" -g "$LOGIN" -m 700 "/usr/home/$LOGIN/gahk_web"

# A self-signed certificate for gahk.dk and www.gahk.dk, trusted inside the container, so the
# updater's own check of https://gahk.dk/api/health works as on the webhost.
RUN install -d /etc/ssl/fake-hetzner \
 && openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj /CN=gahk.dk \
      -addext "subjectAltName=DNS:gahk.dk,DNS:www.gahk.dk" \
      -keyout /etc/ssl/fake-hetzner/gahk.dk.key -out /etc/ssl/fake-hetzner/gahk.dk.crt 2>/dev/null \
 && cp /etc/ssl/fake-hetzner/gahk.dk.crt /usr/local/share/ca-certificates/fake-gahk.dk.crt \
 && update-ca-certificates >/dev/null

# suEXEC (Debian's configurable build): scripts must lie under /usr/home, belong to the account and
# not be writable by group or others, as with any suEXEC.
RUN printf '/usr/home\npublic_html\n' > /etc/apache2/suexec/www-data

RUN a2dismod -q -f mpm_prefork mpm_worker 2>/dev/null; a2enmod -q mpm_event \
 && a2enmod -q rewrite headers expires ssl cgid fcgid suexec mime deflate \
 && a2disconf -q serve-cgi-bin \
 && a2dissite -q 000-default \
 && install -d /srv/releases/download /srv/releases/storage /srv/intern/kalender /etc/apache2/fake-hetzner

COPY apache/gahk.conf /etc/apache2/sites-available/gahk.conf
COPY apache/test-services.conf /etc/apache2/sites-available/test-services.conf
RUN a2ensite -q gahk test-services

# Offline runs only: when the driver mounts a wheelhouse at /wheelhouse, the updater's pip takes the
# wheels from there (GAHK_WEB_PIP_FIND_LINKS, still checking every hash). suEXEC passes no variable
# of ours to a script, so the system python's sitecustomize sets it; without /wheelhouse it does
# nothing and pip goes to PyPI, as on the webhost.
RUN printf '%s\n' \
      'import os as _os' \
      'if _os.path.isdir("/wheelhouse") and not _os.environ.get("GAHK_WEB_PIP_FIND_LINKS"):' \
      '    _os.environ["GAHK_WEB_PIP_FIND_LINKS"] = "/wheelhouse"' \
      >> /etc/python3.13/sitecustomize.py

ENV GAHK_LOGIN=$LOGIN
COPY entrypoint.sh /usr/local/bin/fake-webhost
RUN chmod 755 /usr/local/bin/fake-webhost
EXPOSE 80 443
ENTRYPOINT ["/usr/local/bin/fake-webhost"]
