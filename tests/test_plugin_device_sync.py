import logging

import pytest

import plugin as plugin_module
from jap.topology import (
    JapDevice,
    PendingSwitchTracker,
    RoutingState,
    SwitchPort,
    Topology,
)

TX1_MAC = "c2:00:00:00:00:01"
RX1_MAC = "c2:00:00:00:00:11"


def make_topology(**overrides):
    devices = overrides.pop(
        "devices",
        [
            JapDevice(
                role="tx",
                mac=TX1_MAC,
                ip="172.16.0.2",
                port=SwitchPort("gi2"),
                vlan=11,
                device_name="Apple TV",
            ),
            JapDevice(
                role="rx", mac=RX1_MAC, ip="172.16.128.2", port=SwitchPort("gi12")
            ),
        ],
    )
    return Topology(
        switch_ip="10.66.4.3", mode="jadconfig", model="SG300-28PP", devices=devices
    )


@pytest.fixture
def plugin(fake_indigo, tmp_path):
    p = plugin_module.Plugin(
        "com.vtmikel.justaddpower",
        "Just Add Power",
        "2026.1.0",
        {"switch_ip": "10.66.4.3"},
    )
    p._store = plugin_module.TopologyStore(str(tmp_path / "topology.json"))
    p._ensure_folder()
    return p


class TestAutoCreate:
    def test_creates_devices_in_folder(self, plugin, fake_indigo):
        plugin._topology = make_topology()
        plugin._sync_indigo_devices()

        created = fake_indigo.device.created
        assert len(created) == 3  # switch + tx + rx
        by_type = {d.deviceTypeId: d for d in created}
        assert set(by_type) == {"japSwitch", "japTransmitter", "japReceiver"}

        tx = by_type["japTransmitter"]
        assert tx.address == f"mac:{TX1_MAC}"
        assert tx.name == "JAP Tx 1 - Apple TV"
        assert tx.pluginProps["jap_port"] == "gi2"
        assert tx.pluginProps["jap_vlan"] == "11"
        assert tx.folderId == plugin._folder_id

        rx = by_type["japReceiver"]
        assert rx.name == "JAP Rx gi12"
        assert rx.states["switchPort"] == "gi12"
        assert rx.states["imagePullUrl"] == "http://172.16.128.2/pull.bmp"

        switch = by_type["japSwitch"]
        assert switch.states["mode"] == "jadconfig"
        assert switch.states["model"] == "SG300-28PP"

    def test_ignored_devices_not_created(self, plugin, fake_indigo):
        topo = make_topology()
        topo.devices[1].ignored = True
        plugin._topology = topo
        plugin._sync_indigo_devices()
        types = [d.deviceTypeId for d in fake_indigo.device.created]
        assert "japReceiver" not in types

    def test_name_collision_gets_suffix(self, plugin, fake_indigo):
        existing = fake_indigo.Device(1, name="JAP Tx 1 - Apple TV")
        fake_indigo.devices[1] = existing
        plugin._topology = make_topology()
        plugin._sync_indigo_devices()
        names = [d.name for d in fake_indigo.device.created]
        assert "JAP Tx 1 - Apple TV (2)" in names


class TestAdopt:
    def test_existing_device_adopted_not_duplicated(self, plugin, fake_indigo):
        plugin._topology = make_topology()
        plugin._sync_indigo_devices()
        first_count = len(fake_indigo.device.created)
        plugin._sync_indigo_devices()
        assert len(fake_indigo.device.created) == first_count  # no new devices

    def test_user_rename_is_safe(self, plugin, fake_indigo):
        plugin._topology = make_topology()
        plugin._sync_indigo_devices()
        tx = next(
            d for d in fake_indigo.device.created if d.deviceTypeId == "japTransmitter"
        )
        tx.name = "Living Room Apple TV Feed"
        plugin._sync_indigo_devices()
        assert tx.name == "Living Room Apple TV Feed"

    def test_props_not_rewritten_when_unchanged(self, plugin, fake_indigo):
        plugin._topology = make_topology()
        plugin._sync_indigo_devices()
        tx = next(
            d for d in fake_indigo.device.created if d.deviceTypeId == "japTransmitter"
        )
        props_before = tx.pluginProps
        plugin._sync_indigo_devices()
        assert tx.pluginProps is props_before  # replacePluginPropsOnServer not called

    def test_props_updated_on_change(self, plugin, fake_indigo):
        plugin._topology = make_topology()
        plugin._sync_indigo_devices()
        plugin._topology.devices[0].ip = "172.16.0.99"
        plugin._sync_indigo_devices()
        tx = next(
            d for d in fake_indigo.device.created if d.deviceTypeId == "japTransmitter"
        )
        assert tx.pluginProps["jap_ip"] == "172.16.0.99"

    def test_duplicate_addresses_adopts_lowest_id(self, plugin, fake_indigo, caplog):
        plugin._topology = make_topology()
        plugin._sync_indigo_devices()
        original = next(
            d for d in fake_indigo.device.created if d.deviceTypeId == "japTransmitter"
        )
        clone = fake_indigo.Device(
            original.id + 5000,
            name="Copy of TX",
            address=original.address,
            deviceTypeId="japTransmitter",
        )
        fake_indigo.devices[clone.id] = clone
        with caplog.at_level(logging.WARNING, logger="Plugin"):
            plugin._sync_indigo_devices()
        assert "Duplicate devices" in caplog.text
        assert clone.id in fake_indigo.devices  # never deleted


class TestKeyUpgrade:
    def test_port_key_upgraded_to_mac(self, plugin, fake_indigo):
        # First discovery: device powered off -> port key.
        topo = make_topology(
            devices=[JapDevice(role="rx", mac=None, port=SwitchPort("gi12"))]
        )
        plugin._topology = topo
        plugin._sync_indigo_devices()
        rx = next(
            d for d in fake_indigo.device.created if d.deviceTypeId == "japReceiver"
        )
        assert rx.address == "port:gi12"

        # Rediscovery: MAC now known.
        plugin._topology = make_topology(
            devices=[
                JapDevice(
                    role="rx", mac=RX1_MAC, ip="172.16.128.2", port=SwitchPort("gi12")
                )
            ]
        )
        created_before = len(fake_indigo.device.created)
        plugin._sync_indigo_devices()
        assert len(fake_indigo.device.created) == created_before  # no twin created
        assert rx.address == f"mac:{RX1_MAC}"


class FakeBackend:
    def __init__(self, states):
        self.states = list(states)  # each get_routing_state() pops the next

    def get_routing_state(self):
        result = self.states.pop(0)
        if isinstance(result, Exception):
            raise result
        return RoutingState(rx_source=dict(result))


class TestRoutingPoll:
    def _plugin_with_backend(self, plugin, fake_indigo, states):
        plugin._topology = make_topology()
        plugin._sync_indigo_devices()
        plugin._backend = FakeBackend(states)
        return plugin

    def test_states_applied_and_display_name_used(self, plugin, fake_indigo):
        p = self._plugin_with_backend(plugin, fake_indigo, [{"gi12": 11}])
        p._job_routing_poll()
        rx = next(
            d for d in fake_indigo.device.created if d.deviceTypeId == "japReceiver"
        )
        tx = next(
            d for d in fake_indigo.device.created if d.deviceTypeId == "japTransmitter"
        )
        assert rx.states["currentSource"] == tx.name  # Indigo device name, not VLAN
        assert rx.states["currentSourceVlan"] == 11
        assert tx.states["watchedByCount"] == 1
        switch = next(
            d for d in fake_indigo.device.created if d.deviceTypeId == "japSwitch"
        )
        assert switch.states["online"] is True

    def test_pending_port_shielded_until_confirm(self, plugin, fake_indigo):
        # Optimistic value survives one stale poll, then confirms.
        p = self._plugin_with_backend(plugin, fake_indigo, [{"gi12": 11}, {"gi12": 12}])
        p._pending.record("gi12", 12)
        p._routing = RoutingState(rx_source={"gi12": 12})  # optimistic view
        p._job_routing_poll()  # stale observation (11) — shielded
        rx = next(
            d for d in fake_indigo.device.created if d.deviceTypeId == "japReceiver"
        )
        assert rx.states["currentSourceVlan"] == 12
        p._job_routing_poll()  # confirmation
        assert rx.states["currentSourceVlan"] == 12
        assert p._pending.pending_ports == set()

    def test_revert_after_two_misses(self, plugin, fake_indigo, caplog):
        p = self._plugin_with_backend(plugin, fake_indigo, [{"gi12": 11}, {"gi12": 11}])
        p._pending.record("gi12", 12)
        p._routing = RoutingState(rx_source={"gi12": 12})
        with caplog.at_level(logging.ERROR, logger="Plugin"):
            p._job_routing_poll()
            p._job_routing_poll()
        assert "not confirmed" in caplog.text
        rx = next(
            d for d in fake_indigo.device.created if d.deviceTypeId == "japReceiver"
        )
        assert rx.states["currentSourceVlan"] == 11  # reverted to observed truth

    def test_two_failures_mark_unknown_and_offline(self, plugin, fake_indigo):
        from jap.backends.base import BackendError

        p = self._plugin_with_backend(
            plugin, fake_indigo, [BackendError("down"), BackendError("down")]
        )
        p._job_routing_poll()
        p._job_routing_poll()
        rx = next(
            d for d in fake_indigo.device.created if d.deviceTypeId == "japReceiver"
        )
        switch = next(
            d for d in fake_indigo.device.created if d.deviceTypeId == "japSwitch"
        )
        assert rx.states["currentSource"] == "unknown"
        assert switch.states["online"] is False


class TestTxSourceList:
    def test_menu_items(self, plugin, fake_indigo):
        plugin._topology = make_topology()
        plugin._sync_indigo_devices()
        items = plugin.tx_source_list()
        assert items == [("11", "JAP Tx 1 - Apple TV")]

    def test_ignored_tx_excluded(self, plugin, fake_indigo):
        topo = make_topology()
        topo.devices[0].ignored = True
        plugin._topology = topo
        assert plugin.tx_source_list() == []

    def test_no_topology(self, plugin):
        assert plugin.tx_source_list() == []
