"""Just Add Power — Indigo plugin entry point.

Thin adapter: device lifecycle, action/menu callbacks, and the concurrent
thread. All protocol logic lives in the jap package. Action and menu
callbacks only enqueue jobs; every CLI/HTTP operation runs on the concurrent
thread, which drains a FIFO job queue and fires two poll cadences (routing
and device health).
"""

try:
    import indigo
except ImportError:
    pass

import datetime
import logging
import os
import queue
import re
import time

from jap.backends.amp_jpsw import AmpJpswBackend
from jap.backends.base import BackendError
from jap.backends.jadconfig_cisco import JadConfigCiscoBackend
from jap.cisco_cli import CiscoCliClient, CiscoCliError
from jap.config import PluginSettings, TopologyStore, validate_prefs
from jap.discovery import DiscoveryError, run_discovery
from jap.justapi import JustApiClient
from jap.topology import PendingSwitchTracker, RoutingState

DEVICE_FOLDER_NAME = "JAP Devices"

RECONNECT_BACKOFF_SECS = (5, 10, 20, 40, 60)
DEVICE_POLL_WORKERS = 8


def _now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Plugin(indigo.PluginBase):
    def __init__(
        self, plugin_id, plugin_display_name, plugin_version, plugin_prefs, **kwargs
    ):
        super().__init__(
            plugin_id, plugin_display_name, plugin_version, plugin_prefs, **kwargs
        )
        self.settings = PluginSettings.from_prefs(plugin_prefs)
        self.indigo_log_handler.setLevel(self.settings.log_level)
        self.plugin_file_handler.setLevel(logging.DEBUG)

        prefs_dir = self.plugin_file_handler.baseFilename.replace(
            "Logs", "Preferences"
        ).replace("/plugin.log", "")
        self._prefs_dir = prefs_dir
        self._store = TopologyStore(os.path.join(prefs_dir, "topology.json"))

        self._topology = None
        self._cli = None
        self._backend = None
        self._work_q = queue.Queue()
        self._pending = PendingSwitchTracker()
        self._routing = None  # last RoutingState applied to Indigo
        self._routing_fail_count = 0
        self._backoff_index = 0
        self._next_routing_poll = 0.0
        self._next_device_poll = 0.0
        self._folder_id = 0

    ########################################
    # Lifecycle
    ########################################

    def startup(self):
        self.logger.info("Just Add Power plugin starting")
        self._topology = self._store.load()
        self._build_backend()
        self._ensure_folder()
        if self._topology is not None:
            self._sync_indigo_devices()
            self.logger.info(
                "Loaded stored topology: %d TX, %d RX",
                len(self._topology.tx_devices()),
                len(self._topology.rx_devices()),
            )
        else:
            self.logger.info("No stored topology — queueing initial discovery")
            self._work_q.put(("rediscover",))

    def shutdown(self):
        if self._cli is not None:
            self._cli.close()

    def _build_backend(self):
        if self._cli is not None:
            self._cli.close()
        self._cli = CiscoCliClient(
            self.settings.switch_ip,
            username=self.settings.username or None,
            password=self.settings.password or None,
        )
        mode = self._topology.mode if self._topology else "jadconfig"
        if mode == "amp":
            self._backend = AmpJpswBackend(self.settings, lambda: self._topology)
            self.logger.warning(
                "AMP-standardized mode active — EXPERIMENTAL, best effort"
            )
        else:
            self._backend = JadConfigCiscoBackend(
                self._cli, self.settings, lambda: self._topology
            )

    def runConcurrentThread(self):
        try:
            while True:
                self._drain_work_queue()
                now = time.monotonic()
                if now >= self._next_routing_poll:
                    self._job_routing_poll()
                    self._schedule_next_routing_poll()
                if now >= self._next_device_poll:
                    self._job_device_poll()
                    self._next_device_poll = (
                        time.monotonic() + self.settings.device_poll_secs
                    )
                self.sleep(0.5)
        except self.StopThread:
            pass

    def _schedule_next_routing_poll(self):
        if self._routing_fail_count > 0:
            delay = RECONNECT_BACKOFF_SECS[
                min(self._backoff_index, len(RECONNECT_BACKOFF_SECS) - 1)
            ]
            self._backoff_index += 1
        else:
            self._backoff_index = 0
            delay = self.settings.routing_poll_secs
        self._next_routing_poll = time.monotonic() + delay

    def _drain_work_queue(self):
        while True:
            try:
                job = self._work_q.get_nowait()
            except queue.Empty:
                return
            try:
                self._execute_job(job)
            except (BackendError, CiscoCliError, DiscoveryError) as exc:
                self.logger.error("%s failed: %s", job[0], exc)
            except Exception:
                self.logger.exception("Unexpected error in job %r", job)

    def _execute_job(self, job):
        kind = job[0]
        if kind == "switch":
            self._job_switch(job[1], job[2])
        elif kind == "switch_all":
            self._job_switch_all(job[1])
        elif kind == "refresh_routing":
            self._job_routing_poll()
            self._schedule_next_routing_poll()
        elif kind == "rediscover":
            self._job_rediscover()
        elif kind == "validate":
            self._job_validate()
        elif kind == "image_pull":
            self._job_image_pull(job[1])
        elif kind == "reboot_device":
            self._job_reboot_device(job[1])
        elif kind == "reboot_switch":
            self._job_reboot_switch()
        elif kind == "image_pull_config":
            self._job_image_pull_config(job[1], job[2], job[3])
        else:
            self.logger.error("Unknown job kind %r", kind)

    ########################################
    # Indigo device sync (auto-create / adopt)
    ########################################

    def _ensure_folder(self):
        try:
            if DEVICE_FOLDER_NAME in indigo.devices.folders:
                self._folder_id = indigo.devices.folders[DEVICE_FOLDER_NAME].id
            else:
                self._folder_id = indigo.devices.folder.create(DEVICE_FOLDER_NAME).id
        except Exception:
            self.logger.exception(
                "Could not ensure the '%s' folder", DEVICE_FOLDER_NAME
            )
            self._folder_id = 0

    def _plugin_devices_by_address(self):
        devices = {}
        for dev in indigo.devices.iter("self"):
            address = dev.address or dev.pluginProps.get("address", "")
            if not address:
                continue
            if address in devices:
                keep, other = sorted([devices[address], dev], key=lambda d: d.id)
                devices[address] = keep
                self.logger.warning(
                    "Duplicate devices for %s: adopting '%s' (%d); '%s' (%d) is unmanaged",
                    address,
                    keep.name,
                    keep.id,
                    other.name,
                    other.id,
                )
            else:
                devices[address] = dev
        return devices

    def _desired_props(self, jap_dev):
        return {
            "address": jap_dev.key,
            "jap_key": jap_dev.key,
            "jap_role": jap_dev.role,
            "jap_mac": jap_dev.mac or "",
            "jap_ip": jap_dev.ip or "",
            "jap_port": jap_dev.port.name if jap_dev.port else "",
            "jap_vlan": str(jap_dev.vlan) if jap_dev.vlan is not None else "",
        }

    def _default_name(self, jap_dev):
        if jap_dev.role == "tx":
            n = ""
            if jap_dev.vlan is not None:
                n = str(jap_dev.vlan - self.settings.all_devices_vlan)
            base = f"JAP Tx {n}".strip()
        else:
            port = jap_dev.port.name if jap_dev.port else "?"
            base = f"JAP Rx {port}"
        if jap_dev.device_name:
            return f"{base} - {jap_dev.device_name}"
        return base

    def _sync_indigo_devices(self):
        if self._topology is None:
            return
        existing = self._plugin_devices_by_address()

        # The switch device.
        switch_key = f"switch:{self.settings.switch_ip}"
        switch_dev = existing.get(switch_key)
        if switch_dev is None:
            switch_dev = self._create_device(
                switch_key,
                "japSwitch",
                f"JAP Switch ({self.settings.switch_ip})",
                {"address": switch_key},
            )
        if switch_dev is not None:
            self._update_states(
                switch_dev,
                {"mode": self._topology.mode, "model": self._topology.model or ""},
            )

        for jap_dev in self._topology.devices:
            try:
                key = jap_dev.key
            except ValueError:
                continue
            dev = existing.get(key)
            if dev is None and jap_dev.mac and jap_dev.port:
                # Key upgrade: a device previously keyed by port revealed its MAC.
                dev = existing.get(f"port:{jap_dev.port.name}")
                if dev is not None:
                    self.logger.info(
                        "Re-keying '%s' from port:%s to %s",
                        dev.name,
                        jap_dev.port.name,
                        key,
                    )
            if dev is None:
                if jap_dev.ignored:
                    continue
                type_id = "japTransmitter" if jap_dev.role == "tx" else "japReceiver"
                dev = self._create_device(
                    key,
                    type_id,
                    self._default_name(jap_dev),
                    self._desired_props(jap_dev),
                )
                if dev is None:
                    continue
            else:
                desired = self._desired_props(jap_dev)
                current = {k: dev.pluginProps.get(k, "") for k in desired}
                current["ignore"] = dev.pluginProps.get("ignore", False)
                desired["ignore"] = dev.pluginProps.get("ignore", False)
                if current != desired:
                    props = dict(dev.pluginProps)
                    props.update(desired)
                    dev.replacePluginPropsOnServer(props)
            self._push_device_states(dev, jap_dev)

    def _create_device(self, address, type_id, name, props):
        try:
            dev = indigo.device.create(
                protocol=indigo.kProtocol.Plugin,
                address=address,
                name=self._unique_name(name),
                deviceTypeId=type_id,
                props=props,
                folder=self._folder_id,
            )
            self.logger.info("Created Indigo device '%s' (%s)", dev.name, address)
            return dev
        except Exception:
            self.logger.exception("Could not create Indigo device for %s", address)
            return None

    @staticmethod
    def _unique_name(name):
        existing_names = {d.name for d in indigo.devices}
        if name not in existing_names:
            return name
        for i in range(2, 100):
            candidate = f"{name} ({i})"
            if candidate not in existing_names:
                return candidate
        return f"{name} ({time.time():.0f})"

    def _update_states(self, dev, updates):
        changed = []
        for key, value in updates.items():
            if dev.states.get(key) != value:
                changed.append({"key": key, "value": value})
        if changed:
            dev.updateStatesOnServer(changed)

    def _push_device_states(self, dev, jap_dev):
        updates = {
            "deviceName": jap_dev.device_name or "",
            "switchPort": jap_dev.port.name if jap_dev.port else "",
            "firmwareVersion": jap_dev.firmware or "",
        }
        if jap_dev.ip:
            updates["imagePullUrl"] = JustApiClient(jap_dev.ip).image_pull_urls()[0]
        if jap_dev.role == "tx":
            updates["vlanId"] = jap_dev.vlan or 0
        self._update_states(dev, updates)

    def _find_indigo_device(self, key):
        for dev in indigo.devices.iter("self"):
            if (dev.address or dev.pluginProps.get("address", "")) == key:
                return dev
        return None

    def _switch_indigo_device(self):
        return self._find_indigo_device(f"switch:{self.settings.switch_ip}")

    ########################################
    # Jobs (run on the concurrent thread)
    ########################################

    def _set_switch_online(self, online):
        dev = self._switch_indigo_device()
        if dev is not None:
            updates = {"online": online}
            if online:
                updates["lastStateSync"] = _now_str()
            self._update_states(dev, updates)

    def _tx_display_name(self, vlan):
        """Best display name for a TX VLAN: the Indigo device name, else the
        device's own name, else 'VLAN <n>'."""
        if vlan is None:
            return "none"
        if self._topology is not None:
            tx = self._topology.tx_by_vlan(vlan)
            if tx is not None:
                dev = self._find_indigo_device(tx.key)
                if dev is not None:
                    return dev.name
                if tx.device_name:
                    return tx.device_name
        return f"VLAN {vlan}"

    def _apply_routing_to_devices(self, routing):
        """Push a routing view onto RX and TX device states."""
        if self._topology is None:
            return
        watched_counts = {}
        for rx in self._topology.rx_devices():
            if rx.port is None:
                continue
            vlan = routing.rx_source.get(rx.port.name)
            if vlan is not None:
                watched_counts[vlan] = watched_counts.get(vlan, 0) + 1
            dev = self._find_indigo_device(rx.key)
            if dev is None:
                continue
            self._update_states(
                dev,
                {
                    "currentSource": self._tx_display_name(vlan),
                    "currentSourceVlan": vlan or 0,
                },
            )
        for tx in self._topology.tx_devices():
            dev = self._find_indigo_device(tx.key)
            if dev is not None:
                self._update_states(
                    dev, {"watchedByCount": watched_counts.get(tx.vlan, 0)}
                )

    def _mark_routing_unknown(self):
        if self._topology is None:
            return
        for rx in self._topology.rx_devices():
            dev = self._find_indigo_device(rx.key)
            if dev is not None:
                self._update_states(dev, {"currentSource": "unknown"})

    def _job_routing_poll(self):
        if self._backend is None:
            return
        try:
            observed = self._backend.get_routing_state()
        except (BackendError, CiscoCliError) as exc:
            self._routing_fail_count += 1
            self._set_switch_online(False)
            if self._routing_fail_count == 1:
                self.logger.error("Routing poll failed: %s", exc)
            else:
                self.logger.debug(
                    "Routing poll failed (%d consecutive): %s",
                    self._routing_fail_count,
                    exc,
                )
            if self._routing_fail_count == 2:
                self.logger.error(
                    "Two consecutive routing polls failed — marking receiver sources unknown"
                )
                self._mark_routing_unknown()
                self._routing = None
            return

        self._routing_fail_count = 0
        self._set_switch_online(True)

        result = self._pending.reconcile(observed)
        for rx_port, vlan in result.confirmed:
            self.logger.debug("Confirmed switch of %s to VLAN %s", rx_port, vlan)
        for rx_port, expected, actual in result.reverted:
            self.logger.error(
                "Switch of %s to VLAN %s was not confirmed after 2 polls "
                "(switch reports %s) — reverting to observed state",
                rx_port,
                expected,
                actual if actual is not None else "no source",
            )

        # Apply the observed state, but keep the optimistic value for ports
        # still awaiting confirmation.
        effective = dict(observed.rx_source)
        if self._routing is not None:
            for rx_port in result.pending:
                if rx_port in self._routing.rx_source:
                    effective[rx_port] = self._routing.rx_source[rx_port]
        effective_state = RoutingState(
            rx_source=effective, captured_at=observed.captured_at
        )

        if self._routing is not None:
            for change in self._routing.diff(effective_state):
                self.logger.info(
                    "%s is now watching %s",
                    self._rx_display_name(change.rx_port),
                    self._tx_display_name(change.new_vlan),
                )
        self._apply_routing_to_devices(effective_state)
        self._routing = effective_state

    def _rx_display_name(self, rx_port):
        if self._topology is not None:
            rx = self._topology.find_by_port(rx_port)
            if rx is not None:
                dev = self._find_indigo_device(rx.key)
                if dev is not None:
                    return dev.name
        return rx_port

    def _job_device_poll(self):
        if self._topology is None:
            return
        from concurrent.futures import ThreadPoolExecutor

        targets = [d for d in self._topology.devices if d.ip and not d.ignored]
        if not targets:
            return

        def poll(jap_dev):
            return jap_dev, JustApiClient(jap_dev.ip).get_details()

        with ThreadPoolExecutor(max_workers=DEVICE_POLL_WORKERS) as pool:
            results = list(pool.map(poll, targets))

        for jap_dev, details in results:
            dev = self._find_indigo_device(jap_dev.key)
            if dev is None:
                continue
            updates = {"online": details is not None}
            if details is not None:
                if details.device_name:
                    updates["deviceName"] = details.device_name
                    jap_dev.device_name = details.device_name
                if details.firmware:
                    updates["firmwareVersion"] = details.firmware
                    jap_dev.firmware = details.firmware
            self._update_states(dev, updates)

        # Devices with no IP at all can never be online.
        for jap_dev in self._topology.devices:
            if jap_dev.ip or jap_dev.ignored:
                continue
            dev = self._find_indigo_device(jap_dev.key)
            if dev is not None:
                self._update_states(dev, {"online": False})

    def _resolve_switch_target(self, rx_key, tx_vlan):
        if self._topology is None:
            raise BackendError("No topology yet — run 'Rediscover System' first")
        rx = self._topology.find_by_key(rx_key)
        if rx is None:
            raise BackendError(f"Receiver {rx_key} is not in the topology")
        tx = self._topology.tx_by_vlan(tx_vlan)
        if tx is None:
            raise BackendError(f"No transmitter with VLAN {tx_vlan} in the topology")
        return rx, tx

    def _job_switch(self, rx_key, tx_vlan):
        rx, tx = self._resolve_switch_target(rx_key, tx_vlan)
        self._backend.switch(rx, tx)
        self.logger.info(
            "Switched %s to %s",
            self._rx_display_name(rx.port.name),
            self._tx_display_name(tx_vlan),
        )
        # Optimistic update; confirmed (or reverted) by the routing poll.
        self._pending.record(rx.port.name, tx_vlan)
        if self._routing is not None:
            self._routing.rx_source[rx.port.name] = tx_vlan
        dev = self._find_indigo_device(rx.key)
        if dev is not None:
            self._update_states(
                dev,
                {
                    "currentSource": self._tx_display_name(tx_vlan),
                    "currentSourceVlan": tx_vlan,
                },
            )

    def _job_switch_all(self, tx_vlan):
        if self._topology is None:
            raise BackendError("No topology yet — run 'Rediscover System' first")
        tx = self._topology.tx_by_vlan(tx_vlan)
        if tx is None:
            raise BackendError(f"No transmitter with VLAN {tx_vlan} in the topology")
        self._backend.switch_all(tx)
        self.logger.info("Switched all receivers to %s", self._tx_display_name(tx_vlan))
        for rx in self._topology.rx_devices():
            if rx.port is None or rx.ignored:
                continue
            self._pending.record(rx.port.name, tx_vlan)
            if self._routing is not None:
                self._routing.rx_source[rx.port.name] = tx_vlan
            dev = self._find_indigo_device(rx.key)
            if dev is not None:
                self._update_states(
                    dev,
                    {
                        "currentSource": self._tx_display_name(tx_vlan),
                        "currentSourceVlan": tx_vlan,
                    },
                )

    def _job_rediscover(self):
        self.logger.info("Starting system discovery...")
        fresh, warnings = run_discovery(self._cli, self.settings, self._topology)
        for warning in warnings:
            self.logger.warning("Discovery: %s", warning)
        merged = TopologyStore.merge(self._topology, fresh)
        mode_changed = self._topology is not None and self._topology.mode != merged.mode
        self._topology = merged
        self._store.save(merged)
        if mode_changed or self._backend is None:
            self._build_backend()
        self._sync_indigo_devices()
        self.logger.info(
            "Discovery complete: %d TX, %d RX — topology saved to %s",
            len(merged.tx_devices()),
            len(merged.rx_devices()),
            self._store.path,
        )
        # Refresh routing immediately with the new topology.
        self._job_routing_poll()
        self._schedule_next_routing_poll()

    def _job_validate(self):
        self.logger.info("=== Validate System ===")
        issues = self._backend.validate() if self._backend else []
        for issue in issues:
            self.logger.info("  %s %s", issue.symbol, issue.message)
        if self._topology is not None:
            for jap_dev in self._topology.devices:
                if jap_dev.ignored:
                    continue
                label = f"{jap_dev.role.upper()} {jap_dev.port.name if jap_dev.port else jap_dev.key}"
                if not jap_dev.ip:
                    self.logger.info(
                        "  ⚠ %s: no IP address discovered (device off or unreachable)",
                        label,
                    )
                    continue
                client = JustApiClient(jap_dev.ip)
                if not client.is_online():
                    self.logger.info("  ✗ %s (%s): offline", label, jap_dev.ip)
                    continue
                status = client.get_image_pull()
                if status is None:
                    self.logger.info(
                        "  ✓ %s (%s): online (image pull status unavailable)",
                        label,
                        jap_dev.ip,
                    )
                elif status.enabled:
                    self.logger.info(
                        "  ✓ %s (%s): online, image pull enabled", label, jap_dev.ip
                    )
                else:
                    self.logger.info(
                        "  ⚠ %s (%s): online, image pull DISABLED", label, jap_dev.ip
                    )
        self.logger.info("=== End Validate System ===")

    def _snapshot_dir(self):
        directory = self.settings.snapshot_dir or os.path.join(
            self._prefs_dir, "snapshots"
        )
        os.makedirs(directory, exist_ok=True)
        return directory

    def _job_image_pull(self, dev_key):
        if self._topology is None:
            return
        if dev_key:
            targets = [d for d in self._topology.devices if d.key == dev_key]
        else:
            targets = [d for d in self._topology.devices if d.ip and not d.ignored]
        for jap_dev in targets:
            if not jap_dev.ip:
                self.logger.warning("Image pull: %s has no IP address", jap_dev.key)
                continue
            result = JustApiClient(jap_dev.ip).fetch_image()
            dev = self._find_indigo_device(jap_dev.key)
            if result is None:
                self.logger.warning(
                    "Image pull from %s failed (is image pull enabled on the device?)",
                    jap_dev.ip,
                )
                continue
            url, data = result
            if dev is not None:
                self._update_states(dev, {"imagePullUrl": url})
            if self.settings.snapshots_enabled:
                name = dev.name if dev is not None else jap_dev.key
                safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
                path = os.path.join(self._snapshot_dir(), f"{safe}.bmp")
                path = self._maybe_convert_to_jpeg(data, path)
                self.logger.info("Saved snapshot for %s to %s", name, path)

    def _maybe_convert_to_jpeg(self, data, bmp_path):
        """Write the snapshot; convert BMP->JPEG only if Pillow is available."""
        try:
            from PIL import Image  # noqa: PLC0415
            import io

            jpeg_path = bmp_path.rsplit(".", 1)[0] + ".jpg"
            Image.open(io.BytesIO(data)).convert("RGB").save(jpeg_path, "JPEG")
            return jpeg_path
        except ImportError:
            if not getattr(self, "_warned_no_pillow", False):
                self._warned_no_pillow = True
                self.logger.info("Pillow not available — saving snapshots as BMP")
            with open(bmp_path, "wb") as f:
                f.write(data)
            return bmp_path
        except Exception:
            with open(bmp_path, "wb") as f:
                f.write(data)
            return bmp_path

    def _job_reboot_device(self, dev_key):
        jap_dev = self._topology.find_by_key(dev_key) if self._topology else None
        if jap_dev is None or not jap_dev.ip:
            self.logger.error(
                "Cannot reboot %s: unknown device or no IP address", dev_key
            )
            return
        if JustApiClient(jap_dev.ip).reboot():
            self.logger.info("Reboot sent to %s (%s)", dev_key, jap_dev.ip)
        else:
            self.logger.error("Reboot of %s (%s) failed", dev_key, jap_dev.ip)

    def _job_reboot_switch(self):
        if not isinstance(self._backend, JadConfigCiscoBackend):
            self.logger.error("Reboot Switch is only supported in JADConfig mode")
            return
        self._backend.reboot_switch()
        self.logger.info(
            "Switch reload sent — the switch will be offline for a few minutes"
        )

    def _job_image_pull_config(self, dev_key, enable, params):
        jap_dev = self._topology.find_by_key(dev_key) if self._topology else None
        if jap_dev is None or not jap_dev.ip:
            self.logger.error(
                "Cannot configure image pull for %s: no IP address", dev_key
            )
            return
        client = JustApiClient(jap_dev.ip)
        if enable:
            ok = client.enable_image_pull(**params)
        else:
            ok = client.disable_image_pull()
        if ok:
            self.logger.info(
                "Image pull %s on %s — the device is rebooting to apply it",
                "enabled" if enable else "disabled",
                jap_dev.ip,
            )
        else:
            self.logger.error("Image pull configuration on %s failed", jap_dev.ip)

    ########################################
    # Actions (enqueue and return)
    ########################################

    def action_switch_source(self, action, dev):
        try:
            tx_vlan = int(action.props.get("tx_vlan"))
        except (TypeError, ValueError):
            self.logger.error("Switch Source: no transmitter selected")
            return
        self._work_q.put(("switch", dev.address, tx_vlan))

    def action_switch_all(self, action):
        try:
            tx_vlan = int(action.props.get("tx_vlan"))
        except (TypeError, ValueError):
            self.logger.error("Switch All Receivers: no transmitter selected")
            return
        self._work_q.put(("switch_all", tx_vlan))

    def action_refresh_routing(self, action):
        self._work_q.put(("refresh_routing",))

    def action_refresh_image_pull(self, action, dev):
        self._work_q.put(("image_pull", dev.address))

    def action_reboot_device(self, action, dev):
        self._work_q.put(("reboot_device", dev.address))

    def action_reboot_switch(self, action, dev):
        self._work_q.put(("reboot_switch",))

    def action_enable_image_pull(self, action, dev):
        params = {
            "width": action.props.get("width", "320") or "320",
            "priority": action.props.get("priority", "low") or "low",
            "frequency": action.props.get("frequency", "3") or "3",
        }
        self._work_q.put(("image_pull_config", dev.address, True, params))

    def action_disable_image_pull(self, action, dev):
        self._work_q.put(("image_pull_config", dev.address, False, {}))

    ########################################
    # Dynamic lists
    ########################################

    def tx_source_list(self, filter="", values_dict=None, type_id="", target_id=0):
        items = []
        if self._topology is not None:
            for tx in sorted(self._topology.tx_devices(), key=lambda d: d.vlan or 0):
                if tx.ignored or tx.vlan is None:
                    continue
                items.append((str(tx.vlan), self._tx_display_name(tx.vlan)))
        return items

    ########################################
    # Menu items
    ########################################

    def menu_rediscover(self):
        self.logger.info("Rediscover System queued")
        self._work_q.put(("rediscover",))

    def menu_validate_system(self):
        self.logger.info("Validate System queued")
        self._work_q.put(("validate",))

    def menu_print_matrix(self):
        if self._topology is None:
            self.logger.info("No topology yet — run 'Rediscover System' first")
            return
        routing = self._routing
        self.logger.info("=== JAP Routing Matrix ===")
        for rx in sorted(
            self._topology.rx_devices(), key=lambda d: d.port.name if d.port else ""
        ):
            if rx.port is None:
                continue
            marker = " (not in use)" if rx.ignored else ""
            vlan = routing.rx_source.get(rx.port.name) if routing else None
            self.logger.info(
                "  %-24s -> %s%s",
                self._rx_display_name(rx.port.name),
                self._tx_display_name(vlan) if routing else "unknown (no poll yet)",
                marker,
            )
        self.logger.info("=== End Routing Matrix ===")

    ########################################
    # Config UIs / device lifecycle
    ########################################

    def validatePrefsConfigUi(self, values_dict):
        ok, values, errors = validate_prefs(values_dict)
        if not ok:
            return (False, values, errors)
        return (True, values)

    def closedPrefsConfigUi(self, values_dict, user_cancelled):
        if user_cancelled:
            return
        old = self.settings
        self.settings = PluginSettings.from_prefs(values_dict)
        self.indigo_log_handler.setLevel(self.settings.log_level)
        if (
            old.switch_ip != self.settings.switch_ip
            or old.username != self.settings.username
            or old.password != self.settings.password
        ):
            self.logger.info("Switch connection settings changed — reconnecting")
            self._build_backend()
            self._routing_fail_count = 0
        # Re-poll promptly with the new settings.
        self._next_routing_poll = 0.0
        self._next_device_poll = 0.0

    def deviceStartComm(self, dev):
        # Mirror the user's "not in use" checkbox into the topology so
        # rediscovery honors it.
        if self._topology is None:
            return
        address = dev.address or dev.pluginProps.get("address", "")
        jap_dev = self._topology.find_by_key(address)
        if jap_dev is not None:
            ignored = bool(dev.pluginProps.get("ignore", False))
            if jap_dev.ignored != ignored:
                jap_dev.ignored = ignored
                self._store.save(self._topology)
                self.logger.info(
                    "%s is now %s", dev.name, "not in use" if ignored else "in use"
                )
            self._push_device_states(dev, jap_dev)

    def deviceStopComm(self, dev):
        pass
