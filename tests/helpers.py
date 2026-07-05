"""Shared test doubles: scripted telnet transport, canned CLI, canned HTTP."""

import re

from jap.cisco_cli import TransportTimeout


class Step:
    """One scripted exchange: when `expect` is sent, `chunks` become readable.

    expect may be bytes (exact match) or a compiled regex. chunks entries may
    be bytes (served by recv) or Exception instances (raised by recv).
    """

    def __init__(self, expect, chunks):
        self.expect = expect
        self.chunks = list(chunks)

    def matches(self, data: bytes) -> bool:
        if isinstance(self.expect, bytes):
            return data == self.expect
        return bool(self.expect.search(data))


class FakeTransport:
    """Deterministic transport: recv() serves queued chunks or raises
    TransportTimeout immediately (no real sleeps)."""

    def __init__(self, preload=(), steps=(), connect_error=None):
        self.pending = list(preload)
        self.steps = list(steps)
        self.connect_error = connect_error
        self.sent = []
        self.connected = False
        self.closed = False

    def connect(self, host, port, timeout):
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = True

    def send(self, data: bytes):
        self.sent.append(data)
        for i, step in enumerate(self.steps):
            if step.matches(data):
                self.steps.pop(i)
                self.pending.extend(step.chunks)
                break

    def recv(self, max_bytes, timeout):
        if not self.pending:
            raise TransportTimeout("no scripted data left")
        item = self.pending.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True


def make_factory(*transports):
    """transport_factory yielding the given transports in order."""
    iterator = iter(transports)

    def factory():
        return next(iterator)

    return factory


LOGIN_STEPS = [
    Step(b"cisco\r\n", [b"\r\nPassword:"]),
    Step(b"secret\r\n", [b"\r\nswitch#"]),
    Step(b"terminal datadump\r\n", [b"terminal datadump\r\nswitch#"]),
]
LOGIN_PRELOAD = [b"\r\nUser Name:"]

NO_AUTH_PRELOAD = [b"\r\nswitch#"]
NO_AUTH_STEPS = [Step(b"terminal datadump\r\n", [b"terminal datadump\r\nswitch#"])]


def command_step(cmd: str, body: str, prompt: str = "switch#"):
    """A step answering `cmd` with echo + body + prompt, like the real switch."""
    payload = f"{cmd}\r\n{body}{prompt}".encode()
    return Step(cmd.encode() + b"\r\n", [payload])


class FakeCli:
    """Stands in for CiscoCliClient above the transport layer: records every
    command and answers from a canned {command: output} table."""

    def __init__(self, responses=None, default=""):
        self.responses = dict(responses or {})
        self.default = default
        self.commands = []
        self.command_timeouts = []

    def run_command(self, cmd, *, timeout=None):
        return self.run_commands([cmd], timeout=timeout)[0]

    def run_commands(self, cmds, *, timeout=None):
        outputs = []
        for cmd in cmds:
            self.commands.append(cmd)
            self.command_timeouts.append(timeout)
            outputs.append(self.responses.get(cmd, self.default))
        return outputs

    def connect(self):
        pass

    def close(self):
        pass

    def is_connected(self):
        return True


class FakeHttp:
    """Callable http layer for JustApiClient: (method, url, body, timeout) ->
    (status, bytes). Routes by regex over 'METHOD url'."""

    def __init__(self, routes=None):
        # routes: list of (pattern, status, body_bytes_or_callable_or_Exception)
        self.routes = list(routes or [])
        self.calls = []  # (method, url, body, timeout)

    def add(self, pattern, status, body):
        self.routes.append((re.compile(pattern), status, body))

    def __call__(self, method, url, body, timeout):
        self.calls.append((method, url, body, timeout))
        target = f"{method} {url}"
        for pattern, status, response in self.routes:
            pattern_re = pattern if hasattr(pattern, "search") else re.compile(pattern)
            if pattern_re.search(target):
                if isinstance(response, Exception):
                    raise response
                if callable(response):
                    return response(method, url, body)
                return (status, response)
        raise OSError(f"no route for {target}")
