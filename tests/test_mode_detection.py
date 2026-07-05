import os

from jap.running_config import detect_mode, parse_running_config

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def load(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return f.read()


class TestDetectMode:
    def test_jadconfig_whitepaper(self):
        rc = parse_running_config(load("running_config_whitepaper.txt"))
        mode, reason = detect_mode(rc)
        assert mode == "jadconfig"
        assert "TX-range pvids" in reason

    def test_jadconfig_real(self):
        rc = parse_running_config(load("running_config_sg350_real.txt"))
        mode, _ = detect_mode(rc)
        assert mode == "jadconfig"

    def test_amp(self):
        rc = parse_running_config(load("running_config_amp.txt"))
        mode, reason = detect_mode(rc)
        assert mode == "amp"
        assert "172.27" in reason

    def test_garbage_defaults_to_jadconfig_with_warning_reason(self):
        rc = parse_running_config(load("running_config_garbage.txt"))
        mode, reason = detect_mode(rc)
        assert mode == "jadconfig"
        assert "ambiguous" in reason.lower()
