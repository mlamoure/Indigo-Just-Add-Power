import pytest

from jap.topology import (
    JapDevice,
    PendingSwitchTracker,
    RoutingChange,
    RoutingState,
    SwitchPort,
    Topology,
    normalize_ifname,
    normalize_mac,
)


class TestNormalizeIfname:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("GigabitEthernet7", "gi7"),
            ("gigabitethernet1", "gi1"),
            ("gi7", "gi7"),
            ("gi1/0/7", "gi1/0/7"),
            ("GigabitEthernet1/0/7", "gi1/0/7"),
            ("TenGigabitEthernet2", "te2"),
            ("te1", "te1"),
            ("Port-Channel1", "po1"),
            ("Po1", "po1"),
            ("FastEthernet3", "fa3"),
        ],
    )
    def test_normalization(self, raw, expected):
        assert normalize_ifname(raw) == expected


class TestNormalizeMac:
    @pytest.mark.parametrize(
        "raw",
        ["c2:00:00:6c:32:db", "C2-00-00-6C-32-DB", "c200.006c.32db", "C200006C32DB"],
    )
    def test_formats(self, raw):
        assert normalize_mac(raw) == "c2:00:00:6c:32:db"

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            normalize_mac("not-a-mac")


class TestSwitchPort:
    def test_number(self):
        assert SwitchPort("gi7").number == 7
        assert SwitchPort("gi1/0/12").number == 12


class TestJapDeviceKey:
    def test_mac_preferred(self):
        dev = JapDevice(role="tx", mac="c2:00:00:6c:32:db", port=SwitchPort("gi2"))
        assert dev.key == "mac:c2:00:00:6c:32:db"

    def test_port_fallback(self):
        dev = JapDevice(role="rx", port=SwitchPort("gi12"))
        assert dev.key == "port:gi12"

    def test_no_identity_raises(self):
        with pytest.raises(ValueError):
            _ = JapDevice(role="rx").key


class TestTopology:
    def _topology(self):
        return Topology(
            switch_ip="10.66.4.3",
            devices=[
                JapDevice(
                    role="tx", mac="c2:00:00:00:00:01", port=SwitchPort("gi2"), vlan=11
                ),
                JapDevice(
                    role="tx", mac="c2:00:00:00:00:02", port=SwitchPort("gi3"), vlan=12
                ),
                JapDevice(role="rx", mac="c2:00:00:00:00:03", port=SwitchPort("gi12")),
            ],
        )

    def test_role_helpers(self):
        topo = self._topology()
        assert len(topo.tx_devices()) == 2
        assert len(topo.rx_devices()) == 1

    def test_tx_by_vlan(self):
        topo = self._topology()
        assert topo.tx_by_vlan(12).port.name == "gi3"
        assert topo.tx_by_vlan(99) is None

    def test_find_by_key_and_port(self):
        topo = self._topology()
        assert topo.find_by_key("mac:c2:00:00:00:00:03").role == "rx"
        assert topo.find_by_port("gi2").vlan == 11
        assert topo.find_by_key("mac:ff:ff:ff:ff:ff:ff") is None


class TestRoutingStateDiff:
    def test_diff(self):
        old = RoutingState(rx_source={"gi12": 11, "gi13": 12, "gi14": None})
        new = RoutingState(rx_source={"gi12": 13, "gi13": 12, "gi14": 11})
        changes = old.diff(new)
        assert changes == [
            RoutingChange("gi12", 11, 13),
            RoutingChange("gi14", None, 11),
        ]

    def test_diff_port_appears_and_disappears(self):
        old = RoutingState(rx_source={"gi12": 11})
        new = RoutingState(rx_source={"gi13": 12})
        changes = old.diff(new)
        assert RoutingChange("gi12", 11, None) in changes
        assert RoutingChange("gi13", None, 12) in changes


class TestPendingSwitchTracker:
    def test_confirm_on_first_poll(self):
        tracker = PendingSwitchTracker()
        tracker.record("gi12", 13)
        result = tracker.reconcile(RoutingState(rx_source={"gi12": 13}))
        assert result.confirmed == [("gi12", 13)]
        assert result.reverted == []
        assert result.pending == set()

    def test_confirm_on_second_poll(self):
        tracker = PendingSwitchTracker()
        tracker.record("gi12", 13)
        first = tracker.reconcile(RoutingState(rx_source={"gi12": 11}))
        assert first.confirmed == [] and first.reverted == []
        assert first.pending == {"gi12"}
        second = tracker.reconcile(RoutingState(rx_source={"gi12": 13}))
        assert second.confirmed == [("gi12", 13)]
        assert second.pending == set()

    def test_revert_after_two_misses(self):
        tracker = PendingSwitchTracker()
        tracker.record("gi12", 13)
        tracker.reconcile(RoutingState(rx_source={"gi12": 11}))
        result = tracker.reconcile(RoutingState(rx_source={"gi12": 11}))
        assert result.reverted == [("gi12", 13, 11)]
        assert result.pending == set()

    def test_missing_port_counts_as_miss(self):
        tracker = PendingSwitchTracker()
        tracker.record("gi12", 13)
        tracker.reconcile(RoutingState(rx_source={}))
        result = tracker.reconcile(RoutingState(rx_source={}))
        assert result.reverted == [("gi12", 13, None)]

    def test_re_record_resets_attempts(self):
        tracker = PendingSwitchTracker()
        tracker.record("gi12", 13)
        tracker.reconcile(RoutingState(rx_source={"gi12": 11}))
        tracker.record("gi12", 14)  # user switched again before confirm
        first = tracker.reconcile(RoutingState(rx_source={"gi12": 11}))
        assert first.reverted == []  # attempts were reset by the new record
        assert first.pending == {"gi12"}
