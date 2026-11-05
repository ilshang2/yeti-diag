"""Where reports, backups and plans live.

Running from source they sit next to the package (the project folder), as before. A
PyInstaller exe unpacks itself into a temporary folder that is deleted when it closes,
so anything written there (write backups!) would be lost and the probed TP2.0 address
map would be missing. Frozen, the data folder is therefore:
  1. YETI_DIAG_HOME, if set;
  2. the project folder recorded at build time in the bundled `data_home.txt`, if it exists;
  3. Documents\\YetiDiag.
"""
import os
import pathlib
import sys

CODE_ROOT = pathlib.Path(__file__).resolve().parent.parent


def data_root():
    env = os.environ.get("YETI_DIAG_HOME")
    if env:
        return pathlib.Path(env)
    if not getattr(sys, "frozen", False):
        return CODE_ROOT
    hint = CODE_ROOT / "data_home.txt"
    if hint.exists():
        recorded = pathlib.Path(hint.read_text(encoding="utf-8").strip())
        if recorded.is_dir():
            return recorded
    return pathlib.Path.home() / "Documents" / "YetiDiag"


DATA_ROOT = data_root()
