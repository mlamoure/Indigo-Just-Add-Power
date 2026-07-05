import os

from jap.running_config import classify_ports, parse_running_config

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def load(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return f.read()


class TestClassifyWhitepaper:
    def test_tx_rx_split(self):
        rc = parse_running_config(load("running_config_whitepaper.txt"))
        result = classify_ports(rc, all_devices_vlan=10, tx_range=(11, 410))
        assert result.tx_ports == {"gi1": 11, "gi2": 12, "gi3": 13, "gi4": 14}
        assert result.rx_ports["gi5"] == 11
        assert result.rx_ports["gi7"] is None  # only VLAN 10 -> no source

    def test_trunk_uplink_excluded(self):
        rc = parse_running_config(load("running_config_whitepaper.txt"))
        result = classify_ports(rc, all_devices_vlan=10, tx_range=(11, 410))
        assert "gi9" not in result.tx_ports
        assert "gi9" not in result.rx_ports

    def test_multi_membership_warns_and_uses_lowest(self):
        rc = parse_running_config(load("running_config_whitepaper.txt"))
        result = classify_ports(rc, all_devices_vlan=10, tx_range=(11, 410))
        assert result.rx_ports["gi6"] == 12  # of {12, 13, 14}
        assert any("gi6" in w for w in result.warnings)

    def test_custom_tx_range(self):
        rc = parse_running_config(load("running_config_whitepaper.txt"))
        result = classify_ports(rc, all_devices_vlan=10, tx_range=(11, 12))
        assert result.tx_ports == {"gi1": 11, "gi2": 12}
        # gi3/gi4 (pvid 13/14) fall outside the range: not TX, not RX
        assert "gi3" not in result.tx_ports and "gi3" not in result.rx_ports
        # gi8 watches VLAN 13 which is now out of range -> no source
        assert result.rx_ports["gi8"] is None


class TestClassifyReal:
    def test_production_topology(self):
        rc = parse_running_config(load("running_config_sg350_real.txt"))
        result = classify_ports(rc, all_devices_vlan=10, tx_range=(11, 410))
        assert result.tx_ports == {f"gi{n}": n + 9 for n in range(2, 12)}
        assert result.rx_ports == {
            "gi12": 11,
            "gi13": 12,
            "gi14": 12,
            "gi15": 11,
            "gi16": 11,
            "gi17": 11,
            "gi18": 11,
            "gi19": 11,
            "gi20": 11,
            "gi21": 11,
        }
        assert result.warnings == []

    def test_control_and_lag_ports_excluded(self):
        rc = parse_running_config(load("running_config_sg350_real.txt"))
        result = classify_ports(rc, all_devices_vlan=10, tx_range=(11, 410))
        classified = set(result.tx_ports) | set(result.rx_ports)
        for excluded in ("gi1", "gi22", "gi26", "gi27", "gi28", "po1"):
            assert excluded not in classified
