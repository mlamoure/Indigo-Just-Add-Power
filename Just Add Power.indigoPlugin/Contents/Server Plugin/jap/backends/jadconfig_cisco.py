"""JADConfig backend: matrix switching and routing state via the Cisco
SG300-family CLI.

The switching sequence is the whitepaper/legacy-verified one and is asserted
verbatim by tests — do not restructure it:

    enable
    configure
    interface <rx-port>
    switchport general allowed vlan remove <tx-vlan-range>
    switchport general allowed vlan add <tx-vlan> untagged
    end

Routing state uses `show vlan` (~0.7s on the reference SG350) rather than
`show running-config` (~11s measured); the full config is fetched only for
discovery and validation.
"""

try:
    import indigo  # noqa: F401
except ImportError:
    pass

import logging
import re
import time

from ..cisco_cli import CiscoCliError
from ..running_config import (
    classify_ports,
    parse_running_config,
    parse_vlan_table,
    routing_from_vlan_table,
)
from ..topology import RoutingState
from .base import (
    SEVERITY_ERROR,
    SEVERITY_OK,
    SEVERITY_WARNING,
    BackendError,
    SwitchingBackend,
    ValidationIssue,
)

logger = logging.getLogger("Plugin")

RUNNING_CONFIG_TIMEOUT = 30.0  # measured ~11s on the reference SG350

# Cisco CLI error phrases (checked per line, plus any line starting with %).
_ERROR_PHRASES = ("invalid input", "unrecognized command", "incomplete command")

_RELOAD_CONFIRM_RE = re.compile(rb"\(Y/N\)\[[YN]\] ?$")
_PROMPT_RE = re.compile(rb"(?:^|[\r\n])[^\r\n]*?[>#] ?$")


# `enable` is sent to guarantee privileged EXEC, but many JADConfig switches
# log a level-15 user straight into privileged mode (prompt already "#"), where
# `enable` returns "% Unrecognized command". That is harmless — the following
# `configure` would fail with its own error if we were NOT privileged — so the
# enable command's output is never treated as fatal.
_LENIENT_COMMANDS = {"enable"}


def _check_output(cmd: str, output: str) -> None:
    if cmd in _LENIENT_COMMANDS:
        return
    for line in output.splitlines():
        stripped = line.strip()
        lowered = stripped.lower()
        if stripped.startswith("%") or any(p in lowered for p in _ERROR_PHRASES):
            raise BackendError(f"Switch rejected '{cmd}': {stripped[:200]}")


class JadConfigCiscoBackend(SwitchingBackend):
    def __init__(self, cli, settings, topology_provider):
        self._cli = cli
        self._settings = settings
        self._topology_provider = topology_provider

    # -- reads ----------------------------------------------------------------

    def fetch_running_config(self) -> str:
        return self._cli.run_command(
            "show running-config", timeout=RUNNING_CONFIG_TIMEOUT
        )

    def _rx_port_names(self):
        topology = self._topology_provider()
        if not topology:
            return []
        return [d.port.name for d in topology.rx_devices() if d.port is not None]

    def get_routing_state(self) -> RoutingState:
        rx_ports = self._rx_port_names()
        if rx_ports:
            output = self._cli.run_command("show vlan")
            rows = parse_vlan_table(output)
            if not rows:
                raise BackendError("Could not parse 'show vlan' output")
            rx_source = routing_from_vlan_table(
                rows, rx_ports, self._settings.tx_vlan_range
            )
        else:
            # No topology yet (first startup): derive everything from the
            # running config, which classifies ports on its own.
            rc = parse_running_config(self.fetch_running_config())
            classification = classify_ports(
                rc, self._settings.all_devices_vlan, self._settings.tx_vlan_range
            )
            rx_source = dict(classification.rx_ports)
        return RoutingState(rx_source=rx_source, captured_at=time.monotonic())

    # -- writes ---------------------------------------------------------------

    def _switch_commands(self, rx_port: str, tx_vlan: int):
        return [
            f"interface {rx_port}",
            f"switchport general allowed vlan remove {self._settings.remove_range}",
            f"switchport general allowed vlan add {tx_vlan} untagged",
        ]

    def switch(self, rx, tx) -> None:
        if rx.port is None:
            raise BackendError(f"Receiver {rx.key} has no switch port assigned")
        if tx.vlan is None:
            raise BackendError(f"Transmitter {tx.key} has no VLAN assigned")
        commands = (
            ["enable", "configure"]
            + self._switch_commands(rx.port.name, tx.vlan)
            + ["end"]
        )
        outputs = self._cli.run_commands(commands)
        for cmd, output in zip(commands, outputs):
            _check_output(cmd, output)
        logger.debug("Switched %s -> VLAN %s", rx.port.name, tx.vlan)

    def switch_all(self, tx) -> None:
        if tx.vlan is None:
            raise BackendError(f"Transmitter {tx.key} has no VLAN assigned")
        topology = self._topology_provider()
        if not topology:
            raise BackendError("No topology available; run discovery first")
        rx_devices = [
            d for d in topology.rx_devices() if d.port is not None and not d.ignored
        ]
        if not rx_devices:
            raise BackendError("No receivers with switch ports in the topology")
        commands = ["enable", "configure"]
        for rx in rx_devices:
            commands.extend(self._switch_commands(rx.port.name, tx.vlan))
        commands.append("end")
        outputs = self._cli.run_commands(commands)
        for cmd, output in zip(commands, outputs):
            _check_output(cmd, output)
        logger.debug("Switched all %d receivers -> VLAN %s", len(rx_devices), tx.vlan)

    def reboot_switch(self) -> None:
        """Send `reload` and confirm. The session drops as the switch goes
        down, so transport errors after confirmation are expected."""
        logger.info("Sending reload to the switch")
        try:
            out = self._cli.run_dialog([("reload", [_RELOAD_CONFIRM_RE, _PROMPT_RE])])[
                0
            ]
            for _ in range(2):
                if "(Y/N)" not in out:
                    break
                out = self._cli.run_dialog(
                    [("Y", [_RELOAD_CONFIRM_RE, _PROMPT_RE])], timeout=3.0
                )[0]
        except CiscoCliError as exc:
            logger.debug("Connection dropped during reload (expected): %s", exc)
        finally:
            self._cli.close()

    # -- validation -----------------------------------------------------------

    def validate(self) -> list:
        issues = []
        try:
            self._cli.connect()
            issues.append(
                ValidationIssue(
                    SEVERITY_OK, f"Telnet CLI reachable at {self._settings.switch_ip}"
                )
            )
        except CiscoCliError as exc:
            issues.append(
                ValidationIssue(SEVERITY_ERROR, f"Telnet CLI unreachable: {exc}")
            )
            return issues

        try:
            rc = parse_running_config(self.fetch_running_config())
        except CiscoCliError as exc:
            issues.append(
                ValidationIssue(
                    SEVERITY_ERROR, f"Could not fetch running config: {exc}"
                )
            )
            return issues

        classification = classify_ports(
            rc, self._settings.all_devices_vlan, self._settings.tx_vlan_range
        )
        for warning in classification.warnings:
            issues.append(ValidationIssue(SEVERITY_WARNING, warning))

        if classification.tx_ports:
            issues.append(
                ValidationIssue(
                    SEVERITY_OK,
                    f"{len(classification.tx_ports)} TX and "
                    f"{len(classification.rx_ports)} RX ports classified",
                )
            )
        else:
            issues.append(
                ValidationIssue(
                    SEVERITY_ERROR,
                    "No TX ports found — is this switch JADConfig-configured?",
                )
            )

        jap_ports = sorted(classification.tx_ports) + sorted(classification.rx_ports)
        no_portfast = [
            p for p in jap_ports if p in rc.interfaces and not rc.interfaces[p].portfast
        ]
        if no_portfast:
            issues.append(
                ValidationIssue(
                    SEVERITY_WARNING,
                    "spanning-tree portfast is missing on J+P ports "
                    f"({', '.join(no_portfast)}) — switching may not be seamless",
                )
            )
        else:
            issues.append(
                ValidationIssue(
                    SEVERITY_OK, "spanning-tree portfast set on all J+P ports"
                )
            )

        if rc.jumbo_frames:
            issues.append(ValidationIssue(SEVERITY_OK, "Jumbo frames enabled"))
        else:
            issues.append(
                ValidationIssue(
                    SEVERITY_WARNING,
                    "Jumbo frames not enabled (port jumbo-frame) — video quality may suffer",
                )
            )
        return issues
