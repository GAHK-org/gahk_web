"""The CGI entry point: public_html/cgi-bin/gahk.cgi puts the active release on sys.path and calls
main(). Apache starts one Python process per request; the app reads its config on that request."""

from typing import IO, Any
from wsgiref.handlers import CGIHandler

from .app import config_error_app, create_app
from .config import ConfigError, load_config
from .web import StartResponse, WsgiApp

CHUNK = 65536


class CountingInput:
    """The request body as the app reads it, counting the bytes it took."""

    def __init__(self, stream: IO[bytes]) -> None:
        self._stream = stream
        self.consumed = 0

    def read(self, size: int = -1) -> bytes:
        data = self._stream.read(size)
        self.consumed += len(data)
        return data

    def readline(self, size: int = -1) -> bytes:
        data = self._stream.readline(size)
        self.consumed += len(data)
        return data


def draining(app: WsgiApp) -> WsgiApp:
    """Apache writes the whole request body to a CGI script before it reads the answer, and a script
    that ends without reading it loses its answer: the client gets an empty reply. So what the app
    left of the body (over a route's limit, a beacon that is too long) is read and dropped, never
    more than CONTENT_LENGTH announced."""

    def application(environ: dict[str, Any], start_response: StartResponse) -> Any:  # noqa: ANN401
        counted = CountingInput(environ["wsgi.input"])
        environ["wsgi.input"] = counted
        result = app(environ, start_response)
        try:
            left = int(environ.get("CONTENT_LENGTH") or 0) - counted.consumed
        except ValueError:
            left = 0
        while left > 0 and (chunk := counted.read(min(CHUNK, left))):
            left -= len(chunk)
        return result

    return application


def main() -> None:
    app: WsgiApp
    try:
        app = create_app(load_config(), runner="cgi")
    except ConfigError as exc:
        app = config_error_app(str(exc))
    CGIHandler().run(draining(app))


if __name__ == "__main__":
    main()
