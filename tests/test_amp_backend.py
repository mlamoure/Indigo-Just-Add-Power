import pytest

from jap.backends.amp_jpsw import AmpJpswBackend
from jap.backends.base import BackendError
from jap.config import PluginSettings
from jap.justapi import JustApiClient
from jap.topology import JapDevice, SwitchPort, Topology
from tests.helpers import FakeHttp


def make_topology():
    return Topology(
        switch_ip="10.66.4.3",
        mode="amp",
        devices=[
            JapDevice(
                role="tx", mac="c2:00:00:00:00:01", port=SwitchPort("gi1"), vlan=101
            ),
            JapDevice(
                role="rx",
                mac="c2:00:00:00:00:02",
                port=SwitchPort("gi2"),
                ip="172.27.0.10",
            ),
            JapDevice(
                role="rx",
                mac="c2:00:00:00:00:03",
                port=SwitchPort("gi3"),
                ip="172.27.0.11",
            ),
        ],
    )


def make_backend(http, topology=None, all_devices_vlan=100):
    settings = PluginSettings(switch_ip="10.66.4.3", all_devices_vlan=all_devices_vlan)
    factory = lambda ip: JustApiClient(
        ip, http=http, sleep=lambda s: None
    )  # noqa: E731
    return AmpJpswBackend(settings, lambda: topology, client_factory=factory)


class TestAmpSwitch:
    def test_switch_posts_channel(self):
        http = FakeHttp([(r"POST .*command/channel", 200, b"ok")])
        topo = make_topology()
        backend = make_backend(http, topo)
        backend.switch(topo.find_by_port("gi2"), topo.find_by_port("gi1"))
        assert http.calls[0][1] == "http://172.27.0.10/cgi-bin/api/command/channel"
        assert http.calls[0][2] == "1"  # vlan 101 - all-devices 100

    def test_unreachable_device_raises_backend_error(self):
        http = FakeHttp()  # no routes
        topo = make_topology()
        backend = make_backend(http, topo)
        with pytest.raises(BackendError, match="experimental"):
            backend.switch(topo.find_by_port("gi2"), topo.find_by_port("gi1"))

    def test_rx_without_ip(self):
        topo = make_topology()
        rx = JapDevice(role="rx", mac="c2:00:00:00:00:09", port=SwitchPort("gi9"))
        backend = make_backend(FakeHttp(), topo)
        with pytest.raises(BackendError, match="no IP"):
            backend.switch(rx, topo.find_by_port("gi1"))

    def test_never_uses_command_cli(self):
        http = FakeHttp([(r"POST .*command/channel", 200, b"ok")])
        topo = make_topology()
        backend = make_backend(http, topo)
        backend.switch(topo.find_by_port("gi2"), topo.find_by_port("gi1"))
        backend.get_routing_state()
        assert all("/command/cli" not in call[1] for call in http.calls)


class TestAmpSwitchAll:
    def test_partial_failure_warns_but_succeeds(self, caplog):
        http = FakeHttp(
            [(r"POST http://172\.27\.0\.10/cgi-bin/api/command/channel", 200, b"ok")]
        )  # .11 unreachable
        topo = make_topology()
        backend = make_backend(http, topo)
        backend.switch_all(topo.find_by_port("gi1"))  # does not raise

    def test_total_failure_raises(self):
        topo = make_topology()
        backend = make_backend(FakeHttp(), topo)
        with pytest.raises(BackendError, match="every receiver"):
            backend.switch_all(topo.find_by_port("gi1"))


class TestAmpRoutingState:
    def test_best_effort_channels(self):
        http = FakeHttp(
            [
                (
                    r"GET http://172\.27\.0\.10/cgi-bin/api/details/channel",
                    200,
                    b'{"data": 1}',
                ),
                # .11 unreachable -> unknown
            ]
        )
        topo = make_topology()
        backend = make_backend(http, topo)
        state = backend.get_routing_state()
        assert state.rx_source == {"gi2": 101, "gi3": None}

    def test_no_topology(self):
        backend = make_backend(FakeHttp(), None)
        assert backend.get_routing_state().rx_source == {}


class TestAmpValidate:
    def test_experimental_warning(self):
        backend = make_backend(FakeHttp(), make_topology())
        issues = backend.validate()
        assert any(
            i.severity == "warning" and "EXPERIMENTAL" in i.message for i in issues
        )
