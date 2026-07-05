"""Plugin settings (from Indigo prefs) and persistent topology storage.

PluginSettings.from_prefs never raises — bad values fall back to defaults so a
mangled prefs file can't keep the plugin from starting. TopologyStore owns the
JSON topology file and the discovered/manual merge semantics.
"""

try:
    import indigo  # noqa: F401
except ImportError:
    pass

import datetime
import ipaddress
import json
import logging
import os
import re
from dataclasses import dataclass, field

from .topology import JapDevice, SwitchPort, Topology, normalize_mac

logger = logging.getLogger("Plugin")

SCHEMA_VERSION = 1

DEFAULT_ALL_DEVICES_VLAN = 10
DEFAULT_TX_VLAN_RANGE = (11, 410)
DEFAULT_ROUTING_POLL_SECS = 30
DEFAULT_DEVICE_POLL_SECS = 60


def _coerce_int(value, default):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def parse_vlan_range(value, default=DEFAULT_TX_VLAN_RANGE):
    """Parse '11-410' into (11, 410)."""
    m = re.match(r"^\s*(\d+)\s*-\s*(\d+)\s*$", str(value or ""))
    if not m:
        return default
    lo, hi = int(m.group(1)), int(m.group(2))
    if lo > hi:
        return default
    return (lo, hi)


def parse_subnets(value):
    """Parse a comma-separated CIDR list into [ipaddress.IPv4Network].
    Invalid entries are dropped with a warning."""
    subnets = []
    for token in str(value or "").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            subnets.append(ipaddress.ip_network(token, strict=False))
        except ValueError:
            logger.warning("Ignoring invalid subnet in device_subnet: %r", token)
    return subnets


@dataclass
class PluginSettings:
    switch_ip: str = ""
    username: str = "cisco"
    password: str = "cisco"
    all_devices_vlan: int = DEFAULT_ALL_DEVICES_VLAN
    tx_vlan_range: tuple = DEFAULT_TX_VLAN_RANGE
    routing_poll_secs: int = DEFAULT_ROUTING_POLL_SECS
    device_poll_secs: int = DEFAULT_DEVICE_POLL_SECS
    device_subnets: list = field(default_factory=list)
    snapshots_enabled: bool = False
    snapshot_dir: str = ""
    log_level: int = logging.INFO

    @property
    def remove_range(self) -> str:
        """VLAN range removed from an RX port before adding the new source."""
        return f"{self.tx_vlan_range[0]}-{self.tx_vlan_range[1]}"

    @classmethod
    def from_prefs(cls, prefs) -> "PluginSettings":
        prefs = prefs or {}
        return cls(
            switch_ip=str(prefs.get("switch_ip", "")).strip(),
            username=str(prefs.get("switch_username", "cisco")),
            password=str(prefs.get("switch_password", "cisco")),
            all_devices_vlan=_coerce_int(
                prefs.get("all_devices_vlan"), DEFAULT_ALL_DEVICES_VLAN
            ),
            tx_vlan_range=parse_vlan_range(prefs.get("tx_vlan_range")),
            routing_poll_secs=_coerce_int(
                prefs.get("routing_poll_secs"), DEFAULT_ROUTING_POLL_SECS
            ),
            device_poll_secs=_coerce_int(
                prefs.get("device_poll_secs"), DEFAULT_DEVICE_POLL_SECS
            ),
            device_subnets=parse_subnets(prefs.get("device_subnet")),
            snapshots_enabled=bool(prefs.get("snapshots_enabled", False)),
            snapshot_dir=str(prefs.get("snapshot_dir", "")).strip(),
            log_level=_coerce_int(prefs.get("log_level"), logging.INFO),
        )


def validate_prefs(values_dict):
    """For validatePrefsConfigUi: returns (ok, values_dict, errors_dict)."""
    errors = {}
    ip = str(values_dict.get("switch_ip", "")).strip()
    if not ip:
        errors["switch_ip"] = "Switch IP address is required."
    else:
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            errors["switch_ip"] = f"'{ip}' is not a valid IP address."

    vlan = str(values_dict.get("all_devices_vlan", "")).strip()
    if vlan and not vlan.isdigit():
        errors["all_devices_vlan"] = "All-devices VLAN must be a number."

    rng = str(values_dict.get("tx_vlan_range", "")).strip()
    if rng and not re.match(r"^\d+\s*-\s*\d+$", rng):
        errors["tx_vlan_range"] = "TX VLAN range must look like '11-410'."

    subnets = str(values_dict.get("device_subnet", "")).strip()
    if subnets:
        for token in subnets.split(","):
            token = token.strip()
            if not token:
                continue
            try:
                ipaddress.ip_network(token, strict=False)
            except ValueError:
                errors["device_subnet"] = f"'{token}' is not a valid CIDR subnet."

    return (not errors, values_dict, errors)


def _device_from_dict(entry) -> JapDevice | None:
    """Defensively build a JapDevice from a JSON entry; None if unusable."""
    if not isinstance(entry, dict):
        return None
    role = entry.get("role")
    if role not in ("tx", "rx"):
        return None
    mac = entry.get("mac")
    if mac:
        try:
            mac = normalize_mac(str(mac))
        except ValueError:
            logger.warning("Dropping invalid MAC %r in topology entry", mac)
            mac = None
    port_name = entry.get("port")
    port = SwitchPort(str(port_name)) if port_name else None
    if not mac and not port:
        return None  # no stable identity
    vlan = entry.get("vlan")
    try:
        vlan = int(vlan) if vlan is not None else None
    except (TypeError, ValueError):
        vlan = None

    def _str_or_none(key):
        value = entry.get(key)
        return str(value) if value is not None else None

    return JapDevice(
        role=role,
        mac=mac,
        ip=_str_or_none("ip"),
        port=port,
        vlan=vlan,
        device_name=_str_or_none("device_name"),
        model=_str_or_none("model"),
        firmware=_str_or_none("firmware"),
        discovered=bool(entry.get("discovered", True)),
        manual=bool(entry.get("manual", False)),
        ignored=bool(entry.get("ignored", False)),
        missing_since=_str_or_none("missing_since"),
    )


class TopologyStore:
    """Loads/saves the topology JSON and merges rediscovery results."""

    def __init__(self, path: str):
        self.path = path

    def load(self) -> Topology | None:
        if not os.path.exists(self.path):
            return None
        try:
            with open(self.path) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not load topology file %s: %s", self.path, exc)
            return None
        if not isinstance(data, dict):
            logger.warning("Topology file %s has unexpected shape; ignoring", self.path)
            return None
        switch = data.get("switch") or {}
        devices = []
        for entry in data.get("devices") or []:
            device = _device_from_dict(entry)
            if device is None:
                logger.warning("Dropping malformed topology entry: %r", entry)
            else:
                devices.append(device)
        return Topology(
            switch_ip=str(switch.get("ip", "")),
            mode=str(switch.get("mode", "jadconfig")),
            model=switch.get("model"),
            devices=devices,
        )

    def save(self, topology: Topology, now=None) -> None:
        now = now or datetime.datetime.now().isoformat(timespec="seconds")
        data = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": now,
            "switch": {
                "ip": topology.switch_ip,
                "mode": topology.mode,
                "model": topology.model,
            },
            "devices": [d.to_dict() for d in topology.devices],
        }
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp_path = self.path + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, self.path)

    @staticmethod
    def merge(existing: Topology | None, fresh: Topology, now=None) -> Topology:
        """Merge a rediscovery result into the stored topology.

        - Identity is JapDevice.key (MAC preferred). A port-keyed entry whose
          port now reveals a MAC is re-keyed to the MAC entry.
        - manual entries survive untouched; fresh data only fills None gaps.
        - discovered entries are replaced wholesale (ignored flag preserved).
        - Entries absent from fresh are kept with missing_since set.
        """
        now = now or datetime.datetime.now().isoformat(timespec="seconds")
        if existing is None:
            return fresh

        merged = []
        matched_existing = set()

        def _find_existing(fresh_dev):
            for i, old in enumerate(existing.devices):
                if i in matched_existing:
                    continue
                if old.key == fresh_dev.key:
                    return i, old
            # Port→MAC re-key: old entry keyed by port, fresh has a MAC on that port.
            if fresh_dev.mac and fresh_dev.port:
                for i, old in enumerate(existing.devices):
                    if i in matched_existing:
                        continue
                    if (
                        not old.mac
                        and old.port
                        and old.port.name == fresh_dev.port.name
                    ):
                        return i, old
            return None, None

        for fresh_dev in fresh.devices:
            idx, old = _find_existing(fresh_dev)
            if old is None:
                merged.append(fresh_dev)
                continue
            matched_existing.add(idx)
            if old.manual:
                # Manual entries win; discovery only fills gaps.
                for attr in (
                    "mac",
                    "ip",
                    "vlan",
                    "device_name",
                    "model",
                    "firmware",
                    "port",
                ):
                    if getattr(old, attr) is None:
                        setattr(old, attr, getattr(fresh_dev, attr))
                old.missing_since = None
                merged.append(old)
            else:
                fresh_dev.ignored = old.ignored
                fresh_dev.missing_since = None
                merged.append(fresh_dev)

        for i, old in enumerate(existing.devices):
            if i in matched_existing:
                continue
            if old.missing_since is None:
                old.missing_since = now
            merged.append(old)

        return Topology(
            switch_ip=fresh.switch_ip or existing.switch_ip,
            mode=fresh.mode,
            model=fresh.model or existing.model,
            devices=merged,
        )
