"""AMP-standardized backend (EXPERIMENTAL, best effort).

In AMP mode, switching is performed by sending JPSW channel commands to the
J+P devices themselves rather than reconfiguring the switch. JPSW is
documented for AMP-configured Ultra/MaxColor systems and newer firmware;
availability on 3G B-firmware devices is unverified, so every operation here
degrades to a warning rather than crashing the plugin.

Channel convention: channel N corresponds to TX VLAN (all_devices_vlan + N)
in JADConfig terms, and (100 + N) in the AMP VLAN scheme; we derive the
channel from the TX VLAN relative to the configured all-devices VLAN.

The /cgi-bin/api/command/cli endpoint is never used (JSON-breaking bug on
B1.x firmware).
"""

try:
    import indigo  # noqa: F401
except ImportError:
    pass

import logging
import time

from ..justapi import JustApiClient
from ..topology import RoutingState
from .base import (
    SEVERITY_WARNING,
    BackendError,
    SwitchingBackend,
    ValidationIssue,
)

logger = logging.getLogger("Plugin")


class AmpJpswBackend(SwitchingBackend):
    def __init__(self, settings, topology_provider, client_factory=JustApiClient):
        self._settings = settings
        self._topology_provider = topology_provider
        self._client_factory = client_factory

    def _channel_for(self, tx) -> int:
        if tx.vlan is None:
            raise BackendError(f"Transmitter {tx.key} has no VLAN assigned")
        channel = tx.vlan - self._settings.all_devices_vlan
        if channel < 1:
            raise BackendError(
                f"Cannot derive JPSW channel from TX VLAN {tx.vlan} "
                f"(all-devices VLAN {self._settings.all_devices_vlan})"
            )
        return channel

    def switch(self, rx, tx) -> None:
        if rx.ip is None:
            raise BackendError(f"Receiver {rx.key} has no IP address; JPSW needs one")
        channel = self._channel_for(tx)
        client = self._client_factory(rx.ip)
        if not client.set_channel(channel):
            raise BackendError(
                f"JPSW channel change rejected by {rx.ip} (channel {channel}). "
                "AMP support is experimental — the device may not support JPSW."
            )
        logger.debug("JPSW: %s -> channel %s", rx.ip, channel)

    def switch_all(self, tx) -> None:
        topology = self._topology_provider()
        if not topology:
            raise BackendError("No topology available; run discovery first")
        rx_devices = [
            d for d in topology.rx_devices() if d.ip is not None and not d.ignored
        ]
        if not rx_devices:
            raise BackendError("No receivers with IP addresses in the topology")
        failures = []
        for rx in rx_devices:
            try:
                self.switch(rx, tx)
            except BackendError as exc:
                failures.append(str(exc))
        if failures and len(failures) == len(rx_devices):
            raise BackendError(f"JPSW switch failed for every receiver: {failures[0]}")
        for failure in failures:
            logger.warning("switch_all (AMP): %s", failure)

    def get_routing_state(self) -> RoutingState:
        """Best effort: ask each RX for its current channel. Unreachable or
        unsupported devices report None (unknown)."""
        topology = self._topology_provider()
        rx_source = {}
        if topology:
            for rx in topology.rx_devices():
                if rx.port is None:
                    continue
                vlan = None
                if rx.ip:
                    channel = self._client_factory(rx.ip).get_channel()
                    if channel is not None:
                        vlan = self._settings.all_devices_vlan + channel
                rx_source[rx.port.name] = vlan
        return RoutingState(rx_source=rx_source, captured_at=time.monotonic())

    def validate(self) -> list:
        issues = [
            ValidationIssue(
                SEVERITY_WARNING,
                "AMP-standardized mode is EXPERIMENTAL (best effort): JPSW on "
                "3G B firmware is unverified. Switching failures will be "
                "reported per operation.",
            )
        ]
        return issues
