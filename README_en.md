# yeti_diag: VAG diagnostics over a cheap Bluetooth ELM327, built for AI agents

Command-line diagnostics for VW group cars (developed on a Skoda Yeti 1.2 TSI 2014, PQ35)
through an ordinary ELM327 Bluetooth adapter. The goal is to cover what VCDS is used for
day to day, with output an AI model can read and act on directly: every command can print
one JSON document with fixed exit codes, and an MCP server exposes the commands as tools.

> **Status (2026-11-03):** reading is tested on the car: all 14 modules (UDS and VW TP2.0 /
> KWP2000), fault codes, identification, coding, measuring blocks, odometer. One write (clearing
> a door module's fault memory) was done on the car through the plan → approve → apply flow.
> Coding writes for TP2.0 modules are not implemented yet.

## Disclaimer

**Use at your own risk.** This tool talks to safety-relevant control units. A wrong write can
disable functions, set faults or, in the worst case, make a module unusable. Reading is
designed to be harmless, but cheap adapters and weak batteries can still cause trouble.
The authors accept no liability (see LICENSE). Do not use it while driving.
Not affiliated with Volkswagen AG, Škoda Auto, Ross-Tech (VCDS) or Car Scanner.

## What it does

| Command | VCDS equivalent | Notes |
|---|---|---|
| `autoscan` | Auto-Scan | all modules: part number, coding, fault codes with status (present / intermittent) |
| `dtc 17 [--details]`, `kwp-dtc 62` | Fault Codes | `--details`: freeze frame / extended data, raw |
| `measure 02 122` | Measuring Blocks | values with units; names where confirmed on the car |
| `values` | — | confirmed values such as the odometer |
| `clear-dtc 72` | Fault Codes – Clear | write: plan first, applied only after approval, sent once |
| `verify` | — | self-check against a VCDS reference reading |
| `profile` | — | active vehicle profile (`profiles/*.json`) |

Run `python -m yeti_diag commands --json` for the full catalog. See `AI_GUIDE.md` (Chinese)
for the JSON envelope, exit codes, write rules and the MCP server.

## Safety design

- Reading commands can only send reading services; anything else is refused before it goes out.
- Writes: never on engine, ABS, airbag, immobilizer or steering modules; part number must match the
  profile; old value backed up; battery ≥ 12.2 V; the human sees old and new values and approves;
  sent once, never retried; read back and compared.
- In `--json` mode a write only produces a plan; `apply <id>` sends it (valid 15 minutes, once,
  and only on the target it was planned for — a simulator plan cannot be applied to a car).

## Other cars

Vehicle data (modules, CAN IDs, part numbers, confirmed values, value names) lives in
`profiles/<name>.json`. Copy one, adapt it, select it with `YETI_DIAG_PROFILE=<name>`, run
`profile` to check it, then `probe`, `tp20-probe` and `verify` on the car.

Value names are learned by watching other tools on your own car (for example the Android
Bluetooth HCI snoop log while Car Scanner reads, see `tools/btsnoop_elm.py`) and matching
what they show with the raw values. Their databases and label files are not copied.

## Running

```
pip install -r requirements.txt
python -m yeti_diag --sim-elm autoscan          # simulator, no car needed
python -m yeti_diag --elm COM9 voltage          # ELM327 on COM9 (Bluetooth SPP)
python -m yeti_diag --elm COM9 --json autoscan  # JSON for scripts and AI agents
python -m pytest tests -q -n auto               # simulator tests, in parallel
```

BLE-only adapters (no COM port on Windows): `--elm BLE:<name or address>`, find it with `ble-scan` (not yet tried on a real BLE-only adapter). Other VW-group cars: `discover --name <key>` scans the modules and writes a starting profile.

## License

MIT, see `LICENSE`.
