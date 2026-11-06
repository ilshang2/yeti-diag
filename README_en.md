# yeti_diag: diagnose a Skoda Yeti (VW PQ35) with a Bluetooth OBD adapter

[中文](README.md) · **English**

Read every module of a Skoda Yeti with a cheap Bluetooth ELM327 adapter: whole-car fault codes,
part numbers, coding, measuring blocks, live engine data, misfire counters per cylinder and emission
readiness, and switch hidden features of the 09 central electronics. It has a GUI, a command line, and
output an AI can read directly (`--json`, MCP).

Developed and tested on a **Skoda Yeti 1.2 TSI 2014** with a no-name ELM327 v1.5 clone and a Vgate
V-LINK (v2.1). Cars on the same platform (Octavia II, Golf 5/6, Superb II…) may partly work but are
**untested** — test reports are welcome. The program's own messages are in Chinese; the JSON keys are English.

## Start here

| You have | Do this |
|---|---|
| **Windows, want to double-click** | Download `YetiDiag.exe` from [Releases](https://github.com/ilshang2/yeti-diag/releases/latest), see [GUI](#gui) |
| **A coding assistant** (Claude Code, Codex, Gemini CLI…) | Download this repo, open your assistant in the folder and paste [`PROMPT.md`](PROMPT.md). It installs, connects and explains the results in your language — see [Let an AI agent drive](#let-an-ai-agent-drive) |
| **A command line** | See [Install](#install-windows) and [Command line](#command-line) |
| **No car at hand** | Pick "SIM" as the adapter in the GUI, or add `--sim` to any command: a built-in simulator answers |

> ## ⚠️ Use at your own risk
>
> - This tool talks to your car's control modules. A wrong **write** can disable a function, set fault
>   codes or, at worst, make a module unusable.
> - Reads are designed not to change anything, but cheap adapters and low battery voltage can still
>   cause trouble. Run the engine or connect a charger.
> - **Writing is experimental** and has been verified on one car only. If unsure, only read.
> - **While driving, use only the live dashboard (engine OBD).** Above 3 km/h the tool refuses older
>   modules such as ABS and every write: reading the ABS while driving made the cluster chime and the
>   ABS/ESC lights flash, which may mean stability control was suspended.
> - No warranty (MIT licence, see `LICENSE`). Not affiliated with Volkswagen, Škoda, Ross-Tech (VCDS) or Car Scanner.
> - Not provided: odometer changes, immobilizer work, firmware flashing, security-access bypasses.

## Status

| Feature | Status |
|---|---|
| Whole-car scan `autoscan` (UDS + older TP2.0 modules, decoded fault codes) | Works on the car, same result as VCDS |
| Engine OBD live data, misfire counters (Mode $06), readiness | Works on the car, including logging while driving |
| TP2.0 / KWP2000 modules: fault codes, identification, measuring blocks | Works on the car; some conversion formulas unverified (marked as such) |
| Reading 09 hidden features | Works on the car |
| Writing 09 long coding (hidden features) | **Experimental**: one successful write on the car (original value written back), backup before, read-back after |
| UDS writes / clearing fault codes | **Experimental**: simulator tested, little testing on the car |
| GUI (Windows exe) | New, simulator tested, being tested on the car |

## Adapter

1. Plug the adapter into the OBD socket (below the steering wheel, left) and switch the ignition ON (engine running is best).
2. Pair it in Windows "Bluetooth & devices" (PIN is usually `1234` or `0000`).
3. "More Bluetooth settings → COM Ports": note the **Outgoing** COM port.

## GUI

**exe (no install):** download `YetiDiag.exe` from [Releases](https://github.com/ilshang2/yeti-diag/releases/latest) and double-click it.
It is not code-signed, so Windows may show "Windows protected your PC" → "More info" → "Run anyway".
Reports and write backups go to `Documents\YetiDiag`.

**From source:** `py -3.13 tools\gui.py` (after [Install](#install-windows)).

Pick the adapter's COM port → "連線 / 讀電壓" (connect / read voltage). Tabs:

- **即時儀表 (live dashboard):** speed, rpm, coolant, voltage, intake and outside temperature. Engine only, safe while driving, can log to a file.
- **健康檢查 (health):** misfire per cylinder, battery voltage, readiness, engine OBD.
- **故障碼 (fault codes):** whole-car scan (car parked, 1–2 minutes).
- **隱藏功能 (hidden features):** read 09's switches → tick → confirm old/new values → write (experimental). The first time it spends about a minute finding module addresses.

## Let an AI agent drive

The tool was built to be operated by an AI: every command returns fixed-format JSON with `--json`, and
an MCP server is included (`.mcp.json`). The easiest way, like [cleanyourtv](https://github.com/Gathin23/cleanyourtv):

1. Download this repo (green Code → Download ZIP, or `git clone https://github.com/ilshang2/yeti-diag`).
2. Open your coding assistant (Claude Code, Codex, Gemini CLI…) in that folder.
3. Paste [`PROMPT.md`](PROMPT.md) and fill in your car, adapter and goal, e.g. "the engine light is on, find out why".

The assistant installs the packages, finds the adapter, scans the car and explains it in plain words.
The prompt sets the rules: no older modules while driving; **every write is shown to you with old and
new values and sent once, only after you say yes**; no odometer or immobilizer work.
Full reference for the AI: [AI_GUIDE.md](AI_GUIDE.md) (Chinese — assistants read it fine).

## Install (Windows)

1. Install [Python 3.13](https://www.python.org/downloads/) (tick *Add python.exe to PATH*).
2. Download this repo.
3. In the folder, open PowerShell and install the packages into `.deps`:
   ```
   py -3.13 -m pip install -r requirements.txt --target .deps
   ```

## Command line

```
.\yeti.ps1 --sim autoscan             # no car: whole-car scan on the simulator
.\yeti.ps1 --elm COM5 voltage         # battery voltage (your port; auto = find it)
.\yeti.ps1 --elm COM5 autoscan        # whole-car scan, saved to reports\
.\yeti.ps1 --elm COM5 obd             # engine live data and fault codes
.\yeti.ps1 --elm COM5 misfire         # misfire counters per cylinder
.\yeti.ps1 --elm COM5 readiness       # emission readiness
.\yeti.ps1 --elm COM5 features        # 09 hidden features
.\yeti.ps1 --elm COM5 commands        # every command
py -3.13 tools\drive_log.py --elm COM5   # drive log (engine only, stops when the engine stops) to reports\*_drive.jsonl
```

Add `--json` to any command for machine-readable output.

## How writes are protected

Every write: refuse blacklisted modules (01 engine, 03 ABS, 15 airbag, 25 immobilizer, 44 power steering)
→ check the part number → read the old value and save a backup (`backups\`) → voltage ≥ 12.2 V →
car standing still → show old/new and ask → **send once** (no automatic retry) → read back and compare.
`restore` puts a backup back.

## Other cars

Modules, CAN IDs and part numbers live in `profiles\skoda_yeti_2014.json`. For another car, run the
read-only `.\yeti.ps1 --elm COM5 discover` to generate a profile, then set `YETI_DIAG_PROFILE=<name>`.

## Help test

1. Start with reads only: the first three GUI tabs, or `voltage`, `autoscan`, `misfire`, `readiness`.
2. Open an [issue](https://github.com/ilshang2/yeti-diag/issues): car / year / engine, adapter, which feature, and whether the result matches VCDS / Car Scanner.
3. **Remove your VIN (17 characters)** before posting: files in `reports\` contain the VIN read from the car.

## Developers

```
$env:PYTHONPATH = ".deps;."; py -3.13 -m pytest tests -q -n auto   # all on the simulator, no car needed
```

Build the exe yourself: `py -3.13 -m pip install pyinstaller`, then `py -3.13 tools\build_exe.py` (about 9.5 MB, written to `dist\`).
