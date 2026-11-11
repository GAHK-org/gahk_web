"""The CGI entry point: public_html/cgi-bin/gahk.cgi puts the active release on sys.path and calls
main(). Apache starts one Python process per request; the app reads its config on that request."""

from wsgiref.handlers import CGIHandler

from .app import make_app


def main() -> None:
    CGIHandler().run(make_app())


if __name__ == "__main__":
    main()
