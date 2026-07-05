# CLAUDE.md — Just Add Power plugin

Repo-specific rules and workflow. Global rules in `~/.claude/CLAUDE.md` apply; this file adds plugin specifics.

## Commands

```bash
# Setup (once)
python3 -m venv .venv && source .venv/bin/activate.fish && pip install -r requirements.txt

# Tests (from repo root; conftest.py injects the Server Plugin dir + indigo stub)
source .venv/bin/activate.fish && python -m pytest

# Format (Black defaults; run before every commit)
black .
```

## Architecture

- `Just Add Power.indigoPlugin/Contents/Server Plugin/plugin.py` — thin Indigo adapter only: device lifecycle, action/menu callbacks, `runConcurrentThread` (drains a FIFO job queue + fires the routing/device poll cadences). No protocol logic.
- `…/Server Plugin/jap/` — core logic, importable without Indigo (`try: import indigo / except ImportError: pass` guard in every module; single logger `logging.getLogger("Plugin")`):
  - `cisco_cli.py` — raw-socket telnet client (stdlib only; `telnetlib` is gone in Py3.13). Injectable `Transport` for tests.
  - `running_config.py` — pure parser: running-config → interfaces/VLANs; port classification; MAC table; mode detection.
  - `topology.py` — data model + `RoutingState` diffing + `PendingSwitchTracker` (optimistic switch confirm/revert).
  - `config.py` — prefs coercion + `TopologyStore` (JSON at `…/Preferences/Plugins/com.vtmikel.justaddpower/topology.json`; merge preserves `manual: true` entries).
  - `justapi.py` — justOS HTTP client (uses `requests` if available, else `urllib`; never a hard dep).
  - `discovery.py` — running-config + MAC-table + HTTP-probe correlation. MAC join is authoritative.
  - `backends/` — `SwitchingBackend` ABC; `jadconfig_cisco.py` (production path); `amp_jpsw.py` (experimental).

## Key invariants

- **The switching sequence is sacred** (whitepaper + legacy-verified): `enable` → `configure` → `interface gi<RxPort>` → `switchport general allowed vlan remove <range=11-410>` → `switchport general allowed vlan add <TxVlan> untagged` → `end`. Tests assert it verbatim; do not "improve" it.
- Actions/menus only enqueue jobs; all CLI work happens on the concurrent thread. One CLI session, lock-guarded.
- Indigo devices are keyed by `dev.address` = `mac:<mac>` (preferred) or `port:<ifname>` — user renames are always safe. Never delete user devices from code.
- Discovery never writes to the switch.
- Never use the justAPI `/command/cli` endpoint (JSON-breaking bug on B1.x firmware).

## Production environment

- Switch: Cisco **SG300-28PP** @ `10.66.4.3` (the homelab MCP labels it "SG350-av" — that label is wrong; `show system` says SG300-28PP), telnet, JADConfig-configured. VLAN 10 = all-devices ("JAP_10x10"), TX VLANs 11–20 on ports gi2–gi11, RX ports gi12–gi21. TX device IPs in per-VLAN /30s under 172.16.0.0/24; RX devices in 172.16.128.0/17 (scan capped to /24). Real sanitized captures live in `tests/fixtures/*_real.txt`.
- Measured: `show running-config` ≈ 11s, `show vlan` ≈ 0.7s — hence the show-vlan routing poll.
- Deploy: `deploy_indigo_plugin_to_server.sh "Just Add Power.indigoPlugin" <repo-dir>` then `indigo-restart-plugin com.vtmikel.justaddpower` (see global CLAUDE.md). Check `…/Logs/com.vtmikel.justaddpower/plugin.log`, not Events.txt.
- Legacy cleanup plan and cutover checklist: see the approved plan (legacy devices/action groups/variables on the prod server must be re-pointed/removed at cutover).

## Release gating

- Working remote is Gitea `mike/indigo-just-add-power` (branch workflow: feature branch → PR → merge, never commit to `main` directly after the initial scaffold).
- **GitHub (`mlamoure/Indigo-Just-Add-Power`) is publish-on-public-release only** — after Gitea, live testing, and Mike's explicit review/approval. Never push there before that gate.

## Testing conventions

- All tests in `tests/`; `conftest.py` installs the `indigo` stub into `sys.modules` **before** plugin imports and prepends the Server Plugin dir to `sys.path`.
- Telnet behavior is tested against a scripted `FakeTransport` (no sockets, no sleeps). Backend command emission is asserted against a `FakeCli`. HTTP against `FakeHttp`.
- Fixture running-configs live in `tests/fixtures/` — including (once captured) the real, sanitized SG350 output. When editing the parser, run against the real fixture.
