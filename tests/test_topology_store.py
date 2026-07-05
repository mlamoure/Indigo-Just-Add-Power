import json

from jap.config import TopologyStore
from jap.topology import JapDevice, SwitchPort, Topology


def _tx(mac=None, port=None, vlan=None, **kwargs):
    return JapDevice(
        role="tx",
        mac=mac,
        port=SwitchPort(port) if port else None,
        vlan=vlan,
        **kwargs,
    )


def _rx(mac=None, port=None, **kwargs):
    return JapDevice(
        role="rx", mac=mac, port=SwitchPort(port) if port else None, **kwargs
    )


class TestSaveLoad:
    def test_round_trip(self, tmp_path):
        store = TopologyStore(str(tmp_path / "topology.json"))
        topo = Topology(
            switch_ip="10.66.4.3",
            mode="jadconfig",
            model="SG350",
            devices=[
                _tx(
                    mac="c2:00:00:00:00:01", port="gi2", vlan=11, device_name="Apple TV"
                ),
                _rx(port="gi12", ignored=True),
            ],
        )
        store.save(topo, now="2026-07-05T12:00:00")
        loaded = store.load()
        assert loaded.switch_ip == "10.66.4.3"
        assert loaded.model == "SG350"
        assert len(loaded.devices) == 2
        tx = loaded.find_by_key("mac:c2:00:00:00:00:01")
        assert tx.vlan == 11 and tx.device_name == "Apple TV" and tx.port.name == "gi2"
        rx = loaded.find_by_key("port:gi12")
        assert rx.ignored is True

    def test_missing_file(self, tmp_path):
        assert TopologyStore(str(tmp_path / "nope.json")).load() is None

    def test_corrupt_json(self, tmp_path):
        path = tmp_path / "topology.json"
        path.write_text("{ not json")
        assert TopologyStore(str(path)).load() is None

    def test_wrong_shape(self, tmp_path):
        path = tmp_path / "topology.json"
        path.write_text(json.dumps([1, 2, 3]))
        assert TopologyStore(str(path)).load() is None

    def test_malformed_entries_dropped(self, tmp_path):
        path = tmp_path / "topology.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "switch": {"ip": "10.66.4.3"},
                    "devices": [
                        {"role": "tx", "port": "gi2", "vlan": "11"},  # vlan coerced
                        {"role": "banana"},  # bad role
                        {"role": "rx"},  # no identity
                        "garbage",  # not a dict
                        {
                            "role": "rx",
                            "port": "gi12",
                            "mac": "zz:bad",
                        },  # bad mac dropped, port kept
                    ],
                }
            )
        )
        topo = TopologyStore(str(path)).load()
        assert len(topo.devices) == 2
        assert topo.find_by_port("gi2").vlan == 11
        assert topo.find_by_port("gi12").mac is None

    def test_atomic_write_no_tmp_left(self, tmp_path):
        store = TopologyStore(str(tmp_path / "topology.json"))
        store.save(Topology(switch_ip="1.2.3.4"))
        assert not (tmp_path / "topology.json.tmp").exists()


class TestMerge:
    def test_no_existing(self):
        fresh = Topology(devices=[_tx(mac="c2:00:00:00:00:01", port="gi2", vlan=11)])
        assert TopologyStore.merge(None, fresh) is fresh

    def test_manual_survives_rediscovery(self):
        existing = Topology(
            devices=[
                _tx(
                    mac="c2:00:00:00:00:01",
                    port="gi2",
                    vlan=11,
                    device_name="My Custom Name",
                    manual=True,
                    ip=None,
                )
            ]
        )
        fresh = Topology(
            devices=[
                _tx(
                    mac="c2:00:00:00:00:01",
                    port="gi2",
                    vlan=11,
                    device_name="Discovered Name",
                    ip="172.16.0.2",
                )
            ]
        )
        merged = TopologyStore.merge(existing, fresh, now="2026-07-05T12:00:00")
        dev = merged.find_by_key("mac:c2:00:00:00:00:01")
        assert dev.manual is True
        assert dev.device_name == "My Custom Name"  # manual field untouched
        assert dev.ip == "172.16.0.2"  # None gap filled from discovery

    def test_discovered_replaced_ignored_preserved(self):
        existing = Topology(
            devices=[_tx(mac="c2:00:00:00:00:01", port="gi2", vlan=11, ignored=True)]
        )
        fresh = Topology(
            devices=[
                _tx(mac="c2:00:00:00:00:01", port="gi3", vlan=12, device_name="Moved")
            ]
        )
        merged = TopologyStore.merge(existing, fresh)
        dev = merged.find_by_key("mac:c2:00:00:00:00:01")
        assert dev.port.name == "gi3" and dev.vlan == 12  # replaced wholesale
        assert dev.ignored is True  # user flag preserved

    def test_vanished_marked_missing_not_deleted(self):
        existing = Topology(
            devices=[
                _tx(mac="c2:00:00:00:00:01", port="gi2", vlan=11),
                _rx(mac="c2:00:00:00:00:02", port="gi12"),
            ]
        )
        fresh = Topology(devices=[_tx(mac="c2:00:00:00:00:01", port="gi2", vlan=11)])
        merged = TopologyStore.merge(existing, fresh, now="2026-07-05T12:00:00")
        assert len(merged.devices) == 2
        gone = merged.find_by_key("mac:c2:00:00:00:00:02")
        assert gone.missing_since == "2026-07-05T12:00:00"

    def test_missing_since_not_overwritten(self):
        existing = Topology(
            devices=[
                _rx(
                    mac="c2:00:00:00:00:02",
                    port="gi12",
                    missing_since="2026-01-01T00:00:00",
                )
            ]
        )
        merged = TopologyStore.merge(
            existing, Topology(devices=[]), now="2026-07-05T12:00:00"
        )
        assert merged.devices[0].missing_since == "2026-01-01T00:00:00"

    def test_reappeared_clears_missing_since(self):
        existing = Topology(
            devices=[
                _rx(
                    mac="c2:00:00:00:00:02",
                    port="gi12",
                    missing_since="2026-01-01T00:00:00",
                )
            ]
        )
        fresh = Topology(devices=[_rx(mac="c2:00:00:00:00:02", port="gi12")])
        merged = TopologyStore.merge(existing, fresh)
        assert merged.devices[0].missing_since is None

    def test_port_to_mac_rekey(self):
        existing = Topology(
            devices=[_rx(port="gi12", ignored=True)]
        )  # powered off at discovery
        fresh = Topology(
            devices=[_rx(mac="c2:00:00:00:00:03", port="gi12", ip="172.16.128.2")]
        )
        merged = TopologyStore.merge(existing, fresh)
        assert len(merged.devices) == 1
        dev = merged.devices[0]
        assert dev.key == "mac:c2:00:00:00:00:03"
        assert dev.ignored is True  # carried over through the re-key

    def test_new_device_added(self):
        existing = Topology(devices=[_tx(mac="c2:00:00:00:00:01", port="gi2", vlan=11)])
        fresh = Topology(
            devices=[
                _tx(mac="c2:00:00:00:00:01", port="gi2", vlan=11),
                _tx(mac="c2:00:00:00:00:09", port="gi5", vlan=14),
            ]
        )
        merged = TopologyStore.merge(existing, fresh)
        assert len(merged.devices) == 2

    def test_switch_metadata_from_fresh(self):
        existing = Topology(switch_ip="10.0.0.1", mode="jadconfig", model="SG300")
        fresh = Topology(switch_ip="10.66.4.3", mode="jadconfig", model=None)
        merged = TopologyStore.merge(existing, fresh)
        assert merged.switch_ip == "10.66.4.3"
        assert merged.model == "SG300"  # fresh None falls back to existing
