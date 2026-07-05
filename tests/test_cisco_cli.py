import logging
import threading

import pytest

from jap.cisco_cli import (
    DO,
    IAC,
    WONT,
    CiscoCliAuthError,
    CiscoCliConnectError,
    CiscoCliTimeout,
    CiscoCliClient,
)
from tests.helpers import (
    LOGIN_PRELOAD,
    LOGIN_STEPS,
    NO_AUTH_PRELOAD,
    NO_AUTH_STEPS,
    FakeTransport,
    Step,
    command_step,
    make_factory,
)

ECHO = 1


def make_client(*transports, username="cisco", password="secret", **kwargs):
    return CiscoCliClient(
        "10.66.4.3",
        username=username,
        password=password,
        transport_factory=make_factory(*transports),
        **kwargs,
    )


class TestLogin:
    def test_full_login_handshake(self):
        transport = FakeTransport(
            preload=LOGIN_PRELOAD,
            steps=LOGIN_STEPS + [command_step("show clock", "12:00:00\r\n")],
        )
        client = make_client(transport)
        out = client.run_command("show clock")
        assert "12:00:00" in out
        assert b"cisco\r\n" in transport.sent
        assert b"secret\r\n" in transport.sent

    def test_auth_disabled_prompt_straight_away(self):
        transport = FakeTransport(
            preload=NO_AUTH_PRELOAD,
            steps=NO_AUTH_STEPS + [command_step("show clock", "ok\r\n")],
        )
        client = make_client(transport, username=None, password=None)
        assert "ok" in client.run_command("show clock")

    def test_bad_password_raises_auth_error(self):
        transport = FakeTransport(
            preload=LOGIN_PRELOAD,
            steps=[
                Step(b"cisco\r\n", [b"\r\nPassword:"]),
                Step(b"wrong\r\n", [b"\r\nUser Name:"]),
            ],
        )
        client = make_client(transport, password="wrong")
        with pytest.raises(CiscoCliAuthError):
            client.connect()
        assert transport.closed

    def test_login_prompt_but_no_username_configured(self):
        transport = FakeTransport(preload=LOGIN_PRELOAD)
        client = make_client(transport, username=None)
        with pytest.raises(CiscoCliAuthError):
            client.connect()

    def test_password_only_login(self):
        # Some configs skip the username stage entirely.
        transport = FakeTransport(
            preload=[b"\r\nPassword:"],
            steps=[
                Step(b"secret\r\n", [b"\r\nswitch#"]),
                Step(b"terminal datadump\r\n", [b"terminal datadump\r\nswitch#"]),
            ],
        )
        client = make_client(transport)
        client.connect()
        assert client.is_connected()

    def test_connect_refused(self):
        transport = FakeTransport(connect_error=ConnectionRefusedError("refused"))
        client = make_client(transport)
        with pytest.raises(CiscoCliConnectError):
            client.connect()


class TestPrompts:
    @pytest.mark.parametrize(
        "prompt",
        [
            "switch#",
            "switch572434#",
            "switch(config)#",
            "switch(config-if)#",
            "switch>",
        ],
    )
    def test_prompt_variants(self, prompt):
        transport = FakeTransport(
            preload=NO_AUTH_PRELOAD,
            steps=NO_AUTH_STEPS + [command_step("enable", "", prompt=prompt)],
        )
        client = make_client(transport, username=None, password=None)
        assert prompt in client.run_command("enable")


class TestPager:
    def test_more_prompt_handled(self):
        transport = FakeTransport(
            preload=NO_AUTH_PRELOAD,
            steps=NO_AUTH_STEPS
            + [
                Step(
                    b"show running-config\r\n",
                    [b"show running-config\r\npage one\r\n--More--"],
                ),
                Step(b" ", [b"\r      \rpage two\r\nswitch#"]),
            ],
        )
        client = make_client(transport, username=None, password=None)
        out = client.run_command("show running-config")
        assert "page one" in out
        assert "page two" in out
        assert "--More--" not in out


class TestNegotiation:
    def test_iac_refused_during_banner(self):
        transport = FakeTransport(
            preload=[bytes([IAC, DO, ECHO]) + b"\r\nswitch#"],
            steps=NO_AUTH_STEPS,
        )
        client = make_client(transport, username=None, password=None)
        client.connect()
        assert bytes([IAC, WONT, ECHO]) in transport.sent


class TestRetry:
    def test_timeout_reconnects_and_retries_once(self):
        # First session dies on the command (no scripted data); second succeeds.
        first = FakeTransport(preload=NO_AUTH_PRELOAD, steps=list(NO_AUTH_STEPS))
        second = FakeTransport(
            preload=NO_AUTH_PRELOAD,
            steps=NO_AUTH_STEPS + [command_step("show vlan", "VLAN DATA\r\n")],
        )
        client = make_client(first, second, username=None, password=None)
        out = client.run_command("show vlan")
        assert "VLAN DATA" in out
        assert first.closed
        assert b"show vlan\r\n" in first.sent  # attempted on the first session too

    def test_timeout_twice_raises(self):
        first = FakeTransport(preload=NO_AUTH_PRELOAD, steps=list(NO_AUTH_STEPS))
        second = FakeTransport(preload=NO_AUTH_PRELOAD, steps=list(NO_AUTH_STEPS))
        client = make_client(first, second, username=None, password=None)
        with pytest.raises(CiscoCliTimeout):
            client.run_command("show vlan")
        assert first.closed and second.connected

    def test_eof_reconnects(self):
        first = FakeTransport(
            preload=NO_AUTH_PRELOAD,
            steps=NO_AUTH_STEPS + [Step(b"show vlan\r\n", [b""])],  # EOF mid-command
        )
        second = FakeTransport(
            preload=NO_AUTH_PRELOAD,
            steps=NO_AUTH_STEPS + [command_step("show vlan", "OK\r\n")],
        )
        client = make_client(first, second, username=None, password=None)
        assert "OK" in client.run_command("show vlan")

    def test_run_commands_retries_whole_sequence(self):
        # Failure on the second command of a sequence -> the full sequence
        # replays on the fresh connection (safe: config sequences are idempotent).
        first = FakeTransport(
            preload=NO_AUTH_PRELOAD,
            steps=NO_AUTH_STEPS + [command_step("enable", "")],  # "configure" times out
        )
        second = FakeTransport(
            preload=NO_AUTH_PRELOAD,
            steps=NO_AUTH_STEPS
            + [
                command_step("enable", ""),
                command_step("configure", "", prompt="switch(config)#"),
            ],
        )
        client = make_client(first, second, username=None, password=None)
        outputs = client.run_commands(["enable", "configure"])
        assert len(outputs) == 2
        assert second.sent.count(b"enable\r\n") == 1
        assert first.sent.count(b"enable\r\n") == 1


class TestLockSerialization:
    def test_concurrent_commands_do_not_interleave(self):
        steps = NO_AUTH_STEPS + [
            command_step("cmd-a", "AAA\r\n"),
            command_step("cmd-b", "BBB\r\n"),
        ]
        transport = FakeTransport(preload=NO_AUTH_PRELOAD, steps=steps)
        client = make_client(transport, username=None, password=None)
        client.connect()

        results = {}
        barrier = threading.Barrier(2)

        def run(cmd):
            barrier.wait()
            try:
                results[cmd] = client.run_command(cmd)
            except Exception as exc:  # pragma: no cover
                results[cmd] = exc

        t1 = threading.Thread(target=run, args=("cmd-a",))
        t2 = threading.Thread(target=run, args=("cmd-b",))
        t1.start(), t2.start()
        t1.join(2), t2.join(2)

        # Whichever thread went first, both commands completed with their own
        # output and the wire saw them strictly one after the other.
        sent_lines = [s for s in transport.sent if s in (b"cmd-a\r\n", b"cmd-b\r\n")]
        assert len(sent_lines) == 2
        assert "AAA" in results["cmd-a"]
        assert "BBB" in results["cmd-b"]


class TestLogging:
    def test_password_masked_in_debug_log(self, caplog):
        transport = FakeTransport(
            preload=LOGIN_PRELOAD,
            steps=LOGIN_STEPS,
        )
        client = make_client(transport, password="secret")
        with caplog.at_level(logging.DEBUG, logger="Plugin"):
            client.connect()
        assert "secret" not in caplog.text
        assert "********" in caplog.text

    def test_io_logged_at_debug(self, caplog):
        transport = FakeTransport(
            preload=NO_AUTH_PRELOAD,
            steps=NO_AUTH_STEPS + [command_step("show clock", "tick\r\n")],
        )
        client = make_client(transport, username=None, password=None)
        with caplog.at_level(logging.DEBUG, logger="Plugin"):
            client.run_command("show clock")
        assert "CLI >> 'show clock'" in caplog.text
        assert "CLI <<" in caplog.text
