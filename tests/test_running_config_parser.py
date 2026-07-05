import os

import pytest

from jap.running_config import (
    expand_port_spec,
    expand_vlan_spec,
    format_vlan_spec,
    parse_mac_address_table,
    parse_running_config,
    parse_vlan_table,
    routing_from_vlan_table,
    strip_pager_artifacts,
)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def load(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return f.read()


class TestVlanSpecs:
    @pytest.mark.parametrize(
        "spec,expected",
        [
            ("11", {11}),
            ("10-11", {10, 11}),
            ("10,11", {10, 11}),
            ("10,12-14,20", {10, 12, 13, 14, 20}),
            ("", set()),
            ("banana,11", {11}),
            ("14-12", set()),  # inverted range
        ],
    )
    def test_expand(self, spec, expected):
        assert expand_vlan_spec(spec) == expected

    def test_format_round_trip(self):
        assert format_vlan_spec({10, 12, 13, 14, 20}) == "10,12-14,20"
        assert format_vlan_spec({11}) == "11"
        assert expand_vlan_spec(format_vlan_spec({1, 2, 3, 7})) == {1, 2, 3, 7}


class TestPortSpecs:
    def test_expand_lists_and_ranges(self):
        assert expand_port_spec("gi2,gi12,gi15-21") == [
            "gi2",
            "gi12",
            "gi15",
            "gi16",
            "gi17",
            "gi18",
            "gi19",
            "gi20",
            "gi21",
        ]

    def test_port_channel_and_case(self):
        assert expand_port_spec("Po2-4") == ["po2", "po3", "po4"]
        assert expand_port_spec("GigabitEthernet3") == ["gi3"]

    def test_stacked_names(self):
        assert expand_port_spec("gi1/0/1-3") == ["gi1/0/1", "gi1/0/2", "gi1/0/3"]

    def test_garbage_skipped(self):
        assert expand_port_spec("banana,gi2") == ["gi2"]
        assert expand_port_spec("") == []


class TestParseRunningConfigWhitepaper:
    def test_interfaces_parsed(self):
        rc = parse_running_config(load("running_config_whitepaper.txt"))
        assert rc.hostname == "jap-switch"
        assert rc.jumbo_frames is True
        assert rc.vlans >= {10, 11, 12, 13, 14}

        gi1 = rc.interfaces["gi1"]
        assert gi1.mode == "general"
        assert gi1.pvid == 11
        assert gi1.untagged_vlans == {10, 11}
        assert gi1.portfast is True

        gi6 = rc.interfaces["gi6"]
        assert gi6.pvid == 10
        assert gi6.untagged_vlans == {10, 12, 13, 14}

        gi9 = rc.interfaces["gi9"]
        assert gi9.mode == "trunk"

    def test_vlan_interfaces(self):
        rc = parse_running_config(load("running_config_whitepaper.txt"))
        vi = rc.vlan_interfaces[11]
        assert vi.name == "TRANSMITTER_1"
        assert vi.ip == "172.16.0.1"
        assert vi.prefix_len == 30
        assert str(vi.network) == "172.16.0.0/30"
        assert rc.vlan_interfaces[10].prefix_len == 17

    def test_multi_add_lines_accumulate(self):
        rc = parse_running_config(load("running_config_multi_add.txt"))
        assert rc.interfaces["gi1"].untagged_vlans == {10, 11}
        assert rc.interfaces["gi2"].untagged_vlans == {10, 12}

    def test_pager_artifacts_parse_identically(self):
        clean = load("running_config_whitepaper.txt")
        # Inject the pager marker + erase artifacts mid-file, as the switch
        # would emit them if 'terminal datadump' failed.
        lines = clean.split("\n")
        paged = (
            "\r\n".join(lines[:30])
            + "\r\n--More--\x08\x08\x08\x08        \r"
            + "\r\n".join(lines[30:])
        )
        rc_clean = parse_running_config(clean)
        rc_paged = parse_running_config(paged)
        assert set(rc_paged.interfaces) == set(rc_clean.interfaces)
        for name in rc_clean.interfaces:
            assert rc_paged.interfaces[name].pvid == rc_clean.interfaces[name].pvid
            assert (
                rc_paged.interfaces[name].untagged_vlans
                == rc_clean.interfaces[name].untagged_vlans
            )

    def test_garbage_returns_empty_config(self):
        rc = parse_running_config(load("running_config_garbage.txt"))
        assert rc.interfaces == {}
        assert rc.vlan_interfaces == {}
        assert rc.hostname is None


class TestParseRunningConfigReal:
    """Against the sanitized capture from the production SG350-av."""

    def test_real_fixture(self):
        rc = parse_running_config(load("running_config_sg350_real.txt"))
        assert rc.hostname == "switch572434"
        assert rc.jumbo_frames is True
        assert rc.vlans >= {2, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20}

        # TX port: gi2 pvid 11, member of 10-11 untagged
        assert rc.interfaces["gi2"].pvid == 11
        assert rc.interfaces["gi2"].untagged_vlans == {10, 11}
        # RX port: gi13 pvid 10, watching VLAN 12
        assert rc.interfaces["gi13"].pvid == 10
        assert rc.interfaces["gi13"].untagged_vlans == {10, 12}
        # Uplink LAG members
        assert rc.interfaces["gi27"].channel_group == 1
        assert rc.interfaces["gi27"].pvid == 4095
        # No portfast anywhere in this config (a real Validate System finding)
        assert not any(i.portfast for i in rc.interfaces.values())
        # 28 physical ports + Port-channel1
        assert len(rc.interfaces) == 29
        assert rc.vlan_interfaces[10].network is not None

    def test_real_show_ip_interface_data_via_vlan_interfaces(self):
        rc = parse_running_config(load("running_config_sg350_real.txt"))
        assert str(rc.vlan_interfaces[11].network) == "172.16.0.0/30"
        assert str(rc.vlan_interfaces[10].network) == "172.16.128.0/17"


class TestMacAddressTable:
    def test_real_fixture(self):
        entries = parse_mac_address_table(load("mac_address_table_sg350_real.txt"))
        dynamic = [e for e in entries if e.entry_type == "dynamic"]
        assert len(dynamic) == 7
        by_port = {e.port: e for e in dynamic}
        assert by_port["gi2"].mac == "c2:00:00:6c:32:db"
        assert by_port["gi2"].vlan == 11
        assert by_port["gi12"].mac == "c2:00:00:01:d6:12"
        assert by_port["gi12"].vlan == 10
        # The switch's own CPU entry (port "0") is excluded
        assert all(e.port != "0" for e in entries)

    def test_dotted_mac_format(self):
        text = "    Vlan   Mac Address     Port    Type\n    10   c200.0001.d612   gi5   dynamic\n"
        entries = parse_mac_address_table(text)
        assert entries == [
            type(entries[0])(
                vlan=10, mac="c2:00:00:01:d6:12", port="gi5", entry_type="dynamic"
            )
        ]

    def test_garbage(self):
        assert parse_mac_address_table("<html>nope</html>") == []


class TestVlanTable:
    def test_real_fixture(self):
        rows = parse_vlan_table(load("show_vlan_sg350_real.txt"))
        assert rows[10].name == "JAP_10x10"
        assert rows[10].untagged_ports == [f"gi{n}" for n in range(2, 22)]
        assert rows[11].untagged_ports == [
            "gi2",
            "gi12",
            "gi15",
            "gi16",
            "gi17",
            "gi18",
            "gi19",
            "gi20",
            "gi21",
        ]
        assert rows[12].untagged_ports == ["gi3", "gi13", "gi14"]
        assert rows[13].untagged_ports == ["gi4"]
        assert rows[1].untagged_ports == [
            "po2",
            "po3",
            "po4",
            "po5",
            "po6",
            "po7",
            "po8",
        ]

    def test_garbage(self):
        assert parse_vlan_table("no table here") == {}

    def test_routing_from_vlan_table(self):
        rows = parse_vlan_table(load("show_vlan_sg350_real.txt"))
        rx_ports = [f"gi{n}" for n in range(12, 22)]
        routing = routing_from_vlan_table(rows, rx_ports, (11, 410))
        assert routing["gi12"] == 11
        assert routing["gi13"] == 12
        assert routing["gi14"] == 12
        assert routing["gi15"] == 11
        assert routing["gi21"] == 11
        # gi22 isn't an RX port; unlisted RX ports would be None
        assert "gi22" not in routing

    def test_routing_rx_with_no_source(self):
        rows = parse_vlan_table(load("show_vlan_sg350_real.txt"))
        routing = routing_from_vlan_table(rows, ["gi12", "gi99"], (11, 410))
        assert routing["gi99"] is None


class TestStripPager:
    def test_strip(self):
        text = "line one\r\n--More--\x08\x08   \rline two\r\n"
        assert strip_pager_artifacts(text) == "line one\nline two\n"
