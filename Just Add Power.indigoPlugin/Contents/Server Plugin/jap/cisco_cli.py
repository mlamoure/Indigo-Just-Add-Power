"""Minimal telnet CLI client for Cisco SG300-family switches.

Built on raw sockets (stdlib only) because telnetlib was removed from the
standard library in Python 3.13. The exchange the SG300/SG350 CLI needs is
small: minimal IAC refusal, an optional login handshake, line-in/prompt-out.

The transport is injectable so the full client is unit-testable against a
scripted fake with no real sockets or sleeps.

run_command() returns the full session text for the command — including the
echoed command line and the trailing prompt line — which matches the captured
fixture files byte-for-byte; parsers are written to tolerate both lines.
"""

try:
    import indigo  # noqa: F401
except ImportError:
    pass

import logging
import re
import socket
import threading
import time

logger = logging.getLogger("Plugin")

# Telnet protocol bytes
IAC = 255
DONT, DO, WONT, WILL = 254, 253, 252, 251
SB, SE = 250, 240

DEFAULT_COMMAND_TIMEOUT = 5.0
DEFAULT_CONNECT_TIMEOUT = 5.0


class CiscoCliError(Exception):
    pass


class CiscoCliConnectError(CiscoCliError):
    pass


class CiscoCliAuthError(CiscoCliError):
    pass


class CiscoCliTimeout(CiscoCliError):
    pass


class TransportTimeout(Exception):
    """Raised by a transport when recv() exceeds its timeout."""


class SocketTransport:
    """Real TCP transport."""

    def __init__(self):
        self._sock = None

    def connect(self, host: str, port: int, timeout: float) -> None:
        self._sock = socket.create_connection((host, port), timeout=timeout)

    def send(self, data: bytes) -> None:
        self._sock.sendall(data)

    def recv(self, max_bytes: int, timeout: float) -> bytes:
        self._sock.settimeout(max(timeout, 0.001))
        try:
            return self._sock.recv(max_bytes)
        except socket.timeout as exc:
            raise TransportTimeout(str(exc)) from exc

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None


class TelnetFilter:
    """Stateful IAC handler: strips telnet negotiation from a byte stream and
    produces refusal replies (DO->WONT, WILL->DONT). Survives sequences split
    across feed() calls."""

    NORMAL, IAC_SEEN, OPT_CMD, SUBNEG, SUBNEG_IAC = range(5)

    def __init__(self):
        self._state = self.NORMAL
        self._cmd = None

    def feed(self, data: bytes):
        """Returns (clean_data, replies_to_send)."""
        out = bytearray()
        replies = bytearray()
        for b in data:
            if self._state == self.NORMAL:
                if b == IAC:
                    self._state = self.IAC_SEEN
                else:
                    out.append(b)
            elif self._state == self.IAC_SEEN:
                if b == IAC:
                    out.append(IAC)  # escaped literal 0xFF
                    self._state = self.NORMAL
                elif b in (DO, DONT, WILL, WONT):
                    self._cmd = b
                    self._state = self.OPT_CMD
                elif b == SB:
                    self._state = self.SUBNEG
                else:
                    self._state = self.NORMAL  # NOP/GA/etc: strip
            elif self._state == self.OPT_CMD:
                if self._cmd == DO:
                    replies.extend(bytes([IAC, WONT, b]))
                elif self._cmd == WILL:
                    replies.extend(bytes([IAC, DONT, b]))
                self._cmd = None
                self._state = self.NORMAL
            elif self._state == self.SUBNEG:
                if b == IAC:
                    self._state = self.SUBNEG_IAC
            elif self._state == self.SUBNEG_IAC:
                if b == SE:
                    self._state = self.NORMAL
                else:
                    # IAC IAC inside subneg = escaped byte; anything else we
                    # stay in the subnegotiation and keep discarding.
                    self._state = self.SUBNEG
        return bytes(out), bytes(replies)


# Prompt at end of buffer: "switch#", "switch(config)#", "switch(config-if)#", "switch>"
PROMPT_RE = re.compile(rb"(?:^|[\r\n])[^\r\n]*?[>#] ?$")
LOGIN_RE = re.compile(rb"User Name:\s*$")
PASSWD_RE = re.compile(rb"Password:\s*$")
MORE_RE = re.compile(rb"--More--")
# Erase artifacts the pager leaves after --More--: CR, spaces, backspaces.
_PAGER_JUNK_RE = re.compile(rb"--More--[ \x08\r]*")


class CiscoCliClient:
    """One serialized telnet CLI session. Thread-safe: all public entry points
    take the session lock; concurrent callers queue rather than interleave."""

    def __init__(
        self,
        host: str,
        port: int = 23,
        username: str | None = None,
        password: str | None = None,
        *,
        transport_factory=SocketTransport,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        command_timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self._transport_factory = transport_factory
        self._connect_timeout = connect_timeout
        self._command_timeout = command_timeout
        self._lock = threading.RLock()
        self._transport = None
        self._filter = None
        self._buf = b""

    # -- connection lifecycle ------------------------------------------------

    def is_connected(self) -> bool:
        return self._transport is not None

    def connect(self) -> None:
        with self._lock:
            if self._transport is None:
                self._connect()

    def close(self) -> None:
        with self._lock:
            if self._transport is not None:
                try:
                    self._transport.close()
                finally:
                    self._transport = None
                    self._filter = None
                    self._buf = b""

    def _connect(self) -> None:
        logger.debug("CLI connecting to %s:%s", self.host, self.port)
        transport = self._transport_factory()
        try:
            transport.connect(self.host, self.port, self._connect_timeout)
        except OSError as exc:
            raise CiscoCliConnectError(
                f"Could not connect to {self.host}:{self.port}: {exc}"
            ) from exc
        self._transport = transport
        self._filter = TelnetFilter()
        self._buf = b""
        try:
            self._login()
            # Best effort: disable the --More-- pager for this session. The
            # read loop still handles the pager if this fails.
            self._buf = b""
            self._send_line("terminal datadump")
            self._read_until([PROMPT_RE], self._command_timeout)
        except CiscoCliError:
            self.close()
            raise

    def _login(self) -> None:
        m = self._read_until(
            [LOGIN_RE, PASSWD_RE, PROMPT_RE], self._command_timeout, context="login"
        )
        if m.re is PROMPT_RE:
            logger.debug("CLI: no login prompt (auth-disabled switch)")
            return
        if m.re is LOGIN_RE:
            if not self.username:
                raise CiscoCliAuthError(
                    "Switch asked for a username but none is configured"
                )
            self._buf = b""
            self._send_line(self.username)
            m = self._read_until(
                [PASSWD_RE, PROMPT_RE, LOGIN_RE], self._command_timeout, context="login"
            )
            if m.re is PROMPT_RE:
                return
            if m.re is LOGIN_RE:
                raise CiscoCliAuthError("Switch rejected the username")
        # Password stage
        self._buf = b""
        self._send_line(self.password or "", mask=True)
        m = self._read_until(
            [PROMPT_RE, LOGIN_RE, PASSWD_RE], self._command_timeout, context="login"
        )
        if m.re is not PROMPT_RE:
            raise CiscoCliAuthError("Switch rejected the configured credentials")
        logger.debug("CLI: logged in to %s", self.host)

    # -- command execution ---------------------------------------------------

    def run_command(self, cmd: str, *, timeout: float | None = None) -> str:
        return self.run_commands([cmd], timeout=timeout)[0]

    def run_commands(self, cmds, *, timeout: float | None = None):
        """Run a list of commands in order, holding the session lock for the
        entire sequence (used for atomic config-mode sessions). On the first
        transport failure the whole remaining sequence is retried once on a
        fresh connection."""
        with self._lock:
            try:
                return self._run_commands_once(cmds, timeout)
            except (CiscoCliTimeout, CiscoCliConnectError) as exc:
                logger.debug("CLI failure (%s); reconnecting for one retry", exc)
                self.close()
                return self._run_commands_once(cmds, timeout)

    def _run_commands_once(self, cmds, timeout):
        if self._transport is None:
            self._connect()
        outputs = []
        for cmd in cmds:
            self._buf = b""
            self._send_line(cmd)
            m = self._read_until(
                [PROMPT_RE, PASSWD_RE], timeout or self._command_timeout, context=cmd
            )
            if m.re is PASSWD_RE:
                # Privilege escalation prompt (e.g. `enable` for a non-15 user):
                # answer with the login password and wait for the real prompt.
                self._send_line(self.password or "", mask=True)
                self._read_until(
                    [PROMPT_RE], timeout or self._command_timeout, context=cmd
                )
            outputs.append(self._buf.decode("utf-8", errors="replace"))
        return outputs

    def run_dialog(self, exchanges, *, timeout: float | None = None):
        """Run a scripted dialog with NO reconnect-retry — for non-idempotent
        flows like `reload` confirmation. `exchanges` is a list of
        (line_to_send, expect_patterns) where expect_patterns is a list of
        compiled byte regexes, or None to fire-and-forget. Returns the decoded
        output for each exchange ('' for fire-and-forget steps)."""
        with self._lock:
            if self._transport is None:
                self._connect()
            outputs = []
            for line, patterns in exchanges:
                self._buf = b""
                self._send_line(line)
                if patterns:
                    self._read_until(
                        list(patterns), timeout or self._command_timeout, context=line
                    )
                outputs.append(self._buf.decode("utf-8", errors="replace"))
            return outputs

    # -- internals -------------------------------------------------------------

    def _send_line(self, line: str, mask: bool = False) -> None:
        logger.debug("CLI >> %s", "********" if mask else repr(line))
        try:
            self._transport.send(line.encode("utf-8", errors="replace") + b"\r\n")
        except OSError as exc:
            raise CiscoCliConnectError(f"send failed: {exc}") from exc

    def _read_until(self, patterns, timeout: float, context: str = ""):
        """Read until one of `patterns` matches the end of the buffer.
        Handles telnet negotiation and the --More-- pager along the way."""
        deadline = time.monotonic() + timeout
        while True:
            for pattern in patterns:
                m = pattern.search(self._buf)
                if m:
                    return m
            if MORE_RE.search(self._buf):
                logger.debug("CLI: pager detected; sending space")
                self._transport.send(b" ")
                self._buf = _PAGER_JUNK_RE.sub(b"", self._buf)
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CiscoCliTimeout(
                    f"Timed out waiting for prompt ({context or 'read'}); "
                    f"tail={self._buf[-120:]!r}"
                )
            try:
                chunk = self._transport.recv(4096, remaining)
            except TransportTimeout:
                raise CiscoCliTimeout(
                    f"Timed out waiting for prompt ({context or 'read'}); "
                    f"tail={self._buf[-120:]!r}"
                ) from None
            except OSError as exc:
                raise CiscoCliConnectError(f"recv failed: {exc}") from exc
            if not chunk:
                raise CiscoCliConnectError("Connection closed by switch")
            logger.debug("CLI << %r", chunk)
            clean, replies = self._filter.feed(chunk)
            if replies:
                self._transport.send(replies)
            self._buf += clean
