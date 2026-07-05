"""Pure parsers for Cisco SG300-family CLI output.

No I/O here — every function takes command output text (as returned by
CiscoCliClient.run_command, which includes the echoed command line and the
trailing prompt line) and returns structured data. Shared by discovery and
the JADConfig backend.
"""

try:
    import indigo  # noqa: F401
except ImportError:
    pass

import ipaddress
import logging
import re
from dataclasses import dataclass, field
from typing import NamedTuple

from .topology import normalize_ifname

logger = logging.getLogger("Plugin")

# Physical/aggregate port prefixes we recognize in interface names and port lists.
_PORT_TOKEN_RE = re.compile(
    r"^(gi|te|fa|po|gigabitethernet|tengigabitethernet|fastethernet|port-channel)"
    r"([\d/]*\d)(?:-(\d+))?$",
    re.IGNORECASE,
)

# Pager leftovers: the --More-- marker plus the CR/space/backspace erase dance.
_PAGER_ARTIFACT_RE = re.compile(r"--More--[ \x08]*\r?")


def strip_pager_artifacts(text: str) -> str:
    """Remove --More-- markers/erase artifacts and normalize line endings."""
    text = _PAGER_ARTIFACT_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


def expand_vlan_spec(spec: str) -> set:
    """'11' | '10-11' | '10,12' | '10,12-14,20' -> {ints}. Bad tokens are skipped."""
    vlans = set()
    for token in str(spec or "").split(","):
        token = token.strip()
        if not token:
            continue
        m = re.match(r"^(\d+)-(\d+)$", token)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if lo <= hi:
                vlans.update(range(lo, hi + 1))
        elif token.isdigit():
            vlans.add(int(token))
        else:
            logger.debug("Skipping unparseable VLAN token %r", token)
    return vlans


def format_vlan_spec(vlans) -> str:
    """{10, 12, 13, 14, 20} -> '10,12-14,20'."""
    ordered = sorted(set(vlans))
    parts = []
    i = 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and ordered[j + 1] == ordered[j] + 1:
            j += 1
        if j > i:
            parts.append(f"{ordered[i]}-{ordered[j]}")
        else:
            parts.append(str(ordered[i]))
        i = j + 1
    return ",".join(parts)


def expand_port_spec(spec: str) -> list:
    """'gi2,gi12,gi15-21' -> ['gi2', 'gi12', 'gi15', ..., 'gi21'].
    Tokens that don't look like ports are skipped."""
    ports = []
    for token in str(spec or "").split(","):
        token = token.strip()
        if not token:
            continue
        m = _PORT_TOKEN_RE.match(token)
        if not m:
            logger.debug("Skipping unparseable port token %r", token)
            continue
        prefix, first, range_end = m.group(1), m.group(2), m.group(3)
        base = normalize_ifname(prefix + first)
        if range_end is None:
            ports.append(base)
            continue
        # Range: expand the trailing number ('gi15-21', 'gi1/0/1-4').
        stem_match = re.match(r"^(.*?)(\d+)$", base)
        stem, start_num = stem_match.group(1), int(stem_match.group(2))
        for n in range(start_num, int(range_end) + 1):
            ports.append(f"{stem}{n}")
    return ports


@dataclass
class InterfaceConfig:
    name: str  # normalized, e.g. "gi7"
    pvid: int | None = None
    allowed_vlans: set = field(default_factory=set)
    untagged_vlans: set = field(default_factory=set)
    mode: str | None = None  # "general" | "access" | "trunk" | ...
    portfast: bool = False
    channel_group: int | None = None
    raw_lines: list = field(default_factory=list)


@dataclass
class VlanInterface:
    vlan: int
    name: str | None = None
    ip: str | None = None
    prefix_len: int | None = None

    @property
    def network(self):
        if self.ip is None or self.prefix_len is None:
            return None
        return ipaddress.ip_network(f"{self.ip}/{self.prefix_len}", strict=False)


@dataclass
class RunningConfig:
    hostname: str | None = None
    interfaces: dict = field(default_factory=dict)  # name -> InterfaceConfig
    vlans: set = field(default_factory=set)
    vlan_interfaces: dict = field(default_factory=dict)  # vlan -> VlanInterface
    jumbo_frames: bool = False


_IF_PHYS_RE = re.compile(
    r"^interface\s+((?:\S*ethernet|gi|te|fa|port-channel|po)[\d/]+)$", re.I
)
_IF_VLAN_RE = re.compile(r"^interface\s+vlan\s+(\d+)$", re.I)
_ALLOWED_ADD_RE = re.compile(
    r"^switchport\s+general\s+allowed\s+vlan\s+add\s+([\d,\-]+)\s*(untagged|tagged)?",
    re.I,
)
_PVID_RE = re.compile(r"^switchport\s+general\s+pvid\s+(\d+)", re.I)
_MODE_RE = re.compile(r"^switchport\s+mode\s+(\S+)", re.I)
_PORTFAST_RE = re.compile(r"^spanning-tree\s+portfast\b", re.I)
_CHANNEL_RE = re.compile(r"^channel-group\s+(\d+)", re.I)
_VLAN_DB_RE = re.compile(r"^vlan\s+([\d,\-]+)$", re.I)
_HOSTNAME_RE = re.compile(r"^hostname\s+(\S+)", re.I)
_VLAN_NAME_RE = re.compile(r"^name\s+(\S+)", re.I)
_IP_ADDR_RE = re.compile(
    r"^ip\s+address\s+(\d+\.\d+\.\d+\.\d+)\s+(\d+\.\d+\.\d+\.\d+)", re.I
)


def parse_running_config(text: str) -> RunningConfig:
    """Parse `show running-config` output. Tolerates the echoed command line,
    the trailing prompt line, pager artifacts, and unknown lines."""
    rc = RunningConfig()
    current_if = None  # InterfaceConfig
    current_vlan_if = None  # VlanInterface
    in_vlan_db = False

    for raw_line in strip_pager_artifacts(text).split("\n"):
        line = raw_line.strip()
        if not line:
            continue

        # Block terminators / section boundaries.
        if line in ("!", "exit", "end"):
            current_if = None
            current_vlan_if = None
            in_vlan_db = False
            continue

        m = _IF_VLAN_RE.match(line)
        if m:
            vlan = int(m.group(1))
            current_vlan_if = rc.vlan_interfaces.setdefault(vlan, VlanInterface(vlan))
            current_if = None
            in_vlan_db = False
            continue

        m = _IF_PHYS_RE.match(line)
        if m:
            name = normalize_ifname(m.group(1))
            current_if = rc.interfaces.setdefault(name, InterfaceConfig(name))
            current_vlan_if = None
            in_vlan_db = False
            continue

        if line.lower() == "vlan database":
            in_vlan_db = True
            current_if = None
            current_vlan_if = None
            continue

        if in_vlan_db:
            m = _VLAN_DB_RE.match(line)
            if m:
                rc.vlans.update(expand_vlan_spec(m.group(1)))
            continue

        if current_if is not None:
            current_if.raw_lines.append(line)
            m = _ALLOWED_ADD_RE.match(line)
            if m:
                vlans = expand_vlan_spec(m.group(1))
                current_if.allowed_vlans.update(vlans)
                if (m.group(2) or "untagged").lower() == "untagged":
                    current_if.untagged_vlans.update(vlans)
                continue
            m = _PVID_RE.match(line)
            if m:
                current_if.pvid = int(m.group(1))
                continue
            m = _MODE_RE.match(line)
            if m:
                current_if.mode = m.group(1).lower()
                continue
            if _PORTFAST_RE.match(line):
                current_if.portfast = True
                continue
            m = _CHANNEL_RE.match(line)
            if m:
                current_if.channel_group = int(m.group(1))
                continue
            continue

        if current_vlan_if is not None:
            m = _VLAN_NAME_RE.match(line)
            if m:
                current_vlan_if.name = m.group(1)
                continue
            m = _IP_ADDR_RE.match(line)
            if m:
                current_vlan_if.ip = m.group(1)
                try:
                    current_vlan_if.prefix_len = ipaddress.ip_network(
                        f"0.0.0.0/{m.group(2)}"
                    ).prefixlen
                except ValueError:
                    logger.debug("Bad netmask in line %r", line)
                continue
            continue

        # Global lines.
        if line.lower() == "port jumbo-frame":
            rc.jumbo_frames = True
            continue
        m = _HOSTNAME_RE.match(line)
        if m:
            rc.hostname = m.group(1)
            continue

    # The vlan database may not enumerate VLANs that only exist as interfaces.
    rc.vlans.update(rc.vlan_interfaces)
    return rc


class PortClassification(NamedTuple):
    tx_ports: dict  # ifname -> tx vlan (== pvid)
    rx_ports: dict  # ifname -> current source vlan | None
    warnings: list


def classify_ports(
    rc: RunningConfig, all_devices_vlan: int, tx_range
) -> PortClassification:
    """Classify general-mode ports per the JADConfig pattern:
    TX = pvid within the TX range; RX = pvid == all-devices VLAN."""
    lo, hi = tx_range
    tx_ports = {}
    rx_ports = {}
    warnings = []
    for name, itf in sorted(rc.interfaces.items()):
        if itf.mode != "general" or itf.pvid is None:
            continue
        if lo <= itf.pvid <= hi:
            tx_ports[name] = itf.pvid
        elif itf.pvid == all_devices_vlan:
            sources = sorted(v for v in itf.untagged_vlans if lo <= v <= hi)
            if not sources:
                rx_ports[name] = None
            else:
                if len(sources) > 1:
                    warnings.append(
                        f"RX port {name} is a member of multiple TX VLANs "
                        f"{sources}; using {sources[0]}"
                    )
                rx_ports[name] = sources[0]
    return PortClassification(tx_ports, rx_ports, warnings)


class MacEntry(NamedTuple):
    vlan: int
    mac: str
    port: str  # normalized ifname
    entry_type: str  # "dynamic" | "static" | "self" | ...


_MAC_ROW_RE = re.compile(r"^\s*(\d+)\s+([0-9a-fA-F:.\-]{12,17})\s+(\S+)\s+(\S+)\s*$")


def parse_mac_address_table(text: str) -> list:
    """Parse `show mac address-table` rows into MacEntry items."""
    from .topology import normalize_mac

    entries = []
    for line in strip_pager_artifacts(text).split("\n"):
        m = _MAC_ROW_RE.match(line)
        if not m:
            continue
        try:
            mac = normalize_mac(m.group(2))
        except ValueError:
            continue
        port = m.group(3)
        if port.isdigit():  # e.g. the switch's own CPU entry uses port "0"
            continue
        entries.append(
            MacEntry(
                vlan=int(m.group(1)),
                mac=mac,
                port=normalize_ifname(port),
                entry_type=m.group(4).lower(),
            )
        )
    return entries


class VlanTableRow(NamedTuple):
    vlan: int
    name: str
    tagged_ports: list
    untagged_ports: list


def parse_vlan_table(text: str) -> dict:
    """Parse `show vlan` into {vlan: VlanTableRow} using the dashed ruler line
    for column geometry (values are centered in their columns)."""
    lines = strip_pager_artifacts(text).split("\n")
    ruler_idx = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped and set(stripped) <= {"-", " "} and "-" in stripped:
            ruler_idx = i
            break
    if ruler_idx is None:
        return {}

    ruler = lines[ruler_idx]
    spans = []
    start = None
    for i, ch in enumerate(ruler):
        if ch == "-" and start is None:
            start = i
        elif ch != "-" and start is not None:
            spans.append((start, i))
            start = None
    if start is not None:
        spans.append((start, len(ruler)))
    if len(spans) < 4:
        return {}

    def cell(line, span_idx):
        # Extend one char into the single-space separators for centered
        # values that touch their column edge.
        s, e = spans[span_idx]
        return line[max(s - 1, 0) : e + 1].strip()

    rows = {}
    for line in lines[ruler_idx + 1 :]:
        if not line.strip():
            continue
        vlan_cell = cell(line, 0)
        if not vlan_cell.isdigit():
            continue  # prompt line, "Created by:" legend, wrapped garbage
        vlan = int(vlan_cell)
        rows[vlan] = VlanTableRow(
            vlan=vlan,
            name=cell(line, 1),
            tagged_ports=expand_port_spec(cell(line, 2)),
            untagged_ports=expand_port_spec(cell(line, 3)),
        )
    return rows


def routing_from_vlan_table(vlan_rows: dict, rx_port_names, tx_range) -> dict:
    """Fast routing state from `show vlan`: for each known RX port, find the
    TX-range VLAN that lists it untagged. Returns {rx_port: vlan | None}."""
    lo, hi = tx_range
    routing = {name: None for name in rx_port_names}
    for vlan in sorted(vlan_rows):
        if not lo <= vlan <= hi:
            continue
        for port in vlan_rows[vlan].untagged_ports:
            if port in routing and routing[port] is None:
                routing[port] = vlan
    return routing


def detect_mode(rc: RunningConfig, all_devices_vlan: int = 10, tx_range=(11, 410)):
    """Detect JADConfig vs AMP-standardized configuration.
    Returns (mode, human-readable reason)."""
    lo, hi = tx_range
    jad_score = 0
    amp_score = 0
    evidence = []

    if all_devices_vlan in rc.vlans:
        jad_score += 1
        evidence.append(f"VLAN {all_devices_vlan} (all-devices) exists")
    general_tx_pvids = [
        itf.pvid
        for itf in rc.interfaces.values()
        if itf.mode == "general" and itf.pvid is not None and lo <= itf.pvid <= hi
    ]
    if general_tx_pvids:
        jad_score += 2
        evidence.append(
            f"{len(general_tx_pvids)} general-mode ports with TX-range pvids"
        )

    amp_vlans = [v for v in rc.vlans if 100 <= v <= 201]
    amp_nets = [
        vi
        for vi in rc.vlan_interfaces.values()
        if vi.ip is not None and vi.ip.startswith("172.27.")
    ]
    if amp_vlans and amp_nets:
        amp_score += 3
        evidence.append(
            f"{len(amp_vlans)} VLANs in 100-201 with 172.27.0.0/16 addressing"
        )

    if amp_score > jad_score:
        return ("amp", "; ".join(evidence))
    if jad_score == 0:
        return ("jadconfig", "ambiguous configuration — defaulting to JADConfig")
    return ("jadconfig", "; ".join(evidence))
