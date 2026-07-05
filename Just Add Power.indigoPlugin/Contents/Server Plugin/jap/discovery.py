"""System discovery: build a Topology from the switch config, the MAC
address table, and HTTP probes of the J+P devices.

Never writes to the switch. The MAC-table join is the authoritative
port↔device correlation; the switch's port classification (not a device's
self-reported role) decides TX vs RX. Devices that don't answer HTTP are
still created from switch data alone (partial discovery).
"""

try:
    import indigo  # noqa: F401
except ImportError:
    pass

import logging
import re
from concurrent.futures import ThreadPoolExecutor

from .backends.jadconfig_cisco import RUNNING_CONFIG_TIMEOUT
from .justapi import JustApiClient
from .running_config import (
    classify_ports,
    detect_mode,
    parse_mac_address_table,
    parse_running_config,
)
from .topology import ROLE_RX, ROLE_TX, JapDevice, SwitchPort, Topology

logger = logging.getLogger("Plugin")

PROBE_TIMEOUT = 1.5
MAX_HOSTS_PER_SUBNET = 254  # cap each scanned subnet at /24 worth of hosts

_SYSTEM_DESCRIPTION_RE = re.compile(r"System Description:\s+(.+?)\s*$", re.MULTILINE)


class DiscoveryError(Exception):
    """Discovery aborted: the switch config could not be interpreted."""


def probe_device_http(ip: str, timeout: float = PROBE_TIMEOUT):
    """Returns DeviceDetails or None."""
    return JustApiClient(ip, timeout=timeout).get_details()


def parse_system_model(show_system_output: str):
    m = _SYSTEM_DESCRIPTION_RE.search(show_system_output or "")
    return m.group(1).strip() if m else None


def enumerate_candidate_ips(settings, existing, rc, warnings):
    """Candidate device IPs: existing topology IPs ∪ configured subnets,
    else the J+P-related VLAN interface subnets from the running config.
    Each subnet is capped at MAX_HOSTS_PER_SUBNET hosts."""
    candidates = []
    seen = set()

    def add(ip_str):
        if ip_str not in seen:
            seen.add(ip_str)
            candidates.append(ip_str)

    if existing:
        for device in existing.devices:
            if device.ip:
                add(device.ip)

    if settings.device_subnets:
        subnets = list(settings.device_subnets)
    else:
        lo, hi = settings.tx_vlan_range
        subnets = []
        switch_own_ips = set()
        for vlan, vi in sorted(rc.vlan_interfaces.items()):
            if vi.network is None:
                continue
            if vlan == settings.all_devices_vlan or lo <= vlan <= hi:
                subnets.append(vi.network)
                switch_own_ips.add(vi.ip)
        # Don't probe the switch's own VLAN interface addresses.
        seen.update(switch_own_ips)

    for subnet in subnets:
        count = 0
        for host in subnet.hosts():
            if count >= MAX_HOSTS_PER_SUBNET:
                warnings.append(
                    f"Subnet {subnet} is larger than /24; scanning only the "
                    f"first {MAX_HOSTS_PER_SUBNET} hosts (set Device Subnet(s) "
                    "in Plugin Config to narrow the scan)"
                )
                break
            add(str(host))
            count += 1
    return candidates


def run_discovery(cli, settings, existing, *, prober=probe_device_http, max_workers=16):
    """Full discovery pass. Returns (fresh_topology, warnings).
    The caller merges the result with the stored topology (TopologyStore.merge)."""
    warnings = []

    raw_config = cli.run_command("show running-config", timeout=RUNNING_CONFIG_TIMEOUT)
    rc = parse_running_config(raw_config)
    if not rc.interfaces:
        raise DiscoveryError(
            "Could not parse any interface blocks from the running config — "
            "not proceeding with a half-applied topology"
        )

    mode, reason = detect_mode(rc, settings.all_devices_vlan, settings.tx_vlan_range)
    if "ambiguous" in reason.lower():
        warnings.append(f"Mode detection: {reason}")
    logger.info("Detected system mode: %s (%s)", mode, reason)

    classification = classify_ports(
        rc, settings.all_devices_vlan, settings.tx_vlan_range
    )
    warnings.extend(classification.warnings)
    jap_ports = set(classification.tx_ports) | set(classification.rx_ports)
    if not jap_ports:
        raise DiscoveryError(
            "No J+P ports classified from the switch config — check the "
            "all-devices VLAN and TX VLAN range settings"
        )

    # MAC table join (authoritative port <-> device identity).
    mac_entries = parse_mac_address_table(cli.run_command("show mac address-table"))
    port_macs = {}
    for entry in mac_entries:
        if entry.entry_type != "dynamic" or entry.port not in jap_ports:
            continue
        port_macs.setdefault(entry.port, set()).add(entry.mac)
    port_mac = {}
    for port, macs in port_macs.items():
        if len(macs) > 1:
            warnings.append(
                f"Port {port} has {len(macs)} MAC addresses ({sorted(macs)}); "
                "cannot identify the J+P device — falling back to port identity"
            )
        else:
            port_mac[port] = next(iter(macs))

    # Switch model (best effort).
    model = None
    try:
        model = parse_system_model(cli.run_command("show system"))
    except Exception as exc:  # noqa: BLE001 - model is cosmetic
        logger.debug("show system failed: %s", exc)

    # HTTP probe sweep.
    candidates = enumerate_candidate_ips(settings, existing, rc, warnings)
    responders_by_mac = {}
    if candidates:
        logger.info("Probing %d candidate device IPs...", len(candidates))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            results = pool.map(lambda ip: (ip, prober(ip)), candidates)
        for ip, details in results:
            if details is None:
                continue
            if details.mac is None:
                warnings.append(
                    f"Device at {ip} answered justAPI but reported no MAC; "
                    "cannot correlate it to a switch port"
                )
                continue
            responders_by_mac[details.mac] = (ip, details)

    # Assemble devices. Role comes from the switch classification.
    devices = []
    matched_macs = set()

    def build(port_name, role, vlan):
        mac = port_mac.get(port_name)
        ip = None
        details = None
        if mac and mac in responders_by_mac:
            ip, details = responders_by_mac[mac]
            matched_macs.add(mac)
        device = JapDevice(
            role=role,
            mac=mac,
            ip=ip,
            port=SwitchPort(port_name),
            vlan=vlan,
            device_name=details.device_name if details else None,
            model=details.model if details else None,
            firmware=details.firmware if details else None,
        )
        if details is None:
            logger.info(
                "Port %s (%s): no justAPI responder — created from switch data alone",
                port_name,
                role,
            )
        return device

    for port_name, vlan in sorted(classification.tx_ports.items()):
        devices.append(build(port_name, ROLE_TX, vlan))
    for port_name in sorted(classification.rx_ports):
        devices.append(build(port_name, ROLE_RX, None))

    for mac, (ip, _details) in sorted(responders_by_mac.items()):
        if mac not in matched_macs:
            warnings.append(
                f"justAPI device at {ip} (MAC {mac}) matches no classified "
                "J+P switch port — possibly on another switch; skipped"
            )

    fresh = Topology(
        switch_ip=settings.switch_ip, mode=mode, model=model, devices=devices
    )
    logger.info(
        "Discovery complete: %d TX, %d RX (%d reachable via justAPI)",
        len(classification.tx_ports),
        len(classification.rx_ports),
        len(matched_macs),
    )
    return fresh, warnings
