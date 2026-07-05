"""justOS HTTP API client for Just Add Power 3G endpoints (B firmware).

Endpoint map (legacy-verified 2018 on 3G B firmware, re-verified live at v1):
    GET/POST http://<ip>/cgi-bin/api/settings/imagepull
        GET returns {"data": false | {"width","priority","frequency"}}
        enable body  {"width":"320","priority":"low","frequency":"3"}
        disable body null
    POST http://<ip>/cgi-bin/api/command/device   body "save" | "reboot"
        (reboot: the device drops before responding — a timeout IS success)
    GET http://<ip>/pull.bmp                       image pull snapshot

The HTTP layer is an injectable callable (method, url, body, timeout) ->
(status, bytes). The default uses `requests` when importable, else
urllib.request — neither is a hard dependency of the plugin.

Never use /cgi-bin/api/command/cli: known JSON-breaking bug on B1.x firmware.
"""

try:
    import indigo  # noqa: F401
except ImportError:
    pass

import json
import logging
import time
from dataclasses import dataclass

from .topology import normalize_mac

logger = logging.getLogger("Plugin")

DEFAULT_TIMEOUT = 3.0
IMAGE_TIMEOUT = 5.0
REBOOT_TIMEOUT = 1.0
REST_WAIT_SECS = 4  # settle time between imagepull setting / save / reboot

# Live-verified 2026-07-05 on 3G B2.3.9: /cgi-bin/api/details/device answers;
# the bare /details/device variant returns 404 on this firmware.
DETAILS_PATHS = ("/cgi-bin/api/details/device",)


class HttpTimeout(Exception):
    """Normalized timeout from any HTTP implementation."""


def _requests_http(method, url, body, timeout):
    import requests

    try:
        resp = requests.request(method, url, data=body, timeout=timeout)
        return resp.status_code, resp.content
    except requests.Timeout as exc:
        raise HttpTimeout(str(exc)) from exc
    except requests.RequestException as exc:
        raise OSError(str(exc)) from exc


def _urllib_http(method, url, body, timeout):
    import socket
    import urllib.error
    import urllib.request

    data = body.encode() if isinstance(body, str) else body
    request = urllib.request.Request(url, data=data, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except socket.timeout as exc:
        raise HttpTimeout(str(exc)) from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, socket.timeout) or isinstance(
            exc.reason, TimeoutError
        ):
            raise HttpTimeout(str(exc)) from exc
        raise OSError(str(exc)) from exc
    except TimeoutError as exc:
        raise HttpTimeout(str(exc)) from exc


def default_http():
    try:
        import requests  # noqa: F401

        return _requests_http
    except ImportError:
        return _urllib_http


@dataclass
class DeviceDetails:
    mac: str | None
    model: str | None
    device_name: str | None
    firmware: str | None
    raw: dict


@dataclass
class ImagePullStatus:
    enabled: bool
    width: int | None = None
    priority: str | None = None
    frequency: int | None = None


def _first_of(data: dict, *keys):
    for key in keys:
        for candidate in (key, key.lower(), key.upper()):
            if candidate in data and data[candidate] not in (None, ""):
                return data[candidate]
    return None


class JustApiClient:
    def __init__(
        self, ip: str, *, timeout: float = DEFAULT_TIMEOUT, http=None, sleep=time.sleep
    ):
        self.ip = ip
        self.timeout = timeout
        self._http = http or default_http()
        self._sleep = sleep

    def _url(self, path: str) -> str:
        return f"http://{self.ip}{path}"

    def _get_json(self, path: str):
        try:
            status, body = self._http("GET", self._url(path), None, self.timeout)
        except (HttpTimeout, OSError) as exc:
            logger.debug("justAPI GET %s%s failed: %s", self.ip, path, exc)
            return None
        if status != 200:
            return None
        try:
            return json.loads(body.decode("utf-8", errors="replace"))
        except (ValueError, AttributeError):
            logger.debug("justAPI GET %s%s returned non-JSON", self.ip, path)
            return None

    def _post(self, path: str, body: str, timeout: float | None = None) -> bool:
        try:
            status, _ = self._http(
                "POST", self._url(path), body, timeout or self.timeout
            )
        except (HttpTimeout, OSError) as exc:
            logger.debug("justAPI POST %s%s failed: %s", self.ip, path, exc)
            return False
        return 200 <= status < 300

    # -- identity / liveness ---------------------------------------------------

    def get_details(self) -> DeviceDetails | None:
        """Fetch device identity. Live-verified B2.3.9 shape (nested):

            {"data": {"hostname": "JustAddPower-TX6C32DB", "model": "3G TX",
                      "mode": "Transmitter",
                      "network": {"mac": "C2:00:00:6C:32:DB", "ipaddress": ...},
                      "firmware": {"version": "B2.3.9", ...}, ...}}

        Flat fallbacks are kept for other firmware generations."""
        for path in DETAILS_PATHS:
            payload = self._get_json(path)
            if payload is None:
                continue
            data = payload.get("data", payload) if isinstance(payload, dict) else None
            if not isinstance(data, dict):
                continue
            network = (
                data.get("network") if isinstance(data.get("network"), dict) else {}
            )
            mac = network.get("mac") or _first_of(
                data, "mac", "macaddress", "mac_address"
            )
            if mac is not None:
                try:
                    mac = normalize_mac(str(mac))
                except ValueError:
                    mac = None
            name = _first_of(data, "name", "hostname", "devicename", "web_name")
            model = _first_of(data, "model", "product", "device")
            firmware_field = data.get("firmware")
            if isinstance(firmware_field, dict):
                firmware = firmware_field.get("version")
            else:
                firmware = _first_of(
                    data, "firmware", "version", "fw_version", "sw_version"
                )
            return DeviceDetails(
                mac=mac,
                model=str(model) if model is not None else None,
                device_name=str(name) if name is not None else None,
                firmware=str(firmware) if firmware is not None else None,
                raw=data,
            )
        return None

    def is_online(self) -> bool:
        """Liveness: details endpoint, falling back to the (legacy-verified)
        imagepull settings endpoint for firmware without /details."""
        if self.get_details() is not None:
            return True
        return self._get_json("/cgi-bin/api/settings/imagepull") is not None

    # -- image pull --------------------------------------------------------------

    def get_image_pull(self) -> ImagePullStatus | None:
        payload = self._get_json("/cgi-bin/api/settings/imagepull")
        if not isinstance(payload, dict) or "data" not in payload:
            return None
        data = payload["data"]
        if isinstance(data, dict):
            try:
                width = (
                    int(data.get("width")) if data.get("width") is not None else None
                )
            except (TypeError, ValueError):
                width = None
            try:
                frequency = (
                    int(data.get("frequency"))
                    if data.get("frequency") is not None
                    else None
                )
            except (TypeError, ValueError):
                frequency = None
            return ImagePullStatus(
                enabled=True,
                width=width,
                priority=data.get("priority"),
                frequency=frequency,
            )
        return ImagePullStatus(enabled=bool(data))

    def enable_image_pull(self, width=320, priority="low", frequency=3) -> bool:
        body = json.dumps(
            {
                "width": str(width),
                "priority": str(priority),
                "frequency": str(frequency),
            }
        )
        if not self._post("/cgi-bin/api/settings/imagepull", body):
            return False
        self._sleep(REST_WAIT_SECS)
        if not self.save():
            return False
        self._sleep(REST_WAIT_SECS)
        return self.reboot()

    def disable_image_pull(self) -> bool:
        if not self._post("/cgi-bin/api/settings/imagepull", "null"):
            return False
        self._sleep(REST_WAIT_SECS)
        if not self.save():
            return False
        self._sleep(REST_WAIT_SECS)
        return self.reboot()

    # -- device commands ---------------------------------------------------------

    def save(self) -> bool:
        return self._post("/cgi-bin/api/command/device", "save")

    def reboot(self) -> bool:
        """Reboot the device. It drops the connection before responding, so a
        timeout is treated as success (legacy-verified behavior)."""
        try:
            status, _ = self._http(
                "POST",
                self._url("/cgi-bin/api/command/device"),
                "reboot",
                REBOOT_TIMEOUT,
            )
        except HttpTimeout:
            return True
        except OSError as exc:
            logger.debug("justAPI reboot %s failed: %s", self.ip, exc)
            return False
        return 200 <= status < 300

    # -- AMP / JPSW (experimental) -------------------------------------------------

    def set_channel(self, channel: int) -> bool:
        """AMP-standardized channel switch (JPSW). Documented for Ultra/AMP
        systems; availability on 3G B firmware is unverified — best effort."""
        return self._post("/cgi-bin/api/command/channel", str(channel))

    def get_channel(self) -> int | None:
        payload = self._get_json("/cgi-bin/api/details/channel")
        if not isinstance(payload, dict):
            return None
        data = payload.get("data", payload)
        try:
            return int(data)
        except (TypeError, ValueError):
            return None

    # -- snapshots -------------------------------------------------------------------

    def image_pull_urls(self) -> list:
        """Candidate snapshot URLs, most likely first. The production system's
        legacy variables confirm the portless form."""
        return [
            f"http://{self.ip}/pull.bmp",
            f"http://{self.ip}:8080/pull.bmp",
        ]

    def fetch_image(self):
        """Returns (url, bytes) for the first candidate URL that answers, or None."""
        for url in self.image_pull_urls():
            try:
                status, body = self._http("GET", url, None, IMAGE_TIMEOUT)
            except (HttpTimeout, OSError):
                continue
            if status == 200 and body:
                return url, body
        return None
