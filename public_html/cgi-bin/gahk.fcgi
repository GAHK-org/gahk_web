#!/usr/bin/python3
"""gahk.dk's FastCGI entry point: one long-lived process answers request after request.

Installed into ~/public_html/cgi-bin/ from each release beside gahk.cgi (mode 755), and uploaded
once by hand for the first install. Apache's mod_fcgid starts it on demand (more of them when
requests overlap) and hands it requests over the listening socket on fd 0. Everything but POST
/api/update (and request bodies too large for mod_fcgid) comes here; see .htaccess.

It loads its sibling gahk.cgi as a module and shares all of it: finding the home and the active
release, the check of the release's packages against this python, the sys.path that holds only the
release's src/ and vendor/, and what to answer while the release cannot run. Standard library only,
like gahk.cgi: it must work before any release is installed.

When the release can run, it serves with gahk_web.fcgi_entry (flup and the Flask app) until the
process should end (CURRENT or config.ini changed, enough requests, old enough). mod_fcgid starts a
fresh process for the next request.

When it cannot (no release yet, packages for another python, an import failure), it must not simply
exit: mod_fcgid would start it again for every request and soon throttle it as crashing. Instead a
minimal FastCGI responder below answers every request the way gahk.cgi would (the 503 page, health's
503 JSON, the signed status, even POST /api/update) for STANDIN_SECONDS, then exits so that the next
request starts a process that looks at the release again. It exits sooner, after the response in
which it sees that an install or a reinstall of the packages has finished, so the site comes up at
once rather than up to STANDIN_SECONDS later.
"""

import importlib.machinery
import importlib.util
import os
import select
import socket
import struct
import sys
import time
import traceback
from pathlib import Path
from typing import Any

STANDIN_SECONDS = 30.0
STANDIN_ENV = "GAHK_WEB_FCGI_STANDIN"  # tests shorten it
NAME = "gahk.fcgi"

# FastCGI record types (the FastCGI 1.0 specification).
BEGIN_REQUEST, ABORT_REQUEST, END_REQUEST, PARAMS, STDIN, STDOUT = 1, 2, 3, 4, 5, 6
GET_VALUES, GET_VALUES_RESULT = 9, 10
HEADER = struct.Struct("!BBHHBx")  # version, type, request id, content length, padding length
MAX_CONTENT = 65535
UNAVAILABLE = (
    b"Status: 503 Service Unavailable\r\nContent-Type: text/plain; charset=utf-8\r\n"
    b"Cache-Control: no-store\r\nRetry-After: 300\r\n\r\n" + "Siden opdateres. Prøv igen om lidt.\n".encode()
)


def load_stub() -> Any:  # noqa: ANN401 - the gahk.cgi module
    """gahk.cgi beside this file, as a module (it is not run: its main() is not called)."""
    path = Path(__file__).absolute().with_name("gahk.cgi")
    loader = importlib.machinery.SourceFileLoader("gahk_cgi_stub", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up there
    write_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True  # no __pycache__ in public_html
    try:
        loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = write_bytecode
    return module


# --- the stand-in: a minimal FastCGI responder ---


def read_exact(conn: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = conn.recv(size - len(data))
        if not chunk:
            raise EOFError("the web server closed the connection")
        data += chunk
    return data


def read_record(conn: socket.socket) -> tuple[int, int, bytes]:
    _version, kind, request_id, length, padding = HEADER.unpack(read_exact(conn, HEADER.size))
    content = read_exact(conn, length + padding)[:length]
    return kind, request_id, content


def write_record(conn: socket.socket, kind: int, request_id: int, content: bytes = b"") -> None:
    chunks = [content[at : at + MAX_CONTENT] for at in range(0, len(content), MAX_CONTENT)] or [b""]
    conn.sendall(b"".join(HEADER.pack(1, kind, request_id, len(chunk), 0) + chunk for chunk in chunks))


def name_value_pairs(data: bytes) -> dict[str, str]:
    """FastCGI's PARAMS: lengths of one byte, or four with the high bit set; latin-1 as in WSGI."""
    pairs: dict[str, str] = {}
    at = 0
    while at < len(data):
        lengths = []
        for _ in range(2):
            if data[at] >> 7:
                lengths.append(struct.unpack("!I", data[at : at + 4])[0] & 0x7FFFFFFF)
                at += 4
            else:
                lengths.append(data[at])
                at += 1
        name = data[at : at + lengths[0]].decode("latin-1")
        at += lengths[0]
        pairs[name] = data[at : at + lengths[1]].decode("latin-1")
        at += lengths[1]
    return pairs


def respond(conn: socket.socket, stub: Any, install: Any) -> None:  # noqa: ANN401
    """Read one request (parameters, then the body), answer it as gahk.cgi would."""
    request_id = 0
    params, body, too_large = b"", b"", False
    limit = stub.MAX_UPDATE_BODY if stub is not None else 0
    while True:
        kind, record_id, content = read_record(conn)
        if kind == GET_VALUES:
            write_record(conn, GET_VALUES_RESULT, 0)
        elif kind == BEGIN_REQUEST:
            request_id = record_id
        elif record_id != request_id:
            continue
        elif kind == ABORT_REQUEST:
            return
        elif kind == PARAMS:
            params += content
        elif kind == STDIN and content:
            too_large = too_large or len(body) + len(content) > limit
            body = b"" if too_large else body + content
        elif kind == STDIN:
            break
    if stub is None:
        output = UNAVAILABLE
    else:
        environ = name_value_pairs(params)
        result = stub.stub_answer(install, environ, lambda: None if too_large else body, NAME)
        output = stub.cgi_response(result or stub.unavailable_answer(install, environ, NAME))
    write_record(conn, STDOUT, request_id, output)
    write_record(conn, STDOUT, request_id)
    write_record(conn, END_REQUEST, request_id, struct.pack("!IB3x", 0, 0))


def stand_in(stub: Any, install: Any, located: Any = None) -> None:  # noqa: ANN401
    """Answer requests on mod_fcgid's socket for STANDIN_SECONDS, then return; return sooner, after
    the response, once the release it stands in for has changed (`located`, what gahk.cgi's locate()
    found at the start): an install or a reinstall of the packages has finished, and the next
    request should get a process that runs it."""
    try:
        seconds = float(os.environ.get(STANDIN_ENV) or STANDIN_SECONDS)
    except ValueError:
        seconds = STANDIN_SECONDS
    deadline = time.monotonic() + seconds
    listener = socket.socket(fileno=os.dup(0))
    try:
        while (remaining := deadline - time.monotonic()) > 0:
            if not select.select([listener], [], [], remaining)[0]:
                break
            try:
                conn, _ = listener.accept()
            except (BlockingIOError, InterruptedError):
                continue
            with conn:
                conn.settimeout(30)
                try:
                    respond(conn, stub, install)
                except (OSError, EOFError, ValueError, struct.error) as exc:
                    print(f"{NAME}: a request failed: {exc!r}", file=sys.stderr)
            if located is not None and release_changed(stub, located):
                print(f"{NAME}[{os.getpid()}]: exiting (the release changed)", file=sys.stderr)
                break
    finally:
        listener.close()


def release_changed(stub: Any, located: Any) -> bool:  # noqa: ANN401
    """Whether the active release, or whether its packages fit this python, is no longer what the
    process found at its start (a few small file reads; only while standing in)."""
    try:
        now = stub.locate()
    except Exception:
        return False
    return bool((now.sha, now.problem) != (located.sha, located.problem))


def main() -> None:
    try:
        stub = load_stub()
        located = install = stub.locate()
    except Exception:  # gahk.cgi is missing or broken: still no crash loop
        traceback.print_exc(file=sys.stderr)
        stand_in(None, None)
        return
    if not install.problem:
        try:
            stub.isolate(install.release, __file__)
            from gahk_web.fcgi_entry import serve  # only importable once sys.path is set

            reason = serve()
        except Exception as exc:
            install = stub.cannot_start(install, exc)
        else:
            print(f"{NAME}[{os.getpid()}]: exiting ({reason})", file=sys.stderr)
            return
    print(f"{NAME}[{os.getpid()}]: answering 503 for a while: {install.problem}", file=sys.stderr)
    stand_in(stub, install, located)


if __name__ == "__main__":
    main()
