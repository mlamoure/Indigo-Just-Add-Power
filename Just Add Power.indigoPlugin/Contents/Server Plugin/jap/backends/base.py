"""SwitchingBackend interface: all matrix operations go through this so
JADConfig and AMP systems differ only in backend selection."""

try:
    import indigo  # noqa: F401
except ImportError:
    pass

from abc import ABC, abstractmethod
from dataclasses import dataclass


class BackendError(Exception):
    """A switching operation failed at the backend level."""


SEVERITY_OK = "ok"
SEVERITY_WARNING = "warning"
SEVERITY_ERROR = "error"


@dataclass
class ValidationIssue:
    severity: str  # ok | warning | error
    message: str

    @property
    def symbol(self) -> str:
        return {"ok": "✓", "warning": "⚠", "error": "✗"}.get(self.severity, "?")


class SwitchingBackend(ABC):
    """Matrix switching operations.

    Backends receive a `topology_provider` callable returning the current
    Topology (or None before first discovery), because both read and write
    paths need the port/VLAN map: the source of truth for routing differs by
    mode (switch config for JADConfig, devices for AMP)."""

    @abstractmethod
    def get_routing_state(self):
        """Return a RoutingState mapping each RX port to its watched TX VLAN."""

    @abstractmethod
    def switch(self, rx, tx) -> None:
        """Point one receiver at a transmitter."""

    @abstractmethod
    def switch_all(self, tx) -> None:
        """Point every (non-ignored) receiver at a transmitter."""

    @abstractmethod
    def validate(self) -> list:
        """Health/sanity checks; returns [ValidationIssue]."""
