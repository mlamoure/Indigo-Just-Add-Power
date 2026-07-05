"""Topology data model: switch ports, J+P devices, routing state.

Pure data structures shared by discovery, backends, and plugin.py. No I/O.
"""

try:
    import indigo  # noqa: F401
except ImportError:
    pass

import re
from dataclasses import dataclass, field, asdict
from typing import NamedTuple

# Canonical short prefixes used by the Sx300/Sx500/Sx350 CLI.
_IFNAME_PREFIXES = [
    ("tengigabitethernet", "te"),
    ("gigabitethernet", "gi"),
    ("fastethernet", "fa"),
    ("port-channel", "po"),
]

_IFNAME_RE = re.compile(r"^([a-z\-]+)\s*([\d/]+)$")


def normalize_ifname(raw: str) -> str:
    """Normalize an interface name to the short CLI form, e.g.
    'GigabitEthernet7' -> 'gi7', 'gi1/0/7' -> 'gi1/0/7', 'Po1' -> 'po1'."""
    name = raw.strip().lower()
    m = _IFNAME_RE.match(name)
    if not m:
        return name
    prefix, suffix = m.group(1), m.group(2)
    for long, short in _IFNAME_PREFIXES:
        if prefix == long:
            return short + suffix
    return prefix + suffix


def normalize_mac(raw: str) -> str:
    """Normalize a MAC address to lowercase colon form.
    Accepts 'aa:bb:cc:dd:ee:ff', 'AA-BB-...', 'aabb.ccdd.eeff'."""
    hex_only = re.sub(r"[^0-9a-fA-F]", "", raw)
    if len(hex_only) != 12:
        raise ValueError(f"invalid MAC address: {raw!r}")
    hex_only = hex_only.lower()
    return ":".join(hex_only[i : i + 2] for i in range(0, 12, 2))


@dataclass(frozen=True)
class SwitchPort:
    name: str  # canonical, e.g. "gi7"

    @property
    def number(self):
        """Trailing port number when parseable ('gi7' -> 7), else None."""
        m = re.search(r"(\d+)$", self.name)
        return int(m.group(1)) if m else None


ROLE_TX = "tx"
ROLE_RX = "rx"


@dataclass
class JapDevice:
    role: str  # ROLE_TX | ROLE_RX
    mac: str | None = None
    ip: str | None = None
    port: SwitchPort | None = None
    vlan: int | None = None  # tx: its own VLAN; rx: unused (None)
    device_name: str | None = None
    model: str | None = None
    firmware: str | None = None
    discovered: bool = True
    manual: bool = False
    ignored: bool = False
    missing_since: str | None = None  # ISO timestamp, set when discovery loses it

    @property
    def key(self) -> str:
        """Stable identity used as the Indigo device address."""
        if self.mac:
            return f"mac:{self.mac}"
        if self.port:
            return f"port:{self.port.name}"
        raise ValueError("JapDevice has neither mac nor port; no stable key")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["port"] = self.port.name if self.port else None
        d["key"] = self.key
        return d


@dataclass
class Topology:
    switch_ip: str = ""
    mode: str = "jadconfig"
    model: str | None = None
    devices: list = field(default_factory=list)

    def tx_devices(self) -> list:
        return [d for d in self.devices if d.role == ROLE_TX]

    def rx_devices(self) -> list:
        return [d for d in self.devices if d.role == ROLE_RX]

    def tx_by_vlan(self, vlan: int):
        for d in self.tx_devices():
            if d.vlan == vlan:
                return d
        return None

    def find_by_key(self, key: str):
        for d in self.devices:
            if d.key == key:
                return d
        return None

    def find_by_port(self, port_name: str):
        for d in self.devices:
            if d.port and d.port.name == port_name:
                return d
        return None


class RoutingChange(NamedTuple):
    rx_port: str
    old_vlan: int | None
    new_vlan: int | None


@dataclass
class RoutingState:
    """Maps each RX port name to the TX VLAN it currently watches (None = no/unknown source)."""

    rx_source: dict
    captured_at: float = 0.0

    def diff(self, other: "RoutingState") -> list:
        """Changes going from self (old) to other (new)."""
        changes = []
        ports = set(self.rx_source) | set(other.rx_source)
        for port in sorted(ports):
            old = self.rx_source.get(port)
            new = other.rx_source.get(port)
            if old != new:
                changes.append(RoutingChange(port, old, new))
        return changes


class ReconcileResult(NamedTuple):
    confirmed: list  # [(rx_port, vlan)]
    reverted: list  # [(rx_port, expected_vlan, observed_vlan)]
    pending: set  # rx_port names still awaiting confirmation


class PendingSwitchTracker:
    """Optimistic-update bookkeeping for plugin-initiated switches.

    After a switch command succeeds at the CLI, the RX state is updated
    optimistically and recorded here. Each routing poll calls reconcile():
    a matching observation confirms; two consecutive mismatches revert.
    """

    CONFIRM_ATTEMPTS = 2

    def __init__(self):
        self._pending = {}  # rx_port -> [expected_vlan, attempts_left]

    def record(self, rx_port: str, expected_vlan: int) -> None:
        self._pending[rx_port] = [expected_vlan, self.CONFIRM_ATTEMPTS]

    def clear(self) -> None:
        self._pending.clear()

    @property
    def pending_ports(self) -> set:
        return set(self._pending)

    def reconcile(self, observed: RoutingState) -> ReconcileResult:
        confirmed = []
        reverted = []
        for rx_port in list(self._pending):
            expected, attempts_left = self._pending[rx_port]
            observed_vlan = observed.rx_source.get(rx_port)
            if observed_vlan == expected:
                confirmed.append((rx_port, expected))
                del self._pending[rx_port]
            else:
                attempts_left -= 1
                if attempts_left <= 0:
                    reverted.append((rx_port, expected, observed_vlan))
                    del self._pending[rx_port]
                else:
                    self._pending[rx_port][1] = attempts_left
        return ReconcileResult(confirmed, reverted, set(self._pending))
