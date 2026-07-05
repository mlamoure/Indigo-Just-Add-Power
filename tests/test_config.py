import ipaddress
import logging

from jap.config import PluginSettings, parse_subnets, parse_vlan_range, validate_prefs


class TestPluginSettings:
    def test_defaults_from_empty_prefs(self):
        settings = PluginSettings.from_prefs({})
        assert settings.switch_ip == ""
        assert settings.username == "cisco"
        assert settings.all_devices_vlan == 10
        assert settings.tx_vlan_range == (11, 410)
        assert settings.routing_poll_secs == 30
        assert settings.device_poll_secs == 60
        assert settings.device_subnets == []
        assert settings.log_level == logging.INFO

    def test_from_prefs_full(self):
        settings = PluginSettings.from_prefs(
            {
                "switch_ip": " 10.66.4.3 ",
                "switch_username": "admin",
                "switch_password": "secret",
                "all_devices_vlan": "10",
                "tx_vlan_range": "11-20",
                "routing_poll_secs": "15",
                "device_poll_secs": "120",
                "device_subnet": "172.16.0.0/24, 172.16.128.0/24",
                "snapshots_enabled": True,
                "log_level": "10",
            }
        )
        assert settings.switch_ip == "10.66.4.3"
        assert settings.tx_vlan_range == (11, 20)
        assert settings.remove_range == "11-20"
        assert settings.device_subnets == [
            ipaddress.ip_network("172.16.0.0/24"),
            ipaddress.ip_network("172.16.128.0/24"),
        ]
        assert settings.log_level == 10

    def test_bad_values_fall_back(self):
        settings = PluginSettings.from_prefs(
            {
                "all_devices_vlan": "banana",
                "tx_vlan_range": "wat",
                "routing_poll_secs": None,
                "device_subnet": "not-a-subnet",
            }
        )
        assert settings.all_devices_vlan == 10
        assert settings.tx_vlan_range == (11, 410)
        assert settings.routing_poll_secs == 30
        assert settings.device_subnets == []

    def test_none_prefs(self):
        assert PluginSettings.from_prefs(None).remove_range == "11-410"


class TestParseHelpers:
    def test_parse_vlan_range(self):
        assert parse_vlan_range("11-410") == (11, 410)
        assert parse_vlan_range(" 11 - 20 ") == (11, 20)
        assert parse_vlan_range("20-11") == (11, 410)  # inverted -> default
        assert parse_vlan_range("") == (11, 410)

    def test_parse_subnets_host_bits_ok(self):
        # strict=False lets users paste an interface address like 172.16.128.1/17
        nets = parse_subnets("172.16.128.1/17")
        assert nets == [ipaddress.ip_network("172.16.128.0/17")]

    def test_parse_subnets_drops_invalid(self):
        nets = parse_subnets("172.16.0.0/24, junk, 10.0.0.0/30")
        assert len(nets) == 2


class TestValidatePrefs:
    def test_valid(self):
        ok, _, errors = validate_prefs(
            {
                "switch_ip": "10.66.4.3",
                "all_devices_vlan": "10",
                "tx_vlan_range": "11-410",
                "device_subnet": "172.16.0.0/24",
            }
        )
        assert ok
        assert errors == {}

    def test_missing_ip(self):
        ok, _, errors = validate_prefs({"switch_ip": ""})
        assert not ok
        assert "switch_ip" in errors

    def test_bad_ip(self):
        ok, _, errors = validate_prefs({"switch_ip": "10.66.4"})
        assert not ok

    def test_bad_range_and_subnet(self):
        ok, _, errors = validate_prefs(
            {"switch_ip": "10.66.4.3", "tx_vlan_range": "abc", "device_subnet": "nope"}
        )
        assert not ok
        assert "tx_vlan_range" in errors
        assert "device_subnet" in errors
