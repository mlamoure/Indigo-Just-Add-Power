# Just Add Power — Indigo Plugin

An [Indigo](https://www.indigodomo.com) plugin for [Just Add Power](https://justaddpower.com) HDMI-over-IP AV systems: switch any Receiver (RX) to any Transmitter (TX), monitor endpoint health, and pull video snapshots.

This is a ground-up rewrite of the [original plugin](https://github.com/mlamoure/Indigo-Just-Add-Power) for Indigo 2025.1+ (Python 3).

## How it works

In a Just Add Power VLAN-switching system, source selection is not performed on the J+P devices themselves — each TX lives in its own VLAN, and an RX watches whichever TX it shares a VLAN with. The plugin switches sources by changing the RX port's VLAN membership on the managed switch via its telnet CLI, and talks to each device's justOS HTTP API (justAPI) for identity, health, and image pull.

## Requirements

- Indigo 2025.1 or later
- Just Add Power **3G-series** devices on **justOS Bx.x.x** firmware
- A Cisco **SG300-family** switch (SG300/SG350/Sx500 share the CLI) configured with **JADConfig**, with telnet enabled (`ip telnet server`)

## Status

Under active development — see `CLAUDE.md` for the build/test workflow.

## Documentation

Full documentation will land here as part of v1: installation, configuration, discovery, actions, and the "not in use" (ignore) semantics for auto-created devices.
