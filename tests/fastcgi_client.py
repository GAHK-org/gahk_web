"""A FastCGI process as mod_fcgid runs it, for the tests: the listening socket on fd 0, and a client
that sends one request per connection (as mod_fcgid does) and reads the answer."""

import os
import socket
import struct
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

BEGIN_REQUEST, END_REQUEST, PARAMS, STDIN, STDOUT, STDERR = 1, 3, 4, 5, 6, 7
HEADER = struct.Struct("!BBHHBx")


def _pair(name: str, value: str) -> bytes:
    def length(data: bytes) -> bytes:
        return bytes([len(data)]) if len(data) < 128 else struct.pack("!I", len(data) | 0x80000000)

    name_bytes, value_bytes = name.encode("latin-1"), value.encode("latin-1")
    return length(name_bytes) + length(value_bytes) + name_bytes + value_bytes


def _record(kind: int, content: bytes = b"", request_id: int = 1) -> bytes:
    return HEADER.pack(1, kind, request_id, len(content), 0) + content


@dataclass
class Answer:
    status: int
    headers: dict[str, str]
    body: bytes
    stderr: bytes = b""

    @property
    def text(self) -> str:
        return self.body.decode("utf-8")


@dataclass
class FastCGIProcess:
    command: list[str]
    env: dict[str, str]
    log: Path
    listener: socket.socket = field(init=False)
    process: subprocess.Popen[bytes] = field(init=False)

    def __post_init__(self) -> None:
        self.listener = socket.create_server(("127.0.0.1", 0))
        with self.log.open("ab") as log:
            self.process = subprocess.Popen(
                self.command, stdin=self.listener.fileno(), stdout=log, stderr=log, env=self.env
            )

    def request(
        self,
        method: str,
        uri: str,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
        script: str = "/cgi-bin/gahk.fcgi",
    ) -> Answer:
        path, _, query = uri.partition("?")
        params = {
            "REQUEST_METHOD": method,
            "REQUEST_URI": uri,
            "SCRIPT_NAME": script,
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "CONTENT_LENGTH": str(len(body)),
            "SERVER_NAME": "gahk.dk",
            "SERVER_PORT": "443",
            "SERVER_PROTOCOL": "HTTP/1.1",
            "HTTPS": "on",
            "REMOTE_ADDR": "192.0.2.1",
        }
        for name, value in (headers or {}).items():
            key = name.upper().replace("-", "_")
            params[key if key == "CONTENT_TYPE" else "HTTP_" + key] = value
        encoded = b"".join(_pair(name, value) for name, value in params.items())
        message = (
            _record(BEGIN_REQUEST, struct.pack("!HB5x", 1, 0)) + _record(PARAMS, encoded) + _record(PARAMS)
        )
        for at in range(0, len(body), 60000):
            message += _record(STDIN, body[at : at + 60000])
        message += _record(STDIN)
        out, err = b"", b""
        with socket.create_connection(self.listener.getsockname(), timeout=60) as conn:
            conn.sendall(message)
            buffer = b""
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    raise AssertionError(f"the connection closed before END_REQUEST: {out!r}")
                buffer += chunk
                done = False
                while len(buffer) >= HEADER.size:
                    _version, kind, _rid, length, padding = HEADER.unpack(buffer[: HEADER.size])
                    if len(buffer) < HEADER.size + length + padding:
                        break
                    content = buffer[HEADER.size : HEADER.size + length]
                    buffer = buffer[HEADER.size + length + padding :]
                    if kind == STDOUT:
                        out += content
                    elif kind == STDERR:
                        err += content
                    elif kind == END_REQUEST:
                        done = True
                if done:
                    break
        head, _, content = out.partition(b"\r\n\r\n")
        response_headers = dict(line.split(": ", 1) for line in head.decode("latin-1").split("\r\n"))
        status = int(response_headers.pop("Status", "200").split()[0])
        return Answer(status, response_headers, content, err)

    def wait(self, timeout: float = 10) -> int:
        return self.process.wait(timeout=timeout)

    def alive(self) -> bool:
        return self.process.poll() is None

    def output(self) -> str:
        return self.log.read_text(encoding="utf-8", errors="replace")

    def stop(self) -> None:
        if self.alive():
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.listener.close()


def base_env(**extra: str) -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", ""), **extra}
