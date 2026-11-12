"""The CGI entry point: public_html/cgi-bin/gahk.cgi puts the active release on sys.path and calls
main(). Apache starts one Python process per request; the app reads its config on that request."""

from wsgiref.handlers import CGIHandler

from .app import config_error_app, create_app
from .config import ConfigError, load_config
from .web import WsgiApp


def main() -> None:
    app: WsgiApp
    try:
        app = create_app(load_config(), runner="cgi")
    except ConfigError as exc:
        app = config_error_app(str(exc))
    CGIHandler().run(app)


if __name__ == "__main__":
    main()
