import os

import pytest

from jap.backends.base import BackendError
from jap.backends.jadconfig_cisco import RUNNING_CONFIG_TIMEOUT, JadConfigCiscoBackend
from jap.config import PluginSettings
from jap.topology import JapDevice, SwitchPort, Topology
from tests.helpers import FakeCli

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def load(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return f.read()


def make_topology():
    devices = [
        JapDevice(
            role="tx",
            mac=f"c2:00:00:00:00:0{n}",
            port=SwitchPort(f"gi{n+1}"),
            vlan=n + 10,
        )
        for n in range(1, 4)  # gi2/vlan11, gi3/vlan12, gi4/vlan13
    ]
    devices += [
        JapDevice(role="rx", mac="c2:00:00:00:00:11", port=SwitchPort("gi12")),
        JapDevice(role="rx", mac="c2:00:00:00:00:12", port=SwitchPort("gi13")),
        JapDevice(
            role="rx", mac="c2:00:00:00:00:13", port=SwitchPort("gi14"), ignored=True
        ),
    ]
    return Topology(switch_ip="10.66.4.3", devices=devices)


def make_backend(cli, topology=None, settings=None):
    settings = settings or PluginSettings(switch_ip="10.66.4.3")
    return JadConfigCiscoBackend(cli, settings, lambda: topology)


class TestSwitch:
    def test_exact_command_sequence(self):
        cli = FakeCli()
        topo = make_topology()
        backend = make_backend(cli, topo)
        rx = topo.find_by_port("gi12")
        tx = topo.find_by_port("gi2")
        backend.switch(rx, tx)
        assert cli.commands == [
            "enable",
            "configure",
            "interface gi12",
            "switchport general allowed vlan remove 11-410",
            "switchport general allowed vlan add 11 untagged",
            "end",
        ]

    def test_custom_remove_range(self):
        cli = FakeCli()
        topo = make_topology()
        settings = PluginSettings(switch_ip="10.66.4.3", tx_vlan_range=(11, 20))
        backend = JadConfigCiscoBackend(cli, settings, lambda: topo)
        backend.switch(topo.find_by_port("gi13"), topo.find_by_port("gi3"))
        assert "switchport general allowed vlan remove 11-20" in cli.commands
        assert "switchport general allowed vlan add 12 untagged" in cli.commands

    def test_error_marker_raises(self):
        cli = FakeCli(
            responses={
                "switchport general allowed vlan add 11 untagged": (
                    "switchport general allowed vlan add 11 untagged\r\n"
                    "% Bad VLAN list\r\nswitch(config-if)#"
                )
            }
        )
        topo = make_topology()
        backend = make_backend(cli, topo)
        with pytest.raises(BackendError, match="Bad VLAN list"):
            backend.switch(topo.find_by_port("gi12"), topo.find_by_port("gi2"))

    def test_missing_port_or_vlan(self):
        cli = FakeCli()
        topo = make_topology()
        backend = make_backend(cli, topo)
        portless_rx = JapDevice(role="rx", mac="c2:00:00:00:00:99")
        with pytest.raises(BackendError, match="no switch port"):
            backend.switch(portless_rx, topo.find_by_port("gi2"))
        vlanless_tx = JapDevice(
            role="tx", mac="c2:00:00:00:00:98", port=SwitchPort("gi9")
        )
        with pytest.raises(BackendError, match="no VLAN"):
            backend.switch(topo.find_by_port("gi12"), vlanless_tx)


class TestSwitchAll:
    def test_single_session_per_port_blocks(self):
        cli = FakeCli()
        topo = make_topology()
        backend = make_backend(cli, topo)
        backend.switch_all(topo.find_by_port("gi3"))  # tx vlan 12
        # One enable/configure, blocks for gi12 and gi13 (gi14 is ignored), one end.
        assert cli.commands == [
            "enable",
            "configure",
            "interface gi12",
            "switchport general allowed vlan remove 11-410",
            "switchport general allowed vlan add 12 untagged",
            "interface gi13",
            "switchport general allowed vlan remove 11-410",
            "switchport general allowed vlan add 12 untagged",
            "end",
        ]

    def test_no_topology_raises(self):
        backend = make_backend(FakeCli(), topology=None)
        tx = JapDevice(role="tx", mac="c2:00:00:00:00:01", vlan=11)
        with pytest.raises(BackendError, match="discovery"):
            backend.switch_all(tx)


class TestRoutingState:
    def test_fast_path_uses_show_vlan(self):
        cli = FakeCli(responses={"show vlan": load("show_vlan_sg350_real.txt")})
        topo = make_topology()
        backend = make_backend(cli, topo)
        state = backend.get_routing_state()
        assert cli.commands == ["show vlan"]
        assert state.rx_source == {"gi12": 11, "gi13": 12, "gi14": 12}

    def test_no_topology_falls_back_to_running_config(self):
        cli = FakeCli(
            responses={"show running-config": load("running_config_sg350_real.txt")}
        )
        backend = make_backend(cli, topology=None)
        state = backend.get_routing_state()
        assert cli.commands == ["show running-config"]
        assert cli.command_timeouts == [RUNNING_CONFIG_TIMEOUT]
        assert state.rx_source["gi12"] == 11
        assert state.rx_source["gi21"] == 11
        assert len(state.rx_source) == 10

    def test_unparseable_show_vlan_raises(self):
        cli = FakeCli(responses={"show vlan": "<html>boom</html>"})
        backend = make_backend(cli, make_topology())
        with pytest.raises(BackendError, match="show vlan"):
            backend.get_routing_state()


class TestRebootSwitch:
    def test_reload_confirm_flow(self):
        class DialogCli(FakeCli):
            def __init__(self):
                super().__init__()
                self.dialogs = []
                self.closed = False

            def run_dialog(self, exchanges, *, timeout=None):
                outputs = []
                for line, patterns in exchanges:
                    self.dialogs.append(line)
                    if line == "reload":
                        outputs.append("This will reset the system (Y/N)[N] ")
                    elif line == "Y" and self.dialogs.count("Y") == 1:
                        outputs.append("Are you sure? (Y/N)[N] ")
                    else:
                        outputs.append("Shutting down...")
                return outputs

            def close(self):
                self.closed = True

        cli = DialogCli()
        backend = make_backend(cli, make_topology())
        backend.reboot_switch()
        assert cli.dialogs == ["reload", "Y", "Y"]
        assert cli.closed


class TestValidate:
    def test_real_config_findings(self):
        cli = FakeCli(
            responses={"show running-config": load("running_config_sg350_real.txt")}
        )
        backend = make_backend(cli, make_topology())
        issues = backend.validate()
        by_severity = {}
        for issue in issues:
            by_severity.setdefault(issue.severity, []).append(issue.message)
        assert any("Telnet CLI reachable" in m for m in by_severity["ok"])
        assert any("10 TX and 10 RX" in m for m in by_severity["ok"])
        # The real switch has NO portfast -> genuine warning
        assert any("portfast" in m for m in by_severity["warning"])
        # Jumbo frames are enabled on the real switch
        assert any("Jumbo frames enabled" in m for m in by_severity["ok"])
        assert "error" not in by_severity

    def test_non_jap_config_errors(self):
        cli = FakeCli(
            responses={"show running-config": load("running_config_garbage.txt")}
        )
        backend = make_backend(cli, None)
        issues = backend.validate()
        assert any(i.severity == "error" and "No TX ports" in i.message for i in issues)
