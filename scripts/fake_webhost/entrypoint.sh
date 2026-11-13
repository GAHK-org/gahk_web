#!/bin/sh
# Starts the fake webhost's Apache in the foreground. The guesses about Hetzner (Dockerfile header)
# are knobs, set with `docker run -e NAME=value`:
#
#   FAKE_SUEXEC=1             scripts run through suEXEC as the account (0: Apache itself runs as
#                             the account, no suEXEC checks, no cleaned environment)
#   FCGID_MAX_REQUEST_LEN     mod_fcgid's FcgidMaxRequestLen   (default 131072, mod_fcgid's own)
#   FCGID_IO_TIMEOUT          FcgidIOTimeout, seconds           (default 40)
#   FCGID_IDLE_TIMEOUT        FcgidIdleTimeout, seconds         (default 300)
#   FCGID_MAX_PROCESSES       FcgidMaxProcessesPerClass         (default 100)
#   FCGID_PROCESS_LIFETIME    FcgidProcessLifeTime, seconds     (default 3600)
#   APACHE_TIMEOUT            Timeout, seconds (default 300, Debian's): mod_cgid waits this long for
#                             a CGI script's output (it has no CGIScriptTimeout of its own)
#
# The driver changes a knob on a running container by rewriting knobs.conf and reloading Apache
# (`fake-webhost reload APACHE_TIMEOUT=5`).
set -eu

conf=/etc/apache2/fake-hetzner

write_knobs() {
    {
        echo "# Written by fake-webhost from its environment; see entrypoint.sh."
        echo "FcgidMaxRequestLen ${FCGID_MAX_REQUEST_LEN:-131072}"
        echo "FcgidIOTimeout ${FCGID_IO_TIMEOUT:-40}"
        echo "FcgidIdleTimeout ${FCGID_IDLE_TIMEOUT:-300}"
        echo "FcgidMaxProcessesPerClass ${FCGID_MAX_PROCESSES:-100}"
        echo "FcgidProcessLifeTime ${FCGID_PROCESS_LIFETIME:-3600}"
        echo "Timeout ${APACHE_TIMEOUT:-300}"
    } > "$conf/knobs.conf"
    if [ "${FAKE_SUEXEC:-1}" = 1 ]; then
        echo "SuexecUserGroup ${GAHK_LOGIN} ${GAHK_LOGIN}" > "$conf/identity.conf"
        sed -i "s/^export APACHE_RUN_USER=.*/export APACHE_RUN_USER=www-data/;s/^export APACHE_RUN_GROUP=.*/export APACHE_RUN_GROUP=www-data/" /etc/apache2/envvars
    else
        echo "# no suEXEC: Apache itself runs as the account" > "$conf/identity.conf"
        sed -i "s/^export APACHE_RUN_USER=.*/export APACHE_RUN_USER=${GAHK_LOGIN}/;s/^export APACHE_RUN_GROUP=.*/export APACHE_RUN_GROUP=${GAHK_LOGIN}/" /etc/apache2/envvars
    fi
    if ! grep -q "fake-hetzner/knobs.conf" /etc/apache2/apache2.conf; then
        echo "Include $conf/knobs.conf" >> /etc/apache2/apache2.conf
    fi
}

if [ "${1:-}" = reload ]; then
    shift
    for setting in "$@"; do
        export "$setting"
    done
    write_knobs
    exec apache2ctl -k graceful
fi

write_knobs
# mod_fcgid's sockets, owned by the user Apache runs as (it creates them before suEXEC hands them on).
set +u  # envvars reads variables that may be unset
. /etc/apache2/envvars
set -u
install -d -o "$APACHE_RUN_USER" -g "$APACHE_RUN_GROUP" -m 755 /var/lib/apache2/fcgid /var/lib/apache2/fcgid/sock
apache2ctl configtest
exec apache2ctl -D FOREGROUND
