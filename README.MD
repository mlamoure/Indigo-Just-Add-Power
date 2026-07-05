# Just Add Power — Indigo Plugin

An [Indigo](https://www.indigodomo.com) plugin for [Just Add Power](https://justaddpower.com) HDMI-over-IP AV systems: switch any Receiver (RX) to any Transmitter (TX), monitor endpoint health, and pull video snapshots.

This is a ground-up rewrite of the [original plugin](https://github.com/mlamoure/Indigo-Just-Add-Power) (2017–2018) for Indigo 2025.1+ and Python 3.

## How it works

In a Just Add Power VLAN-switching system, source selection is not performed on the J+P devices themselves. Each TX lives in its own VLAN, and an RX watches whichever TX it shares a VLAN with. The plugin switches sources by changing the RX port's VLAN membership on the managed switch via its telnet CLI, and talks to each device's justOS HTTP API (justAPI) for identity, health, and image pull.

The plugin has two control planes:

1. **Switch plane** — a built-in Cisco SG300-family CLI client (raw-socket telnet, no dependencies) performs matrix switching and reads routing state.
2. **Device plane** — a justAPI HTTP client talks to each TX/RX for identity, reachability, and snapshots.

## Requirements

- Indigo 2025.1 or later
- Just Add Power **3G-series** devices on **justOS Bx.x.x** firmware
- A Cisco **SG300-family** switch (SG300/SG350/Sx500 share the CLI; developed and tested against an SG300-28PP) configured with **JADConfig**
- Telnet enabled on the switch (`ip telnet server`) and reachable from the Indigo server
- A static route (or equivalent) so the Indigo server can reach the J+P device subnets over HTTP

AMP-standardized systems are detected and supported **experimentally** (switching via JPSW device commands); JADConfig is the production-quality path.

## Setup

1. Install the plugin and open **Plugins → Just Add Power → Configure…**
2. Enter the switch IP address and telnet credentials. The VLAN defaults (all-devices VLAN 10, TX VLAN range 11-410) match a standard JADConfig install.
3. The plugin discovers the system automatically on first start (or run **Plugins → Just Add Power → Rediscover System**): it reads the switch config, classifies TX/RX ports, correlates devices to ports via the switch's MAC address table, probes the J+P subnets over HTTP, and **auto-creates** Indigo devices in a "JAP Devices" folder.
4. Rename the created devices freely — they are identified by MAC address (or switch port for powered-off devices), never by name.

### Devices

| Type | Display state | Notable states |
|---|---|---|
| JAP Receiver | `currentSource` (the watched TX) | `currentSourceVlan`, `online`, `switchPort`, `firmwareVersion`, `imagePullUrl` |
| JAP Transmitter | `watchedByCount` | `vlanId`, `online`, `switchPort`, `firmwareVersion`, `imagePullUrl` |
| JAP Switch | `online` | `mode` (jadconfig/amp), `model`, `lastStateSync` |

Routing state is polled (default 30s) and reflects changes made by **any** control system — the J+P web UI, Roomie, or a manual CLI session — so `currentSource` triggers work regardless of who switched. Device health is polled separately (default 60s).

### Actions

- **Switch Source** (on a receiver) — point the RX at any TX, chosen from a live menu.
- **Switch All Receivers** — point every in-use RX at one TX.
- **Refresh Routing State** — poll now.
- **Refresh Image Pull** — fetch a snapshot; optionally saved to disk (BMP→JPEG when Pillow is available) for Control Pages.
- **Reboot JAP Device** / **Reboot Switch** — restart an endpoint or the Cisco switch.
- **Enable / Disable Image Pull** — configure image pull on a device (the device saves and reboots to apply).

### Menu items

- **Rediscover System** — re-run discovery. Manual topology edits (entries marked `manual` in `topology.json`) and "not in use" flags survive rediscovery.
- **Print Routing Matrix to Event Log** — RX → TX table.
- **Validate System** — health report: telnet reachability, port classification, `spanning-tree portfast` (needed for seamless switching), jumbo frames, per-device reachability and image-pull status.

## Notes and semantics

- **"Not in use" (ignore)**: check it in a device's settings to skip that endpoint in polling, source menus, Switch All, and rediscovery. To permanently drop an auto-created device, mark it "not in use" — simply deleting it will get it recreated on the next Rediscover.
- **Topology file**: discovered topology persists at `.../Preferences/Plugins/com.vtmikel.justaddpower/topology.json`. Every discovered fact can be overridden by editing an entry and setting `"manual": true`.
- **Switch password** is stored in Indigo's plugin preferences in plain text (as the legacy plugin did). JADConfig installs commonly use the factory `cisco`/`cisco` — the plugin also supports switches with CLI authentication disabled.
- The switching sequence sends `switchport general allowed vlan remove <range>` before adding the new source VLAN; the range (default 11-410) is configurable and covers all JADConfig TX VLANs so the currently-watched VLAN never needs to be known.

## Development

See `CLAUDE.md` for architecture, testing (194 pytest tests against a stubbed `indigo` module, scripted telnet transport, and real sanitized switch captures), and deploy workflow.
