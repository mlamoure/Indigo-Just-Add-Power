from jap.cisco_cli import DO, DONT, IAC, SB, SE, WILL, WONT, TelnetFilter

ECHO = 1
SGA = 3


class TestTelnetFilter:
    def test_plain_data_passthrough(self):
        f = TelnetFilter()
        clean, replies = f.feed(b"hello world")
        assert clean == b"hello world"
        assert replies == b""

    def test_do_refused_with_wont(self):
        f = TelnetFilter()
        clean, replies = f.feed(bytes([IAC, DO, ECHO]) + b"data")
        assert clean == b"data"
        assert replies == bytes([IAC, WONT, ECHO])

    def test_will_refused_with_dont(self):
        f = TelnetFilter()
        clean, replies = f.feed(bytes([IAC, WILL, SGA]))
        assert clean == b""
        assert replies == bytes([IAC, DONT, SGA])

    def test_dont_and_wont_ignored(self):
        f = TelnetFilter()
        clean, replies = f.feed(bytes([IAC, DONT, ECHO, IAC, WONT, SGA]) + b"x")
        assert clean == b"x"
        assert replies == b""

    def test_iac_iac_literal(self):
        f = TelnetFilter()
        clean, replies = f.feed(b"a" + bytes([IAC, IAC]) + b"b")
        assert clean == b"a\xffb"
        assert replies == b""

    def test_subnegotiation_swallowed(self):
        f = TelnetFilter()
        data = b"pre" + bytes([IAC, SB, 24, 1, 2, 3, IAC, SE]) + b"post"
        clean, replies = f.feed(data)
        assert clean == b"prepost"
        assert replies == b""

    def test_subnegotiation_with_escaped_iac(self):
        f = TelnetFilter()
        data = bytes([IAC, SB, 24, IAC, IAC, 7, IAC, SE]) + b"tail"
        clean, _ = f.feed(data)
        assert clean == b"tail"

    def test_sequence_split_across_feeds(self):
        f = TelnetFilter()
        clean1, replies1 = f.feed(b"ab" + bytes([IAC]))
        assert clean1 == b"ab" and replies1 == b""
        clean2, replies2 = f.feed(bytes([DO]))
        assert clean2 == b"" and replies2 == b""
        clean3, replies3 = f.feed(bytes([ECHO]) + b"cd")
        assert clean3 == b"cd"
        assert replies3 == bytes([IAC, WONT, ECHO])

    def test_subnegotiation_split_across_feeds(self):
        f = TelnetFilter()
        clean1, _ = f.feed(bytes([IAC, SB, 24, 1]))
        clean2, _ = f.feed(bytes([2, IAC]))
        clean3, _ = f.feed(bytes([SE]) + b"done")
        assert clean1 == clean2 == b""
        assert clean3 == b"done"

    def test_other_two_byte_commands_stripped(self):
        f = TelnetFilter()
        clean, replies = f.feed(b"x" + bytes([IAC, 241]) + b"y")  # NOP
        assert clean == b"xy"
        assert replies == b""
