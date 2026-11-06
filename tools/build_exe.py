"""Build the GUI as one small exe (about 9.5 MB) with PyInstaller.

usage: py -3.13 tools/build_exe.py [--data-home <folder>] [--out dist]

--data-home bakes data_home.txt into the exe so reports/backups go to that folder (the
owner's own build). Leave it out for the public build: data then goes to Documents\\YetiDiag.

Kept small on purpose (GitHub's browser upload and quick downloads): numpy, pytest and
others got pulled in and made it 25 MB. Each exclusion was checked with a console build
running every GUI command on the simulator (2026-11-06). Do NOT exclude asyncio, decimal
or sqlite3 (python-can imports them at start), and keep can.interfaces.virtual: python-can
loads it by name, so without it the exe's SIM mode fails ("No module named
can.interfaces.virtual") — the 25 MB build had that bug.
"""
import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDE = ["numpy", "ssl", "_ssl", "_hashlib", "pytest", "_pytest", "pygments", "xdist", "execnet",
           "pluggy", "iniconfig", "bleak", "winrt", "pydoc", "doctest", "pdb", "lzma", "_lzma",
           "bz2", "_bz2", "unicodedata", "yeti_diag.ble"]
HIDDEN = ["serial", "serial.tools.list_ports", "can.interfaces.virtual"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-home", help="folder for reports/backups (owner's build only)")
    ap.add_argument("--out", default=str(ROOT / "dist"))
    a = ap.parse_args()
    work = Path(tempfile.mkdtemp(prefix="yeti_build_"))
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--onefile", "--noconsole",
           "--name", "YetiDiag", "--paths", str(ROOT / ".deps"), "--paths", str(ROOT),
           "--add-data", f"{ROOT / 'profiles'};profiles", "--collect-submodules", "yeti_diag",
           "--distpath", a.out, "--workpath", str(work), "--specpath", str(work)]
    if a.data_home:
        hint = work / "data_home.txt"
        hint.write_text(a.data_home, encoding="utf-8")
        cmd += ["--add-data", f"{hint};."]
    for m in HIDDEN:
        cmd += ["--hidden-import", m]
    for m in EXCLUDE:
        cmd += ["--exclude-module", m]
    cmd.append(str(ROOT / "tools" / "gui.py"))
    subprocess.run(cmd, check=True)
    exe = Path(a.out) / "YetiDiag.exe"
    print(f"{exe}  {exe.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
