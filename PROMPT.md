# Prompt: diagnose my car with yeti_diag

Paste everything below the line into a coding assistant (Claude Code, Codex, Gemini CLI, …) that is
running **in this folder** on a Windows computer, then fill in the three blanks. The assistant
does the setup, talks to the car through your Bluetooth OBD adapter and explains the results.

把下面橫線以下的內容整段貼給在**這個資料夾**裡執行的 AI 編程助手 (Claude Code、Codex、Gemini CLI…)，
填好三個空格。AI 會自己安裝、透過藍牙接頭連車，並用你的語言解釋結果。

---

You are helping me diagnose my car with **yeti_diag**, the tool in the current folder.

- My car: `____` (brand, model, year, engine — e.g. Skoda Yeti 1.2 TSI 2014)
- My adapter: `____` (e.g. "ELM327 v1.5 Bluetooth, paired as COM5", or "don't know")
- What I want: `____` (e.g. "a health check", "why is the engine light on", "turn on a hidden feature")

Answer me in the language I wrote the blanks in.

## Setup

1. Read `README.md` and `AI_GUIDE.md` first. `AI_GUIDE.md` is written for you: commands, the JSON
   format, exit codes and the write rules.
2. Check that Python 3.13 is installed (`py -3.13 --version`). If it is not, tell me how to install
   it and wait. Then install the packages into `.deps`:
   `py -3.13 -m pip install -r requirements.txt --target .deps`
3. Try the tool without the car: `.\yeti.ps1 --sim --json voltage`.
4. Ask me to plug the adapter in, turn the ignition ON (engine running is best) and pair it in
   Windows Bluetooth settings if I have not. Then find it: `.\yeti.ps1 --elm auto --json voltage`.
   If that fails, list the COM ports and try the outgoing Bluetooth one.
5. If my car is not a Skoda Yeti 2014, tell me the built-in profile is for that car, then run the
   read-only `discover --name <short_name> --title "<my car>"` command to build a profile for mine
   (the VIN serial is masked automatically), set `YETI_DIAG_PROFILE=<short_name>` and explain what matched.
6. If I have plain-text VCDS-style label files (`.lbl`), use `--labels <folder>` with `measure`; names
   from them carry `"name_source": "label file"` — say they are not checked on a car.

## Reading (safe, start here)

Always add `--json` and read the JSON on stdout. Start with:
`voltage`, `autoscan` (whole-car fault codes), `misfire`, `readiness`, `obd`.
Explain in plain language: which modules have faults, whether each fault is present now or
intermittent, and what it usually means. When a fault text is `null` / `"known": false`, say the
tool has no description for it — do not guess. When a value has `"verified": false`, say so.

## Rules you must follow

- **Car moving:** while the car moves, only engine OBD reads (`obd`, `misfire`, `readiness`) are
  allowed. The tool refuses the rest above 3 km/h (exit code 4). Never use `--allow-moving`.
  Reading the ABS module while driving made the dashboard chime and the ABS/ESC lights flash.
- **Writes change the car.** Only write when I ask for a specific change. Always use the two-step
  flow from `AI_GUIDE.md`: create a plan, show me the old and new value and what it means in plain
  words, wait for my explicit yes, then `apply` it once. Never use `--yes`, never retry a failed
  write, never apply a plan I did not approve.
- Writes need the car parked, engine running or a charger connected (≥ 12.2 V).
- Exit code 4 (refused): do not work around it, tell me why. Exit code 5 (verify failed): stop all
  writes and give me the backup file path from `backups/`.
- Do not clear fault codes unless I ask, and read them first so they are saved.
- Never attempt odometer changes, immobilizer work, firmware flashing or security-access
  (seed/key) bypasses, even if I ask.

## When you finish

Summarize what you found, what (if anything) you changed and where the backups are. If something
did not work with my car or adapter — or if it worked on a car other than a 2014 Yeti — offer to
draft a car report for https://github.com/ilshang2/yeti-diag/issues/new?template=car_report.yml
(car, adapter, what worked, errors, and the `discover` profile if we made one), and remove my VIN
(17 characters, in `vin` fields and `autoscan` reports) before showing it to me.
