import ipaddress
import os

import pytest

from jap.config import PluginSettings, TopologyStore
from jap.discovery import (
    DiscoveryError,
    enumerate_candidate_ips,
    parse_system_model,
    run_discovery,
)
from jap.justapi import DeviceDetails
from jap.running_config import parse_running_config
from jap.topology import JapDevice, SwitchPort, Topology

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def load(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return f.read()


class RecordingCli:
    def __init__(self, responses):
        self.responses = responses
        self.commands = []

    def run_command(self, cmd, *, timeout=None):
        self.commands.append(cmd)
        return self.responses.get(cmd, "")


def real_cli():
    return RecordingCli(
        {
            "show running-config": load("running_config_sg350_real.txt"),
            "show mac address-table": load("mac_address_table_sg350_real.txt"),
            "show system": load("show_system_sg300_real.txt"),
        }
    )


def make_prober(details_by_ip):
    def prober(ip, timeout=1.5):
        return details_by_ip.get(ip)

    return prober


def details(mac, name=None, model="3G", firmware="B2.4"):
    return DeviceDetails(
        mac=mac, model=model, device_name=name, firmware=firmware, raw={}
    )


SETTINGS = PluginSettings(switch_ip="10.66.4.3")

# Real MACs from the production MAC table.
TX1_MAC = "c2:00:00:6c:32:db"  # gi2, vlan 11
TX2_MAC = "c2:00:00:ae:9f:b6"  # gi3, vlan 12
RX1_MAC = "c2:00:00:01:d6:12"  # gi12
RX2_MAC = "c2:00:00:02:cc:13"  # gi13


class TestRunDiscoveryReal:
    def test_full_flow_with_real_fixtures(self):
        prober = make_prober(
            {
                "172.16.0.2": details(TX1_MAC, "Mac Mini Tx"),
                "172.16.128.2": details(RX1_MAC, "Onkyo Rx"),
            }
        )
        topo, warnings = run_discovery(real_cli(), SETTINGS, None, prober=prober)

        assert topo.mode == "jadconfig"
        assert topo.model == "SG300-28PP 28-Port Gigabit PoE+ Managed Switch"
        assert len(topo.tx_devices()) == 10
        assert len(topo.rx_devices()) == 10

        tx1 = topo.find_by_port("gi2")
        assert tx1.mac == TX1_MAC
        assert tx1.ip == "172.16.0.2"
        assert tx1.device_name == "Mac Mini Tx"
        assert tx1.vlan == 11
        assert tx1.key == f"mac:{TX1_MAC}"

        rx1 = topo.find_by_port("gi12")
        assert rx1.mac == RX1_MAC
        assert rx1.ip == "172.16.128.2"
        assert rx1.role == "rx"

    def test_partial_discovery_no_http_responder(self):
        # gi3 has a MAC in the table but no HTTP responder.
        topo, _ = run_discovery(real_cli(), SETTINGS, None, prober=make_prober({}))
        tx2 = topo.find_by_port("gi3")
        assert tx2.mac == TX2_MAC
        assert tx2.ip is None
        assert tx2.device_name is None
        assert tx2.key == f"mac:{TX2_MAC}"

    def test_powered_off_port_gets_port_key(self):
        # gi5 (TX4) has no MAC table entry at all (device off).
        topo, _ = run_discovery(real_cli(), SETTINGS, None, prober=make_prober({}))
        tx4 = topo.find_by_port("gi5")
        assert tx4.mac is None
        assert tx4.key == "port:gi5"
        assert tx4.vlan == 14

    def test_role_comes_from_switch_not_device(self):
        # A responder on an RX port claiming to be whatever it wants is still RX.
        prober = make_prober({"172.16.128.2": details(RX1_MAC, "I claim to be a TX")})
        topo, _ = run_discovery(real_cli(), SETTINGS, None, prober=prober)
        assert topo.find_by_port("gi12").role == "rx"

    def test_unmatched_responder_warns(self):
        prober = make_prober({"172.16.128.9": details("aa:bb:cc:dd:ee:ff", "Stranger")})
        topo, warnings = run_discovery(real_cli(), SETTINGS, None, prober=prober)
        assert any("matches no classified" in w for w in warnings)
        assert topo.find_by_key("mac:aa:bb:cc:dd:ee:ff") is None

    def test_garbage_config_aborts(self):
        cli = RecordingCli({"show running-config": load("running_config_garbage.txt")})
        with pytest.raises(DiscoveryError):
            run_discovery(cli, SETTINGS, None, prober=make_prober({}))

    def test_merge_preserves_manual_after_rediscovery(self):
        prober = make_prober({"172.16.0.2": details(TX1_MAC, "Discovered Name")})
        fresh, _ = run_discovery(real_cli(), SETTINGS, None, prober=prober)
        existing = Topology(
            switch_ip="10.66.4.3",
            devices=[
                JapDevice(
                    role="tx",
                    mac=TX1_MAC,
                    port=SwitchPort("gi2"),
                    vlan=11,
                    device_name="My Name",
                    manual=True,
                )
            ],
        )
        merged = TopologyStore.merge(existing, fresh)
        assert merged.find_by_key(f"mac:{TX1_MAC}").device_name == "My Name"
        assert len(merged.devices) == 20


class TestCandidateIps:
    def test_derived_from_vlan_interfaces_with_cap(self):
        rc = parse_running_config(load("running_config_sg350_real.txt"))
        warnings = []
        ips = enumerate_candidate_ips(SETTINGS, None, rc, warnings)
        # 10 TX /30s contribute 1 usable host each (interface IP excluded)
        assert "172.16.0.2" in ips  # TX1 (vlan 11: 172.16.0.0/30, .1 is the switch)
        assert "172.16.0.38" in ips  # TX10
        # The /17 all-devices subnet is capped at 254 hosts with a warning
        assert "172.16.128.2" in ips
        assert any("larger than /24" in w for w in warnings)
        assert len(ips) <= 10 * 1 + 254
        # The switch's own interface IPs are excluded
        assert "172.16.0.1" not in ips
        assert "172.16.128.1" not in ips

    def test_configured_subnets_override(self):
        rc = parse_running_config(load("running_config_sg350_real.txt"))
        settings = PluginSettings(
            switch_ip="10.66.4.3",
            device_subnets=[ipaddress.ip_network("10.0.0.0/30")],
        )
        ips = enumerate_candidate_ips(settings, None, rc, [])
        assert ips == ["10.0.0.1", "10.0.0.2"]

    def test_existing_topology_ips_included_first(self):
        rc = parse_running_config(load("running_config_sg350_real.txt"))
        existing = Topology(
            devices=[
                JapDevice(role="tx", port=SwitchPort("gi2"), vlan=11, ip="192.168.9.9")
            ]
        )
        ips = enumerate_candidate_ips(SETTINGS, existing, rc, [])
        assert ips[0] == "192.168.9.9"


class TestSystemModel:
    def test_parse(self):
        assert (
            parse_system_model(load("show_system_sg300_real.txt"))
            == "SG300-28PP 28-Port Gigabit PoE+ Managed Switch"
        )

    def test_no_match(self):
        assert parse_system_model("garbage") is None
        assert parse_system_model("") is None


class TestMultipleMacsOnPort:
    def test_falls_back_to_port_identity(self):
        mac_table = load("mac_address_table_sg350_real.txt") + (
            "\n     11        aa:aa:aa:aa:aa:aa      gi2      dynamic   \n"
        )
        cli = RecordingCli(
            {
                "show running-config": load("running_config_sg350_real.txt"),
                "show mac address-table": mac_table,
                "show system": load("show_system_sg300_real.txt"),
            }
        )
        topo, warnings = run_discovery(cli, SETTINGS, None, prober=make_prober({}))
        assert any("gi2 has 2 MAC" in w for w in warnings)
        assert topo.find_by_port("gi2").mac is None
        assert topo.find_by_port("gi2").key == "port:gi2"
